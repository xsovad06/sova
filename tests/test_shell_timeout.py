"""Tests for shell command timeout and kill handling."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_proc(*, pid: int = 12345, returncode: int | None = None, communicate: object = None) -> AsyncMock:
    """A child whose communicate() raises ``communicate`` and whose wait() returns at once."""
    proc = AsyncMock()
    proc.pid = pid
    proc.returncode = returncode
    proc.communicate = AsyncMock(side_effect=communicate)
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


class TestShellKillTimeout:
    """Test that shell.run kills the whole process group and handles unkillable processes gracefully."""

    async def test_run_handles_kill_timeout(self) -> None:
        """When process.wait() hangs after SIGKILL (uninterruptible I/O), the bounded wait gives up and logs it.

        The kill bounds are patched down from seconds: the bounds are what is under test, not their length.
        """
        from sova.utils.shell import run

        mock_proc = _mock_proc(communicate=TimeoutError)

        async def hanging_wait() -> None:
            await asyncio.sleep(100)

        mock_proc.wait = AsyncMock(side_effect=hanging_wait)

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("os.killpg") as mock_killpg,
            patch("sova.utils.shell._TERM_GRACE_SECONDS", 0.01),
            patch("sova.utils.shell._KILL_TIMEOUT_SECONDS", 0.05),
            patch("sova.utils.shell.log") as mock_log,
        ):
            result = await run("test_command", timeout=0.01)

        assert result.returncode == -1
        assert "timed out" in result.stderr.lower()
        mock_log.warning.assert_called_once_with("shell.kill_timeout", cmd="test_command", pid=12345)
        # The leader never exits, so the grace period expires and SIGKILL follows SIGTERM.
        assert [c.args for c in mock_killpg.call_args_list] == [(12345, signal.SIGTERM), (12345, signal.SIGKILL)]

    async def test_run_starts_child_in_its_own_session(self) -> None:
        from sova.utils.shell import run

        mock_proc = _mock_proc(returncode=0)
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            await run("test_command")

        assert mock_exec.call_args.kwargs["start_new_session"] is True

    async def test_timeout_terminates_then_kills_the_group(self) -> None:
        """SIGTERM first, then an unconditional group SIGKILL, even though the leader exited on SIGTERM."""
        from sova.utils.shell import run

        mock_proc = _mock_proc(communicate=TimeoutError)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc), patch("os.killpg") as mock_killpg:
            result = await run("test_command", timeout=1)

        assert [c.args for c in mock_killpg.call_args_list] == [(12345, signal.SIGTERM), (12345, signal.SIGKILL)]
        mock_proc.kill.assert_not_called()
        assert mock_proc.wait.await_count == 2
        assert result.returncode == -1
        assert result.timed_out is True
        assert result.stderr == "Command timed out after 1s"

    async def test_timeout_group_already_gone_is_swallowed(self) -> None:
        """ProcessLookupError and PermissionError from killpg (a finished or zombie-led group) are not raised."""
        from sova.utils.shell import run

        mock_proc = _mock_proc(communicate=TimeoutError)

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("os.killpg", side_effect=[ProcessLookupError, PermissionError]),
        ):
            result = await run("test_command", timeout=1)

        assert result.timed_out is True

    async def test_timeout_with_exited_leader_still_kills_the_group(self) -> None:
        """An exited leader skips SIGTERM, but survivors in its group still get SIGKILL."""
        from sova.utils.shell import run

        mock_proc = _mock_proc(returncode=0, communicate=TimeoutError)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc), patch("os.killpg") as mock_killpg:
            await run("test_command", timeout=1)

        assert [c.args for c in mock_killpg.call_args_list] == [(12345, signal.SIGKILL)]

    async def test_run_kills_process_group_on_cancelled_error(self) -> None:
        """When an outer scope cancels the task, shell.run SIGKILLs the child's group at once and re-raises."""
        from sova.utils.shell import run

        mock_proc = _mock_proc(communicate=asyncio.CancelledError)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc), patch("os.killpg") as mock_killpg:
            with pytest.raises(asyncio.CancelledError):
                await run("long_running_test", timeout=300)

        assert [c.args for c in mock_killpg.call_args_list] == [(12345, signal.SIGKILL)]
        mock_proc.wait.assert_awaited_once()

    async def test_cancel_during_grace_wait_still_kills_the_group(self) -> None:
        """A cancellation while waiting for the leader after SIGTERM must not leave the group alive."""
        from sova.utils.shell import _kill_process_group

        mock_proc = _mock_proc()
        mock_proc.wait = AsyncMock(side_effect=asyncio.CancelledError)

        with patch("os.killpg") as mock_killpg:
            with pytest.raises(asyncio.CancelledError):
                await _kill_process_group(mock_proc, "cmd", grace=1, event="shell.kill_timeout")

        assert [c.args for c in mock_killpg.call_args_list] == [(12345, signal.SIGTERM), (12345, signal.SIGKILL)]

    @pytest.mark.parametrize("pid", [None, 0, -5])
    async def test_invalid_pid_never_reaches_killpg(self, pid: int | None) -> None:
        """A missing or non-positive pid (0 would signal our own group) falls back to proc.kill() for SIGKILL."""
        from sova.utils.shell import run

        mock_proc = _mock_proc(pid=pid, communicate=TimeoutError)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc), patch("os.killpg") as mock_killpg:
            result = await run("test_command", timeout=1)

        mock_killpg.assert_not_called()
        mock_proc.kill.assert_called_once_with()
        assert result.timed_out is True

    async def test_permission_error_is_logged(self) -> None:
        from sova.utils.shell import _signal_group

        mock_proc = _mock_proc()

        with patch("os.killpg", side_effect=PermissionError), patch("sova.utils.shell.log") as mock_log:
            _signal_group(mock_proc, signal.SIGKILL)

        mock_log.debug.assert_called_once_with("shell.killpg_denied", pgid=12345, sig="SIGKILL")

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

    async def test_run_nonzero_exit_returns_code_and_logs_failure(self) -> None:
        """A failing command keeps its exit code and output, logs it, and sends no signal to its group."""
        from sova.utils.shell import run

        mock_proc = _mock_proc(returncode=3)
        mock_proc.communicate = AsyncMock(return_value=(b"partial", b"boom"))

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("os.killpg") as mock_killpg,
            patch("sova.utils.shell.log") as mock_log,
        ):
            result = await run("failing_command", timeout=10)

        assert result.returncode == 3
        assert result.stdout == "partial"
        assert result.stderr == "boom"
        assert result.timed_out is False
        mock_killpg.assert_not_called()
        mock_log.debug.assert_any_call("shell.failed", cmd="failing_command", returncode=3, stderr="boom")


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


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX-only")
class TestShellKillsDescendants:
    """Real processes: a timeout must take the whole tree down, not only the direct child."""

    @staticmethod
    def _assert_dead(pid: int) -> None:
        """Assert ``pid`` is gone, allowing a moment for init to reap the killed orphan; kill a survivor."""
        for _ in range(40):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            # A killed orphan stays a zombie until its new parent reaps it (a container
            # without an init); a zombie is dead for this purpose.
            stat = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
            ).stdout.strip()
            if stat.startswith("Z"):
                return
            time.sleep(0.05)
        os.kill(pid, signal.SIGKILL)
        pytest.fail(f"grandchild {pid} survived the timeout")

    async def _run_and_read_grandchild(self, script: str, tmp_path: Path) -> int:
        from sova.utils.shell import run

        pid_file = tmp_path / "grandchild.pid"
        result = await run("sh", "-c", script, "sh", str(pid_file), timeout=1)
        assert result.timed_out is True
        return int(pid_file.read_text(encoding="utf-8").strip())

    async def test_timeout_kills_grandchild(self, tmp_path: Path) -> None:
        pid = await self._run_and_read_grandchild('sleep 60 & echo $! > "$1"; wait', tmp_path)
        self._assert_dead(pid)

    async def test_timeout_kills_grandchild_that_ignores_sigterm(self, tmp_path: Path) -> None:
        """The leader dies on SIGTERM but the descendant ignores it: only the group SIGKILL reaches it."""
        script = '(trap "" TERM; sleep 60) & echo $! > "$1"; wait'
        with patch("sova.utils.shell._TERM_GRACE_SECONDS", 0.2):
            pid = await self._run_and_read_grandchild(f"trap 'exit 0' TERM; {script}", tmp_path)
        self._assert_dead(pid)

    async def test_timeout_kills_group_that_ignores_sigterm(self, tmp_path: Path) -> None:
        """The whole group ignores SIGTERM, so the full grace period expires and only SIGKILL ends it."""
        script = 'trap "" TERM; sleep 60 & echo $! > "$1"; wait'
        with patch("sova.utils.shell._TERM_GRACE_SECONDS", 0.2):
            pid = await self._run_and_read_grandchild(script, tmp_path)
        self._assert_dead(pid)
