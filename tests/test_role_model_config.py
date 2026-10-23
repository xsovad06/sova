"""Tests for role-scoped model config (reviewer, developer, planner).

Covers the new ``RolesConfig`` fields, their registration in
``_ROLE_MODEL_FIELDS`` and the settings UI, and per-tier parity: under stock
config the reviewer, panel, and supervisor planner must resolve to exactly the
models they used before role config existed.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.adapters.base import Task, TaskState
from sova.config.models import LLMConfig, ProjectConfig, ReviewPanelConfig, RolesConfig, SupervisorConfig
from sova.core.context import ExecutionContext
from sova.dashboard.settings_meta import get_meta
from sova.llm.client import _ROLE_MODEL_FIELDS, _resolve_task_type_model, resolve_model
from sova.llm.complexity import ComplexityTier
from sova.llm.models import LLMResult
from sova.llm.routing import route_model
from sova.roles.panel_review import _estimate_call_cost, _group_dimensions_by_model, run_panel_review
from sova.roles.reviewer import ReviewerRole
from sova.supervisor.planner import _DEFAULT_MODEL, SupervisorPlanner


def _task() -> Task:
    return Task(id="42", title="Test issue", body="Some description", state=TaskState.IN_REVIEW)


def _review_ctx(tmp_path: Path, config: ProjectConfig | None = None) -> ExecutionContext:
    adapter = AsyncMock()
    adapter.get_state.return_value = TaskState.IN_REVIEW
    return ExecutionContext(
        project_dir=tmp_path,
        config=config or ProjectConfig(),
        adapter=adapter,
        issue_number="42",
        role="reviewer",
        pr_number=99,
    )


def _findings_response(summary: str = "OK") -> str:
    return json.dumps({"findings": [], "summary": summary})


# ---------------------------------------------------------------------------
# RolesConfig defaults
# ---------------------------------------------------------------------------


class TestRolesConfigDefaults:
    def test_reviewer_model_defaults_to_sonnet(self) -> None:
        assert RolesConfig().reviewer_model == "sonnet"

    def test_developer_model_defaults_to_empty(self) -> None:
        """Empty so complexity routing keeps working for the developer role."""
        assert RolesConfig().developer_model == ""

    def test_planner_model_defaults_to_sonnet(self) -> None:
        assert RolesConfig().planner_model == "sonnet"

    def test_env_prefix_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOVA_ROLES_REVIEWER_MODEL", "haiku")
        assert RolesConfig().reviewer_model == "haiku"


class TestSettingsMetaRegistration:
    @pytest.mark.parametrize("key", ["roles.reviewer_model", "roles.developer_model", "roles.planner_model"])
    def test_registered_in_settings_ui(self, key: str) -> None:
        meta = get_meta(key)
        assert meta is not None
        assert meta.group == "roles"


# ---------------------------------------------------------------------------
# resolve_model() lookup table
# ---------------------------------------------------------------------------


class TestResolveModelRoleFields:
    def test_review_and_reviewer_map_to_same_field(self) -> None:
        assert _ROLE_MODEL_FIELDS["review"] == "reviewer_model"
        assert _ROLE_MODEL_FIELDS["reviewer"] == "reviewer_model"

    @pytest.mark.parametrize("role", ["review", "reviewer"])
    def test_reviewer_resolves_from_config(self, role: str) -> None:
        roles = RolesConfig(reviewer_model="haiku")
        assert resolve_model(role, roles) == ("haiku", f"role:{role}->haiku")

    def test_planner_resolves_from_config(self) -> None:
        roles = RolesConfig(planner_model="opus")
        assert resolve_model("planner", roles) == ("opus", "role:planner->opus")

    def test_developer_model_when_set_wins_over_complexity(self) -> None:
        roles = RolesConfig(developer_model="haiku")
        assert resolve_model("developer", roles, complexity=ComplexityTier.EPIC) == (
            "haiku",
            "role:developer->haiku",
        )

    def test_empty_role_field_falls_through_to_complexity(self) -> None:
        roles = RolesConfig(reviewer_model="")
        assert resolve_model("reviewer", roles, complexity=ComplexityTier.TRIVIAL) == (
            "haiku",
            "complexity:trivial->haiku",
        )

    def test_none_role_field_is_tolerated(self) -> None:
        """Mock-based tests set these fields to None rather than ''."""
        roles = MagicMock()
        roles.reviewer_model = None
        assert resolve_model("reviewer", roles, complexity=ComplexityTier.SIMPLE) == (
            "sonnet",
            "complexity:simple->sonnet",
        )


class TestPerTierParity:
    """Stock config must resolve exactly as it did before the new role fields."""

    @pytest.mark.parametrize("tier", list(ComplexityTier))
    def test_developer_matches_pure_complexity_routing(self, tier: ComplexityTier) -> None:
        llm_cfg = LLMConfig()
        assert resolve_model("developer", RolesConfig(), complexity=tier, llm_config=llm_cfg) == route_model(
            tier, llm_config=llm_cfg
        )

    @pytest.mark.parametrize("tier", list(ComplexityTier))
    def test_developer_matches_complexity_routing_with_pinned_agent_model(self, tier: ComplexityTier) -> None:
        llm_cfg = LLMConfig()
        assert resolve_model(
            "developer",
            RolesConfig(),
            complexity=tier,
            llm_config=llm_cfg,
            agent_model="claude-opus-4-6",
        ) == route_model(tier, llm_config=llm_cfg, agent_model="claude-opus-4-6")

    def test_reviewer_stock_config_resolves_to_sonnet(self) -> None:
        assert resolve_model("reviewer", RolesConfig()) == ("sonnet", "role:reviewer->sonnet")

    def test_planner_stock_config_resolves_to_sonnet(self) -> None:
        assert resolve_model("planner", RolesConfig()) == ("sonnet", "role:planner->sonnet")


# ---------------------------------------------------------------------------
# ReviewerRole single-review path
# ---------------------------------------------------------------------------


class TestReviewerModelResolution:
    def test_stock_config_uses_sonnet(self, tmp_path: Path) -> None:
        assert ReviewerRole()._resolve_review_model(_review_ctx(tmp_path)) == "sonnet"

    def test_configured_reviewer_model_is_used(self, tmp_path: Path) -> None:
        config = ProjectConfig(roles=RolesConfig(reviewer_model="haiku"))
        assert ReviewerRole()._resolve_review_model(_review_ctx(tmp_path, config)) == "haiku"

    def test_task_type_routing_outranks_role_config_at_invoke(self, tmp_path: Path) -> None:
        """Role config decides what the reviewer asks for; llm.routing decides what runs.

        ``roles.reviewer_model`` wins inside the role, so the reviewer resolves
        "opus". invoke() then runs that value through ``_resolve_task_type_model``
        (via ``prepare_invocation``), where a configured ``llm.routing["review"]``
        route outranks the explicit ``model=``. The model that actually runs is
        therefore the routed one, not the role's.
        """
        config = ProjectConfig(roles=RolesConfig(reviewer_model="opus"), llm=LLMConfig(routing={"review": "haiku"}))
        role_model = ReviewerRole()._resolve_review_model(_review_ctx(tmp_path, config))

        assert role_model == "opus"
        assert _resolve_task_type_model(role_model, "review", cfg=config) == "haiku"

    async def test_review_passes_task_type(self, tmp_path: Path) -> None:
        """task_type is only observable at the invoke boundary: the role records it nowhere else."""
        ctx = _review_ctx(tmp_path)
        role = ReviewerRole()
        with patch("sova.roles.reviewer.invoke", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.return_value = LLMResult(text=_findings_response(), model="sonnet")
            await role._run_review(ctx, _task(), "diff --git a/a.py b/a.py\n+x", ["a.py"])

        mock_invoke.assert_awaited_once()
        assert mock_invoke.call_args[1]["task_type"] == "review"

    async def test_schema_retry_uses_same_model(self, tmp_path: Path) -> None:
        """The retry closure must not fall back to the hardcoded literal.

        The model is resolved inline in ``_run_single_review`` and only reaches
        the retry closure, so the invoke boundary is the one place it is
        observable; it is compared against the resolver's own answer.
        """
        config = ProjectConfig(roles=RolesConfig(reviewer_model="haiku"))
        ctx = _review_ctx(tmp_path, config)
        role = ReviewerRole()
        resolved = role._resolve_review_model(ctx)
        responses = [
            LLMResult(text="not json at all", model="haiku"),
            LLMResult(text=_findings_response(), model="haiku"),
        ]
        with patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, side_effect=responses) as mock_invoke:
            await role._run_review(ctx, _task(), "diff --git a/a.py b/a.py\n+x", ["a.py"])

        assert resolved == "haiku"
        assert mock_invoke.await_count == 2
        assert [call[1]["model"] for call in mock_invoke.call_args_list] == [resolved, resolved]

    async def test_all_chunks_use_one_model(self, tmp_path: Path) -> None:
        """Same seam as the schema retry: per-chunk models exist only as invoke arguments."""
        from sova.roles.reviewer import DIFF_CHUNK_SIZE

        config = ProjectConfig(roles=RolesConfig(reviewer_model="haiku"))
        ctx = _review_ctx(tmp_path, config)
        role = ReviewerRole()
        resolved = role._resolve_review_model(ctx)
        big_diff = "diff --git a/a.py b/a.py\n" + "+" * DIFF_CHUNK_SIZE + "\ndiff --git a/b.py b/b.py\n" + "+" * 100
        with patch("sova.roles.reviewer.invoke", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.return_value = LLMResult(text=_findings_response(), model="haiku")
            await role._run_review(ctx, _task(), big_diff, ["a.py", "b.py"])

        assert resolved == "haiku"
        assert mock_invoke.await_count == 2
        assert {call[1]["model"] for call in mock_invoke.call_args_list} == {resolved}

    async def test_panel_path_receives_resolved_default_model(self, tmp_path: Path) -> None:
        """The hand-off to run_panel_review is the seam: the role passes its resolved model as an argument."""
        config = ProjectConfig(roles=RolesConfig(reviewer_model="haiku"))
        config.review.panel.enabled = True
        ctx = _review_ctx(tmp_path, config)
        role = ReviewerRole()
        resolved = role._resolve_review_model(ctx)
        with patch("sova.roles.panel_review.run_panel_review", new_callable=AsyncMock) as mock_panel:
            mock_panel.return_value = MagicMock(total_cost=Decimal("0"))
            await role._run_review(ctx, _task(), "diff --git a/a.py b/a.py\n+x", ["a.py"])

        assert resolved == "haiku"
        mock_panel.assert_awaited_once()
        assert mock_panel.call_args[1]["default_model"] == resolved


# ---------------------------------------------------------------------------
# Provider-aware model fallback (issue #1126)
# ---------------------------------------------------------------------------


class TestReviewerModelProviderAwareFallback:
    """A bare Claude family alias ("sonnet", "opus", ...) is not a real model ID for
    any backend other than Claude Code / the Anthropic API's own alias table. A
    non-Claude llm.provider must fall back to its own configured llm.model instead."""

    def test_stock_reviewer_model_falls_back_to_llm_model_for_openai(self, tmp_path: Path) -> None:
        config = ProjectConfig(llm=LLMConfig(provider="openai", model="gpt-5"))
        assert ReviewerRole()._resolve_review_model(_review_ctx(tmp_path, config)) == "gpt-5"

    def test_explicit_reviewer_model_alias_resolves_to_its_own_vertex_candidate(self, tmp_path: Path) -> None:
        """An explicitly configured but still-bare alias ("opus") is resolved through
        resolve_alias()'s own Vertex tier-candidate table rather than being silently
        replaced by the unrelated general-purpose llm.model."""
        config = ProjectConfig(
            roles=RolesConfig(reviewer_model="opus"),
            llm=LLMConfig(provider="vertex", model="vertex_ai/gemini-2.5-pro"),
        )
        resolved = ReviewerRole()._resolve_review_model(_review_ctx(tmp_path, config))
        assert resolved == "vertex_ai/claude-opus-4-6@20260401"

    def test_non_alias_reviewer_model_is_not_overridden(self, tmp_path: Path) -> None:
        """A reviewer_model that is already a concrete, non-alias ID passes through
        unchanged regardless of provider."""
        config = ProjectConfig(
            roles=RolesConfig(reviewer_model="gpt-4o-mini"),
            llm=LLMConfig(provider="openai", model="gpt-5"),
        )
        assert ReviewerRole()._resolve_review_model(_review_ctx(tmp_path, config)) == "gpt-4o-mini"

    def test_anthropic_provider_keeps_bare_alias(self, tmp_path: Path) -> None:
        """The Anthropic API provider resolves bare aliases itself (resolve_model_alias),
        so it is excluded from the override."""
        config = ProjectConfig(llm=LLMConfig(provider="anthropic", model="claude-sonnet-5"))
        assert ReviewerRole()._resolve_review_model(_review_ctx(tmp_path, config)) == "sonnet"

    def test_claude_code_provider_keeps_bare_alias(self, tmp_path: Path) -> None:
        config = ProjectConfig(llm=LLMConfig(provider="claude-code"))
        assert ReviewerRole()._resolve_review_model(_review_ctx(tmp_path, config)) == "sonnet"


# ---------------------------------------------------------------------------
# Panel review default model
# ---------------------------------------------------------------------------


class TestPanelDefaultModel:
    """Combined-mode grouping resolves each dimension's model in ``_group_dimensions_by_model``.

    The per-dimension path (``_review_dimensions``, which a single-dimension
    group also takes) re-resolves the same value inline and records it nowhere,
    so each scenario additionally checks the model that path hands to invoke.
    """

    async def test_default_model_used_when_no_dimension_pin(self) -> None:
        panel_config = ReviewPanelConfig(enabled=True, dimensions=["correctness"])
        assert _group_dimensions_by_model(["correctness"], panel_config, "haiku") == [("haiku", ["correctness"])]

        with patch("sova.roles.panel_review.invoke", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.return_value = LLMResult(text=_findings_response(), model="haiku")
            await run_panel_review(
                task=_task(),
                diff="small diff",
                files=["a.py"],
                panel_config=panel_config,
                default_model="haiku",
            )

        mock_invoke.assert_awaited_once()
        assert mock_invoke.call_args[1]["model"] == "haiku"

    async def test_stock_default_is_sonnet(self) -> None:
        panel_config = ReviewPanelConfig(enabled=True, dimensions=["correctness"])
        assert _group_dimensions_by_model(["correctness"], panel_config) == [("sonnet", ["correctness"])]

        with patch("sova.roles.panel_review.invoke", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.return_value = LLMResult(text=_findings_response(), model="sonnet")
            await run_panel_review(
                task=_task(),
                diff="small diff",
                files=["a.py"],
                panel_config=panel_config,
            )

        mock_invoke.assert_awaited_once()
        assert mock_invoke.call_args[1]["model"] == "sonnet"

    async def test_dimension_pin_wins_over_default(self) -> None:
        panel_config = ReviewPanelConfig(
            enabled=True, dimensions=["correctness"], dimension_models={"correctness": "opus"}
        )
        assert _group_dimensions_by_model(["correctness"], panel_config, "haiku") == [("opus", ["correctness"])]

        with patch("sova.roles.panel_review.invoke", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.return_value = LLMResult(text=_findings_response(), model="opus")
            await run_panel_review(
                task=_task(),
                diff="small diff",
                files=["a.py"],
                panel_config=panel_config,
                default_model="haiku",
            )

        mock_invoke.assert_awaited_once()
        assert mock_invoke.call_args[1]["model"] == "opus"

    def test_unrecognized_model_keeps_cost_fallback(self) -> None:
        assert _estimate_call_cost("ollama/llama3") == Decimal("0.01")

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("opus", Decimal("0.05")),
            ("claude-opus-4-6", Decimal("0.05")),
            ("claude-haiku-4-5", Decimal("0.002")),
            ("claude-sonnet-4-5", Decimal("0.01")),
        ],
    )
    def test_pinned_versions_cost_like_their_family(self, model: str, expected: Decimal) -> None:
        """A pinned ``roles.reviewer_model`` must not be priced as sonnet by the budget gate."""
        assert _estimate_call_cost(model) == expected


# ---------------------------------------------------------------------------
# Supervisor planner
# ---------------------------------------------------------------------------


class TestPlannerModelResolution:
    def _planner(self, tmp_path: Path, roles: RolesConfig | None = None) -> SupervisorPlanner:
        config = ProjectConfig(
            supervisor=SupervisorConfig(enabled=True, llm_planning=True),
            github_repo="test/repo",
            roles=roles or RolesConfig(),
        )
        return SupervisorPlanner(config=config, project_dir=tmp_path, session_factory=MagicMock())

    def test_stock_config_uses_default_model(self, tmp_path: Path) -> None:
        assert self._planner(tmp_path)._resolve_model() == _DEFAULT_MODEL

    def test_configured_planner_model_is_used(self, tmp_path: Path) -> None:
        assert self._planner(tmp_path, RolesConfig(planner_model="opus"))._resolve_model() == "opus"

    def test_empty_planner_model_falls_back_to_literal(self, tmp_path: Path) -> None:
        planner = self._planner(tmp_path, RolesConfig(planner_model=""))
        assert planner._resolve_model() == _DEFAULT_MODEL
