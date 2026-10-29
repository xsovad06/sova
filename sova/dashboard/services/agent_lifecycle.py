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

from sqlalchemy.exc import SQLAlchemyError

from sova.config.models import RUNTIME_MODEL_PIN_ROLES
from sova.core.output import OutputWriter
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
from sova.dashboard.services.agent_validation import (
    check_network_connectivity as check_network_connectivity,
)
from sova.dashboard.services.agent_validation import (
    check_stale_command as check_stale_command,
)
from sova.dashboard.services.feed_service import emit_safe
from sova.ipc.runtime import (
    _PIPELINE_ROLES,
    RUN_RUNTIME_MODEL_OVERRIDE_ENV,
    _codex_extra_env,
    get_runtime,
    spawn_direct,
)
from sova.llm.backends import TIER_NAMES
from sova.llm.client import RUN_MODEL_OVERRIDE_ENV
from sova.utils.formatting import format_cost_usd
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.control")

# Roles spawned as a plain trusted subprocess (spawn_direct) rather than
# through a coding-agent runtime wrapper. Reviewer joins the pipeline roles
# here, scoped to this one spawn-path decision only: ``_PIPELINE_ROLES``
# names the WorkflowEngine-driven step-pipeline roles, which ReviewerRole is
# not (it bypasses WorkflowEngine entirely). Rather than add "reviewer" to
# that constant, it is unioned in here, local to this one spawn-path
# decision. Note this is a real duplication hazard, not just a naming one:
# ``agent_db.py`` and ``work_service.py`` each independently define their own
# local ``_PIPELINE_ROLES`` frozenset: ``agent_db.py``'s copy gates
# ``_validate_pipeline_outcome()`` (pipeline-bypass detection at run exit),
# ``work_service.py``'s gates step-count display (neither imports this
# module's constant, and work_service.py's copy doesn't even include
# "planner"), so editing one copy does not propagate to the others.
# The reviewer never needed interactive coding-agent tool-use: it only makes
# a model call over a diff plus GitHub/DB writes, so running it outside any
# runtime sandbox is correct regardless of ``agent.runtime``.
_DIRECT_SPAWN_ROLES = _PIPELINE_ROLES | {"reviewer"}

# Roles whose spawned ``sova run`` child reads the per-run "Internal LLM model"
# override (``RUN_MODEL_OVERRIDE_ENV``) in ``_invoke_with_fallback``. That env
# var only reaches the child through ``spawn_direct(extra_env=...)`` below, so
# the set is exactly the direct-spawn roles rather than its own literal: a
# second copy of the same four names would silently stop matching the spawn
# path the moment a pipeline role is added. Any other role is rejected up
# front rather than launched with an override that never reaches a model call.
LLM_MODEL_OVERRIDE_ROLES = _DIRECT_SPAWN_ROLES

# Direct-spawn roles whose launch-modal Model choice reaches a runtime: the
# ``sova run`` child reads it from ``RUN_RUNTIME_MODEL_OVERRIDE_ENV`` and pins
# every tool-using step to it (agent_dispatch, issue #1039). The same set the
# config pin uses (sova.config.models.RUNTIME_MODEL_PIN_ROLES), since only
# those roles have steps that reach the coding-agent runtime. Any other
# direct-spawn role keeps ignoring the choice with a warning, and so does a
# bare tier alias for any role: see _is_runtime_model_pin().


def _is_runtime_model_pin(role: str, model: str | None) -> bool:
    """Whether a start request's *model* becomes a runtime pin for *role*.

    A bare tier alias (``sonnet``) is not a catalog ID: pinned, it would skip
    the backend-aware ``resolve_alias()`` every routed model goes through, and
    ``backend_can_serve()`` documents a bare tier name as unservable on Vertex
    and Bedrock. The alias keeps the old behavior for a direct-spawn role: it
    is ignored with a warning.
    """
    return bool(model) and role in RUNTIME_MODEL_PIN_ROLES and model not in TIER_NAMES


def llm_model_override_role_error(role: str | None) -> str | None:
    """Return why *role* cannot take a per-run LLM model override, or None if it can."""
    effective_role = role if role is not None else "developer"
    if effective_role in LLM_MODEL_OVERRIDE_ROLES:
        return None
    return (
        f"The {effective_role} role cannot honor a per-run internal LLM model override; "
        f"supported roles: {', '.join(sorted(LLM_MODEL_OVERRIDE_ROLES))}"
    )


# Status queries


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
        progress = await get_step_progress(
            current_step, role=agent.role, pr_number=db.get("pr_number"), project_dir=agent.project_dir
        )
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
                "cost_usd": db.get("cost_usd", format_cost_usd(agent.last_result_cost)),
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
                "cost_usd": format_cost_usd(ca.cost),
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
            progress = await get_step_progress(
                run.current_step, role=run.role, pr_number=run.pr_number, project_dir=pa.project_dir
            )
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
                    "cost_usd": format_cost_usd(run.total_cost_usd),
                    "output_lines": 0,
                    "source": "external",
                    "pr_number": run.pr_number,
                }
            )
    except (OSError, RuntimeError, SQLAlchemyError):
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
                    TaskRun.status.in_({"interrupted", "failed", "paused", "stopped"}),
                )
                .order_by(TaskRun.id.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            pr = result.scalar_one_or_none()
            if pr is not None:
                log.info("start_agent.recovered_pr_number", issue=issue, pr_number=pr)
            return pr
    except (OSError, RuntimeError, SQLAlchemyError):
        log.debug("start_agent.recover_pr_number_failed", issue=issue, exc_info=True)
        return None


def _resolve_config_model(project_dir: Path) -> str | None:
    """Return the agent model alias from SOVA config, or None if unset."""
    try:
        from sova.config.loader import load_config

        cfg = load_config(project_dir)
        return cfg.agent.model or None
    except Exception:  # noqa: BLE001 (model resolution falls back to the runtime default)
        log.debug("resolve_config_model.failed", exc_info=True)
        return None


def _resolve_config_fallback_model(project_dir: Path) -> str | None:
    """Return the first fallback model from SOVA config, or None if empty."""
    try:
        from sova.config.loader import load_config

        cfg = load_config(project_dir)
        return cfg.agent.fallback_models[0] if cfg.agent.fallback_models else None
    except Exception:  # noqa: BLE001 (model resolution falls back to the runtime default)
        log.debug("resolve_config_fallback_model.failed", exc_info=True)
        return None


def _resolve_repo_context_agent_enabled(project_dir: Path) -> bool:
    """Return whether the reviewer's optional read-only Codex repo-context sub-call is enabled.

    Gates re-admitting ``CODEX_API_KEY`` into the reviewer's spawn: the feature
    defaults off, and the key must not be exported into the reviewer's `sova
    run` process (and everything it spawns) when nothing will ever read it.
    """
    try:
        from sova.config.loader import load_config

        return load_config(project_dir).review.repo_context_agent
    except Exception:  # noqa: BLE001 (config load failure defaults the feature off)
        log.debug("resolve_repo_context_agent_enabled.failed", exc_info=True)
        return False


async def _record_budget_override(issue: str, run_id: int, budget_error: dict, project_dir: Path) -> None:
    """Persist a BudgetOverride record and emit a feed event."""
    from decimal import Decimal

    from sova.db.models import BudgetOverride
    from sova.db.session import get_session

    spend = budget_error.get("total_cost_usd", "0")
    limit = budget_error.get("max_issue_budget", "0")
    try:
        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                override = BudgetOverride(
                    issue_number=issue,
                    task_run_id=run_id,
                    spend_usd=Decimal(str(spend)),
                    limit_usd=Decimal(str(limit)),
                )
                session.add(override)
        from sova.dashboard.services.feed_service import FeedEventSeverity

        emit_safe(
            f"Budget override: issue #{issue} (${spend} / ${limit})",
            severity=FeedEventSeverity.warning,
            category="budget",
            metadata={"issue": issue, "run_id": run_id, "spend_usd": spend, "limit_usd": limit},
        )
        log.info("agent.budget_override_recorded", issue=issue, run_id=run_id, spend=spend, limit=limit)
    except (OSError, RuntimeError, SQLAlchemyError):
        log.warning("agent.budget_override_record_failed", issue=issue, run_id=run_id, exc_info=True)


async def start_agent(
    issue: str,
    *,
    role: str | None = None,
    force: bool = False,
    budget_override: bool = False,
    slug: str | None = None,
    resume_run_id: int | None = None,
    pr_number: int | None = None,
    model: str | None = None,
    llm_model_override: str | None = None,
    _skip_handoff_clear: bool = False,
) -> dict:
    """Start an agent process for the given issue.

    *llm_model_override* is a per-run "Internal LLM model" choice. It is
    passed only into this run's spawned child environment and is never
    written to config or the DB. Callers validate the model ID against
    discovery first (see the ``/agents/start`` router); the role check is
    repeated here so no caller can launch a run whose override is ignored.
    """
    from sova.dashboard.services.agent_db import _capture_pr_head_sha
    from sova.dashboard.services.agent_output import _read_output, _read_stderr

    if llm_model_override:
        role_error = llm_model_override_role_error(role)
        if role_error:
            return {"error": role_error, "code": "llm_model_override_unsupported"}

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

        net_block, _ = check_network_connectivity(pa.project_dir)
        if net_block:
            log.warning("start_agent.network_unreachable", issue=issue, role=role)
            return net_block

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

        _budget_override_pending: dict | None = None
        if issue:
            budget_error = await _check_issue_budget(issue, project_dir)
            if budget_error:
                if not budget_override:
                    return budget_error
                _budget_override_pending = budget_error
        else:
            log.info(
                "agent.issueless_budget_skip",
                role=role,
                detail="Per-issue budget N/A; per-run budget still applies",
            )

        run_id = await _create_task_run(issue or None, role or "developer", project_dir, pr_number=pr_number)
        if run_id is None:
            return {"error": "Failed to create task run record"}

        if _budget_override_pending:
            await _record_budget_override(issue, run_id, _budget_override_pending, project_dir)

        if not _skip_handoff_clear:
            try:
                from sova.dashboard.services import handoff_service

                if issue:
                    handoff_service.clear_handoff(project_dir, issue=issue)
                else:
                    handoff_service.clear_handoff(project_dir, issue=role or "run")
            except Exception:  # noqa: BLE001 (non-fatal: handoff cleanup must not block agent spawn)
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
        if budget_override:
            cmd_parts.append("--budget-override")
        if pr_number:
            cmd_parts.extend(["--pr", str(pr_number)])

        effective_role = role or "developer"
        gh_env = await _resolve_project_gh_env(project_dir)

        from sova.dashboard.services.agent_context import merge_mcp_env

        gh_env = merge_mcp_env(gh_env, run_id, project_dir)
        output_dir = project_dir / ".claude" / "agent-output"

        stream_parser = None
        try:
            output_dir.mkdir(parents=True, exist_ok=True)

            model_override_warning = None
            if effective_role in _DIRECT_SPAWN_ROLES:
                if model and not _is_runtime_model_pin(effective_role, model):
                    log.warning("agent.model_override_ignored", role=effective_role, model=model)
                    model_override_warning = (
                        f"model override {model!r} ignored: {effective_role} runs resolve their model from config"
                    )
                log.info(
                    "agent.spawn_direct",
                    run_id=run_id,
                    role=effective_role,
                    cwd=str(cwd),
                    cmd=cmd_parts[:4],
                )
                # Built fresh per call: this run's override must never be
                # visible to a concurrently started run's environment.
                direct_extra_env: dict[str, str] = {}
                if effective_role == "reviewer" and _resolve_repo_context_agent_enabled(project_dir):
                    direct_extra_env.update(_codex_extra_env(gh_env) or {})
                if _is_runtime_model_pin(effective_role, model):
                    direct_extra_env[RUN_RUNTIME_MODEL_OVERRIDE_ENV] = model
                    log.info("agent.runtime_model_override", run_id=run_id, role=effective_role, model=model)
                if llm_model_override:
                    direct_extra_env[RUN_MODEL_OVERRIDE_ENV] = llm_model_override
                    log.info(
                        "agent.llm_model_override",
                        run_id=run_id,
                        role=effective_role,
                        model=llm_model_override,
                    )
                process = await spawn_direct(
                    cmd_parts,
                    cwd,
                    env=gh_env,
                    output_dir=output_dir,
                    run_label=str(run_id),
                    extra_env=direct_extra_env or None,
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
                    "command, not a task description. Do not implement the work "
                    "yourself. Execute it exactly as written and let it complete:\n\n"
                    f"```bash\n{cmd}\n```"
                )
                if not model:
                    model = _resolve_config_model(project_dir)
                fallback_model = _resolve_config_fallback_model(project_dir)
                runtime = get_runtime()
                process = await runtime.spawn(
                    prompt,
                    cwd,
                    env=gh_env,
                    model=model,
                    fallback_model=fallback_model,
                    output_dir=output_dir,
                    run_label=str(run_id),
                )
        except Exception as exc:  # noqa: BLE001 (any spawn failure must finalize the run rather than leave it orphaned)
            log.error("agent.spawn_failed", run_id=run_id, error=str(exc), exc_info=True)
            await _finalize_orphaned_run(run_id, project_dir)
            return {"error": f"Failed to spawn agent process: {exc}"}

        stream_reader_drain_timeout = None
        if effective_role not in _DIRECT_SPAWN_ROLES:
            try:
                stream_parser = runtime.create_stream_parser()
            except Exception:  # noqa: BLE001 (parser creation failure must not orphan the already-running process)
                log.warning("agent.create_stream_parser_failed", run_id=run_id, exc_info=True)
                stream_parser = None
            if stream_parser is not None:
                try:
                    stream_reader_drain_timeout = runtime.stream_reader_drain_timeout()
                except Exception:  # noqa: BLE001 (timeout lookup failure must not discard an already-built parser)
                    log.warning("agent.stream_reader_drain_timeout_failed", run_id=run_id, exc_info=True)
                    stream_reader_drain_timeout = None

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
            worktree_path=cwd,
            stream_parser=stream_parser,
            stream_reader_drain_timeout=stream_reader_drain_timeout,
            runtime_name=runtime.name if effective_role not in _DIRECT_SPAWN_ROLES else None,
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

    result = {"status": "started", "pid": pid, "run_id": run_id}
    if model_override_warning:
        result["warnings"] = [model_override_warning]
    return result


async def stop_agent(
    slug: str | None = None,
    *,
    run_id: int | None = None,
    cause: str = "manual_stop",
    requester: str = "dashboard",
) -> dict:
    """Stop a running agent process.

    ``cause``/``requester`` are recorded on the process wrapper's
    ``TerminationRecord`` (see ``sova.ipc.control``) before the signal is
    sent, so ``_finalize_task_run()`` can later tell a deliberate stop
    (status "stopped") apart from an external kill. Defaults describe a
    human-initiated dashboard stop; callers like the watchdog pass their own
    cause (e.g. the anomaly signal name) and requester ("watchdog").
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
        await agent.process.stop(cause=cause, requester=requester)

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
    from sova.dashboard.services.agent_db import _capture_pr_head_sha, _capture_worktree_head_sha
    from sova.dashboard.services.agent_output import _read_output, _read_stderr

    pa = _get_project_agents(slug)
    safe_args = args or {}
    pr_number, issue = await _resolve_command_context(safe_args, command, pa.project_dir)

    mem_block, mem_warn = check_memory_pressure(pa.project_dir)
    if mem_block:
        return mem_block
    if mem_warn:
        log.warning("start_command.memory_pressure_warning", message=mem_warn)

    net_block, _ = check_network_connectivity(pa.project_dir)
    if net_block:
        log.warning("start_command.network_unreachable", command=command)
        return net_block

    # Renders the canonical templates and reads the install, so keep it off the event loop.
    stale_block = await asyncio.to_thread(check_stale_command, pa.project_dir, command)
    if stale_block:
        log.warning("start_command.stale_command", command=command, reason=stale_block["error"])
        return stale_block

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

        # The slash command resolves from the worktree, whose branch may track an
        # older copy than the primary checkout just verified.
        if cwd.resolve() != project_dir.resolve():
            branch_block = await asyncio.to_thread(check_stale_command, project_dir, command, cwd)
            if branch_block:
                log.warning("start_command.stale_branch_command", command=command, reason=branch_block["error"])
                return branch_block

        pre_run_head_sha = None
        if pre_run_sha is None:
            pre_run_head_sha = await _capture_worktree_head_sha(cwd)

        prompt = _resolve_command_prompt(command, args, project_dir)

        try:
            from sova.dashboard.services import handoff_service

            handoff_service.clear_handoff(project_dir, issue=issue)
        except Exception:  # noqa: BLE001 (non-fatal: handoff cleanup must not block command spawn)
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

        stream_parser = None
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            runtime = get_runtime()
            process = await runtime.spawn(
                prompt,
                cwd,
                env=gh_env,
                model=model,
                fallback_model=fallback_model,
                output_dir=output_dir,
                run_label=str(pre_run_id),
            )
        except Exception as exc:  # noqa: BLE001 (any spawn failure must finalize the run rather than leave it orphaned)
            log.error("command.spawn_failed", command=command, issue=issue, error=str(exc), exc_info=True)
            await _finalize_orphaned_run(pre_run_id, project_dir)
            return {"error": f"Failed to spawn runtime: {exc}"}

        stream_reader_drain_timeout = None
        try:
            stream_parser = runtime.create_stream_parser()
        except Exception:  # noqa: BLE001 (parser creation failure must not orphan the already-running process)
            log.warning("command.create_stream_parser_failed", command=command, run_id=pre_run_id, exc_info=True)
            stream_parser = None
        if stream_parser is not None:
            try:
                stream_reader_drain_timeout = runtime.stream_reader_drain_timeout()
            except Exception:  # noqa: BLE001 (timeout lookup failure must not discard an already-built parser)
                log.warning(
                    "command.stream_reader_drain_timeout_failed", command=command, run_id=pre_run_id, exc_info=True
                )
                stream_reader_drain_timeout = None

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
            pre_run_head_sha=pre_run_head_sha,
            prompt=prompt,
            project_dir=project_dir,
            worktree_path=cwd,
            stream_parser=stream_parser,
            stream_reader_drain_timeout=stream_reader_drain_timeout,
            runtime_name=runtime.name,
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
