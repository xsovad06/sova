"""Tests for shell command timeout and kill handling."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestShellKillTimeout:
    """Test that shell.run handles unkillable processes gracefully."""

    async def test_run_handles_kill_timeout(self) -> None:
        """When process.wait() hangs after SIGKILL, timeout prevents indefinite hang."""
        from sova.utils.shell import run

        # Mock a process that times out, then hangs on wait() after kill
        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_proc.communicate = AsyncMock(side_effect=TimeoutError)
        mock_proc.kill = MagicMock()

        # Simulate wait() hanging forever (simulate uninterruptible I/O)
        async def hanging_wait():
            import asyncio

            await asyncio.sleep(100)  # Will be interrupted by timeout

        mock_proc.wait = AsyncMock(side_effect=hanging_wait)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await run("test_command", timeout=1)

        # Process should be killed
        mock_proc.kill.assert_called_once()

        # Result should indicate timeout
        assert result.returncode == -1
        assert "timed out" in result.stderr.lower()

    async def test_run_normal_timeout_without_kill_hang(self) -> None:
        """Normal timeout case where process exits cleanly after SIGKILL."""
        from sova.utils.shell import run

        # Mock a process that times out but exits cleanly when killed
        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_proc.communicate = AsyncMock(side_effect=TimeoutError)
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()  # Returns immediately

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await run("test_command", timeout=1)

        # Process should be killed and waited on
        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_called_once()

        # Result should indicate timeout
        assert result.returncode == -1
        assert "timed out" in result.stderr.lower()

    async def test_run_success_no_timeout(self) -> None:
        """Normal successful execution without timeout."""
        from sova.utils.shell import run

        # Mock a successful process
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"output", b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await run("test_command", timeout=10)

        # Process should not be killed
        assert not hasattr(mock_proc, "kill") or not mock_proc.kill.called

        # Result should be successful
        assert result.returncode == 0
        assert result.stdout == "output"
        assert result.stderr == ""

    async def test_run_kills_process_on_cancelled_error(self) -> None:
        """When an outer scope cancels the task, shell.run kills the child process."""
        from sova.utils.shell import run

        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_proc.communicate = AsyncMock(side_effect=asyncio.CancelledError)
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(asyncio.CancelledError):
                await run("long_running_test", timeout=300)

        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_called_once()


class TestWriteStdinAndClose:
    """Test write_stdin_and_close's happy path and swallowed-error paths."""

    async def test_noop_when_stdin_is_none(self) -> None:
        """Inherited stdin (or a test double with none configured) is a no-op."""
        from sova.utils.shell import write_stdin_and_close

        mock_proc = MagicMock()
        mock_proc.stdin = None

        await write_stdin_and_close(mock_proc, "hello")

    async def test_writes_drains_and_closes_on_success(self) -> None:
        from sova.utils.shell import write_stdin_and_close

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.drain = AsyncMock()

        await write_stdin_and_close(mock_proc, "hello")

        mock_proc.stdin.write.assert_called_once_with(b"hello")
        mock_proc.stdin.drain.assert_awaited_once()
        mock_proc.stdin.close.assert_called_once()

    async def test_broken_pipe_is_swallowed_and_child_is_killed(self) -> None:
        """A pipe that breaks before the write completes is logged, not raised.

        Delivery could not be confirmed, so the child (which may have only
        received a truncated prompt) is killed and reaped rather than left
        running, matching ``run()``'s own timeout-kill pattern.
        """
        from sova.utils.shell import write_stdin_and_close

        mock_proc = MagicMock()
        mock_proc.pid = 999
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.drain = AsyncMock(side_effect=BrokenPipeError)
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        await write_stdin_and_close(mock_proc, "hello")

        mock_proc.stdin.close.assert_called_once()
        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_awaited_once()

    async def test_connection_reset_is_swallowed_and_child_is_killed(self) -> None:
        from sova.utils.shell import write_stdin_and_close

        mock_proc = MagicMock()
        mock_proc.pid = 999
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.drain = AsyncMock(side_effect=ConnectionResetError)
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        await write_stdin_and_close(mock_proc, "hello")

        mock_proc.stdin.close.assert_called_once()
        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_awaited_once()

    async def test_timeout_is_swallowed_and_child_is_killed(self) -> None:
        """A write that never drains (child not reading stdin) times out, not hangs."""
        from sova.utils.shell import write_stdin_and_close

        mock_proc = MagicMock()
        mock_proc.pid = 999
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.drain = AsyncMock(side_effect=TimeoutError)
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        await write_stdin_and_close(mock_proc, "hello")

        mock_proc.stdin.close.assert_called_once()
        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_awaited_once()

    async def test_kill_already_dead_process_is_a_noop(self) -> None:
        """A process that already exited (e.g. the broken pipe's own cause) is fine to kill twice."""
        from sova.utils.shell import write_stdin_and_close

        mock_proc = MagicMock()
        mock_proc.pid = 999
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.drain = AsyncMock(side_effect=BrokenPipeError)
        mock_proc.kill = MagicMock(side_effect=ProcessLookupError)
        mock_proc.wait = AsyncMock()

        await write_stdin_and_close(mock_proc, "hello")

        mock_proc.stdin.close.assert_called_once()
        mock_proc.wait.assert_not_awaited()

    async def test_cancelled_error_kills_child_and_still_propagates(self) -> None:
        """Cancellation must never be swallowed, but the child must not leak untracked.

        The caller has not yet received a process wrapper (``AgentProcess`` /
        ``FileAgentProcess``) when cancellation interrupts the drain, so this
        is the only place that can clean up the already-spawned child.
        """
        from sova.utils.shell import write_stdin_and_close

        mock_proc = MagicMock()
        mock_proc.pid = 999
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.drain = AsyncMock(side_effect=asyncio.CancelledError)
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        with pytest.raises(asyncio.CancelledError):
            await write_stdin_and_close(mock_proc, "hello")

        mock_proc.stdin.close.assert_called_once()
        mock_proc.kill.assert_called_once()
        mock_proc.wait.assert_awaited_once()
