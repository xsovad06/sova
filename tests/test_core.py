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
            "COMMITTING",
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
    async def test_execute_generates_rich_body(self, mock_create_pr, mock_run, mock_invoke, _find) -> None:
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.return_value = MagicMock(success=True, stdout="abc123 feat: add widget\n")
        mock_invoke.return_value = LLMResult(
            text="## Summary\n- Added widget support\n\nCloses #42",
            model="sonnet",
            cost_usd=Decimal("0.01"),
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
        assert "Added widget" in body_arg

    @patch("sova.core.steps.create_pr.git_ops.find_pr_for_issue", new_callable=AsyncMock, return_value=None)
    @patch("sova.core.steps.create_pr.invoke")
    @patch("sova.core.steps.create_pr.run")
    @patch("sova.core.steps.create_pr.git_ops.create_pr")
    async def test_execute_appends_closes_when_missing(self, mock_create_pr, mock_run, mock_invoke, _find) -> None:
        from sova.core.steps.create_pr import CreatePRStep
        from sova.llm.models import LLMResult

        mock_run.return_value = MagicMock(success=True, stdout="abc123 feat\n")
        mock_invoke.return_value = LLMResult(
            text="## Summary\n- Did stuff",
            model="sonnet",
            cost_usd=Decimal("0.01"),
        )
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
    async def test_execute_falls_back_on_llm_failure(self, mock_create_pr, mock_run, mock_invoke, _find) -> None:
        from sova.core.steps.create_pr import CreatePRStep

        mock_run.side_effect = [
            MagicMock(success=True, stdout="abc123 feat: add widget\n"),
            MagicMock(success=True, stdout=" src/app.py | 10 ++++\n 1 file changed, 10 insertions(+)\n"),
        ]
        mock_invoke.side_effect = RuntimeError("LLM unavailable")
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

        mock_run.return_value = MagicMock(success=True, stdout="abc123 feat\n")
        mock_invoke.return_value = LLMResult(
            text="## Summary\n- Added widget\n\nCloses #42",
            model="sonnet",
            cost_usd=Decimal("0.01"),
        )
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

        mock_run.return_value = MagicMock(success=True, stdout="abc123 feat\n")
        mock_invoke.return_value = LLMResult(
            text="## Summary\n- Added widget\n\nCloses #42",
            model="sonnet",
            cost_usd=Decimal("0.01"),
        )
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

        ctx = _make_ctx(branch_name="feat/issue-42")
        step = CreatePRStep()
        result = await step.execute(ctx)

        assert result.success
        assert "Adopted existing PR #55" in result.summary
        assert ctx.pr_number == 55
        assert ctx.pr_url == "https://github.com/x/y/pull/55"


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
        assert len(handoff.next_actions) == 2
        assert handoff.next_actions[0].id == "integrate"
        assert handoff.next_actions[1].id == "approve"
        assert all(not a.auto_execute for a in handoff.next_actions)


class TestAddressReviewStep:
    async def test_no_findings_returns_success(self) -> None:
        from sova.core.steps.address_review import AddressReviewStep

        ctx = _make_ctx(pr_number=42)
        ctx.project_dir = Path("/tmp/nonexistent")
        step = AddressReviewStep()

        result = await step.execute(ctx)

        assert result.success
        assert "No review findings" in result.summary


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
            "commit",
            "validate",
            "push",
            "create_pr",
            "monitor_ci",
            "handoff_to_reviewer",
        ]

    def test_get_address_review_steps(self) -> None:
        from sova.core.steps import get_address_review_steps

        steps = get_address_review_steps()
        names = [s.name for s in steps]
        assert names == [
            "rebase",
            "address_review",
            "commit",
            "validate",
            "push",
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


# ---------------------------------------------------------------------------
# MonitorCIStep -- no-checks grace period
# ---------------------------------------------------------------------------


class TestMonitorCIStep:
    async def test_passes_when_all_checks_pass(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(pr_number=10)
        step = MonitorCIStep()

        check = MagicMock(is_completed=True, is_passed=True, name="CI")
        with patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks:
            mock_checks.return_value = [check]
            result = await step.execute(ctx)

        assert result.success
        assert "1 CI checks passed" in result.summary

    async def test_fails_when_check_fails(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(pr_number=10)
        step = MonitorCIStep()

        check = MagicMock(is_completed=True, is_passed=False)
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

        check = MagicMock(is_completed=True, is_passed=True, name="CI")
        with (
            patch("sova.core.steps.monitor_ci.get_ci_checks", new_callable=AsyncMock) as mock_checks,
            patch("sova.core.steps.monitor_ci.asyncio.sleep", new_callable=AsyncMock),
        ):
            # No checks on first poll, then checks appear
            mock_checks.side_effect = [[], [check]]
            result = await step.execute(ctx)

        assert result.success
        assert "1 CI checks passed" in result.summary

    async def test_fails_when_no_pr(self) -> None:
        from sova.core.steps.monitor_ci import MonitorCIStep

        ctx = _make_ctx(pr_number=None)
        step = MonitorCIStep()

        result = await step.execute(ctx)

        assert not result.success
        assert "no pr" in result.summary.lower()


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
                MagicMock(success=True, stdout=""),  # rebase (clean)
            ]
            result, cost = await rebase_with_conflict_resolution("main", cwd=Path("/tmp"))

        assert result.success
        assert cost == Decimal("0")

    async def test_conflict_resolved_by_llm(self) -> None:
        from sova.git.operations import rebase_with_conflict_resolution
        from sova.llm.models import LLMResult

        with (
            patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.rebase.invoke_command", new_callable=AsyncMock) as mock_llm,
        ):
            mock_run.side_effect = [
                MagicMock(success=True),  # fetch
                MagicMock(success=False, stderr="CONFLICT"),  # rebase fails
                MagicMock(success=True, stdout="file.py\n"),  # conflicted files
                MagicMock(success=True, stdout=""),  # no remaining conflicts
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
                MagicMock(success=False, stderr="CONFLICT"),  # rebase fails
                MagicMock(success=True, stdout="file.py\n"),  # conflicted files
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
