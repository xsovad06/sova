"""Agent control -- process management for the dashboard.

Manages a single agent process (start/stop/status/output).
Uses sova.ipc.control.AgentProcess under the hood.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from pathlib import Path

from sova.ipc.control import AgentProcess
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.control")

_process: AgentProcess | None = None
_output_lines: deque[str] = deque(maxlen=5000)
_reader_task: asyncio.Task | None = None
_project_dir: Path | None = None


def set_project_dir(path: Path) -> None:
    """Set the project directory for agent processes."""
    global _project_dir
    _project_dir = path


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

    cwd = _project_dir or Path.cwd()
    _process = await AgentProcess.spawn(prompt=prompt, cwd=cwd)
    _reader_task = asyncio.create_task(_read_output(_process))
    # Also capture stderr so errors are visible in the dashboard
    asyncio.create_task(_read_stderr(_process))

    log.info("agent.started", issue=issue, pid=_process.pid, cwd=str(cwd))
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
    """Background task to read stdout lines into the deque.

    Parses Claude CLI stream-json output to extract human-readable text.
    Stream-json emits JSONL where assistant messages have content blocks.
    """
    try:
        async for line in process.stdout_lines():
            text = _parse_stream_line(line)
            if text:
                _output_lines.append(text)
    except asyncio.CancelledError:
        pass


def _parse_stream_line(line: str) -> str:
    """Extract readable text from a Claude stream-json line.

    Returns the text content, or the raw line if it's not parseable JSON.
    """
    if not line.strip():
        return ""

    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return line

    msg_type = data.get("type", "")

    # Assistant text content
    if msg_type == "assistant":
        content = data.get("message", {}).get("content", [])
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)

    # Content block delta (streaming chunks)
    if msg_type == "content_block_delta":
        delta = data.get("delta", {})
        if delta.get("type") == "text_delta":
            return delta.get("text", "")

    # Result summary at the end -- only emit cost marker, not the text
    # (the text was already emitted via assistant messages)
    if msg_type == "result":
        cost = data.get("total_cost_usd")
        if cost:
            return f"\n--- Result [cost: ${cost}] ---"

    return ""


async def _read_stderr(process: AgentProcess) -> None:
    """Background task to capture stderr lines into the output deque."""
    try:
        async for line in process.stderr_lines():
            if line.strip():
                _output_lines.append(f"[stderr] {line}")
    except asyncio.CancelledError:
        pass
