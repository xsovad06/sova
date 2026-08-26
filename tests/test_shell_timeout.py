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
