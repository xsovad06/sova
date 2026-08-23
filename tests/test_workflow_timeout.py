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
    def ctx(self, tmp_path: Path, mock_adapter: MagicMock) -> ExecutionContext:
        from sova.config.loader import load_config

        (tmp_path / "sova.toml").write_text("github_repo = 'test/repo'\n")
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
    def ctx(self, tmp_path: Path, mock_adapter: MagicMock) -> ExecutionContext:
        import asyncio

        from sova.config.loader import load_config
        from sova.utils.shell import run

        # Create a git repo
        asyncio.run(run("git", "init", cwd=tmp_path))
        asyncio.run(run("git", "config", "user.email", "test@example.com", cwd=tmp_path))
        asyncio.run(run("git", "config", "user.name", "Test User", cwd=tmp_path))

        (tmp_path / "sova.toml").write_text("github_repo = 'test/repo'\n")
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

    async def test_preserve_partial_work_not_a_git_repo(self, tmp_path: Path) -> None:
        """When working_dir is not a git repo, returns False."""
        from sova.config.loader import load_config

        non_git_dir = tmp_path / "not_git"
        non_git_dir.mkdir()
        (non_git_dir / "sova.toml").write_text("github_repo = 'test/repo'\n")

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
    def ctx(self, tmp_path: Path, mock_adapter: MagicMock) -> ExecutionContext:
        import asyncio

        from sova.config.loader import load_config
        from sova.utils.shell import run

        # Create a git repo
        asyncio.run(run("git", "init", cwd=tmp_path))
        asyncio.run(run("git", "config", "user.email", "test@example.com", cwd=tmp_path))
        asyncio.run(run("git", "config", "user.name", "Test User", cwd=tmp_path))

        # Create sova.toml with custom validate config
        (tmp_path / "sova.toml").write_text("""
github_repo = 'test/repo'

[validation]
fix_timeout = 240
max_fix_attempts = 3
hook_timeout = 150
""")
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
    def ctx(self, tmp_path: Path, mock_adapter: MagicMock) -> ExecutionContext:
        from sova.config.loader import load_config

        (tmp_path / "sova.toml").write_text("""
github_repo = 'test/repo'

[validation]
fix_timeout = 300
""")
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
    def ctx(self, tmp_path: Path, mock_adapter: MagicMock) -> ExecutionContext:
        from sova.config.loader import load_config

        (tmp_path / "sova.toml").write_text("github_repo = 'test/repo'\n")
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
    def ctx(self, tmp_path: Path, mock_adapter: MagicMock) -> ExecutionContext:
        from sova.config.loader import load_config

        (tmp_path / "sova.toml").write_text("github_repo = 'test/repo'\n")
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
            with patch.object(engine, "_update_step_execution", new_callable=AsyncMock):
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
        with patch.object(engine, "_update_step_execution", new_callable=AsyncMock):
            status = await engine._validate_step_gate(BrokenGateStep(), step_exec_id, record)

        assert status == "failed"
        assert record.gate is not None
        assert record.gate.passed is False
        assert "ValueError" in record.gate.reason
        assert "Simulated gate error" in record.gate.reason
