"""Tests for plan filtering in TaskProgressionEngine.evaluate_all()."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from sova.adapters.base import Task, TaskState
from sova.config.models import ProjectConfig, SupervisorConfig
from sova.supervisor.planner import PlannedAction, PlanResult
from sova.supervisor.progression import (
    BlockReason,
    ProgressionAction,
    ProgressionDecision,
    TaskProgressionEngine,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(
    issue_id: int,
    state: TaskState = TaskState.RESEARCHED,
    labels: list[str] | None = None,
) -> Task:
    return Task(
        id=str(issue_id),
        title=f"Issue #{issue_id}",
        body="",
        state=state,
        labels=labels or [],
    )


def _make_engine(
    config: SupervisorConfig | None = None,
    max_parallel_agents: int = 2,
) -> TaskProgressionEngine:
    supervisor_cfg = config or SupervisorConfig(auto_develop=True, auto_research=True, auto_integrate=True)
    cfg = ProjectConfig(supervisor=supervisor_cfg, max_parallel_agents=max_parallel_agents)
    return TaskProgressionEngine(
        config=cfg,
        adapter=AsyncMock(),
        project_dir=Path(tempfile.mkdtemp()),
        session_factory=MagicMock(),
    )


def _decision(
    issue: int,
    action: ProgressionAction,
    reason: str = "",
    blocked_by: tuple[BlockReason, ...] = (),
    pr_number: int | None = None,
) -> ProgressionDecision:
    return ProgressionDecision(
        issue_number=issue,
        action=action,
        reason=reason,
        blocked_by=blocked_by,
        pr_number=pr_number,
    )


# ---------------------------------------------------------------------------
# Plan filtering tests (unit-level: test the filtering logic directly)
# ---------------------------------------------------------------------------


class TestPlanFiltering:
    """Test the plan filtering logic that runs at the end of evaluate_all()."""

    async def _evaluate_with_plan(
        self,
        decisions: list[ProgressionDecision],
        plan: PlanResult | None,
    ) -> list[ProgressionDecision]:
        """Run evaluate_all with mocked internals, returning filtered decisions."""
        issue_ids_list = sorted(d.issue_number for d in decisions)
        engine = _make_engine(
            SupervisorConfig(
                auto_develop=True,
                auto_research=True,
                auto_integrate=True,
                task_queue=issue_ids_list,
            ),
            max_parallel_agents=10,
        )

        # Mock all the internals so evaluate_all just returns our decisions
        with (
            patch("sova.supervisor.progression.check_github_rate_limit_gate", return_value=None),
            patch("sova.supervisor.progression.build_dependency_graph", new_callable=AsyncMock) as mock_graph,
            patch("sova.supervisor.progression.check_memory_pressure_gate", return_value=None),
            patch("sova.supervisor.progression.check_quota_gate", new_callable=AsyncMock, return_value=None),
            patch("sova.supervisor.progression.check_ci_budget_gate", new_callable=AsyncMock, return_value=None),
            patch("sova.supervisor.progression.get_alive_count", new_callable=AsyncMock, return_value=0),
            patch(
                "sova.dashboard.services.pr_service.get_pr_mergeability_map",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            # Build a mock graph that returns our tasks
            graph = MagicMock()
            issue_ids = {d.issue_number for d in decisions}
            graph.nodes = issue_ids
            graph.get_task.side_effect = lambda nid: _task(nid)
            mock_graph.return_value = graph

            # Mock _evaluate_single to return our pre-built decisions
            decision_map = {d.issue_number: d for d in decisions}
            engine._evaluate_single = AsyncMock(side_effect=lambda issue, *args, **kwargs: decision_map[issue])

            return await engine.evaluate_all(plan=plan)

    async def test_plan_none_passes_all_through(self) -> None:
        decisions = [
            _decision(1, ProgressionAction.SPAWN_DEVELOPER, "ready"),
            _decision(2, ProgressionAction.SPAWN_RESEARCHER, "triaged"),
            _decision(3, ProgressionAction.WAIT, "waiting"),
        ]
        result = await self._evaluate_with_plan(decisions, plan=None)
        assert len(result) == 3
        actions = {d.issue_number: d.action for d in result}
        assert actions[1] == ProgressionAction.SPAWN_DEVELOPER
        assert actions[2] == ProgressionAction.SPAWN_RESEARCHER
        assert actions[3] == ProgressionAction.WAIT

    async def test_plan_filters_unapproved_actions(self) -> None:
        decisions = [
            _decision(1, ProgressionAction.SPAWN_DEVELOPER, "ready"),
            _decision(2, ProgressionAction.SPAWN_DEVELOPER, "also ready"),
            _decision(3, ProgressionAction.SPAWN_RESEARCHER, "triaged"),
        ]
        plan = PlanResult(
            reasoning="only start issue 1",
            actions=(PlannedAction(action="spawn_developer", issue=1, priority=1, reason="highest priority"),),
        )
        result = await self._evaluate_with_plan(decisions, plan=plan)
        actions = {d.issue_number: d.action for d in result}
        assert actions[1] == ProgressionAction.SPAWN_DEVELOPER
        assert actions[2] == ProgressionAction.WAIT
        assert actions[3] == ProgressionAction.WAIT
        # Filtered items should have informative reason
        filtered_2 = next(d for d in result if d.issue_number == 2)
        assert "not in LLM plan" in filtered_2.reason
        assert "spawn_developer" in filtered_2.reason

    async def test_plan_preserves_blocked_decisions(self) -> None:
        blocker = BlockReason(gate="memory_pressure", detail="system memory low")
        decisions = [
            _decision(1, ProgressionAction.BLOCKED, "blocked by gate", blocked_by=(blocker,)),
            _decision(2, ProgressionAction.SPAWN_DEVELOPER, "ready"),
        ]
        plan = PlanResult(
            reasoning="approve issue 2 only",
            actions=(PlannedAction(action="spawn_developer", issue=2, priority=1, reason="go"),),
        )
        result = await self._evaluate_with_plan(decisions, plan=plan)
        actions = {d.issue_number: d.action for d in result}
        assert actions[1] == ProgressionAction.BLOCKED
        assert actions[2] == ProgressionAction.SPAWN_DEVELOPER

    async def test_plan_preserves_wait_decisions(self) -> None:
        decisions = [
            _decision(1, ProgressionAction.WAIT, "no transition"),
            _decision(2, ProgressionAction.CHECKPOINT_NEEDED, "needs approval"),
        ]
        plan = PlanResult(
            reasoning="empty plan",
            actions=(),
        )
        result = await self._evaluate_with_plan(decisions, plan=plan)
        actions = {d.issue_number: d.action for d in result}
        assert actions[1] == ProgressionAction.WAIT
        assert actions[2] == ProgressionAction.CHECKPOINT_NEEDED

    async def test_action_type_mismatch_filters_out(self) -> None:
        decisions = [
            _decision(1, ProgressionAction.SPAWN_RESEARCHER, "triaged"),
        ]
        plan = PlanResult(
            reasoning="approve developer for issue 1",
            actions=(PlannedAction(action="spawn_developer", issue=1, priority=1, reason="dev"),),
        )
        result = await self._evaluate_with_plan(decisions, plan=plan)
        assert result[0].action == ProgressionAction.WAIT
        assert "spawn_researcher" in result[0].reason

    async def test_empty_plan_actions_filters_all_actionable(self) -> None:
        decisions = [
            _decision(1, ProgressionAction.SPAWN_DEVELOPER, "ready"),
            _decision(2, ProgressionAction.SPAWN_INTEGRATE, "approved"),
        ]
        plan = PlanResult(reasoning="do nothing this cycle", actions=())
        result = await self._evaluate_with_plan(decisions, plan=plan)
        for d in result:
            assert d.action == ProgressionAction.WAIT

    async def test_plan_preserves_pr_number_on_filtered(self) -> None:
        decisions = [
            _decision(1, ProgressionAction.SPAWN_INTEGRATE, "merge", pr_number=100),
        ]
        plan = PlanResult(reasoning="defer merge", actions=())
        result = await self._evaluate_with_plan(decisions, plan=plan)
        assert result[0].pr_number == 100
        assert result[0].action == ProgressionAction.WAIT


# ---------------------------------------------------------------------------
# Supervisor service plan reasoning tests
# ---------------------------------------------------------------------------


class TestSupervisorServicePlanState:
    def test_set_and_get_reasoning(self) -> None:
        from sova.dashboard.services import supervisor_service as svc

        svc.set_pending_plan(
            [],
            project_slug="test/repo",
            reasoning="test reasoning",
            deferred=[{"action": "spawn_developer", "issue": 1, "reason": "budget"}],
        )
        assert svc.get_plan_reasoning("test/repo") == "test reasoning"
        assert len(svc.get_plan_deferred("test/repo")) == 1
        assert svc.get_plan_deferred("test/repo")[0]["issue"] == 1

    def test_set_clears_previous(self) -> None:
        from sova.dashboard.services import supervisor_service as svc

        svc.set_pending_plan([], project_slug="test/repo", reasoning="first")
        svc.set_pending_plan([], project_slug="test/repo")
        assert svc.get_plan_reasoning("test/repo") is None
        assert svc.get_plan_deferred("test/repo") == []

    def test_defaults_without_optional_kwargs(self) -> None:
        from sova.dashboard.services import supervisor_service as svc

        svc.set_pending_plan([], project_slug="test/repo")
        assert svc.get_plan_reasoning("test/repo") is None
        assert svc.get_plan_deferred("test/repo") == []
