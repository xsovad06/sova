"""Tests for FileAgentProcess: file-based stdout/stderr tailing."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from sova.ipc.control import ExitClassification


def _stdin_mock() -> MagicMock:
    """A stdin mock shaped like a real StreamWriter: write/close are sync, drain is async."""
    stdin = MagicMock()
    stdin.drain = AsyncMock()
    return stdin


class TestFileAgentProcessInit:
    def test_stores_file_paths(self, tmp_path: Path) -> None:
        from sova.ipc.control import FileAgentProcess

        stdout_path = tmp_path / "test.stdout"
        stderr_path = tmp_path / "test.stderr"
        stdout_path.touch()
        stderr_path.touch()

        mock_proc = AsyncMock()
        mock_proc.pid = 12345
        mock_proc.returncode = None

        fp = FileAgentProcess(mock_proc, stdout_path=stdout_path, stderr_path=stderr_path)
        assert fp.pid == 12345
        assert fp.stdout_path == stdout_path
        assert fp.stderr_path == stderr_path

    def test_pid_property(self, tmp_path: Path) -> None:
        from sova.ipc.control import FileAgentProcess

        stdout_path = tmp_path / "test.stdout"
        stderr_path = tmp_path / "test.stderr"
        stdout_path.touch()
        stderr_path.touch()

        mock_proc = AsyncMock()
        mock_proc.pid = 42
        mock_proc.returncode = None

        fp = FileAgentProcess(mock_proc, stdout_path=stdout_path, stderr_path=stderr_path)
        assert fp.pid == 42

    def test_is_running_when_alive(self, tmp_path: Path) -> None:
        from sova.ipc.control import FileAgentProcess

        stdout_path = tmp_path / "test.stdout"
        stderr_path = tmp_path / "test.stderr"
        stdout_path.touch()
        stderr_path.touch()

        mock_proc = AsyncMock()
        mock_proc.pid = 42
        mock_proc.returncode = None

        fp = FileAgentProcess(mock_proc, stdout_path=stdout_path, stderr_path=stderr_path)
        assert fp.is_running is True

    def test_is_running_when_exited(self, tmp_path: Path) -> None:
        from sova.ipc.control import FileAgentProcess

        stdout_path = tmp_path / "test.stdout"
        stderr_path = tmp_path / "test.stderr"
        stdout_path.touch()
        stderr_path.touch()

        mock_proc = AsyncMock()
        mock_proc.pid = 42
        mock_proc.returncode = 0

        fp = FileAgentProcess(mock_proc, stdout_path=stdout_path, stderr_path=stderr_path)
        assert fp.is_running is False

    def test_returncode_none_while_running(self, tmp_path: Path) -> None:
        from sova.ipc.control import FileAgentProcess

        stdout_path = tmp_path / "test.stdout"
        stderr_path = tmp_path / "test.stderr"
        stdout_path.touch()
        stderr_path.touch()

        mock_proc = AsyncMock()
        mock_proc.pid = 42
        mock_proc.returncode = None

        fp = FileAgentProcess(mock_proc, stdout_path=stdout_path, stderr_path=stderr_path)
        assert fp.returncode is None

    def test_returncode_after_exit(self, tmp_path: Path) -> None:
        from sova.ipc.control import FileAgentProcess

        stdout_path = tmp_path / "test.stdout"
        stderr_path = tmp_path / "test.stderr"
        stdout_path.touch()
        stderr_path.touch()

        mock_proc = AsyncMock()
        mock_proc.pid = 42
        mock_proc.returncode = 1

        fp = FileAgentProcess(mock_proc, stdout_path=stdout_path, stderr_path=stderr_path)
        assert fp.returncode == 1


class TestFileAgentProcessWait:
    async def test_wait_returns_exit_code(self, tmp_path: Path) -> None:
        from sova.ipc.control import FileAgentProcess

        stdout_path = tmp_path / "test.stdout"
        stderr_path = tmp_path / "test.stderr"
        stdout_path.touch()
        stderr_path.touch()

        mock_proc = AsyncMock()
        mock_proc.pid = 42
        mock_proc.returncode = None
        mock_proc.wait = AsyncMock(return_value=0)

        fp = FileAgentProcess(mock_proc, stdout_path=stdout_path, stderr_path=stderr_path)
        mock_proc.returncode = 0
        code = await fp.wait()
        assert code == 0

    async def test_wait_classified(self, tmp_path: Path) -> None:
        from sova.ipc.control import FileAgentProcess

        stdout_path = tmp_path / "test.stdout"
        stderr_path = tmp_path / "test.stderr"
        stdout_path.touch()
        stderr_path.touch()

        mock_proc = AsyncMock()
        mock_proc.pid = 42
        mock_proc.returncode = None
        mock_proc.wait = AsyncMock(return_value=137)

        fp = FileAgentProcess(mock_proc, stdout_path=stdout_path, stderr_path=stderr_path)
        mock_proc.returncode = 137
        code, classification = await fp.wait_classified()
        assert code == 137
        assert classification == ExitClassification.CRASH


class TestFileAgentProcessStop:
    async def test_stop_terminates_process(self, tmp_path: Path) -> None:
        from sova.ipc.control import FileAgentProcess

        stdout_path = tmp_path / "test.stdout"
        stderr_path = tmp_path / "test.stderr"
        stdout_path.touch()
        stderr_path.touch()

        mock_proc = AsyncMock()
        mock_proc.pid = 42
        mock_proc.returncode = None
        mock_proc.terminate = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.kill = MagicMock()

        fp = FileAgentProcess(mock_proc, stdout_path=stdout_path, stderr_path=stderr_path)
        await fp.stop()

        mock_proc.terminate.assert_called_once()

    async def test_stop_noop_if_already_exited(self, tmp_path: Path) -> None:
        from sova.ipc.control import FileAgentProcess

        stdout_path = tmp_path / "test.stdout"
        stderr_path = tmp_path / "test.stderr"
        stdout_path.touch()
        stderr_path.touch()

        mock_proc = AsyncMock()
        mock_proc.pid = 42
        mock_proc.returncode = 0
        mock_proc.terminate = MagicMock()

        fp = FileAgentProcess(mock_proc, stdout_path=stdout_path, stderr_path=stderr_path)
        await fp.stop()

        mock_proc.terminate.assert_not_called()


class TestFileAgentProcessStdoutLines:
    async def test_tails_existing_lines(self, tmp_path: Path) -> None:
        from sova.ipc.control import FileAgentProcess

        stdout_path = tmp_path / "test.stdout"
        stderr_path = tmp_path / "test.stderr"
        stderr_path.touch()
        stdout_path.write_text("line 1\nline 2\n")

        mock_proc = AsyncMock()
        mock_proc.pid = 42
        mock_proc.returncode = 0

        fp = FileAgentProcess(mock_proc, stdout_path=stdout_path, stderr_path=stderr_path)
        lines: list[str] = []
        async for line in fp.stdout_lines():
            lines.append(line)

        assert lines == ["line 1", "line 2"]

    async def test_tails_new_lines_as_they_appear(self, tmp_path: Path) -> None:
        from sova.ipc.control import FileAgentProcess

        stdout_path = tmp_path / "test.stdout"
        stderr_path = tmp_path / "test.stderr"
        stderr_path.touch()
        stdout_path.touch()

        mock_proc = AsyncMock()
        mock_proc.pid = 42
        mock_proc.returncode = None

        fp = FileAgentProcess(mock_proc, stdout_path=stdout_path, stderr_path=stderr_path)

        collected: list[str] = []
        done = asyncio.Event()

        async def reader() -> None:
            async for line in fp.stdout_lines():
                collected.append(line)
                if len(collected) >= 3:
                    done.set()

        reader_task = asyncio.create_task(reader())

        await asyncio.sleep(0.05)
        with open(stdout_path, "a") as f:
            f.write("first\n")
            f.flush()

        await asyncio.sleep(0.15)
        with open(stdout_path, "a") as f:
            f.write("second\nthird\n")
            f.flush()

        try:
            await asyncio.wait_for(done.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass

        mock_proc.returncode = 0
        reader_task.cancel()
        try:
            await reader_task
        except asyncio.CancelledError:
            pass

        assert "first" in collected
        assert "second" in collected
        assert "third" in collected

    async def test_stops_after_process_exits_and_file_drained(self, tmp_path: Path) -> None:
        from sova.ipc.control import FileAgentProcess

        stdout_path = tmp_path / "test.stdout"
        stderr_path = tmp_path / "test.stderr"
        stderr_path.touch()
        stdout_path.write_text("only line\n")

        mock_proc = AsyncMock()
        mock_proc.pid = 42
        mock_proc.returncode = 0

        fp = FileAgentProcess(mock_proc, stdout_path=stdout_path, stderr_path=stderr_path)
        lines: list[str] = []
        async for line in fp.stdout_lines():
            lines.append(line)

        assert lines == ["only line"]

    async def test_empty_file_with_exited_process(self, tmp_path: Path) -> None:
        from sova.ipc.control import FileAgentProcess

        stdout_path = tmp_path / "test.stdout"
        stderr_path = tmp_path / "test.stderr"
        stdout_path.touch()
        stderr_path.touch()

        mock_proc = AsyncMock()
        mock_proc.pid = 42
        mock_proc.returncode = 0

        fp = FileAgentProcess(mock_proc, stdout_path=stdout_path, stderr_path=stderr_path)
        lines: list[str] = []
        async for line in fp.stdout_lines():
            lines.append(line)

        assert lines == []

    async def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        from sova.ipc.control import FileAgentProcess

        stdout_path = tmp_path / "nonexistent.stdout"
        stderr_path = tmp_path / "test.stderr"
        stderr_path.touch()

        mock_proc = AsyncMock()
        mock_proc.pid = 42
        mock_proc.returncode = 0

        fp = FileAgentProcess(mock_proc, stdout_path=stdout_path, stderr_path=stderr_path)
        lines: list[str] = []
        async for line in fp.stdout_lines():
            lines.append(line)

        assert lines == []


class TestFileAgentProcessStderrLines:
    async def test_tails_stderr(self, tmp_path: Path) -> None:
        from sova.ipc.control import FileAgentProcess

        stdout_path = tmp_path / "test.stdout"
        stderr_path = tmp_path / "test.stderr"
        stdout_path.touch()
        stderr_path.write_text("err 1\nerr 2\n")

        mock_proc = AsyncMock()
        mock_proc.pid = 42
        mock_proc.returncode = 0

        fp = FileAgentProcess(mock_proc, stdout_path=stdout_path, stderr_path=stderr_path)
        lines: list[str] = []
        async for line in fp.stderr_lines():
            lines.append(line)

        assert lines == ["err 1", "err 2"]


class TestFileAgentProcessClassifyExit:
    def test_delegates_to_agent_process(self) -> None:
        from sova.ipc.control import FileAgentProcess

        assert FileAgentProcess.classify_exit(0) == ExitClassification.SUCCESS
        assert FileAgentProcess.classify_exit(1) == ExitClassification.ERROR
        assert FileAgentProcess.classify_exit(137) == ExitClassification.CRASH


class TestRuntimeFileOutput:
    """Test that runtimes use file-based output when output_dir is provided."""

    async def test_claude_spawn_with_output_dir(self, tmp_path: Path) -> None:
        from sova.ipc.control import FileAgentProcess
        from sova.ipc.runtime import ClaudeCodeRuntime

        output_dir = tmp_path / "agent-output"
        output_dir.mkdir()

        mock_proc = AsyncMock()
        mock_proc.pid = 42
        mock_proc.returncode = None
        mock_proc.stdin = _stdin_mock()

        rt = ClaudeCodeRuntime()
        with patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            result = await rt.spawn("test prompt", tmp_path, output_dir=output_dir, run_label="100")

        assert isinstance(result, FileAgentProcess)
        assert result.pid == 42
        assert result.stdout_path == output_dir / "100.stdout"
        assert result.stderr_path == output_dir / "100.stderr"

        call_kwargs = mock_exec.call_args[1]
        assert "stdout" in call_kwargs
        assert "stderr" in call_kwargs
        assert call_kwargs["stdout"] != asyncio.subprocess.PIPE
        assert call_kwargs["stderr"] != asyncio.subprocess.PIPE

    async def test_claude_spawn_without_output_dir_uses_pipes(self, tmp_path: Path) -> None:
        from sova.ipc.control import AgentProcess, FileAgentProcess
        from sova.ipc.runtime import ClaudeCodeRuntime

        mock_proc = AsyncMock()
        mock_proc.pid = 42
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.stdin = _stdin_mock()

        rt = ClaudeCodeRuntime()
        with patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await rt.spawn("test prompt", tmp_path)

        assert isinstance(result, AgentProcess)
        assert not isinstance(result, FileAgentProcess)

    async def test_aider_spawn_with_output_dir(self, tmp_path: Path) -> None:
        from sova.ipc.control import FileAgentProcess
        from sova.ipc.runtime import AiderRuntime

        output_dir = tmp_path / "agent-output"
        output_dir.mkdir()

        mock_proc = AsyncMock()
        mock_proc.pid = 43
        mock_proc.returncode = None

        rt = AiderRuntime()
        with patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await rt.spawn("fix bug", tmp_path, output_dir=output_dir, run_label="200")

        assert isinstance(result, FileAgentProcess)
        assert result.stdout_path == output_dir / "200.stdout"

    async def test_codex_spawn_with_output_dir(self, tmp_path: Path) -> None:
        from sova.ipc.control import FileAgentProcess
        from sova.ipc.runtime import CodexRuntime

        output_dir = tmp_path / "agent-output"
        output_dir.mkdir()

        mock_proc = AsyncMock()
        mock_proc.pid = 44
        mock_proc.returncode = None

        rt = CodexRuntime()
        rt._stdin_capable = False
        with patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await rt.spawn("fix bug", tmp_path, output_dir=output_dir, run_label="300")

        assert isinstance(result, FileAgentProcess)
        assert result.pid == 44
        assert result.stdout_path == output_dir / "300.stdout"
        assert result.stderr_path == output_dir / "300.stderr"

    async def test_mock_runtime_accepts_output_dir(self, tmp_path: Path) -> None:
        from sova.ipc.testing import MockRuntime

        output_dir = tmp_path / "agent-output"
        output_dir.mkdir()

        rt = MockRuntime(stdout_lines=["hello"])
        proc = await rt.spawn("test", tmp_path, output_dir=output_dir, run_label="300")

        lines = [line async for line in proc.stdout_lines()]
        assert lines == ["hello"]

    async def test_output_files_created_on_spawn(self, tmp_path: Path) -> None:
        from sova.ipc.runtime import ClaudeCodeRuntime

        output_dir = tmp_path / "agent-output"
        output_dir.mkdir()

        mock_proc = AsyncMock()
        mock_proc.pid = 42
        mock_proc.returncode = None
        mock_proc.stdin = _stdin_mock()

        rt = ClaudeCodeRuntime()
        with patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc):
            await rt.spawn("test", tmp_path, output_dir=output_dir, run_label="100")

        assert (output_dir / "100.stdout").exists()
        assert (output_dir / "100.stderr").exists()


class TestAdoptedProcess:
    """AdoptedProcess: a live process this server did not spawn, polled instead of waited on."""

    async def test_wait_returns_the_derived_code_once_the_process_is_gone(self) -> None:
        from sova.ipc.control import AdoptedProcess

        alive = iter([True, True, False])
        proc = AdoptedProcess(4242, is_alive=lambda: next(alive), resolve_exit_code=AsyncMock(return_value=7))

        with patch("sova.ipc.control._ADOPTED_POLL_INTERVAL", 0.0):
            assert await proc.wait() == 7
        assert proc.returncode == 7

    async def test_signals_are_sent_only_while_the_identity_still_matches(self) -> None:
        from sova.ipc.control import AdoptedProcess

        state = {"alive": True}
        proc = AdoptedProcess(4242, is_alive=lambda: state["alive"], resolve_exit_code=AsyncMock(return_value=0))

        with patch("sova.ipc.control.os.kill") as kill:
            proc.terminate()
            kill.assert_called_once()
            kill.reset_mock()
            state["alive"] = False
            proc.terminate()
            proc.kill()
            kill.assert_not_called()

    async def test_a_pid_owned_by_another_user_is_not_signalled_and_does_not_raise(self) -> None:
        from sova.ipc.control import AdoptedProcess

        proc = AdoptedProcess(4242, is_alive=lambda: True, resolve_exit_code=AsyncMock(return_value=0))
        with patch("sova.ipc.control.os.kill", side_effect=PermissionError("not permitted")) as kill:
            proc.terminate()
            proc.kill()
        assert kill.call_count == 2

    async def test_file_process_over_an_adopted_proc_tails_from_the_end(self, tmp_path: Path) -> None:
        from sova.ipc.control import AdoptedProcess, FileAgentProcess

        stdout_path = tmp_path / "7.stdout"
        stdout_path.write_text("already captured\n", encoding="utf-8")
        stderr_path = tmp_path / "7.stderr"
        proc = AdoptedProcess(4242, is_alive=lambda: True, resolve_exit_code=AsyncMock(return_value=0))
        fp = FileAgentProcess(proc, stdout_path=stdout_path, stderr_path=stderr_path, tail_from_end=True)

        lines: list[str] = []

        async def collect() -> None:
            async for line in fp.stdout_lines():
                lines.append(line)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.3)
        with open(stdout_path, "a", encoding="utf-8") as fh:
            fh.write("written after adoption\n")
        await asyncio.sleep(0.3)
        proc.returncode = 0
        await asyncio.wait_for(task, timeout=5)

        assert lines == ["written after adoption"]

    async def test_a_process_that_exits_before_the_signal_lands_is_not_an_error(self) -> None:
        from sova.ipc.control import AdoptedProcess

        proc = AdoptedProcess(4242, is_alive=lambda: True, resolve_exit_code=AsyncMock(return_value=0))

        with patch("sova.ipc.control.os.kill", side_effect=ProcessLookupError):
            proc.terminate()
            proc.kill()

    async def test_tailing_skips_the_line_the_agent_was_mid_way_through_writing(self, tmp_path: Path) -> None:
        from sova.ipc.control import AdoptedProcess, FileAgentProcess

        stdout_path = tmp_path / "8.stdout"
        stdout_path.write_text('{"type":"assistant"}\n{"type":"res', encoding="utf-8")
        proc = AdoptedProcess(4242, is_alive=lambda: True, resolve_exit_code=AsyncMock(return_value=0))
        fp = FileAgentProcess(proc, stdout_path=stdout_path, stderr_path=tmp_path / "8.stderr", tail_from_end=True)

        lines: list[str] = []

        async def collect() -> None:
            async for line in fp.stdout_lines():
                lines.append(line)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.3)
        with open(stdout_path, "a", encoding="utf-8") as fh:
            fh.write('ult"}\n{"type":"next"}\n')
        await asyncio.sleep(0.3)
        proc.returncode = 0
        await asyncio.wait_for(task, timeout=5)

        assert lines == ['{"type":"next"}']

    async def test_a_partial_line_still_open_at_exit_is_dropped(self, tmp_path: Path) -> None:
        from sova.ipc.control import AdoptedProcess, FileAgentProcess

        stdout_path = tmp_path / "9.stdout"
        stdout_path.write_text('{"type":"par', encoding="utf-8")
        proc = AdoptedProcess(4242, is_alive=lambda: True, resolve_exit_code=AsyncMock(return_value=0))
        proc.returncode = 0
        fp = FileAgentProcess(proc, stdout_path=stdout_path, stderr_path=tmp_path / "9.stderr", tail_from_end=True)

        assert [line async for line in fp.stdout_lines()] == []
