"""Tests for the research lookahead gate (sova.supervisor.gates.research_lookahead) and its engine wiring."""

from __future__ import annotations

import re
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog

from sova.adapters.base import Task, TaskState
from sova.config.models import ProjectConfig, SupervisorConfig
from sova.db.models import TaskRun
from sova.supervisor.dependency_graph import DependencyGraph
from sova.supervisor.gates.research_lookahead import (
    RESEARCHED,
    RESEARCHING,
    LookaheadHolder,
    check_research_lookahead_gate,
    collect_lookahead_holders,
)
from sova.supervisor.persona import DEFAULT_SUPERVISOR_PERSONA
from sova.supervisor.planner import PlannedAction, PlanResult
from sova.supervisor.progression import ProgressionAction, TaskProgressionEngine

_GATES = "sova.supervisor.gates.research_lookahead"


def _task(
    issue: int,
    state: TaskState,
    body: str = "",
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
) -> Task:
    return Task(
        id=str(issue), title=f"Issue #{issue}", body=body, state=state, labels=labels or [], assignees=assignees or []
    )


def _adapter(github_user: str = "me") -> MagicMock:
    adapter = MagicMock()
    adapter.github_user = github_user
    return adapter


def _config(**supervisor: object) -> ProjectConfig:
    return ProjectConfig(supervisor=SupervisorConfig(auto_research=True, auto_develop=True, **supervisor))


@pytest.fixture
async def session_factory(reset_db_rows: None, tmp_path: Path):
    from sova.db.session import get_session_factory

    return await get_session_factory(tmp_path)


async def _add_runs(session_factory, *runs: TaskRun) -> None:
    async with session_factory() as session:
        async with session.begin():
            session.add_all(runs)


def _researcher_run(issue: int, status: str = "running", pid: int | None = None) -> TaskRun:
    return TaskRun(issue_number=str(issue), role="researcher", status=status, pid=pid)


class TestCheckResearchLookaheadGate:
    @pytest.mark.parametrize(
        ("holders", "limit", "blocks"),
        [
            ([LookaheadHolder(1, RESEARCHED), LookaheadHolder(2, RESEARCHED)], 0, False),
            (None, 2, False),
            ([LookaheadHolder(1, RESEARCHED)], 2, False),
            ([LookaheadHolder(1, RESEARCHED)], 5, False),
            ([LookaheadHolder(1, RESEARCHED), LookaheadHolder(2, RESEARCHING)], 2, True),
        ],
    )
    def test_blocks_only_at_limit(self, holders: list[LookaheadHolder] | None, limit: int, blocks: bool) -> None:
        result = check_research_lookahead_gate(holders, limit)
        assert (result is not None) is blocks

    def test_detail_names_each_holder(self) -> None:
        result = check_research_lookahead_gate([LookaheadHolder(1, RESEARCHED), LookaheadHolder(7, RESEARCHING)], 2)
        assert result is not None
        assert result.gate == "research_lookahead"
        assert "#1 researched" in result.detail
        assert "#7 researching" in result.detail


class TestCollectLookaheadHolders:
    @pytest.mark.asyncio
    async def test_counts_researched_and_live_researching_only_in_queue(self, session_factory) -> None:
        graph = DependencyGraph(
            [
                _task(1, TaskState.RESEARCHED),
                _task(2, TaskState.TRIAGED),  # live researcher
                _task(3, TaskState.TRIAGED),  # dead pid
                _task(4, TaskState.TRIAGED),  # awaiting_approval is terminal
                _task(5, TaskState.TRIAGED),  # no run
                _task(6, TaskState.IN_PROGRESS),  # developer already started
                _task(7, TaskState.RESEARCHED),  # not in the queue
            ]
        )
        await _add_runs(
            session_factory,
            _researcher_run(2, pid=None),
            _researcher_run(3, pid=999999),
            _researcher_run(4, status="awaiting_approval", pid=None),
        )
        with patch("sova.supervisor.gates.research_lookahead.is_process_alive", return_value=False):
            holders = await collect_lookahead_holders(graph, [1, 2, 3, 4, 5, 6], _config(), session_factory, _adapter())

        assert holders == [LookaheadHolder(1, RESEARCHED), LookaheadHolder(2, RESEARCHING)]

    @pytest.mark.asyncio
    async def test_alive_pid_counts_as_researching(self, session_factory) -> None:
        graph = DependencyGraph([_task(2, TaskState.TRIAGED)])
        await _add_runs(session_factory, _researcher_run(2, pid=4242))
        with patch("sova.supervisor.gates.research_lookahead.is_process_alive", return_value=True):
            holders = await collect_lookahead_holders(graph, [2], _config(), session_factory, _adapter())
        assert holders == [LookaheadHolder(2, RESEARCHING)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("body", "labels"),
        [
            ("## Dependencies\n- #99\n", []),
            ("", ["agent:on-hold"]),
            ("", ["agent:human-only"]),
        ],
    )
    async def test_held_researched_issue_is_excluded(self, session_factory, body: str, labels: list[str]) -> None:
        graph = DependencyGraph(
            [_task(1, TaskState.RESEARCHED, body=body, labels=labels), _task(99, TaskState.TRIAGED)]
        )
        holders = await collect_lookahead_holders(graph, [1], _config(), session_factory, _adapter())
        assert holders == []

    @pytest.mark.asyncio
    async def test_dependency_ignored_when_respect_dependencies_off(self, session_factory) -> None:
        graph = DependencyGraph(
            [_task(1, TaskState.RESEARCHED, body="## Dependencies\n- #99\n"), _task(99, TaskState.TRIAGED)]
        )
        holders = await collect_lookahead_holders(
            graph, [1], _config(respect_dependencies=False), session_factory, _adapter()
        )
        assert holders == [LookaheadHolder(1, RESEARCHED)]

    @pytest.mark.asyncio
    async def test_issue_over_developer_failure_limit_is_excluded(self, session_factory) -> None:
        graph = DependencyGraph([_task(1, TaskState.RESEARCHED)])
        await _add_runs(
            session_factory,
            TaskRun(issue_number="1", role="developer", status="failed"),
            TaskRun(issue_number="1", role="developer", status="failed"),
        )
        holders = await collect_lookahead_holders(
            graph, [1], _config(max_developer_failures=2), session_factory, _adapter()
        )
        assert holders == []

    @pytest.mark.asyncio
    async def test_issue_owned_by_teammate_is_excluded(self, session_factory) -> None:
        graph = DependencyGraph(
            [
                _task(1, TaskState.RESEARCHED, assignees=["teammate"]),
                _task(2, TaskState.RESEARCHED, assignees=["me"]),
            ]
        )
        holders = await collect_lookahead_holders(graph, [1, 2], _config(), session_factory, _adapter("me"))
        assert holders == [LookaheadHolder(2, RESEARCHED)]

    @pytest.mark.asyncio
    async def test_teammate_issue_counts_when_ownership_not_respected(self, session_factory) -> None:
        graph = DependencyGraph([_task(1, TaskState.RESEARCHED, assignees=["teammate"])])
        holders = await collect_lookahead_holders(
            graph, [1], _config(respect_ownership=False), session_factory, _adapter("me")
        )
        assert holders == [LookaheadHolder(1, RESEARCHED)]

    @pytest.mark.asyncio
    async def test_duplicate_and_unknown_queue_entries(self, session_factory) -> None:
        graph = DependencyGraph([_task(1, TaskState.RESEARCHED), _task(2, TaskState.TRIAGED)])
        holders = await collect_lookahead_holders(graph, [1, 1, 2, 3], _config(), session_factory, _adapter())
        assert holders == [LookaheadHolder(1, RESEARCHED)]

    @pytest.mark.asyncio
    async def test_researched_issues_awaiting_spec_approval_block_triaged_candidate(self, session_factory) -> None:
        graph = DependencyGraph(
            [_task(1, TaskState.RESEARCHED), _task(2, TaskState.RESEARCHED), _task(3, TaskState.TRIAGED)]
        )
        await _add_runs(
            session_factory,
            _researcher_run(1, status="awaiting_approval"),
            _researcher_run(2, status="awaiting_approval"),
        )
        holders = await collect_lookahead_holders(graph, [1, 2, 3], _config(), session_factory, _adapter())
        assert holders == [LookaheadHolder(1, RESEARCHED), LookaheadHolder(2, RESEARCHED)]
        block = check_research_lookahead_gate(holders, 2)
        assert block is not None
        assert "#1 researched" in block.detail
        assert "#2 researched" in block.detail

    @pytest.mark.asyncio
    async def test_unavailable_count_is_none_and_logged(self) -> None:
        graph = DependencyGraph([_task(2, TaskState.TRIAGED)])
        broken = MagicMock(side_effect=RuntimeError("db down"))
        with structlog.testing.capture_logs() as logs:
            assert await collect_lookahead_holders(graph, [2], _config(), broken, _adapter()) is None
        assert [e for e in logs if e["event"] == "research_lookahead.collect_failed" and e["log_level"] == "warning"]


class TestDefaultPersona:
    def test_developer_priority_precedes_researcher_priority(self) -> None:
        section = DEFAULT_SUPERVISOR_PERSONA.split("## Priorities", 1)[1]
        items = [m.group(2).lower() for m in re.finditer(r"^(\d+)\. (.+)$", section, re.MULTILINE)]
        developer = next(i for i, text in enumerate(items) if "developer" in text)
        researcher = next(i for i, text in enumerate(items) if "researcher" in text)
        assert developer < researcher


def _engine(tasks: list[Task], queue: list[int], **supervisor: object) -> TaskProgressionEngine:
    adapter = AsyncMock()
    adapter.list_tasks = AsyncMock(return_value=tasks)
    adapter.get_state = AsyncMock(side_effect=lambda issue: next(t.state for t in tasks if t.id == issue))
    cfg = ProjectConfig(
        supervisor=SupervisorConfig(auto_research=True, auto_develop=True, task_queue=queue, **supervisor),
        max_parallel_agents=10,
    )
    return TaskProgressionEngine(
        config=cfg, adapter=adapter, project_dir=Path(tempfile.mkdtemp()), session_factory=MagicMock()
    )


def _patched_gates(stack: ExitStack, holders: list[LookaheadHolder] | None) -> AsyncMock:
    """Neutralise every gate except the lookahead one; return the mock standing in for the holder query."""
    for name in ("check_already_running", "check_human_involvement_gate", "check_quota_gate", "check_budget_gate"):
        stack.enter_context(patch(f"sova.supervisor.progression.{name}", new_callable=AsyncMock, return_value=None))
    stack.enter_context(
        patch("sova.supervisor.progression.check_repeated_failures_gate", new_callable=AsyncMock, return_value=None)
    )
    stack.enter_context(patch("sova.supervisor.progression.check_dependency_gate", return_value=None))
    stack.enter_context(
        patch("sova.supervisor.progression.check_ownership_gate", new_callable=AsyncMock, return_value=(None, None))
    )
    stack.enter_context(patch("sova.supervisor.progression.get_alive_count", new_callable=AsyncMock, return_value=0))
    return stack.enter_context(
        patch("sova.supervisor.progression.collect_lookahead_holders", new_callable=AsyncMock, return_value=holders)
    )


class TestEngineWiring:
    @pytest.mark.asyncio
    async def test_same_cycle_overshoot_and_developer_unaffected(self) -> None:
        tasks = [_task(1, TaskState.RESEARCHED), _task(2, TaskState.TRIAGED), _task(3, TaskState.TRIAGED)]
        engine = _engine(tasks, queue=[2, 3, 1], research_lookahead=2)
        with ExitStack() as stack:
            _patched_gates(stack, [LookaheadHolder(1, RESEARCHED)])
            decisions = await engine.evaluate_all()

        by_issue = {d.issue_number: d for d in decisions}
        assert by_issue[2].action == ProgressionAction.SPAWN_RESEARCHER
        assert by_issue[3].action == ProgressionAction.BLOCKED
        assert [b.gate for b in by_issue[3].blocked_by] == ["research_lookahead"]
        assert "#2 researching" in by_issue[3].blocked_by[0].detail
        assert by_issue[1].action == ProgressionAction.SPAWN_DEVELOPER

    @pytest.mark.asyncio
    async def test_place_goes_to_the_spawn_the_plan_keeps(self) -> None:
        tasks = [_task(1, TaskState.TRIAGED), _task(2, TaskState.TRIAGED)]
        engine = _engine(tasks, queue=[1, 2], research_lookahead=1)
        plan = PlanResult(
            reasoning="", actions=(PlannedAction(action="spawn_researcher", issue=2, priority=1, reason=""),)
        )
        with ExitStack() as stack:
            _patched_gates(stack, [])
            decisions = await engine.evaluate_all(plan=plan)

        by_issue = {d.issue_number: d for d in decisions}
        assert by_issue[1].action == ProgressionAction.WAIT
        assert by_issue[2].action == ProgressionAction.SPAWN_RESEARCHER

    @pytest.mark.asyncio
    async def test_disjoint_plan_fallback_still_caps_researchers(self) -> None:
        tasks = [_task(1, TaskState.TRIAGED), _task(2, TaskState.TRIAGED)]
        engine = _engine(tasks, queue=[1, 2], research_lookahead=1)
        plan = PlanResult(
            reasoning="", actions=(PlannedAction(action="spawn_researcher", issue=99, priority=1, reason=""),)
        )
        with ExitStack() as stack:
            _patched_gates(stack, [])
            decisions = await engine.evaluate_all(plan=plan)

        by_issue = {d.issue_number: d for d in decisions}
        assert by_issue[1].action == ProgressionAction.SPAWN_RESEARCHER
        assert by_issue[2].action == ProgressionAction.BLOCKED
        assert [b.gate for b in by_issue[2].blocked_by] == ["research_lookahead"]

    @pytest.mark.asyncio
    async def test_lookahead_blocked_researcher_does_not_consume_a_slot(self) -> None:
        tasks = [_task(1, TaskState.TRIAGED), _task(2, TaskState.TRIAGED), _task(3, TaskState.RESEARCHED)]
        engine = _engine(tasks, queue=[1, 2, 3], research_lookahead=1)
        engine._config.max_parallel_agents = 2
        with ExitStack() as stack:
            _patched_gates(stack, [])
            decisions = await engine.evaluate_all()

        by_issue = {d.issue_number: d for d in decisions}
        assert by_issue[1].action == ProgressionAction.SPAWN_RESEARCHER
        assert by_issue[2].action == ProgressionAction.BLOCKED
        assert by_issue[3].action == ProgressionAction.SPAWN_DEVELOPER

    @pytest.mark.asyncio
    async def test_plan_dropped_researcher_does_not_consume_a_slot(self) -> None:
        tasks = [_task(1, TaskState.TRIAGED), _task(2, TaskState.RESEARCHED)]
        engine = _engine(tasks, queue=[1, 2], research_lookahead=2)
        engine._config.max_parallel_agents = 1
        plan = PlanResult(
            reasoning="", actions=(PlannedAction(action="spawn_developer", issue=2, priority=1, reason=""),)
        )
        with ExitStack() as stack:
            _patched_gates(stack, [])
            decisions = await engine.evaluate_all(plan=plan)

        by_issue = {d.issue_number: d for d in decisions}
        assert by_issue[1].action == ProgressionAction.WAIT
        assert by_issue[2].action == ProgressionAction.SPAWN_DEVELOPER

    @pytest.mark.asyncio
    async def test_limit_zero_never_blocks_and_does_no_counting(self) -> None:
        tasks = [_task(1, TaskState.TRIAGED), _task(2, TaskState.TRIAGED)]
        engine = _engine(tasks, queue=[1, 2], research_lookahead=0)
        with ExitStack() as stack:
            collect = _patched_gates(stack, [])
            decisions = await engine.evaluate_all()

        assert [d.action for d in decisions] == [ProgressionAction.SPAWN_RESEARCHER] * 2
        collect.assert_not_called()

    @pytest.mark.asyncio
    async def test_unavailable_count_fails_open(self) -> None:
        tasks = [_task(1, TaskState.TRIAGED), _task(2, TaskState.TRIAGED), _task(3, TaskState.TRIAGED)]
        engine = _engine(tasks, queue=[1, 2, 3], research_lookahead=1)
        with ExitStack() as stack:
            _patched_gates(stack, None)
            decisions = await engine.evaluate_all()

        assert [d.action for d in decisions] == [ProgressionAction.SPAWN_RESEARCHER] * 3

    @pytest.mark.asyncio
    async def test_evaluate_task_computes_holders_on_demand(self) -> None:
        tasks = [_task(1, TaskState.RESEARCHED), _task(2, TaskState.RESEARCHED), _task(3, TaskState.TRIAGED)]
        engine = _engine(tasks, queue=[1, 2, 3], research_lookahead=2)
        holders = [LookaheadHolder(1, RESEARCHED), LookaheadHolder(2, RESEARCHED)]
        with ExitStack() as stack:
            collect = _patched_gates(stack, holders)
            decision = await engine.evaluate_task(3)

        assert decision.action == ProgressionAction.BLOCKED
        assert [b.gate for b in decision.blocked_by] == ["research_lookahead"]
        collect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_gate_does_not_apply_to_developer_candidates(self) -> None:
        tasks = [_task(1, TaskState.RESEARCHED), _task(2, TaskState.RESEARCHED)]
        engine = _engine(tasks, queue=[1, 2], research_lookahead=1)
        with ExitStack() as stack:
            collect = _patched_gates(stack, [LookaheadHolder(1, RESEARCHED), LookaheadHolder(2, RESEARCHED)])
            decision = await engine.evaluate_task(1)

        assert decision.action == ProgressionAction.SPAWN_DEVELOPER
        collect.assert_not_called()
