"""Tests for subprocess isolation (start_new_session flag)."""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_proc(pid: int = 100, *, with_streams: bool = False) -> AsyncMock:
    """Create a mock subprocess with optional stdout/stderr streams."""
    proc = AsyncMock()
    proc.pid = pid
    proc.returncode = None
    if with_streams:
        proc.stdout = AsyncMock()
        proc.stderr = AsyncMock()
    # Shaped like a real StreamWriter: write/close are sync, drain is async.
    # Only ClaudeCodeRuntime.spawn() writes a stdin payload, but every
    # runtime shares _spawn_agent_process()/_spawn_with_file_output(), so
    # every mock proc needs this to avoid an unawaited-coroutine warning.
    proc.stdin = MagicMock()
    proc.stdin.drain = AsyncMock()
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
        ("runtime_module", "runtime_cls", "prompt", "use_file_output"),
        [
            ("sova.ipc.runtime", "ClaudeCodeRuntime", "test prompt", False),
            ("sova.ipc.runtime", "ClaudeCodeRuntime", "test prompt", True),
            ("sova.ipc.runtime", "AiderRuntime", "fix bug", False),
            ("sova.ipc.runtime", "AiderRuntime", "fix bug", True),
            ("sova.ipc.runtime", "AiderRuntime", "Run the following command:\n```bash\nsova run 42\n```", False),
            ("sova.ipc.codex_runtime", "CodexRuntime", "fix bug", False),
            ("sova.ipc.codex_runtime", "CodexRuntime", "fix bug", True),
        ],
        ids=[
            "claude-pipe",
            "claude-file",
            "aider-pipe",
            "aider-file",
            "aider-sova-cmd",
            "codex-pipe",
            "codex-file",
        ],
    )
    async def test_runtime_spawn_uses_start_new_session(
        self, tmp_path: Path, runtime_module: str, runtime_cls: str, prompt: str, use_file_output: bool
    ) -> None:
        proc = _mock_proc(with_streams=not use_file_output)
        runtime = getattr(importlib.import_module(runtime_module), runtime_cls)()
        if runtime_cls == "CodexRuntime":
            # Skip the stdin-support probe: it's a separate subprocess call
            # not modeled by this shared mock, and not what this test covers.
            runtime._stdin_capable = False

        with patch("sova.ipc.runtime.asyncio.create_subprocess_exec", return_value=proc) as mock_exec:
            kwargs: dict = {}
            if use_file_output:
                output_dir = tmp_path / "output"
                output_dir.mkdir(exist_ok=True)
                kwargs.update(output_dir=output_dir, run_label="test")
            await runtime.spawn(prompt, tmp_path, **kwargs)

        assert mock_exec.call_args[1].get("start_new_session") is True
