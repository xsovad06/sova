"""Tests for sova.core -- state machine, context, steps, and workflow engine."""

from __future__ import annotations

import asyncio
import json
import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from sova.adapters.base import Task, TaskState
from sova.config.models import ProjectConfig, TaskSourceConfig
from sova.core.context import ExecutionContext
from sova.core.state import InvalidTransitionError, TaskStatus, get_valid_transitions
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.core.workflow import WorkflowEngine
from sova.db.models import CostRecord, FailureRecord, StepExecution, TaskRun
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
            "RUNNING",
            "PENDING",
            "ASSESSING",
            "RESEARCHED",
            "IN_PROGRESS",
            "DEVELOPING",
            "SIMPLIFYING",
            "REVIEWING",
            "COMMITTING",
            "PUSHING",
            "PR_CREATED",
            "CI_MONITORING",
            "AUTOMATED_REVIEW",
            "ADDRESSING_REVIEW",
            "AWAITING_APPROVAL",
            "DONE",
            "PAUSED",
            "FAILED",
            "REJECTED",
        }
        actual = {s.name for s in TaskStatus}
        assert expected == actual

    def test_running_transitions(self) -> None:
        valid = get_valid_transitions(TaskStatus.RUNNING)
        assert TaskStatus.PENDING in valid
        assert TaskStatus.ADDRESSING_REVIEW in valid
        assert TaskStatus.PAUSED in valid
        assert TaskStatus.FAILED in valid
        assert TaskStatus.DONE not in valid

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

    def test_awaiting_approval_can_resume(self) -> None:
        valid = get_valid_transitions(TaskStatus.AWAITING_APPROVAL)
        assert TaskStatus.PENDING in valid
        assert TaskStatus.DEVELOPING in valid
        assert TaskStatus.DONE not in valid

    def test_awaiting_approval_reachable_from_non_terminal(self) -> None:
        valid = get_valid_transitions(TaskStatus.DEVELOPING)
        assert TaskStatus.AWAITING_APPROVAL in valid

    def test_awaiting_approval_not_reachable_from_terminal(self) -> None:
        valid = get_valid_transitions(TaskStatus.DONE)
        assert TaskStatus.AWAITING_APPROVAL not in valid

    def test_awaiting_approval_in_task_run_terminal(self) -> None:
        from sova.core.state import TASK_RUN_TERMINAL

        assert "awaiting_approval" in TASK_RUN_TERMINAL

    def test_paused_in_task_run_terminal(self) -> None:
        """Paused runs are terminal for DB queries (process has exited)."""
        from sova.core.state import TASK_RUN_TERMINAL

        assert "paused" in TASK_RUN_TERMINAL

    def test_happy_path_sequence(self) -> None:
        """The happy path through the pipeline is valid."""
        happy_path = [
            TaskStatus.PENDING,
            TaskStatus.ASSESSING,
            TaskStatus.IN_PROGRESS,
            TaskStatus.DEVELOPING,
            TaskStatus.SIMPLIFYING,
            TaskStatus.REVIEWING,
            TaskStatus.COMMITTING,
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

    def test_issueless_context(self) -> None:
        ctx = _make_ctx(issue_number="", run_label="planner-1234")
        assert not ctx.has_issue
        assert ctx.display_label == "planner-1234"
        assert ctx.issue_number == ""

    def test_issueless_context_no_label(self) -> None:
        ctx = _make_ctx(issue_number="", run_label="", task_run_id=99)
        assert not ctx.has_issue
        assert ctx.display_label == "run-99"

    def test_issueless_context_fallback(self) -> None:
        ctx = _make_ctx(issue_number="", run_label="")
        assert ctx.display_label == "issue-less"

    def test_issue_context_display(self) -> None:
        ctx = _make_ctx(issue_number="42")
        assert ctx.has_issue
        assert ctx.display_label == "#42"


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
        assert result.step_records[0].status == "done"

    async def test_adopt_existing_task_run(self) -> None:
        """When ctx.task_run_id is set, the engine should reuse it instead of creating a new one."""
        async with await get_session() as session:
            async with session.begin():
                existing = TaskRun(
                    issue_number="42",
                    role="developer",
                    status="running",
                    current_step="agent",
                    pid=99999,
                )
                session.add(existing)
                await session.flush()
                existing_id = existing.id

        ctx = _make_ctx(task_run_id=existing_id)
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert result.success
        assert result.task_run_id == existing_id

        async with await get_session() as session:
            task_run = await session.get(TaskRun, existing_id)
            assert task_run.pid == 99999  # PID preserved from dashboard
            assert task_run.current_step != "agent"

    async def test_adopt_does_not_create_extra_task_run(self) -> None:
        """Adopting must not create a second TaskRun for the same issue."""
        async with await get_session() as session:
            async with session.begin():
                existing = TaskRun(
                    issue_number="42",
                    role="developer",
                    status="running",
                    current_step="agent",
                    pid=88888,
                )
                session.add(existing)
                await session.flush()
                existing_id = existing.id

        ctx = _make_ctx(task_run_id=existing_id)
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)
        await engine.run()

        async with await get_session() as session:
            from sqlalchemy import func

            count = (await session.execute(select(func.count(TaskRun.id)).where(TaskRun.issue_number == "42"))).scalar()
            assert count == 1

    async def test_no_run_id_creates_new_task_run(self) -> None:
        """Without task_run_id, the engine should create its own TaskRun (backward compat)."""
        ctx = _make_ctx()
        assert ctx.task_run_id is None

        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)
        result = await engine.run()

        assert result.success
        assert ctx.task_run_id is not None
        assert result.task_run_id == ctx.task_run_id

    async def test_create_step_execution_db_error_retries(self) -> None:
        """When _create_step_execution raises, the engine retries and can still succeed."""
        ctx = _make_ctx()
        step = DummyStep(should_pass=True, gate_pass=True)
        step.max_retries = 1
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        call_count = 0
        original_create = engine._create_step_execution

        async def failing_create(step_name: str, retry_count: int = 0) -> int:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("DB connection lost")
            return await original_create(step_name, retry_count=retry_count)

        engine._create_step_execution = failing_create
        result = await engine.run()

        assert result.success
        assert call_count == 2
        assert result.step_records[0].retries == 1

    async def test_create_step_execution_db_error_exhausts_retries(self) -> None:
        """When _create_step_execution fails on every attempt, the step fails."""
        ctx = _make_ctx()
        step = DummyStep(should_pass=True, gate_pass=True)
        step.max_retries = 0
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        async def always_failing_create(step_name: str, retry_count: int = 0) -> int:
            raise RuntimeError("DB permanently down")

        engine._create_step_execution = always_failing_create
        result = await engine.run()

        assert not result.success
        assert result.steps_failed == 1
        record = result.step_records[0]
        assert record.status == "failed"
        assert record.result is not None
        assert "DB error" in (record.result.summary or "")

    async def test_update_step_execution_db_error_non_fatal(self) -> None:
        """When _update_step_execution raises, the step still succeeds."""
        ctx = _make_ctx()
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        async def failing_update(step_exec_id: int, result: StepResult, elapsed_ms: int) -> None:
            raise RuntimeError("DB write failed")

        engine._update_step_execution = failing_update
        result = await engine.run()

        assert result.success
        assert result.steps_completed == 1

    async def test_awaiting_approval_pauses_pipeline(self) -> None:
        """A step returning awaiting_approval=True pauses the pipeline."""
        ctx = _make_ctx()

        class ApprovalStep(BaseStep):
            name = "needs_approval"

            async def execute(self, ctx_inner: ExecutionContext) -> StepResult:
                return StepResult(success=True, summary="Needs human approval", awaiting_approval=True)

            async def validate_output(self, ctx_inner: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

        step1 = ApprovalStep()
        step2 = DummyStep(should_pass=True, gate_pass=True, name="after_approval")
        engine = WorkflowEngine(steps=[step1, step2], ctx=ctx)

        result = await engine.run()

        assert not result.success
        assert result.final_status == TaskStatus.AWAITING_APPROVAL
        assert result.steps_completed == 1
        # Second step should NOT have run
        assert len(result.step_records) == 1
        assert result.step_records[0].step_name == "needs_approval"

    async def test_awaiting_approval_default_false_no_pause(self) -> None:
        """Steps returning default awaiting_approval=False do not pause."""
        ctx = _make_ctx()
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert result.success
        assert result.final_status == TaskStatus.DONE

    async def test_awaiting_approval_with_handoff_actions(self) -> None:
        """A step returning handoff_actions writes a handoff file."""
        from sova.ipc.handoff import HandoffAction

        ctx = _make_ctx()
        actions = [
            HandoffAction(
                id="approve_spec",
                label="Approve",
                description="Approve the spec",
                style="approve",
                auto_execute=False,
            ),
        ]

        class ApprovalWithActionsStep(BaseStep):
            name = "spec"

            async def execute(self, ctx_inner: ExecutionContext) -> StepResult:
                return StepResult(
                    success=True,
                    summary="Spec ready for approval",
                    awaiting_approval=True,
                    handoff_actions=actions,
                )

            async def validate_output(self, ctx_inner: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

        step = ApprovalWithActionsStep()
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        with patch("sova.ipc.handoff.write_handoff_file") as mock_write:
            result = await engine.run()

        assert not result.success
        assert result.final_status == TaskStatus.AWAITING_APPROVAL
        mock_write.assert_called_once()
        written_handoff = mock_write.call_args[0][1]
        assert written_handoff.status == "awaiting_action"
        assert len(written_handoff.next_actions) == 1
        assert written_handoff.next_actions[0].id == "approve_spec"

    async def test_step_hard_timeout(self) -> None:
        """Steps exceeding the hard timeout are terminated with a failure."""

        class SlowStep(BaseStep):
            name = "slow"
            max_retries = 0

            async def execute(self, ctx: ExecutionContext) -> StepResult:
                await asyncio.sleep(999)
                return StepResult(success=True, summary="should not reach here")

            async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

        config = ProjectConfig(agent={"step_timeout": 1})
        ctx = _make_ctx(config=config)
        step = SlowStep()
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert not result.success
        assert result.steps_failed == 1
        assert result.error == "step_hard_timeout"


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

    async def test_existing_pr_adopted_into_context(self) -> None:
        """When a fresh developer run finds an existing PR, AssessStep should
        adopt it (set ctx.pr_number + ctx.branch_name) and succeed, routing
        the pipeline into the address-review variant."""
        from sova.core.steps.assess import AssessStep
        from sova.git.pr import PRInfo

        adapter = _mock_adapter()
        adapter.get_state.return_value = TaskState.IN_PROGRESS
        ctx = _make_ctx(adapter=adapter, issue_number="42")
        step = AssessStep()

        with (
            patch("sova.core.steps.assess.find_pr_for_issue", new_callable=AsyncMock) as mock_find,
            patch("sova.core.steps.assess.get_pr_branch", new_callable=AsyncMock, return_value="feat/issue-42"),
        ):
            mock_find.return_value = PRInfo(number=99, url="https://github.com/test/repo/pull/99")
            result = await step.execute(ctx)

        assert result.success, f"Expected success, got: {result.error}"
        assert "Adopted existing PR #99" in result.summary
        assert ctx.pr_number == 99
        assert ctx.branch_name == "feat/issue-42"

    async def test_existing_pr_adoption_uses_prinfo_branch_without_api_call(self) -> None:
        """When PRInfo already carries a branch, AssessStep must use it directly
        and must NOT call get_pr_branch (avoids an extra GitHub API round-trip)."""
        from sova.core.steps.assess import AssessStep
        from sova.git.pr import PRInfo

        adapter = _mock_adapter()
        adapter.get_state.return_value = TaskState.IN_PROGRESS
        ctx = _make_ctx(adapter=adapter, issue_number="42")
        step = AssessStep()

        with (
            patch("sova.core.steps.assess.find_pr_for_issue", new_callable=AsyncMock) as mock_find,
            patch("sova.core.steps.assess.get_pr_branch", new_callable=AsyncMock) as mock_get_branch,
        ):
            mock_find.return_value = PRInfo(
                number=99, url="https://github.com/test/repo/pull/99", branch="feat/issue-42"
            )
            result = await step.execute(ctx)

        assert result.success
        assert ctx.pr_number == 99
        assert ctx.branch_name == "feat/issue-42"
        mock_get_branch.assert_not_awaited()

    async def test_existing_pr_adoption_sets_pr_number_even_without_branch(self) -> None:
        """PR adoption should still succeed when get_pr_branch returns empty string."""
        from sova.core.steps.assess import AssessStep
        from sova.git.pr import PRInfo

        adapter = _mock_adapter()
        adapter.get_state.return_value = TaskState.IN_PROGRESS
        ctx = _make_ctx(adapter=adapter, issue_number="42")
        step = AssessStep()

        with (
            patch("sova.core.steps.assess.find_pr_for_issue", new_callable=AsyncMock) as mock_find,
            patch("sova.core.steps.assess.get_pr_branch", new_callable=AsyncMock, return_value=""),
        ):
            mock_find.return_value = PRInfo(number=99, url="https://github.com/test/repo/pull/99")
            result = await step.execute(ctx)

        assert result.success
        assert ctx.pr_number == 99

    async def test_existing_pr_allowed_when_pr_number_set(self) -> None:
        from sova.core.steps.assess import AssessStep

        adapter = _mock_adapter()
        adapter.get_state.return_value = TaskState.IN_PROGRESS
        ctx = _make_ctx(adapter=adapter, pr_number=99)
        step = AssessStep()

        with patch("sova.core.steps.assess.find_pr_for_issue", new_callable=AsyncMock) as mock_find:
            result = await step.execute(ctx)

        mock_find.assert_not_called()
        assert result.success

    async def test_existing_pr_allowed_with_force(self) -> None:
        from sova.core.steps.assess import AssessStep

        adapter = _mock_adapter()
        adapter.get_state.return_value = TaskState.IN_PROGRESS
        ctx = _make_ctx(adapter=adapter, force=True)
        step = AssessStep()

        with patch("sova.core.steps.assess.find_pr_for_issue", new_callable=AsyncMock) as mock_find:
            result = await step.execute(ctx)

        mock_find.assert_not_called()
        assert result.success

    async def test_no_existing_pr_passes(self) -> None:
        from sova.core.steps.assess import AssessStep

        adapter = _mock_adapter()
        adapter.get_state.return_value = TaskState.RESEARCHED
        ctx = _make_ctx(adapter=adapter)
        step = AssessStep()

        with patch("sova.core.steps.assess.find_pr_for_issue", new_callable=AsyncMock) as mock_find:
            mock_find.return_value = None
            result = await step.execute(ctx)

        assert result.success


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


class TestEnsureWorktreeStep:
    async def test_skips_when_worktree_exists(self) -> None:
        from sova.core.steps.ensure_worktree import EnsureWorktreeStep

        wt = Path("/tmp/test/.claude/worktrees/42")
        ctx = _make_ctx(worktree_dir=wt, branch_name="feat/issue-42")
        step = EnsureWorktreeStep()

        with patch.object(Path, "exists", return_value=True):
            assert await step.can_skip(ctx) is True

    async def test_fails_without_branch_name(self) -> None:
        from sova.core.steps.ensure_worktree import EnsureWorktreeStep

        ctx = _make_ctx(branch_name="")
        step = EnsureWorktreeStep()
        result = await step.execute(ctx)
        assert not result.success
        assert "branch_name" in result.error

    async def test_finds_existing_worktree_by_branch(self) -> None:
        from sova.core.steps.ensure_worktree import EnsureWorktreeStep

        ctx = _make_ctx(branch_name="feat/issue-42", project_dir=Path("/tmp/proj"))
        step = EnsureWorktreeStep()
        wt_path = Path("/tmp/proj/.claude/worktrees/42")

        with patch(
            "sova.core.steps.ensure_worktree.find_worktree_by_branch",
            new_callable=AsyncMock,
            return_value=wt_path,
        ):
            result = await step.execute(ctx)

        assert result.success
        assert ctx.worktree_dir == wt_path

    async def test_creates_worktree_for_pr(self) -> None:
        from sova.core.steps.ensure_worktree import EnsureWorktreeStep

        ctx = _make_ctx(
            issue_number="",
            pr_number=55,
            branch_name="fix/standalone",
            project_dir=Path("/tmp/proj"),
        )
        step = EnsureWorktreeStep()
        wt_path = Path("/tmp/proj/.claude/worktrees/pr-55")

        with (
            patch(
                "sova.core.steps.ensure_worktree.find_worktree_by_branch",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "sova.core.steps.ensure_worktree.create_worktree",
                new_callable=AsyncMock,
            ) as mock_create,
            patch.object(
                EnsureWorktreeStep,
                "_resolve_base_branch",
                new_callable=AsyncMock,
                return_value="fix/standalone",
            ),
        ):
            mock_info = MagicMock()
            mock_info.path = wt_path
            mock_create.return_value = mock_info
            result = await step.execute(ctx)

        assert result.success
        assert ctx.worktree_dir == wt_path
        mock_create.assert_awaited_once()
        call_kwargs = mock_create.call_args
        assert call_kwargs[1]["issue_id"] == "pr-55"

    async def test_rejects_project_dir_as_worktree(self) -> None:
        from sova.core.steps.ensure_worktree import EnsureWorktreeStep

        project = Path("/tmp/proj")
        ctx = _make_ctx(
            worktree_dir=project,
            branch_name="feat/issue-42",
            project_dir=project,
        )
        step = EnsureWorktreeStep()

        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "resolve", return_value=project),
        ):
            assert await step.can_skip(ctx) is False

    async def test_clears_project_dir_worktree_and_discovers(self) -> None:
        from sova.core.steps.ensure_worktree import EnsureWorktreeStep

        project = Path("/tmp/proj")
        wt_path = Path("/tmp/proj/.claude/worktrees/42")
        ctx = _make_ctx(
            worktree_dir=project,
            branch_name="feat/issue-42",
            project_dir=project,
        )
        step = EnsureWorktreeStep()

        with patch(
            "sova.core.steps.ensure_worktree.find_worktree_by_branch",
            new_callable=AsyncMock,
            return_value=wt_path,
        ):
            result = await step.execute(ctx)

        assert result.success
        assert ctx.worktree_dir == wt_path

    async def test_validate_output_rejects_project_dir(self) -> None:
        from sova.core.steps.ensure_worktree import EnsureWorktreeStep

        project = Path("/tmp/proj")
        ctx = _make_ctx(worktree_dir=project, project_dir=project)
        step = EnsureWorktreeStep()

        with patch.object(Path, "exists", return_value=True):
            gate = await step.validate_output(ctx)

        assert not gate.passed
        assert "project root" in gate.reason

    async def test_resolve_base_branch_prefers_local(self) -> None:
        from sova.core.steps.ensure_worktree import EnsureWorktreeStep

        mock_result = MagicMock(success=True, stdout="abc123\n")
        with patch("sova.core.steps.ensure_worktree.run", new_callable=AsyncMock, return_value=mock_result):
            branch = await EnsureWorktreeStep._resolve_base_branch("feat/test", Path("/tmp"))

        assert branch == "feat/test"

    async def test_resolve_base_branch_falls_back_to_origin(self) -> None:
        from sova.core.steps.ensure_worktree import EnsureWorktreeStep

        fail = MagicMock(success=False)
        ok = MagicMock(success=True, stdout="abc123\n")
        with patch("sova.core.steps.ensure_worktree.run", new_callable=AsyncMock, side_effect=[fail, ok]):
            branch = await EnsureWorktreeStep._resolve_base_branch("feat/test", Path("/tmp"))

        assert branch == "origin/feat/test"

    async def test_resolve_base_branch_returns_name_when_unresolved(self) -> None:
        from sova.core.steps.ensure_worktree import EnsureWorktreeStep

        fail = MagicMock(success=False)
        with patch("sova.core.steps.ensure_worktree.run", new_callable=AsyncMock, return_value=fail):
            branch = await EnsureWorktreeStep._resolve_base_branch("feat/test", Path("/tmp"))

        assert branch == "feat/test"


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

    async def test_gate_check_passes_with_only_untracked_files(self) -> None:
        """Gate must pass when Claude wrote new files but never staged them."""
        from sova.core.steps.develop import DevelopStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with patch("sova.core.steps.develop.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # git diff --stat HEAD (no tracked changes)
                MagicMock(success=True, stdout=""),  # git diff --cached --stat (nothing staged)
                MagicMock(success=True, stdout=""),  # git log base..HEAD (no commits)
                MagicMock(success=True, stdout="?? navigator.py\n?? tests/test_navigator.py\n"),  # untracked
            ]
            gate = await step.validate_output(ctx)

        assert gate.passed

    async def test_gate_check_fails_with_all_signals_empty(self) -> None:
        """Gate must fail when no diffs, no commits, and no untracked files exist."""
        from sova.core.steps.develop import DevelopStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with patch("sova.core.steps.develop.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # git diff --stat HEAD
                MagicMock(success=True, stdout=""),  # git diff --cached --stat
                MagicMock(success=True, stdout=""),  # git log base..HEAD
                MagicMock(success=True, stdout=""),  # git status --porcelain
            ]
            gate = await step.validate_output(ctx)

        assert not gate.passed
        assert "no code changes" in gate.reason.lower()

    async def test_gate_check_ignores_non_untracked_porcelain_lines(self) -> None:
        """Only lines starting with '??' count as untracked; modified lines do not."""
        from sova.core.steps.develop import DevelopStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with patch("sova.core.steps.develop.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # git diff --stat HEAD
                MagicMock(success=True, stdout=""),  # git diff --cached --stat
                MagicMock(success=True, stdout=""),  # git log base..HEAD
                MagicMock(success=True, stdout=" M modified.py\n"),  # porcelain: modified but tracked
            ]
            gate = await step.validate_output(ctx)

        assert not gate.passed

    async def test_gate_check_fails_with_only_lockfile_commits(self) -> None:
        """Gate must fail when only lockfile/metadata files were changed (#532)."""
        from sova.core.steps.develop import DevelopStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with patch("sova.core.steps.develop.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # git diff --stat HEAD
                MagicMock(success=True, stdout=""),  # git diff --cached --stat
                MagicMock(success=True, stdout="abc123 chore(deps): update lockfile\n"),  # commits exist
                MagicMock(success=True, stdout=""),  # git status --porcelain (no untracked)
                MagicMock(success=True, stdout="Pipfile.lock\n"),  # git diff --name-only base..HEAD
            ]
            gate = await step.validate_output(ctx)

        assert not gate.passed
        assert "no substantive" in gate.reason.lower()

    async def test_gate_check_fails_with_only_metadata_commits(self) -> None:
        """Gate must fail when only .sova/ or package-lock.json files changed (#532)."""
        from sova.core.steps.develop import DevelopStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with patch("sova.core.steps.develop.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # git diff --stat HEAD
                MagicMock(success=True, stdout=""),  # git diff --cached --stat
                MagicMock(success=True, stdout="abc123 chore: update metadata\n"),  # commits exist
                MagicMock(success=True, stdout=""),  # git status --porcelain
                MagicMock(
                    success=True,
                    stdout="package-lock.json\n.sova/test-baseline.json\nyarn.lock\n",
                ),  # only metadata
            ]
            gate = await step.validate_output(ctx)

        assert not gate.passed

    async def test_gate_check_passes_with_mixed_lockfile_and_source(self) -> None:
        """Gate must pass when lockfile changes accompany real source changes."""
        from sova.core.steps.develop import DevelopStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with patch("sova.core.steps.develop.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # git diff --stat HEAD
                MagicMock(success=True, stdout=""),  # git diff --cached --stat
                MagicMock(success=True, stdout="abc123 feat: add feature\n"),  # commits exist
                MagicMock(success=True, stdout=""),  # git status --porcelain
                MagicMock(
                    success=True,
                    stdout="Pipfile.lock\nsrc/feature.py\ntests/test_feature.py\n",
                ),  # lockfile + source
            ]
            gate = await step.validate_output(ctx)

        assert gate.passed

    async def test_gate_check_passes_with_only_source_commits(self) -> None:
        """Gate must pass when commits contain only source code changes."""
        from sova.core.steps.develop import DevelopStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with patch("sova.core.steps.develop.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # git diff --stat HEAD
                MagicMock(success=True, stdout=""),  # git diff --cached --stat
                MagicMock(success=True, stdout="abc123 feat: new endpoint\n"),  # commits exist
                MagicMock(success=True, stdout=""),  # git status --porcelain
                MagicMock(success=True, stdout="src/api/handler.py\ntests/test_handler.py\n"),
            ]
            gate = await step.validate_output(ctx)

        assert gate.passed

    async def test_gate_check_still_passes_with_unstaged_changes(self) -> None:
        """When there are unstaged source file changes, the gate passes."""
        from sova.core.steps.develop import DevelopStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with patch("sova.core.steps.develop.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" src/main.py | 10 ++++\n"),  # git diff --stat HEAD
                MagicMock(success=True, stdout=""),  # git diff --cached --stat
                MagicMock(success=True, stdout=""),  # git log base..HEAD
                MagicMock(success=True, stdout=""),  # git status --porcelain
                MagicMock(success=True, stdout="src/main.py\n"),  # git diff --name-only HEAD
                MagicMock(success=True, stdout=""),  # git diff --cached --name-only
            ]
            gate = await step.validate_output(ctx)

        assert gate.passed

    async def test_gate_check_fails_with_only_unstaged_lockfile(self) -> None:
        """Gate must fail when only unstaged lockfile changes exist."""
        from sova.core.steps.develop import DevelopStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with patch("sova.core.steps.develop.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" Pipfile.lock | 50 ++++\n"),  # git diff --stat HEAD
                MagicMock(success=True, stdout=""),  # git diff --cached --stat
                MagicMock(success=True, stdout=""),  # git log base..HEAD
                MagicMock(success=True, stdout=""),  # git status --porcelain
                MagicMock(success=True, stdout="Pipfile.lock\n"),  # git diff --name-only HEAD
                MagicMock(success=True, stdout=""),  # git diff --cached --name-only
            ]
            gate = await step.validate_output(ctx)

        assert not gate.passed
        assert "no substantive" in gate.reason.lower()

    async def test_gate_check_fails_with_only_untracked_metadata(self) -> None:
        """Gate must fail when only untracked .sova/ or .claude/ metadata exists."""
        from sova.core.steps.develop import DevelopStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with patch("sova.core.steps.develop.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # git diff --stat HEAD
                MagicMock(success=True, stdout=""),  # git diff --cached --stat
                MagicMock(success=True, stdout=""),  # git log base..HEAD
                MagicMock(success=True, stdout="?? .sova/test-baseline.json\n?? .claude/memory.md\n"),
            ]
            gate = await step.validate_output(ctx)

        assert not gate.passed
        assert "no substantive" in gate.reason.lower()

    async def test_gate_check_passes_with_mixed_unstaged_lockfile_and_source(self) -> None:
        """Gate must pass when unstaged lockfile changes accompany source changes."""
        from sova.core.steps.develop import DevelopStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with patch("sova.core.steps.develop.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" Pipfile.lock | 50 ++++\nsrc/app.py | 5 ++\n"),
                MagicMock(success=True, stdout=""),  # git diff --cached --stat
                MagicMock(success=True, stdout=""),  # git log base..HEAD
                MagicMock(success=True, stdout=""),  # git status --porcelain
                MagicMock(success=True, stdout="Pipfile.lock\nsrc/app.py\n"),  # --name-only HEAD
                MagicMock(success=True, stdout=""),  # git diff --cached --name-only
            ]
            gate = await step.validate_output(ctx)

        assert gate.passed


class TestCommitStep:
    async def test_commits_uncommitted_changes(self) -> None:
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        ctx.task = Task(id="73", title="LLM provider abstraction")
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" config/base.py | 5 +++++\n"),  # diff --stat
                MagicMock(success=True, stdout=""),  # staged
                MagicMock(success=True, stdout="apps/ai/__init__.py\n"),  # untracked
                MagicMock(success=True, stdout=""),  # log (no commits yet)
            ]
            result = await step.execute(ctx)

        assert result.success
        mock_commit.assert_awaited_once()
        msg = mock_commit.call_args[0][0]
        assert "LLM provider abstraction" in msg
        assert "Closes #42" in msg

    async def test_normalizes_double_prefixed_task_title(self) -> None:
        """Generated commit messages should not contain duplicate conventional prefixes."""
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        ctx.task = Task(id="73", title="feat(core): validate commit messages")
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" commit.py | 3 +++\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
            ]
            result = await step.execute(ctx)

        assert result.success
        msg = mock_commit.call_args[0][0]
        assert msg.startswith("feat(core): validate commit messages")
        assert "feat(core): feat(core):" not in msg
        assert "Closes #42" in msg

    async def test_normalizes_scope_with_digits(self) -> None:
        """Generated commit messages should normalize scopes containing digits."""
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        ctx.task = Task(id="73", title="feat(api2): validate commit messages")
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" commit.py | 3 +++\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
            ]
            result = await step.execute(ctx)

        assert result.success
        msg = mock_commit.call_args[0][0]
        assert msg.startswith("feat(core): validate commit messages")
        assert "Closes #42" in msg

    async def test_skips_when_already_committed(self) -> None:
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = CommitStep()

        with patch("sova.core.steps.commit.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # diff --stat (clean)
                MagicMock(success=True, stdout=""),  # staged (clean)
                MagicMock(success=True, stdout=""),  # untracked (clean)
                MagicMock(success=True, stdout="abc123 feat: something\n"),  # log (has commits)
            ]
            result = await step.execute(ctx)

        assert result.success
        assert "already exist" in result.summary

    async def test_fails_when_no_changes_and_no_commits(self) -> None:
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = CommitStep()

        with patch("sova.core.steps.commit.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # diff --stat
                MagicMock(success=True, stdout=""),  # staged
                MagicMock(success=True, stdout=""),  # untracked
                MagicMock(success=True, stdout=""),  # log (no commits)
            ]
            result = await step.execute(ctx)

        assert not result.success

    async def test_gate_requires_commits_ahead(self) -> None:
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = CommitStep()

        with patch("sova.core.steps.commit.run") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="")
            gate = await step.validate_output(ctx)

        assert not gate.passed

    async def test_gate_passes_with_commits(self) -> None:
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = CommitStep()

        with patch("sova.core.steps.commit.run") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="abc123 feat: something\n")
            gate = await step.validate_output(ctx)

        assert gate.passed

    async def test_address_review_uses_fix_prefix(self) -> None:
        """Address-review commits should use 'fix:' not 'feat:'."""
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(
            worktree_dir=Path("/tmp/worktree"),
            pr_number=130,
            completed_steps=frozenset({"address_review"}),
        )
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" fix.py | 3 +++\n"),  # diff --stat
                MagicMock(success=True, stdout=""),  # staged
                MagicMock(success=True, stdout=""),  # untracked
                MagicMock(success=True, stdout=""),  # log
            ]
            result = await step.execute(ctx)

        assert result.success
        msg = mock_commit.call_args[0][0]
        assert msg.startswith("fix(core):")
        assert "issue 42" in msg
        assert "Closes" not in msg

    async def test_developer_commit_without_task_uses_issue_number(self) -> None:
        """When ctx.task is None (no adapter fetch), falls back to issue number."""
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        ctx.task = None
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" fix.py | 3 +++\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
            ]
            result = await step.execute(ctx)

        assert result.success
        msg = mock_commit.call_args[0][0]
        assert "feat(core): issue 42" in msg
        assert "Closes #42" in msg


class TestValidateStep:
    async def test_skips_when_no_hook(self) -> None:
        from sova.core.steps.validate import ValidateStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = ValidateStep()

        with patch("sova.core.steps.validate.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=False, stdout=""),  # git config --get core.hooksPath
                MagicMock(success=True, stdout=".git\n"),  # git rev-parse --git-dir
                MagicMock(success=False, stdout=""),  # test -x (no hook)
            ]
            result = await step.execute(ctx)

        assert result.success
        assert "no pre-push hook" in result.summary.lower()

    async def test_passes_when_hook_succeeds(self) -> None:
        from sova.core.steps.validate import ValidateStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = ValidateStep()

        with patch("sova.core.steps.validate.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=".githooks\n"),  # core.hooksPath
                MagicMock(success=True, stdout=""),  # test -x (hook exists)
                MagicMock(success=True, stdout="All checks passed\n", stderr=""),  # hook run
            ]
            result = await step.execute(ctx)

        assert result.success
        assert "passed" in result.summary.lower()

    async def test_invokes_llm_on_hook_failure(self) -> None:
        from sova.core.steps.validate import ValidateStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = ValidateStep()

        with (
            patch("sova.core.steps.validate.run") as mock_run,
            patch("sova.core.steps.validate.invoke", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=".githooks\n"),  # core.hooksPath
                MagicMock(success=True, stdout=""),  # test -x
                MagicMock(success=False, stdout="FAIL: missing type hints\n", stderr=""),  # hook fails
                MagicMock(success=True, stdout="All checks passed\n", stderr=""),  # hook passes after fix
            ]
            mock_invoke.return_value = LLMResult(
                text="Fixed type hints",
                model="sonnet",
                cost_usd=Decimal("0.02"),
            )
            result = await step.execute(ctx)

        assert result.success
        assert "1 fix attempt" in result.summary
        mock_invoke.assert_awaited_once()
        assert "missing type hints" in mock_invoke.call_args[1].get("prompt", mock_invoke.call_args[0][0])

    async def test_fails_after_max_attempts(self) -> None:
        from sova.core.steps.validate import ValidateStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = ValidateStep()

        with (
            patch("sova.core.steps.validate.run") as mock_run,
            patch("sova.core.steps.validate.invoke", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=".githooks\n"),  # core.hooksPath
                MagicMock(success=True, stdout=""),  # test -x
                MagicMock(success=False, stdout="FAIL: error\n", stderr=""),  # hook fails
                MagicMock(success=False, stdout="FAIL: error\n", stderr=""),  # still fails after fix 1
                MagicMock(success=False, stdout="FAIL: error\n", stderr=""),  # still fails after fix 2
            ]
            mock_invoke.return_value = LLMResult(
                text="Attempted fix",
                model="sonnet",
                cost_usd=Decimal("0.01"),
            )
            result = await step.execute(ctx)

        assert not result.success
        assert "still failing" in result.summary.lower()
        assert mock_invoke.await_count == 2

    async def test_gate_requires_commits(self) -> None:
        from sova.core.steps.validate import ValidateStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = ValidateStep()

        with patch("sova.core.steps.validate.run") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="")
            gate = await step.validate_output(ctx)

        assert not gate.passed

    async def test_gate_passes_with_commits(self) -> None:
        from sova.core.steps.validate import ValidateStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = ValidateStep()

        with patch("sova.core.steps.validate.run") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="abc123 feat: something\n")
            gate = await step.validate_output(ctx)

        assert gate.passed

    async def test_auto_detects_githooks_directory(self, tmp_path: Path) -> None:
        from sova.core.steps.validate import find_pre_push_hook

        hooks_dir = tmp_path / ".githooks"
        hooks_dir.mkdir()
        hook_file = hooks_dir / "pre-push"
        hook_file.write_text("#!/bin/bash\nexit 0\n")
        hook_file.chmod(0o755)

        with patch("sova.core.steps.validate.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=False, stdout=""),  # core.hooksPath not set
                MagicMock(success=True, stdout=f"{tmp_path}\n"),  # git rev-parse --show-toplevel
                MagicMock(success=True),  # git config core.hooksPath .githooks
                MagicMock(success=True),  # test -x .githooks/pre-push
            ]

            result = await find_pre_push_hook(tmp_path)

        assert result == ".githooks/pre-push"
        config_call = mock_run.call_args_list[2]
        assert config_call[0] == ("git", "config", "core.hooksPath", ".githooks")


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

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_execute_generates_structured_body(self, mock_create_pr, mock_run, mock_invoke, _find) -> None:
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat: add widget\n"),
            MagicMock(success=True, stdout=" src/app.py | 10 ++++\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(
            text="## Summary\n- Add widget\n\nCloses #42", model="sonnet", cost_usd=Decimal("0.01")
        )
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        adapter = _mock_adapter()
        ctx = _make_ctx(
            adapter=adapter,
            task=Task(id="42", title="Add widget", body="We need a widget"),
            branch_name="feat/issue-42",
        )
        step = CreatePRStep()
        result = await step.execute(ctx)

        assert result.success
        assert ctx.pr_number == 10
        body_arg = mock_create_pr.call_args.kwargs["body"]
        assert "## Summary" in body_arg
        assert "Closes #42" in body_arg

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_execute_includes_closes_for_issue(self, mock_create_pr, mock_run, mock_invoke, _find) -> None:
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=12, url="https://github.com/x/y/pull/12")

        ctx = _make_ctx(branch_name="feat/issue-42")
        step = CreatePRStep()
        await step.execute(ctx)

        body_arg = mock_create_pr.call_args.kwargs["body"]
        assert "Closes #42" in body_arg

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_execute_body_includes_commits_and_diff(self, mock_create_pr, mock_run, mock_invoke, _find) -> None:
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat: add widget\n"),
            MagicMock(success=True, stdout=" src/app.py | 10 ++++\n 1 file changed, 10 insertions(+)\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py b/src/app.py\n+widget code\n"),
        ]
        mock_invoke.return_value = LLMResult(
            text=(
                "## Summary\n- widget\n\n## Commits\nabc123 feat: add widget\n\n## Files changed\nsrc/app.py | 10 ++++"
            ),
            model="sonnet",
            cost_usd=Decimal("0.01"),
        )
        mock_create_pr.return_value = MagicMock(number=11, url="https://github.com/x/y/pull/11")

        ctx = _make_ctx(
            branch_name="feat/issue-42",
            task=Task(id="42", title="Add widget", body="We need a widget"),
        )
        step = CreatePRStep()
        result = await step.execute(ctx)

        assert result.success
        assert ctx.pr_number == 11
        body_arg = mock_create_pr.call_args.kwargs["body"]
        assert "Closes #42" in body_arg
        assert "## Summary" in body_arg
        assert "abc123" in body_arg
        assert "src/app.py" in body_arg

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_execute_assigns_pr_to_user(self, mock_create_pr, mock_run, mock_invoke, _find) -> None:
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        adapter = _mock_adapter()
        ctx = _make_ctx(adapter=adapter, branch_name="feat/issue-42")
        ctx.config = ProjectConfig(github_user="xsovad06")
        step = CreatePRStep()

        with patch("sova.core.steps.create_pr.git_ops.assign_pr", new_callable=AsyncMock) as mock_assign:
            result = await step.execute(ctx)

        assert result.success
        mock_assign.assert_awaited_once_with(10, assignee="xsovad06", repo="", github_user="xsovad06")

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_execute_skips_assignment_when_no_github_user(
        self,
        mock_create_pr,
        mock_run,
        mock_invoke,
        _find,
    ) -> None:
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        ctx = _make_ctx(branch_name="feat/issue-42")
        ctx.config = ProjectConfig(github_user="")
        step = CreatePRStep()

        with patch("sova.core.steps.create_pr.git_ops.assign_pr", new_callable=AsyncMock) as mock_assign:
            result = await step.execute(ctx)

        assert result.success
        mock_assign.assert_not_awaited()

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock)
    async def test_execute_adopts_existing_pr(self, mock_find) -> None:
        from sova.core.steps.create_pr import CreatePRStep

        mock_find.return_value = MagicMock(number=55, url="https://github.com/x/y/pull/55")

        adapter = _mock_adapter()
        ctx = _make_ctx(adapter=adapter, branch_name="feat/issue-42")
        step = CreatePRStep()
        result = await step.execute(ctx)

        assert result.success
        assert "Adopted existing PR #55" in result.summary
        assert ctx.pr_number == 55
        assert ctx.pr_url == "https://github.com/x/y/pull/55"

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock)
    async def test_execute_transitions_to_in_review_on_adopt(self, mock_find) -> None:
        """Adopting an existing PR must still transition issue to IN_REVIEW."""
        from sova.core.steps.create_pr import CreatePRStep

        mock_find.return_value = MagicMock(number=55, url="https://github.com/x/y/pull/55")

        adapter = _mock_adapter()
        ctx = _make_ctx(adapter=adapter, branch_name="feat/issue-42")
        step = CreatePRStep()
        await step.execute(ctx)

        adapter.transition_state.assert_awaited_once_with("42", TaskState.IN_REVIEW)

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_execute_adopts_pr_from_already_exists_error(
        self, mock_create_pr, mock_run, mock_invoke, _find
    ) -> None:
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.side_effect = RuntimeError(
            'a pull request for branch "feat/issue-48809" into branch "master" '
            "already exists: https://github.com/org/repo/pull/3148"
        )

        adapter = _mock_adapter()
        ctx = _make_ctx(adapter=adapter, branch_name="feat/issue-48809")
        step = CreatePRStep()
        result = await step.execute(ctx)

        assert result.success
        assert "Adopted existing PR #3148" in result.summary
        assert ctx.pr_number == 3148

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_execute_fails_on_non_duplicate_runtime_error(
        self, mock_create_pr, mock_run, mock_invoke, _find
    ) -> None:
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.side_effect = RuntimeError("permission denied")

        ctx = _make_ctx(branch_name="feat/issue-42")
        step = CreatePRStep()
        result = await step.execute(ctx)

        assert not result.success
        assert "permission denied" in result.error


class TestCreatePRStepJira:
    """PR creation with JIRA task source -- title prefix and body link."""

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_jira_pr_title_has_ticket_prefix(self, mock_create_pr, mock_run, mock_invoke, _find) -> None:
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        adapter = _mock_adapter()
        ctx = _make_ctx(
            adapter=adapter,
            task=Task(id="48928", title="Improve parity check log output"),
            issue_number="48928",
            branch_name="feat/issue-48928",
        )
        ctx.config = ProjectConfig(
            task_source=TaskSourceConfig(
                type="jira",
                jira_project_key="RHCLOUD",
                jira_base_url="https://issues.redhat.com",
            )
        )
        step = CreatePRStep()
        result = await step.execute(ctx)

        assert result.success
        title_arg = mock_create_pr.call_args.kwargs["title"]
        assert "[RHCLOUD-48928]" in title_arg

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_jira_pr_body_has_ticket_link(self, mock_create_pr, mock_run, mock_invoke, _find) -> None:
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        adapter = _mock_adapter()
        ctx = _make_ctx(
            adapter=adapter,
            task=Task(id="48928", title="Improve parity check log output"),
            issue_number="48928",
            branch_name="feat/issue-48928",
        )
        ctx.config = ProjectConfig(
            task_source=TaskSourceConfig(
                type="jira",
                jira_project_key="RHCLOUD",
                jira_base_url="https://issues.redhat.com",
            )
        )
        step = CreatePRStep()
        await step.execute(ctx)

        body_arg = mock_create_pr.call_args.kwargs["body"]
        assert "https://issues.redhat.com/browse/RHCLOUD-48928" in body_arg
        assert "Closes #48928" not in body_arg

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_jira_fallback_body_has_ticket_link(self, mock_create_pr, mock_run, mock_invoke, _find) -> None:
        from sova.core.steps.create_pr import CreatePRStep

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.side_effect = RuntimeError("LLM unavailable")
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        adapter = _mock_adapter()
        ctx = _make_ctx(
            adapter=adapter,
            task=Task(id="48928", title="Improve parity check log output"),
            issue_number="48928",
            branch_name="feat/issue-48928",
        )
        ctx.config = ProjectConfig(
            task_source=TaskSourceConfig(
                type="jira",
                jira_project_key="RHCLOUD",
                jira_base_url="https://issues.redhat.com",
            )
        )
        step = CreatePRStep()
        await step.execute(ctx)

        body_arg = mock_create_pr.call_args.kwargs["body"]
        assert "https://issues.redhat.com/browse/RHCLOUD-48928" in body_arg
        assert "Closes #48928" not in body_arg

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_github_pr_title_unchanged(self, mock_create_pr, mock_run, mock_invoke, _find) -> None:
        """GitHub-backed projects should NOT get a JIRA prefix."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(
            text="## Summary\n- stuff\n\nCloses #42", model="sonnet", cost_usd=Decimal("0.01")
        )
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        adapter = _mock_adapter()
        ctx = _make_ctx(adapter=adapter, branch_name="feat/issue-42")
        ctx.config = ProjectConfig(task_source=TaskSourceConfig(type="github"))
        step = CreatePRStep()
        await step.execute(ctx)

        title_arg = mock_create_pr.call_args.kwargs["title"]
        assert "[" not in title_arg
        assert "feat(#42)" in title_arg


class TestCreatePRStepJiraPrompt:
    """Verify the LLM prompt itself is JIRA-aware (not just post-processing)."""

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_jira_prompt_has_no_closes_syntax(self, mock_create_pr, mock_run, mock_invoke, _find) -> None:
        """The LLM prompt for JIRA projects should not contain Closes #N."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        ctx = _make_ctx(
            task=Task(id="48928", title="Improve output"),
            issue_number="48928",
            branch_name="feat/RHCLOUD-48928-improve-output",
        )
        ctx.config = ProjectConfig(
            task_source=TaskSourceConfig(
                type="jira",
                jira_project_key="RHCLOUD",
                jira_base_url="https://issues.redhat.com",
            )
        )
        step = CreatePRStep()
        await step.execute(ctx)

        prompt_arg = mock_invoke.call_args[0][0]
        assert "Closes #48928" not in prompt_arg
        assert "RHCLOUD-48928" in prompt_arg
        assert "issues.redhat.com/browse/RHCLOUD-48928" in prompt_arg
        assert "JIRA: JIRA:" not in prompt_arg

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_branch_fallback_title_strips_prefixes(self, mock_create_pr, mock_run, mock_invoke, _find) -> None:
        """When task.title is missing, branch name should be cleaned for PR title."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        adapter = _mock_adapter()
        adapter.get_task.side_effect = RuntimeError("unavailable")
        ctx = _make_ctx(
            adapter=adapter,
            task=None,
            issue_number="48928",
            branch_name="feat/RHCLOUD-48928-security-logging",
        )
        ctx.config = ProjectConfig(
            task_source=TaskSourceConfig(
                type="jira",
                jira_project_key="RHCLOUD",
                jira_base_url="https://issues.redhat.com",
            )
        )
        step = CreatePRStep()
        await step.execute(ctx)

        title_arg = mock_create_pr.call_args.kwargs["title"]
        assert "feat/RHCLOUD" not in title_arg
        assert "security logging" in title_arg.lower() or "security-logging" in title_arg.lower()


class TestCreatePRStepDiffContent:
    """Tests for diff content inclusion in PR body generation."""

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_prompt_includes_actual_diff_content(self, mock_create_pr, mock_run, mock_invoke, _find) -> None:
        """The LLM prompt must include actual diff content, not just --stat."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat: add widget\n"),
            MagicMock(success=True, stdout=" src/app.py | 10 ++++\n 1 file changed, 10 insertions(+)\n"),
            MagicMock(
                success=True,
                stdout="diff --git a/src/app.py b/src/app.py\n+def widget():\n+    return 'widget'\n",
            ),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        ctx = _make_ctx(
            branch_name="feat/issue-42",
            task=Task(id="42", title="Add widget", body="We need a widget"),
        )
        step = CreatePRStep()
        await step.execute(ctx)

        prompt_arg = mock_invoke.call_args[0][0]
        assert "diff --git a/src/app.py b/src/app.py" in prompt_arg
        assert "+def widget():" in prompt_arg
        assert "Actual diff" in prompt_arg or "diff content" in prompt_arg.lower()

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_large_diff_is_truncated(self, mock_create_pr, mock_run, mock_invoke, _find) -> None:
        """Large diffs should be truncated to avoid exceeding LLM context."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        large_diff = "diff --git a/src/file.py b/src/file.py\n" + ("+" + "x" * 100 + "\n") * 200
        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat: add widget\n"),
            MagicMock(success=True, stdout=" src/file.py | 200 ++++\n"),
            MagicMock(success=True, stdout=large_diff),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        ctx = _make_ctx(branch_name="feat/issue-42", task=Task(id="42", title="Add widget"))
        step = CreatePRStep()
        await step.execute(ctx)

        prompt_arg = mock_invoke.call_args[0][0]
        # Verify truncation: known prefix present, content beyond 8000 absent
        assert "+xxxx" in prompt_arg  # First 5 chars of diff should be present
        assert "x" * 8500 not in prompt_arg  # Content beyond 8000 should be excluded
        assert "truncated" in prompt_arg.lower() or "..." in prompt_arg

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_prompt_truncates_issue_body(self, mock_create_pr, mock_run, mock_invoke, _find) -> None:
        """Issue body should be truncated in the prompt to avoid over-weighting it."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        long_body = "x" * 1000
        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat\n"),
            MagicMock(success=True, stdout=" src/app.py | 1 +\n"),
            MagicMock(success=True, stdout="diff --git a/src/app.py\n+change\n"),
        ]
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        ctx = _make_ctx(branch_name="feat/issue-42", task=Task(id="42", title="Title", body=long_body))
        step = CreatePRStep()
        await step.execute(ctx)

        prompt_arg = mock_invoke.call_args[0][0]
        assert long_body not in prompt_arg
        assert len([line for line in prompt_arg.split("\n") if "x" * 500 in line]) == 0


class TestHandoffToReviewerStep:
    async def test_writes_handoff_and_succeeds(self) -> None:
        from sova.core.steps.handoff_to_reviewer import HandoffToReviewerStep

        adapter = _mock_adapter()
        ctx = _make_ctx(adapter=adapter, pr_number=42)
        ctx.project_dir = Path("/tmp/test-handoff")
        step = HandoffToReviewerStep()

        with (
            patch("sova.core.steps._handoff_helpers.write_handoff", new_callable=AsyncMock),
            patch("sova.core.steps._handoff_helpers.write_handoff_file") as mock_file,
            patch("sova.core.steps._handoff_helpers.notify"),
        ):
            result = await step.execute(ctx)

        assert result.success
        assert "Reviewer" in result.summary
        mock_file.assert_called_once()
        handoff = mock_file.call_args[0][1]
        assert handoff.status == "awaiting_action"
        assert len(handoff.next_actions) == 1
        assert handoff.next_actions[0].auto_execute is True
        assert handoff.next_actions[0].mode == "agent"

    async def test_auto_execute_disabled_by_config(self) -> None:
        from sova.core.steps.handoff_to_reviewer import HandoffToReviewerStep

        config = ProjectConfig()
        config.pipeline.auto_handoff = False
        adapter = _mock_adapter()
        ctx = _make_ctx(adapter=adapter, pr_number=42, config=config)
        ctx.project_dir = Path("/tmp/test-handoff")
        step = HandoffToReviewerStep()

        with (
            patch("sova.core.steps._handoff_helpers.write_handoff", new_callable=AsyncMock),
            patch("sova.core.steps._handoff_helpers.write_handoff_file") as mock_file,
            patch("sova.core.steps._handoff_helpers.notify"),
        ):
            result = await step.execute(ctx)

        assert result.success
        handoff = mock_file.call_args[0][1]
        assert handoff.next_actions[0].auto_execute is False

    async def test_can_skip_when_completed(self) -> None:
        from sova.core.steps.handoff_to_reviewer import HandoffToReviewerStep

        ctx = _make_ctx(completed_steps=frozenset({"handoff_to_reviewer"}))
        step = HandoffToReviewerStep()
        assert await step.can_skip(ctx)


class TestHandoffToUserStep:
    async def test_writes_handoff_with_integrate_actions(self) -> None:
        from sova.core.steps.handoff_to_user import HandoffToUserStep

        ctx = _make_ctx(pr_number=42)
        ctx.project_dir = Path("/tmp/test-handoff")
        step = HandoffToUserStep()

        with (
            patch("sova.core.steps._handoff_helpers.write_handoff", new_callable=AsyncMock),
            patch("sova.core.steps._handoff_helpers.write_handoff_file") as mock_file,
            patch("sova.core.steps._handoff_helpers.notify"),
        ):
            result = await step.execute(ctx)

        assert result.success
        handoff = mock_file.call_args[0][1]
        assert len(handoff.next_actions) == 1
        assert handoff.next_actions[0].id == "integrate"
        assert not handoff.next_actions[0].auto_execute


class TestAddressReviewStep:
    async def test_no_findings_returns_success(self) -> None:
        from sova.core.steps.address_review import AddressReviewStep

        ctx = _make_ctx(pr_number=42)
        ctx.project_dir = Path("/tmp/nonexistent")
        step = AddressReviewStep()

        with (
            patch("sova.core.steps.address_review.run", new_callable=AsyncMock) as mock_run,
            patch(
                "sova.core.steps.address_review._load_coderabbit_findings",
                new_callable=AsyncMock,
                return_value=([], []),
            ),
        ):
            mock_run.return_value = MagicMock(success=True, stdout="abc123")
            result = await step.execute(ctx)

        assert result.success
        assert "No review findings" in result.summary


class TestRearrangeCommitsStep:
    async def test_execute_invokes_rearrange_command(self) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = RearrangeCommitsStep()

        with patch("sova.core.steps.rearrange_commits.invoke_command", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.return_value = MagicMock(cost_usd=Decimal("0.02"), text="Commits reorganized")
            result = await step.execute(ctx)

        assert result.success
        assert "reorganized" in result.summary
        mock_invoke.assert_awaited_once_with(
            "/rearrange-commits",
            model=ctx.config.agent.model,
            cwd=ctx.working_dir,
            max_budget_usd=ANY,
            timeout=ANY,
        )

    async def test_execute_returns_failure_on_error(self) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = RearrangeCommitsStep()

        with patch("sova.core.steps.rearrange_commits.invoke_command", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.side_effect = RuntimeError("command not found")
            result = await step.execute(ctx)

        assert not result.success
        assert "failed" in result.summary

    async def test_validate_passes_when_commits_ahead_and_clean(self) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = RearrangeCommitsStep()

        with patch("sova.core.steps.rearrange_commits.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout="abc123 feat(core): something\n"),  # log
                MagicMock(success=True, stdout=""),  # diff --stat (clean)
                MagicMock(success=True, stdout=""),  # staged (clean)
                MagicMock(success=True, stdout=""),  # status --porcelain (no untracked)
            ]
            gate = await step.validate_output(ctx)

        assert gate.passed

    async def test_validate_fails_when_untracked_files_remain(self) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = RearrangeCommitsStep()

        with patch("sova.core.steps.rearrange_commits.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout="abc123 feat(core): something\n"),  # log
                MagicMock(success=True, stdout=""),  # diff --stat (clean)
                MagicMock(success=True, stdout=""),  # staged (clean)
                MagicMock(success=True, stdout="?? new_file.py\n"),  # status --porcelain (untracked)
            ]
            gate = await step.validate_output(ctx)

        assert not gate.passed
        assert "Untracked" in gate.reason

    async def test_validate_fails_when_no_commits(self) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = RearrangeCommitsStep()

        with patch("sova.core.steps.rearrange_commits.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="")  # no commits
            gate = await step.validate_output(ctx)

        assert not gate.passed
        assert "No commits" in gate.reason

    async def test_validate_fails_when_uncommitted_changes_remain(self) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = RearrangeCommitsStep()

        with patch("sova.core.steps.rearrange_commits.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout="abc123 feat(core): something\n"),  # log (commits exist)
                MagicMock(success=True, stdout=" file.py | 3 +++\n"),  # diff --stat (dirty)
                MagicMock(success=True, stdout=""),  # staged
            ]
            gate = await step.validate_output(ctx)

        assert not gate.passed
        assert "Uncommitted" in gate.reason

    async def test_can_skip_when_already_completed(self) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"), completed_steps=frozenset({"rearrange_commits"}))
        step = RearrangeCommitsStep()

        assert await step.can_skip(ctx)

    async def test_cannot_skip_when_not_completed(self) -> None:
        from sova.core.steps.rearrange_commits import RearrangeCommitsStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = RearrangeCommitsStep()

        assert not await step.can_skip(ctx)


class TestStepRegistry:
    def test_get_developer_steps_returns_all(self) -> None:
        from sova.core.steps import get_developer_steps

        steps = get_developer_steps()
        names = [s.name for s in steps]
        assert names == [
            "sync",
            "assess",
            "create_worktree",
            "capture_baseline",
            "develop",
            "simplify",
            "self_review",
            "commit",
            "validate",
            "push",
            "create_pr",
            "wait_for_external_reviews",
            "address_external_findings",
            "monitor_ci",
            "extract_memory",
            "handoff_to_reviewer",
        ]

    def test_get_address_review_steps(self) -> None:
        from sova.core.steps import get_address_review_steps

        steps = get_address_review_steps()
        names = [s.name for s in steps]
        assert names == [
            "ensure_worktree",
            "rebase",
            "address_review",
            "rearrange_commits",
            "validate",
            "push",
            "monitor_ci",
            "resolve_external_reviews",
            "extract_memory",
            "handoff_to_user",
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
            assert rows[0].status == "done"

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

    async def test_skipped_step_creates_step_execution(self) -> None:
        """Skipped steps create a StepExecution record with status 'skipped'."""
        ctx = _make_ctx()
        step_a = DummyStep(should_pass=True, gate_pass=True, skip=True, name="step_a")
        step_b = DummyStep(should_pass=True, gate_pass=True, name="step_b")
        engine = WorkflowEngine(steps=[step_a, step_b], ctx=ctx)

        result = await engine.run()

        assert result.success
        assert result.steps_skipped == 1
        assert result.steps_completed == 1
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
            assert rows[0].status == "skipped"
            assert rows[0].duration_ms == 0
            assert rows[0].started_at is not None
            assert rows[0].ended_at is not None
            assert rows[0].started_at == rows[0].ended_at
            assert rows[1].step_name == "step_b"
            assert rows[1].status == "done"

    async def test_skipped_step_db_failure_does_not_break_workflow(self) -> None:
        """When _create_step_execution raises during a skip, the workflow continues."""
        ctx = _make_ctx()
        step_a = DummyStep(should_pass=True, gate_pass=True, skip=True, name="step_a")
        step_b = DummyStep(should_pass=True, gate_pass=True, name="step_b")
        engine = WorkflowEngine(steps=[step_a, step_b], ctx=ctx)

        original = engine._create_step_execution
        call_count = 0

        async def fail_first_call(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("db down")
            return await original(*args, **kwargs)

        with patch.object(engine, "_create_step_execution", side_effect=fail_first_call):
            result = await engine.run()

        assert result.success
        assert result.steps_skipped == 1
        assert result.steps_completed == 1

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

    async def test_step_in_status_map_sets_final_status(self) -> None:
        """A step whose name is in _STEP_STATUS_MAP updates final_status mid-pipeline."""
        ctx = _make_ctx()
        step_sync = DummyStep(should_pass=True, gate_pass=True, name="sync")
        step_fail = DummyStep(should_pass=True, gate_pass=False, name="next")
        engine = WorkflowEngine(steps=[step_sync, step_fail], ctx=ctx)

        status_updates: list[TaskStatus] = []
        original = engine._update_task_run_status

        async def spy_update(status: TaskStatus, **kwargs: object) -> None:
            status_updates.append(status)
            await original(status, **kwargs)

        engine._update_task_run_status = spy_update  # type: ignore[assignment]

        result = await engine.run()

        assert result.final_status == TaskStatus.PAUSED
        assert TaskStatus.PAUSED in status_updates

    async def test_step_execute_generic_exception(self) -> None:
        """A generic exception in step.execute() is caught and recorded."""
        ctx = _make_ctx()
        step = DummyStep(should_pass=True, gate_pass=True)
        step.execute = AsyncMock(side_effect=ValueError("boom"))
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert not result.success
        assert "boom" in (result.error or "")

    async def test_step_timeout_monitor_ci(self) -> None:
        """_step_timeout returns ci.max_wait + 120 for monitor_ci."""
        ctx = _make_ctx()
        engine = WorkflowEngine(steps=[], ctx=ctx)

        timeout = engine._step_timeout("monitor_ci")

        assert timeout == ctx.config.ci.max_wait + 120

    async def test_step_timeout_regular_step(self) -> None:
        """_step_timeout returns agent.step_timeout for non-monitor_ci steps."""
        ctx = _make_ctx()
        engine = WorkflowEngine(steps=[], ctx=ctx)

        timeout = engine._step_timeout("develop")

        assert timeout == ctx.config.agent.step_timeout

    async def test_write_output_flushes_when_needed(self) -> None:
        """_write_output calls flush() when should_flush() returns True."""
        ctx = _make_ctx()
        engine = WorkflowEngine(steps=[], ctx=ctx)
        writer = MagicMock()
        writer.should_flush.return_value = True
        writer.flush = AsyncMock()
        engine._output_writer = writer

        await engine._write_output("hello")

        writer.write_line.assert_called_once_with("hello")
        writer.flush.assert_awaited_once()

    async def test_update_step_execution_status_none_id(self) -> None:
        """_update_step_execution_status returns early when step_exec_id is None."""
        ctx = _make_ctx()
        engine = WorkflowEngine(steps=[], ctx=ctx)

        await engine._update_step_execution_status(None, "done")

    async def test_update_step_execution_status_db_error(self) -> None:
        """_update_step_execution_status logs warning on DB error."""
        ctx = _make_ctx()
        engine = WorkflowEngine(steps=[], ctx=ctx)

        with patch("sova.core.workflow.get_session", side_effect=RuntimeError("db down")):
            with patch("sova.core.workflow.log") as mock_log:
                await engine._update_step_execution_status(999, "done")

            mock_log.warning.assert_called_once()
            call_kwargs = mock_log.warning.call_args
            assert "status_update_failed" in str(call_kwargs)

    async def test_write_approval_handoff_exception(self) -> None:
        """_write_approval_handoff logs warning when write_handoff_file raises."""
        ctx = _make_ctx()
        engine = WorkflowEngine(steps=[], ctx=ctx)
        result = StepResult(success=True, summary="needs approval", awaiting_approval=True)

        with patch("sova.core.workflow.log") as mock_log:
            with patch("sova.ipc.handoff.write_handoff_file", side_effect=OSError("disk full")):
                engine._write_approval_handoff("develop", result)

            mock_log.warning.assert_called()

    async def test_step_with_cost_creates_cost_record(self) -> None:
        """When a step returns cost_usd > 0, a CostRecord is created."""
        ctx = _make_ctx()
        step = DummyStep(should_pass=True, gate_pass=True)

        async def execute_with_cost(_ctx: object) -> StepResult:
            return StepResult(success=True, summary="Done", cost_usd=Decimal("0.05"))

        step.execute = execute_with_cost
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert result.success
        session = await get_session()
        async with session.begin():
            rows = (
                (await session.execute(select(CostRecord).where(CostRecord.task_run_id == result.task_run_id)))
                .scalars()
                .all()
            )
            assert len(rows) == 1
            assert rows[0].cost_usd == Decimal("0.05")
            assert rows[0].phase == "dummy"

    async def test_finalize_task_run_preserves_existing_ended_at(self) -> None:
        """_finalize_task_run does not overwrite ended_at if already set."""
        from datetime import datetime, timezone

        ctx = _make_ctx()
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        known_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
        session = await get_session()
        async with session.begin():
            task_run = await session.get(TaskRun, result.task_run_id)
            task_run.ended_at = known_time

        await engine._finalize_task_run()

        session = await get_session()
        async with session.begin():
            task_run = await session.get(TaskRun, result.task_run_id)
            assert task_run.ended_at.replace(tzinfo=None) == known_time.replace(tzinfo=None)


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


# ---------------------------------------------------------------------------
# Context persistence on non-success paths
# ---------------------------------------------------------------------------


class TestContextPersistence:
    """Verify worktree_path/branch_name are saved to TaskRun on every exit path."""

    async def test_context_persisted_after_successful_step(self) -> None:
        """worktree_path and branch_name are saved after each successful step."""
        ctx = _make_ctx(
            branch_name="feat/issue-42",
            worktree_dir=Path("/tmp/wt/42"),
        )
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert result.success
        session = await get_session()
        async with session.begin():
            task_run = await session.get(TaskRun, result.task_run_id)
            assert task_run.branch_name == "feat/issue-42"
            assert task_run.worktree_path == "/tmp/wt/42"

    async def test_context_persisted_on_gate_failure(self) -> None:
        """worktree_path and branch_name survive a gate_failed pause."""
        ctx = _make_ctx(
            branch_name="feat/issue-73",
            worktree_dir=Path("/tmp/wt/73"),
        )
        step = DummyStep(should_pass=True, gate_pass=False)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert result.final_status == TaskStatus.PAUSED
        session = await get_session()
        async with session.begin():
            task_run = await session.get(TaskRun, result.task_run_id)
            assert task_run.branch_name == "feat/issue-73"
            assert task_run.worktree_path == "/tmp/wt/73"

    async def test_context_persisted_on_step_failure(self) -> None:
        """worktree_path and branch_name survive a step execution failure."""
        ctx = _make_ctx(
            branch_name="fix/broken",
            worktree_dir=Path("/tmp/wt/99"),
        )
        step = DummyStep(should_pass=False)
        step.max_retries = 0
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert result.final_status == TaskStatus.FAILED
        session = await get_session()
        async with session.begin():
            task_run = await session.get(TaskRun, result.task_run_id)
            assert task_run.branch_name == "fix/broken"
            assert task_run.worktree_path == "/tmp/wt/99"

    async def test_context_persisted_on_budget_exceeded(self) -> None:
        """worktree_path and branch_name survive a budget pause."""
        config = ProjectConfig(agent={"max_budget": Decimal("0.01")})
        ctx = _make_ctx(
            config=config,
            branch_name="feat/expensive",
            worktree_dir=Path("/tmp/wt/88"),
        )
        ctx.add_cost(Decimal("0.02"))

        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert result.final_status == TaskStatus.PAUSED
        session = await get_session()
        async with session.begin():
            task_run = await session.get(TaskRun, result.task_run_id)
            assert task_run.branch_name == "feat/expensive"
            assert task_run.worktree_path == "/tmp/wt/88"

    async def test_context_updated_mid_pipeline(self) -> None:
        """Context fields set by a middle step are persisted even if a later step fails."""
        ctx = _make_ctx()

        class WorktreeSettingStep(BaseStep):
            name = "set_worktree"

            async def execute(self, ctx_inner: ExecutionContext) -> StepResult:
                ctx_inner.branch_name = "feat/dynamic"
                ctx_inner.worktree_dir = Path("/tmp/wt/dynamic")
                return StepResult(success=True, summary="Set worktree")

            async def validate_output(self, ctx_inner: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

        step_a = WorktreeSettingStep()
        step_b = DummyStep(should_pass=True, gate_pass=False, name="failing_gate")

        engine = WorkflowEngine(steps=[step_a, step_b], ctx=ctx)
        result = await engine.run()

        assert result.final_status == TaskStatus.PAUSED
        session = await get_session()
        async with session.begin():
            task_run = await session.get(TaskRun, result.task_run_id)
            assert task_run.branch_name == "feat/dynamic"
            assert task_run.worktree_path == "/tmp/wt/dynamic"

    async def test_pr_number_persisted_on_failure(self) -> None:
        """pr_number set during execution is saved even if a later step fails."""
        ctx = _make_ctx(
            branch_name="feat/pr-test",
            pr_number=42,
        )
        step = DummyStep(should_pass=True, gate_pass=False)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert result.final_status == TaskStatus.PAUSED
        session = await get_session()
        async with session.begin():
            task_run = await session.get(TaskRun, result.task_run_id)
            assert task_run.pr_number == 42

    async def test_awaiting_approval_sets_task_run_status(self) -> None:
        """TaskRun status is set to awaiting_approval when a step pauses."""
        ctx = _make_ctx(
            branch_name="feat/spec-approval",
            worktree_dir=Path("/tmp/wt/approval"),
        )

        class ApprovalStep(BaseStep):
            name = "spec"

            async def execute(self, ctx_inner: ExecutionContext) -> StepResult:
                return StepResult(success=True, summary="Spec ready", awaiting_approval=True)

            async def validate_output(self, ctx_inner: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

        step = ApprovalStep()
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert result.final_status == TaskStatus.AWAITING_APPROVAL
        session = await get_session()
        async with session.begin():
            task_run = await session.get(TaskRun, result.task_run_id)
            assert task_run.status == "awaiting_approval"
            assert task_run.current_step == "spec"
            assert task_run.ended_at is not None
            assert task_run.branch_name == "feat/spec-approval"
            assert task_run.worktree_path == "/tmp/wt/approval"

    async def test_awaiting_approval_step_execution_status(self) -> None:
        """StepExecution is marked 'awaiting_approval' (not 'done') so resume re-executes it."""
        ctx = _make_ctx()

        class ApprovalStep(BaseStep):
            name = "needs_approval"

            async def execute(self, ctx_inner: ExecutionContext) -> StepResult:
                return StepResult(success=True, summary="Awaiting", awaiting_approval=True)

            async def validate_output(self, ctx_inner: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

        step = ApprovalStep()
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
            assert rows[0].step_name == "needs_approval"
            assert rows[0].status == "awaiting_approval"

    async def test_awaiting_approval_step_not_in_completed_on_resume(self) -> None:
        """The paused step's 'awaiting_approval' status is NOT in STEP_DONE_STATUSES, so resume re-executes it."""
        from sova.core.state import STEP_DONE_STATUSES

        assert "awaiting_approval" not in STEP_DONE_STATUSES


# ---------------------------------------------------------------------------
# MonitorCIStep -- no-checks grace period
# ---------------------------------------------------------------------------


class TestMonitorCIStep:
    async def test_passes_when_all_checks_pass(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(pr_number=10)
        step = MonitorCIStep()

        check = MagicMock(is_completed=True, is_passed=True, is_failed=False, name="CI")
        with patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks:
            mock_checks.return_value = [check]
            result = await step.execute(ctx)

        assert result.success
        assert "1 CI checks passed" in result.summary

    async def test_fails_when_check_fails_and_fix_disabled(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.max_fix_attempts = 0
        ctx = _make_ctx(pr_number=10, config=config)
        step = MonitorCIStep()

        check = MagicMock(is_completed=True, is_passed=False, is_failed=True, details_url="")
        check.name = "lint"
        with patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks:
            mock_checks.return_value = [check]
            result = await step.execute(ctx)

        assert not result.success
        assert "lint" in result.summary

    async def test_no_checks_passes_after_grace_period(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.no_checks_grace_period = 0
        ctx = _make_ctx(pr_number=10, config=config)
        step = MonitorCIStep()

        with patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks:
            mock_checks.return_value = []
            result = await step.execute(ctx)

        assert result.success
        assert "no ci checks" in result.summary.lower()

    async def test_no_checks_waits_during_grace_period(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.no_checks_grace_period = 120
        config.ci.poll_interval = 60
        config.ci.max_wait = 180
        ctx = _make_ctx(pr_number=10, config=config)
        step = MonitorCIStep()

        check = MagicMock(is_completed=True, is_passed=True, is_failed=False, name="CI")
        with (
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            # No checks on first poll, then checks appear
            mock_checks.side_effect = [[], [check]]
            result = await step.execute(ctx)

        assert result.success
        assert "1 CI checks passed" in result.summary

    async def test_fetch_failure_does_not_false_pass(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.no_checks_grace_period = 0
        config.ci.poll_interval = 10
        config.ci.max_wait = 30
        ctx = _make_ctx(pr_number=10, config=config)
        step = MonitorCIStep()

        passed_check = MagicMock(is_completed=True, is_passed=True, is_failed=False, name="CI")
        with (
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_checks.side_effect = [None, None, [passed_check]]
            result = await step.execute(ctx)

        assert result.success
        assert "1 CI checks passed" in result.summary
        assert mock_checks.call_count == 3

    async def test_fetch_failure_times_out(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.poll_interval = 10
        config.ci.max_wait = 20
        ctx = _make_ctx(pr_number=10, config=config)
        step = MonitorCIStep()

        with (
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_checks.return_value = None
            result = await step.execute(ctx)

        assert not result.success
        assert "timed out" in result.summary.lower()

    async def test_fails_when_no_pr(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(pr_number=None)
        step = MonitorCIStep()

        result = await step.execute(ctx)

        assert not result.success
        assert "no pr" in result.summary.lower()

    async def test_poll_ci_waits_for_sha_match(self) -> None:
        """When expected_sha is set, poll waits until PR head matches before checking CI."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.poll_interval = 10
        config.ci.max_wait = 60
        ctx = _make_ctx(pr_number=10, config=config)
        step = MonitorCIStep()

        passed_check = MagicMock(is_completed=True, is_passed=True, is_failed=False, name="CI")
        with (
            patch.object(step, "_verify_pr_head_sha", new_callable=AsyncMock) as mock_verify,
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_verify.side_effect = [False, False, True]
            mock_checks.return_value = [passed_check]
            result, _ = await step._poll_ci(ctx, expected_sha="abc123def456")

        assert result.success
        assert mock_verify.call_count == 3
        assert mock_checks.call_count == 1

    async def test_skipped_checks_do_not_block_or_fail(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(pr_number=10)
        step = MonitorCIStep()

        passed = MagicMock(is_completed=True, is_passed=True, is_failed=False, name="Tests")
        skipped = MagicMock(is_completed=True, is_passed=False, is_failed=False, name="Fork Gate")
        with patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks:
            mock_checks.return_value = [passed, skipped]
            result = await step.execute(ctx)

        assert result.success
        assert "2 CI checks passed" in result.summary

    async def test_exclude_checks_filters_matching(self) -> None:
        """Checks matching exclude_checks patterns are ignored entirely."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.exclude_checks = ["bonfire-tekton"]
        ctx = _make_ctx(pr_number=10, config=config)
        step = MonitorCIStep()

        passed = MagicMock(is_completed=True, is_passed=True, is_failed=False)
        passed.name = "Tests"
        excluded = MagicMock(is_completed=False, is_passed=False, is_failed=False)
        excluded.name = "Red Hat Konflux / rbac-bonfire-tekton / insights-rbac"
        with patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks:
            mock_checks.return_value = [passed, excluded]
            result = await step.execute(ctx)

        assert result.success
        assert "1 CI checks passed" in result.summary

    async def test_exclude_checks_does_not_filter_non_matching(self) -> None:
        """Checks not matching exclude_checks patterns are still evaluated."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.exclude_checks = ["bonfire-tekton"]
        config.ci.max_fix_attempts = 0
        ctx = _make_ctx(pr_number=10, config=config)
        step = MonitorCIStep()

        failed = MagicMock(is_completed=True, is_passed=False, is_failed=True, details_url="")
        failed.name = "lint"
        with patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks:
            mock_checks.return_value = [failed]
            result = await step.execute(ctx)

        assert not result.success
        assert "lint" in result.summary

    async def test_resume_skips_poll_when_ci_passed(self) -> None:
        """On resume, if CI already passed, skip polling entirely."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(pr_number=10, resume_run_id=5)
        step = MonitorCIStep()

        check = MagicMock(is_completed=True, is_passed=True, is_failed=False, name="CI")
        with patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks:
            mock_checks.return_value = [check]
            result = await step.execute(ctx)

        assert result.success
        assert "resumed after interruption" in result.summary

    async def test_resume_skips_poll_when_ci_failed(self) -> None:
        """On resume, if CI already failed, return failure immediately."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.max_fix_attempts = 0
        ctx = _make_ctx(pr_number=10, config=config, resume_run_id=5)
        step = MonitorCIStep()

        check = MagicMock(is_completed=True, is_passed=False, is_failed=True, details_url="")
        check.name = "lint"
        with patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks:
            mock_checks.return_value = [check]
            result = await step.execute(ctx)

        assert not result.success
        assert "lint" in result.summary

    async def test_resume_polls_when_ci_incomplete(self) -> None:
        """On resume, if CI is still running, fall through to normal polling."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(pr_number=10, resume_run_id=5)
        step = MonitorCIStep()

        incomplete = MagicMock(is_completed=False, is_passed=False, is_failed=False, name="CI")
        passed = MagicMock(is_completed=True, is_passed=True, is_failed=False, name="CI")
        with (
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            # Resume check returns incomplete, then normal poll returns passed
            mock_checks.side_effect = [[incomplete], [passed]]
            result = await step.execute(ctx)

        assert result.success
        assert "1 CI checks passed" in result.summary

    async def test_resume_respects_exclude_checks(self) -> None:
        """On resume, excluded checks should not cause false failures."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.exclude_checks = ["sonar"]
        ctx = _make_ctx(pr_number=10, config=config, resume_run_id=5)
        step = MonitorCIStep()

        passed = MagicMock(is_completed=True, is_passed=True, is_failed=False)
        passed.name = "Tests"
        excluded_fail = MagicMock(is_completed=True, is_passed=False, is_failed=True)
        excluded_fail.name = "sonar-quality-gate"
        with patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks:
            mock_checks.return_value = [passed, excluded_fail]
            result = await step.execute(ctx)

        assert result.success
        assert "resumed after interruption" in result.summary

    async def test_heartbeat_writes_during_poll(self) -> None:
        """CI polling should write heartbeat messages to the output writer."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.poll_interval = 10
        config.ci.max_wait = 30
        ctx = _make_ctx(pr_number=10, config=config)
        mock_writer = MagicMock()
        ctx.output_writer = mock_writer
        step = MonitorCIStep()

        incomplete = MagicMock(is_completed=False, is_passed=False, is_failed=False, name="CI")
        passed = MagicMock(is_completed=True, is_passed=True, is_failed=False, name="CI")
        with (
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_checks.side_effect = [[incomplete], [passed]]
            result = await step.execute(ctx)

        assert result.success
        assert mock_writer.write_line.called
        heartbeat_msg = mock_writer.write_line.call_args_list[0][0][0]
        assert "CI poll:" in heartbeat_msg


# ---------------------------------------------------------------------------
class TestCheckExistingCI:
    """Tests for MonitorCIStep._check_existing_ci fast path."""

    async def test_no_new_commits_all_passed(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(pr_number=10)
        ctx.no_new_commits = True
        step = MonitorCIStep()

        check = MagicMock(is_completed=True, is_failed=False)
        check.name = "CI"
        with patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks:
            mock_checks.return_value = [check]
            result = await step.execute(ctx)

        assert result.success
        assert "already passed" in result.summary

    async def test_no_new_commits_some_failed(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.max_fix_attempts = 0
        ctx = _make_ctx(pr_number=10, config=config)
        ctx.no_new_commits = True
        step = MonitorCIStep()

        check = MagicMock(is_completed=True, is_failed=True)
        check.name = "lint"
        with patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks:
            mock_checks.return_value = [check]
            result = await step.execute(ctx)

        assert not result.success
        assert "already failed" in result.summary
        assert "lint" in result.summary

    async def test_no_new_commits_still_running_falls_through(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.no_checks_grace_period = 0
        config.ci.poll_interval = 10
        config.ci.max_wait = 20
        ctx = _make_ctx(pr_number=10, config=config)
        ctx.no_new_commits = True
        step = MonitorCIStep()

        running = MagicMock(is_completed=False, is_failed=False)
        running.name = "CI"
        passed = MagicMock(is_completed=True, is_failed=False)
        passed.name = "CI"
        with (
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_checks.side_effect = [[running], [passed]]
            result = await step.execute(ctx)

        assert result.success

    async def test_no_new_commits_no_checks(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(pr_number=10)
        ctx.no_new_commits = True
        step = MonitorCIStep()

        with patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks:
            mock_checks.return_value = []
            result = await step.execute(ctx)

        assert result.success
        assert "no ci checks" in result.summary.lower() or "No new commits" in result.summary

    async def test_no_new_commits_fetch_failure_falls_through(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.no_checks_grace_period = 0
        config.ci.poll_interval = 10
        config.ci.max_wait = 20
        ctx = _make_ctx(pr_number=10, config=config)
        ctx.no_new_commits = True
        step = MonitorCIStep()

        passed = MagicMock(is_completed=True, is_failed=False)
        passed.name = "CI"
        with (
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_checks.side_effect = [None, [passed]]
            result = await step.execute(ctx)

        assert result.success
        assert mock_checks.call_count == 2

    async def test_no_new_commits_respects_exclude_checks(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.exclude_checks = ["flaky-sonar"]
        ctx = _make_ctx(pr_number=10, config=config)
        ctx.no_new_commits = True
        step = MonitorCIStep()

        good = MagicMock(is_completed=True, is_failed=False)
        good.name = "tests"
        flaky = MagicMock(is_completed=True, is_failed=True)
        flaky.name = "flaky-sonar-check"
        with patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks:
            mock_checks.return_value = [good, flaky]
            result = await step.execute(ctx)

        assert result.success
        assert "1" in result.summary


class TestEnsureCommitted:
    """Tests for MonitorCIStep._ensure_committed lint-then-commit safety net."""

    async def test_ruff_fixes_committed(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(pr_number=10)
        step = MonitorCIStep()
        run = AsyncMock()
        run.return_value = MagicMock(success=True, stdout="M sova/core/foo.py\n", stderr="")

        result = await step._ensure_committed(ctx, run)

        assert result is True
        calls = [c[0] for c in run.call_args_list]
        assert ("ruff", "check", "--fix", ".") == calls[0][:4]
        assert ("ruff", "format", ".") == calls[1][:3]
        assert ("git", "status", "--porcelain") == calls[2][:3]
        assert ("git", "add", "-u") == calls[3][:3]
        assert calls[4][0] == "git" and calls[4][1] == "commit"

    async def test_no_changes_returns_false(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(pr_number=10)
        step = MonitorCIStep()
        run = AsyncMock()
        run.return_value = MagicMock(success=True, stdout="", stderr="")

        result = await step._ensure_committed(ctx, run)

        assert result is False

    async def test_ruff_not_installed_continues(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(pr_number=10)
        step = MonitorCIStep()

        ruff_fail = MagicMock(success=False, stdout="", stderr="command not found")
        status_clean = MagicMock(success=True, stdout="", stderr="")

        run = AsyncMock(side_effect=[ruff_fail, status_clean])
        result = await step._ensure_committed(ctx, run)

        assert result is False
        assert run.call_count == 2

    async def test_ruff_makes_no_changes(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(pr_number=10)
        step = MonitorCIStep()

        ruff_ok = MagicMock(success=True, stdout="", stderr="")
        format_ok = MagicMock(success=True, stdout="", stderr="")
        status_clean = MagicMock(success=True, stdout="", stderr="")

        run = AsyncMock(side_effect=[ruff_ok, format_ok, status_clean])
        result = await step._ensure_committed(ctx, run)

        assert result is False

    async def test_stage_failure_returns_none(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(pr_number=10)
        step = MonitorCIStep()

        ruff_ok = MagicMock(success=True, stdout="", stderr="")
        format_ok = MagicMock(success=True, stdout="", stderr="")
        status_dirty = MagicMock(success=True, stdout="M foo.py\n", stderr="")
        add_fail = MagicMock(success=False, stdout="", stderr="error")

        run = AsyncMock(side_effect=[ruff_ok, format_ok, status_dirty, add_fail])
        result = await step._ensure_committed(ctx, run)

        assert result is None

    async def test_commit_failure_returns_none(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(pr_number=10)
        step = MonitorCIStep()

        ruff_ok = MagicMock(success=True, stdout="", stderr="")
        format_ok = MagicMock(success=True, stdout="", stderr="")
        status_dirty = MagicMock(success=True, stdout="M foo.py\n", stderr="")
        add_ok = MagicMock(success=True, stdout="", stderr="")
        commit_fail = MagicMock(success=False, stdout="", stderr="error")

        run = AsyncMock(side_effect=[ruff_ok, format_ok, status_dirty, add_ok, commit_fail])
        result = await step._ensure_committed(ctx, run)

        assert result is None


class TestValidateFixRunsEnsureCommitted:
    """Verify _ensure_committed is called before the pre-push hook in _validate_fix."""

    async def test_ensure_committed_called_before_hook(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(pr_number=10, branch_name="feat/test")
        step = MonitorCIStep()

        call_order = []

        async def mock_ensure(c, r):
            call_order.append("ensure_committed")
            return False

        async def mock_find_hook(wd):
            call_order.append("find_hook")
            return None

        run = AsyncMock()
        run.return_value = MagicMock(success=True, stdout="1\n", stderr="")

        with (
            patch.object(step, "_ensure_committed", side_effect=mock_ensure),
            patch("sova.core.steps.validate.find_pre_push_hook", mock_find_hook),
        ):
            await step._validate_fix(ctx, 1, 1, run)

        assert call_order == ["ensure_committed", "find_hook"]

    async def test_auto_commits_when_llm_skips_commit(self) -> None:
        """_ensure_committed creates a commit when LLM left changes uncommitted."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(pr_number=10, branch_name="feat/test")
        step = MonitorCIStep()

        ruff_ok = MagicMock(success=True, stdout="", stderr="")
        format_ok = MagicMock(success=True, stdout="", stderr="")
        status_dirty = MagicMock(success=True, stdout="M foo.py\n", stderr="")
        add_ok = MagicMock(success=True, stdout="", stderr="")
        commit_ok = MagicMock(success=True, stdout="", stderr="")

        run = AsyncMock(side_effect=[ruff_ok, format_ok, status_dirty, add_ok, commit_ok])
        committed = await step._ensure_committed(ctx, run)

        assert committed is True
        commit_call = run.call_args_list[4]
        assert commit_call[0][1] == "commit"
        assert "fix(core):" in commit_call[0][3]

    async def test_ensure_committed_failure_skips_attempt(self) -> None:
        """_validate_fix skips attempt when _ensure_committed returns None."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(pr_number=10, branch_name="feat/test")
        step = MonitorCIStep()

        async def mock_ensure_fail(c, r):
            return None

        run = AsyncMock()
        run.return_value = MagicMock(success=True, stdout="1\n", stderr="")

        with patch.object(step, "_ensure_committed", side_effect=mock_ensure_fail):
            should_skip, error = await step._validate_fix(ctx, 1, 2, run)

        assert should_skip is True
        assert error is None

    async def test_ensure_committed_failure_last_attempt_errors(self) -> None:
        """_validate_fix returns error on last attempt when _ensure_committed fails."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(pr_number=10, branch_name="feat/test")
        step = MonitorCIStep()

        async def mock_ensure_fail(c, r):
            return None

        run = AsyncMock()
        run.return_value = MagicMock(success=True, stdout="1\n", stderr="")

        with patch.object(step, "_ensure_committed", side_effect=mock_ensure_fail):
            should_skip, error = await step._validate_fix(ctx, 2, 2, run)

        assert should_skip is False
        assert error is not None
        assert not error.success
        assert "stage/commit" in error.summary


class TestBuildFixPromptEnhancements:
    """Verify _build_fix_prompt includes lint instructions and invariant rules."""

    def test_includes_ruff_instructions(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(pr_number=10)
        check = MagicMock()
        check.name = "tests"
        step = MonitorCIStep()

        prompt = step._build_fix_prompt([check], "error log", "abc123 commit", ctx)

        assert "ruff check --fix" in prompt
        assert "ruff format" in prompt

    def test_includes_invariant_rules(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(pr_number=10)
        check = MagicMock()
        check.name = "tests"
        step = MonitorCIStep()

        prompt = step._build_fix_prompt([check], "error log", "abc123 commit", ctx)

        assert "Decimal" in prompt
        assert "floats" in prompt
        assert "co-author" in prompt
        assert "emojis" in prompt
        assert "Project Invariant Rules" in prompt

    def test_prompt_uses_scoped_commit_format(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(pr_number=10)
        check = MagicMock()
        check.name = "tests"
        step = MonitorCIStep()

        prompt = step._build_fix_prompt([check], "error log", "abc123 commit", ctx)

        assert "fix(core):" in prompt


class TestCIConfigMaxWaitDefault:
    """Verify CIConfig.max_wait default is 1500."""

    def test_default_max_wait(self) -> None:
        config = ProjectConfig()
        assert config.ci.max_wait == 1500


class TestCIMaxFixAttemptsInSettingsMeta:
    """Verify ci.max_fix_attempts is registered in settings_meta."""

    def test_registered(self) -> None:
        from sova.dashboard.settings_meta import get_meta

        meta = get_meta("ci.max_fix_attempts")
        assert meta is not None
        assert meta.group == "ci"
        assert meta.value_type == "number"


class TestParseReviewBody:
    """Tests for _parse_review_body structured finding parser."""

    def test_structured_finding(self) -> None:
        from sova.core.steps.address_review import _parse_review_body

        body = (
            "[HIGH] Correctness -- Missing null check\n"
            "Location: sova/core/steps/commit.py:42\n"
            "Problem: The variable could be None when accessed.\n"
            "Suggestion: Add an explicit None check before use."
        )
        findings = _parse_review_body(body)
        assert len(findings) == 1
        assert findings[0]["severity"] == 9
        assert findings[0]["category"] == "correctness"
        assert findings[0]["file"] == "sova/core/steps/commit.py"
        assert findings[0]["line"] == 42
        assert "None" in findings[0]["description"]
        assert "check" in findings[0]["suggestion"]

    def test_critical_severity(self) -> None:
        from sova.core.steps.address_review import _parse_review_body

        body = "[CRITICAL] Security -- SQL injection risk\nLocation: app.py:10"
        findings = _parse_review_body(body)
        assert len(findings) == 1
        assert findings[0]["severity"] == 10

    def test_unstructured_fallback(self) -> None:
        from sova.core.steps.address_review import _parse_review_body

        body = "This code has several issues that need addressing. " * 3
        findings = _parse_review_body(body)
        assert len(findings) == 1
        assert findings[0]["category"] == "review"
        assert findings[0]["severity"] == 7

    def test_short_body_returns_empty(self) -> None:
        from sova.core.steps.address_review import _parse_review_body

        findings = _parse_review_body("LGTM")
        assert findings == []

    def test_empty_body_returns_empty(self) -> None:
        from sova.core.steps.address_review import _parse_review_body

        findings = _parse_review_body("")
        assert findings == []

    def test_line_range_location_preserves_file_path(self) -> None:
        from sova.core.steps.address_review import _parse_review_body

        body = "[HIGH] Correctness -- Range location\nLocation: foo.py:10-15\nProblem: Something is wrong."
        findings = _parse_review_body(body)
        assert len(findings) == 1
        assert findings[0]["file"] == "foo.py"
        assert findings[0]["line"] is None

    def test_multiple_findings(self) -> None:
        from sova.core.steps.address_review import _parse_review_body

        body = "[HIGH] Correctness -- First issue\nLocation: a.py:1\n\n[LOW] Style -- Second issue\nLocation: b.py:2"
        findings = _parse_review_body(body)
        assert len(findings) == 2
        assert findings[0]["severity"] == 9
        assert findings[1]["severity"] == 3

    def test_multi_word_category(self) -> None:
        from sova.core.steps.address_review import _parse_review_body

        body = "[HIGH] Type Safety -- Missing type annotation\nLocation: sova/core/context.py:55"
        findings = _parse_review_body(body)
        assert len(findings) == 1
        assert findings[0]["category"] == "type safety"
        assert findings[0]["severity"] == 9


# ---------------------------------------------------------------------------
def _gh_reviews_json(*reviews: dict) -> str:
    """Build a JSON string mimicking ``gh api repos/.../pulls/N/reviews``."""
    return json.dumps(reviews)


class TestLoadFindingsFromGithubReviews:
    """Tests for _load_findings_from_github_reviews (calls gh api directly)."""

    def _patch_run(self, gh_json: str):
        return patch(
            "sova.core.steps.address_review.run",
            new_callable=AsyncMock,
            return_value=MagicMock(success=True, stdout=gh_json),
        )

    async def test_filters_bot_reviews(self) -> None:
        from sova.core.steps.address_review import _load_findings_from_github_reviews

        ctx = _make_ctx(pr_number=10)
        gh_json = _gh_reviews_json(
            {"user": {"login": "bot", "type": "Bot"}, "state": "COMMENTED", "body": "[HIGH] Bug -- some issue"},
            {
                "user": {"login": "dev", "type": "User"},
                "state": "COMMENTED",
                "body": "[HIGH] Bug -- real issue\nLocation: a.py:1",
            },
        )
        with self._patch_run(gh_json):
            findings = await _load_findings_from_github_reviews(ctx)
        assert len(findings) == 1
        assert findings[0]["description"] == "real issue"

    async def test_filters_dismissed_reviews(self) -> None:
        from sova.core.steps.address_review import _load_findings_from_github_reviews

        ctx = _make_ctx(pr_number=10)
        gh_json = _gh_reviews_json(
            {"user": {"login": "dev", "type": "User"}, "state": "DISMISSED", "body": "[HIGH] Bug -- old issue"},
        )
        with self._patch_run(gh_json):
            findings = await _load_findings_from_github_reviews(ctx)
        assert findings == []

    async def test_filters_empty_body(self) -> None:
        from sova.core.steps.address_review import _load_findings_from_github_reviews

        ctx = _make_ctx(pr_number=10)
        gh_json = _gh_reviews_json(
            {"user": {"login": "dev", "type": "User"}, "state": "COMMENTED", "body": "  "},
        )
        with self._patch_run(gh_json):
            findings = await _load_findings_from_github_reviews(ctx)
        assert findings == []

    async def test_gh_api_failure_returns_empty(self) -> None:
        from sova.core.steps.address_review import _load_findings_from_github_reviews

        ctx = _make_ctx(pr_number=10)
        mock_result = MagicMock(success=False, stdout="", stderr="API error")
        with patch(
            "sova.core.steps.address_review.run",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            findings = await _load_findings_from_github_reviews(ctx)
        assert findings == []

    async def test_parses_bold_wrapped_findings(self) -> None:
        from sova.core.steps.address_review import _load_findings_from_github_reviews

        ctx = _make_ctx(pr_number=10)
        body = (
            "**[HIGH] Security -- XSS vulnerability**\n"
            "Location: app.py:42\n"
            "Problem: Unsanitized input\n"
            "Suggestion: Use escape()"
        )
        gh_json = _gh_reviews_json(
            {"user": {"login": "reviewer", "type": "User"}, "state": "COMMENTED", "body": body},
        )
        with self._patch_run(gh_json):
            findings = await _load_findings_from_github_reviews(ctx)
        assert len(findings) == 1
        assert findings[0]["severity"] == 9
        assert findings[0]["file"] == "app.py"
        assert findings[0]["line"] == 42
        assert findings[0]["category"] == "security"


# MonitorCIStep -- CI fix loop
# ---------------------------------------------------------------------------


def _make_ci_check(name: str = "Tests", passed: bool = False, details_url: str = "") -> MagicMock:
    """Create a mock CICheck for CI fix tests."""
    check = MagicMock(is_completed=True, is_passed=passed, is_failed=not passed, details_url=details_url)
    check.name = name
    return check


def _shell_side_effect(*args: str, **kwargs: object) -> MagicMock:
    """Dispatch shell.run mock based on the command being called."""
    if "rev-list" in args:
        return MagicMock(success=True, stdout="1\n")
    if "rev-parse" in args:
        return MagicMock(success=True, stdout="abc123def456\n")
    if "ruff" in args:
        return MagicMock(success=True, stdout="")
    if args and args[0] == "git" and len(args) > 1 and args[1] == "status":
        return MagicMock(success=True, stdout="")
    return MagicMock(success=True, stdout="abc1234 fix\n")


class TestMonitorCIFixLoop:
    async def test_ci_fails_fix_succeeds_first_attempt(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(pr_number=10, branch_name="feat/test", worktree_dir=Path("/tmp/wt"))
        step = MonitorCIStep()

        failed_check = _make_ci_check(
            "Tests", passed=False, details_url="https://github.com/o/r/actions/runs/123/job/456"
        )
        passed_check = _make_ci_check("Tests", passed=True)

        with (
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.get_ci_failure_logs", new_callable=AsyncMock) as mock_logs,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_checks.side_effect = [
                [failed_check],  # initial poll
                [passed_check],  # re-poll after fix
            ]
            mock_logs.return_value = "ERROR: ModuleNotFoundError: No module named 'PIL'"

            with (
                patch("sova.core.steps.validate.find_pre_push_hook", new_callable=AsyncMock, return_value=None),
                patch("sova.git.operations.push", new_callable=AsyncMock),
                patch("sova.llm.client.invoke", new_callable=AsyncMock) as mock_invoke,
                patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run,
                patch.object(step, "_verify_pr_head_sha", new_callable=AsyncMock, return_value=True),
            ):
                mock_invoke.return_value = LLMResult(text="Fixed", model="opus", cost_usd=Decimal("1.00"))
                mock_run.side_effect = _shell_side_effect
                result = await step.execute(ctx)

        assert result.success
        assert "1 fix attempt" in result.summary
        assert ctx.cost_usd == Decimal("1.00")

    async def test_ci_fails_fix_succeeds_second_attempt(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(pr_number=10, branch_name="feat/test", worktree_dir=Path("/tmp/wt"))
        step = MonitorCIStep()

        failed_check = _make_ci_check(
            "Tests", passed=False, details_url="https://github.com/o/r/actions/runs/123/job/456"
        )
        passed_check = _make_ci_check("Tests", passed=True)

        with (
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.get_ci_failure_logs", new_callable=AsyncMock) as mock_logs,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_checks.side_effect = [
                [failed_check],  # initial poll
                [failed_check],  # re-poll after fix 1 (still fails)
                [passed_check],  # re-poll after fix 2 (passes)
            ]
            mock_logs.return_value = "ERROR: test failure"

            with (
                patch("sova.core.steps.validate.find_pre_push_hook", new_callable=AsyncMock, return_value=None),
                patch("sova.git.operations.push", new_callable=AsyncMock),
                patch("sova.llm.client.invoke", new_callable=AsyncMock) as mock_invoke,
                patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run,
                patch.object(step, "_verify_pr_head_sha", new_callable=AsyncMock, return_value=True),
            ):
                mock_invoke.return_value = LLMResult(text="Fixed", model="opus", cost_usd=Decimal("0.50"))
                mock_run.side_effect = _shell_side_effect
                result = await step.execute(ctx)

        assert result.success
        assert "2 fix attempt" in result.summary
        assert mock_invoke.await_count == 2
        assert ctx.cost_usd == Decimal("1.00")

    async def test_ci_fails_all_attempts_exhausted(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep
        from sova.llm.models import LLMResult

        config = ProjectConfig()
        config.ci.max_fix_attempts = 2
        ctx = _make_ctx(pr_number=10, branch_name="feat/test", worktree_dir=Path("/tmp/wt"), config=config)
        step = MonitorCIStep()

        failed_check = _make_ci_check(
            "Tests", passed=False, details_url="https://github.com/o/r/actions/runs/123/job/456"
        )

        with (
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.get_ci_failure_logs", new_callable=AsyncMock) as mock_logs,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_checks.return_value = [failed_check]
            mock_logs.return_value = "ERROR: persistent failure"

            with (
                patch("sova.core.steps.validate.find_pre_push_hook", new_callable=AsyncMock, return_value=None),
                patch("sova.git.operations.push", new_callable=AsyncMock),
                patch("sova.llm.client.invoke", new_callable=AsyncMock) as mock_invoke,
                patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run,
                patch.object(step, "_verify_pr_head_sha", new_callable=AsyncMock, return_value=True),
            ):
                mock_invoke.return_value = LLMResult(text="Tried fix", model="opus", cost_usd=Decimal("0.50"))
                mock_run.side_effect = _shell_side_effect
                result = await step.execute(ctx)

        assert not result.success
        assert "2 fix attempt" in result.summary
        assert "Tests" in result.summary
        assert mock_invoke.await_count == 2

    async def test_ci_fix_disabled_when_zero(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig()
        config.ci.max_fix_attempts = 0
        ctx = _make_ctx(pr_number=10, config=config)
        step = MonitorCIStep()

        failed_check = _make_ci_check("lint", passed=False)
        with patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks:
            mock_checks.return_value = [failed_check]
            result = await step.execute(ctx)

        assert not result.success
        assert "lint" in result.summary

    async def test_ci_fix_respects_budget(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(pr_number=10, branch_name="feat/test", worktree_dir=Path("/tmp/wt"))
        ctx.cost_usd = Decimal("999")  # over budget
        step = MonitorCIStep()

        failed_check = _make_ci_check("Tests", passed=False, details_url="https://github.com/o/r/actions/runs/1/job/2")

        with (
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_checks.return_value = [failed_check]
            result = await step.execute(ctx)

        assert not result.success
        assert "budget" in result.summary.lower()

    async def test_ci_fix_local_hook_fails(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep
        from sova.llm.models import LLMResult

        config = ProjectConfig()
        config.ci.max_fix_attempts = 1
        ctx = _make_ctx(pr_number=10, branch_name="feat/test", worktree_dir=Path("/tmp/wt"), config=config)
        step = MonitorCIStep()

        failed_check = _make_ci_check("Tests", passed=False, details_url="https://github.com/o/r/actions/runs/1/job/2")

        with (
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.get_ci_failure_logs", new_callable=AsyncMock, return_value="error"),
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_checks.return_value = [failed_check]

            with (
                patch(
                    "sova.core.steps.validate.find_pre_push_hook",
                    new_callable=AsyncMock,
                    return_value="/hooks/pre-push",
                ),
                patch("sova.git.operations.push", new_callable=AsyncMock) as mock_push,
                patch("sova.llm.client.invoke", new_callable=AsyncMock) as mock_invoke,
                patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run,
            ):
                mock_invoke.return_value = LLMResult(text="Fixed", model="opus", cost_usd=Decimal("0.50"))
                mock_run.side_effect = [
                    MagicMock(success=True, stdout="abc fix\n"),  # git log
                    MagicMock(success=True, stdout=""),  # ruff check (from _ensure_committed)
                    MagicMock(success=True, stdout=""),  # ruff format (from _ensure_committed)
                    MagicMock(success=True, stdout=""),  # git status (from _ensure_committed, clean)
                    MagicMock(success=False, stdout="FAIL\n", stderr="hook error"),  # pre-push fails
                ]
                result = await step.execute(ctx)

        assert not result.success
        assert "local validation" in result.summary.lower()
        mock_push.assert_not_awaited()

    async def test_ci_fix_push_fails(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(pr_number=10, branch_name="feat/test", worktree_dir=Path("/tmp/wt"))
        step = MonitorCIStep()

        failed_check = _make_ci_check("Tests", passed=False, details_url="https://github.com/o/r/actions/runs/1/job/2")

        with (
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.get_ci_failure_logs", new_callable=AsyncMock, return_value="error"),
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_checks.return_value = [failed_check]

            with (
                patch("sova.core.steps.validate.find_pre_push_hook", new_callable=AsyncMock, return_value=None),
                patch("sova.git.operations.push", new_callable=AsyncMock, side_effect=RuntimeError("push rejected")),
                patch("sova.llm.client.invoke", new_callable=AsyncMock) as mock_invoke,
                patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run,
            ):
                mock_invoke.return_value = LLMResult(text="Fixed", model="opus", cost_usd=Decimal("0.50"))
                mock_run.side_effect = _shell_side_effect
                result = await step.execute(ctx)

        assert not result.success
        assert "push failed" in result.summary.lower()

    async def test_ci_log_fetch_graceful_failure(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(pr_number=10, branch_name="feat/test", worktree_dir=Path("/tmp/wt"))
        step = MonitorCIStep()

        failed_check = _make_ci_check("Tests", passed=False, details_url="")
        passed_check = _make_ci_check("Tests", passed=True)

        with (
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.get_ci_failure_logs", new_callable=AsyncMock) as mock_logs,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_checks.side_effect = [[failed_check], [passed_check]]
            mock_logs.return_value = ""

            with (
                patch("sova.core.steps.validate.find_pre_push_hook", new_callable=AsyncMock, return_value=None),
                patch("sova.git.operations.push", new_callable=AsyncMock),
                patch("sova.llm.client.invoke", new_callable=AsyncMock) as mock_invoke,
                patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run,
                patch.object(step, "_verify_pr_head_sha", new_callable=AsyncMock, return_value=True),
            ):
                mock_invoke.return_value = LLMResult(text="Fixed", model="opus", cost_usd=Decimal("0.50"))
                mock_run.side_effect = _shell_side_effect
                result = await step.execute(ctx)

        assert result.success
        mock_invoke.assert_awaited_once()


# ---------------------------------------------------------------------------
# MonitorCIStep -- SonarCloud coverage handling
# ---------------------------------------------------------------------------


class TestMonitorCISonarCloud:
    async def test_split_sonarcloud_checks(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        sonar = _make_ci_check("SonarCloud Code Analysis", passed=False)
        tests = _make_ci_check("Tests", passed=False)
        lint = _make_ci_check("Lint", passed=False)

        sonar_checks, other_checks = MonitorCIStep._split_sonarcloud_checks([sonar, tests, lint])
        assert len(sonar_checks) == 1
        assert sonar_checks[0].name == "SonarCloud Code Analysis"
        assert len(other_checks) == 2

    async def test_split_no_sonarcloud(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        tests = _make_ci_check("Tests", passed=False)
        sonar_checks, other_checks = MonitorCIStep._split_sonarcloud_checks([tests])
        assert sonar_checks == []
        assert len(other_checks) == 1

    async def test_fetch_coverage_prompt_returns_empty_without_sonar_checks(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(pr_number=10)
        result = await MonitorCIStep._fetch_sonarcloud_coverage_prompt(ctx, [])
        assert result == ""

    async def test_fetch_coverage_prompt_returns_empty_without_config(self) -> None:
        from sova.config.models import ExternalReviewsConfig
        from sova.core.steps.monitor_ci import MonitorCIStep

        config = ProjectConfig(external_reviews=ExternalReviewsConfig())
        ctx = _make_ctx(pr_number=10, config=config)
        sonar = _make_ci_check("SonarCloud Code Analysis", passed=False)
        result = await MonitorCIStep._fetch_sonarcloud_coverage_prompt(ctx, [sonar])
        assert result == ""

    async def test_fetch_coverage_prompt_returns_empty_when_sonarcloud_not_in_tools(self) -> None:
        from sova.config.models import ExternalReviewsConfig, SonarCloudConfig
        from sova.core.steps.monitor_ci import MonitorCIStep

        ext = ExternalReviewsConfig(
            enabled=True,
            tools=["coderabbit"],
            sonarcloud=SonarCloudConfig(project_key="org_repo"),
        )
        config = ProjectConfig(external_reviews=ext)
        ctx = _make_ctx(pr_number=10, config=config)
        sonar = _make_ci_check("SonarCloud Code Analysis", passed=False)
        result = await MonitorCIStep._fetch_sonarcloud_coverage_prompt(ctx, [sonar])
        assert result == ""

    async def test_fetch_coverage_prompt_with_gap(self) -> None:
        from sova.adapters.external_reviews import CoverageReport, ExternalFinding
        from sova.config.models import ExternalReviewsConfig, SonarCloudConfig
        from sova.core.steps.monitor_ci import MonitorCIStep

        ext = ExternalReviewsConfig(
            enabled=True,
            tools=["sonarcloud"],
            sonarcloud=SonarCloudConfig(project_key="org_repo"),
        )
        config = ProjectConfig(external_reviews=ext)
        ctx = _make_ctx(pr_number=10, config=config)
        sonar = _make_ci_check("SonarCloud Code Analysis", passed=False)

        report = CoverageReport(
            coverage_pct=Decimal("65.0"),
            required_pct=Decimal("80.0"),
            findings=[
                ExternalFinding("sonarcloud-coverage", "sova/core/workflow.py", 235, "MAJOR", "Uncovered"),
            ],
        )
        with patch(
            "sova.adapters.external_reviews.fetch_sonarcloud_coverage_issues",
            new_callable=AsyncMock,
            return_value=report,
        ):
            result = await MonitorCIStep._fetch_sonarcloud_coverage_prompt(ctx, [sonar])

        assert "65.0%" in result
        assert "pytest" in result
        assert "sova/core/workflow.py:235" in result

    async def test_fetch_coverage_prompt_coverage_ok(self) -> None:
        from sova.adapters.external_reviews import CoverageReport
        from sova.config.models import ExternalReviewsConfig, SonarCloudConfig
        from sova.core.steps.monitor_ci import MonitorCIStep

        ext = ExternalReviewsConfig(
            enabled=True,
            tools=["sonarcloud"],
            sonarcloud=SonarCloudConfig(project_key="org_repo"),
        )
        config = ProjectConfig(external_reviews=ext)
        ctx = _make_ctx(pr_number=10, config=config)
        sonar = _make_ci_check("SonarCloud Code Analysis", passed=False)

        report = CoverageReport(coverage_pct=Decimal("85.0"), required_pct=Decimal("80.0"), findings=[])
        with patch(
            "sova.adapters.external_reviews.fetch_sonarcloud_coverage_issues",
            new_callable=AsyncMock,
            return_value=report,
        ):
            result = await MonitorCIStep._fetch_sonarcloud_coverage_prompt(ctx, [sonar])

        assert result == ""

    async def test_invoke_fix_uses_coverage_prompt_for_sonarcloud(self) -> None:
        """When only SonarCloud fails, _invoke_fix uses the coverage prompt."""
        from sova.adapters.external_reviews import CoverageReport
        from sova.config.models import ExternalReviewsConfig, SonarCloudConfig
        from sova.core.steps.monitor_ci import MonitorCIStep
        from sova.llm.models import LLMResult

        ext = ExternalReviewsConfig(
            enabled=True,
            tools=["sonarcloud"],
            sonarcloud=SonarCloudConfig(project_key="org_repo"),
        )
        config = ProjectConfig(external_reviews=ext)
        ctx = _make_ctx(pr_number=10, branch_name="feat/test", worktree_dir=Path("/tmp/wt"), config=config)
        step = MonitorCIStep()

        sonar_check = _make_ci_check("SonarCloud Code Analysis", passed=False)
        report = CoverageReport(coverage_pct=Decimal("65.0"), required_pct=Decimal("80.0"), findings=[])

        mock_invoke = AsyncMock(return_value=LLMResult(text="Fixed", model="opus", cost_usd=Decimal("0.10")))
        mock_run = AsyncMock(side_effect=_shell_side_effect)

        with (
            patch(
                "sova.adapters.external_reviews.fetch_sonarcloud_coverage_issues",
                new_callable=AsyncMock,
                return_value=report,
            ),
            patch("sova.core.steps.monitor_ci.get_ci_failure_logs", new_callable=AsyncMock, return_value=""),
        ):
            cost, error = await step._invoke_fix(ctx, [sonar_check], mock_invoke, mock_run)

        assert error is None
        prompt_arg = mock_invoke.call_args[0][0]
        assert "65.0%" in prompt_arg
        assert "pytest" in prompt_arg

    async def test_invoke_fix_combined_sonar_and_other_failures(self) -> None:
        """When both SonarCloud and other checks fail, logs are fetched for non-Sonar checks only."""
        from sova.adapters.external_reviews import CoverageReport
        from sova.config.models import ExternalReviewsConfig, SonarCloudConfig
        from sova.core.steps.monitor_ci import MonitorCIStep
        from sova.llm.models import LLMResult

        ext = ExternalReviewsConfig(
            enabled=True,
            tools=["sonarcloud"],
            sonarcloud=SonarCloudConfig(project_key="org_repo"),
        )
        config = ProjectConfig(external_reviews=ext)
        ctx = _make_ctx(pr_number=10, branch_name="feat/test", worktree_dir=Path("/tmp/wt"), config=config)
        step = MonitorCIStep()

        sonar = _make_ci_check("SonarCloud Code Analysis", passed=False)
        tests = _make_ci_check("Tests", passed=False)
        report = CoverageReport(coverage_pct=Decimal("65.0"), required_pct=Decimal("80.0"), findings=[])

        mock_invoke = AsyncMock(return_value=LLMResult(text="Fixed", model="opus", cost_usd=Decimal("0.10")))
        mock_run = AsyncMock(side_effect=_shell_side_effect)

        with (
            patch(
                "sova.adapters.external_reviews.fetch_sonarcloud_coverage_issues",
                new_callable=AsyncMock,
                return_value=report,
            ),
            patch(
                "sova.core.steps.monitor_ci.get_ci_failure_logs",
                new_callable=AsyncMock,
                return_value="test failure logs",
            ) as mock_logs,
        ):
            cost, error = await step._invoke_fix(ctx, [sonar, tests], mock_invoke, mock_run)

        assert error is None
        mock_logs.assert_called_once()
        log_checks = mock_logs.call_args[0][0]
        assert len(log_checks) == 1
        assert log_checks[0].name == "Tests"
        prompt_arg = mock_invoke.call_args[0][0]
        assert "65.0%" in prompt_arg
        assert "test failure logs" in prompt_arg


# ---------------------------------------------------------------------------
# SyncStep -- task fetch
# ---------------------------------------------------------------------------


class TestSyncStepTaskFetch:
    async def test_fetches_task_during_sync(self) -> None:
        from sova.core.steps.sync import SyncStep

        adapter = _mock_adapter()
        adapter.get_task.return_value = Task(id="42", title="Test task")
        ctx = _make_ctx(adapter=adapter)
        assert ctx.task is None

        step = SyncStep()
        with patch("sova.core.steps.sync.git_ops") as mock_ops:
            mock_ops.sync_branch = AsyncMock()
            result = await step.execute(ctx)

        assert result.success
        assert ctx.task is not None
        assert ctx.task.title == "Test task"

    async def test_preserves_existing_task(self) -> None:
        from sova.core.steps.sync import SyncStep

        existing_task = Task(id="42", title="Already set")
        adapter = _mock_adapter()
        ctx = _make_ctx(adapter=adapter, task=existing_task)

        step = SyncStep()
        with patch("sova.core.steps.sync.git_ops") as mock_ops:
            mock_ops.sync_branch = AsyncMock()
            result = await step.execute(ctx)

        assert result.success
        assert ctx.task.title == "Already set"
        adapter.get_task.assert_not_awaited()

    async def test_sync_succeeds_even_if_task_fetch_fails(self) -> None:
        from sova.core.steps.sync import SyncStep

        adapter = _mock_adapter()
        adapter.get_task.side_effect = RuntimeError("GitHub API down")
        ctx = _make_ctx(adapter=adapter)

        step = SyncStep()
        with patch("sova.core.steps.sync.git_ops") as mock_ops:
            mock_ops.sync_branch = AsyncMock()
            result = await step.execute(ctx)

        assert result.success
        assert ctx.task is None


# ---------------------------------------------------------------------------
# CommitStep -- agent artifact filtering
# ---------------------------------------------------------------------------


class TestCommitStepAgentArtifacts:
    async def test_skips_commit_when_only_agent_artifacts_untracked(self) -> None:
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = CommitStep()

        with patch("sova.core.steps.commit.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # diff --stat (clean)
                MagicMock(success=True, stdout=""),  # staged (clean)
                MagicMock(success=True, stdout=".claude/settings.json\n.agent-summary.md\n"),  # only artifacts
                MagicMock(success=True, stdout="abc123 feat: real work\n"),  # has commits
            ]
            result = await step.execute(ctx)

        assert result.success
        assert "already exist" in result.summary

    async def test_commits_meaningful_untracked_files(self) -> None:
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        ctx.task = Task(id="42", title="Add feature")
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # diff --stat
                MagicMock(success=True, stdout=""),  # staged
                MagicMock(success=True, stdout="src/new_file.py\n.claude/tmp.json\n"),  # mixed
                MagicMock(success=True, stdout=""),  # log (no commits)
            ]
            result = await step.execute(ctx)

        assert result.success
        mock_commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# CommitStep -- issueless runs
# ---------------------------------------------------------------------------


class TestCommitStepIssueless:
    async def test_issueless_commit_uses_run_label(self) -> None:
        """Issueless runs should use run_label in commit message, no Closes line."""
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(issue_number="", run_label="sprint-planning", worktree_dir=Path("/tmp/worktree"))
        ctx.task = None
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" plan.py | 3 +++\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
            ]
            result = await step.execute(ctx)

        assert result.success
        msg = mock_commit.call_args[0][0]
        assert "sprint-planning" in msg
        assert "Closes" not in msg

    async def test_issueless_commit_fallback_title(self) -> None:
        """When no task, no issue, and no run_label, commit uses 'run'."""
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(issue_number="", run_label="", worktree_dir=Path("/tmp/worktree"))
        ctx.task = None
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" plan.py | 3 +++\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
            ]
            result = await step.execute(ctx)

        assert result.success
        msg = mock_commit.call_args[0][0]
        assert "feat(core): run" in msg
        assert "Closes" not in msg

    async def test_issueless_address_review_uses_run_label(self) -> None:
        """Address-review without issue uses run_label for commit message."""
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(
            issue_number="",
            run_label="sprint-planning",
            worktree_dir=Path("/tmp/worktree"),
            pr_number=200,
            completed_steps=frozenset({"address_review"}),
        )
        ctx.task = None
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" fix.py | 3 +++\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
            ]
            result = await step.execute(ctx)

        assert result.success
        msg = mock_commit.call_args[0][0]
        assert msg.startswith("fix(core):")
        assert "sprint-planning" in msg

    async def test_commit_failure_returns_error(self) -> None:
        """RuntimeError during git commit should return a failed StepResult."""
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" fix.py | 3 +++\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
            ]
            mock_commit.side_effect = RuntimeError("suspicious file staged")
            result = await step.execute(ctx)

        assert not result.success
        assert "Commit failed" in result.summary


# ---------------------------------------------------------------------------
# CommitStep -- JIRA-aware linking
# ---------------------------------------------------------------------------


class TestCommitStepJira:
    async def test_jira_commit_message_has_ticket_link(self) -> None:
        """JIRA projects should emit JIRA: link instead of Closes #N."""
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(
            worktree_dir=Path("/tmp/worktree"),
            issue_number="48767",
            config=ProjectConfig(
                task_source=TaskSourceConfig(
                    type="jira",
                    jira_project_key="RHCLOUD",
                    jira_base_url="https://issues.redhat.com",
                )
            ),
        )
        ctx.task = Task(id="48767", title="Add security logging")
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" security.py | 5 +++++\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
            ]
            result = await step.execute(ctx)

        assert result.success
        msg = mock_commit.call_args[0][0]
        assert "JIRA: https://issues.redhat.com/browse/RHCLOUD-48767" in msg
        assert "Closes #" not in msg

    async def test_github_commit_message_unchanged(self) -> None:
        """GitHub projects should still produce Closes #N."""
        from sova.core.steps.commit import CommitStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        ctx.task = Task(id="42", title="Fix login bug")
        ctx.config = ProjectConfig(task_source=TaskSourceConfig(type="github"))
        step = CommitStep()

        with (
            patch("sova.core.steps.commit.run") as mock_run,
            patch("sova.core.steps.commit.git_ops.commit", new_callable=AsyncMock) as mock_commit,
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" login.py | 3 +++\n"),
                MagicMock(success=True, stdout=""),
                MagicMock(success=True, stdout=""),
            ]
            result = await step.execute(ctx)

        assert result.success
        msg = mock_commit.call_args[0][0]
        assert "Closes #42" in msg
        assert "JIRA:" not in msg


# ---------------------------------------------------------------------------
# WorktreeStep -- JIRA-aware branch naming
# ---------------------------------------------------------------------------


class TestWorktreeStepJira:
    async def test_jira_branch_name_uses_project_key(self) -> None:
        """JIRA projects should produce feat/PROJKEY-N-slug branch names."""
        from sova.core.steps.create_worktree import WorktreeStep

        ctx = _make_ctx(issue_number="48767")
        ctx.task = Task(id="48767", title="Add security logging")
        ctx.config = ProjectConfig(
            task_source=TaskSourceConfig(
                type="jira",
                jira_project_key="RHCLOUD",
                jira_base_url="https://issues.redhat.com",
            )
        )
        step = WorktreeStep()

        with patch("sova.core.steps.create_worktree.worktree.create_worktree", new_callable=AsyncMock) as mock_wt:
            mock_wt.return_value = MagicMock(path=Path("/tmp/wt"))
            await step.execute(ctx)

        assert ctx.branch_name == "feat/RHCLOUD-48767-Add-security-logging"

    async def test_jira_branch_name_without_task_title(self) -> None:
        """JIRA branch name without task title should use bare JIRA key."""
        from sova.core.steps.create_worktree import WorktreeStep

        ctx = _make_ctx(issue_number="48767")
        ctx.task = None
        ctx.config = ProjectConfig(
            task_source=TaskSourceConfig(
                type="jira",
                jira_project_key="RHCLOUD",
                jira_base_url="https://issues.redhat.com",
            )
        )
        step = WorktreeStep()

        with patch("sova.core.steps.create_worktree.worktree.create_worktree", new_callable=AsyncMock) as mock_wt:
            mock_wt.return_value = MagicMock(path=Path("/tmp/wt"))
            await step.execute(ctx)

        assert ctx.branch_name == "feat/RHCLOUD-48767"

    async def test_github_branch_name_unchanged(self) -> None:
        """GitHub projects should still produce feat/issue-N branch names."""
        from sova.core.steps.create_worktree import WorktreeStep

        ctx = _make_ctx(issue_number="42")
        ctx.config = ProjectConfig(task_source=TaskSourceConfig(type="github"))
        step = WorktreeStep()

        with patch("sova.core.steps.create_worktree.worktree.create_worktree", new_callable=AsyncMock) as mock_wt:
            mock_wt.return_value = MagicMock(path=Path("/tmp/wt"))
            await step.execute(ctx)

        assert ctx.branch_name == "feat/issue-42"


# ---------------------------------------------------------------------------
# CreatePRStep -- branch name to title fallback
# ---------------------------------------------------------------------------


class TestCreatePRTitleFromBranch:
    def test_bare_issue_branch_returns_update_fallback(self) -> None:
        from sova.core.steps.create_pr import _title_from_branch

        assert _title_from_branch("feat/issue-48767") == "update"

    def test_strips_jira_key_prefix(self) -> None:
        from sova.core.steps.create_pr import _title_from_branch

        result = _title_from_branch("feat/RHCLOUD-48767-security-logging")
        assert result == "security logging"

    def test_strips_fix_prefix(self) -> None:
        from sova.core.steps.create_pr import _title_from_branch

        assert _title_from_branch("fix/issue-42-login-bug") == "login bug"

    def test_bare_branch_returned_unchanged(self) -> None:
        from sova.core.steps.create_pr import _title_from_branch

        assert _title_from_branch("main") == "main"

    def test_prefix_only_returns_update_fallback(self) -> None:
        from sova.core.steps.create_pr import _title_from_branch

        assert _title_from_branch("feat/") == "update"

    def test_empty_branch_returns_update_fallback(self) -> None:
        from sova.core.steps.create_pr import _title_from_branch

        assert _title_from_branch("") == "update"


# ---------------------------------------------------------------------------
# CreatePRStep -- task title fallback via adapter (#438)
# ---------------------------------------------------------------------------


class TestCreatePRStepTitleFallback:
    """When ctx.task is None but an issue number exists, CreatePRStep should
    fetch the issue title from the adapter instead of using 'update'."""

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_fetches_title_from_adapter_when_task_is_none(
        self, mock_create_pr, mock_run, mock_invoke, _find
    ) -> None:
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.return_value = MagicMock(success=True, stdout="abc feat\n")
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        adapter = _mock_adapter()
        adapter.get_task.return_value = Task(id="375", title="concurrent branch file-overlap prevention gate")
        ctx = _make_ctx(adapter=adapter, branch_name="feat/issue-375", task=None)

        step = CreatePRStep()
        await step.execute(ctx)
        adapter.get_task.assert_awaited_once_with(ctx.issue_number)

        title_arg = mock_create_pr.call_args.kwargs["title"]
        assert "concurrent branch file-overlap prevention gate" in title_arg
        assert "update" not in title_arg
        assert ctx.task is not None, "adapter fetch should populate ctx.task"

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_falls_back_to_branch_name_when_adapter_fails(
        self, mock_create_pr, mock_run, mock_invoke, _find
    ) -> None:
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.return_value = MagicMock(success=True, stdout="abc feat\n")
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        adapter = _mock_adapter()
        adapter.get_task.side_effect = RuntimeError("API error")
        ctx = _make_ctx(adapter=adapter, branch_name="feat/issue-375", task=None)

        step = CreatePRStep()
        await step.execute(ctx)
        adapter.get_task.assert_awaited_once_with(ctx.issue_number)

        title_arg = mock_create_pr.call_args.kwargs["title"]
        assert "update" in title_arg

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_uses_task_title_directly_when_available(self, mock_create_pr, mock_run, mock_invoke, _find) -> None:
        """When ctx.task is populated, no adapter call is needed."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.return_value = MagicMock(success=True, stdout="abc feat\n")
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        adapter = _mock_adapter()
        ctx = _make_ctx(
            adapter=adapter,
            branch_name="feat/issue-42",
            task=Task(id="42", title="Add widget support"),
        )

        step = CreatePRStep()
        await step.execute(ctx)

        adapter.get_task.assert_not_called()
        title_arg = mock_create_pr.call_args.kwargs["title"]
        assert "Add widget support" in title_arg

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_no_adapter_call_for_issueless_run(self, mock_create_pr, mock_run, mock_invoke, _find) -> None:
        """Issueless runs should use branch name, not try the adapter."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.return_value = MagicMock(success=True, stdout="abc feat\n")
        mock_invoke.return_value = LLMResult(text="## Summary\n- stuff", model="sonnet", cost_usd=Decimal("0.01"))
        mock_create_pr.return_value = MagicMock(number=10, url="https://github.com/x/y/pull/10")

        adapter = _mock_adapter()
        ctx = _make_ctx(adapter=adapter, issue_number="", branch_name="feat/sprint-plan", task=None)

        step = CreatePRStep()
        await step.execute(ctx)

        adapter.get_task.assert_not_called()
        title_arg = mock_create_pr.call_args.kwargs["title"]
        assert "sprint plan" in title_arg


# ---------------------------------------------------------------------------
# CreatePRStep -- issueless runs
# ---------------------------------------------------------------------------


class TestCreatePRStepIssueless:
    async def test_issueless_pr_title_has_no_issue_ref(self) -> None:
        """Issueless PRs should not include '#(none)' in the title."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(
            issue_number="",
            run_label="sprint-planning",
            branch_name="feat/sprint-plan",
            worktree_dir=Path("/tmp/worktree"),
        )
        ctx.task = None
        step = CreatePRStep()

        pr_info = MagicMock(number=99, url="https://github.com/test/repo/pull/99")

        with (
            patch("sova.core.steps.create_pr.run") as mock_run,
            patch("sova.core.steps.create_pr.invoke", new_callable=AsyncMock) as mock_invoke,
            patch("sova.core.steps.create_pr.git_ops.create_pr", new_callable=AsyncMock, return_value=pr_info),
        ):
            mock_run.side_effect = [
                MagicMock(success=True, stdout="abc123 feat: plan\n"),
                MagicMock(success=True, stdout=" plan.py | 3 +++\n"),
                MagicMock(success=True, stdout="diff --git a/plan.py\n+planning\n"),
            ]
            mock_invoke.return_value = LLMResult(
                text="## Summary\n- Sprint planning", model="sonnet", cost_usd=Decimal("0.01")
            )
            result = await step.execute(ctx)

        assert result.success
        assert ctx.pr_number == 99
        assert "(#" not in result.summary or "99" in result.summary

    async def test_issueless_pr_body_has_no_closes(self) -> None:
        """PR body for issueless runs should omit Closes line."""
        from sova.core.steps.create_pr import CreatePRStep

        ctx = _make_ctx(issue_number="", run_label="sprint-planning")
        ctx.task = None
        body = CreatePRStep._build_fallback_body(ctx, "sprint plan", "abc123 feat: plan", "plan.py | 3 +++")
        assert "Closes" not in body
        assert "sprint plan" in body

    async def test_pr_body_includes_issue_excerpt(self) -> None:
        """PR body should include a truncated issue body excerpt."""
        from sova.core.steps.create_pr import CreatePRStep

        ctx = _make_ctx(branch_name="feat/issue-42", task=Task(id="42", title="Widget", body="Add widget support"))
        body = CreatePRStep._build_fallback_body(ctx, "Widget", "abc feat", "x.py | 3 +++")
        assert "## Context" in body
        assert "Add widget support" in body
        assert "Closes #42" in body

    async def test_pr_body_truncates_long_issue_body(self) -> None:
        """Issue body excerpts longer than 500 chars should be truncated."""
        from sova.core.steps.create_pr import CreatePRStep

        long_body = "x" * 600
        ctx = _make_ctx(branch_name="feat/issue-42", task=Task(id="42", title="Big", body=long_body))
        body = CreatePRStep._build_fallback_body(ctx, "Big", "abc feat", "x.py | 3 +++")
        assert "## Context" in body
        assert "..." in body
        assert long_body not in body  # should be truncated

    async def test_pr_body_omits_context_when_no_issue_body(self) -> None:
        """PR body should not include Context section when issue has no body."""
        from sova.core.steps.create_pr import CreatePRStep

        ctx = _make_ctx(branch_name="feat/issue-42", task=Task(id="42", title="Fix", body=""))
        body = CreatePRStep._build_fallback_body(ctx, "Fix", "abc feat", "x.py | 3 +++")
        assert "## Context" not in body
        assert "Closes #42" in body

    async def test_pr_body_omits_context_when_whitespace_only_body(self) -> None:
        """PR body should not include Context section when issue body is whitespace-only."""
        from sova.core.steps.create_pr import CreatePRStep

        ctx = _make_ctx(branch_name="feat/issue-42", task=Task(id="42", title="Fix", body="   "))
        body = CreatePRStep._build_fallback_body(ctx, "Fix", "abc feat", "x.py | 3 +++")
        assert "## Context" not in body
        assert "Closes #42" in body


# ---------------------------------------------------------------------------
# RebaseStep
# ---------------------------------------------------------------------------


class TestRebaseStep:
    async def test_clean_rebase(self) -> None:
        from sova.core.steps.rebase import RebaseStep

        ctx = _make_ctx(branch_name="feat/test", worktree_dir=Path("/tmp/worktree"))
        step = RebaseStep()

        with patch("sova.core.steps.rebase.rebase_with_conflict_resolution", new_callable=AsyncMock) as mock_rebase:
            mock_rebase.return_value = (
                MagicMock(success=True, conflicts_resolved=0),
                Decimal("0"),
            )
            result = await step.execute(ctx)

        assert result.success
        assert "Rebased onto" in result.summary

    async def test_rebase_with_conflicts_resolved(self) -> None:
        from sova.core.steps.rebase import RebaseStep

        ctx = _make_ctx(branch_name="feat/test", worktree_dir=Path("/tmp/worktree"))
        step = RebaseStep()

        with patch("sova.core.steps.rebase.rebase_with_conflict_resolution", new_callable=AsyncMock) as mock_rebase:
            mock_rebase.return_value = (
                MagicMock(success=True, conflicts_resolved=2),
                Decimal("0.05"),
            )
            result = await step.execute(ctx)

        assert result.success
        assert "2 conflicts resolved" in result.summary

    async def test_rebase_failure(self) -> None:
        from sova.core.steps.rebase import RebaseStep

        ctx = _make_ctx(branch_name="feat/test", worktree_dir=Path("/tmp/worktree"))
        step = RebaseStep()

        with patch("sova.core.steps.rebase.rebase_with_conflict_resolution", new_callable=AsyncMock) as mock_rebase:
            mock_rebase.return_value = (
                MagicMock(success=False, conflicts_resolved=0, error="Unresolved conflicts"),
                Decimal("0.03"),
            )
            result = await step.execute(ctx)

        assert not result.success
        assert "Unresolved conflicts" in result.error

    async def test_gate_check_detects_rebase_in_progress(self, tmp_path: Path) -> None:
        from sova.core.steps.rebase import RebaseStep

        ctx = _make_ctx(worktree_dir=tmp_path)
        step = RebaseStep()

        # Simulate rebase-merge marker in the git directory
        git_dir = tmp_path / ".git"
        git_dir.mkdir(exist_ok=True)
        (git_dir / "rebase-merge").mkdir()

        with patch("sova.core.steps.rebase.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout=str(git_dir))
            gate = await step.validate_output(ctx)

        assert not gate.passed

    async def test_gate_check_passes_when_clean(self, tmp_path: Path) -> None:
        from sova.core.steps.rebase import RebaseStep

        ctx = _make_ctx(worktree_dir=tmp_path)
        step = RebaseStep()

        git_dir = tmp_path / ".git"
        git_dir.mkdir(exist_ok=True)

        with patch("sova.core.steps.rebase.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout=str(git_dir))
            gate = await step.validate_output(ctx)

        assert gate.passed

    async def test_gate_check_fails_when_git_dir_unknown(self) -> None:
        from sova.core.steps.rebase import RebaseStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = RebaseStep()

        with patch("sova.core.steps.rebase.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=False, stdout="", stderr="not a git repo")
            gate = await step.validate_output(ctx)

        assert not gate.passed
        assert "Cannot determine git directory" in gate.reason

    async def test_can_skip_when_completed(self) -> None:
        from sova.core.steps.rebase import RebaseStep

        ctx = _make_ctx(completed_steps=frozenset({"rebase"}))
        step = RebaseStep()
        assert await step.can_skip(ctx)


# ---------------------------------------------------------------------------
# RebaseWithConflictResolution (git operations)
# ---------------------------------------------------------------------------


class TestRebaseWithConflictResolution:
    async def test_clean_rebase_no_conflicts(self) -> None:
        from sova.git.operations import rebase_with_conflict_resolution

        with patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True),  # fetch
                MagicMock(success=True, stdout="No local changes to save"),  # stash (nothing to stash)
                MagicMock(success=True, stdout=""),  # rebase (clean)
            ]
            result, cost = await rebase_with_conflict_resolution("main", cwd=Path("/tmp"))

        assert result.success
        assert cost == Decimal("0")

    async def test_clean_rebase_stashes_and_restores_dirty_worktree(self) -> None:
        """Unstaged changes are stashed before rebasing and restored after."""
        from sova.git.operations import rebase_with_conflict_resolution

        with patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True),  # fetch
                MagicMock(success=True, stdout="Saved working directory"),  # stash (changes stashed)
                MagicMock(success=True, stdout=""),  # rebase (clean)
                MagicMock(success=True),  # stash pop
            ]
            result, cost = await rebase_with_conflict_resolution("main", cwd=Path("/tmp"))

        assert result.success
        assert cost == Decimal("0")
        assert mock_run.call_args_list[1][0][:3] == ("git", "stash", "--include-untracked")
        assert mock_run.call_args_list[3][0][:3] == ("git", "stash", "pop")

    async def test_unstaged_changes_no_conflict_surfaces_real_error(self) -> None:
        """When rebase fails for a non-conflict reason, the real error is reported."""
        from sova.git.operations import rebase_with_conflict_resolution

        with patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True),  # fetch
                MagicMock(success=True, stdout="No local changes to save"),  # stash
                MagicMock(success=False, stderr="fatal: cannot rebase: index mismatch"),  # rebase fails
                MagicMock(success=True, stdout=""),  # conflicted files (none)
                MagicMock(success=True),  # rebase --abort
            ]
            result, cost = await rebase_with_conflict_resolution("main", cwd=Path("/tmp"))

        assert not result.success
        assert "Rebase failed" in result.error
        assert "index mismatch" in result.error

    async def test_conflict_resolved_by_llm(self) -> None:
        from sova.git.operations import rebase_with_conflict_resolution
        from sova.llm.models import LLMResult

        with (
            patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.rebase.invoke_command", new_callable=AsyncMock) as mock_llm,
        ):
            mock_run.side_effect = [
                MagicMock(success=True),  # fetch
                MagicMock(success=True, stdout="No local changes to save"),  # stash
                MagicMock(success=False, stderr="CONFLICT"),  # rebase fails
                MagicMock(success=True, stdout="file.py\n"),  # conflicted files (initial check)
                MagicMock(success=True, stdout=""),  # no remaining conflicts (after LLM)
                MagicMock(success=True),  # rebase --continue
            ]
            mock_llm.return_value = LLMResult(text="resolved", model="sonnet", cost_usd=Decimal("0.02"))

            result, cost = await rebase_with_conflict_resolution("main", cwd=Path("/tmp"))

        assert result.success
        assert result.conflicts_resolved == 1
        assert cost == Decimal("0.02")

    async def test_conflict_resolution_fails_aborts(self) -> None:
        from sova.git.operations import rebase_with_conflict_resolution

        with (
            patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.rebase.invoke_command", new_callable=AsyncMock) as mock_llm,
        ):
            mock_run.side_effect = [
                MagicMock(success=True),  # fetch
                MagicMock(success=True, stdout="No local changes to save"),  # stash
                MagicMock(success=False, stderr="CONFLICT"),  # rebase fails
                MagicMock(success=True, stdout="file.py\n"),  # conflicted files (initial check)
                MagicMock(success=True),  # rebase --abort
            ]
            mock_llm.side_effect = RuntimeError("LLM failed")

            result, cost = await rebase_with_conflict_resolution("main", cwd=Path("/tmp"))

        assert not result.success
        assert "LLM failed" in result.error

    async def test_fetch_failure_returns_error(self) -> None:
        from sova.git.operations import rebase_with_conflict_resolution

        with patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=False, stderr="Could not resolve host")
            result, cost = await rebase_with_conflict_resolution("main", cwd=Path("/tmp"))

        assert not result.success
        assert "Fetch failed" in result.error


# ---------------------------------------------------------------------------
# SimplifyStep -- execute and validate_output
# ---------------------------------------------------------------------------


class TestSimplifyStep:
    async def test_execute_calls_llm(self) -> None:
        from sova.core.steps.simplify import SimplifyStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = SimplifyStep()

        with patch("sova.core.steps.simplify.invoke_command", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.return_value = LLMResult(
                text="Simplified",
                model="sonnet",
                cost_usd=Decimal("0.03"),
            )
            result = await step.execute(ctx)

        assert result.success
        assert "Simplification pass completed" in result.summary
        assert ctx.cost_usd == Decimal("0.03")
        mock_invoke.assert_awaited_once()
        assert mock_invoke.call_args[0][0] == "/simplify"

    async def test_execute_handles_runtime_error(self) -> None:
        from sova.core.steps.simplify import SimplifyStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = SimplifyStep()

        with patch("sova.core.steps.simplify.invoke_command", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.side_effect = RuntimeError("Claude CLI unavailable")
            result = await step.execute(ctx)

        assert not result.success
        assert "Claude CLI unavailable" in result.error

    async def test_validate_output_passes_with_unstaged_changes(self) -> None:
        from sova.core.steps.simplify import SimplifyStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = SimplifyStep()

        with patch("sova.core.steps.simplify.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" src/app.py | 5 +++++\n"),  # unstaged
                MagicMock(success=True, stdout=""),  # staged
                MagicMock(success=True, stdout=""),  # log
                MagicMock(success=True, stdout=""),  # status --porcelain
            ]
            gate = await step.validate_output(ctx)

        assert gate.passed

    async def test_validate_output_passes_with_staged_changes(self) -> None:
        from sova.core.steps.simplify import SimplifyStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = SimplifyStep()

        with patch("sova.core.steps.simplify.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # unstaged
                MagicMock(success=True, stdout=" src/app.py | 3 +++\n"),  # staged
                MagicMock(success=True, stdout=""),  # log
                MagicMock(success=True, stdout=""),  # status --porcelain
            ]
            gate = await step.validate_output(ctx)

        assert gate.passed

    async def test_validate_output_passes_with_commits_ahead(self) -> None:
        from sova.core.steps.simplify import SimplifyStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = SimplifyStep()

        with patch("sova.core.steps.simplify.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # unstaged
                MagicMock(success=True, stdout=""),  # staged
                MagicMock(success=True, stdout="abc123 feat: something\n"),  # log
                MagicMock(success=True, stdout=""),  # status --porcelain
            ]
            gate = await step.validate_output(ctx)

        assert gate.passed

    async def test_validate_output_passes_with_only_untracked_files(self) -> None:
        """Gate must pass when Claude wrote new files but never staged them."""
        from sova.core.steps.simplify import SimplifyStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = SimplifyStep()

        with patch("sova.core.steps.simplify.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # git diff --stat HEAD (no tracked changes)
                MagicMock(success=True, stdout=""),  # git diff --cached --stat (nothing staged)
                MagicMock(success=True, stdout=""),  # git log base..HEAD (no commits)
                MagicMock(success=True, stdout="?? new_module.py\n?? tests/test_new_module.py\n"),  # untracked
            ]
            gate = await step.validate_output(ctx)

        assert gate.passed

    async def test_validate_output_fails_when_all_reverted(self) -> None:
        from sova.core.steps.simplify import SimplifyStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = SimplifyStep()

        with patch("sova.core.steps.simplify.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # unstaged
                MagicMock(success=True, stdout=""),  # staged
                MagicMock(success=True, stdout=""),  # log
                MagicMock(success=True, stdout=""),  # status --porcelain
            ]
            gate = await step.validate_output(ctx)

        assert not gate.passed
        assert "reverted" in gate.reason.lower()

    async def test_validate_output_status_failure_falls_back_to_other_checks(self) -> None:
        """When git status --porcelain fails, has_untracked is False; gate still passes via other checks."""
        from sova.core.steps.simplify import SimplifyStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = SimplifyStep()

        with patch("sova.core.steps.simplify.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=" src/app.py | 2 +-\n"),  # unstaged diff present
                MagicMock(success=True, stdout=""),  # staged
                MagicMock(success=True, stdout=""),  # log
                MagicMock(success=False, stdout=""),  # status --porcelain fails
            ]
            gate = await step.validate_output(ctx)

        assert gate.passed


# ---------------------------------------------------------------------------
# SelfReviewStep -- execute and validate_output
# ---------------------------------------------------------------------------


class TestSelfReviewStep:
    async def test_execute_calls_review_command(self) -> None:
        from sova.core.steps.self_review import SelfReviewStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = SelfReviewStep()

        with patch("sova.core.steps.self_review.invoke_command", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.return_value = LLMResult(
                text="Reviewed",
                model="sonnet",
                cost_usd=Decimal("0.02"),
            )
            result = await step.execute(ctx)

        assert result.success
        assert "Self-review completed" in result.summary
        assert ctx.cost_usd == Decimal("0.02")
        mock_invoke.assert_awaited_once()
        assert mock_invoke.call_args[0][0] == "/review"

    async def test_execute_handles_runtime_error(self) -> None:
        from sova.core.steps.self_review import SelfReviewStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = SelfReviewStep()

        with patch("sova.core.steps.self_review.invoke_command", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.side_effect = RuntimeError("LLM failed")
            result = await step.execute(ctx)

        assert not result.success
        assert "LLM failed" in result.error

    async def test_validate_output_checks_untracked_files(self) -> None:
        from sova.core.steps.self_review import SelfReviewStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = SelfReviewStep()

        with patch("sova.utils.shell.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # log
                MagicMock(success=True, stdout=""),  # unstaged
                MagicMock(success=True, stdout=""),  # staged
                MagicMock(success=True, stdout="new_file.py\n"),  # untracked
            ]
            gate = await step.validate_output(ctx)

        assert gate.passed

    async def test_validate_output_fails_when_all_lost(self) -> None:
        from sova.core.steps.self_review import SelfReviewStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = SelfReviewStep()

        with patch("sova.utils.shell.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # log
                MagicMock(success=True, stdout=""),  # unstaged
                MagicMock(success=True, stdout=""),  # staged
                MagicMock(success=True, stdout=""),  # untracked
            ]
            gate = await step.validate_output(ctx)

        assert not gate.passed
        assert "lost" in gate.reason.lower()

    async def test_can_skip_when_review_disabled(self) -> None:
        from sova.core.steps.self_review import SelfReviewStep

        config = ProjectConfig()
        config.review.enabled = False
        ctx = _make_ctx(config=config)
        step = SelfReviewStep()
        assert await step.can_skip(ctx)

    async def test_can_skip_when_completed(self) -> None:
        from sova.core.steps.self_review import SelfReviewStep

        ctx = _make_ctx(completed_steps=frozenset({"self_review"}))
        step = SelfReviewStep()
        assert await step.can_skip(ctx)


# ---------------------------------------------------------------------------
# DevelopStep -- execute path
# ---------------------------------------------------------------------------


class TestDevelopStepExecute:
    async def test_execute_calls_develop_command(self) -> None:
        from sova.core.steps.develop import DevelopStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with patch("sova.core.steps.develop.invoke_command", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.return_value = LLMResult(
                text="Developed",
                model="opus",
                cost_usd=Decimal("1.50"),
                input_tokens=5000,
                output_tokens=3000,
                session_id="sess-123",
            )
            result = await step.execute(ctx)

        assert result.success
        assert "8000 tokens" in result.summary
        assert ctx.cost_usd == Decimal("1.50")
        assert ctx.session_id == "sess-123"
        mock_invoke.assert_awaited_once()
        assert mock_invoke.call_args.kwargs["args"] == "42"

    async def test_execute_handles_runtime_error(self) -> None:
        from sova.core.steps.develop import DevelopStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with patch("sova.core.steps.develop.invoke_command", new_callable=AsyncMock) as mock_invoke:
            mock_invoke.side_effect = RuntimeError("Timeout")
            result = await step.execute(ctx)

        assert not result.success
        assert "Timeout" in result.error

    async def test_execute_fails_when_checks_exhausted(self) -> None:
        """execute() must return success=False when fix cycles are exhausted (#532)."""
        from sova.core.steps.develop import DevelopStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with (
            patch("sova.core.steps.develop.invoke_command", new_callable=AsyncMock) as mock_invoke,
            patch.object(step, "_run_inner_check_loop", new_callable=AsyncMock) as mock_loop,
            patch("sova.core.steps.develop._append_implementation_notes", new_callable=AsyncMock),
        ):
            mock_invoke.return_value = LLMResult(
                text="Developed",
                model="opus",
                cost_usd=Decimal("1.50"),
                input_tokens=5000,
                output_tokens=3000,
                session_id="sess-123",
            )
            mock_loop.return_value = (False, "checks still failing after 3 fix cycle(s)")
            result = await step.execute(ctx)

        assert not result.success
        assert "checks still failing" in result.summary

    async def test_execute_fails_when_checks_exhausted_budget(self) -> None:
        """execute() must return success=False when fix cycles hit budget limit."""
        from sova.core.steps.develop import DevelopStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with (
            patch("sova.core.steps.develop.invoke_command", new_callable=AsyncMock) as mock_invoke,
            patch.object(step, "_run_inner_check_loop", new_callable=AsyncMock) as mock_loop,
            patch("sova.core.steps.develop._append_implementation_notes", new_callable=AsyncMock),
        ):
            mock_invoke.return_value = LLMResult(
                text="Developed",
                model="opus",
                cost_usd=Decimal("1.50"),
                input_tokens=5000,
                output_tokens=3000,
                session_id="sess-123",
            )
            mock_loop.return_value = (False, "checks still failing after 2 fix cycle(s) (budget exceeded)")
            result = await step.execute(ctx)

        assert not result.success
        assert "budget exceeded" in result.summary

    async def test_execute_fails_when_no_changes_produced(self) -> None:
        """execute() must return success=False when fix cycles produce no changes."""
        from sova.core.steps.develop import DevelopStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with (
            patch("sova.core.steps.develop.invoke_command", new_callable=AsyncMock) as mock_invoke,
            patch.object(step, "_run_inner_check_loop", new_callable=AsyncMock) as mock_loop,
            patch("sova.core.steps.develop._append_implementation_notes", new_callable=AsyncMock),
        ):
            mock_invoke.return_value = LLMResult(
                text="Developed",
                model="opus",
                cost_usd=Decimal("1.50"),
                input_tokens=5000,
                output_tokens=3000,
                session_id="sess-123",
            )
            mock_loop.return_value = (False, "checks still failing after 3 fix cycle(s) (no changes produced)")
            result = await step.execute(ctx)

        assert not result.success
        assert "no changes produced" in result.summary

    async def test_execute_succeeds_when_checks_pass(self) -> None:
        """execute() must return success=True when checks pass."""
        from sova.core.steps.develop import DevelopStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with (
            patch("sova.core.steps.develop.invoke_command", new_callable=AsyncMock) as mock_invoke,
            patch.object(step, "_run_inner_check_loop", new_callable=AsyncMock) as mock_loop,
            patch("sova.core.steps.develop._append_implementation_notes", new_callable=AsyncMock),
        ):
            mock_invoke.return_value = LLMResult(
                text="Developed",
                model="opus",
                cost_usd=Decimal("1.50"),
                input_tokens=5000,
                output_tokens=3000,
                session_id="sess-123",
            )
            mock_loop.return_value = (True, "checks passed after 1 fix cycle(s)")
            result = await step.execute(ctx)

        assert result.success
        assert "checks passed" in result.summary

    async def test_execute_succeeds_when_no_check_cmd(self) -> None:
        """execute() must return success=True when no check command is configured."""
        from sova.core.steps.develop import DevelopStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with (
            patch("sova.core.steps.develop.invoke_command", new_callable=AsyncMock) as mock_invoke,
            patch.object(step, "_run_inner_check_loop", new_callable=AsyncMock) as mock_loop,
            patch("sova.core.steps.develop._append_implementation_notes", new_callable=AsyncMock),
        ):
            mock_invoke.return_value = LLMResult(
                text="Developed",
                model="opus",
                cost_usd=Decimal("1.50"),
                input_tokens=5000,
                output_tokens=3000,
                session_id="sess-123",
            )
            mock_loop.return_value = (True, "")  # no check cmd configured
            result = await step.execute(ctx)

        assert result.success

    async def test_execute_fails_when_fix_llm_fails(self) -> None:
        """execute() must return success=False when the fix LLM call fails."""
        from sova.core.steps.develop import DevelopStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with (
            patch("sova.core.steps.develop.invoke_command", new_callable=AsyncMock) as mock_invoke,
            patch.object(step, "_run_inner_check_loop", new_callable=AsyncMock) as mock_loop,
            patch("sova.core.steps.develop._append_implementation_notes", new_callable=AsyncMock),
        ):
            mock_invoke.return_value = LLMResult(
                text="Developed",
                model="opus",
                cost_usd=Decimal("1.50"),
                input_tokens=5000,
                output_tokens=3000,
                session_id="sess-123",
            )
            mock_loop.return_value = (False, "check fix LLM failed on cycle 1: API timeout")
            result = await step.execute(ctx)

        assert not result.success
        assert "check fix LLM failed" in result.summary
        assert result.error is not None

    def test_max_retries_is_one(self) -> None:
        from sova.core.steps.develop import DevelopStep

        step = DevelopStep()
        assert step.max_retries == 1


# ---------------------------------------------------------------------------
# PushStep -- execute path
# ---------------------------------------------------------------------------


class TestPushStepExecute:
    async def test_execute_calls_git_push(self) -> None:
        from sova.core.steps.push import PushStep

        ctx = _make_ctx(branch_name="feat/test", worktree_dir=Path("/tmp/worktree"))
        step = PushStep()

        with patch("sova.core.steps.push.git_ops.push", new_callable=AsyncMock) as mock_push:
            result = await step.execute(ctx)

        assert result.success
        assert "feat/test" in result.summary
        mock_push.assert_awaited_once_with(
            "feat/test",
            force=False,
            set_upstream=True,
            cwd=Path("/tmp/worktree"),
        )

    async def test_execute_force_with_lease_when_pr_exists(self) -> None:
        from sova.core.steps.push import PushStep

        ctx = _make_ctx(branch_name="feat/test", worktree_dir=Path("/tmp/worktree"), pr_number=42)
        step = PushStep()

        with patch("sova.core.steps.push.git_ops.push", new_callable=AsyncMock) as mock_push:
            result = await step.execute(ctx)

        assert result.success
        mock_push.assert_awaited_once_with(
            "feat/test",
            force=True,
            set_upstream=True,
            cwd=Path("/tmp/worktree"),
        )

    async def test_execute_handles_push_failure(self) -> None:
        from sova.core.steps.push import PushStep

        ctx = _make_ctx(branch_name="feat/test", worktree_dir=Path("/tmp/worktree"))
        step = PushStep()

        with patch("sova.core.steps.push.git_ops.push", new_callable=AsyncMock) as mock_push:
            mock_push.side_effect = RuntimeError("Permission denied")
            result = await step.execute(ctx)

        assert not result.success
        assert "Permission denied" in result.error

    async def test_validate_output_passes_with_commits(self) -> None:
        from sova.core.steps.push import PushStep

        ctx = _make_ctx(branch_name="feat/test", worktree_dir=Path("/tmp/worktree"))
        step = PushStep()

        with patch("sova.core.steps.push.run") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="3\n")
            gate = await step.validate_output(ctx)

        assert gate.passed

    async def test_validate_output_fails_on_command_failure(self) -> None:
        from sova.core.steps.push import PushStep

        ctx = _make_ctx(branch_name="feat/test", worktree_dir=Path("/tmp/worktree"))
        step = PushStep()

        with patch("sova.core.steps.push.run") as mock_run:
            mock_run.return_value = MagicMock(success=False, stdout="")
            gate = await step.validate_output(ctx)

        assert not gate.passed
        assert "Failed to count" in gate.reason

    async def test_validate_output_fails_on_bad_output(self) -> None:
        from sova.core.steps.push import PushStep

        ctx = _make_ctx(branch_name="feat/test", worktree_dir=Path("/tmp/worktree"))
        step = PushStep()

        with patch("sova.core.steps.push.run") as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="not-a-number\n")
            gate = await step.validate_output(ctx)

        assert not gate.passed
        assert "Unexpected rev-list" in gate.reason


# ---------------------------------------------------------------------------
# AddressReviewStep -- findings loading and execute
# ---------------------------------------------------------------------------


class TestAddressReviewStepExecute:
    async def test_execute_loads_findings_from_file(self) -> None:
        from sova.core.steps.address_review import AddressReviewStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(pr_number=42, worktree_dir=Path("/tmp/worktree"))
        step = AddressReviewStep()

        findings = [
            {"file": "foo.py", "line": 10, "severity": 7, "category": "bug", "description": "Null check"},
        ]
        mock_handoff = MagicMock()
        mock_handoff.details = {"findings": findings}

        with (
            patch("sova.core.steps.address_review.run", new_callable=AsyncMock) as mock_run,
            patch("sova.core.steps.address_review.read_handoff_file", return_value=mock_handoff),
            patch(
                "sova.core.steps.address_review._load_coderabbit_findings",
                new_callable=AsyncMock,
                return_value=([], []),
            ),
            patch("sova.core.steps.address_review.invoke_command", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.return_value = MagicMock(success=True, stdout="abc123")
            mock_invoke.return_value = LLMResult(
                text="Fixed",
                model="sonnet",
                cost_usd=Decimal("0.05"),
            )
            result = await step.execute(ctx)

        assert result.success
        assert "1 review findings" in result.summary
        assert ctx.cost_usd == Decimal("0.05")
        prompt = mock_invoke.call_args[0][0]
        assert "Null check" in prompt
        assert "foo.py:10" in prompt

    async def test_execute_includes_coderabbit_findings(self) -> None:
        from sova.core.steps.address_review import AddressReviewStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(pr_number=42, worktree_dir=Path("/tmp/worktree"))
        step = AddressReviewStep()

        sova_findings = [
            {"file": "foo.py", "severity": 7, "category": "bug", "description": "SOVA finding"},
        ]
        cr_findings = [
            {
                "file": "bar.py",
                "line": 5,
                "severity": 6,
                "category": "external-review",
                "description": "CodeRabbit finding",
                "suggestion": "",
                "source": "coderabbit",
            },
        ]
        mock_handoff = MagicMock()
        mock_handoff.details = {"findings": sova_findings}

        with (
            patch("sova.core.steps.address_review.run", new_callable=AsyncMock) as mock_run,
            patch("sova.core.steps.address_review.read_handoff_file", return_value=mock_handoff),
            patch(
                "sova.core.steps.address_review._load_coderabbit_findings",
                new_callable=AsyncMock,
                return_value=(cr_findings, ["thread-1"]),
            ),
            patch("sova.core.steps.address_review.invoke_command", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.return_value = MagicMock(success=True, stdout="abc123")
            mock_invoke.return_value = LLMResult(text="Fixed", model="sonnet", cost_usd=Decimal("0.05"))
            result = await step.execute(ctx)

        assert result.success
        assert "2 review findings" in result.summary
        prompt = mock_invoke.call_args[0][0]
        assert "SOVA finding" in prompt
        assert "CodeRabbit finding" in prompt
        assert "coderabbit" in prompt

    async def test_execute_captures_head_before_llm(self) -> None:
        from sova.core.steps.address_review import AddressReviewStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(pr_number=42, worktree_dir=Path("/tmp/worktree"))
        step = AddressReviewStep()

        findings = [{"file": "x.py", "severity": 5, "category": "bug", "description": "Issue"}]
        mock_handoff = MagicMock()
        mock_handoff.details = {"findings": findings}

        with (
            patch("sova.core.steps.address_review.run", new_callable=AsyncMock) as mock_run,
            patch("sova.core.steps.address_review.read_handoff_file", return_value=mock_handoff),
            patch(
                "sova.core.steps.address_review._load_coderabbit_findings",
                new_callable=AsyncMock,
                return_value=([], []),
            ),
            patch("sova.core.steps.address_review.invoke_command", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.return_value = MagicMock(success=True, stdout="abc123\n")
            mock_invoke.return_value = LLMResult(text="Fixed", model="sonnet", cost_usd=Decimal("0.01"))
            await step.execute(ctx)

        assert step._head_before_llm == "abc123"

    async def test_execute_falls_back_to_db(self) -> None:
        from sova.core.steps.address_review import AddressReviewStep
        from sova.llm.models import LLMResult

        ctx = _make_ctx(pr_number=42, worktree_dir=Path("/tmp/worktree"))
        ctx.resume_run_id = 99
        step = AddressReviewStep()

        db_findings = [
            {"file": "bar.py", "severity": 5, "category": "style", "description": "Formatting"},
        ]

        with (
            patch("sova.core.steps.address_review.run", new_callable=AsyncMock) as mock_run,
            patch("sova.core.steps.address_review.read_handoff_file", return_value=None),
            patch("sova.core.steps.address_review.read_handoff", new_callable=AsyncMock) as mock_db,
            patch(
                "sova.core.steps.address_review._load_coderabbit_findings",
                new_callable=AsyncMock,
                return_value=([], []),
            ),
            patch("sova.core.steps.address_review.invoke_command", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.return_value = MagicMock(success=True, stdout="abc123")
            mock_db_handoff = MagicMock()
            mock_db_handoff.pending_findings = db_findings
            mock_db.return_value = mock_db_handoff
            mock_invoke.return_value = LLMResult(
                text="Fixed",
                model="sonnet",
                cost_usd=Decimal("0.04"),
            )
            result = await step.execute(ctx)

        assert result.success
        assert "1 review findings" in result.summary
        prompt = mock_invoke.call_args[0][0]
        assert "Formatting" in prompt

    async def test_execute_handles_llm_failure(self) -> None:
        from sova.core.steps.address_review import AddressReviewStep

        ctx = _make_ctx(pr_number=42, worktree_dir=Path("/tmp/worktree"))
        step = AddressReviewStep()

        findings = [{"file": "x.py", "severity": 5, "category": "bug", "description": "Issue"}]
        mock_handoff = MagicMock()
        mock_handoff.details = {"findings": findings}

        with (
            patch("sova.core.steps.address_review.run", new_callable=AsyncMock) as mock_run,
            patch("sova.core.steps.address_review.read_handoff_file", return_value=mock_handoff),
            patch(
                "sova.core.steps.address_review._load_coderabbit_findings",
                new_callable=AsyncMock,
                return_value=([], []),
            ),
            patch("sova.core.steps.address_review.invoke_command", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.return_value = MagicMock(success=True, stdout="abc123")
            mock_invoke.side_effect = RuntimeError("LLM crashed")
            result = await step.execute(ctx)

        assert not result.success
        assert "LLM crashed" in result.error

    async def test_validate_output_passes_with_staged_changes(self) -> None:
        from sova.core.steps.address_review import AddressReviewStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = AddressReviewStep()

        with patch("sova.core.steps.address_review.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # unstaged diff
                MagicMock(success=True, stdout=" bar.py | 3 +++\n"),  # staged diff
                MagicMock(success=True, stdout="abc123"),  # rev-parse HEAD
            ]
            gate = await step.validate_output(ctx)

        assert gate.passed

    async def test_validate_output_passes_when_head_moved(self) -> None:
        from sova.core.steps.address_review import AddressReviewStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = AddressReviewStep()
        step._head_before_llm = "abc123"

        with patch("sova.core.steps.address_review.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # unstaged diff
                MagicMock(success=True, stdout=""),  # staged diff
                MagicMock(success=True, stdout="def456"),  # rev-parse HEAD (different)
            ]
            gate = await step.validate_output(ctx)

        assert gate.passed

    async def test_validate_output_passes_when_findings_already_fixed(self) -> None:
        """When LLM produces no changes but branch has prior commits, findings are already fixed."""
        from sova.core.steps.address_review import AddressReviewStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = AddressReviewStep()
        step._head_before_llm = "abc123"

        with patch("sova.core.steps.address_review.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # unstaged diff
                MagicMock(success=True, stdout=""),  # staged diff
                MagicMock(success=True, stdout="abc123"),  # rev-parse HEAD (same)
                MagicMock(success=True, stdout="abc123 fix: prior work\n"),  # git log base..HEAD
            ]
            gate = await step.validate_output(ctx)

        assert gate.passed

    async def test_validate_output_fails_with_no_changes_and_no_prior_commits(self) -> None:
        from sova.core.steps.address_review import AddressReviewStep

        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))
        step = AddressReviewStep()
        step._head_before_llm = "abc123"

        with patch("sova.core.steps.address_review.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # unstaged diff
                MagicMock(success=True, stdout=""),  # staged diff
                MagicMock(success=True, stdout="abc123"),  # rev-parse HEAD (same)
                MagicMock(success=True, stdout=""),  # git log base..HEAD (empty)
            ]
            gate = await step.validate_output(ctx)

        assert not gate.passed
        assert "No changes" in gate.reason


# ---------------------------------------------------------------------------
# AddressReviewStep helper functions
# ---------------------------------------------------------------------------


class TestAddressReviewHelpers:
    def test_format_findings_prompt(self) -> None:
        from sova.core.steps.address_review import _format_findings_prompt

        findings = [
            {
                "file": "a.py",
                "line": 5,
                "severity": 7,
                "category": "bug",
                "description": "Null ref",
                "suggestion": "Add check",
            },
            {"file": "b.py", "severity": 3, "category": "style", "description": "Whitespace"},
        ]
        prompt = _format_findings_prompt(findings)
        assert "a.py:5" in prompt
        assert "Null ref" in prompt
        assert "Add check" in prompt
        assert "b.py" in prompt
        assert "Whitespace" in prompt
        assert "1." in prompt
        assert "2." in prompt

    def test_load_review_findings_from_handoff(self) -> None:
        from sova.core.steps.address_review import _load_review_findings

        mock_handoff = MagicMock()
        mock_handoff.details = {"findings": [{"file": "x.py"}]}

        with patch("sova.core.steps.address_review.read_handoff_file", return_value=mock_handoff):
            findings = _load_review_findings(Path("/tmp"))

        assert len(findings) == 1

    def test_load_review_findings_returns_empty_when_no_handoff(self) -> None:
        from sova.core.steps.address_review import _load_review_findings

        with patch("sova.core.steps.address_review.read_handoff_file", return_value=None):
            findings = _load_review_findings(Path("/tmp"))

        assert findings == []

    async def test_load_review_findings_from_db(self) -> None:
        from sova.core.steps.address_review import _load_review_findings_from_db

        mock_handoff = MagicMock()
        mock_handoff.pending_findings = [{"file": "y.py"}]

        with patch("sova.core.steps.address_review.read_handoff", new_callable=AsyncMock, return_value=mock_handoff):
            findings = await _load_review_findings_from_db(99)

        assert len(findings) == 1

    async def test_load_review_findings_from_db_returns_empty_on_none_id(self) -> None:
        from sova.core.steps.address_review import _load_review_findings_from_db

        findings = await _load_review_findings_from_db(None)
        assert findings == []

    async def test_load_review_findings_by_issue(self) -> None:
        from datetime import datetime, timezone

        from sova.core.steps.address_review import _load_review_findings_by_issue
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        session = await get_session()
        async with session.begin():
            session.add(
                TaskRun(
                    issue_number="200",
                    role="reviewer",
                    status="failed",
                    handoff_json={
                        "pending_findings": [
                            {"file": "a.py", "severity": 8, "description": "bug"},
                        ],
                    },
                    started_at=datetime.now(timezone.utc),
                )
            )

        findings = await _load_review_findings_by_issue("200")
        assert len(findings) == 1
        assert findings[0]["file"] == "a.py"

    async def test_execute_falls_back_to_issue_query(self) -> None:
        """When file and resume_run_id both miss, finds reviewer run by issue."""
        from datetime import datetime, timezone

        from sova.core.steps.address_review import AddressReviewStep
        from sova.db.models import TaskRun
        from sova.db.session import get_session
        from sova.llm.models import LLMResult

        session = await get_session()
        async with session.begin():
            session.add(
                TaskRun(
                    issue_number="201",
                    role="reviewer",
                    status="failed",
                    handoff_json={
                        "next_action": "address_review",
                        "pending_findings": [
                            {"file": "z.py", "severity": 6, "category": "style", "description": "Needs fix"},
                        ],
                    },
                    started_at=datetime.now(timezone.utc),
                )
            )

        ctx = _make_ctx(pr_number=42, worktree_dir=Path("/tmp/worktree"))
        ctx.issue_number = "201"
        ctx.resume_run_id = None
        step = AddressReviewStep()

        with (
            patch("sova.core.steps.address_review.run", new_callable=AsyncMock) as mock_run,
            patch("sova.core.steps.address_review.read_handoff_file", return_value=None),
            patch(
                "sova.core.steps.address_review._load_coderabbit_findings",
                new_callable=AsyncMock,
                return_value=([], []),
            ),
            patch("sova.core.steps.address_review.invoke_command", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.return_value = MagicMock(success=True, stdout="abc123")
            mock_invoke.return_value = LLMResult(text="Fixed", model="sonnet", cost_usd=Decimal("0.03"))
            result = await step.execute(ctx)

        assert result.success
        assert "1 review findings" in result.summary
        prompt = mock_invoke.call_args[0][0]
        assert "Needs fix" in prompt


# ---------------------------------------------------------------------------
# ResolveExternalReviewsStep
# ---------------------------------------------------------------------------


class TestResolveExternalReviewsStep:
    async def test_skips_when_no_pr(self) -> None:
        from sova.core.steps.resolve_external_reviews import ResolveExternalReviewsStep

        ctx = _make_ctx()
        ctx.pr_number = None
        step = ResolveExternalReviewsStep()

        result = await step.execute(ctx)
        assert result.success
        assert "No PR" in result.summary

    async def test_resolves_threads_and_dismisses_reviews(self) -> None:
        from sova.adapters.external_reviews import _ThreadsResult
        from sova.core.steps.resolve_external_reviews import ResolveExternalReviewsStep

        ctx = _make_ctx(pr_number=42)
        step = ResolveExternalReviewsStep()

        cr_result = _ThreadsResult(thread_ids=["thread-1", "thread-2"])
        with (
            patch(
                "sova.adapters.external_reviews._fetch_coderabbit_threads",
                new_callable=AsyncMock,
                return_value=cr_result,
            ),
            patch(
                "sova.adapters.external_reviews.resolve_coderabbit_threads",
                new_callable=AsyncMock,
                return_value=2,
            ) as mock_resolve,
            patch(
                "sova.core.steps.resolve_external_reviews._dismiss_bot_reviews",
                new_callable=AsyncMock,
                return_value=1,
            ),
            patch(
                "sova.core.steps.resolve_external_reviews.get_active_gh_user",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await step.execute(ctx)

        assert result.success
        assert "2 threads resolved" in result.summary
        assert "1 bot reviews dismissed" in result.summary
        mock_resolve.assert_awaited_once_with(["thread-1", "thread-2"], github_user="")

    async def test_includes_github_user_in_authors(self) -> None:
        """SOVA reviewer threads (posted under github_user) should also be resolved."""
        from sova.adapters.external_reviews import _ThreadsResult
        from sova.config.models import ProjectConfig
        from sova.core.steps.resolve_external_reviews import ResolveExternalReviewsStep

        config = ProjectConfig(github_user="xsovad06", github_repo="user/repo")
        ctx = _make_ctx(pr_number=42, config=config)
        step = ResolveExternalReviewsStep()

        cr_result = _ThreadsResult(thread_ids=["thread-sova"])
        with (
            patch(
                "sova.adapters.external_reviews._fetch_coderabbit_threads",
                new_callable=AsyncMock,
                return_value=cr_result,
            ) as mock_fetch,
            patch(
                "sova.adapters.external_reviews.resolve_coderabbit_threads",
                new_callable=AsyncMock,
                return_value=1,
            ),
            patch(
                "sova.core.steps.resolve_external_reviews._dismiss_bot_reviews",
                new_callable=AsyncMock,
                return_value=0,
            ),
            patch(
                "sova.core.steps.resolve_external_reviews.get_active_gh_user",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await step.execute(ctx)

        assert result.success
        assert "1 threads resolved" in result.summary
        call_kwargs = mock_fetch.call_args[1]
        assert "xsovad06" in call_kwargs["authors"]
        assert "coderabbitai" in call_kwargs["authors"]

    async def test_includes_active_gh_user_in_authors(self) -> None:
        """When active gh user differs from config, both should be in the authors filter."""
        from sova.adapters.external_reviews import _ThreadsResult
        from sova.config.models import ProjectConfig
        from sova.core.steps.resolve_external_reviews import ResolveExternalReviewsStep

        config = ProjectConfig(github_user="xsovad06", github_repo="user/repo")
        ctx = _make_ctx(pr_number=42, config=config)
        step = ResolveExternalReviewsStep()

        cr_result = _ThreadsResult(thread_ids=["thread-sova"])
        with (
            patch(
                "sova.adapters.external_reviews._fetch_coderabbit_threads",
                new_callable=AsyncMock,
                return_value=cr_result,
            ) as mock_fetch,
            patch(
                "sova.adapters.external_reviews.resolve_coderabbit_threads",
                new_callable=AsyncMock,
                return_value=1,
            ),
            patch(
                "sova.core.steps.resolve_external_reviews._dismiss_bot_reviews",
                new_callable=AsyncMock,
                return_value=0,
            ),
            patch(
                "sova.core.steps.resolve_external_reviews.get_active_gh_user",
                new_callable=AsyncMock,
                return_value="dsova06",
            ),
        ):
            result = await step.execute(ctx)

        assert result.success
        call_kwargs = mock_fetch.call_args[1]
        assert "xsovad06" in call_kwargs["authors"], "configured github_user must be included"
        assert "dsova06" in call_kwargs["authors"], "active gh auth user must be included"
        assert "coderabbitai" in call_kwargs["authors"], "default bot authors must be included"

    async def test_active_gh_user_failure_does_not_break_resolution(self) -> None:
        """If get_active_gh_user fails, resolution should still work with configured user."""
        from sova.adapters.external_reviews import _ThreadsResult
        from sova.config.models import ProjectConfig
        from sova.core.steps.resolve_external_reviews import ResolveExternalReviewsStep

        config = ProjectConfig(github_user="xsovad06", github_repo="user/repo")
        ctx = _make_ctx(pr_number=42, config=config)
        step = ResolveExternalReviewsStep()

        cr_result = _ThreadsResult(thread_ids=["thread-1"])
        with (
            patch(
                "sova.adapters.external_reviews._fetch_coderabbit_threads",
                new_callable=AsyncMock,
                return_value=cr_result,
            ) as mock_fetch,
            patch(
                "sova.adapters.external_reviews.resolve_coderabbit_threads",
                new_callable=AsyncMock,
                return_value=1,
            ),
            patch(
                "sova.core.steps.resolve_external_reviews._dismiss_bot_reviews",
                new_callable=AsyncMock,
                return_value=0,
            ),
            patch(
                "sova.core.steps.resolve_external_reviews.get_active_gh_user",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await step.execute(ctx)

        assert result.success
        call_kwargs = mock_fetch.call_args[1]
        assert "xsovad06" in call_kwargs["authors"]

    async def test_no_threads_to_resolve(self) -> None:
        from sova.adapters.external_reviews import _ThreadsResult
        from sova.core.steps.resolve_external_reviews import ResolveExternalReviewsStep

        ctx = _make_ctx(pr_number=42)
        step = ResolveExternalReviewsStep()

        with (
            patch(
                "sova.adapters.external_reviews._fetch_coderabbit_threads",
                new_callable=AsyncMock,
                return_value=_ThreadsResult(),
            ),
            patch(
                "sova.core.steps.resolve_external_reviews._dismiss_bot_reviews",
                new_callable=AsyncMock,
                return_value=0,
            ),
            patch(
                "sova.core.steps.resolve_external_reviews.get_active_gh_user",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await step.execute(ctx)

        assert result.success
        assert "No external review threads" in result.summary

    async def test_can_skip_without_pr(self) -> None:
        from sova.core.steps.resolve_external_reviews import ResolveExternalReviewsStep

        ctx = _make_ctx()
        ctx.pr_number = None
        step = ResolveExternalReviewsStep()
        assert await step.can_skip(ctx)

    async def test_gate_always_passes(self) -> None:
        from sova.core.steps.resolve_external_reviews import ResolveExternalReviewsStep

        ctx = _make_ctx(pr_number=42)
        step = ResolveExternalReviewsStep()
        gate = await step.validate_output(ctx)
        assert gate.passed


class TestDismissBotReviews:
    async def test_dismisses_bot_changes_requested(self) -> None:
        from sova.core.steps.resolve_external_reviews import _dismiss_bot_reviews

        reviews_json = json.dumps(
            [
                {"id": 1001, "state": "CHANGES_REQUESTED", "user": {"login": "coderabbitai[bot]", "type": "Bot"}},
                {"id": 1002, "state": "APPROVED", "user": {"login": "human-dev", "type": "User"}},
                {"id": 1003, "state": "CHANGES_REQUESTED", "user": {"login": "human-reviewer", "type": "User"}},
            ]
        )

        with patch("sova.core.steps.resolve_external_reviews.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=reviews_json),  # fetch reviews
                MagicMock(success=True, stdout=""),  # dismiss review 1001
            ]
            count = await _dismiss_bot_reviews(42, repo="user/repo")

        assert count == 1
        dismiss_call = mock_run.call_args_list[1]
        assert "1001" in dismiss_call[0][4]
        assert "dismissals" in dismiss_call[0][4]

    async def test_skips_human_reviews(self) -> None:
        from sova.core.steps.resolve_external_reviews import _dismiss_bot_reviews

        reviews_json = json.dumps(
            [
                {"id": 1001, "state": "CHANGES_REQUESTED", "user": {"login": "human", "type": "User"}},
            ]
        )

        with patch("sova.core.steps.resolve_external_reviews.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout=reviews_json)
            count = await _dismiss_bot_reviews(42, repo="user/repo")

        assert count == 0

    async def test_handles_fetch_failure(self) -> None:
        from sova.core.steps.resolve_external_reviews import _dismiss_bot_reviews

        with patch("sova.core.steps.resolve_external_reviews.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=False, stderr="API error")
            count = await _dismiss_bot_reviews(42, repo="user/repo")

        assert count == 0


class TestFetchThreadsWithAuthorsFilter:
    async def test_filters_by_custom_authors(self) -> None:
        from sova.adapters.external_reviews import _fetch_coderabbit_threads

        graphql_response = json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [
                                    {
                                        "id": "t1",
                                        "isResolved": False,
                                        "path": "a.py",
                                        "line": 10,
                                        "comments": {"nodes": [{"body": "fix", "author": {"login": "xsovad06"}}]},
                                    },
                                    {
                                        "id": "t2",
                                        "isResolved": False,
                                        "path": "b.py",
                                        "line": 20,
                                        "comments": {"nodes": [{"body": "issue", "author": {"login": "coderabbitai"}}]},
                                    },
                                    {
                                        "id": "t3",
                                        "isResolved": False,
                                        "path": "c.py",
                                        "line": 30,
                                        "comments": {"nodes": [{"body": "nit", "author": {"login": "other-human"}}]},
                                    },
                                    {
                                        "id": "t4",
                                        "isResolved": True,
                                        "path": "d.py",
                                        "line": 40,
                                        "comments": {"nodes": [{"body": "old", "author": {"login": "xsovad06"}}]},
                                    },
                                ]
                            }
                        }
                    }
                }
            }
        )

        with patch("sova.adapters.external_reviews.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout=graphql_response)
            result = await _fetch_coderabbit_threads(
                "user/repo",
                42,
                authors={"xsovad06", "coderabbitai"},
                github_user="xsovad06",
            )

        assert sorted(result.thread_ids) == ["t1", "t2"]

    async def test_returns_empty_on_failure(self) -> None:
        from sova.adapters.external_reviews import _fetch_coderabbit_threads

        with patch("sova.adapters.external_reviews.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=False, stderr="error")
            result = await _fetch_coderabbit_threads("user/repo", 42, authors={"x"}, github_user="x")

        assert result.thread_ids == []
        assert result.findings == []

    async def test_returns_empty_on_bad_json(self) -> None:
        from sova.adapters.external_reviews import _fetch_coderabbit_threads

        with patch("sova.adapters.external_reviews.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="not json")
            result = await _fetch_coderabbit_threads("user/repo", 42, authors={"x"}, github_user="x")

        assert result.thread_ids == []

    async def test_skips_threads_without_comments(self) -> None:
        from sova.adapters.external_reviews import _fetch_coderabbit_threads

        graphql_response = json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [
                                    {
                                        "id": "t1",
                                        "isResolved": False,
                                        "path": "a.py",
                                        "line": 1,
                                        "comments": {"nodes": []},
                                    },
                                ]
                            }
                        }
                    }
                }
            }
        )

        with patch("sova.adapters.external_reviews.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout=graphql_response)
            result = await _fetch_coderabbit_threads("user/repo", 42, authors={"x"}, github_user="x")

        assert result.thread_ids == []


class TestDismissBotReviewsEdgeCases:
    async def test_handles_bad_json(self) -> None:
        from sova.core.steps.resolve_external_reviews import _dismiss_bot_reviews

        with patch("sova.core.steps.resolve_external_reviews.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="not json")
            count = await _dismiss_bot_reviews(42, repo="user/repo")

        assert count == 0

    async def test_handles_non_list_response(self) -> None:
        from sova.core.steps.resolve_external_reviews import _dismiss_bot_reviews

        with patch("sova.core.steps.resolve_external_reviews.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout='{"error": "something"}')
            count = await _dismiss_bot_reviews(42, repo="user/repo")

        assert count == 0

    async def test_handles_dismiss_failure(self) -> None:
        from sova.core.steps.resolve_external_reviews import _dismiss_bot_reviews

        reviews_json = json.dumps(
            [
                {"id": 1001, "state": "CHANGES_REQUESTED", "user": {"login": "bot[bot]", "type": "Bot"}},
            ]
        )

        with patch("sova.core.steps.resolve_external_reviews.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=reviews_json),
                MagicMock(success=False, stderr="403 Forbidden"),
            ]
            count = await _dismiss_bot_reviews(42, repo="user/repo")

        assert count == 0


# ---------------------------------------------------------------------------
# AddressReviewStep -- _load_coderabbit_findings
# ---------------------------------------------------------------------------


class TestLoadCoderabbitFindings:
    async def test_returns_findings_from_external_reviews(self) -> None:
        from sova.core.steps.address_review import _load_coderabbit_findings

        ctx = _make_ctx(pr_number=42)

        mock_result = MagicMock()
        mock_result.findings = [
            MagicMock(file_path="foo.py", line=10, message="Fix this"),
        ]
        mock_result.thread_ids = ["t1"]

        with patch(
            "sova.adapters.external_reviews._fetch_coderabbit_threads",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            findings, thread_ids = await _load_coderabbit_findings(ctx)

        assert len(findings) == 1
        assert findings[0]["file"] == "foo.py"
        assert findings[0]["source"] == "coderabbit"
        assert thread_ids == ["t1"]

    async def test_returns_empty_without_pr(self) -> None:
        from sova.core.steps.address_review import _load_coderabbit_findings

        ctx = _make_ctx()
        ctx.pr_number = None
        findings, thread_ids = await _load_coderabbit_findings(ctx)
        assert findings == []
        assert thread_ids == []

    async def test_returns_empty_on_exception(self) -> None:
        from sova.core.steps.address_review import _load_coderabbit_findings

        ctx = _make_ctx(pr_number=42)

        with patch(
            "sova.adapters.external_reviews._fetch_coderabbit_threads",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Network error"),
        ):
            findings, thread_ids = await _load_coderabbit_findings(ctx)

        assert findings == []
        assert thread_ids == []


# ---------------------------------------------------------------------------
# Issue-less workflow runs
# ---------------------------------------------------------------------------


class TestIssuelessWorkflow:
    """Test that the workflow engine and steps handle issue-less runs."""

    async def test_workflow_engine_issueless_run(self) -> None:
        ctx = _make_ctx(issue_number="", run_label="planner-123")
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()

        assert result.success
        assert result.steps_completed == 1

        async with await get_session() as session:
            task_run = await session.get(TaskRun, result.task_run_id)
            assert task_run is not None

    async def test_workflow_engine_issueless_creates_task_run(self) -> None:
        ctx = _make_ctx(issue_number="", run_label="test-run")
        step = DummyStep(should_pass=True, gate_pass=True)
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        result = await engine.run()
        assert result.task_run_id is not None

    async def test_dispatch_requires_role_for_issueless(self) -> None:
        """Dispatch without issue and without role should raise ValueError."""
        from sova.roles.dispatcher import dispatch

        ctx = _make_ctx(issue_number="")
        with pytest.raises(ValueError, match="Issue-less runs require"):
            await dispatch(ctx)


# ---------------------------------------------------------------------------
# Resume validation -- _load_checkpoint issue/issueless mismatch
# ---------------------------------------------------------------------------


class TestResumeValidation:
    """Verify _load_checkpoint catches issue/issueless resume mismatches."""

    async def test_resume_issue_run_without_issue_arg_fails(self) -> None:
        """Resuming an issue-based run without providing an issue should fail."""
        from sova.cli.commands.run import _load_checkpoint

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="42", role="developer", status="paused")
                session.add(run)
                await session.flush()
                run_id = run.id

        result = await _load_checkpoint(run_id, "")
        assert "error" in result
        assert "cannot resume without an issue" in result["error"]

    async def test_resume_issueless_run_with_issue_arg_fails(self) -> None:
        """Resuming an issue-less run with an issue arg should fail."""
        from sova.cli.commands.run import _load_checkpoint

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number=None, role="planner", status="paused")
                session.add(run)
                await session.flush()
                run_id = run.id

        result = await _load_checkpoint(run_id, "42")
        assert "error" in result
        assert "issue-less run" in result["error"]

    async def test_resume_matching_issue_succeeds(self) -> None:
        """Resuming with the correct issue should succeed."""
        from sova.cli.commands.run import _load_checkpoint

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="42", role="developer", status="paused")
                session.add(run)
                await session.flush()
                run_id = run.id

        result = await _load_checkpoint(run_id, "42")
        assert "error" not in result
        assert result["role"] == "developer"

    async def test_resume_issueless_without_issue_succeeds(self) -> None:
        """Resuming an issue-less run without an issue arg should succeed."""
        from sova.cli.commands.run import _load_checkpoint

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number=None, role="planner", status="paused")
                session.add(run)
                await session.flush()
                run_id = run.id

        result = await _load_checkpoint(run_id, "")
        assert "error" not in result
        assert result["role"] == "planner"


# ---------------------------------------------------------------------------
# DevelopStep -- inner check loop
# ---------------------------------------------------------------------------


class TestDevelopStepInnerCheckLoop:
    """Tests for _run_inner_check_loop and its helpers."""

    async def test_check_loop_skipped_when_disabled(self) -> None:
        from sova.config.models import DevelopConfig
        from sova.core.steps.develop import DevelopStep

        cfg = ProjectConfig(develop=DevelopConfig(max_fix_cycles=0))
        ctx = _make_ctx(config=cfg, worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        passed, summary = await step._run_inner_check_loop(ctx)
        assert passed is True
        assert summary == ""

    async def test_check_loop_skipped_when_no_check_cmd(self, tmp_path: Path) -> None:
        from sova.config.models import DevelopConfig
        from sova.core.steps.develop import DevelopStep

        cfg = ProjectConfig(check_cmd="", develop=DevelopConfig(max_fix_cycles=3))
        ctx = _make_ctx(config=cfg, worktree_dir=tmp_path)
        step = DevelopStep()

        passed, summary = await step._run_inner_check_loop(ctx)

        assert passed is True
        assert summary == ""

    async def test_check_loop_skipped_when_cmd_not_found(self) -> None:
        from sova.core.steps.develop import DevelopStep

        cfg = ProjectConfig(check_cmd="nonexistent_tool check")
        ctx = _make_ctx(config=cfg, worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with patch("sova.core.steps.develop.run", new_callable=AsyncMock) as mock_run:
            # command -v probe fails
            mock_run.return_value = MagicMock(success=False, stdout="", stderr="not found")
            passed, summary = await step._run_inner_check_loop(ctx)

        assert passed is True
        assert summary == ""

    async def test_checks_pass_first_try(self) -> None:
        from sova.core.steps.develop import DevelopStep

        cfg = ProjectConfig(check_cmd="make check")
        ctx = _make_ctx(config=cfg, worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with patch("sova.core.steps.develop.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout="", stderr=""),  # command -v probe
                MagicMock(success=True, stdout="All tests passed", stderr=""),  # check run
            ]
            passed, summary = await step._run_inner_check_loop(ctx)

        assert passed is True
        assert summary == "checks passed"

    async def test_fix_cycle_succeeds(self) -> None:
        from sova.core.steps.develop import DevelopStep
        from sova.llm.models import LLMResult

        cfg = ProjectConfig(check_cmd="make check")
        ctx = _make_ctx(config=cfg, worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with (
            patch("sova.core.steps.develop.run", new_callable=AsyncMock) as mock_run,
            patch("sova.core.steps.develop.invoke", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.side_effect = [
                MagicMock(success=True),  # command -v probe
                MagicMock(success=False, stdout="FAILED test_foo", stderr=""),  # initial check
                MagicMock(success=True, stdout=""),  # _get_dirty_test_files pre
                MagicMock(success=True, stdout="abc123\n"),  # git rev-parse HEAD (pre)
                MagicMock(success=True, stdout=" src/foo.py | 2 +-\n"),  # git diff (has changes)
                MagicMock(success=True, stdout=""),  # git diff --cached
                MagicMock(success=True, stdout="abc123\n"),  # git rev-parse HEAD (post)
                MagicMock(success=True, stdout=""),  # git status --porcelain (no untracked)
                MagicMock(success=True, stdout=""),  # _get_dirty_test_files post
                MagicMock(success=True, stdout="All tests passed", stderr=""),  # re-check passes
            ]
            mock_invoke.return_value = LLMResult(
                text="Fixed",
                model="opus",
                cost_usd=Decimal("0.50"),
                input_tokens=100,
                output_tokens=50,
            )
            passed, summary = await step._run_inner_check_loop(ctx)

        assert passed is True
        assert summary == "checks passed after 1 fix cycle(s)"
        assert ctx.cost_usd == Decimal("0.50")

    async def test_fix_cycle_no_changes_exhausts_attempts(self) -> None:
        from sova.config.models import DevelopConfig
        from sova.core.steps.develop import DevelopStep
        from sova.llm.models import LLMResult

        cfg = ProjectConfig(
            check_cmd="make check",
            develop=DevelopConfig(max_fix_cycles=2, guard_test_weakening=False),
        )
        ctx = _make_ctx(config=cfg, worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with (
            patch("sova.core.steps.develop.run", new_callable=AsyncMock) as mock_run,
            patch("sova.core.steps.develop.invoke", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.side_effect = [
                MagicMock(success=True),  # command -v probe
                MagicMock(success=False, stdout="FAIL", stderr=""),  # initial check
                # Cycle 1: no changes
                MagicMock(success=True, stdout="abc123\n"),  # git rev-parse HEAD (pre)
                MagicMock(success=True, stdout=""),  # git diff
                MagicMock(success=True, stdout=""),  # git diff --cached
                MagicMock(success=True, stdout="abc123\n"),  # git rev-parse HEAD (post, same)
                MagicMock(success=True, stdout=""),  # git status --porcelain (no untracked)
                # Cycle 2: no changes
                MagicMock(success=True, stdout="abc123\n"),  # git rev-parse HEAD (pre)
                MagicMock(success=True, stdout=""),  # git diff
                MagicMock(success=True, stdout=""),  # git diff --cached
                MagicMock(success=True, stdout="abc123\n"),  # git rev-parse HEAD (post, same)
                MagicMock(success=True, stdout=""),  # git status --porcelain (no untracked)
            ]
            mock_invoke.return_value = LLMResult(
                text="",
                model="opus",
                cost_usd=Decimal("0.10"),
                input_tokens=100,
                output_tokens=10,
            )
            passed, summary = await step._run_inner_check_loop(ctx)

        assert passed is False
        assert "no changes produced" in summary
        assert mock_invoke.await_count == 2

    async def test_budget_exceeded_stops_loop(self) -> None:
        from sova.config.models import AgentConfig, DevelopConfig
        from sova.core.steps.develop import DevelopStep

        cfg = ProjectConfig(
            check_cmd="make check",
            agent=AgentConfig(max_budget=Decimal("1.00")),
            develop=DevelopConfig(max_fix_cycles=3),
        )
        ctx = _make_ctx(config=cfg, worktree_dir=Path("/tmp/worktree"))
        ctx.cost_usd = Decimal("2.00")  # Already over budget
        step = DevelopStep()

        with patch("sova.core.steps.develop.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True),  # command -v probe
                MagicMock(success=False, stdout="FAIL", stderr=""),  # initial check
            ]
            passed, summary = await step._run_inner_check_loop(ctx)

        assert passed is False
        assert "budget exceeded" in summary

    async def test_llm_failure_returns_error_summary(self) -> None:
        from sova.core.steps.develop import DevelopStep

        cfg = ProjectConfig(check_cmd="make check")
        ctx = _make_ctx(config=cfg, worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with (
            patch("sova.core.steps.develop.run", new_callable=AsyncMock) as mock_run,
            patch("sova.core.steps.develop.invoke", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.side_effect = [
                MagicMock(success=True),  # command -v probe
                MagicMock(success=False, stdout="FAIL", stderr=""),  # initial check
                MagicMock(success=True, stdout=""),  # _get_dirty_test_files pre
                MagicMock(success=True, stdout="abc123\n"),  # git rev-parse HEAD (pre)
            ]
            mock_invoke.side_effect = RuntimeError("LLM timeout")
            passed, summary = await step._run_inner_check_loop(ctx)

        assert passed is False
        assert "LLM timeout" in summary

    async def test_test_weakening_detected_and_reverted(self) -> None:
        from sova.config.models import DevelopConfig
        from sova.core.steps.develop import DevelopStep
        from sova.llm.models import LLMResult

        cfg = ProjectConfig(
            check_cmd="make check",
            develop=DevelopConfig(max_fix_cycles=1, guard_test_weakening=True),
        )
        ctx = _make_ctx(config=cfg, worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with (
            patch("sova.core.steps.develop.run", new_callable=AsyncMock) as mock_run,
            patch("sova.core.steps.develop.invoke", new_callable=AsyncMock) as mock_invoke,
            patch("sova.core.steps.develop._get_dirty_test_files", new_callable=AsyncMock) as mock_dirty,
        ):
            mock_run.side_effect = [
                MagicMock(success=True),  # command -v probe
                MagicMock(success=False, stdout="FAIL", stderr=""),  # initial check
                MagicMock(success=True, stdout="abc123\n"),  # git rev-parse HEAD (pre)
                MagicMock(success=True, stdout=" src/foo.py | 2 +-\n"),  # git diff (has changes)
                MagicMock(success=True, stdout=""),  # git diff --cached
                MagicMock(success=True, stdout="abc123\n"),  # git rev-parse HEAD (post, same)
                MagicMock(success=True, stdout=""),  # git status --porcelain (no untracked)
                MagicMock(success=True),  # git checkout to restore tests
            ]
            mock_invoke.return_value = LLMResult(
                text="Fixed",
                model="opus",
                cost_usd=Decimal("0.10"),
                input_tokens=100,
                output_tokens=50,
            )
            mock_dirty.side_effect = [
                set(),  # pre_dirty: no tests dirty before fix
                {"tests/test_foo.py"},  # post_dirty: LLM modified a test file
            ]
            passed, summary = await step._run_inner_check_loop(ctx)

        assert passed is False
        assert "test weakening detected" in summary

    async def test_committed_test_weakening_triggers_hard_reset(self) -> None:
        """When the LLM commits test file changes (disobeying instructions),
        git reset --hard pre_hash is used instead of git checkout HEAD."""
        from sova.config.models import DevelopConfig
        from sova.core.steps.develop import DevelopStep
        from sova.llm.models import LLMResult

        cfg = ProjectConfig(
            check_cmd="make check",
            develop=DevelopConfig(max_fix_cycles=1, guard_test_weakening=True),
        )
        ctx = _make_ctx(config=cfg, worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with (
            patch("sova.core.steps.develop.run", new_callable=AsyncMock) as mock_run,
            patch("sova.core.steps.develop.invoke", new_callable=AsyncMock) as mock_invoke,
            patch("sova.core.steps.develop._get_dirty_test_files", new_callable=AsyncMock) as mock_dirty,
        ):
            mock_run.side_effect = [
                MagicMock(success=True),  # command -v probe
                MagicMock(success=False, stdout="FAIL", stderr=""),  # initial check
                MagicMock(success=True, stdout="abc123\n"),  # git rev-parse HEAD (pre)
                MagicMock(success=True, stdout=""),  # git diff --stat HEAD (no unstaged)
                MagicMock(success=True, stdout=""),  # git diff --cached --stat (no staged)
                MagicMock(success=True, stdout="def456\n"),  # git rev-parse HEAD (post, DIFFERENT)
                MagicMock(success=True, stdout=""),  # git status --porcelain (no untracked)
                # _check_test_weakening: committed diff check
                MagicMock(success=True, stdout="tests/test_foo.py\n"),  # git diff --name-only abc123 HEAD
                MagicMock(success=True),  # git reset --hard abc123
            ]
            mock_invoke.return_value = LLMResult(
                text="Fixed",
                model="opus",
                cost_usd=Decimal("0.10"),
                input_tokens=100,
                output_tokens=50,
            )
            mock_dirty.side_effect = [
                set(),  # pre_dirty: no tests dirty before fix
                set(),  # post_dirty: no uncommitted test changes (committed instead)
            ]
            passed, summary = await step._run_inner_check_loop(ctx)

        assert passed is False
        assert "test weakening detected" in summary
        # Verify git reset --hard was called with the pre-fix hash
        reset_call = mock_run.call_args_list[-1]
        assert reset_call.args == ("git", "reset", "--hard", "abc123")

    async def test_execute_includes_check_summary(self) -> None:
        from sova.core.steps.develop import DevelopStep
        from sova.llm.models import LLMResult

        cfg = ProjectConfig(check_cmd="make check")
        ctx = _make_ctx(config=cfg, worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with (
            patch("sova.core.steps.develop.invoke_command", new_callable=AsyncMock) as mock_cmd,
            patch.object(step, "_run_inner_check_loop", new_callable=AsyncMock) as mock_loop,
        ):
            mock_cmd.return_value = LLMResult(
                text="Done",
                model="opus",
                cost_usd=Decimal("1.00"),
                input_tokens=5000,
                output_tokens=3000,
                session_id="s1",
            )
            mock_loop.return_value = (True, "checks passed after 1 fix cycle(s)")
            result = await step.execute(ctx)

        assert result.success
        assert "checks passed after 1 fix cycle(s)" in result.summary

    async def test_resolve_check_cmd_uses_config(self) -> None:
        from sova.core.steps.develop import _resolve_check_cmd

        ctx = _make_ctx(config=ProjectConfig(check_cmd="pytest"))
        assert _resolve_check_cmd(ctx) == "pytest"

    async def test_resolve_check_cmd_fallback_makefile(self, tmp_path: Path) -> None:
        from sova.core.steps.develop import _resolve_check_cmd

        (tmp_path / "Makefile").write_text("check:\n\techo ok\n")
        ctx = _make_ctx(config=ProjectConfig(check_cmd=""), worktree_dir=tmp_path)
        assert _resolve_check_cmd(ctx) == "make check"

    async def test_resolve_check_cmd_returns_none(self, tmp_path: Path) -> None:
        from sova.core.steps.develop import _resolve_check_cmd

        ctx = _make_ctx(config=ProjectConfig(check_cmd=""), worktree_dir=tmp_path)
        assert _resolve_check_cmd(ctx) is None

    async def test_checks_still_failing_after_all_cycles(self) -> None:
        """Loop exhaustion: changes are produced but checks keep failing."""
        from sova.config.models import DevelopConfig
        from sova.core.steps.develop import DevelopStep
        from sova.llm.models import LLMResult

        cfg = ProjectConfig(
            check_cmd="make check",
            develop=DevelopConfig(max_fix_cycles=2, guard_test_weakening=False),
        )
        ctx = _make_ctx(config=cfg, worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with (
            patch("sova.core.steps.develop.run", new_callable=AsyncMock) as mock_run,
            patch("sova.core.steps.develop.invoke", new_callable=AsyncMock) as mock_invoke,
        ):
            mock_run.side_effect = [
                MagicMock(success=True),  # command -v probe
                MagicMock(success=False, stdout="FAIL", stderr="err1"),  # initial check
                # Cycle 1: has changes, re-check fails
                MagicMock(success=True, stdout="abc123\n"),  # rev-parse HEAD (pre)
                MagicMock(success=True, stdout=" src/foo.py | 2 +-\n"),  # git diff (unstaged)
                MagicMock(success=True, stdout=""),  # git diff --cached
                MagicMock(success=True, stdout="abc123\n"),  # rev-parse HEAD (post, same)
                MagicMock(success=True, stdout=""),  # git status --porcelain (no untracked)
                MagicMock(success=False, stdout="FAIL again", stderr="err2"),  # re-check fails
                # Cycle 2: has changes, re-check fails
                MagicMock(success=True, stdout="abc123\n"),  # rev-parse HEAD (pre)
                MagicMock(success=True, stdout=" src/foo.py | 2 +-\n"),  # git diff (unstaged)
                MagicMock(success=True, stdout=""),  # git diff --cached
                MagicMock(success=True, stdout="abc123\n"),  # rev-parse HEAD (post, same)
                MagicMock(success=True, stdout=""),  # git status --porcelain (no untracked)
                MagicMock(success=False, stdout="FAIL still", stderr="err3"),  # re-check fails
            ]
            mock_invoke.return_value = LLMResult(
                text="Fixed",
                model="opus",
                cost_usd=Decimal("0.10"),
                input_tokens=100,
                output_tokens=50,
            )
            passed, summary = await step._run_inner_check_loop(ctx)

        assert passed is False
        assert summary == "checks still failing after 2 fix cycle(s)"
        assert mock_invoke.await_count == 2

    async def test_test_weakening_continues_when_not_last_cycle(self) -> None:
        """Test weakening on non-final cycle skips re-run but continues loop."""
        from sova.config.models import DevelopConfig
        from sova.core.steps.develop import DevelopStep
        from sova.llm.models import LLMResult

        cfg = ProjectConfig(
            check_cmd="make check",
            develop=DevelopConfig(max_fix_cycles=2, guard_test_weakening=True),
        )
        ctx = _make_ctx(config=cfg, worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with (
            patch("sova.core.steps.develop.run", new_callable=AsyncMock) as mock_run,
            patch("sova.core.steps.develop.invoke", new_callable=AsyncMock) as mock_invoke,
            patch("sova.core.steps.develop._get_dirty_test_files", new_callable=AsyncMock) as mock_dirty,
        ):
            mock_run.side_effect = [
                MagicMock(success=True),  # command -v probe
                MagicMock(success=False, stdout="FAIL", stderr=""),  # initial check
                # Cycle 1: test weakening -> skip (not last cycle)
                MagicMock(success=True, stdout="abc123\n"),  # rev-parse HEAD (pre)
                MagicMock(success=True, stdout=" src/foo.py | 2 +-\n"),  # git diff (has changes)
                MagicMock(success=True, stdout=""),  # git diff --cached
                MagicMock(success=True, stdout="abc123\n"),  # rev-parse HEAD (post, same)
                MagicMock(success=True, stdout=""),  # git status --porcelain (no untracked)
                MagicMock(success=True),  # git checkout to restore tests
                # Cycle 2: no test weakening, re-check passes
                MagicMock(success=True, stdout="abc123\n"),  # rev-parse HEAD (pre)
                MagicMock(success=True, stdout=" src/foo.py | 2 +-\n"),  # git diff (has changes)
                MagicMock(success=True, stdout=""),  # git diff --cached
                MagicMock(success=True, stdout="abc123\n"),  # rev-parse HEAD (post, same)
                MagicMock(success=True, stdout=""),  # git status --porcelain (no untracked)
                MagicMock(success=True, stdout="All passed", stderr=""),  # re-check passes
            ]
            mock_invoke.return_value = LLMResult(
                text="Fixed",
                model="opus",
                cost_usd=Decimal("0.10"),
                input_tokens=100,
                output_tokens=50,
            )
            mock_dirty.side_effect = [
                set(),  # cycle 1 pre
                {"tests/test_foo.py"},  # cycle 1 post (weakened)
                set(),  # cycle 2 pre
                set(),  # cycle 2 post (clean)
            ]
            passed, summary = await step._run_inner_check_loop(ctx)

        assert passed is True
        assert summary == "checks passed after 2 fix cycle(s)"

    async def test_committed_test_weakening_detected(self) -> None:
        """Test weakening via committed test files uses git reset --hard, not checkout."""
        from sova.config.models import DevelopConfig
        from sova.core.steps.develop import DevelopStep
        from sova.llm.models import LLMResult

        cfg = ProjectConfig(
            check_cmd="make check",
            develop=DevelopConfig(max_fix_cycles=1, guard_test_weakening=True),
        )
        ctx = _make_ctx(config=cfg, worktree_dir=Path("/tmp/worktree"))
        step = DevelopStep()

        with (
            patch("sova.core.steps.develop.run", new_callable=AsyncMock) as mock_run,
            patch("sova.core.steps.develop.invoke", new_callable=AsyncMock) as mock_invoke,
            patch("sova.core.steps.develop._get_dirty_test_files", new_callable=AsyncMock) as mock_dirty,
        ):
            mock_run.side_effect = [
                MagicMock(success=True),  # command -v probe
                MagicMock(success=False, stdout="FAIL", stderr=""),  # initial check
                # Cycle 1: LLM committed, so new commits exist
                MagicMock(success=True, stdout="abc123\n"),  # rev-parse HEAD (pre)
                MagicMock(success=True, stdout=""),  # git diff (no unstaged)
                MagicMock(success=True, stdout=""),  # git diff --cached (no staged)
                MagicMock(success=True, stdout="def456\n"),  # rev-parse HEAD (post, DIFFERENT)
                MagicMock(success=True, stdout=""),  # git status --porcelain (no untracked)
                # _check_test_weakening: committed diff shows test file
                MagicMock(success=True, stdout="tests/test_bar.py\nsrc/bar.py\n"),
                MagicMock(success=True),  # git reset --hard pre_hash
            ]
            mock_invoke.return_value = LLMResult(
                text="Fixed",
                model="opus",
                cost_usd=Decimal("0.10"),
                input_tokens=100,
                output_tokens=50,
            )
            mock_dirty.side_effect = [
                set(),  # pre: no dirty test files
                set(),  # post: no uncommitted dirty (committed instead)
            ]
            passed, summary = await step._run_inner_check_loop(ctx)

        assert passed is False
        assert "test weakening detected" in summary
        # Verify git reset --hard was used (not git checkout HEAD --)
        reset_call = mock_run.call_args_list[-1]
        assert reset_call[0] == ("git", "reset", "--hard", "abc123")

    async def test_detect_fix_changes_all_sources(self) -> None:
        """_detect_fix_changes detects unstaged, staged, and commit changes independently."""
        from sova.core.steps.develop import DevelopStep

        step = DevelopStep()
        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))

        with patch("sova.core.steps.develop.run", new_callable=AsyncMock) as mock_run:
            # Case: only staged changes
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # git diff (no unstaged)
                MagicMock(success=True, stdout=" file.py | 1 +\n"),  # git diff --cached (staged)
                MagicMock(success=True, stdout="abc123\n"),  # rev-parse HEAD (same as pre)
                MagicMock(success=True, stdout=""),  # git status --porcelain (no untracked)
            ]
            changes = await step._detect_fix_changes(ctx, "abc123")

        assert changes["has_staged"] is True
        assert changes["has_unstaged"] is False
        assert changes["has_new_commits"] is False
        assert changes["has_untracked"] is False
        assert changes["any"] is True

    async def test_detect_fix_changes_untracked_only(self) -> None:
        """_detect_fix_changes returns any=True when only untracked files exist."""
        from sova.core.steps.develop import DevelopStep

        step = DevelopStep()
        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))

        with patch("sova.core.steps.develop.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                MagicMock(success=True, stdout=""),  # git diff (no unstaged)
                MagicMock(success=True, stdout=""),  # git diff --cached (no staged)
                MagicMock(success=True, stdout="abc123\n"),  # rev-parse HEAD (same as pre)
                MagicMock(success=True, stdout="?? new_file.py\n"),  # git status --porcelain (untracked)
            ]
            changes = await step._detect_fix_changes(ctx, "abc123")

        assert changes["has_untracked"] is True
        assert changes["has_unstaged"] is False
        assert changes["has_staged"] is False
        assert changes["has_new_commits"] is False
        assert changes["any"] is True

    async def test_get_head_hash_failure(self) -> None:
        """_get_head_hash returns empty string on failure."""
        from sova.core.steps.develop import DevelopStep

        step = DevelopStep()
        ctx = _make_ctx(worktree_dir=Path("/tmp/worktree"))

        with patch("sova.core.steps.develop.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=False, stdout="", stderr="not a repo")
            result = await step._get_head_hash(ctx)

        assert result == ""

    async def test_get_dirty_test_files_returns_matches(self) -> None:
        """_get_dirty_test_files filters for test file patterns."""
        from sova.core.steps.develop import _get_dirty_test_files

        with patch("sova.core.steps.develop.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(
                success=True,
                stdout="src/module.py\ntests/test_core.py\nutils_test.py\n",
            )
            result = await _get_dirty_test_files(Path("/tmp/worktree"))

        assert result == {"tests/test_core.py", "utils_test.py"}


# ---------------------------------------------------------------------------
# get_active_gh_user (sova/utils/gh.py)
# ---------------------------------------------------------------------------


class TestGetActiveGhUser:
    async def test_returns_active_login(self) -> None:
        from sova.utils.gh import get_active_gh_user

        auth_json = json.dumps(
            {
                "hosts": {
                    "github.com": [
                        {"active": True, "login": "dsova06"},
                        {"active": False, "login": "xsovad06"},
                    ]
                }
            }
        )
        with patch("sova.utils.gh.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout=auth_json, stderr="")
            result = await get_active_gh_user()

        assert result == "dsova06"

    async def test_returns_none_on_command_failure(self) -> None:
        from sova.utils.gh import get_active_gh_user

        with patch("sova.utils.gh.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=False, stdout="", stderr="gh not found")
            result = await get_active_gh_user()

        assert result is None

    async def test_returns_none_on_bad_json(self) -> None:
        from sova.utils.gh import get_active_gh_user

        with patch("sova.utils.gh.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout="not json", stderr="")
            result = await get_active_gh_user()

        assert result is None

    async def test_returns_none_when_no_active_account(self) -> None:
        from sova.utils.gh import get_active_gh_user

        auth_json = json.dumps({"hosts": {"github.com": [{"active": False, "login": "xsovad06"}]}})
        with patch("sova.utils.gh.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout=auth_json, stderr="")
            result = await get_active_gh_user()

        assert result is None

    async def test_returns_none_when_hosts_empty(self) -> None:
        from sova.utils.gh import get_active_gh_user

        auth_json = json.dumps({"hosts": {}})
        with patch("sova.utils.gh.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout=auth_json, stderr="")
            result = await get_active_gh_user()

        assert result is None

    async def test_returns_none_on_unexpected_structure(self) -> None:
        from sova.utils.gh import get_active_gh_user

        auth_json = json.dumps({"hosts": "not-a-dict"})
        with patch("sova.utils.gh.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout=auth_json, stderr="")
            result = await get_active_gh_user()

        assert result is None
