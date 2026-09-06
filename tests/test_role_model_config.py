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
from sova.llm.client import _ROLE_MODEL_FIELDS, resolve_model
from sova.llm.complexity import ComplexityTier
from sova.llm.models import LLMResult
from sova.llm.routing import route_model
from sova.roles.panel_review import _estimate_dimension_cost, run_panel_review
from sova.roles.reviewer import ReviewerRole
from sova.supervisor.planner import _DEFAULT_MODEL, SupervisorPlanner


def _task() -> Task:
    return Task(id="42", title="Test issue", body="Some description", state=TaskState.IN_REVIEW)


def _review_ctx(config: ProjectConfig | None = None) -> ExecutionContext:
    adapter = AsyncMock()
    adapter.get_state.return_value = TaskState.IN_REVIEW
    return ExecutionContext(
        project_dir=Path("/tmp/test"),
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
    async def test_stock_config_uses_sonnet(self) -> None:
        ctx = _review_ctx()
        role = ReviewerRole()
        with patch("sova.roles.reviewer.invoke", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.return_value = LLMResult(text=_findings_response(), model="sonnet")
            await role._run_review(ctx, _task(), "diff --git a/a.py b/a.py\n+x", ["a.py"])

        assert mock_invoke.call_args[1]["model"] == "sonnet"

    async def test_configured_reviewer_model_is_used(self) -> None:
        config = ProjectConfig(roles=RolesConfig(reviewer_model="haiku"))
        ctx = _review_ctx(config)
        role = ReviewerRole()
        with patch("sova.roles.reviewer.invoke", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.return_value = LLMResult(text=_findings_response(), model="haiku")
            await role._run_review(ctx, _task(), "diff --git a/a.py b/a.py\n+x", ["a.py"])

        assert mock_invoke.call_args[1]["model"] == "haiku"

    async def test_role_config_currently_wins_over_task_type_routing(self) -> None:
        """Locks in the documented precedence gap: llm.routing[task_type] is inert

        today because invoke() is always called with an explicit, already-resolved
        ``model=``. This must be updated deliberately when PR7 reworks
        ``_resolve_task_type_model`` to consult config even when model is set.
        """
        config = ProjectConfig(roles=RolesConfig(reviewer_model="opus"), llm=LLMConfig(routing={"review": "haiku"}))
        ctx = _review_ctx(config)
        role = ReviewerRole()
        with patch("sova.roles.reviewer.invoke", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.return_value = LLMResult(text=_findings_response(), model="opus")
            await role._run_review(ctx, _task(), "diff --git a/a.py b/a.py\n+x", ["a.py"])

        assert mock_invoke.call_args[1]["model"] == "opus"

    async def test_review_passes_task_type(self) -> None:
        ctx = _review_ctx()
        role = ReviewerRole()
        with patch("sova.roles.reviewer.invoke", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.return_value = LLMResult(text=_findings_response(), model="sonnet")
            await role._run_review(ctx, _task(), "diff --git a/a.py b/a.py\n+x", ["a.py"])

        assert mock_invoke.call_args[1]["task_type"] == "review"

    async def test_schema_retry_uses_same_model(self) -> None:
        """The retry closure must not fall back to the hardcoded literal."""
        config = ProjectConfig(roles=RolesConfig(reviewer_model="haiku"))
        ctx = _review_ctx(config)
        role = ReviewerRole()
        responses = [
            LLMResult(text="not json at all", model="haiku"),
            LLMResult(text=_findings_response(), model="haiku"),
        ]
        with patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, side_effect=responses) as mock_invoke:
            await role._run_review(ctx, _task(), "diff --git a/a.py b/a.py\n+x", ["a.py"])

        assert mock_invoke.await_count == 2
        assert [call[1]["model"] for call in mock_invoke.call_args_list] == ["haiku", "haiku"]

    async def test_all_chunks_use_one_model(self) -> None:
        from sova.roles.reviewer import DIFF_CHUNK_SIZE

        config = ProjectConfig(roles=RolesConfig(reviewer_model="haiku"))
        ctx = _review_ctx(config)
        role = ReviewerRole()
        big_diff = "diff --git a/a.py b/a.py\n" + "+" * DIFF_CHUNK_SIZE + "\ndiff --git a/b.py b/b.py\n" + "+" * 100
        with patch("sova.roles.reviewer.invoke", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.return_value = LLMResult(text=_findings_response(), model="haiku")
            await role._run_review(ctx, _task(), big_diff, ["a.py", "b.py"])

        assert mock_invoke.await_count == 2
        assert {call[1]["model"] for call in mock_invoke.call_args_list} == {"haiku"}

    async def test_panel_path_receives_resolved_default_model(self) -> None:
        config = ProjectConfig(roles=RolesConfig(reviewer_model="haiku"))
        config.review.panel.enabled = True
        ctx = _review_ctx(config)
        role = ReviewerRole()
        with patch("sova.roles.panel_review.run_panel_review", new_callable=AsyncMock) as mock_panel:
            mock_panel.return_value = MagicMock(total_cost=Decimal("0"))
            await role._run_review(ctx, _task(), "diff --git a/a.py b/a.py\n+x", ["a.py"])

        assert mock_panel.call_args[1]["default_model"] == "haiku"


# ---------------------------------------------------------------------------
# Panel review default model
# ---------------------------------------------------------------------------


class TestPanelDefaultModel:
    async def test_default_model_used_when_no_dimension_pin(self) -> None:
        panel_config = ReviewPanelConfig(enabled=True, dimensions=["correctness"])
        with patch("sova.roles.panel_review.invoke", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.return_value = LLMResult(text=_findings_response(), model="haiku")
            await run_panel_review(
                task=_task(),
                diff="small diff",
                files=["a.py"],
                panel_config=panel_config,
                default_model="haiku",
            )

        assert mock_invoke.call_args[1]["model"] == "haiku"

    async def test_stock_default_is_sonnet(self) -> None:
        panel_config = ReviewPanelConfig(enabled=True, dimensions=["correctness"])
        with patch("sova.roles.panel_review.invoke", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.return_value = LLMResult(text=_findings_response(), model="sonnet")
            await run_panel_review(
                task=_task(),
                diff="small diff",
                files=["a.py"],
                panel_config=panel_config,
            )

        assert mock_invoke.call_args[1]["model"] == "sonnet"

    async def test_dimension_pin_wins_over_default(self) -> None:
        panel_config = ReviewPanelConfig(
            enabled=True, dimensions=["correctness"], dimension_models={"correctness": "opus"}
        )
        with patch("sova.roles.panel_review.invoke", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.return_value = LLMResult(text=_findings_response(), model="opus")
            await run_panel_review(
                task=_task(),
                diff="small diff",
                files=["a.py"],
                panel_config=panel_config,
                default_model="haiku",
            )

        assert mock_invoke.call_args[1]["model"] == "opus"

    def test_unrecognized_model_keeps_cost_fallback(self) -> None:
        assert _estimate_dimension_cost("ollama/llama3") == Decimal("0.01")

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
        assert _estimate_dimension_cost(model) == expected


# ---------------------------------------------------------------------------
# Supervisor planner
# ---------------------------------------------------------------------------


class TestPlannerModelResolution:
    def _planner(self, roles: RolesConfig | None = None) -> SupervisorPlanner:
        config = ProjectConfig(
            supervisor=SupervisorConfig(enabled=True, llm_planning=True),
            github_repo="test/repo",
            roles=roles or RolesConfig(),
        )
        return SupervisorPlanner(config=config, project_dir=Path("/tmp/test"), session_factory=MagicMock())

    async def test_stock_config_uses_default_model(self) -> None:
        planner = self._planner()
        mock_result = LLMResult(text='{"reasoning": "x", "actions": []}', model=_DEFAULT_MODEL)
        with patch("sova.supervisor.planner.invoke", new_callable=AsyncMock, return_value=mock_result) as mock_invoke:
            await planner._call_llm("system", "user", planner._resolve_model())

        assert mock_invoke.call_args[1]["model"] == _DEFAULT_MODEL

    async def test_configured_planner_model_is_used(self) -> None:
        planner = self._planner(RolesConfig(planner_model="opus"))
        mock_result = LLMResult(text='{"reasoning": "x", "actions": []}', model="opus")
        with patch("sova.supervisor.planner.invoke", new_callable=AsyncMock, return_value=mock_result) as mock_invoke:
            await planner._call_llm("system", "user", planner._resolve_model())

        assert mock_invoke.call_args[1]["model"] == "opus"

    def test_empty_planner_model_falls_back_to_literal(self) -> None:
        planner = self._planner(RolesConfig(planner_model=""))
        assert planner._resolve_model() == _DEFAULT_MODEL
