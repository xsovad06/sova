"""Tests for sova.core -- state machine, context, steps, and workflow engine."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from sova.adapters.base import Task, TaskState
from sova.config.models import ProjectConfig
from sova.core.context import ExecutionContext
from sova.core.state import InvalidTransitionError, TaskStatus, get_valid_transitions
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.core.workflow import WorkflowEngine
from sova.db.models import FailureRecord, StepExecution, TaskRun
from sova.db.session import close_db, get_session, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    """Initialize an in-memory DB for workflow engine tests."""
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


def _mock_adapter() -> AsyncMock:
    """Create a mock TaskAdapter for tests."""
    adapter = AsyncMock()
    adapter.get_state.return_value = TaskState.RESEARCHED
    adapter.get_task.return_value = Task(id="1", title="Test issue")
    return adapter


def _make_ctx(**kwargs) -> ExecutionContext:
    """Create a test ExecutionContext with sensible defaults."""
    defaults = {
        "project_dir": Path("/tmp/test"),
        "config": ProjectConfig(),
        "adapter": _mock_adapter(),
        "issue_number": "42",
        "role": "developer",
    }
    defaults.update(kwargs)
    return ExecutionContext(**defaults)


# ---------------------------------------------------------------------------
# TaskStatus (state machine)
# ---------------------------------------------------------------------------


class TestTaskStatus:
    def test_all_states_exist(self) -> None:
        expected = {
            "PENDING",
            "ASSESSING",
            "RESEARCHED",
            "IN_PROGRESS",
            "DEVELOPING",
            "SIMPLIFYING",
            "REVIEWING",
            "PUSHING",
            "PR_CREATED",
            "CI_MONITORING",
            "AUTOMATED_REVIEW",
            "ADDRESSING_REVIEW",
            "DONE",
            "PAUSED",
            "FAILED",
            "REJECTED",
        }
        actual = {s.name for s in TaskStatus}
        assert expected == actual

    def test_pending_transitions(self) -> None:
        valid = get_valid_transitions(TaskStatus.PENDING)
        assert TaskStatus.ASSESSING in valid
        assert TaskStatus.PAUSED in valid
        assert TaskStatus.FAILED in valid

    def test_assessing_can_reject(self) -> None:
        valid = get_valid_transitions(TaskStatus.ASSESSING)
        assert TaskStatus.REJECTED in valid

    def test_developing_transitions(self) -> None:
        valid = get_valid_transitions(TaskStatus.DEVELOPING)
        assert TaskStatus.SIMPLIFYING in valid
        assert TaskStatus.PAUSED in valid
        assert TaskStatus.FAILED in valid

    def test_done_is_terminal(self) -> None:
        valid = get_valid_transitions(TaskStatus.DONE)
        assert len(valid) == 0

    def test_rejected_is_terminal(self) -> None:
        valid = get_valid_transitions(TaskStatus.REJECTED)
        assert len(valid) == 0

    def test_failed_is_terminal(self) -> None:
        valid = get_valid_transitions(TaskStatus.FAILED)
        assert len(valid) == 0

    def test_paused_can_resume(self) -> None:
        valid = get_valid_transitions(TaskStatus.PAUSED)
        assert TaskStatus.PENDING in valid
        assert TaskStatus.DEVELOPING in valid

    def test_happy_path_sequence(self) -> None:
        """The happy path through the pipeline is valid."""
        happy_path = [
            TaskStatus.PENDING,
            TaskStatus.ASSESSING,
            TaskStatus.IN_PROGRESS,
            TaskStatus.DEVELOPING,
            TaskStatus.SIMPLIFYING,
            TaskStatus.REVIEWING,
            TaskStatus.PUSHING,
            TaskStatus.PR_CREATED,
            TaskStatus.CI_MONITORING,
            TaskStatus.AUTOMATED_REVIEW,
            TaskStatus.ADDRESSING_REVIEW,
            TaskStatus.DONE,
        ]
        for i in range(len(happy_path) - 1):
            current = happy_path[i]
            next_state = happy_path[i + 1]
            valid = get_valid_transitions(current)
            assert next_state in valid, f"Cannot transition from {current} to {next_state}"

    def test_validate_transition_raises_on_invalid(self) -> None:
        from sova.core.state import validate_transition

        with pytest.raises(InvalidTransitionError):
            validate_transition(TaskStatus.DONE, TaskStatus.PENDING)

    def test_validate_transition_allows_valid(self) -> None:
        from sova.core.state import validate_transition

        validate_transition(TaskStatus.PENDING, TaskStatus.ASSESSING)


# ---------------------------------------------------------------------------
# ExecutionContext
# ---------------------------------------------------------------------------


class TestExecutionContext:
    def test_create_minimal(self) -> None:
        ctx = _make_ctx()
        assert ctx.project_dir == Path("/tmp/test")
        assert ctx.issue_number == "42"
        assert ctx.role == "developer"
        assert ctx.cost_usd == Decimal("0")

    def test_add_cost(self) -> None:
        ctx = _make_ctx()
        ctx.add_cost(Decimal("1.50"))
        ctx.add_cost(Decimal("0.75"))
        assert ctx.cost_usd == Decimal("2.25")

    def test_budget_exceeded(self) -> None:
        config = ProjectConfig(agent={"max_budget": Decimal("5.00")})
        ctx = _make_ctx(config=config)
        ctx.add_cost(Decimal("5.01"))
        assert ctx.is_budget_exceeded

    def test_budget_not_exceeded(self) -> None:
        config = ProjectConfig(agent={"max_budget": Decimal("5.00")})
        ctx = _make_ctx(config=config)
        ctx.add_cost(Decimal("4.99"))
        assert not ctx.is_budget_exceeded

    def test_working_dir_defaults_to_project(self) -> None:
        ctx = _make_ctx()
        assert ctx.working_dir == Path("/tmp/test")

    def test_working_dir_uses_worktree(self) -> None:
        ctx = _make_ctx(worktree_dir=Path("/tmp/wt"))
        assert ctx.working_dir == Path("/tmp/wt")

    def test_repo_property(self) -> None:
        ctx = _make_ctx(config=ProjectConfig(github_repo="owner/repo"))
        assert ctx.repo == "owner/repo"


# ---------------------------------------------------------------------------
# BaseStep / GateCheckResult / StepResult
# ---------------------------------------------------------------------------


class DummyStep(BaseStep):
    """A test step with configurable behavior."""

    name = "dummy"
    max_retries = 1

    def __init__(
        self,
        *,
        should_pass: bool = True,
        gate_pass: bool = True,
        skip: bool = False,
        name: str = "dummy",
    ) -> None:
        self.name = name
        self._should_pass = should_pass
        self._gate_pass = gate_pass
        self._skip = skip

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        if self._should_pass:
            return StepResult(success=True, summary="Dummy passed")
        return StepResult(success=False, summary="Dummy failed", error="test error")

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        if self._gate_pass:
            return GateCheckResult(passed=True)
        return GateCheckResult(passed=False, reason="Gate failed")

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        return self.name in ctx.completed_steps or self._skip


class TestStepResult:
    def test_success(self) -> None:
        r = StepResult(success=True, summary="ok")
        assert r.success
        assert r.error is None

    def test_failure(self) -> None:
        r = StepResult(success=False, summary="bad", error="something broke")
        assert not r.success
        assert r.error == "something broke"


class TestGateCheckResult:
    def test_pass(self) -> None:
        r = GateCheckResult(passed=True)
        assert r.passed
        assert r.reason is None

    def test_fail(self) -> None:
        r = GateCheckResult(passed=False, reason="No code changes")
        assert not r.passed
        assert r.reason == "No code changes"


class TestBaseStep:
    async def test_execute_success(self) -> None:
        step = DummyStep(should_pass=True)
        ctx = _make_ctx()
        result = await step.execute(ctx)
        assert result.success

    async def test_execute_failure(self) -> None:
        step = DummyStep(should_pass=False)
        ctx = _make_ctx()
        result = await step.execute(ctx)
        assert not result.success

    async def test_gate_check(self) -> None:
        step = DummyStep(gate_pass=True)
        ctx = _make_ctx()
        gate = await step.validate_output(ctx)
        assert gate.passed

    async def test_can_skip(self) -> None:
        step = DummyStep(skip=True)
        ctx = _make_ctx()
        assert await step.can_skip(ctx)


# ---------------------------------------------------------------------------
# WorkflowEngine
# ---------------------------------------------------------------------------


class TestWorkflowEngine:
    async def test_run_single_step_success(self) -> None:
        ctx = _make_ctx()
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert result.success
        assert result.steps_completed == 1
        assert result.steps_failed == 0

    async def test_run_step_failure_records_error(self) -> None:
        ctx = _make_ctx()
        step = DummyStep(should_pass=False, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert not result.success
        assert result.steps_failed == 1

    async def test_run_gate_check_failure_pauses(self) -> None:
        ctx = _make_ctx()
        step = DummyStep(should_pass=True, gate_pass=False)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert not result.success
        assert result.final_status == TaskStatus.PAUSED

    async def test_skipped_step(self) -> None:
        ctx = _make_ctx()
        step1 = DummyStep(should_pass=True, gate_pass=True, skip=True)
        step2 = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step1, step2], ctx=ctx)

        result = await engine.run()

        assert result.success
        assert result.steps_completed == 1
        assert result.steps_skipped == 1

    async def test_multiple_steps_happy_path(self) -> None:
        ctx = _make_ctx()
        steps = [
            DummyStep(should_pass=True, gate_pass=True),
            DummyStep(should_pass=True, gate_pass=True),
            DummyStep(should_pass=True, gate_pass=True),
        ]
        engine = WorkflowEngine(steps=steps, ctx=ctx)

        result = await engine.run()

        assert result.success
        assert result.steps_completed == 3
        assert result.final_status == TaskStatus.DONE

    async def test_budget_exceeded_pauses(self) -> None:
        config = ProjectConfig(agent={"max_budget": Decimal("0.01")})
        ctx = _make_ctx(config=config)
        ctx.add_cost(Decimal("0.02"))

        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert not result.success
        assert result.final_status == TaskStatus.PAUSED

    async def test_step_retry_on_failure(self) -> None:
        """A step with max_retries > 0 retries before failing."""
        ctx = _make_ctx()

        step = DummyStep(should_pass=False)
        step.max_retries = 1
        call_count = 0

        async def flaky_execute(ctx: ExecutionContext) -> StepResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return StepResult(success=False, summary="Flaky fail", error="transient")
            return StepResult(success=True, summary="Succeeded on retry")

        step.execute = flaky_execute

        engine = WorkflowEngine(steps=[step], ctx=ctx)
        result = await engine.run()

        assert result.success
        assert call_count == 2

    async def test_step_records_populated(self) -> None:
        ctx = _make_ctx()
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert len(result.step_records) == 1
        assert result.step_records[0].step_name == "dummy"
        assert result.step_records[0].status == "completed"


# ---------------------------------------------------------------------------
# Step implementations (unit tests for individual steps)
# ---------------------------------------------------------------------------


class TestSyncStep:
    async def test_execute_syncs_branch(self) -> None:
        from sova.core.steps.sync import SyncStep

        ctx = _make_ctx()
        step = SyncStep()

        with patch("sova.core.steps.sync.git_ops") as mock_ops:
            mock_ops.sync_branch = AsyncMock()
            result = await step.execute(ctx)

        assert result.success
        mock_ops.sync_branch.assert_awaited_once_with("main", cwd=Path("/tmp/test"))


class TestAssessStep:
    async def test_researched_issue_passes(self) -> None:
        from sova.core.steps.assess import AssessStep

        adapter = _mock_adapter()
        adapter.get_state.return_value = TaskState.RESEARCHED
        ctx = _make_ctx(adapter=adapter)
        step = AssessStep()

        result = await step.execute(ctx)

        assert result.success

    async def test_non_researched_issue_fails(self) -> None:
        from sova.core.steps.assess import AssessStep

        adapter = _mock_adapter()
        adapter.get_state.return_value = TaskState.BACKLOG
        ctx = _make_ctx(adapter=adapter)
        step = AssessStep()

        result = await step.execute(ctx)

        assert not result.success

    async def test_force_mode_skips_gate(self) -> None:
        from sova.core.steps.assess import AssessStep

        ctx = _make_ctx(force=True)
        step = AssessStep()
        assert await step.can_skip(ctx)


class TestWorktreeStep:
    async def test_creates_worktree(self) -> None:
        from sova.core.steps.create_worktree import WorktreeStep

        ctx = _make_ctx(issue_number="42", branch_name="feat/test")
        step = WorktreeStep()

        with patch("sova.core.steps.create_worktree.worktree") as mock_wt:
            mock_info = MagicMock()
            mock_info.path = Path("/tmp/test/.claude/worktrees/42")
            mock_wt.create_worktree = AsyncMock(return_value=mock_info)
            result = await step.execute(ctx)

        assert result.success
        assert ctx.worktree_dir == Path("/tmp/test/.claude/worktrees/42")


class TestDevelopStep:
    async def test_gate_check_requires_code_changes(self) -> None:
        from sova.core.steps.develop import DevelopStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with patch("sova.core.steps.develop.run") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="")
            gate = await step.validate_output(ctx)

        assert not gate.passed
        assert "no code changes" in gate.reason.lower()

    async def test_gate_check_passes_with_changes(self) -> None:
        from sova.core.steps.develop import DevelopStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with patch("sova.core.steps.develop.run") as mock_run:
            mock_run.return_value = MagicMock(
                success=True,
                stdout=" src/main.py | 10 +++++++---\n 1 file changed\n",
            )
            gate = await step.validate_output(ctx)

        assert gate.passed


class TestPushStep:
    async def test_gate_check_requires_commits_ahead(self) -> None:
        from sova.core.steps.push import PushStep

        ctx = _make_ctx(branch_name="feat/test", worktree_dir=Path("/tmp/worktree"))
        step = PushStep()

        with patch("sova.core.steps.push.run") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="0\n")
            gate = await step.validate_output(ctx)

        assert not gate.passed


class TestCreatePRStep:
    async def test_gate_check_requires_pr_number(self) -> None:
        from sova.core.steps.create_pr import CreatePRStep

        ctx = _make_ctx(pr_number=None)
        step = CreatePRStep()
        gate = await step.validate_output(ctx)
        assert not gate.passed

    async def test_gate_check_passes_with_pr(self) -> None:
        from sova.core.steps.create_pr import CreatePRStep

        ctx = _make_ctx(pr_number=99)
        step = CreatePRStep()
        gate = await step.validate_output(ctx)
        assert gate.passed


class TestCompleteStep:
    async def test_completes_and_transitions_tracker(self) -> None:
        from sova.core.steps.complete import CompleteStep

        adapter = _mock_adapter()
        ctx = _make_ctx(adapter=adapter, pr_number=42)
        step = CompleteStep()

        result = await step.execute(ctx)

        assert result.success
        adapter.transition_state.assert_awaited_once_with("42", TaskState.DONE)


class TestStepRegistry:
    def test_get_developer_steps_returns_all(self) -> None:
        from sova.core.steps import get_developer_steps

        steps = get_developer_steps()
        names = [s.name for s in steps]
        assert names == [
            "sync",
            "assess",
            "create_worktree",
            "develop",
            "simplify",
            "self_review",
            "push",
            "create_pr",
            "monitor_ci",
            "automated_review",
            "address_review",
            "complete",
        ]


# ---------------------------------------------------------------------------
# WorkflowEngine DB persistence
# ---------------------------------------------------------------------------


class TestWorkflowDB:
    async def test_creates_task_run_record(self) -> None:
        ctx = _make_ctx()
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert result.task_run_id is not None
        session = await get_session()
        async with session.begin():
            task_run = await session.get(TaskRun, result.task_run_id)
            assert task_run is not None
            assert task_run.issue_number == "42"
            assert task_run.role == "developer"

    async def test_task_run_finalized_on_success(self) -> None:
        ctx = _make_ctx()
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        session = await get_session()
        async with session.begin():
            task_run = await session.get(TaskRun, result.task_run_id)
            assert task_run.status == TaskStatus.DONE.value
            assert task_run.ended_at is not None

    async def test_step_execution_created(self) -> None:
        ctx = _make_ctx()
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        session = await get_session()
        async with session.begin():
            rows = (
                (await session.execute(select(StepExecution).where(StepExecution.task_run_id == result.task_run_id)))
                .scalars()
                .all()
            )
            assert len(rows) == 1
            assert rows[0].step_name == "dummy"
            assert rows[0].status == "passed"

    async def test_failure_creates_failure_record(self) -> None:
        ctx = _make_ctx()
        step = DummyStep(should_pass=False, gate_pass=True)
        step.max_retries = 0
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert not result.success
        session = await get_session()
        async with session.begin():
            rows = (
                (await session.execute(select(FailureRecord).where(FailureRecord.task_run_id == result.task_run_id)))
                .scalars()
                .all()
            )
            assert len(rows) == 1
            assert rows[0].failure_type == "exception"
            assert rows[0].step_name == "dummy"

    async def test_gate_failure_creates_failure_record(self) -> None:
        ctx = _make_ctx()
        step = DummyStep(should_pass=True, gate_pass=False)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert result.final_status == TaskStatus.PAUSED
        session = await get_session()
        async with session.begin():
            rows = (
                (await session.execute(select(FailureRecord).where(FailureRecord.task_run_id == result.task_run_id)))
                .scalars()
                .all()
            )
            assert len(rows) == 1
            assert rows[0].failure_type == "gate_check"

    async def test_budget_exceeded_creates_failure_record(self) -> None:
        config = ProjectConfig(agent={"max_budget": Decimal("0.01")})
        ctx = _make_ctx(config=config)
        ctx.add_cost(Decimal("0.02"))

        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert result.final_status == TaskStatus.PAUSED
        session = await get_session()
        async with session.begin():
            rows = (
                (await session.execute(select(FailureRecord).where(FailureRecord.task_run_id == result.task_run_id)))
                .scalars()
                .all()
            )
            assert len(rows) == 1
            assert rows[0].failure_type == "budget_exceeded"

    async def test_multiple_steps_create_multiple_step_executions(self) -> None:
        ctx = _make_ctx()
        steps = [
            DummyStep(should_pass=True, gate_pass=True),
            DummyStep(should_pass=True, gate_pass=True),
        ]
        # Give them distinct names
        steps[0].name = "step_a"
        steps[1].name = "step_b"
        engine = WorkflowEngine(steps=steps, ctx=ctx)

        result = await engine.run()

        session = await get_session()
        async with session.begin():
            rows = (
                (
                    await session.execute(
                        select(StepExecution)
                        .where(StepExecution.task_run_id == result.task_run_id)
                        .order_by(StepExecution.id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 2
            assert rows[0].step_name == "step_a"
            assert rows[1].step_name == "step_b"

    async def test_task_run_id_set_on_context(self) -> None:
        ctx = _make_ctx()
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        await engine.run()

        assert ctx.task_run_id is not None

    async def test_resumed_from_id_stored(self) -> None:
        ctx = _make_ctx(resume_run_id=99)
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        session = await get_session()
        async with session.begin():
            task_run = await session.get(TaskRun, result.task_run_id)
            assert task_run.resumed_from_id == 99


# ---------------------------------------------------------------------------
# Checkpoint / Resume
# ---------------------------------------------------------------------------


class TestCheckpointResume:
    async def test_completed_steps_skips_steps(self) -> None:
        """Steps in completed_steps are skipped via can_skip()."""
        ctx = _make_ctx(completed_steps=frozenset({"step_a"}))

        step_a = DummyStep(should_pass=True, gate_pass=True, name="step_a")
        step_b = DummyStep(should_pass=True, gate_pass=True, name="step_b")

        engine = WorkflowEngine(steps=[step_a, step_b], ctx=ctx)
        result = await engine.run()

        assert result.success
        assert result.steps_skipped == 1
        assert result.steps_completed == 1

    async def test_failed_step_not_skipped(self) -> None:
        """Steps NOT in completed_steps execute normally."""
        ctx = _make_ctx(completed_steps=frozenset({"step_a"}))

        step_a = DummyStep(should_pass=True, gate_pass=True, name="step_a")
        step_b = DummyStep(should_pass=False, gate_pass=True, name="step_b")

        engine = WorkflowEngine(steps=[step_a, step_b], ctx=ctx)
        result = await engine.run()

        assert not result.success
        assert result.steps_skipped == 1
        assert result.steps_failed == 1

    async def test_empty_completed_steps_runs_all(self) -> None:
        """With no completed_steps, all steps execute."""
        ctx = _make_ctx(completed_steps=frozenset())

        step_a = DummyStep(should_pass=True, gate_pass=True, name="step_a")
        step_b = DummyStep(should_pass=True, gate_pass=True, name="step_b")

        engine = WorkflowEngine(steps=[step_a, step_b], ctx=ctx)
        result = await engine.run()

        assert result.success
        assert result.steps_skipped == 0
        assert result.steps_completed == 2

    async def test_resume_preserves_cost_from_context(self) -> None:
        """Resumed run's budget check uses pre-loaded cost."""
        ctx = _make_ctx(
            completed_steps=frozenset({"step_a"}),
            cost_usd=Decimal("100"),
        )
        ctx.config.agent.max_budget = Decimal("50")

        step_a = DummyStep(should_pass=True, gate_pass=True, name="step_a")
        step_b = DummyStep(should_pass=True, gate_pass=True, name="step_b")

        engine = WorkflowEngine(steps=[step_a, step_b], ctx=ctx)
        result = await engine.run()

        assert not result.success
        assert result.final_status == TaskStatus.PAUSED
        assert "Budget exceeded" in (result.error or "")
