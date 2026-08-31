"""Agent process lifecycle: start/stop/wait, status queries.

Manages concurrent agent processes per project.
Uses sova.ipc.runtime.AgentRuntime under the hood.
Delegates DB persistence to agent_db, pool management to agent_pool,
context resolution to agent_context, validation to agent_validation,
pipeline progress to agent_progress, resource monitoring to agent_resource,
completion handling to agent_finalize, and approval flow to agent_approval.
"""

from __future__ import annotations

import asyncio
import shlex
import time
from pathlib import Path

from sova.dashboard.services.agent_approval import (  # re-export facade
    _claim_awaiting_approval as _claim_awaiting_approval,
)
from sova.dashboard.services.agent_approval import (
    _clear_handoff_for_issue as _clear_handoff_for_issue,
)
from sova.dashboard.services.agent_approval import (
    _finalize_lifecycle_phase as _finalize_lifecycle_phase,
)
from sova.dashboard.services.agent_approval import (
    _link_run_to_lifecycle as _link_run_to_lifecycle,
)
from sova.dashboard.services.agent_approval import (
    complete_awaiting_approval_by_issue as complete_awaiting_approval_by_issue,
)
from sova.dashboard.services.agent_approval import (
    reject_spec as reject_spec,
)
from sova.dashboard.services.agent_approval import (
    resume_from_approval as resume_from_approval,
)
from sova.dashboard.services.agent_context import (
    _resolve_branch_name as _resolve_branch_name,
)
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
    _update_task_run_output_path,
    _update_task_run_pid,
)
from sova.dashboard.services.agent_finalize import (
    _DB_TERMINAL_POLL_INTERVAL as _DB_TERMINAL_POLL_INTERVAL,
)
from sova.dashboard.services.agent_finalize import (  # re-export facade
    _MERGE_ROLES as _MERGE_ROLES,
)
from sova.dashboard.services.agent_finalize import (
    _check_merge_queue_marker_file as _check_merge_queue_marker_file,
)
from sova.dashboard.services.agent_finalize import (
    _check_merge_queue_on_failure as _check_merge_queue_on_failure,
)
from sova.dashboard.services.agent_finalize import (
    _crash_recovery_cleanup as _crash_recovery_cleanup,
)
from sova.dashboard.services.agent_finalize import (
    _is_run_terminal_in_db as _is_run_terminal_in_db,
)
from sova.dashboard.services.agent_finalize import (
    _wait_and_finalize as _wait_and_finalize,
)
from sova.dashboard.services.agent_finalize import (
    _wait_with_terminal_check as _wait_with_terminal_check,
)
from sova.dashboard.services.agent_pool import (
    AgentState as AgentState,
)
from sova.dashboard.services.agent_pool import (
    CompletedAgent as CompletedAgent,
)
from sova.dashboard.services.agent_pool import (
    ProjectAgents as ProjectAgents,
)
from sova.dashboard.services.agent_pool import (
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
from sova.dashboard.services.agent_resource import (  # re-export facade
    _background_tasks as _background_tasks,
)
from sova.dashboard.services.agent_resource import (
    _cancel_agent_io_tasks as _cancel_agent_io_tasks,
)
from sova.dashboard.services.agent_resource import (
    _compute_and_store_energy as _compute_and_store_energy,
)
from sova.dashboard.services.agent_resource import (
    _finalize_resource_monitoring as _finalize_resource_monitoring,
)
from sova.dashboard.services.agent_resource import (
    _resource_flush_loop as _resource_flush_loop,
)
from sova.dashboard.services.agent_resource import (
    _start_resource_monitoring as _start_resource_monitoring,
)
from sova.dashboard.services.agent_resource import (
    cancel_background_tasks as cancel_background_tasks,
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
from sova.dashboard.services.agent_validation import (
    check_memory_pressure as check_memory_pressure,
)
from sova.dashboard.services.feed_service import emit_safe
from sova.dashboard.services.output_service import OutputWriter
from sova.ipc.runtime import _PIPELINE_ROLES, get_runtime, spawn_direct
from sova.utils.formatting import decimal_to_json
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.control")


# Status queries


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
        cpu_pct = None
        mem_rss = None
        collector = agent.resource_collector
        if collector is not None and collector.samples:
            latest = collector.samples[-1]
            cpu_pct = latest.cpu_percent
            mem_rss = latest.memory_rss_bytes
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
                "cpu_percent": cpu_pct,
                "memory_rss_bytes": mem_rss,
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
    """Get all agents: dashboard-spawned (in-memory) + CLI-spawned (DB with alive PID)."""
    from datetime import datetime, timezone

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


# Agent lifecycle


async def _recover_last_pr_number(issue: str, project_dir: "Path") -> int | None:
    """Return the pr_number from the most recent non-successful terminal developer run for an issue."""
    try:
        from sqlalchemy import select

        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session(project_dir=project_dir) as session:
            stmt = (
                select(TaskRun.pr_number)
                .where(
                    TaskRun.issue_number == issue,
                    TaskRun.role == "developer",
                    TaskRun.pr_number.is_not(None),
                    TaskRun.status.in_({"interrupted", "failed", "paused"}),
                )
                .order_by(TaskRun.id.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            pr = result.scalar_one_or_none()
            if pr is not None:
                log.info("start_agent.recovered_pr_number", issue=issue, pr_number=pr)
            return pr
    except Exception:
        log.debug("start_agent.recover_pr_number_failed", issue=issue, exc_info=True)
        return None


def _resolve_config_model(project_dir: Path) -> str | None:
    """Return the agent model alias from sova.toml, or None if unset."""
    try:
        from sova.config.loader import load_config

        cfg = load_config(project_dir)
        return cfg.agent.model or None
    except Exception:
        log.debug("resolve_config_model.failed", exc_info=True)
        return None


def _resolve_config_fallback_model(project_dir: Path) -> str | None:
    """Return the first fallback model from sova.toml, or None if empty."""
    try:
        from sova.config.loader import load_config

        cfg = load_config(project_dir)
        return cfg.agent.fallback_models[0] if cfg.agent.fallback_models else None
    except Exception:
        log.debug("resolve_config_fallback_model.failed", exc_info=True)
        return None


async def start_agent(
    issue: str,
    *,
    role: str | None = None,
    force: bool = False,
    slug: str | None = None,
    resume_run_id: int | None = None,
    pr_number: int | None = None,
    model: str | None = None,
    _skip_handoff_clear: bool = False,
) -> dict:
    """Start an agent process for the given issue."""
    from sova.dashboard.services.agent_db import _capture_pr_head_sha
    from sova.dashboard.services.agent_output import _read_output, _read_stderr

    issue = issue.lstrip("#").strip() if issue else ""
    pa = _get_project_agents(slug)
    if not issue and pr_number:
        issue = await _resolve_issue_from_pr(pr_number, pa.project_dir)

    if issue and (role or "developer") == "developer" and not pr_number:
        pr_number = await _recover_last_pr_number(issue, pa.project_dir)

    if not force:
        mem_block, mem_warn = check_memory_pressure(pa.project_dir)
        if mem_block:
            return mem_block
        if mem_warn:
            log.warning("start_agent.memory_pressure_warning", message=mem_warn)

    pre_run_sha = None
    if pr_number:
        pre_run_sha = await _capture_pr_head_sha(pr_number, pa.project_dir)

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

        project_dir = pa.project_dir

        branch_name = await _resolve_branch_name(pr_number, project_dir)
        cwd = await _resolve_issue_worktree(issue, project_dir, branch_name=branch_name, pr_number=pr_number)

        if pr_number and cwd.resolve() == project_dir.resolve():
            log.error("agent.worktree_isolation_failed", pr=pr_number, issue=issue)
            return {"error": f"Cannot start PR-based run: worktree isolation failed for PR {pr_number}"}

        if not force and issue:
            budget_error = await _check_issue_budget(issue, project_dir)
            if budget_error:
                return budget_error
        elif not issue:
            log.info(
                "agent.issueless_budget_skip",
                role=role,
                detail="Per-issue budget N/A; per-run budget still applies",
            )

        run_id = await _create_task_run(issue or None, role or "developer", project_dir, pr_number=pr_number)
        if run_id is None:
            return {"error": "Failed to create task run record"}

        if not _skip_handoff_clear:
            try:
                from sova.dashboard.services import handoff_service

                if issue:
                    handoff_service.clear_handoff(project_dir, issue=issue)
                else:
                    handoff_service.clear_handoff(project_dir, issue=role or "run")
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

        effective_role = role or "developer"
        gh_env = await _resolve_project_gh_env(project_dir)

        from sova.dashboard.services.agent_context import merge_mcp_env

        gh_env = merge_mcp_env(gh_env, run_id, project_dir)
        output_dir = project_dir / ".claude" / "agent-output"

        try:
            output_dir.mkdir(parents=True, exist_ok=True)

            if effective_role in _PIPELINE_ROLES:
                log.info(
                    "agent.spawn_direct",
                    run_id=run_id,
                    role=effective_role,
                    cwd=str(cwd),
                    cmd=cmd_parts[:4],
                )
                process = await spawn_direct(
                    cmd_parts,
                    cwd,
                    env=gh_env,
                    output_dir=output_dir,
                    run_label=str(run_id),
                )
            else:
                log.info(
                    "agent.spawn_claude",
                    run_id=run_id,
                    role=effective_role,
                    cwd=str(cwd),
                )
                cmd = " ".join(cmd_parts)
                prompt = (
                    "Run the following command in your bash shell. This is a CLI "
                    "command, not a task description -- do not implement the work "
                    "yourself. Execute it exactly as written and let it complete:\n\n"
                    f"```bash\n{cmd}\n```"
                )
                if not model:
                    model = _resolve_config_model(project_dir)
                fallback_model = _resolve_config_fallback_model(project_dir)
                process = await get_runtime().spawn(
                    prompt,
                    cwd,
                    env=gh_env,
                    model=model,
                    fallback_model=fallback_model,
                    output_dir=output_dir,
                    run_label=str(run_id),
                )
        except Exception:
            log.error("agent.spawn_failed", run_id=run_id, exc_info=True)
            await _finalize_orphaned_run(run_id, project_dir)
            return {"error": "Failed to spawn agent process"}
        pid = process.pid
        await _update_task_run_pid(run_id, pid, project_dir)
        await _update_task_run_output_path(run_id, str(output_dir / f"{run_id}.stdout"), project_dir)

        # Link to lifecycle (only for issue-based runs)
        if issue:
            await _link_run_to_lifecycle(run_id, issue, role or "developer", project_dir, pr_number=pr_number)

        writer = OutputWriter(project_dir, run_id)

        agent = AgentState(
            run_id=run_id,
            issue=issue,
            role=role or "developer",
            process=process,
            output_writer=writer,
            pr_number=pr_number,
            pre_run_sha=pre_run_sha,
            prompt=" ".join(cmd_parts),
            project_dir=project_dir,
        )
        pa.agents[run_id] = agent

    agent.reader_task = asyncio.create_task(_read_output(agent))
    agent.stderr_task = asyncio.create_task(_read_stderr(agent))
    _start_resource_monitoring(agent, project_dir, pid)
    wait_task = asyncio.create_task(_wait_and_finalize(pa, agent))
    _background_tasks.add(wait_task)
    wait_task.add_done_callback(_background_tasks.discard)
    if issue and (role or "developer") == "developer" and not pr_number:
        transition_task = asyncio.create_task(_transition_to_in_progress(issue, pa.project_dir))
        _background_tasks.add(transition_task)
        transition_task.add_done_callback(_background_tasks.discard)

    log.info("agent.started", issue=issue, pid=pid, run_id=run_id, cwd=str(cwd), project_dir=str(project_dir))

    label = f"#{issue}" if issue else "Agent"
    role_label = (role or "developer").capitalize()
    emit_safe(
        f"{label} {role_label} started",
        category="agent",
        metadata={"run_id": run_id, "issue": issue, "role": role or "developer"},
    )

    return {"status": "started", "pid": pid, "run_id": run_id}


async def stop_agent(slug: str | None = None, *, run_id: int | None = None) -> dict:
    """Stop a running agent process."""
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
    from sova.dashboard.services.agent_db import _capture_pr_head_sha
    from sova.dashboard.services.agent_output import _read_output, _read_stderr

    pa = _get_project_agents(slug)
    safe_args = args or {}
    pr_number, issue = await _resolve_command_context(safe_args, command, pa.project_dir)

    mem_block, mem_warn = check_memory_pressure(pa.project_dir)
    if mem_block:
        return mem_block
    if mem_warn:
        log.warning("start_command.memory_pressure_warning", message=mem_warn)

    pre_run_sha = None
    if pr_number:
        pre_run_sha = await _capture_pr_head_sha(pr_number, pa.project_dir)

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

        branch_name = await _resolve_branch_name(pr_number, project_dir)
        cwd = await _resolve_issue_worktree(issue, project_dir, branch_name=branch_name, pr_number=pr_number)

        if pr_number and cwd.resolve() == project_dir.resolve():
            log.error("command.worktree_isolation_failed", pr=pr_number, command=command)
            return {"error": f"Cannot start PR-based command: worktree isolation failed for PR {pr_number}"}

        prompt = _resolve_command_prompt(command, args, project_dir)

        try:
            from sova.dashboard.services import handoff_service

            handoff_service.clear_handoff(project_dir, issue=issue)
        except Exception:
            log.debug("command.clear_handoff_failed", issue=issue, exc_info=True)

        model = _resolve_config_model(project_dir)
        fallback_model = _resolve_config_fallback_model(project_dir)

        gh_env = await _resolve_project_gh_env(project_dir)

        role = f"command:{command}"
        pre_run_id = await _create_task_run(issue, role, project_dir, pr_number=pr_number)
        if pre_run_id is None:
            return {"error": "Failed to create task run record"}

        from sova.dashboard.services.agent_context import merge_mcp_env

        gh_env = merge_mcp_env(gh_env, pre_run_id, project_dir)
        output_dir = project_dir / ".claude" / "agent-output"

        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            process = await get_runtime().spawn(
                prompt,
                cwd,
                env=gh_env,
                model=model,
                fallback_model=fallback_model,
                output_dir=output_dir,
                run_label=str(pre_run_id),
            )
        except Exception as exc:
            log.error("command.spawn_failed", command=command, issue=issue, error=str(exc), exc_info=True)
            await _finalize_orphaned_run(pre_run_id, project_dir)
            return {"error": f"Failed to spawn runtime: {exc}"}

        run_id = pre_run_id
        await _update_task_run_pid(run_id, process.pid, project_dir)
        await _update_task_run_output_path(run_id, str(output_dir / f"{run_id}.stdout"), project_dir)

        await _link_run_to_lifecycle(run_id, issue, role, project_dir)

        writer = OutputWriter(project_dir, run_id)

        agent = AgentState(
            run_id=run_id,
            issue=issue,
            role=role,
            process=process,
            output_writer=writer,
            pr_number=pr_number,
            pre_run_sha=pre_run_sha,
            prompt=prompt,
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
