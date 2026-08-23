"""Tests for worktree deletion handling (issue #838)."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.config.models import ProjectConfig
from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.core.workflow import WorkflowEngine


@pytest.fixture
def mock_adapter() -> MagicMock:
    """Create a mock TaskAdapter for tests."""
    adapter = MagicMock()
    adapter.repo = "test/repo"
    return adapter


async def _setup_git_repo(tmp_path: Path) -> ProjectConfig:
    """Initialize a git repo with minimal config and return loaded config."""
    from sova.config.loader import load_config
    from sova.utils.shell import run

    await run("git", "init", cwd=tmp_path)
    await run("git", "config", "user.email", "test@example.com", cwd=tmp_path)
    await run("git", "config", "user.name", "Test User", cwd=tmp_path)

    (tmp_path / "sova.toml").write_text("github_repo = 'test/repo'\n")
    return load_config(tmp_path)


class TestWorktreeExistenceCheck:
    """Test that _execute_step checks for worktree existence before running."""

    @pytest.fixture
    async def ctx_with_worktree(self, tmp_path: Path, mock_adapter: MagicMock) -> ExecutionContext:
        from sova.utils.shell import run

        cfg = await _setup_git_repo(tmp_path)

        # Create worktree directory
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        await run("git", "init", cwd=worktree)

        ctx = ExecutionContext(
            project_dir=tmp_path,
            config=cfg,
            adapter=mock_adapter,
            issue_number="123",
            role="developer",
        )
        ctx.worktree_dir = worktree
        return ctx

    async def test_step_fails_when_worktree_deleted(self, ctx_with_worktree: ExecutionContext, tmp_path: Path) -> None:
        """When worktree is deleted before step execution, step fails cleanly."""

        class DummyStep(BaseStep):
            name = "dummy"

            async def execute(self, ctx: ExecutionContext) -> StepResult:
                return StepResult(success=True, summary="Should not execute")

            async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

        # Delete the worktree
        shutil.rmtree(ctx_with_worktree.worktree_dir)

        engine = WorkflowEngine(steps=[DummyStep()], ctx=ctx_with_worktree)
        result = await engine.run()

        assert result.success is False
        assert "worktree" in result.error.lower()
        assert "does not exist" in result.error.lower()
        assert result.steps_completed == 0

    async def test_step_runs_when_no_worktree_set(self, tmp_path: Path, mock_adapter: MagicMock) -> None:
        """Steps before create_worktree run normally (ctx.worktree_dir is None)."""
        cfg = await _setup_git_repo(tmp_path)

        ctx = ExecutionContext(
            project_dir=tmp_path,
            config=cfg,
            adapter=mock_adapter,
            issue_number="123",
            role="developer",
        )
        ctx.worktree_dir = None

        class DummyStep(BaseStep):
            name = "dummy"
            executed = False

            async def execute(self, ctx: ExecutionContext) -> StepResult:
                DummyStep.executed = True
                return StepResult(success=True, summary="Executed")

            async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
                return GateCheckResult(passed=True)

        engine = WorkflowEngine(steps=[DummyStep()], ctx=ctx)
        result = await engine.run()

        assert result.success is True
        assert DummyStep.executed is True


class TestGateCheckTimeout:
    """Test that gate checks run within the step timeout scope."""

    @pytest.fixture
    async def ctx(self, tmp_path: Path, mock_adapter: MagicMock) -> ExecutionContext:
        cfg = await _setup_git_repo(tmp_path)
        return ExecutionContext(
            project_dir=tmp_path,
            config=cfg,
            adapter=mock_adapter,
            issue_number="123",
            role="developer",
        )

    async def test_gate_check_times_out(self, ctx: ExecutionContext) -> None:
        """When validate_output hangs, it times out instead of blocking forever."""

        class SlowGateStep(BaseStep):
            name = "slow_gate"

            async def execute(self, ctx: ExecutionContext) -> StepResult:
                return StepResult(success=True, summary="Executed")

            async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
                # Hang indefinitely
                await asyncio.sleep(1000)
                return GateCheckResult(passed=True)

        # Set a very short timeout
        with patch.object(WorkflowEngine, "_step_timeout", return_value=0.1):
            engine = WorkflowEngine(steps=[SlowGateStep()], ctx=ctx)
            result = await engine.run()

        assert result.success is False
        assert result.steps_failed == 1


class TestGateCheckExceptionHandling:
    """Test that gate check exceptions are caught and produce failed gate results."""

    @pytest.fixture
    async def ctx(self, tmp_path: Path, mock_adapter: MagicMock) -> ExecutionContext:
        cfg = await _setup_git_repo(tmp_path)
        return ExecutionContext(
            project_dir=tmp_path,
            config=cfg,
            adapter=mock_adapter,
            issue_number="123",
            role="developer",
        )

    async def test_gate_check_filesystem_exception_caught(self, ctx: ExecutionContext) -> None:
        """When validate_output raises FileNotFoundError, gate fails gracefully."""

        class FailingGateStep(BaseStep):
            name = "failing_gate"

            async def execute(self, ctx: ExecutionContext) -> StepResult:
                return StepResult(success=True, summary="Executed")

            async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
                # Simulate filesystem error during gate check
                raise FileNotFoundError("Worktree deleted during gate check")

        engine = WorkflowEngine(steps=[FailingGateStep()], ctx=ctx)
        result = await engine.run()

        assert result.success is False
        assert result.steps_failed == 1
        # Should record a failed gate, not an exception
        assert result.step_records[0].gate is not None
        assert result.step_records[0].gate.passed is False


class TestShellProcessKillTimeout:
    """Test that proc.wait() after kill has a bounded timeout."""

    async def test_kill_timeout_prevents_hang(self) -> None:
        """When killed process doesn't exit (uninterruptible I/O), wait times out after 5s."""
        from sova.utils.shell import run

        # Create a process that we'll kill
        with (
            patch("asyncio.create_subprocess_exec") as mock_create,
            patch("sova.utils.shell.log") as mock_log,
        ):
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
            mock_proc.kill = MagicMock()
            mock_proc.pid = 12345

            # Simulate a process that never exits after kill by hanging forever
            async def never_exits():
                await asyncio.sleep(1000)

            mock_proc.wait = AsyncMock(side_effect=never_exits)
            mock_proc.returncode = None
            mock_create.return_value = mock_proc

            # Run with a short timeout to trigger the kill path
            result = await run("sleep", "1000", timeout=0.1)

            # Should have called kill
            mock_proc.kill.assert_called_once()

            # Should return a timeout result without hanging
            assert result.returncode == -1
            assert "timed out" in result.stderr.lower()

            # Should log warning about kill timeout
            mock_log.warning.assert_called_once_with("shell.kill_timeout", cmd="sleep", pid=12345)

    async def test_normal_kill_completes_quickly(self) -> None:
        """When process exits normally after kill, wait completes immediately."""
        from sova.utils.shell import run

        with patch("asyncio.create_subprocess_exec") as mock_create:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
            mock_proc.kill = MagicMock()
            # Process exits immediately after kill
            mock_proc.wait = AsyncMock(return_value=None)
            mock_proc.returncode = -9
            mock_create.return_value = mock_proc

            result = await run("sleep", "1000", timeout=0.1)

            mock_proc.kill.assert_called_once()
            mock_proc.wait.assert_called_once()
            assert result.returncode == -1
