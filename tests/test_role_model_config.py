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


# ---------------------------------------------------------------------------
# Fallbacks per role (#1463)
# ---------------------------------------------------------------------------

_DEFAULT_CHAIN = ["claude-sonnet-5-5", "claude-haiku-5-5"]


def _fallback_config(**roles: object) -> ProjectConfig:
    return ProjectConfig(agent={"model": "claude-opus-5-5", "fallback_models": list(_DEFAULT_CHAIN)}, roles=roles)


def _identity(model: str | None) -> str | None:
    return model


class TestRoleFallbackSettings:
    @pytest.mark.parametrize("role", ["triage", "researcher", "developer", "reviewer", "planner"])
    def test_each_role_has_a_fallback_setting_that_defaults_to_none(self, role: str) -> None:
        key = f"roles.{role}_fallback_models"
        assert getattr(RolesConfig(), f"{role}_fallback_models") is None
        meta = get_meta(key)
        assert meta is not None
        assert (meta.group, meta.value_type, meta.options_source) == ("roles", "list", "llm")

    def test_a_role_without_its_own_list_uses_the_default_fallbacks(self) -> None:
        assert _fallback_config().fallback_models_for("developer") == _DEFAULT_CHAIN

    def test_a_role_with_its_own_list_uses_it(self) -> None:
        cfg = _fallback_config(developer_fallback_models=["claude-haiku-5-5"])
        assert cfg.fallback_models_for("developer") == ["claude-haiku-5-5"]
        assert cfg.fallback_models_for("reviewer") == _DEFAULT_CHAIN

    def test_an_empty_list_means_no_fallbacks(self) -> None:
        assert _fallback_config(reviewer_fallback_models=[]).fallback_models_for("reviewer") == []

    @pytest.mark.parametrize("role", [None, "", "custom"])
    def test_no_role_or_a_role_without_a_field_uses_the_default_fallbacks(self, role: str | None) -> None:
        cfg = _fallback_config(developer_fallback_models=["x"])
        assert cfg.fallback_models_for(role) == _DEFAULT_CHAIN

    def test_the_doctor_enumeration_lists_role_fallback_entries(self) -> None:
        from sova.utils.diagnostics import configured_model_fields

        fields = dict(configured_model_fields(_fallback_config(triage_fallback_models=["claude-haiku-5-5"])))
        assert fields["roles.triage_fallback_models[0]"] == "claude-haiku-5-5"


class TestCandidateChainUsesTheRunningRole:
    """_build_candidate_chain() walks the running role's fallbacks; outside every role's run, the default."""

    def _chain(self, cfg: ProjectConfig, **kwargs: object) -> list[str | None]:
        from sova.llm.client import _build_candidate_chain

        return _build_candidate_chain("claude-opus-5-5", cfg, normalize=_identity, **kwargs)

    def test_a_role_with_its_own_fallbacks_uses_them(self) -> None:
        from sova.llm.client import running_role

        cfg = _fallback_config(developer_fallback_models=["claude-haiku-5-5"])
        with running_role("developer"):
            assert self._chain(cfg) == ["claude-opus-5-5", "claude-haiku-5-5"]

    def test_a_role_without_its_own_fallbacks_uses_the_default(self) -> None:
        from sova.llm.client import running_role

        cfg = _fallback_config(developer_fallback_models=["claude-haiku-5-5"])
        with running_role("reviewer"):
            assert self._chain(cfg) == ["claude-opus-5-5", *_DEFAULT_CHAIN]

    def test_outside_every_role_the_default_applies(self) -> None:
        cfg = _fallback_config(developer_fallback_models=["claude-haiku-5-5"])
        assert self._chain(cfg) == ["claude-opus-5-5", *_DEFAULT_CHAIN]

    def test_an_empty_role_list_leaves_only_the_primary(self) -> None:
        assert self._chain(_fallback_config(triage_fallback_models=[]), role="triage") == ["claude-opus-5-5"]

    def test_the_scope_ends_with_the_block(self) -> None:
        from sova.llm.client import current_role, running_role

        with running_role("triage"):
            with running_role("reviewer"):
                assert current_role() == "reviewer"
            assert current_role() == "triage"
        assert current_role() is None


class TestEngineFallbackWalkUsesTheRole:
    """WorkflowEngine's step fallback walk and the CLI --fallback-model read the run's role list."""

    def _ctx(self, tmp_path: Path, cfg: ProjectConfig, role: str = "developer") -> ExecutionContext:
        return ExecutionContext(project_dir=tmp_path, config=cfg, adapter=AsyncMock(), issue_number="42", role=role)

    def test_cli_fallback_model_is_the_roles_first_fallback(self, tmp_path: Path) -> None:
        cfg = _fallback_config(developer_fallback_models=["claude-haiku-5-5"])
        assert self._ctx(tmp_path, cfg).get_cli_fallback_model() == "claude-haiku-5-5"
        assert self._ctx(tmp_path, cfg, role="reviewer").get_cli_fallback_model() == "claude-sonnet-5-5"

    def test_has_fallback_models_reads_the_role_list(self, tmp_path: Path) -> None:
        from sova.core.workflow import WorkflowEngine

        cfg = _fallback_config(developer_fallback_models=[])
        cfg.llm.engine_owned_fallback = True
        assert WorkflowEngine(steps=[], ctx=self._ctx(tmp_path, cfg))._has_fallback_models() is False
        assert WorkflowEngine(steps=[], ctx=self._ctx(tmp_path, cfg, role="triage"))._has_fallback_models() is True

    @pytest.mark.parametrize(
        ("roles", "expected"),
        [({"developer_fallback_models": ["claude-haiku-5-5"]}, "claude-haiku-5-5"), ({}, "claude-sonnet-5-5")],
    )
    async def test_a_billing_failure_switches_to_the_roles_next_fallback(
        self, tmp_path: Path, roles: dict, expected: str
    ) -> None:
        from sova.core.workflow import WorkflowEngine

        cfg = _fallback_config(**roles)
        cfg.llm.engine_owned_fallback = True
        ctx = self._ctx(tmp_path, cfg)
        ctx.resolved_model = "claude-opus-5-5"
        engine = WorkflowEngine(steps=[], ctx=ctx)
        seen: list[str | None] = []

        async def attempt(_step: object, _record: object) -> str:
            seen.append(ctx.resolved_model)
            return "billing_exhausted" if len(seen) == 1 else "done"

        with patch.object(engine, "_try_step_with_retries", side_effect=attempt):
            await engine._execute_with_retries(MagicMock(name="develop"))

        assert seen == ["claude-opus-5-5", expected]

    async def test_run_marks_its_llm_calls_as_the_roles(self, tmp_path: Path) -> None:
        from sova.core.workflow import WorkflowEngine
        from sova.llm.client import current_role

        engine = WorkflowEngine(steps=[], ctx=self._ctx(tmp_path, ProjectConfig(), role="researcher"))
        seen: list[str | None] = []

        async def body() -> MagicMock:
            seen.append(current_role())
            return MagicMock()

        with patch.object(engine, "_run", side_effect=body):
            await engine.run()
        assert seen == ["researcher"]
        assert current_role() is None


class TestRolesRunInTheirScope:
    """Each role that runs outside the WorkflowEngine marks its own LLM calls."""

    async def test_triage_llm_assessment(self, tmp_path: Path) -> None:
        from sova.llm.client import current_role
        from sova.roles.triage import TriageRole

        seen: list[str | None] = []

        async def fake_invoke(*_a: object, **_k: object) -> LLMResult:
            seen.append(current_role())
            raise RuntimeError("stop here")

        ctx = ExecutionContext(project_dir=tmp_path, config=ProjectConfig(), adapter=AsyncMock(), role="developer")
        with patch("sova.llm.client.invoke", side_effect=fake_invoke):
            await TriageRole().assess_task_with_llm(_task(), ctx)
        assert seen == ["triage"]

    async def test_reviewer_execute(self, tmp_path: Path) -> None:
        from sova.llm.client import current_role

        seen: list[str | None] = []

        async def inner(_ctx: ExecutionContext) -> MagicMock:
            seen.append(current_role())
            return MagicMock()

        role = ReviewerRole()
        with patch.object(role, "_execute", side_effect=inner):
            await role.execute(_review_ctx(tmp_path))
        assert seen == ["reviewer"]

    async def test_planner_role_execute(self, tmp_path: Path) -> None:
        from sova.llm.client import current_role
        from sova.roles.planner import PlannerRole

        seen: list[str | None] = []

        async def fake_invoke(*_a: object, **_k: object) -> LLMResult:
            seen.append(current_role())
            raise RuntimeError("stop here")

        role = PlannerRole()
        ctx = ExecutionContext(project_dir=tmp_path, config=ProjectConfig(), adapter=AsyncMock(), role="planner")
        with (
            patch.object(role, "_gather_open_issues", AsyncMock(return_value=[])),
            patch("sova.llm.client.invoke", side_effect=fake_invoke),
        ):
            await role.execute(ctx)
        assert seen == ["planner"]

    async def test_supervisor_planner_call(self, tmp_path: Path) -> None:
        from sova.llm.client import current_role

        seen: list[str | None] = []

        async def fake_invoke(*_a: object, **_k: object) -> LLMResult:
            seen.append(current_role())
            raise RuntimeError("stop here")

        planner = TestPlannerModelResolution()._planner(tmp_path)
        with patch("sova.supervisor.planner.invoke", side_effect=fake_invoke):
            await planner._call_llm("system", "user", "claude-sonnet-5-5")
        assert seen == ["planner"]


# ---------------------------------------------------------------------------
# Models view: which model each step runs on, and why (#1463)
# ---------------------------------------------------------------------------


@pytest.fixture
def _firstparty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve short names for the first-party API, whatever the developer's shell routes Claude Code through."""
    for name in ("CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_BEDROCK"):
        monkeypatch.delenv(name, raising=False)


def _steps(payload: dict) -> dict[str, dict]:
    steps = {s["step"]: s for role in payload["roles"] for s in role["steps"]}
    steps.update({s["step"]: s for s in payload["writing"]})
    return steps


@pytest.mark.usefixtures("_firstparty")
class TestModelMap:
    """A config with a step setting (develop), a role model (researcher) and the default for everything else."""

    def _payload(self, **overrides: object) -> dict:
        from sova.dashboard.services.model_map_service import build_model_map

        cfg = ProjectConfig(
            agent={"model": "claude-sonnet-5-5", "fallback_models": list(_DEFAULT_CHAIN)},
            llm={"routing": {"develop": "claude-opus-5-5", **overrides.pop("routing", {})}},
            roles={"researcher_model": "claude-opus-5-5", "triage_model": "", **overrides.pop("roles", {})},
        )
        return build_model_map(cfg)

    def test_a_step_setting_wins_and_says_so(self) -> None:
        develop = _steps(self._payload())["develop"]
        assert (develop["runs_as"], develop["source"], develop["route"]) == (
            "claude-opus-5-5",
            "route",
            "claude-opus-5-5",
        )

    def test_a_role_model_applies_to_the_roles_steps(self) -> None:
        steps = _steps(self._payload())
        for name in ("research", "spec"):
            assert (steps[name]["runs_as"], steps[name]["source"]) == ("claude-opus-5-5", "role")

    def test_the_default_model_applies_where_nothing_more_specific_is_set(self) -> None:
        steps = _steps(self._payload(roles={"researcher_model": ""}))
        assert (steps["research"]["runs_as"], steps["research"]["source"]) == ("claude-sonnet-5-5", "default")

    def test_without_a_developer_model_the_developer_pipeline_picks_by_issue_size(self) -> None:
        simplify = _steps(self._payload())["simplify"]
        assert simplify["source"] == "size"
        sizes = {entry["tier"]: entry["runs_as"] for entry in simplify["by_size"]}
        # Built-in tiers pinned to agent.model's family, then resolved: sonnet pins to agent.model.
        assert sizes == {
            "trivial": "claude-haiku-5-5",
            "simple": "claude-sonnet-5-5",
            "moderate": "claude-sonnet-5-5",
            "complex": "claude-opus-5-5",
            "epic": "claude-opus-5-5",
        }

    def test_an_address_review_run_uses_the_default_model_not_the_developer_model(self) -> None:
        steps = _steps(self._payload(roles={"developer_model": "claude-opus-5-5"}))
        assert (steps["rearrange_commits"]["runs_as"], steps["rearrange_commits"]["source"]) == (
            "claude-sonnet-5-5",
            "default",
        )
        validate = steps["validate"]
        assert (validate["runs_as"], validate["source"]) == ("claude-opus-5-5", "role")
        assert validate["address_review"]["runs_as"] == "claude-sonnet-5-5"

    def test_validate_route_applies_to_fix_attempt_one_only(self) -> None:
        validate = _steps(self._payload(routing={"validate": "claude-haiku-5-5"}))["validate"]
        assert (validate["runs_as"], validate["source"]) == ("claude-haiku-5-5", "route")
        assert validate["later_attempts"]["source"] == "size"

    def test_the_developer_pin_outranks_a_step_setting_on_code_editing_steps_only(self) -> None:
        steps = _steps(self._payload(roles={"developer_runtime_model": "claude-fable-5-2"}))
        assert (steps["develop"]["runs_as"], steps["develop"]["source"]) == ("claude-fable-5-2", "pin")
        assert steps["commit_message"]["source"] != "pin"
        assert steps["rebase"]["source"] == "default"

    def test_the_developer_pin_does_nothing_on_a_runtime_that_drops_the_model(self) -> None:
        from sova.dashboard.services.model_map_service import build_model_map

        cfg = ProjectConfig(roles={"developer_runtime_model": "claude-fable-5-2"})
        develop = _steps(build_model_map(cfg, runtime_honours_model=False))["develop"]
        assert develop["source"] != "pin"

    def test_rebase_passes_the_default_model_even_with_a_developer_model(self) -> None:
        rebase = _steps(self._payload(roles={"developer_model": "claude-opus-5-5"}))["rebase"]
        assert (rebase["runs_as"], rebase["source"]) == ("claude-sonnet-5-5", "default")

    def test_triage_with_no_model_runs_on_the_provider_default(self) -> None:
        triage = _steps(self._payload())["triage"]
        assert (triage["model"], triage["source"]) == (None, "provider_default")

    def test_a_short_role_model_runs_as_its_exact_id(self) -> None:
        review = _steps(self._payload(roles={"reviewer_model": "sonnet"}))["review"]
        assert (review["model"], review["runs_as"], review["source"]) == ("sonnet", "claude-sonnet-5-5", "role")

    def test_extraction_falls_back_to_the_built_in_short_name(self) -> None:
        extraction = _steps(self._payload())["extraction"]
        assert (extraction["model"], extraction["runs_as"], extraction["source"]) == (
            "haiku",
            "claude-haiku-5-5",
            "built_in",
        )

    def test_each_step_lists_its_roles_fallback_chain(self) -> None:
        steps = _steps(self._payload(roles={"researcher_fallback_models": ["claude-haiku-5-5"]}))
        assert steps["research"]["fallbacks"] == ["claude-haiku-5-5"]
        # The default chain, minus the model already tried first.
        assert steps["rebase"]["fallbacks"] == ["claude-haiku-5-5"]
        assert steps["develop"]["fallbacks"] == _DEFAULT_CHAIN

    def test_a_step_picked_by_issue_size_skips_the_fallback_equal_to_each_sizes_model(self) -> None:
        simplify = _steps(self._payload())["simplify"]
        chains = {entry["tier"]: entry["fallbacks"] for entry in simplify["by_size"]}
        # The default chain is Sonnet then Haiku: a Sonnet run never retries Sonnet, a Haiku run never Haiku.
        assert chains["moderate"] == ["claude-haiku-5-5"]
        assert chains["trivial"] == ["claude-sonnet-5-5"]
        assert chains["complex"] == _DEFAULT_CHAIN

    def test_a_role_block_reports_whether_its_fallbacks_are_its_own(self) -> None:
        roles = {r["id"]: r for r in self._payload(roles={"reviewer_fallback_models": []})["roles"]}
        assert roles["reviewer"]["fallbacks"] == {"key": "roles.reviewer_fallback_models", "own": True, "models": []}
        assert roles["triage"]["fallbacks"]["own"] is False
        assert roles["triage"]["fallbacks"]["models"] == _DEFAULT_CHAIN

    def test_every_step_names_a_routing_key(self) -> None:
        from sova.dashboard.services.model_map_service import ROUTING_KEYS

        assert set(_steps(self._payload())) <= ROUTING_KEYS


# ---------------------------------------------------------------------------
# Step rows and Pin all, against a project database (#1463)
# ---------------------------------------------------------------------------


async def _store_rows(project_dir: Path, rows: dict[str, object]) -> None:
    from sova.config.db_loader import save_setting
    from sova.config.loader import invalidate_config_cache
    from sova.db.session import get_session

    async with await get_session(project_dir=project_dir) as session, session.begin():
        for key, value in rows.items():
            await save_setting(session, key, value)
    invalidate_config_cache(project_dir)


async def _stored(project_dir: Path) -> dict[str, object]:
    from sqlalchemy import select

    from sova.db.models import ProjectSetting
    from sova.db.session import _ORPHAN_SWEEP_MARKER_KEY, get_session

    async with await get_session(project_dir=project_dir) as session:
        rows = (await session.execute(select(ProjectSetting))).scalars().all()
    return {row.key: json.loads(row.value) for row in rows if row.key != _ORPHAN_SWEEP_MARKER_KEY}


@pytest.mark.usefixtures("_dispose_project_engines")
class TestStepModelRows:
    async def test_choosing_a_model_writes_exactly_one_routing_row(self, tmp_path: Path) -> None:
        from sova.config.loader import load_config
        from sova.dashboard.services.settings_service import set_step_model

        before = await _stored(tmp_path)
        result = await set_step_model(tmp_path, step="simplify", model="claude-haiku-5-5")

        assert result == {"status": "ok", "step": "simplify", "model": "claude-haiku-5-5"}
        added = {k: v for k, v in (await _stored(tmp_path)).items() if k not in before}
        assert added == {"llm.routing.simplify": "claude-haiku-5-5"}
        assert load_config(tmp_path).llm.routing == {"simplify": "claude-haiku-5-5"}

    async def test_use_the_role_model_deletes_that_row_only(self, tmp_path: Path) -> None:
        from sova.config.loader import load_config
        from sova.dashboard.services.settings_service import clear_step_model

        await _store_rows(tmp_path, {"llm.routing.simplify": "claude-haiku-5-5", "llm.routing.develop": "x-model"})
        result = await clear_step_model(tmp_path, step="simplify")

        assert (result["status"], result["deleted"]) == ("ok", 1)
        stored = await _stored(tmp_path)
        assert "llm.routing.simplify" not in stored
        assert stored["llm.routing.develop"] == "x-model"
        assert load_config(tmp_path).llm.routing == {"develop": "x-model"}

    async def test_an_empty_model_deletes_the_row(self, tmp_path: Path) -> None:
        from sova.dashboard.services.settings_service import set_step_model

        await _store_rows(tmp_path, {"llm.routing.review": "claude-opus-5-5"})
        result = await set_step_model(tmp_path, step="review", model="  ")
        assert result["model"] is None
        assert "llm.routing.review" not in await _stored(tmp_path)

    async def test_an_issue_size_is_a_valid_step(self, tmp_path: Path) -> None:
        from sova.dashboard.services.settings_service import set_step_model

        assert (await set_step_model(tmp_path, step="complex", model="claude-opus-5-5"))["status"] == "ok"

    @pytest.mark.parametrize("step", ["deploy", "llm.routing", "develop.extra", ""])
    async def test_an_unknown_step_is_refused(self, tmp_path: Path, step: str) -> None:
        from sova.dashboard.services.settings_service import set_step_model

        result = await set_step_model(tmp_path, step=step, model="claude-opus-5-5")
        assert result.get("unknown") is True
        assert not any(k.startswith("llm.routing") for k in await _stored(tmp_path))

    async def test_a_model_id_with_whitespace_is_refused(self, tmp_path: Path) -> None:
        from sova.dashboard.services.settings_service import set_step_model

        assert "error" in await set_step_model(tmp_path, step="develop", model="claude opus")

    async def test_a_routing_table_set_by_the_environment_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sova.dashboard.services.settings_service import clear_step_model, set_step_model

        monkeypatch.setenv("SOVA_LLM_ROUTING", '{"develop": "x"}')
        assert (await set_step_model(tmp_path, step="develop", model="y"))["env_var"] == "SOVA_LLM_ROUTING"
        assert (await clear_step_model(tmp_path, step="develop"))["env_var"] == "SOVA_LLM_ROUTING"

    async def test_a_whole_table_row_is_split_so_it_cannot_hide_the_new_row(self, tmp_path: Path) -> None:
        from sova.config.loader import load_config
        from sova.dashboard.services.settings_service import set_step_model

        await _store_rows(tmp_path, {"llm.routing": {"develop": "a-model", "review": "b-model"}})
        await set_step_model(tmp_path, step="develop", model="c-model")

        stored = await _stored(tmp_path)
        assert "llm.routing" not in stored
        assert (stored["llm.routing.develop"], stored["llm.routing.review"]) == ("c-model", "b-model")
        assert load_config(tmp_path).llm.routing == {"develop": "c-model", "review": "b-model"}


@pytest.mark.usefixtures("_dispose_project_engines", "_firstparty")
class TestPinAll:
    async def test_pin_all_replaces_every_short_name(self, tmp_path: Path) -> None:
        from sova.config.loader import load_config
        from sova.dashboard.services.model_map_service import short_name_settings
        from sova.dashboard.services.settings_service import pin_short_names

        await _store_rows(
            tmp_path,
            {
                "agent.model": "opus",
                "roles.reviewer_model": "sonnet",
                "agent.fallback_models": ["sonnet", "claude-haiku-5-5"],
                "roles.developer_fallback_models": ["haiku"],
                "llm.routing.extraction": "haiku",
            },
        )
        # roles.triage_model, roles.planner_model, confidence.model and oversight.analysis_model default to "sonnet".
        assert len(short_name_settings(load_config(tmp_path))) >= 8

        result = await pin_short_names(tmp_path)

        assert result["status"] == "ok", result
        cfg = load_config(tmp_path)
        assert short_name_settings(cfg) == []
        assert cfg.agent.model == "claude-opus-5-5"
        assert cfg.roles.reviewer_model == "claude-sonnet-5-5"
        assert cfg.roles.triage_model == "claude-sonnet-5-5"
        assert cfg.agent.fallback_models == ["claude-sonnet-5-5", "claude-haiku-5-5"]
        assert cfg.roles.developer_fallback_models == ["claude-haiku-5-5"]
        # A routing row is pinned to agent.model's family first, as routing applies it.
        assert cfg.llm.routing == {"extraction": "claude-haiku-5-5"}

    async def test_pin_one_changes_only_that_setting(self, tmp_path: Path) -> None:
        from sova.config.loader import load_config
        from sova.dashboard.services.settings_service import pin_short_names

        result = await pin_short_names(tmp_path, keys=["roles.planner_model"])

        assert result["pinned"] == {"roles.planner_model": "claude-sonnet-5-5"}
        cfg = load_config(tmp_path)
        assert (cfg.roles.planner_model, cfg.roles.triage_model) == ("claude-sonnet-5-5", "sonnet")

    async def test_an_unresolved_name_takes_the_single_model_of_its_family(self, tmp_path: Path) -> None:
        from sova.config.loader import load_config
        from sova.dashboard.services.settings_service import pin_short_names

        await _store_rows(tmp_path, {"llm.resolve_tier_aliases": False})
        catalog = ["claude-sonnet-5-5", "claude-haiku-5-5"]
        result = await pin_short_names(tmp_path, keys=["roles.triage_model"], catalog=catalog)

        assert result["pinned"] == {"roles.triage_model": "claude-sonnet-5-5"}
        assert load_config(tmp_path).roles.triage_model == "claude-sonnet-5-5"

    async def test_an_ambiguous_name_is_left_alone(self, tmp_path: Path) -> None:
        from sova.config.loader import load_config
        from sova.dashboard.services.settings_service import pin_short_names

        await _store_rows(tmp_path, {"llm.resolve_tier_aliases": False})
        catalog = ["claude-sonnet-5-5", "claude-sonnet-5-2"]
        result = await pin_short_names(tmp_path, keys=["roles.triage_model"], catalog=catalog)

        assert result["pinned"] == {}
        assert [s["key"] for s in result["skipped"]] == ["roles.triage_model"]
        assert load_config(tmp_path).roles.triage_model == "sonnet"

    async def test_a_value_set_by_the_environment_is_not_pinned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sova.dashboard.services.settings_service import pin_short_names

        monkeypatch.setenv("SOVA_ROLES_TRIAGE_MODEL", "haiku")
        result = await pin_short_names(tmp_path, keys=["roles.triage_model"])
        assert result["pinned"] == {}
        assert "SOVA_ROLES_TRIAGE_MODEL" in result["skipped"][0]["reason"]
