"""Agent control -- process management for the dashboard.

Manages a single agent process (start/stop/status/output).
Uses sova.ipc.control.AgentProcess under the hood.
Creates TaskRun + CostRecord DB entries for persistence.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sova.ipc.control import AgentProcess
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.control")

_process: AgentProcess | None = None
_output_lines: deque[str] = deque(maxlen=5000)
_reader_task: asyncio.Task | None = None
_project_dir: Path | None = None
_current_run_id: int | None = None
_last_result_cost: float | None = None


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
    global _process, _reader_task, _current_run_id, _last_result_cost

    if _process is not None and _process.is_running:
        return {"error": "Agent already running", "pid": _process.pid}

    _output_lines.clear()
    _last_result_cost = None

    prompt = f"sova run {issue}"
    if role:
        prompt += f" --role {role}"
    if force:
        prompt += " --force"

    cwd = _project_dir or Path.cwd()
    _process = await AgentProcess.spawn(prompt=prompt, cwd=cwd)

    # Create TaskRun DB record
    _current_run_id = await _create_task_run(issue, role or "auto")

    _reader_task = asyncio.create_task(_read_output(_process))
    # Also capture stderr so errors are visible in the dashboard
    asyncio.create_task(_read_stderr(_process))
    # Monitor process exit to finalize DB record
    asyncio.create_task(_wait_and_finalize(_process))

    log.info("agent.started", issue=issue, pid=_process.pid, cwd=str(cwd))
    return {"status": "started", "pid": _process.pid}


async def stop_agent() -> dict:
    """Stop the running agent process."""
    global _process, _reader_task, _current_run_id

    if _process is None or not _process.is_running:
        _current_run_id = None
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
    # _current_run_id is cleared by _wait_and_finalize

    log.info("agent.stopped", pid=pid)
    return {"status": "stopped", "pid": pid}


async def start_command(
    command: str,
    args: dict | None = None,
) -> dict:
    """Start a Claude Code command (e.g. /agent-resume, /approve-merge).

    Used by handoff action execution to run Claude commands.
    """
    global _process, _reader_task, _current_run_id, _last_result_cost

    if _process is not None and _process.is_running:
        return {"error": "Agent already running", "pid": _process.pid}

    _output_lines.clear()
    _last_result_cost = None

    # Build prompt: "Run the /<command>" with args serialized
    prompt = f"/{command}"
    if args:
        arg_parts = [f"{k}={v}" for k, v in args.items()]
        prompt += " " + " ".join(arg_parts)

    cwd = _project_dir or Path.cwd()
    _process = await AgentProcess.spawn(prompt=prompt, cwd=cwd)

    # Create TaskRun DB record for the command
    issue = (args or {}).get("issue", command)
    _current_run_id = await _create_task_run(str(issue), f"command:{command}")

    _reader_task = asyncio.create_task(_read_output(_process))
    asyncio.create_task(_read_stderr(_process))
    asyncio.create_task(_wait_and_finalize(_process))

    log.info("command.started", command=command, pid=_process.pid, cwd=str(cwd))
    return {"status": "started", "pid": _process.pid}


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
            global _last_result_cost
            _last_result_cost = float(cost)
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


# -- DB persistence ----------------------------------------------------------


async def _create_task_run(issue: str, role: str) -> int | None:
    """Create a TaskRun record and return its ID."""
    try:
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        session = await get_session()
        async with session.begin():
            task_run = TaskRun(
                issue_number=issue,
                role=role,
                status="running",
                current_step="agent",
            )
            session.add(task_run)
            await session.flush()
            run_id = task_run.id
        await session.close()
        log.info("task_run.created", run_id=run_id, issue=issue)
        return run_id
    except Exception:
        log.warning("task_run.create_failed", exc_info=True)
        return None


async def _finalize_task_run(run_id: int, *, exit_code: int) -> None:
    """Update the TaskRun with final status and cost."""
    try:
        from sova.db.models import CostRecord, TaskRun
        from sova.db.session import get_session

        status = "done" if exit_code == 0 else "failed"
        cost = Decimal(str(_last_result_cost)) if _last_result_cost else Decimal("0")

        session = await get_session()
        async with session.begin():
            task_run = await session.get(TaskRun, run_id)
            if task_run is None:
                return
            task_run.status = status
            task_run.total_cost_usd = cost
            task_run.ended_at = datetime.now(timezone.utc)
            if exit_code != 0:
                task_run.error_message = f"Process exited with code {exit_code}"

            # Also create a CostRecord if we captured cost
            if _last_result_cost and _last_result_cost > 0:
                cost_record = CostRecord(
                    task_run_id=run_id,
                    phase="agent",
                    issue=task_run.issue_number,
                    model="claude",
                    cost_usd=cost,
                )
                session.add(cost_record)
        await session.close()
        log.info("task_run.finalized", run_id=run_id, status=status, cost=float(cost))
    except Exception:
        log.warning("task_run.finalize_failed", exc_info=True)


async def _wait_and_finalize(process: AgentProcess) -> None:
    """Wait for the process to exit, then finalize the DB record."""
    global _current_run_id

    exit_code = await process.wait()
    run_id = _current_run_id
    _current_run_id = None

    if run_id is not None:
        await _finalize_task_run(run_id, exit_code=exit_code)
