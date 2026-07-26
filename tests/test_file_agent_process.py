"""Tests for FileAgentProcess: file-based stdout/stderr tailing."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from sova.ipc.control import ExitClassification


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

        rt = ClaudeCodeRuntime()
        with patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=mock_proc):
            await rt.spawn("test", tmp_path, output_dir=output_dir, run_label="100")

        assert (output_dir / "100.stdout").exists()
        assert (output_dir / "100.stderr").exists()
