"""Agent process lifecycle -- start/stop/wait, status queries.

Manages concurrent agent processes per project.
Uses sova.ipc.runtime.AgentRuntime under the hood.
Delegates DB persistence to agent_db, pool management to agent_pool,
context resolution to agent_context, validation to agent_validation,
and pipeline progress to agent_progress.
"""

from __future__ import annotations

import asyncio
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from sova.dashboard.services.agent_context import (  # re-export facade
    _resolve_command_context as _resolve_command_context,
)
from sova.dashboard.services.agent_context import (
    _resolve_command_prompt as _resolve_command_prompt,
)
from sova.dashboard.services.agent_context import (
    _resolve_issue_from_pr as _resolve_issue_from_pr,
)
from sova.dashboard.services.agent_context import (
    _resolve_issue_worktree as _resolve_issue_worktree,
)
from sova.dashboard.services.agent_context import (
    _resolve_project_gh_env as _resolve_project_gh_env,
)
from sova.dashboard.services.agent_context import (
    _strip_frontmatter as _strip_frontmatter,
)
from sova.dashboard.services.agent_db import (
    _create_task_run,
    _fetch_run_states,
    _finalize_orphaned_run,
    _finalize_task_run,
    _update_task_run_pid,
)
from sova.dashboard.services.agent_pool import (
    AgentState,
    CompletedAgent,
    ProjectAgents,
    _evict_completed_for_issue,
    _get_project_agents,
    _prune_completed,
)
from sova.dashboard.services.agent_progress import (
    _ADDRESS_REVIEW_ONLY as _ADDRESS_REVIEW_ONLY,
)
from sova.dashboard.services.agent_progress import (
    _PLANNER_ONLY as _PLANNER_ONLY,
)
from sova.dashboard.services.agent_progress import (
    _RESEARCHER_ONLY as _RESEARCHER_ONLY,
)
from sova.dashboard.services.agent_progress import (
    _STANDALONE_ROLES as _STANDALONE_ROLES,
)
from sova.dashboard.services.agent_progress import (  # re-export facade
    ADDRESS_REVIEW_PIPELINE as ADDRESS_REVIEW_PIPELINE,
)
from sova.dashboard.services.agent_progress import (
    DEVELOPER_PIPELINE as DEVELOPER_PIPELINE,
)
from sova.dashboard.services.agent_progress import (
    PLANNER_PIPELINE as PLANNER_PIPELINE,
)
from sova.dashboard.services.agent_progress import (
    RESEARCHER_PIPELINE as RESEARCHER_PIPELINE,
)
from sova.dashboard.services.agent_progress import (
    _detect_pipeline as _detect_pipeline,
)
from sova.dashboard.services.agent_progress import (
    get_step_progress as get_step_progress,
)
from sova.dashboard.services.agent_validation import (  # re-export facade
    _check_issue_budget as _check_issue_budget,
)
from sova.dashboard.services.agent_validation import (
    _check_issue_conflict as _check_issue_conflict,
)
from sova.dashboard.services.agent_validation import (
    _check_pr_merged_on_failure as _check_pr_merged_on_failure,
)
from sova.dashboard.services.agent_validation import (
    _transition_to_in_progress as _transition_to_in_progress,
)
from sova.dashboard.services.output_service import OutputWriter
from sova.ipc.runtime import get_runtime
from sova.utils.formatting import decimal_to_json
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.control")

_background_tasks: set[asyncio.Task[None]] = set()


def _start_resource_monitoring(agent: AgentState, project_dir: Path, pid: int) -> None:
    """Start resource collector and writer for an agent (if monitoring is enabled)."""
    try:
        from sova.config.loader import load_config

        cfg = load_config(project_dir)
        if not cfg.monitoring.enabled:
            return
    except Exception:
        log.debug("resource_monitoring.config_load_failed", exc_info=True)
        return

    try:
        from sova.monitoring.collector import ResourceCollector
        from sova.monitoring.writer import ResourceWriter

        collector = ResourceCollector(pid=pid, interval=cfg.monitoring.interval)
        collector.start()
        writer = ResourceWriter(project_dir, agent.run_id)
        agent.resource_collector = collector
        agent.resource_writer = writer
        agent.resource_flush_task = asyncio.create_task(_resource_flush_loop(agent))
    except Exception:
        log.debug("resource_monitoring.start_failed", run_id=agent.run_id, exc_info=True)


async def _finalize_resource_monitoring(agent: AgentState) -> None:
    """Stop the collector, flush remaining samples, write summary, close writer."""
    try:
        # Cancel the periodic flush task
        if agent.resource_flush_task and not agent.resource_flush_task.done():
            agent.resource_flush_task.cancel()
            try:
                await agent.resource_flush_task
            except asyncio.CancelledError:
                pass

        collector = agent.resource_collector
        writer = agent.resource_writer
        if collector is None or writer is None:
            return

        summary = await collector.stop()

        # Drain any remaining samples from the deque
        while collector.samples:
            writer.add_sample(collector.samples.popleft())

        await writer.write_summary(summary)
        await writer.close()
    except Exception:
        log.warning("resource_monitoring.finalize_failed", run_id=agent.run_id, exc_info=True)


async def _resource_flush_loop(agent: AgentState) -> None:
    """Periodically flush buffered resource samples to the database."""
    try:
        while True:
            await asyncio.sleep(30.0)
            collector = agent.resource_collector
            writer = agent.resource_writer
            if collector is None or writer is None:
                return
            # Drain with popleft to avoid losing samples appended during iteration
            while collector.samples:
                writer.add_sample(collector.samples.popleft())
            await writer.flush()
    except asyncio.CancelledError:
        return
    except Exception:
        log.debug("resource_flush_loop.failed", run_id=agent.run_id, exc_info=True)


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
                    "cost_usd": decimal_to_json(run.total_cost_usd),
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
    _skip_handoff_clear: bool = False,
) -> dict:
    """Start an agent process for the given issue."""
    from sova.dashboard.services.agent_output import _read_output, _read_stderr

    issue = issue.lstrip("#").strip() if issue else ""
    pa = _get_project_agents(slug)

    async with pa._lock:
        if len(pa.agents) >= pa.max_concurrent:
            return {
                "error": f"Maximum concurrent agents reached ({pa.max_concurrent})",
                "running": len(pa.agents),
            }

        if issue:
            conflict = await _check_issue_conflict(issue, pa, force=force)
            if conflict:
                return conflict
            _evict_completed_for_issue(pa, issue)

        cwd = pa.project_dir

        if not force and issue:
            budget_error = await _check_issue_budget(issue, cwd)
            if budget_error:
                return budget_error
        elif not issue:
            log.info(
                "agent.issueless_budget_skip",
                role=role,
                detail="Per-issue budget N/A; per-run budget still applies",
            )

        run_id = await _create_task_run(issue or None, role or "developer", cwd, pr_number=pr_number)
        if run_id is None:
            return {"error": "Failed to create task run record"}

        if not _skip_handoff_clear:
            try:
                from sova.dashboard.services import handoff_service

                if issue:
                    handoff_service.clear_handoff(cwd, issue=issue)
                else:
                    handoff_service.clear_handoff(cwd, issue=role or "run")
            except Exception:
                log.debug("agent.clear_handoff_failed", issue=issue or role, exc_info=True)

        cmd_parts = ["sova", "run"]
        if issue:
            cmd_parts.append(shlex.quote(issue))
        cmd_parts.extend(["--run-id", str(run_id)])
        if resume_run_id:
            cmd_parts.extend(["--resume", str(resume_run_id)])
        if role:
            cmd_parts.extend(["--role", shlex.quote(role)])
        if force:
            cmd_parts.append("--force")
        if pr_number:
            cmd_parts.extend(["--pr", str(pr_number)])
        cmd = " ".join(cmd_parts)
        prompt = (
            "Run the following command in your bash shell. This is a CLI "
            "command, not a task description -- do not implement the work "
            "yourself. Execute it exactly as written and let it complete:\n\n"
            f"```bash\n{cmd}\n```"
        )

        gh_env = await _resolve_project_gh_env(cwd)
        try:
            process = await get_runtime().spawn(prompt, cwd, env=gh_env)
        except Exception:
            await _finalize_orphaned_run(run_id, cwd)
            return {"error": "Failed to spawn agent process"}
        pid = process.pid
        await _update_task_run_pid(run_id, pid, cwd)

        # Link to lifecycle (only for issue-based runs)
        if issue:
            await _link_run_to_lifecycle(run_id, issue, role or "developer", cwd, pr_number=pr_number)

        writer = OutputWriter(cwd, run_id)

        agent = AgentState(
            run_id=run_id,
            issue=issue,
            role=role or "developer",
            process=process,
            output_writer=writer,
            pr_number=pr_number,
            project_dir=cwd,
        )
        pa.agents[run_id] = agent

    agent.reader_task = asyncio.create_task(_read_output(agent))
    agent.stderr_task = asyncio.create_task(_read_stderr(agent))
    _start_resource_monitoring(agent, cwd, pid)
    wait_task = asyncio.create_task(_wait_and_finalize(pa, agent))
    _background_tasks.add(wait_task)
    wait_task.add_done_callback(_background_tasks.discard)
    if issue and (role or "developer") == "developer" and not pr_number:
        transition_task = asyncio.create_task(_transition_to_in_progress(issue, pa.project_dir))
        _background_tasks.add(transition_task)
        transition_task.add_done_callback(_background_tasks.discard)

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

        pending: list[asyncio.Task[None]] = []
        if agent.reader_task and not agent.reader_task.done():
            agent.reader_task.cancel()
            pending.append(agent.reader_task)
        if agent.stderr_task and not agent.stderr_task.done():
            agent.stderr_task.cancel()
            pending.append(agent.stderr_task)
        if agent.resource_flush_task and not agent.resource_flush_task.done():
            agent.resource_flush_task.cancel()
            pending.append(agent.resource_flush_task)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    log.info("agent.stopped", pid=pid, run_id=agent.run_id)
    return {"status": "stopped", "pid": pid, "run_id": agent.run_id}


async def start_command(
    command: str,
    args: dict | None = None,
    slug: str | None = None,
) -> dict:
    """Start a Claude Code command (e.g. /agent-resume, /integrate-pr)."""
    from sova.dashboard.services.agent_output import _read_output, _read_stderr

    pa = _get_project_agents(slug)
    safe_args = args or {}
    pr_number, issue = await _resolve_command_context(safe_args, command, pa.project_dir)

    async with pa._lock:
        if len(pa.agents) >= pa.max_concurrent:
            return {
                "error": f"Maximum concurrent agents reached ({pa.max_concurrent})",
                "running": len(pa.agents),
            }

        if issue:
            conflict = await _check_issue_conflict(issue, pa)
            if conflict:
                return conflict

            _evict_completed_for_issue(pa, issue)

        project_dir = pa.project_dir

        branch_name = ""
        if pr_number:
            try:
                from sova.config.loader import load_config
                from sova.git.pr import get_pr_branch

                cfg = load_config(project_dir)
                if cfg.github_repo:
                    branch_name = await get_pr_branch(
                        pr_number,
                        repo=cfg.github_repo,
                        github_user=cfg.github_user,
                    )
                    if not branch_name:
                        log.debug("command.pr_branch_empty", pr=pr_number)
            except (RuntimeError, KeyError, subprocess.CalledProcessError, FileNotFoundError):
                log.debug("command.pr_branch_lookup_failed", pr=pr_number, exc_info=True)

        cwd = await _resolve_issue_worktree(issue, project_dir, branch_name=branch_name)
        prompt = _resolve_command_prompt(command, args, project_dir)

        try:
            from sova.dashboard.services import handoff_service

            handoff_service.clear_handoff(project_dir, issue=issue)
        except Exception:
            log.debug("command.clear_handoff_failed", issue=issue, exc_info=True)

        gh_env = await _resolve_project_gh_env(project_dir)
        try:
            process = await get_runtime().spawn(prompt, cwd, env=gh_env)
        except Exception as exc:
            log.error("command.spawn_failed", command=command, issue=issue, error=str(exc), exc_info=True)
            return {"error": f"Failed to spawn runtime: {exc}"}
        role = f"command:{command}"
        run_id = await _create_task_run(issue, role, project_dir, pid=process.pid, pr_number=pr_number)
        if run_id is None:
            await process.stop()
            return {"error": "Failed to create task run record"}

        # Link to lifecycle
        await _link_run_to_lifecycle(run_id, issue, role, project_dir)

        writer = OutputWriter(project_dir, run_id)

        agent = AgentState(
            run_id=run_id,
            issue=issue,
            role=role,
            process=process,
            output_writer=writer,
            pr_number=pr_number,
            project_dir=project_dir,
        )
        pa.agents[run_id] = agent

    agent.reader_task = asyncio.create_task(_read_output(agent))
    agent.stderr_task = asyncio.create_task(_read_stderr(agent))
    _start_resource_monitoring(agent, project_dir, process.pid)
    wait_task = asyncio.create_task(_wait_and_finalize(pa, agent))
    _background_tasks.add(wait_task)
    wait_task.add_done_callback(_background_tasks.discard)

    log.info("command.started", command=command, pid=process.pid, run_id=run_id, cwd=str(cwd))
    return {"status": "started", "pid": process.pid, "run_id": run_id}


# -- Completion handling ------------------------------------------------------

_MERGE_ROLES = frozenset({"integrate-pr", "approve-merge"})


async def _wait_and_finalize(pa: ProjectAgents, agent: AgentState) -> None:
    """Wait for the process to exit, then finalize the DB record."""
    from sova.dashboard.services.agent_handoff import _process_auto_handoff

    if agent.process is None:
        return

    exit_code = await agent.process.wait()
    run_id = agent.run_id

    status = "done" if exit_code == 0 else "failed"

    if exit_code != 0 and agent.pr_number is not None:
        cmd_name = agent.role.removeprefix("command:").removeprefix("/").split()[0]
        if cmd_name in _MERGE_ROLES:
            if await _check_pr_merged_on_failure(agent.pr_number, agent.project_dir):
                log.info(
                    "finalize.merge_succeeded_despite_crash",
                    run_id=run_id,
                    pr=agent.pr_number,
                    exit_code=exit_code,
                )
                status = "done"
                exit_code = 0

    cost = agent.last_result_cost or 0.0

    if agent.output_writer:
        try:
            await agent.output_writer.close()
        except Exception:
            log.warning("output_writer.close_failed", run_id=run_id, exc_info=True)

    await _finalize_resource_monitoring(agent)

    # Finalize the DB record BEFORE removing from pa.agents so the
    # liveness sweep (which skips managed run_ids) cannot race us and
    # stamp the run "interrupted" in the window between pop and finalize.
    await _finalize_task_run(run_id, exit_code=exit_code, agent=agent)

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

    # Finalize lifecycle phase (only for issue-based runs)
    if agent.issue:
        await _finalize_lifecycle_phase(run_id, exit_code, agent.last_result_cost or 0.0, agent.project_dir)

    try:
        from sova.config.loader import load_config
        from sova.ipc.notifications import notify

        cfg = load_config(agent.project_dir)
        role_label = agent.role.split(":")[-1].replace("-", " ").title()
        project_name = agent.project_dir.name
        issue_label = f"#{agent.issue}" if agent.issue else role_label
        group_key = f"sova-{agent.issue or agent.role or 'run'}"
        if exit_code != 0:
            notify(
                cfg.notification,
                "SOVA",
                f"{project_name} | Exit code {exit_code}",
                subtitle=f"{role_label} failed {issue_label}",
                group=group_key,
            )
        else:
            msg = project_name
            if cost:
                msg += f" | ${cost:.4f}"
            notify(
                cfg.notification,
                "SOVA",
                msg,
                subtitle=f"{role_label} finished {issue_label}",
                group=group_key,
            )
    except Exception:
        log.debug("notify.failed", run_id=run_id, exc_info=True)

    log.info("agent.completed", run_id=run_id, issue=agent.issue, status=status, cost=cost)

    if exit_code == 0:
        await _process_auto_handoff(agent)


# -- Approval resume ----------------------------------------------------------


async def resume_from_approval(run_id: int) -> dict:
    """Resume a paused pipeline run after human approval.

    Validates that the TaskRun exists and has status ``awaiting_approval``,
    then spawns a new agent with ``resume_run_id`` pointing to the paused run.
    Clears the handoff file on success to prevent stale UI buttons.

    Returns a dict with the new ``run_id``, ``resumed_from``, ``issue``, and ``role``.
    Raises appropriate errors (via returned dict) for 404 and 409 cases.
    """
    from sqlalchemy import update

    from sova.core.state import TaskStatus
    from sova.db.models import TaskRun
    from sova.db.session import get_session

    async with await get_session() as session:
        async with session.begin():
            task_run = await session.get(TaskRun, run_id)

    if task_run is None:
        return {"error": "not_found", "detail": f"TaskRun #{run_id} not found"}

    if task_run.status != TaskStatus.AWAITING_APPROVAL:
        return {
            "error": "conflict",
            "detail": f"Run #{run_id} has status '{task_run.status}', expected 'awaiting_approval'",
        }

    # Atomic CAS: claim the run so a concurrent request sees "pending" and fails
    async with await get_session() as session:
        async with session.begin():
            result = await session.execute(
                update(TaskRun)
                .where(TaskRun.id == run_id, TaskRun.status == TaskStatus.AWAITING_APPROVAL)
                .values(status=TaskStatus.PENDING)
            )
            if result.rowcount == 0:
                return {
                    "error": "conflict",
                    "detail": f"Run #{run_id} was already claimed by another request",
                }

    issue = task_run.issue_number or ""
    role = task_run.role or "developer"
    pr_number = task_run.pr_number

    # Spawn first, clear state second -- _skip_handoff_clear prevents start_agent
    # from clearing the approval handoff before spawn succeeds
    result = await start_agent(
        issue,
        role=role,
        resume_run_id=run_id,
        pr_number=pr_number,
        force=True,
        _skip_handoff_clear=True,
    )

    if "error" in result:
        # Revert the CAS so the approval button reappears on failure
        async with await get_session() as session:
            async with session.begin():
                await session.execute(
                    update(TaskRun)
                    .where(TaskRun.id == run_id, TaskRun.status == TaskStatus.PENDING)
                    .values(status=TaskStatus.AWAITING_APPROVAL)
                )
        return result

    # Clear handoff file on successful spawn
    if issue:
        try:
            from sova.dashboard.services import handoff_service

            handoff_service.clear_handoff(issue=issue)
        except Exception:
            log.debug("resume_from_approval.clear_handoff_failed", issue=issue, exc_info=True)

    return {
        "run_id": result["run_id"],
        "resumed_from": run_id,
        "issue": issue,
        "role": role,
    }


# -- Lifecycle integration ----------------------------------------------------


async def _link_run_to_lifecycle(
    run_id: int,
    issue: str,
    _role: str,
    project_dir: Path,
    *,
    pr_number: int | None = None,  # noqa: ARG001
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
