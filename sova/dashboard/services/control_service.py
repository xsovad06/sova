"""Agent control -- process management for the dashboard.

Manages agent processes per project (start/stop/status/output).
Uses sova.ipc.control.AgentProcess under the hood.
Creates TaskRun + CostRecord DB entries for persistence.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sova.dashboard.project_context import get_project_dir, get_project_slug
from sova.ipc.control import AgentProcess
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.control")

# Default slug for single-project mode
_DEFAULT_SLUG = "__default__"


@dataclass
class ProjectProcess:
    """Per-project agent process state."""

    process: AgentProcess | None = None
    output_lines: deque[str] = field(default_factory=lambda: deque(maxlen=5000))
    reader_task: asyncio.Task | None = None
    current_run_id: int | None = None
    last_result_cost: float | None = None
    project_dir: Path = field(default_factory=Path.cwd)


# Per-project process state
_projects: dict[str, ProjectProcess] = {}

# Legacy single-project dir (for backward compat with set_project_dir)
_default_project_dir: Path | None = None


def _get_state(slug: str | None = None) -> ProjectProcess:
    """Get or create process state for a project slug."""
    if slug is None:
        slug = get_project_slug() or _DEFAULT_SLUG

    state = _projects.get(slug)
    if state is None:
        project_dir = get_project_dir()
        if project_dir is None:
            project_dir = _default_project_dir or Path.cwd()
        state = _projects.setdefault(slug, ProjectProcess(project_dir=project_dir.resolve()))

    return state


def set_project_dir(path: Path) -> None:
    """Set the default project directory (single-project mode)."""
    global _default_project_dir
    _default_project_dir = path
    state = _get_state(_DEFAULT_SLUG)
    state.project_dir = path.resolve()


def get_status(slug: str | None = None) -> dict:
    """Get current agent status."""
    state = _get_state(slug)
    if state.process is None or not state.process.is_running:
        return {"status": "idle", "running": False, "pid": None}
    return {"status": "running", "running": True, "pid": state.process.pid}


def get_output(since: int = 0, slug: str | None = None) -> list[str]:
    """Get output lines since the given cursor."""
    state = _get_state(slug)
    lines = list(state.output_lines)
    return lines[since:]


async def start_agent(
    issue: str,
    *,
    role: str | None = None,
    force: bool = False,
    slug: str | None = None,
) -> dict:
    """Start an agent process for the given issue."""
    # Normalize: strip "#" prefix so "#67" and "67" are treated the same
    issue = issue.lstrip("#").strip()

    state = _get_state(slug)

    if state.process is not None and state.process.is_running:
        return {"error": "Agent already running", "pid": state.process.pid}

    state.output_lines.clear()
    state.last_result_cost = None

    prompt = f"sova run {issue}"
    if role:
        prompt += f" --role {role}"
    if force:
        prompt += " --force"

    cwd = state.project_dir
    state.process = await AgentProcess.spawn(prompt=prompt, cwd=cwd)

    pid = state.process.pid

    # Create TaskRun DB record (with PID for recovery after dashboard crash)
    state.current_run_id = await _create_task_run(issue, role or "auto", state.project_dir, pid=pid)

    # Transition the issue to IN_PROGRESS on the tracker (non-blocking)
    asyncio.create_task(_transition_to_in_progress(issue, state.project_dir))

    state.reader_task = asyncio.create_task(_read_output(state))
    asyncio.create_task(_read_stderr(state))
    asyncio.create_task(_wait_and_finalize(state))

    log.info("agent.started", issue=issue, pid=pid, cwd=str(cwd))
    return {"status": "started", "pid": pid}


async def stop_agent(slug: str | None = None) -> dict:
    """Stop the running agent process."""
    state = _get_state(slug)

    if state.process is None or not state.process.is_running:
        state.current_run_id = None
        return {"status": "idle", "message": "No agent running"}

    pid = state.process.pid
    await state.process.stop()

    if state.reader_task and not state.reader_task.done():
        state.reader_task.cancel()
        try:
            await state.reader_task
        except asyncio.CancelledError:
            pass

    state.process = None
    state.reader_task = None

    log.info("agent.stopped", pid=pid)
    return {"status": "stopped", "pid": pid}


async def start_command(
    command: str,
    args: dict | None = None,
    slug: str | None = None,
) -> dict:
    """Start a Claude Code command (e.g. /agent-resume, /approve-merge).

    Used by handoff action execution to run Claude commands.
    """
    state = _get_state(slug)

    if state.process is not None and state.process.is_running:
        return {"error": "Agent already running", "pid": state.process.pid}

    state.output_lines.clear()
    state.last_result_cost = None

    # Build prompt: "Run the /<command>" with args serialized
    prompt = f"/{command}"
    if args:
        arg_parts = [f"{k}={v}" for k, v in args.items()]
        prompt += " " + " ".join(arg_parts)

    cwd = state.project_dir
    state.process = await AgentProcess.spawn(prompt=prompt, cwd=cwd)

    # Create TaskRun DB record for the command
    issue = (args or {}).get("issue", command)
    state.current_run_id = await _create_task_run(
        str(issue), f"command:{command}", state.project_dir, pid=state.process.pid
    )

    state.reader_task = asyncio.create_task(_read_output(state))
    asyncio.create_task(_read_stderr(state))
    asyncio.create_task(_wait_and_finalize(state))

    log.info("command.started", command=command, pid=state.process.pid, cwd=str(cwd))
    return {"status": "started", "pid": state.process.pid}


async def _read_output(state: ProjectProcess) -> None:
    """Background task to read stdout lines into the deque.

    Parses Claude CLI stream-json output to extract human-readable text.
    Stream-json emits JSONL where assistant messages have content blocks.
    """
    try:
        if state.process is None:
            return
        async for line in state.process.stdout_lines():
            text = _parse_stream_line(line, state)
            if text:
                state.output_lines.append(text)
    except asyncio.CancelledError:
        pass


def _parse_stream_line(line: str, state: ProjectProcess) -> str:
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
    if msg_type == "result":
        cost = data.get("total_cost_usd")
        if cost:
            state.last_result_cost = float(cost)
            return f"\n--- Result [cost: ${cost}] ---"

    return ""


async def _read_stderr(state: ProjectProcess) -> None:
    """Background task to capture stderr lines into the output deque."""
    try:
        if state.process is None:
            return
        async for line in state.process.stderr_lines():
            if line.strip():
                state.output_lines.append(f"[stderr] {line}")
    except asyncio.CancelledError:
        pass


# -- Startup recovery --------------------------------------------------------


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


async def recover_stale_runs(project_dir: Path | None = None) -> list[dict]:
    """Detect and mark stale 'running' TaskRuns on dashboard startup.

    For each TaskRun still in 'running' status:
    - If it has a PID and the process is still alive: leave it (still running)
    - Otherwise: mark as 'interrupted' so the user can restart it

    Returns a list of interrupted run summaries for logging.
    """
    try:
        from sqlalchemy import select

        from sova.db.models import TaskRun
        from sova.db.session import get_session

        session = await get_session(project_dir=project_dir)
        interrupted = []

        async with session.begin():
            stmt = select(TaskRun).where(TaskRun.status == "running")
            result = await session.execute(stmt)
            stale_runs = result.scalars().all()

            for run in stale_runs:
                # If PID is known and process is alive, skip
                if run.pid and _is_process_alive(run.pid):
                    log.info("recovery.still_alive", run_id=run.id, pid=run.pid)
                    continue

                run.status = "interrupted"
                run.error_message = "Dashboard restarted while agent was running"
                run.ended_at = datetime.now(timezone.utc)
                interrupted.append({
                    "run_id": run.id,
                    "issue": run.issue_number,
                    "role": run.role,
                    "pid": run.pid,
                })
                log.warning(
                    "recovery.interrupted",
                    run_id=run.id,
                    issue=run.issue_number,
                    pid=run.pid,
                )

        await session.close()

        if interrupted:
            log.info("recovery.complete", interrupted_count=len(interrupted))
        return interrupted
    except Exception:
        log.warning("recovery.failed", exc_info=True)
        return []


# -- Tracker state transitions -----------------------------------------------


async def _transition_to_in_progress(issue: str, project_dir: Path) -> None:
    """Move the issue to IN_PROGRESS on the configured tracker.

    Uses the project's adapter (GitHub, Jira, MCP, etc.) to transition
    the issue state. Failures are logged but do not block the agent start.
    """
    try:
        from sova.adapters import create_adapter
        from sova.adapters.base import TaskState
        from sova.config.loader import load_config

        cfg = load_config(project_dir)
        adapter = create_adapter(cfg.task_source.type, cfg.github_repo)
        await adapter.transition_state(issue, TaskState.IN_PROGRESS)
        log.info("issue.transitioned", issue=issue, state="in_progress")
    except Exception:
        log.warning("issue.transition_failed", issue=issue, exc_info=True)


# -- DB persistence ----------------------------------------------------------


async def _create_task_run(issue: str, role: str, project_dir: Path, *, pid: int | None = None) -> int | None:
    """Create a TaskRun record and return its ID."""
    try:
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        session = await get_session(project_dir=project_dir)
        async with session.begin():
            task_run = TaskRun(
                issue_number=issue,
                role=role,
                status="running",
                current_step="agent",
                pid=pid,
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


async def _finalize_task_run(run_id: int, *, exit_code: int, state: ProjectProcess) -> None:
    """Update the TaskRun with final status and cost."""
    try:
        from sova.db.models import CostRecord, TaskRun
        from sova.db.session import get_session

        status = "done" if exit_code == 0 else "failed"
        cost = Decimal(str(state.last_result_cost)) if state.last_result_cost else Decimal("0")

        session = await get_session(project_dir=state.project_dir)
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
            if state.last_result_cost and state.last_result_cost > 0:
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


async def _wait_and_finalize(state: ProjectProcess) -> None:
    """Wait for the process to exit, then finalize the DB record."""
    if state.process is None:
        return
    exit_code = await state.process.wait()
    run_id = state.current_run_id
    state.current_run_id = None

    if run_id is not None:
        await _finalize_task_run(run_id, exit_code=exit_code, state=state)
