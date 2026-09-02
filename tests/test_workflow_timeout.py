"""Tests for workflow timeout features (complexity multiplier, partial work preservation)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.core.context import ExecutionContext
from sova.core.workflow import WorkflowEngine
from sova.llm.complexity import ComplexityTier


class TestComplexityTimeoutMultiplier:
    """Test that complexity tier applies timeout multipliers."""

    @pytest.fixture
    def mock_adapter(self) -> MagicMock:
        adapter = MagicMock()
        adapter.repo = "test/repo"
        return adapter

    @pytest.fixture
    def ctx(self, tmp_path: Path, mock_adapter: MagicMock, seed_config) -> ExecutionContext:
        from sova.config.loader import load_config

        seed_config(tmp_path, github_repo="test/repo")
        cfg = load_config(tmp_path)
        return ExecutionContext(
            project_dir=tmp_path,
            config=cfg,
            adapter=mock_adapter,
            issue_number="123",
            role="developer",
        )

    def test_step_timeout_no_complexity(self, ctx: ExecutionContext) -> None:
        """When complexity is None, multiplier is 1.0."""
        engine = WorkflowEngine(steps=[], ctx=ctx)
        ctx.complexity = None
        assert engine._step_timeout("develop") == min(1200, 1800)

    def test_step_timeout_complex_multiplier(self, ctx: ExecutionContext) -> None:
        """COMPLEX issues get 1.5x timeout."""
        engine = WorkflowEngine(steps=[], ctx=ctx)
        ctx.complexity = ComplexityTier.COMPLEX
        base = min(ctx.config.develop.step_timeout, ctx.config.agent.step_timeout)
        assert engine._step_timeout("develop") == int(base * 1.5)

    def test_step_timeout_epic_multiplier(self, ctx: ExecutionContext) -> None:
        """EPIC issues get 2.0x timeout."""
        engine = WorkflowEngine(steps=[], ctx=ctx)
        ctx.complexity = ComplexityTier.EPIC
        base = min(ctx.config.develop.step_timeout, ctx.config.agent.step_timeout)
        assert engine._step_timeout("develop") == int(base * 2.0)

    def test_step_timeout_trivial_no_multiplier(self, ctx: ExecutionContext) -> None:
        """TRIVIAL and SIMPLE issues get no multiplier."""
        engine = WorkflowEngine(steps=[], ctx=ctx)
        ctx.complexity = ComplexityTier.TRIVIAL
        assert engine._step_timeout("develop") == min(1200, 1800)

        ctx.complexity = ComplexityTier.SIMPLE
        assert engine._step_timeout("develop") == min(1200, 1800)

    def test_step_timeout_multiplier_capped_at_3x(self, ctx: ExecutionContext, tmp_path: Path) -> None:
        """Multiplier is capped at 3.0x to prevent unbounded timeouts."""

        # This test just verifies the cap logic in the code
        # The max multiplier in the current implementation is 2.0 (EPIC)
        # but the cap is set to 3.0 for future extensibility
        engine = WorkflowEngine(steps=[], ctx=ctx)
        ctx.complexity = ComplexityTier.EPIC
        base = min(ctx.config.develop.step_timeout, ctx.config.agent.step_timeout)
        result = engine._step_timeout("develop")
        # Result should be 2.0x, well under the 3.0x cap
        assert result == int(base * 2.0)
        assert result <= int(base * 3.0)

    def test_monitor_ci_timeout_with_complexity(self, ctx: ExecutionContext) -> None:
        """monitor_ci timeout also gets complexity multiplier."""
        engine = WorkflowEngine(steps=[], ctx=ctx)
        ctx.complexity = ComplexityTier.COMPLEX
        base = ctx.config.ci.max_wait + 120
        assert engine._step_timeout("monitor_ci") == int(base * 1.5)

    def test_generic_step_timeout_with_complexity(self, ctx: ExecutionContext) -> None:
        """Generic steps (validate, commit, etc.) also get multiplier."""
        engine = WorkflowEngine(steps=[], ctx=ctx)
        ctx.complexity = ComplexityTier.EPIC
        base = ctx.config.agent.step_timeout
        assert engine._step_timeout("validate") == int(base * 2.0)


class TestPartialWorkPreservation:
    """Test that partial work is committed on timeout."""

    @pytest.fixture
    def mock_adapter(self) -> MagicMock:
        adapter = MagicMock()
        adapter.repo = "test/repo"
        return adapter

    @pytest.fixture
    def ctx(self, tmp_path: Path, mock_adapter: MagicMock, seed_config) -> ExecutionContext:
        import asyncio

        from sova.config.loader import load_config
        from sova.utils.shell import run

        # Create a git repo
        asyncio.run(run("git", "init", cwd=tmp_path))
        asyncio.run(run("git", "config", "user.email", "test@example.com", cwd=tmp_path))
        asyncio.run(run("git", "config", "user.name", "Test User", cwd=tmp_path))

        seed_config(tmp_path, github_repo="test/repo")
        cfg = load_config(tmp_path)
        ctx = ExecutionContext(
            project_dir=tmp_path,
            config=cfg,
            adapter=mock_adapter,
            issue_number="123",
            role="developer",
        )
        ctx.worktree_dir = tmp_path
        return ctx

    async def test_preserve_partial_work_with_changes(self, ctx: ExecutionContext, tmp_path: Path) -> None:
        """When timeout occurs with staged changes, they are committed."""
        from sova.utils.shell import run

        # Create and commit a file so we have something to modify
        test_file = tmp_path / "test.txt"
        test_file.write_text("initial")
        await run("git", "add", "test.txt", cwd=tmp_path)
        await run("git", "commit", "-m", "initial", cwd=tmp_path)

        # Modify the file
        test_file.write_text("modified")

        engine = WorkflowEngine(steps=[], ctx=ctx)
        result = await engine._preserve_partial_work_on_timeout("develop")

        assert result is True

        # Verify commit was created
        log_result = await run("git", "log", "--oneline", "-1", cwd=tmp_path)
        assert "wip: partial work from develop (timeout)" in log_result.stdout

    async def test_preserve_partial_work_no_changes(self, ctx: ExecutionContext, tmp_path: Path) -> None:
        """When there are no staged changes, no commit is created."""
        from sova.utils.shell import run

        # Initialize repo with a commit
        test_file = tmp_path / "test.txt"
        test_file.write_text("initial")
        await run("git", "add", "test.txt", cwd=tmp_path)
        await run("git", "commit", "-m", "initial", cwd=tmp_path)

        engine = WorkflowEngine(steps=[], ctx=ctx)
        result = await engine._preserve_partial_work_on_timeout("develop")

        assert result is False

        # Verify no new commit
        log_result = await run("git", "log", "--oneline", cwd=tmp_path)
        assert "wip:" not in log_result.stdout

    async def test_preserve_partial_work_not_a_git_repo(self, tmp_path: Path, seed_config) -> None:
        """When working_dir is not a git repo, returns False."""
        from sova.config.loader import load_config

        non_git_dir = tmp_path / "not_git"
        non_git_dir.mkdir()
        seed_config(non_git_dir, github_repo="test/repo")

        cfg = load_config(non_git_dir)
        adapter = MagicMock()
        adapter.repo = "test/repo"
        ctx = ExecutionContext(
            project_dir=non_git_dir,
            config=cfg,
            adapter=adapter,
            issue_number="123",
            role="developer",
        )

        engine = WorkflowEngine(steps=[], ctx=ctx)
        result = await engine._preserve_partial_work_on_timeout("develop")

        assert result is False

    async def test_timeout_sets_partial_work_flag(self, ctx: ExecutionContext, tmp_path: Path) -> None:
        """Step timeout sets partial_work=True in StepResult when work was committed."""
        from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
        from sova.utils.shell import run

        # Setup: create a file and commit it
        test_file = tmp_path / "test.txt"
        test_file.write_text("initial")
        await run("git", "add", "test.txt", cwd=tmp_path)
        await run("git", "commit", "-m", "initial", cwd=tmp_path)

        class SlowStep(BaseStep):
            name = "slow_step"

            async def execute(self, ctx: ExecutionContext) -> StepResult:
                import asyncio

                # Modify a file before timing out
                test_file.write_text("modified during step")
                await asyncio.sleep(10)  # Will be interrupted by timeout
                return StepResult(success=True, summary="Should not reach here")

            async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

        # Set a very short timeout for testing
        with patch.object(WorkflowEngine, "_step_timeout", return_value=1):
            engine = WorkflowEngine(steps=[SlowStep()], ctx=ctx)
            result = await engine._run_step_with_timeout(SlowStep())

        assert result.success is False
        assert result.error == "step_hard_timeout"
        assert result.partial_work is True

        # Verify the WIP commit exists
        log_result = await run("git", "log", "--oneline", "-1", cwd=tmp_path)
        assert "wip:" in log_result.stdout


class TestValidateStepConfig:
    """Test that ValidateStep uses config values for timeouts and max attempts."""

    @pytest.fixture
    def mock_adapter(self) -> MagicMock:
        adapter = MagicMock()
        adapter.repo = "test/repo"
        return adapter

    @pytest.fixture
    def ctx(self, tmp_path: Path, mock_adapter: MagicMock, seed_config) -> ExecutionContext:
        import asyncio

        from sova.config.loader import load_config
        from sova.utils.shell import run

        # Create a git repo
        asyncio.run(run("git", "init", cwd=tmp_path))
        asyncio.run(run("git", "config", "user.email", "test@example.com", cwd=tmp_path))
        asyncio.run(run("git", "config", "user.name", "Test User", cwd=tmp_path))

        # Seed custom validate config
        seed_config(
            tmp_path,
            github_repo="test/repo",
            validation={"fix_timeout": 240, "max_fix_attempts": 3, "hook_timeout": 150},
        )
        cfg = load_config(tmp_path)
        return ExecutionContext(
            project_dir=tmp_path,
            config=cfg,
            adapter=mock_adapter,
            issue_number="123",
            role="developer",
        )

    async def test_validate_uses_config_hook_timeout(self, ctx: ExecutionContext, tmp_path: Path) -> None:
        """ValidateStep passes config.validation.hook_timeout to hook execution."""
        from sova.core.steps.validate import ValidateStep
        from sova.utils.shell import run

        # Create a simple pre-push hook
        hooks_dir = tmp_path / ".githooks"
        hooks_dir.mkdir()
        hook = hooks_dir / "pre-push"
        hook.write_text("#!/bin/bash\nexit 0\n")
        hook.chmod(0o755)
        await run("git", "config", "core.hooksPath", ".githooks", cwd=tmp_path)

        step = ValidateStep()

        with patch("sova.core.steps.validate.run", new_callable=AsyncMock) as mock_run:
            from sova.utils.shell import ShellResult

            mock_run.return_value = ShellResult(returncode=0, stdout="", stderr="")

            await step.execute(ctx)

            # Verify hook was called with config timeout (150s)
            assert mock_run.call_count >= 1
            call_kwargs = mock_run.call_args[1]
            assert call_kwargs["timeout"] == 150

    async def test_validate_uses_config_max_fix_attempts(self, ctx: ExecutionContext, tmp_path: Path) -> None:
        """ValidateStep respects config.validation.max_fix_attempts."""
        from sova.core.steps.validate import ValidateStep
        from sova.utils.shell import run

        # Create a failing pre-push hook
        hooks_dir = tmp_path / ".githooks"
        hooks_dir.mkdir()
        hook = hooks_dir / "pre-push"
        hook.write_text("#!/bin/bash\necho 'Hook failed'\nexit 1\n")
        hook.chmod(0o755)
        await run("git", "config", "core.hooksPath", ".githooks", cwd=tmp_path)

        step = ValidateStep()

        with patch("sova.core.steps.validate.invoke", new_callable=AsyncMock) as mock_invoke:
            from sova.llm.models import LLMResult

            mock_invoke.return_value = LLMResult(
                text="fixed",
                model="claude-sonnet-4",
                cost_usd=Decimal("0.01"),
                input_tokens=10,
                output_tokens=5,
                stop_reason="end_turn",
            )

            await step.execute(ctx)

            # Should try max_fix_attempts (3) times
            # verify timeout kwarg is passed to invoke
            assert mock_invoke.call_count == 3
            for call in mock_invoke.call_args_list:
                assert call[1]["timeout"] == 240  # config.validation.fix_timeout

    async def test_validate_checks_budget_before_fix(self, ctx: ExecutionContext, tmp_path: Path) -> None:
        """ValidateStep checks budget before each fix attempt."""
        from sova.core.steps.validate import ValidateStep
        from sova.utils.shell import run

        # Create a failing hook
        hooks_dir = tmp_path / ".githooks"
        hooks_dir.mkdir()
        hook = hooks_dir / "pre-push"
        hook.write_text("#!/bin/bash\nexit 1\n")
        hook.chmod(0o755)
        await run("git", "config", "core.hooksPath", ".githooks", cwd=tmp_path)

        # Exhaust budget (set slightly above max to trigger is_budget_exceeded)
        from decimal import Decimal

        ctx.cost_usd = Decimal(str(ctx.config.agent.max_budget)) + Decimal("0.01")

        step = ValidateStep()
        result = await step.execute(ctx)

        assert result.success is False
        assert "budget exceeded" in result.summary.lower()


class TestMonitorCIStepConfig:
    """Test that MonitorCIStep uses validation.fix_timeout for LLM invocations."""

    @pytest.fixture
    def mock_adapter(self) -> MagicMock:
        adapter = MagicMock()
        adapter.repo = "test/repo"
        adapter.github_user = "testuser"
        return adapter

    @pytest.fixture
    def ctx(self, tmp_path: Path, mock_adapter: MagicMock, seed_config) -> ExecutionContext:
        from sova.config.loader import load_config

        seed_config(tmp_path, github_repo="test/repo", validation={"fix_timeout": 300})
        cfg = load_config(tmp_path)
        ctx = ExecutionContext(
            project_dir=tmp_path,
            config=cfg,
            adapter=mock_adapter,
            issue_number="123",
            role="developer",
        )
        ctx.branch_name = "feat/test"
        ctx.pr_number = 42
        return ctx

    async def test_monitor_ci_passes_fix_timeout_to_invoke(self, ctx: ExecutionContext) -> None:
        """MonitorCIStep passes config.validation.fix_timeout to invoke() in fix loop."""
        from sova.core.steps.monitor_ci import MonitorCIStep

        step = MonitorCIStep()

        with patch("sova.llm.client.invoke", new_callable=AsyncMock) as mock_invoke:
            from sova.llm.models import LLMResult

            mock_invoke.return_value = LLMResult(
                text="fixed",
                model="claude-sonnet-4",
                cost_usd=Decimal("0.01"),
                input_tokens=10,
                output_tokens=5,
                stop_reason="end_turn",
            )

            with patch("sova.git.operations.get_ci_failure_logs", new_callable=AsyncMock) as mock_logs:
                mock_logs.return_value = "CI failed: test error"

                with patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run:
                    from sova.utils.shell import ShellResult

                    # Mock successful git operations
                    mock_run.return_value = ShellResult(returncode=0, stdout="", stderr="")

                    from sova.git.operations import CICheck
                    from sova.git.pr import CheckConclusion, CheckStatus

                    failed_checks = [
                        CICheck(
                            name="test",
                            status=CheckStatus.COMPLETED,
                            conclusion=CheckConclusion.FAILURE,
                            details_url="https://example.com",
                        )
                    ]

                    # Call _invoke_fix directly to verify timeout is passed
                    await step._invoke_fix(ctx, failed_checks, mock_invoke, mock_run)

                    # Verify timeout was passed to invoke
                    assert mock_invoke.call_count == 1
                    call_kwargs = mock_invoke.call_args[1]
                    assert call_kwargs["timeout"] == 300  # config.validation.fix_timeout


class TestWorktreeDeletion:
    """Test worktree deletion detection in _execute_step."""

    @pytest.fixture
    def mock_adapter(self) -> MagicMock:
        adapter = MagicMock()
        adapter.repo = "test/repo"
        return adapter

    @pytest.fixture
    def ctx(self, tmp_path: Path, mock_adapter: MagicMock, seed_config) -> ExecutionContext:
        from sova.config.loader import load_config

        seed_config(tmp_path, github_repo="test/repo")
        cfg = load_config(tmp_path)
        return ExecutionContext(
            project_dir=tmp_path,
            config=cfg,
            adapter=mock_adapter,
            issue_number="123",
            role="developer",
        )

    async def test_execute_step_fails_when_worktree_deleted(self, ctx: ExecutionContext, tmp_path: Path) -> None:
        """When worktree is deleted mid-pipeline, step fails immediately."""
        from sova.core.state import TaskStatus
        from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
        from sova.core.workflow import WorkflowResult

        class DummyStep(BaseStep):
            name = "dummy_step"

            async def execute(self, ctx: ExecutionContext) -> StepResult:
                return StepResult(success=True, summary="Should not reach here")

            async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

        # Set worktree_dir to a non-existent path
        deleted_worktree = tmp_path / "deleted_worktree"
        ctx.worktree_dir = deleted_worktree
        ctx.run_id = 999  # Mock run ID

        engine = WorkflowEngine(steps=[DummyStep()], ctx=ctx)
        result = WorkflowResult(success=True, final_status=TaskStatus.IN_PROGRESS)

        # Mock the DB and output methods that _execute_step calls
        with patch.object(engine, "_write_output", new_callable=AsyncMock) as mock_output:
            with patch.object(engine, "_close_output", new_callable=AsyncMock):
                with patch.object(engine, "_record_failure", new_callable=AsyncMock):
                    with patch.object(engine, "_update_task_run_status", new_callable=AsyncMock):
                        # Execute step should detect deleted worktree and fail
                        success = await engine._execute_step(DummyStep(), result)

        assert success is False
        assert result.final_status == TaskStatus.FAILED
        assert "Worktree does not exist" in result.error
        # Verify failure was logged
        mock_output.assert_called_once()
        assert "FAILED" in mock_output.call_args[0][0]


class TestGateCheckTimeout:
    """Test gate check timeout and exception handling."""

    @pytest.fixture
    def mock_adapter(self) -> MagicMock:
        adapter = MagicMock()
        adapter.repo = "test/repo"
        return adapter

    @pytest.fixture
    def ctx(self, tmp_path: Path, mock_adapter: MagicMock, seed_config) -> ExecutionContext:
        from sova.config.loader import load_config

        seed_config(tmp_path, github_repo="test/repo")
        cfg = load_config(tmp_path)
        return ExecutionContext(
            project_dir=tmp_path,
            config=cfg,
            adapter=mock_adapter,
            issue_number="123",
            role="developer",
        )

    async def test_validate_step_gate_timeout(self, ctx: ExecutionContext, tmp_path: Path) -> None:
        """When gate check times out, it returns failed with timeout reason."""
        from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
        from sova.core.workflow import StepRecord

        class SlowGateStep(BaseStep):
            name = "slow_gate"

            async def execute(self, ctx: ExecutionContext) -> StepResult:
                return StepResult(success=True, summary="Executed")

            async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
                import asyncio

                await asyncio.sleep(100)  # Will timeout
                return GateCheckResult(passed=True)

        ctx.run_id = 999  # Mock run ID
        step_exec_id = 1  # Mock step execution ID

        record = StepRecord(step_name="slow_gate", status="running")

        # Mock a very short timeout and DB update
        engine = WorkflowEngine(steps=[SlowGateStep()], ctx=ctx)
        with patch.object(engine, "_step_timeout", return_value=1):
            with patch.object(engine, "_update_step_execution_gate", new_callable=AsyncMock):
                status = await engine._validate_step_gate(SlowGateStep(), step_exec_id, record)

        assert status == "failed"
        assert record.gate is not None
        assert record.gate.passed is False
        assert "timed out" in record.gate.reason.lower()

    async def test_validate_step_gate_exception(self, ctx: ExecutionContext, tmp_path: Path) -> None:
        """When gate check raises an exception, it returns failed with error reason."""
        from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
        from sova.core.workflow import StepRecord

        class BrokenGateStep(BaseStep):
            name = "broken_gate"

            async def execute(self, ctx: ExecutionContext) -> StepResult:
                return StepResult(success=True, summary="Executed")

            async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
                raise ValueError("Simulated gate error")

        ctx.run_id = 999  # Mock run ID
        step_exec_id = 1  # Mock step execution ID

        record = StepRecord(step_name="broken_gate", status="running")

        engine = WorkflowEngine(steps=[BrokenGateStep()], ctx=ctx)
        with patch.object(engine, "_update_step_execution_gate", new_callable=AsyncMock):
            status = await engine._validate_step_gate(BrokenGateStep(), step_exec_id, record)

        assert status == "failed"
        assert record.gate is not None
        assert record.gate.passed is False
        assert "ValueError" in record.gate.reason
        assert "Simulated gate error" in record.gate.reason


class TestVerifyOutput:
    """Test the verify_output heavyweight verification pass."""

    @pytest.fixture
    def mock_adapter(self) -> MagicMock:
        adapter = MagicMock()
        adapter.repo = "test/repo"
        return adapter

    @pytest.fixture
    def ctx(self, tmp_path: Path, mock_adapter: MagicMock, seed_config) -> ExecutionContext:
        from sova.config.loader import load_config

        seed_config(tmp_path, github_repo="test/repo")
        cfg = load_config(tmp_path)
        return ExecutionContext(
            project_dir=tmp_path,
            config=cfg,
            adapter=mock_adapter,
            issue_number="123",
            role="developer",
        )

    async def test_base_step_verify_output_noop(self, ctx: ExecutionContext) -> None:
        """BaseStep.verify_output() returns passed=True by default."""
        from sova.core.steps.base import BaseStep, GateCheckResult, StepResult

        class SimpleStep(BaseStep):
            name = "simple"

            async def execute(self, ctx: ExecutionContext) -> StepResult:
                return StepResult(success=True, summary="ok")

            async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

        step = SimpleStep()
        result = await step.verify_output(ctx)
        assert result.passed is True
        assert result.reason is None

    async def test_base_step_can_skip_with_completed_step(self, ctx: ExecutionContext) -> None:
        """can_skip returns True when step name is in completed_steps."""
        from sova.core.steps.base import BaseStep, GateCheckResult, StepResult

        class SkippableStep(BaseStep):
            name = "already_done"

            async def execute(self, ctx: ExecutionContext) -> StepResult:
                return StepResult(success=True, summary="ok")

            async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

        step = SkippableStep()
        ctx.completed_steps = {"already_done"}
        assert await step.can_skip(ctx) is True

    async def test_base_step_can_skip_without_completed_step(self, ctx: ExecutionContext) -> None:
        """can_skip returns False when step name is not in completed_steps."""
        from sova.core.steps.base import BaseStep, GateCheckResult, StepResult

        class FreshStep(BaseStep):
            name = "not_done"

            async def execute(self, ctx: ExecutionContext) -> StepResult:
                return StepResult(success=True, summary="ok")

            async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

        step = FreshStep()
        ctx.completed_steps = {"other_step"}
        assert await step.can_skip(ctx) is False

    async def test_verify_called_after_validate_passes(self, ctx: ExecutionContext) -> None:
        """verify_output() is called when validate_output() passes."""
        from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
        from sova.core.workflow import StepRecord

        call_order: list[str] = []

        class TrackingStep(BaseStep):
            name = "tracking"

            async def execute(self, ctx: ExecutionContext) -> StepResult:
                return StepResult(success=True, summary="ok")

            async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
                call_order.append("validate")
                return GateCheckResult(passed=True)

            async def verify_output(self, ctx: ExecutionContext) -> GateCheckResult:
                call_order.append("verify")
                return GateCheckResult(passed=True)

        step = TrackingStep()
        record = StepRecord(step_name="tracking", status="running")
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        with patch.object(engine, "_update_step_execution_gate", new_callable=AsyncMock):
            status = await engine._validate_step_gate(step, 1, record)

        assert status == "done"
        assert call_order == ["validate", "verify"]
        assert record.gate is not None
        assert record.gate.passed is True

    async def test_verify_not_called_when_validate_fails(self, ctx: ExecutionContext) -> None:
        """verify_output() is NOT called when validate_output() fails."""
        from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
        from sova.core.workflow import StepRecord

        verify_called = False

        class FailGateStep(BaseStep):
            name = "fail_gate"

            async def execute(self, ctx: ExecutionContext) -> StepResult:
                return StepResult(success=True, summary="ok")

            async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=False, reason="structural failure")

            async def verify_output(self, ctx: ExecutionContext) -> GateCheckResult:
                nonlocal verify_called
                verify_called = True
                return GateCheckResult(passed=True)

        step = FailGateStep()
        record = StepRecord(step_name="fail_gate", status="running")
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        with patch.object(engine, "_update_step_execution_gate", new_callable=AsyncMock):
            status = await engine._validate_step_gate(step, 1, record)

        assert status == "failed"
        assert verify_called is False

    async def test_verify_failure_returns_failed(self, ctx: ExecutionContext) -> None:
        """When verify_output() fails, the overall gate returns 'failed'."""
        from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
        from sova.core.workflow import StepRecord

        class VerifyFailStep(BaseStep):
            name = "verify_fail"

            async def execute(self, ctx: ExecutionContext) -> StepResult:
                return StepResult(success=True, summary="ok")

            async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

            async def verify_output(self, ctx: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=False, reason="regression detected")

        step = VerifyFailStep()
        record = StepRecord(step_name="verify_fail", status="running")
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        with patch.object(engine, "_update_step_execution_gate", new_callable=AsyncMock):
            status = await engine._validate_step_gate(step, 1, record)

        assert status == "failed"
        assert record.gate is not None
        assert record.gate.passed is False
        assert "regression detected" in record.gate.reason

    async def test_verify_timeout_returns_failed(self, ctx: ExecutionContext) -> None:
        """When verify_output() exceeds its timeout, returns 'failed'."""
        import asyncio as aio

        from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
        from sova.core.workflow import StepRecord

        class SlowVerifyStep(BaseStep):
            name = "slow_verify"

            async def execute(self, ctx: ExecutionContext) -> StepResult:
                return StepResult(success=True, summary="ok")

            async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

            async def verify_output(self, ctx: ExecutionContext) -> GateCheckResult:
                await aio.sleep(100)
                return GateCheckResult(passed=True)

        step = SlowVerifyStep()
        record = StepRecord(step_name="slow_verify", status="running")
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        with patch.object(engine, "_step_timeout", return_value=1):
            with patch.object(engine, "_update_step_execution_gate", new_callable=AsyncMock):
                status = await engine._validate_step_gate(step, 1, record)

        assert status == "failed"
        assert record.gate is not None
        assert record.gate.passed is False
        assert "timed out" in record.gate.reason.lower()

    async def test_verify_exception_returns_failed(self, ctx: ExecutionContext) -> None:
        """When verify_output() raises, returns 'failed' with exception info."""
        from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
        from sova.core.workflow import StepRecord

        class CrashVerifyStep(BaseStep):
            name = "crash_verify"

            async def execute(self, ctx: ExecutionContext) -> StepResult:
                return StepResult(success=True, summary="ok")

            async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

            async def verify_output(self, ctx: ExecutionContext) -> GateCheckResult:
                raise RuntimeError("test suite crashed")

        step = CrashVerifyStep()
        record = StepRecord(step_name="crash_verify", status="running")
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        with patch.object(engine, "_update_step_execution_gate", new_callable=AsyncMock):
            status = await engine._validate_step_gate(step, 1, record)

        assert status == "failed"
        assert record.gate.passed is False
        assert "RuntimeError" in record.gate.reason
        assert "test suite crashed" in record.gate.reason


class TestGateTimeoutConfig:
    """Test that gate_timeout and verify_timeout config knobs work."""

    @pytest.fixture
    def mock_adapter(self) -> MagicMock:
        adapter = MagicMock()
        adapter.repo = "test/repo"
        return adapter

    def _make_ctx(
        self,
        tmp_path: Path,
        mock_adapter: MagicMock,
        seed_config,
        extra: dict | None = None,
    ) -> ExecutionContext:
        from sova.config.loader import load_config

        seed_config(tmp_path, {"github_repo": "test/repo", **(extra or {})})
        cfg = load_config(tmp_path)
        return ExecutionContext(
            project_dir=tmp_path,
            config=cfg,
            adapter=mock_adapter,
            issue_number="123",
            role="developer",
        )

    def test_gate_timeout_default(self, tmp_path: Path, mock_adapter: MagicMock, seed_config) -> None:
        """Default gate_timeout is 60."""
        ctx = self._make_ctx(tmp_path, mock_adapter, seed_config)
        assert ctx.config.validation.gate_timeout == 60

    def test_gate_timeout_custom(self, tmp_path: Path, mock_adapter: MagicMock, seed_config) -> None:
        """gate_timeout can be set via config."""
        ctx = self._make_ctx(tmp_path, mock_adapter, seed_config, {"validation": {"gate_timeout": 30}})
        assert ctx.config.validation.gate_timeout == 30

    def test_verify_timeout_default(self, tmp_path: Path, mock_adapter: MagicMock, seed_config) -> None:
        """Default verify_timeout is 0 (use full step timeout)."""
        ctx = self._make_ctx(tmp_path, mock_adapter, seed_config)
        assert ctx.config.validation.verify_timeout == 0

    def test_verify_timeout_custom(self, tmp_path: Path, mock_adapter: MagicMock, seed_config) -> None:
        """verify_timeout can be set via config."""
        ctx = self._make_ctx(tmp_path, mock_adapter, seed_config, {"validation": {"verify_timeout": 600}})
        assert ctx.config.validation.verify_timeout == 600

    async def test_gate_timeout_used_in_validate_step_gate(
        self, tmp_path: Path, mock_adapter: MagicMock, seed_config
    ) -> None:
        """_validate_step_gate uses config gate_timeout instead of hardcoded 60."""
        import asyncio as aio

        from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
        from sova.core.workflow import StepRecord

        class SlowGateStep(BaseStep):
            name = "slow_gate"

            async def execute(self, ctx: ExecutionContext) -> StepResult:
                return StepResult(success=True, summary="ok")

            async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
                await aio.sleep(100)
                return GateCheckResult(passed=True)

            async def verify_output(self, ctx: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

        ctx = self._make_ctx(tmp_path, mock_adapter, seed_config, {"validation": {"gate_timeout": 1}})
        step = SlowGateStep()
        record = StepRecord(step_name="slow_gate", status="running")
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        with patch.object(engine, "_update_step_execution_gate", new_callable=AsyncMock):
            status = await engine._validate_step_gate(step, 1, record)

        assert status == "failed"
        assert "timed out" in record.gate.reason.lower()

    async def test_gate_timeout_clamped_by_step_timeout(
        self, tmp_path: Path, mock_adapter: MagicMock, seed_config
    ) -> None:
        """When gate_timeout > step_timeout, the smaller step_timeout is used."""
        import asyncio as aio_mod

        from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
        from sova.core.workflow import StepRecord

        captured_timeout: int | None = None
        real_timeout = aio_mod.timeout

        class InspectStep(BaseStep):
            name = "inspect"

            async def execute(self, ctx: ExecutionContext) -> StepResult:
                return StepResult(success=True, summary="ok")

            async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

            async def verify_output(self, ctx: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

        ctx = self._make_ctx(tmp_path, mock_adapter, seed_config, {"validation": {"gate_timeout": 120}})
        step = InspectStep()
        record = StepRecord(step_name="inspect", status="running")
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        def capture_timeout(seconds):
            nonlocal captured_timeout
            captured_timeout = seconds
            return real_timeout(seconds)

        with patch.object(engine, "_step_timeout", return_value=30):
            with patch.object(engine, "_update_step_execution_gate", new_callable=AsyncMock):
                with patch("sova.core.workflow.asyncio.timeout", side_effect=capture_timeout):
                    await engine._validate_step_gate(step, 1, record)

        assert captured_timeout == 30

    async def test_verify_timeout_zero_uses_step_timeout(
        self, tmp_path: Path, mock_adapter: MagicMock, seed_config
    ) -> None:
        """When verify_timeout=0, verification uses the full step timeout."""
        import asyncio as aio_mod

        from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
        from sova.core.workflow import StepRecord

        captured_timeout: int | None = None
        real_timeout = aio_mod.timeout

        class InspectStep(BaseStep):
            name = "inspect"

            async def execute(self, ctx: ExecutionContext) -> StepResult:
                return StepResult(success=True, summary="ok")

            async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

            async def verify_output(self, ctx: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

        ctx = self._make_ctx(tmp_path, mock_adapter, seed_config, {"validation": {"verify_timeout": 0}})
        step = InspectStep()
        record = StepRecord(step_name="inspect", status="running")
        engine = WorkflowEngine(steps=[step], ctx=ctx)
        step_timeout = engine._step_timeout(step.name)

        def capture_timeout(seconds):
            nonlocal captured_timeout
            captured_timeout = seconds
            return real_timeout(seconds)

        with patch.object(engine, "_update_step_execution_gate", new_callable=AsyncMock):
            with patch("sova.core.workflow.asyncio.timeout", side_effect=capture_timeout):
                await engine._verify_step_output(step, 1, record)

        assert captured_timeout == step_timeout

    async def test_verify_timeout_nonzero_clamps_to_min(
        self, tmp_path: Path, mock_adapter: MagicMock, seed_config
    ) -> None:
        """When verify_timeout > 0, uses min(step_timeout, verify_timeout)."""
        import asyncio as aio_mod

        from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
        from sova.core.workflow import StepRecord

        captured_timeout: int | None = None
        real_timeout = aio_mod.timeout

        class InspectStep(BaseStep):
            name = "inspect"

            async def execute(self, ctx: ExecutionContext) -> StepResult:
                return StepResult(success=True, summary="ok")

            async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

            async def verify_output(self, ctx: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

        ctx = self._make_ctx(tmp_path, mock_adapter, seed_config, {"validation": {"verify_timeout": 300}})
        step = InspectStep()
        record = StepRecord(step_name="inspect", status="running")
        engine = WorkflowEngine(steps=[step], ctx=ctx)

        def capture_timeout(seconds):
            nonlocal captured_timeout
            captured_timeout = seconds
            return real_timeout(seconds)

        with patch.object(engine, "_update_step_execution_gate", new_callable=AsyncMock):
            with patch("sova.core.workflow.asyncio.timeout", side_effect=capture_timeout):
                await engine._verify_step_output(step, 1, record)

        assert captured_timeout == 300


class TestValidateStepVerifyOutput:
    """Test that ValidateStep.verify_output() runs regression checks."""

    @pytest.fixture
    def mock_adapter(self) -> MagicMock:
        adapter = MagicMock()
        adapter.repo = "test/repo"
        return adapter

    @pytest.fixture
    def ctx(self, tmp_path: Path, mock_adapter: MagicMock, seed_config) -> ExecutionContext:
        from sova.config.loader import load_config

        seed_config(tmp_path, github_repo="test/repo")
        cfg = load_config(tmp_path)
        return ExecutionContext(
            project_dir=tmp_path,
            config=cfg,
            adapter=mock_adapter,
            issue_number="123",
            role="developer",
        )

    async def test_verify_output_passes_when_no_baseline(self, ctx: ExecutionContext) -> None:
        """When there's no baseline, verify_output passes."""
        from sova.core.steps.validate import ValidateStep

        step = ValidateStep()
        ctx.test_baseline_path = None

        result = await step.verify_output(ctx)
        assert result.passed is True

    async def test_verify_output_calls_check_regressions(self, ctx: ExecutionContext) -> None:
        """verify_output delegates to _check_regressions."""
        from sova.core.steps.base import GateCheckResult
        from sova.core.steps.validate import ValidateStep

        step = ValidateStep()

        with patch.object(
            step,
            "_check_regressions",
            new_callable=AsyncMock,
            return_value=GateCheckResult(passed=False, reason="regressions found"),
        ):
            result = await step.verify_output(ctx)

        assert result.passed is False
        assert "regressions found" in result.reason

    async def test_validate_output_no_longer_checks_regressions(self, ctx: ExecutionContext) -> None:
        """validate_output no longer calls _check_regressions (moved to verify_output)."""
        from sova.core.steps.validate import ValidateStep

        step = ValidateStep()

        with patch("sova.core.steps.validate.run", new_callable=AsyncMock) as mock_run:
            from sova.utils.shell import ShellResult

            clean_result = ShellResult(returncode=0, stdout="", stderr="")
            mock_run.side_effect = [
                ShellResult(returncode=0, stdout="abc123 some commit", stderr=""),  # git log
                clean_result,  # git diff --stat HEAD (unstaged)
                clean_result,  # git diff --cached --stat (staged)
            ]

            with patch.object(step, "_check_regressions", new_callable=AsyncMock) as mock_regression:
                result = await step.validate_output(ctx)

            mock_regression.assert_not_called()

        assert result.passed is True

    async def test_validate_output_fails_on_unstaged_changes(self, ctx: ExecutionContext) -> None:
        """validate_output fails when unstaged changes exist after validation."""
        from sova.core.steps.validate import ValidateStep

        step = ValidateStep()

        with patch("sova.core.steps.validate.run", new_callable=AsyncMock) as mock_run:
            from sova.utils.shell import ShellResult

            mock_run.side_effect = [
                ShellResult(returncode=0, stdout="abc123 some commit", stderr=""),  # git log
                ShellResult(returncode=0, stdout=" src/foo.py | 2 +-\n", stderr=""),  # unstaged diff
            ]

            result = await step.validate_output(ctx)

        assert result.passed is False
        assert "Unstaged changes" in result.reason

    async def test_validate_output_fails_on_staged_changes(self, ctx: ExecutionContext) -> None:
        """validate_output fails when staged but uncommitted changes exist."""
        from sova.core.steps.validate import ValidateStep

        step = ValidateStep()

        with patch("sova.core.steps.validate.run", new_callable=AsyncMock) as mock_run:
            from sova.utils.shell import ShellResult

            clean_result = ShellResult(returncode=0, stdout="", stderr="")
            mock_run.side_effect = [
                ShellResult(returncode=0, stdout="abc123 some commit", stderr=""),  # git log
                clean_result,  # git diff --stat HEAD (unstaged, clean)
                ShellResult(returncode=0, stdout=" src/bar.py | 1 +\n", stderr=""),  # staged diff
            ]

            result = await step.validate_output(ctx)

        assert result.passed is False
        assert "Staged but uncommitted" in result.reason

    async def test_verify_output_returns_none_regression_as_pass(self, ctx: ExecutionContext) -> None:
        """When _check_regressions returns None, verify_output passes."""
        from sova.core.steps.validate import ValidateStep

        step = ValidateStep()

        with patch.object(
            step,
            "_check_regressions",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await step.verify_output(ctx)

        assert result.passed is True
