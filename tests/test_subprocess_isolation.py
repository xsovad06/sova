"""Tests for subprocess isolation (start_new_session flag)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


def _mock_proc(pid: int = 100, *, with_streams: bool = False) -> AsyncMock:
    """Create a mock subprocess with optional stdout/stderr streams."""
    proc = AsyncMock()
    proc.pid = pid
    proc.returncode = None
    if with_streams:
        proc.stdout = AsyncMock()
        proc.stderr = AsyncMock()
    return proc


class TestSubprocessIsolation:
    """Verify that agent subprocesses use start_new_session=True for process group isolation."""

    async def test_spawn_with_file_output_uses_start_new_session(self, tmp_path: Path) -> None:
        from sova.ipc.runtime import _spawn_with_file_output

        with patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=_mock_proc()) as mock_exec:
            output_dir = tmp_path / "output"
            output_dir.mkdir()
            await _spawn_with_file_output(
                ["sova", "run", "42"], cwd=tmp_path, env=None, output_dir=output_dir, run_label="test-run"
            )
        assert mock_exec.call_args[1].get("start_new_session") is True

    async def test_spawn_direct_pipe_path_uses_start_new_session(self, tmp_path: Path) -> None:
        from sova.ipc.runtime import spawn_direct

        proc = _mock_proc(with_streams=True)
        with patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            await spawn_direct(["sova", "run", "42"], cwd=tmp_path, env=None)
        assert mock_exec.call_args[1].get("start_new_session") is True

    @pytest.mark.parametrize(
        ("runtime_cls", "prompt", "use_file_output"),
        [
            ("ClaudeCodeRuntime", "test prompt", False),
            ("ClaudeCodeRuntime", "test prompt", True),
            ("AiderRuntime", "fix bug", False),
            ("AiderRuntime", "fix bug", True),
            ("AiderRuntime", "Run the following command:\n```bash\nsova run 42\n```", False),
        ],
        ids=["claude-pipe", "claude-file", "aider-pipe", "aider-file", "aider-sova-cmd"],
    )
    async def test_runtime_spawn_uses_start_new_session(
        self, tmp_path: Path, runtime_cls: str, prompt: str, use_file_output: bool
    ) -> None:
        import sova.ipc.runtime as rt

        proc = _mock_proc(with_streams=not use_file_output)
        runtime = getattr(rt, runtime_cls)()

        with patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            kwargs: dict = {}
            if use_file_output:
                output_dir = tmp_path / "output"
                output_dir.mkdir(exist_ok=True)
                kwargs.update(output_dir=output_dir, run_label="test")
            await runtime.spawn(prompt, tmp_path, **kwargs)

        assert mock_exec.call_args[1].get("start_new_session") is True
