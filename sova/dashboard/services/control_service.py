"""Agent control -- process management for the dashboard.

Manages a single agent process (start/stop/status/output).
Uses sova.ipc.control.AgentProcess under the hood.
"""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path

from sova.ipc.control import AgentProcess
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.control")

_process: AgentProcess | None = None
_output_lines: deque[str] = deque(maxlen=5000)
_reader_task: asyncio.Task | None = None


def get_status() -> dict:
    """Get current agent status."""
    if _process is None or not _process.is_running:
        return {"status": "idle", "running": False, "pid": None}
    return {"status": "running", "running": True, "pid": _process.pid}


def get_output(since: int = 0) -> list[str]:
    """Get output lines since the given cursor."""
    lines = list(_output_lines)
    return lines[since:]


async def start_agent(
    issue: str,
    *,
    cwd: str | Path = ".",
    role: str | None = None,
    force: bool = False,
) -> dict:
    """Start an agent process for the given issue."""
    global _process, _reader_task

    if _process is not None and _process.is_running:
        return {"error": "Agent already running", "pid": _process.pid}

    _output_lines.clear()

    prompt = f"sova run {issue}"
    if role:
        prompt += f" --role {role}"
    if force:
        prompt += " --force"

    _process = await AgentProcess.spawn(prompt=prompt, cwd=Path(cwd))
    _reader_task = asyncio.create_task(_read_output(_process))

    log.info("agent.started", issue=issue, pid=_process.pid)
    return {"status": "started", "pid": _process.pid}


async def stop_agent() -> dict:
    """Stop the running agent process."""
    global _process, _reader_task

    if _process is None or not _process.is_running:
        return {"status": "idle", "message": "No agent running"}

    pid = _process.pid
    await _process.stop()

    if _reader_task and not _reader_task.done():
        _reader_task.cancel()
        try:
            await _reader_task
        except asyncio.CancelledError:
            pass

    _process = None
    _reader_task = None

    log.info("agent.stopped", pid=pid)
    return {"status": "stopped", "pid": pid}


async def _read_output(process: AgentProcess) -> None:
    """Background task to read stdout lines into the deque."""
    try:
        async for line in process.stdout_lines():
            _output_lines.append(line)
    except asyncio.CancelledError:
        pass
