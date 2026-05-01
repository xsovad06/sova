"""Agent control -- multi-agent process management for the dashboard.

Manages multiple concurrent agent processes per project.
Uses sova.ipc.control.AgentProcess under the hood.
Creates TaskRun + CostRecord DB entries for persistence.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sova.core.steps import get_address_review_step_names, get_developer_step_names
from sova.dashboard.project_context import get_project_dir, get_project_slug
from sova.dashboard.services.output_service import OutputWriter
from sova.ipc.control import AgentProcess
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.control")

_DEFAULT_SLUG = "__default__"

DEVELOPER_PIPELINE = get_developer_step_names()
ADDRESS_REVIEW_PIPELINE = get_address_review_step_names()

MAX_RECENTLY_COMPLETED = 5
RECENTLY_COMPLETED_TTL = 60.0


@dataclass
class AgentState:
    """Per-agent process state (one per running agent)."""

    run_id: int
    issue: str
    role: str
    process: AgentProcess
    output_lines: deque[str] = field(default_factory=lambda: deque(maxlen=5000))
    output_writer: OutputWriter | None = None
    reader_task: asyncio.Task | None = None
    stderr_task: asyncio.Task | None = None
    started_at: float = field(default_factory=time.monotonic)
    started_at_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_result_cost: float | None = None
    project_dir: Path = field(default_factory=Path.cwd)


@dataclass
class CompletedAgent:
    """Recently completed agent kept briefly for UI transition."""

    run_id: int
    issue: str
    role: str
    status: str
    cost: float
    completed_at: float = field(default_factory=time.monotonic)


@dataclass
class ProjectAgents:
    """Per-project collection of running agents."""

    agents: dict[int, AgentState] = field(default_factory=dict)
    recently_completed: deque[CompletedAgent] = field(
        default_factory=lambda: deque(maxlen=MAX_RECENTLY_COMPLETED),
    )
    max_concurrent: int = 3
    project_dir: Path = field(default_factory=Path.cwd)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_projects: dict[str, ProjectAgents] = {}
_default_project_dir: Path | None = None


def _get_project_agents(slug: str | None = None) -> ProjectAgents:
    """Get or create agent collection for a project slug."""
    if slug is None:
        slug = get_project_slug() or _DEFAULT_SLUG

    pa = _projects.get(slug)
    if pa is None:
        project_dir = get_project_dir()
        if project_dir is None:
            project_dir = _default_project_dir or Path.cwd()
        pa = _projects.setdefault(slug, ProjectAgents(project_dir=project_dir.resolve()))

    return pa


def set_project_dir(path: Path) -> None:
    """Set the default project directory (single-project mode)."""
    global _default_project_dir
    _default_project_dir = path
    pa = _get_project_agents(_DEFAULT_SLUG)
    pa.project_dir = path.resolve()


# -- Status queries -----------------------------------------------------------


def get_status(slug: str | None = None) -> dict:
    """Get legacy single-agent status (backward compat for /api/control/status)."""
    pa = _get_project_agents(slug)
    if not pa.agents:
        return {"status": "idle", "running": False, "pid": None}
    first = next(iter(pa.agents.values()))
    return {"status": "running", "running": True, "pid": first.process.pid}


async def get_all_agents(slug: str | None = None) -> dict:
    """Get status of all running + recently completed agents."""
    pa = _get_project_agents(slug)
    now = time.monotonic()

    _prune_completed(pa, now)

    all_run_ids = [a.run_id for a in pa.agents.values()] + [ca.run_id for ca in pa.recently_completed]
    db_states = await _fetch_run_states(all_run_ids)

    agents = []
    for agent in pa.agents.values():
        elapsed = now - agent.started_at
        db = db_states.get(agent.run_id, {})
        current_step = db.get("current_step", "agent")
        progress = get_step_progress(current_step)
        agents.append(
            {
                "run_id": agent.run_id,
                "issue": agent.issue,
                "role": agent.role,
                "status": db.get("status", "running"),
                "pid": agent.process.pid,
                "current_step": current_step,
                "step_index": progress["step_index"],
                "total_steps": progress["total_steps"],
                "elapsed_seconds": round(elapsed),
                "cost_usd": db.get("cost_usd", agent.last_result_cost or 0.0),
                "output_lines": len(agent.output_lines),
                "pr_number": db.get("pr_number"),
            }
        )

    completed = []
    for ca in pa.recently_completed:
        db = db_states.get(ca.run_id, {})
        completed.append(
            {
                "run_id": ca.run_id,
                "issue": ca.issue,
                "role": ca.role,
                "status": ca.status,
                "cost_usd": ca.cost,
                "completed_seconds_ago": round(now - ca.completed_at),
                "pr_number": db.get("pr_number"),
            }
        )

    return {
        "agents": agents,
        "completed": completed,
        "max_concurrent": pa.max_concurrent,
        "slots_available": max(0, pa.max_concurrent - len(pa.agents)),
    }


async def _fetch_run_states(run_ids: list[int]) -> dict[int, dict]:
    """Fetch current_step, status, and cost from the DB for running agents."""
    if not run_ids:
        return {}
    try:
        from sqlalchemy import select

        from sova.db.models import TaskRun
        from sova.db.session import get_session

        session = await get_session()
        async with session.begin():
            stmt = select(TaskRun).where(TaskRun.id.in_(run_ids))
            result = await session.execute(stmt)
            runs = result.scalars().all()
        await session.close()
        return {
            r.id: {
                "current_step": r.current_step or "agent",
                "status": r.status,
                "cost_usd": float(r.total_cost_usd or 0),
                "pr_number": r.pr_number,
            }
            for r in runs
        }
    except Exception:
        log.debug("fetch_run_states.failed", exc_info=True)
        return {}


def get_output(since: int = 0, slug: str | None = None, *, run_id: int | None = None) -> list[str]:
    """Get output lines since the given cursor.

    If run_id is specified, returns output for that specific agent.
    Falls back to the persisted output file when the agent is not in memory.
    Otherwise returns output for the first (legacy single-agent compat).
    """
    from sova.dashboard.services.output_service import read_lines

    pa = _get_project_agents(slug)

    if run_id is not None:
        agent = pa.agents.get(run_id)
        if agent is not None:
            lines = list(agent.output_lines)
            return lines[since:]
        lines, _total = read_lines(pa.project_dir, run_id, since)
        return lines

    if not pa.agents:
        return []
    first = next(iter(pa.agents.values()))
    lines = list(first.output_lines)
    return lines[since:]


async def get_unified_agents(slug: str | None = None) -> dict:
    """Get all agents: dashboard-spawned (in-memory) + CLI-spawned (DB with alive PID).

    Returns the same shape as get_all_agents() but includes externally started agents
    detected via PID liveness checks on non-terminal TaskRun records.
    """
    base = await get_all_agents(slug)
    pa = _get_project_agents(slug)

    in_memory_run_ids = {a["run_id"] for a in base["agents"]}

    try:
        from sqlalchemy import select

        from sova.db.models import TaskRun
        from sova.db.session import get_session

        _TERMINAL = {"done", "failed", "rejected", "interrupted"}
        session = await get_session(project_dir=pa.project_dir)
        async with session.begin():
            stmt = select(TaskRun).where(
                TaskRun.status.notin_(_TERMINAL),
                TaskRun.pid.isnot(None),
            )
            result = await session.execute(stmt)
            runs = result.scalars().all()

        await session.close()

        now = datetime.now(timezone.utc)
        for run in runs:
            if run.id in in_memory_run_ids:
                continue
            if not _is_process_alive(run.pid):
                continue
            progress = get_step_progress(run.current_step)
            started = run.started_at or now
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            elapsed = (now - started).total_seconds()
            base["agents"].append(
                {
                    "run_id": run.id,
                    "issue": run.issue_number,
                    "role": run.role,
                    "status": run.status,
                    "pid": run.pid,
                    "current_step": run.current_step or "agent",
                    "step_index": progress["step_index"],
                    "total_steps": progress["total_steps"],
                    "elapsed_seconds": round(elapsed),
                    "cost_usd": float(run.total_cost_usd or 0),
                    "output_lines": 0,
                    "source": "external",
                    "pr_number": run.pr_number,
                }
            )
    except Exception:
        log.debug("unified_agents.external_fetch_failed", exc_info=True)

    for agent in base["agents"]:
        if "source" not in agent:
            agent["source"] = "dashboard"

    return base


# -- Agent lifecycle ----------------------------------------------------------


async def start_agent(
    issue: str,
    *,
    role: str | None = None,
    force: bool = False,
    slug: str | None = None,
    resume_run_id: int | None = None,
    pr_number: int | None = None,
) -> dict:
    """Start an agent process for the given issue."""
    issue = issue.lstrip("#").strip()
    pa = _get_project_agents(slug)

    async with pa._lock:
        if len(pa.agents) >= pa.max_concurrent:
            return {
                "error": f"Maximum concurrent agents reached ({pa.max_concurrent})",
                "running": len(pa.agents),
            }

        for existing in pa.agents.values():
            if existing.issue == issue:
                return {
                    "error": f"Issue #{issue} already has an active agent (run {existing.run_id})",
                    "existing_run_id": existing.run_id,
                }

        prompt = f"sova run {issue}"
        if resume_run_id:
            prompt += f" --resume {resume_run_id}"
        if role:
            prompt += f" --role {role}"
        if force:
            prompt += " --force"
        if pr_number:
            prompt += f" --pr {pr_number}"

        cwd = pa.project_dir
        gh_env = await _resolve_project_gh_env(cwd)
        process = await AgentProcess.spawn(prompt=prompt, cwd=cwd, env=gh_env)
        pid = process.pid

        run_id = await _create_task_run(issue, role or "developer", cwd, pid=pid)
        if run_id is None:
            await process.stop()
            return {"error": "Failed to create task run record"}

        writer = OutputWriter(cwd, run_id)
        await _set_output_file_path(run_id, writer.path, cwd)

        agent = AgentState(
            run_id=run_id,
            issue=issue,
            role=role or "developer",
            process=process,
            output_writer=writer,
            project_dir=cwd,
        )
        pa.agents[run_id] = agent

    agent.reader_task = asyncio.create_task(_read_output(agent))
    agent.stderr_task = asyncio.create_task(_read_stderr(agent))
    asyncio.create_task(_wait_and_finalize(pa, agent))
    asyncio.create_task(_transition_to_in_progress(issue, pa.project_dir))

    log.info("agent.started", issue=issue, pid=pid, run_id=run_id, cwd=str(cwd))
    return {"status": "started", "pid": pid, "run_id": run_id}


async def stop_agent(slug: str | None = None, *, run_id: int | None = None) -> dict:
    """Stop a running agent process.

    If run_id is specified, stops that specific agent.
    Otherwise stops the first agent (legacy single-agent compat).
    """
    pa = _get_project_agents(slug)

    async with pa._lock:
        if run_id is not None:
            agent = pa.agents.get(run_id)
        elif pa.agents:
            agent = next(iter(pa.agents.values()))
        else:
            return {"status": "idle", "message": "No agent running"}

        if agent is None:
            return {"status": "not_found", "message": f"No agent with run_id {run_id}"}

        pid = agent.process.pid
        await agent.process.stop()

        if agent.reader_task and not agent.reader_task.done():
            agent.reader_task.cancel()
            try:
                await agent.reader_task
            except asyncio.CancelledError:
                pass

        if agent.stderr_task and not agent.stderr_task.done():
            agent.stderr_task.cancel()
            try:
                await agent.stderr_task
            except asyncio.CancelledError:
                pass

    log.info("agent.stopped", pid=pid, run_id=agent.run_id)
    return {"status": "stopped", "pid": pid, "run_id": agent.run_id}


def _strip_frontmatter(content: str) -> str:
    """Remove YAML frontmatter (--- ... ---) from command file content."""
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            return content[end + 3 :].lstrip("\n")
    return content


def _resolve_command_prompt(command: str, args: dict | None, project_dir: Path) -> str:
    """Build the prompt for a Claude Code command.

    If the command exists in the target project's .claude/commands/, use
    slash-command syntax (Claude Code resolves it locally). Otherwise, read
    the command file from SOVA's own commands directory and send the full
    rendered content as the prompt.
    """
    arg_str = ""
    if args:
        arg_str = " ".join(f"{k}={v}" for k, v in args.items())

    target_cmd = project_dir / ".claude" / "commands" / f"{command}.md"
    if target_cmd.is_file():
        prompt = f"/{command}"
        if arg_str:
            prompt += " " + arg_str
        return prompt

    sova_root = Path(__file__).resolve().parent.parent.parent.parent
    sova_cmd = sova_root / ".claude" / "commands" / f"{command}.md"
    if not sova_cmd.is_file():
        sova_cmd = sova_root / "commands" / f"{command}.md"

    if sova_cmd.is_file():
        content = sova_cmd.read_text(encoding="utf-8")
        content = _strip_frontmatter(content)
        content = content.replace("$ARGUMENTS", arg_str)
        log.info("command.resolved_from_sova", command=command, source=str(sova_cmd))
        return content

    prompt = f"/{command}"
    if arg_str:
        prompt += " " + arg_str
    return prompt


async def start_command(
    command: str,
    args: dict | None = None,
    slug: str | None = None,
) -> dict:
    """Start a Claude Code command (e.g. /agent-resume, /approve-merge)."""
    pa = _get_project_agents(slug)

    async with pa._lock:
        if len(pa.agents) >= pa.max_concurrent:
            return {
                "error": f"Maximum concurrent agents reached ({pa.max_concurrent})",
                "running": len(pa.agents),
            }

        cwd = pa.project_dir
        prompt = _resolve_command_prompt(command, args, cwd)

        gh_env = await _resolve_project_gh_env(cwd)
        process = await AgentProcess.spawn(prompt=prompt, cwd=cwd, env=gh_env)

        issue = str((args or {}).get("issue", command))
        role = f"command:{command}"
        run_id = await _create_task_run(issue, role, cwd, pid=process.pid)
        if run_id is None:
            await process.stop()
            return {"error": "Failed to create task run record"}

        writer = OutputWriter(cwd, run_id)
        await _set_output_file_path(run_id, writer.path, cwd)

        agent = AgentState(
            run_id=run_id,
            issue=issue,
            role=role,
            process=process,
            output_writer=writer,
            project_dir=cwd,
        )
        pa.agents[run_id] = agent

    agent.reader_task = asyncio.create_task(_read_output(agent))
    agent.stderr_task = asyncio.create_task(_read_stderr(agent))
    asyncio.create_task(_wait_and_finalize(pa, agent))

    log.info("command.started", command=command, pid=process.pid, run_id=run_id, cwd=str(cwd))
    return {"status": "started", "pid": process.pid, "run_id": run_id}


# -- Output streaming --------------------------------------------------------


async def _read_output(agent: AgentState) -> None:
    """Background task to read stdout lines into the agent's deque and output file."""
    try:
        if agent.process is None:
            return
        async for line in agent.process.stdout_lines():
            text = _parse_stream_line(line, agent)
            if text:
                agent.output_lines.append(text)
                if agent.output_writer:
                    agent.output_writer.write_line(text)
    except asyncio.CancelledError:
        pass


def _parse_stream_line(line: str, agent: AgentState) -> str:
    """Extract readable text from a Claude stream-json line."""
    if not line.strip():
        return ""

    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return line

    msg_type = data.get("type", "")

    if msg_type == "assistant":
        content = data.get("message", {}).get("content", [])
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)

    if msg_type == "content_block_delta":
        delta = data.get("delta", {})
        if delta.get("type") == "text_delta":
            return delta.get("text", "")

    if msg_type == "result":
        cost = data.get("total_cost_usd")
        if cost:
            agent.last_result_cost = float(cost)
            return f"\n--- Result [cost: ${cost}] ---"

    return ""


async def _read_stderr(agent: AgentState) -> None:
    """Background task to capture stderr lines into the agent's output deque and file."""
    try:
        if agent.process is None:
            return
        async for line in agent.process.stderr_lines():
            if line.strip():
                text = f"[stderr] {line}"
                agent.output_lines.append(text)
                if agent.output_writer:
                    agent.output_writer.write_line(text)
    except asyncio.CancelledError:
        pass


# -- Completion handling ------------------------------------------------------


def _prune_completed(pa: ProjectAgents, now: float | None = None) -> None:
    """Remove expired entries from recently_completed."""
    if now is None:
        now = time.monotonic()
    while pa.recently_completed and (now - pa.recently_completed[0].completed_at) > RECENTLY_COMPLETED_TTL:
        pa.recently_completed.popleft()


async def _wait_and_finalize(pa: ProjectAgents, agent: AgentState) -> None:
    """Wait for the process to exit, then finalize the DB record."""
    if agent.process is None:
        return

    exit_code = await agent.process.wait()
    run_id = agent.run_id

    status = "done" if exit_code == 0 else "failed"
    cost = agent.last_result_cost or 0.0

    async with pa._lock:
        pa.agents.pop(run_id, None)
        pa.recently_completed.append(
            CompletedAgent(
                run_id=run_id,
                issue=agent.issue,
                role=agent.role,
                status=status,
                cost=cost,
            )
        )

    if agent.output_writer:
        agent.output_writer.close()

    await _finalize_task_run(run_id, exit_code=exit_code, agent=agent)

    try:
        from sova.config.loader import load_config
        from sova.ipc.notifications import notify

        cfg = load_config(agent.project_dir)
        if exit_code != 0:
            notify(
                cfg.notification,
                f"SOVA -- #{agent.issue} failed",
                f"Agent exited with code {exit_code}",
            )
        else:
            msg = f"Agent finished #{agent.issue}"
            if cost:
                msg += f" (${cost:.4f})"
            notify(cfg.notification, f"SOVA -- #{agent.issue} done", msg)
    except Exception:
        log.debug("notify.failed", run_id=run_id, exc_info=True)

    log.info("agent.completed", run_id=run_id, issue=agent.issue, status=status, cost=cost)

    if exit_code == 0:
        await _process_auto_handoff(agent)


# -- Startup recovery --------------------------------------------------------


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


async def recover_stale_runs(project_dir: Path | None = None) -> list[dict]:
    """Detect and mark stale 'running' TaskRuns on dashboard startup."""
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
                if run.pid and _is_process_alive(run.pid):
                    log.info("recovery.still_alive", run_id=run.id, pid=run.pid)
                    continue

                run.status = "interrupted"
                run.error_message = "Dashboard restarted while agent was running"
                run.ended_at = datetime.now(timezone.utc)
                interrupted.append(
                    {
                        "run_id": run.id,
                        "issue": run.issue_number,
                        "role": run.role,
                        "pid": run.pid,
                    }
                )
                log.warning("recovery.interrupted", run_id=run.id, issue=run.issue_number, pid=run.pid)

        await session.close()

        if interrupted:
            log.info("recovery.complete", interrupted_count=len(interrupted))
        return interrupted
    except Exception:
        log.warning("recovery.failed", exc_info=True)
        return []


# -- GH auth resolution ------------------------------------------------------


async def _resolve_project_gh_env(project_dir: Path) -> dict[str, str] | None:
    """Resolve GH_TOKEN env for the project's configured github_user."""
    try:
        from sova.config.loader import load_config
        from sova.utils.gh import resolve_gh_env

        cfg = load_config(project_dir)
        return await resolve_gh_env(cfg.github_user)
    except Exception:
        log.debug("gh_env.resolve_failed", exc_info=True)
        return None


# -- Tracker state transitions -----------------------------------------------


async def _transition_to_in_progress(issue: str, project_dir: Path) -> None:
    """Move the issue to IN_PROGRESS on the configured tracker."""
    try:
        from sova.adapters import create_adapter
        from sova.adapters.base import TaskState
        from sova.config.loader import load_config

        cfg = load_config(project_dir)
        ts = cfg.task_source
        adapter = create_adapter(ts.type, cfg.github_repo, cfg.github_user, ts.github_project_number)
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


async def _set_output_file_path(run_id: int, path: Path, project_dir: Path) -> None:
    """Store the output file path on the TaskRun record."""
    try:
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        session = await get_session(project_dir=project_dir)
        async with session.begin():
            task_run = await session.get(TaskRun, run_id)
            if task_run:
                task_run.output_file_path = str(path)
        await session.close()
    except Exception:
        log.debug("output_file_path.set_failed", run_id=run_id, exc_info=True)


async def _finalize_task_run(run_id: int, *, exit_code: int, agent: AgentState) -> None:
    """Update the TaskRun with final status and cost."""
    try:
        from sova.db.models import CostRecord, TaskRun
        from sova.db.session import get_session

        status = "done" if exit_code == 0 else "failed"
        cost = Decimal(str(agent.last_result_cost)) if agent.last_result_cost else Decimal("0")

        session = await get_session(project_dir=agent.project_dir)
        async with session.begin():
            task_run = await session.get(TaskRun, run_id)
            if task_run is None:
                return
            task_run.status = status
            task_run.total_cost_usd = cost
            task_run.ended_at = datetime.now(timezone.utc)
            if exit_code != 0:
                task_run.error_message = f"Process exited with code {exit_code}"

            if agent.last_result_cost and agent.last_result_cost > 0:
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


# -- Auto-handoff orchestration -----------------------------------------------


async def _process_auto_handoff(agent: AgentState) -> None:
    """Check for auto-executable handoff actions after an agent completes.

    Reads the handoff file and auto-triggers the first action marked
    with auto_execute=True. This enables role chaining (e.g., Developer
    hands off to Reviewer automatically after CI passes).
    """
    try:
        from sova.ipc.handoff import read_handoff_file

        handoff = read_handoff_file(agent.project_dir)
        if handoff is None or handoff.status != "awaiting_action":
            return

        for action in handoff.next_actions:
            if not action.auto_execute:
                continue

            log.info(
                "auto_handoff.executing",
                run_id=agent.run_id,
                action_id=action.id,
                mode=action.mode,
                issue=handoff.issue,
            )

            if action.mode == "agent":
                args = action.args or {}
                raw_pr = args.get("pr") or handoff.pr_number
                result = await start_agent(
                    str(args.get("issue", handoff.issue)),
                    role=args.get("role"),
                    pr_number=int(raw_pr) if raw_pr is not None else None,
                    slug=None,
                )
                log.info("auto_handoff.agent_started", result=result)
            elif action.mode == "claude-command":
                cmd = action.command.lstrip("/").split()[0] if action.command else ""
                if cmd:
                    result = await start_command(cmd, action.args, slug=None)
                    log.info("auto_handoff.command_started", result=result)
            else:
                log.warning("auto_handoff.unsupported_mode", mode=action.mode)

            return  # only execute the first auto action

    except Exception:
        log.warning("auto_handoff.failed", run_id=agent.run_id, exc_info=True)


# -- Pipeline progress -------------------------------------------------------


_ADDRESS_REVIEW_ONLY = frozenset({"address_review", "handoff_to_user"})


def get_step_progress(current_step: str | None) -> dict:
    """Compute step index from current_step name."""
    if current_step is None:
        return {"step_index": -1, "total_steps": len(DEVELOPER_PIPELINE), "steps": DEVELOPER_PIPELINE}

    pipeline = ADDRESS_REVIEW_PIPELINE if current_step in _ADDRESS_REVIEW_ONLY else DEVELOPER_PIPELINE

    try:
        idx = pipeline.index(current_step)
    except ValueError:
        idx = -1
    return {"step_index": idx, "total_steps": len(pipeline), "steps": pipeline}
