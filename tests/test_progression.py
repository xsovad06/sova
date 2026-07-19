"""Tests for sova.supervisor.progression -- task progression engine."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.adapters.base import Task, TaskAdapter, TaskState
from sova.config.models import SupervisorConfig
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
    title: str = "",
    body: str = "",
    state: TaskState = TaskState.BACKLOG,
) -> Task:
    return Task(
        id=str(issue_id),
        title=title or f"Issue #{issue_id}",
        body=body,
        state=state,
    )


def _make_engine(
    config: SupervisorConfig | None = None,
    adapter: TaskAdapter | None = None,
) -> TaskProgressionEngine:
    """Create a TaskProgressionEngine with mock dependencies."""
    cfg = config or SupervisorConfig()
    mock_adapter = adapter or AsyncMock()
    mock_session_factory = MagicMock()
    return TaskProgressionEngine(
        config=cfg,
        adapter=mock_adapter,
        project_dir=MagicMock(),
        session_factory=mock_session_factory,
    )


# ---------------------------------------------------------------------------
# ProgressionAction / BlockReason / ProgressionDecision dataclass tests
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_progression_action_values(self) -> None:
        assert ProgressionAction.SPAWN_RESEARCHER == "spawn_researcher"
        assert ProgressionAction.SPAWN_DEVELOPER == "spawn_developer"
        assert ProgressionAction.SPAWN_INTEGRATE == "spawn_integrate"
        assert ProgressionAction.WAIT == "wait"
        assert ProgressionAction.BLOCKED == "blocked"
        assert ProgressionAction.CHECKPOINT_NEEDED == "checkpoint_needed"

    def test_block_reason_frozen(self) -> None:
        br = BlockReason(gate="dependency", detail="blocked by #10")
        assert br.gate == "dependency"
        assert br.detail == "blocked by #10"
        with pytest.raises(AttributeError):
            br.gate = "other"  # type: ignore[misc]

    def test_progression_decision_defaults(self) -> None:
        d = ProgressionDecision(
            issue_number=42,
            action=ProgressionAction.WAIT,
        )
        assert d.issue_number == 42
        assert d.role is None
        assert d.reason == ""
        assert d.blocked_by == ()

    def test_progression_decision_frozen(self) -> None:
        d = ProgressionDecision(issue_number=1, action=ProgressionAction.WAIT)
        with pytest.raises(AttributeError):
            d.issue_number = 2  # type: ignore[misc]

    def test_progression_decision_with_blockers(self) -> None:
        blockers = (
            BlockReason(gate="dependency", detail="blocked by #5"),
            BlockReason(gate="slots", detail="all slots full"),
        )
        d = ProgressionDecision(
            issue_number=10,
            action=ProgressionAction.BLOCKED,
            role="developer",
            reason="Blocked: multiple reasons",
            blocked_by=blockers,
        )
        assert len(d.blocked_by) == 2
        assert d.blocked_by[0].gate == "dependency"


# ---------------------------------------------------------------------------
# _determine_transition
# ---------------------------------------------------------------------------


class TestDetermineTransition:
    def test_triaged_auto_research_enabled(self) -> None:
        engine = _make_engine(SupervisorConfig(auto_research=True))
        assert engine._determine_transition(TaskState.TRIAGED) == ProgressionAction.SPAWN_RESEARCHER

    def test_triaged_auto_research_disabled(self) -> None:
        engine = _make_engine(SupervisorConfig(auto_research=False))
        assert engine._determine_transition(TaskState.TRIAGED) == ProgressionAction.CHECKPOINT_NEEDED

    def test_researched_auto_develop_enabled(self) -> None:
        engine = _make_engine(SupervisorConfig(auto_develop=True))
        assert engine._determine_transition(TaskState.RESEARCHED) == ProgressionAction.SPAWN_DEVELOPER

    def test_researched_auto_develop_disabled(self) -> None:
        engine = _make_engine(SupervisorConfig(auto_develop=False))
        assert engine._determine_transition(TaskState.RESEARCHED) == ProgressionAction.CHECKPOINT_NEEDED

    def test_in_review_auto_integrate_enabled(self) -> None:
        engine = _make_engine(SupervisorConfig(auto_integrate=True))
        assert engine._determine_transition(TaskState.IN_REVIEW) == ProgressionAction.SPAWN_INTEGRATE

    def test_in_review_auto_integrate_disabled(self) -> None:
        engine = _make_engine(SupervisorConfig(auto_integrate=False))
        assert engine._determine_transition(TaskState.IN_REVIEW) == ProgressionAction.CHECKPOINT_NEEDED

    def test_backlog_returns_none(self) -> None:
        engine = _make_engine()
        assert engine._determine_transition(TaskState.BACKLOG) is None

    def test_in_progress_returns_none(self) -> None:
        engine = _make_engine()
        assert engine._determine_transition(TaskState.IN_PROGRESS) is None

    def test_done_returns_none(self) -> None:
        engine = _make_engine()
        assert engine._determine_transition(TaskState.DONE) is None

    def test_needs_spec_returns_none(self) -> None:
        engine = _make_engine()
        assert engine._determine_transition(TaskState.NEEDS_SPEC) is None

    def test_human_only_returns_none(self) -> None:
        engine = _make_engine()
        assert engine._determine_transition(TaskState.HUMAN_ONLY) is None

    def test_all_states_covered(self) -> None:
        """Verify _determine_transition handles every TaskState value."""
        engine = _make_engine(
            SupervisorConfig(
                auto_research=True,
                auto_develop=True,
                auto_integrate=True,
            )
        )
        for state in TaskState:
            result = engine._determine_transition(state)
            assert result is None or isinstance(result, ProgressionAction)


# ---------------------------------------------------------------------------
# _check_dependency_gate
# ---------------------------------------------------------------------------


class TestDependencyGate:
    def test_no_deps_passes(self) -> None:
        from sova.supervisor.dependency_graph import DependencyGraph

        graph = DependencyGraph([_task(1, state=TaskState.TRIAGED)])
        engine = _make_engine()
        result = engine._check_dependency_gate(1, graph)
        assert result is None

    def test_deps_all_done_passes(self) -> None:
        from sova.supervisor.dependency_graph import DependencyGraph

        tasks = [
            _task(1, body="## Dependencies\n- #2\n", state=TaskState.TRIAGED),
            _task(2, state=TaskState.DONE),
        ]
        graph = DependencyGraph(tasks)
        engine = _make_engine()
        result = engine._check_dependency_gate(1, graph)
        assert result is None

    def test_deps_not_done_blocks(self) -> None:
        from sova.supervisor.dependency_graph import DependencyGraph

        tasks = [
            _task(1, body="## Dependencies\n- #2\n", state=TaskState.TRIAGED),
            _task(2, state=TaskState.IN_PROGRESS),
        ]
        graph = DependencyGraph(tasks)
        engine = _make_engine()
        result = engine._check_dependency_gate(1, graph)
        assert result is not None
        assert result.gate == "dependency"
        assert "#2" in result.detail

    def test_missing_dep_blocks(self) -> None:
        from sova.supervisor.dependency_graph import DependencyGraph

        tasks = [
            _task(1, body="## Dependencies\n- #99\n", state=TaskState.TRIAGED),
        ]
        graph = DependencyGraph(tasks)
        engine = _make_engine()
        result = engine._check_dependency_gate(1, graph)
        assert result is not None
        assert result.gate == "dependency"
        assert "missing" in result.detail.lower()


# ---------------------------------------------------------------------------
# _check_quota_gate
# ---------------------------------------------------------------------------


class TestQuotaGate:
    @pytest.mark.asyncio
    async def test_non_developer_action_skips(self) -> None:
        engine = _make_engine()
        result = await engine._check_quota_gate(ProgressionAction.SPAWN_RESEARCHER)
        assert result is None

    @pytest.mark.asyncio
    @patch("sova.supervisor.progression.load_config")
    async def test_quota_disabled_passes(self, mock_cfg: MagicMock) -> None:
        mock_cfg.return_value.coderabbit_quota.enabled = False
        engine = _make_engine()
        result = await engine._check_quota_gate(ProgressionAction.SPAWN_DEVELOPER)
        assert result is None

    @pytest.mark.asyncio
    @patch("sova.supervisor.coderabbit_quota.get_quota_status")
    @patch("sova.supervisor.progression.load_config")
    async def test_quota_exhausted_blocks(self, mock_cfg: MagicMock, mock_quota: AsyncMock) -> None:
        from sova.supervisor.coderabbit_quota import QuotaStatus

        mock_cfg.return_value.coderabbit_quota.enabled = True
        mock_quota.return_value = QuotaStatus(
            enabled=True,
            reviews_in_window=4,
            reviews_per_hour=4,
            can_create_pr=False,
            next_available_minutes=15.0,
            window_minutes=60,
        )
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        engine = _make_engine()
        engine._session_factory = MagicMock(return_value=mock_session)
        result = await engine._check_quota_gate(ProgressionAction.SPAWN_DEVELOPER)
        assert result is not None
        assert result.gate == "quota"


# ---------------------------------------------------------------------------
# _check_already_running
# ---------------------------------------------------------------------------


class TestAlreadyRunning:
    @pytest.mark.asyncio
    async def test_no_runs_passes(self) -> None:
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)
        engine = _make_engine()
        engine._session_factory = MagicMock(return_value=mock_session)
        result = await engine._check_already_running(42)
        assert result is None

    @pytest.mark.asyncio
    async def test_alive_process_blocks(self) -> None:
        mock_run = MagicMock()
        mock_run.id = 1
        mock_run.pid = 12345
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_run]
        mock_session.execute = AsyncMock(return_value=mock_result)
        engine = _make_engine()
        engine._session_factory = MagicMock(return_value=mock_session)
        with patch("sova.supervisor.progression._is_process_alive", return_value=True):
            result = await engine._check_already_running(42)
        assert result is not None
        assert result.gate == "already_running"

    @pytest.mark.asyncio
    async def test_dead_process_passes(self) -> None:
        mock_run = MagicMock()
        mock_run.id = 1
        mock_run.pid = 12345
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_run]
        mock_session.execute = AsyncMock(return_value=mock_result)
        engine = _make_engine()
        engine._session_factory = MagicMock(return_value=mock_session)
        with patch("sova.supervisor.progression._is_process_alive", return_value=False):
            result = await engine._check_already_running(42)
        assert result is None

    @pytest.mark.asyncio
    async def test_pending_run_blocks(self) -> None:
        """PID-less nonterminal runs (agent starting up) block as active reservations."""
        mock_run = MagicMock()
        mock_run.id = 7
        mock_run.pid = None
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_run]
        mock_session.execute = AsyncMock(return_value=mock_result)
        engine = _make_engine()
        engine._session_factory = MagicMock(return_value=mock_session)
        result = await engine._check_already_running(42)
        assert result is not None
        assert result.gate == "already_running"
        assert "PID not yet assigned" in result.detail


# ---------------------------------------------------------------------------
# _check_slot_gate
# ---------------------------------------------------------------------------


class TestSlotGate:
    @pytest.mark.asyncio
    @patch("sova.supervisor.progression.load_config")
    async def test_slots_available_passes(self, mock_cfg: MagicMock) -> None:
        mock_cfg.return_value.max_parallel_agents = 3
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)
        engine = _make_engine()
        engine._session_factory = MagicMock(return_value=mock_session)
        result = await engine._check_slot_gate()
        assert result is None

    @pytest.mark.asyncio
    @patch("sova.supervisor.progression.load_config")
    async def test_slots_full_blocks(self, mock_cfg: MagicMock) -> None:
        mock_cfg.return_value.max_parallel_agents = 2
        mock_run1, mock_run2 = MagicMock(), MagicMock()
        mock_run1.pid = 100
        mock_run2.pid = 200
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_run1, mock_run2]
        mock_session.execute = AsyncMock(return_value=mock_result)
        engine = _make_engine()
        engine._session_factory = MagicMock(return_value=mock_session)
        with patch("sova.supervisor.progression._is_process_alive", return_value=True):
            result = await engine._check_slot_gate()
        assert result is not None
        assert result.gate == "slots"

    @pytest.mark.asyncio
    @patch("sova.supervisor.progression.load_config")
    async def test_pending_run_counts_as_reservation(self, mock_cfg: MagicMock) -> None:
        """PID-less nonterminal runs count as active reservations for slot capacity."""
        mock_cfg.return_value.max_parallel_agents = 1
        mock_run = MagicMock()
        mock_run.pid = None  # pending: agent starting up
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_run]
        mock_session.execute = AsyncMock(return_value=mock_result)
        engine = _make_engine()
        engine._session_factory = MagicMock(return_value=mock_session)
        result = await engine._check_slot_gate()
        assert result is not None
        assert result.gate == "slots"


# ---------------------------------------------------------------------------
# _check_budget_gate
# ---------------------------------------------------------------------------


class TestBudgetGate:
    @pytest.mark.asyncio
    @patch("sova.supervisor.progression._check_issue_budget", new_callable=AsyncMock)
    async def test_within_budget_passes(self, mock_budget: AsyncMock) -> None:
        mock_budget.return_value = None
        engine = _make_engine()
        result = await engine._check_budget_gate(42)
        assert result is None

    @pytest.mark.asyncio
    @patch("sova.supervisor.progression._check_issue_budget", new_callable=AsyncMock)
    async def test_over_budget_blocks(self, mock_budget: AsyncMock) -> None:
        mock_budget.return_value = {"error": "Issue #42 exceeded budget ($5.00 / $3.00)"}
        engine = _make_engine()
        result = await engine._check_budget_gate(42)
        assert result is not None
        assert result.gate == "budget"
        assert "#42" in result.detail


# ---------------------------------------------------------------------------
# evaluate_task (integration of state machine + gates)
# ---------------------------------------------------------------------------


class TestEvaluateTask:
    @pytest.mark.asyncio
    async def test_backlog_returns_wait(self) -> None:
        adapter = AsyncMock()
        adapter.get_state = AsyncMock(return_value=TaskState.BACKLOG)
        adapter.list_tasks = AsyncMock(return_value=[_task(1, state=TaskState.BACKLOG)])
        engine = _make_engine(adapter=adapter)
        decision = await engine.evaluate_task(1)
        assert decision.action == ProgressionAction.WAIT
        assert decision.issue_number == 1

    @pytest.mark.asyncio
    async def test_triaged_with_auto_research_spawns(self) -> None:
        adapter = AsyncMock()
        adapter.get_state = AsyncMock(return_value=TaskState.TRIAGED)
        adapter.list_tasks = AsyncMock(return_value=[_task(1, state=TaskState.TRIAGED)])
        engine = _make_engine(
            config=SupervisorConfig(auto_research=True),
            adapter=adapter,
        )
        with (
            patch.object(engine, "_check_already_running", new_callable=AsyncMock, return_value=None),
            patch.object(engine, "_check_dependency_gate", return_value=None),
            patch.object(engine, "_check_quota_gate", new_callable=AsyncMock, return_value=None),
            patch.object(engine, "_check_slot_gate", new_callable=AsyncMock, return_value=None),
            patch.object(engine, "_check_budget_gate", new_callable=AsyncMock, return_value=None),
        ):
            decision = await engine.evaluate_task(1)
        assert decision.action == ProgressionAction.SPAWN_RESEARCHER
        assert decision.role == "researcher"

    @pytest.mark.asyncio
    async def test_triaged_blocked_by_dependency(self) -> None:
        adapter = AsyncMock()
        adapter.get_state = AsyncMock(return_value=TaskState.TRIAGED)
        tasks = [
            _task(1, body="## Dependencies\n- #2\n", state=TaskState.TRIAGED),
            _task(2, state=TaskState.IN_PROGRESS),
        ]
        adapter.list_tasks = AsyncMock(return_value=tasks)
        engine = _make_engine(
            config=SupervisorConfig(auto_research=True, respect_dependencies=True),
            adapter=adapter,
        )
        with (
            patch.object(engine, "_check_already_running", new_callable=AsyncMock, return_value=None),
            patch.object(engine, "_check_quota_gate", new_callable=AsyncMock, return_value=None),
            patch.object(engine, "_check_slot_gate", new_callable=AsyncMock, return_value=None),
            patch.object(engine, "_check_budget_gate", new_callable=AsyncMock, return_value=None),
        ):
            decision = await engine.evaluate_task(1)
        assert decision.action == ProgressionAction.BLOCKED
        assert any(b.gate == "dependency" for b in decision.blocked_by)

    @pytest.mark.asyncio
    async def test_triaged_dependencies_disabled(self) -> None:
        adapter = AsyncMock()
        adapter.get_state = AsyncMock(return_value=TaskState.TRIAGED)
        tasks = [
            _task(1, body="## Dependencies\n- #2\n", state=TaskState.TRIAGED),
            _task(2, state=TaskState.IN_PROGRESS),
        ]
        adapter.list_tasks = AsyncMock(return_value=tasks)
        engine = _make_engine(
            config=SupervisorConfig(auto_research=True, respect_dependencies=False),
            adapter=adapter,
        )
        with (
            patch.object(engine, "_check_already_running", new_callable=AsyncMock, return_value=None),
            patch.object(engine, "_check_quota_gate", new_callable=AsyncMock, return_value=None),
            patch.object(engine, "_check_slot_gate", new_callable=AsyncMock, return_value=None),
            patch.object(engine, "_check_budget_gate", new_callable=AsyncMock, return_value=None),
        ):
            decision = await engine.evaluate_task(1)
        assert decision.action == ProgressionAction.SPAWN_RESEARCHER

    @pytest.mark.asyncio
    async def test_researched_checkpoint_when_manual(self) -> None:
        adapter = AsyncMock()
        adapter.get_state = AsyncMock(return_value=TaskState.RESEARCHED)
        adapter.list_tasks = AsyncMock(return_value=[_task(1, state=TaskState.RESEARCHED)])
        engine = _make_engine(
            config=SupervisorConfig(auto_develop=False),
            adapter=adapter,
        )
        decision = await engine.evaluate_task(1)
        # auto_develop=False: a transition is possible but needs human approval
        assert decision.action == ProgressionAction.CHECKPOINT_NEEDED

    @pytest.mark.asyncio
    async def test_adapter_failure_returns_blocked(self) -> None:
        adapter = AsyncMock()
        adapter.get_state = AsyncMock(side_effect=Exception("API error"))
        engine = _make_engine(adapter=adapter)
        decision = await engine.evaluate_task(1)
        assert decision.action == ProgressionAction.BLOCKED
        assert "Failed to fetch" in decision.reason


# ---------------------------------------------------------------------------
# evaluate_all
# ---------------------------------------------------------------------------


class TestEvaluateAll:
    @pytest.mark.asyncio
    @patch("sova.supervisor.progression.load_config")
    async def test_mixed_states(self, mock_cfg: MagicMock) -> None:
        mock_cfg.return_value.max_parallel_agents = 5
        tasks = [
            _task(1, state=TaskState.TRIAGED),
            _task(2, state=TaskState.DONE),
            _task(3, state=TaskState.BACKLOG),
        ]
        adapter = AsyncMock()
        adapter.list_tasks = AsyncMock(return_value=tasks)
        engine = _make_engine(
            config=SupervisorConfig(auto_research=True),
            adapter=adapter,
        )
        with (
            patch.object(engine, "_check_already_running", new_callable=AsyncMock, return_value=None),
            patch.object(engine, "_check_dependency_gate", return_value=None),
            patch.object(engine, "_check_quota_gate", new_callable=AsyncMock, return_value=None),
            patch.object(engine, "_check_slot_gate", new_callable=AsyncMock, return_value=None),
            patch.object(engine, "_check_budget_gate", new_callable=AsyncMock, return_value=None),
            patch.object(engine, "_get_alive_count", new_callable=AsyncMock, return_value=0),
        ):
            decisions = await engine.evaluate_all()

        assert len(decisions) == 3
        by_issue = {d.issue_number: d for d in decisions}
        assert by_issue[1].action == ProgressionAction.SPAWN_RESEARCHER
        assert by_issue[2].action == ProgressionAction.WAIT
        assert by_issue[3].action == ProgressionAction.WAIT

    @pytest.mark.asyncio
    async def test_graph_build_failure_returns_empty(self) -> None:
        adapter = AsyncMock()
        adapter.list_tasks = AsyncMock(side_effect=Exception("Network error"))
        engine = _make_engine(adapter=adapter)
        decisions = await engine.evaluate_all()
        assert decisions == []

    @pytest.mark.asyncio
    @patch("sova.supervisor.progression.load_config")
    async def test_capacity_decremented_across_batch(self, mock_cfg: MagicMock) -> None:
        """Verify that evaluate_all decrements slot capacity as decisions are made."""
        mock_cfg.return_value.max_parallel_agents = 1
        tasks = [
            _task(1, state=TaskState.TRIAGED),
            _task(2, state=TaskState.TRIAGED),
        ]
        adapter = AsyncMock()
        adapter.list_tasks = AsyncMock(return_value=tasks)
        engine = _make_engine(
            config=SupervisorConfig(auto_research=True),
            adapter=adapter,
        )
        with (
            patch.object(engine, "_check_already_running", new_callable=AsyncMock, return_value=None),
            patch.object(engine, "_check_dependency_gate", return_value=None),
            patch.object(engine, "_check_quota_gate", new_callable=AsyncMock, return_value=None),
            patch.object(engine, "_check_slot_gate", new_callable=AsyncMock, return_value=None),
            patch.object(engine, "_check_budget_gate", new_callable=AsyncMock, return_value=None),
            patch.object(engine, "_get_alive_count", new_callable=AsyncMock, return_value=0),
        ):
            decisions = await engine.evaluate_all()

        assert len(decisions) == 2
        actionable = [d for d in decisions if d.action == ProgressionAction.SPAWN_RESEARCHER]
        blocked = [d for d in decisions if d.action == ProgressionAction.BLOCKED]
        # Only 1 slot available, so first task passes, second is blocked
        assert len(actionable) == 1
        assert len(blocked) == 1
        assert any(b.gate == "slots" for b in blocked[0].blocked_by)

    @pytest.mark.asyncio
    @patch("sova.supervisor.progression.load_config")
    async def test_quota_does_not_block_researcher(self, mock_cfg: MagicMock) -> None:
        """Verify precomputed quota blocker only applies to developer actions."""
        mock_cfg.return_value.max_parallel_agents = 5
        tasks = [
            _task(1, state=TaskState.TRIAGED),  # -> SPAWN_RESEARCHER
        ]
        adapter = AsyncMock()
        adapter.list_tasks = AsyncMock(return_value=tasks)
        engine = _make_engine(
            config=SupervisorConfig(auto_research=True),
            adapter=adapter,
        )
        quota_block = BlockReason(gate="quota", detail="CodeRabbit quota exhausted")
        with (
            patch.object(engine, "_check_already_running", new_callable=AsyncMock, return_value=None),
            patch.object(engine, "_check_dependency_gate", return_value=None),
            patch.object(engine, "_check_quota_gate", new_callable=AsyncMock, return_value=quota_block),
            patch.object(engine, "_check_slot_gate", new_callable=AsyncMock, return_value=None),
            patch.object(engine, "_check_budget_gate", new_callable=AsyncMock, return_value=None),
            patch.object(engine, "_get_alive_count", new_callable=AsyncMock, return_value=0),
        ):
            decisions = await engine.evaluate_all()

        assert len(decisions) == 1
        # Researcher should NOT be blocked by developer quota
        assert decisions[0].action == ProgressionAction.SPAWN_RESEARCHER


# ---------------------------------------------------------------------------
# execute_decision / execute_decisions
# ---------------------------------------------------------------------------


class TestExecuteDecision:
    @pytest.mark.asyncio
    async def test_wait_skipped(self) -> None:
        engine = _make_engine()
        decision = ProgressionDecision(
            issue_number=1,
            action=ProgressionAction.WAIT,
            reason="No transition",
        )
        result = await engine.execute_decision(decision)
        assert result["skipped"] is True

    @pytest.mark.asyncio
    async def test_blocked_skipped(self) -> None:
        engine = _make_engine()
        decision = ProgressionDecision(
            issue_number=1,
            action=ProgressionAction.BLOCKED,
            reason="Dependency gate",
        )
        result = await engine.execute_decision(decision)
        assert result["skipped"] is True

    @pytest.mark.asyncio
    async def test_checkpoint_skipped(self) -> None:
        engine = _make_engine()
        decision = ProgressionDecision(
            issue_number=1,
            action=ProgressionAction.CHECKPOINT_NEEDED,
            reason="Human approval needed",
        )
        result = await engine.execute_decision(decision)
        assert result["skipped"] is True

    @pytest.mark.asyncio
    @patch("sova.dashboard.services.agent_lifecycle.start_agent", new_callable=AsyncMock)
    async def test_spawn_researcher_calls_start_agent(self, mock_start: AsyncMock) -> None:
        mock_start.return_value = {"run_id": 1}
        engine = _make_engine()
        decision = ProgressionDecision(
            issue_number=42,
            action=ProgressionAction.SPAWN_RESEARCHER,
            role="researcher",
            reason="Ready",
        )
        result = await engine.execute_decision(decision)
        mock_start.assert_called_once_with(issue="42", role="researcher")
        assert result["run_id"] == 1

    @pytest.mark.asyncio
    @patch("sova.dashboard.services.agent_lifecycle.start_agent", new_callable=AsyncMock)
    async def test_spawn_developer_calls_start_agent(self, mock_start: AsyncMock) -> None:
        mock_start.return_value = {"run_id": 2}
        engine = _make_engine()
        decision = ProgressionDecision(
            issue_number=10,
            action=ProgressionAction.SPAWN_DEVELOPER,
            role="developer",
            reason="Ready",
        )
        result = await engine.execute_decision(decision)
        mock_start.assert_called_once_with(issue="10", role="developer")
        assert result["run_id"] == 2

    @pytest.mark.asyncio
    @patch("sova.dashboard.services.agent_lifecycle.start_agent", new_callable=AsyncMock)
    async def test_spawn_integrate_needs_pr(self, mock_start: AsyncMock) -> None:
        mock_start.return_value = {"run_id": 3}
        engine = _make_engine()
        engine._find_pr_for_issue = AsyncMock(return_value=55)
        decision = ProgressionDecision(
            issue_number=20,
            action=ProgressionAction.SPAWN_INTEGRATE,
            role="command:integrate-pr",
            reason="Ready",
        )
        result = await engine.execute_decision(decision)
        mock_start.assert_called_once_with(issue="20", role="command:integrate-pr", pr_number=55)
        assert result["run_id"] == 3

    @pytest.mark.asyncio
    async def test_spawn_integrate_no_pr_errors(self) -> None:
        engine = _make_engine()
        engine._find_pr_for_issue = AsyncMock(return_value=None)
        decision = ProgressionDecision(
            issue_number=20,
            action=ProgressionAction.SPAWN_INTEGRATE,
            role="command:integrate-pr",
            reason="Ready",
        )
        result = await engine.execute_decision(decision)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_execute_decisions_filters_non_actionable(self) -> None:
        engine = _make_engine()
        decisions = [
            ProgressionDecision(issue_number=1, action=ProgressionAction.WAIT),
            ProgressionDecision(issue_number=2, action=ProgressionAction.BLOCKED),
            ProgressionDecision(issue_number=3, action=ProgressionAction.CHECKPOINT_NEEDED),
        ]
        results = await engine.execute_decisions(decisions)
        assert results == []


# ---------------------------------------------------------------------------
# SupervisorConfig
# ---------------------------------------------------------------------------


class TestSupervisorConfig:
    def test_defaults(self) -> None:
        cfg = SupervisorConfig()
        assert cfg.enabled is False
        assert cfg.auto_research is True
        assert cfg.auto_develop is False
        assert cfg.auto_address_review is False
        assert cfg.auto_integrate is False
        assert cfg.respect_dependencies is True
        assert cfg.poll_interval_seconds == 120

    def test_custom_values(self) -> None:
        cfg = SupervisorConfig(
            enabled=True,
            auto_research=False,
            auto_develop=True,
            auto_integrate=True,
            poll_interval_seconds=60,
        )
        assert cfg.enabled is True
        assert cfg.auto_research is False
        assert cfg.auto_develop is True
        assert cfg.poll_interval_seconds == 60

    def test_poll_interval_must_be_positive(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            SupervisorConfig(poll_interval_seconds=0)

    def test_env_prefix(self) -> None:
        assert SupervisorConfig.model_config["env_prefix"] == "SOVA_SUPERVISOR_"


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------


class TestConfigIntegration:
    def test_supervisor_field_on_project_config(self) -> None:
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        assert isinstance(cfg.supervisor, SupervisorConfig)
        assert cfg.supervisor.enabled is False

    def test_supervisor_toml_loading(self, tmp_path: Path) -> None:
        """Verify [supervisor] section is actually loaded from sova.toml."""
        from sova.config.loader import load_config

        toml_file = tmp_path / "sova.toml"
        toml_file.write_text("[supervisor]\nenabled = true\nauto_research = false\npoll_interval_seconds = 30\n")
        cfg = load_config(tmp_path)
        assert cfg.supervisor.enabled is True
        assert cfg.supervisor.auto_research is False
        assert cfg.supervisor.poll_interval_seconds == 30

    def test_supervisor_settings_metadata(self) -> None:
        from sova.dashboard.settings_meta import GROUP_ORDER, GROUPS, get_meta

        assert "supervisor" in GROUPS
        assert "supervisor" in GROUP_ORDER

        expected_keys = [
            "supervisor.enabled",
            "supervisor.auto_research",
            "supervisor.auto_develop",
            "supervisor.auto_address_review",
            "supervisor.auto_integrate",
            "supervisor.respect_dependencies",
            "supervisor.poll_interval_seconds",
        ]
        for key in expected_keys:
            meta = get_meta(key)
            assert meta is not None, f"Missing settings metadata for {key}"
            assert meta.group == "supervisor"


# ---------------------------------------------------------------------------
# ResourcesConfig
# ---------------------------------------------------------------------------


class TestResourcesConfig:
    def test_defaults(self) -> None:
        from sova.config.models import ResourcesConfig

        cfg = ResourcesConfig()
        assert cfg.memory_block_threshold_gb == 1.0
        assert cfg.memory_warn_threshold_gb == 2.0

    def test_custom_values(self) -> None:
        from sova.config.models import ResourcesConfig

        cfg = ResourcesConfig(memory_block_threshold_gb=0.5, memory_warn_threshold_gb=1.5)
        assert cfg.memory_block_threshold_gb == 0.5
        assert cfg.memory_warn_threshold_gb == 1.5

    def test_field_on_project_config(self) -> None:
        from sova.config.models import ProjectConfig, ResourcesConfig

        cfg = ProjectConfig()
        assert isinstance(cfg.resources, ResourcesConfig)

    def test_toml_loading(self, tmp_path: Path) -> None:
        from sova.config.loader import load_config

        toml_file = tmp_path / "sova.toml"
        toml_file.write_text("[resources]\nmemory_block_threshold_gb = 0.5\nmemory_warn_threshold_gb = 3.0\n")
        cfg = load_config(tmp_path)
        assert cfg.resources.memory_block_threshold_gb == 0.5
        assert cfg.resources.memory_warn_threshold_gb == 3.0

    def test_settings_metadata(self) -> None:
        from sova.dashboard.settings_meta import GROUP_ORDER, GROUPS, get_meta

        assert "resources" in GROUPS
        assert "resources" in GROUP_ORDER

        expected_keys = [
            "resources.memory_block_threshold_gb",
            "resources.memory_warn_threshold_gb",
        ]
        for key in expected_keys:
            meta = get_meta(key)
            assert meta is not None, f"Missing settings metadata for {key}"
            assert meta.group == "resources"

    def test_env_prefix(self) -> None:
        from sova.config.models import ResourcesConfig

        assert ResourcesConfig.model_config["env_prefix"] == "SOVA_RESOURCES_"


# ---------------------------------------------------------------------------
# _check_memory_pressure_gate
# ---------------------------------------------------------------------------


class TestMemoryPressureGate:
    def test_below_block_threshold_blocks(self) -> None:
        from sova.config.models import ProjectConfig

        engine = _make_engine()
        mem_mock = MagicMock()
        mem_mock.available = int(0.5 * 1024**3)  # 0.5 GB
        cfg = ProjectConfig(resources={"memory_block_threshold_gb": 1.0, "memory_warn_threshold_gb": 2.0})
        with patch("psutil.virtual_memory", return_value=mem_mock):
            result = engine._check_memory_pressure_gate(cfg)
        assert result is not None
        assert result.gate == "memory"
        assert "0.5" in result.detail

    def test_above_block_threshold_passes(self) -> None:
        from sova.config.models import ProjectConfig

        engine = _make_engine()
        mem_mock = MagicMock()
        mem_mock.available = int(4.0 * 1024**3)  # 4 GB
        cfg = ProjectConfig(resources={"memory_block_threshold_gb": 1.0, "memory_warn_threshold_gb": 2.0})
        with patch("psutil.virtual_memory", return_value=mem_mock):
            result = engine._check_memory_pressure_gate(cfg)
        assert result is None

    def test_between_warn_and_block_logs_warning(self) -> None:
        from sova.config.models import ProjectConfig

        engine = _make_engine()
        mem_mock = MagicMock()
        mem_mock.available = int(1.5 * 1024**3)  # 1.5 GB (between 1.0 block and 2.0 warn)
        cfg = ProjectConfig(resources={"memory_block_threshold_gb": 1.0, "memory_warn_threshold_gb": 2.0})
        with (
            patch("psutil.virtual_memory", return_value=mem_mock),
            patch("sova.supervisor.progression.log") as mock_log,
        ):
            result = engine._check_memory_pressure_gate(cfg)
        assert result is None  # warn does not block
        mock_log.warning.assert_called_once()

    def test_psutil_unavailable_fails_open(self) -> None:
        """When psutil is not installed, the gate fails open (no block)."""
        engine = _make_engine()
        with patch("sova.supervisor.progression._PSUTIL_AVAILABLE", False):
            result = engine._check_memory_pressure_gate()
        assert result is None

    def test_psutil_exception_fails_open(self) -> None:
        """Any exception during the check fails open."""
        engine = _make_engine()
        with patch("psutil.virtual_memory", side_effect=RuntimeError("fake error")):
            result = engine._check_memory_pressure_gate()
        assert result is None

    @pytest.mark.asyncio
    @patch("sova.supervisor.progression.load_config")
    async def test_memory_gate_integrated_in_evaluate_all(self, mock_cfg: MagicMock) -> None:
        """Verify that memory pressure blocks all tasks in evaluate_all."""
        from sova.config.models import ResourcesConfig

        mock_cfg.return_value.max_parallel_agents = 5
        mock_cfg.return_value.resources = ResourcesConfig(memory_block_threshold_gb=2.0)
        tasks = [_task(1, state=TaskState.TRIAGED)]
        adapter = AsyncMock()
        adapter.list_tasks = AsyncMock(return_value=tasks)
        engine = _make_engine(
            config=SupervisorConfig(auto_research=True),
            adapter=adapter,
        )
        memory_block = BlockReason(gate="memory", detail="System memory pressure: 1.0 GB available < 2.0 GB threshold")
        with (
            patch.object(engine, "_check_already_running", new_callable=AsyncMock, return_value=None),
            patch.object(engine, "_check_dependency_gate", return_value=None),
            patch.object(engine, "_check_quota_gate", new_callable=AsyncMock, return_value=None),
            patch.object(engine, "_check_budget_gate", new_callable=AsyncMock, return_value=None),
            patch.object(engine, "_get_alive_count", new_callable=AsyncMock, return_value=0),
            patch.object(engine, "_check_memory_pressure_gate", return_value=memory_block),
        ):
            decisions = await engine.evaluate_all()

        assert len(decisions) == 1
        assert decisions[0].action == ProgressionAction.BLOCKED
        assert any(b.gate == "memory" for b in decisions[0].blocked_by)
