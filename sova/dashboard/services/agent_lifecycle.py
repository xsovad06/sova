"""Agent process lifecycle -- start/stop/wait, status queries, pipeline progress.

Manages concurrent agent processes per project.
Uses sova.ipc.control.AgentProcess under the hood.
Delegates DB persistence to agent_db and pool management to agent_pool.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

from sova.core.steps import get_address_review_step_names, get_developer_step_names
from sova.dashboard.services.agent_db import (
    _create_task_run,
    _fetch_run_states,
    _finalize_orphaned_run,
    _finalize_task_run,
    _set_output_file_path,
    _update_task_run_pid,
)
from sova.dashboard.services.agent_pool import (
    AgentState,
    CompletedAgent,
    ProjectAgents,
    _get_project_agents,
    _prune_completed,
)
from sova.dashboard.services.output_service import OutputWriter
from sova.ipc.control import AgentProcess
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.control")

DEVELOPER_PIPELINE = get_developer_step_names()
ADDRESS_REVIEW_PIPELINE = get_address_review_step_names()


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
        progress = get_step_progress(current_step, role=agent.role, pr_number=db.get("pr_number"))
        agents.append(
            {
                "run_id": agent.run_id,
                "issue": agent.issue,
                "role": agent.role,
                "status": db.get("status", "running"),
                "pid": agent.process.pid,
                "current_step": current_step,
                "pipeline_variant": progress.get("pipeline_variant", "developer"),
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


async def get_unified_agents(slug: str | None = None) -> dict:
    """Get all agents: dashboard-spawned (in-memory) + CLI-spawned (DB with alive PID).

    Returns the same shape as get_all_agents() but includes externally started agents
    detected via PID liveness checks on non-terminal TaskRun records.
    """
    from sova.dashboard.services.agent_recovery import _is_process_alive

    base = await get_all_agents(slug)
    pa = _get_project_agents(slug)

    in_memory_run_ids = {a["run_id"] for a in base["agents"]}

    try:
        from sqlalchemy import select

        from sova.dashboard.services.work_service import _TERMINAL
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session(project_dir=pa.project_dir) as session:
            async with session.begin():
                stmt = select(TaskRun).where(
                    TaskRun.status.notin_(_TERMINAL),
                    TaskRun.pid.isnot(None),
                )
                result = await session.execute(stmt)
                runs = result.scalars().all()

        now = datetime.now(timezone.utc)
        for run in runs:
            if run.id in in_memory_run_ids:
                continue
            if not _is_process_alive(run.pid):
                continue
            progress = get_step_progress(run.current_step, role=run.role, pr_number=run.pr_number)
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
                    "pipeline_variant": progress.get("pipeline_variant", "developer"),
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


async def _check_issue_conflict(issue: str, pa: ProjectAgents, *, force: bool = False) -> dict | None:
    """Check if an agent is already running for this issue (in-memory + DB).

    Returns an error dict if a conflict exists, None if clear.
    When *force* is True, stale (dead-PID) DB runs are marked interrupted
    and live conflicts are skipped so the caller can proceed.
    Must be called inside ``pa._lock``.
    """
    for existing in pa.agents.values():
        if existing.issue == issue:
            if force:
                log.info("issue_conflict.force_skipped", issue=issue, run_id=existing.run_id)
                continue
            return {
                "error": f"Issue #{issue} already has an active agent (run {existing.run_id})",
                "existing_run_id": existing.run_id,
            }

    try:
        from sqlalchemy import select

        from sova.dashboard.services.agent_recovery import _is_process_alive
        from sova.dashboard.services.work_service import _TERMINAL
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        in_memory_ids = set(pa.agents.keys())
        async with await get_session(project_dir=pa.project_dir) as session:
            async with session.begin():
                stmt = select(TaskRun).where(
                    TaskRun.issue_number == issue,
                    TaskRun.status.notin_(_TERMINAL),
                    TaskRun.pid.isnot(None),
                )
                result = await session.execute(stmt)
                runs = result.scalars().all()

                for run in runs:
                    if run.id in in_memory_ids:
                        continue
                    if _is_process_alive(run.pid):
                        if force:
                            log.info("issue_conflict.force_skipped_external", issue=issue, run_id=run.id, pid=run.pid)
                            continue
                        msg = f"Issue #{issue} already has an active agent (external run {run.id}, PID {run.pid})"
                        return {"error": msg, "existing_run_id": run.id}
                    run.status = "interrupted"
                    run.error_message = "Stale run: process no longer alive"
                    run.ended_at = datetime.now(timezone.utc)
                    log.warning("issue_conflict.auto_recovered", run_id=run.id, issue=issue, pid=run.pid)
    except Exception:
        log.warning("issue_conflict_check.db_failed", issue=issue, exc_info=True)

    return None


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
    from sova.dashboard.services.agent_output import _read_output, _read_stderr

    issue = issue.lstrip("#").strip()
    pa = _get_project_agents(slug)

    async with pa._lock:
        if len(pa.agents) >= pa.max_concurrent:
            return {
                "error": f"Maximum concurrent agents reached ({pa.max_concurrent})",
                "running": len(pa.agents),
            }

        conflict = await _check_issue_conflict(issue, pa, force=force)
        if conflict:
            return conflict

        cwd = pa.project_dir

        if not force:
            budget_error = await _check_issue_budget(issue, cwd)
            if budget_error:
                return budget_error

        run_id = await _create_task_run(issue, role or "developer", cwd, pr_number=pr_number)
        if run_id is None:
            return {"error": "Failed to create task run record"}

        prompt = f"sova run {issue} --run-id {run_id}"
        if resume_run_id:
            prompt += f" --resume {resume_run_id}"
        if role:
            prompt += f" --role {role}"
        if force:
            prompt += " --force"
        if pr_number:
            prompt += f" --pr {pr_number}"

        gh_env = await _resolve_project_gh_env(cwd)
        try:
            process = await AgentProcess.spawn(prompt=prompt, cwd=cwd, env=gh_env)
        except Exception:
            await _finalize_orphaned_run(run_id, cwd)
            return {"error": "Failed to spawn agent process"}
        pid = process.pid
        await _update_task_run_pid(run_id, pid, cwd)

        # Link to lifecycle
        await _link_run_to_lifecycle(run_id, issue, role or "developer", cwd, pr_number=pr_number)

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
    if (role or "developer") == "developer" and not pr_number:
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
    """Build the prompt for a Claude Code command."""
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
    from sova.dashboard.services.agent_output import _read_output, _read_stderr

    pa = _get_project_agents(slug)

    issue = str((args or {}).get("issue", command))

    async with pa._lock:
        if len(pa.agents) >= pa.max_concurrent:
            return {
                "error": f"Maximum concurrent agents reached ({pa.max_concurrent})",
                "running": len(pa.agents),
            }

        conflict = await _check_issue_conflict(issue, pa)
        if conflict:
            return conflict

        cwd = pa.project_dir
        prompt = _resolve_command_prompt(command, args, cwd)

        gh_env = await _resolve_project_gh_env(cwd)
        process = await AgentProcess.spawn(prompt=prompt, cwd=cwd, env=gh_env)
        role = f"command:{command}"
        run_id = await _create_task_run(issue, role, cwd, pid=process.pid)
        if run_id is None:
            await process.stop()
            return {"error": "Failed to create task run record"}

        # Link to lifecycle
        await _link_run_to_lifecycle(run_id, issue, role, cwd)

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


# -- Completion handling ------------------------------------------------------


async def _wait_and_finalize(pa: ProjectAgents, agent: AgentState) -> None:
    """Wait for the process to exit, then finalize the DB record."""
    from sova.dashboard.services.agent_handoff import _process_auto_handoff

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

    # Finalize lifecycle phase
    await _finalize_lifecycle_phase(run_id, exit_code, agent.last_result_cost or 0.0, agent.project_dir)

    try:
        from sova.config.loader import load_config
        from sova.ipc.notifications import notify

        cfg = load_config(agent.project_dir)
        role_label = agent.role.split(":")[-1].replace("-", " ").title()
        project_name = agent.project_dir.name
        if exit_code != 0:
            notify(
                cfg.notification,
                "SOVA",
                f"{project_name} | Exit code {exit_code}",
                subtitle=f"{role_label} failed #{agent.issue}",
                group=f"sova-{agent.issue}",
            )
        else:
            msg = project_name
            if cost:
                msg += f" | ${cost:.4f}"
            notify(
                cfg.notification,
                "SOVA",
                msg,
                subtitle=f"{role_label} finished #{agent.issue}",
                group=f"sova-{agent.issue}",
            )
    except Exception:
        log.debug("notify.failed", run_id=run_id, exc_info=True)

    log.info("agent.completed", run_id=run_id, issue=agent.issue, status=status, cost=cost)

    if exit_code == 0:
        await _process_auto_handoff(agent)


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


# -- Budget checks -----------------------------------------------------------


async def _check_issue_budget(issue: str, project_dir: Path) -> dict | None:
    """Check if the issue has exceeded its cumulative budget across all runs.

    Returns an error dict if over budget, None if clear.
    """
    try:
        from sova.config.loader import load_config
        from sova.dashboard.services.lifecycle_service import get_lifecycle_for_issue
        from sova.db.session import get_session

        cfg = load_config(project_dir)
        max_budget = cfg.agent.max_issue_budget

        async with await get_session(project_dir=project_dir) as session:
            lifecycle = await get_lifecycle_for_issue(session, issue)
            if lifecycle is None:
                return None

            if lifecycle.total_cost_usd >= max_budget:
                return {
                    "error": (
                        f"Issue #{issue} has exceeded the per-issue budget "
                        f"(${lifecycle.total_cost_usd:.2f} / ${max_budget:.2f}). "
                        f"Use --force to bypass."
                    ),
                    "total_cost_usd": float(lifecycle.total_cost_usd),
                    "max_issue_budget": float(max_budget),
                }
    except Exception:
        log.warning("issue_budget_check.failed", issue=issue, exc_info=True)

    return None


# -- Tracker state transitions -----------------------------------------------


async def _transition_to_in_progress(issue: str, project_dir: Path) -> None:
    """Move the issue to IN_PROGRESS on the configured tracker."""
    try:
        from sova.adapters import create_adapter
        from sova.adapters.base import TaskState
        from sova.config.loader import load_config

        cfg = load_config(project_dir)
        adapter = create_adapter(cfg)
        await adapter.transition_state(issue, TaskState.IN_PROGRESS)
        log.info("issue.transitioned", issue=issue, state="in_progress")
    except Exception:
        log.warning("issue.transition_failed", issue=issue, exc_info=True)


# -- Pipeline progress -------------------------------------------------------


_ADDRESS_REVIEW_ONLY = frozenset({"rebase", "address_review", "handoff_to_user"})


def get_step_progress(current_step: str | None, *, role: str | None = None, pr_number: int | None = None) -> dict:
    """Compute step index from current_step name.

    Uses role+pr_number only when current_step is None or "agent" (the
    dashboard outer-process TaskRun sentinel). WorkflowEngine TaskRuns
    progress through real step names and acquire pr_number mid-pipeline
    via _sync_task_run_context, so gating on current_step avoids false
    positives for developer runs that created a PR.
    """
    is_address_review = (current_step in (None, "agent") and role == "developer" and pr_number is not None) or (
        current_step is not None and current_step in _ADDRESS_REVIEW_ONLY
    )
    pipeline = ADDRESS_REVIEW_PIPELINE if is_address_review else DEVELOPER_PIPELINE
    variant = "address_review" if is_address_review else "developer"

    if current_step is None:
        return {
            "step_index": -1,
            "total_steps": len(pipeline),
            "steps": pipeline,
            "pipeline_variant": variant,
        }

    try:
        idx = pipeline.index(current_step)
    except ValueError:
        idx = -1
    return {
        "step_index": idx,
        "total_steps": len(pipeline),
        "steps": pipeline,
        "pipeline_variant": variant,
    }


# -- Lifecycle integration ----------------------------------------------------


async def _link_run_to_lifecycle(
    run_id: int,
    issue: str,
    role: str,
    project_dir: Path,
    *,
    pr_number: int | None = None,
) -> None:
    """Link a newly created TaskRun to an IssueLifecycle."""
    try:
        from sova.dashboard.services.lifecycle_service import link_task_run_to_lifecycle
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                run = await session.get(TaskRun, run_id)
                if run:
                    await link_task_run_to_lifecycle(session, run)
    except Exception:
        log.warning("lifecycle.link_failed", run_id=run_id, issue=issue, exc_info=True)


async def _finalize_lifecycle_phase(
    run_id: int,
    exit_code: int,
    cost: float,
    project_dir: Path,
) -> None:
    """Update lifecycle phase status after a run completes."""
    try:
        from sova.dashboard.services.lifecycle_service import finalize_phase_from_run
        from sova.db.session import get_session

        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                await finalize_phase_from_run(session, run_id, exit_code, cost)
    except Exception:
        log.warning("lifecycle.finalize_failed", run_id=run_id, exc_info=True)
