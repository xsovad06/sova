"""Agent validation -- conflict checks, budget limits, PR merge status, state transitions.

Separated from agent_lifecycle to isolate validation and guard logic.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sova.dashboard.services.agent_pool import ProjectAgents
from sova.utils.formatting import decimal_to_json
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.control.validation")


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


async def _check_pr_merged_on_failure(pr_number: int | None, project_dir: Path | None) -> bool:
    """Check if a PR was merged on GitHub despite the agent process failing.

    Used by _wait_and_finalize to avoid marking integration runs as "failed"
    when the merge succeeded but post-merge cleanup crashed.
    """
    if pr_number is None:
        return False
    try:
        from sova.config.loader import load_config
        from sova.git.pr import get_pr_status

        cfg = load_config(project_dir)
        repo = cfg.github_repo
        if not repo:
            return False
        status = await get_pr_status(pr_number, repo=repo, github_user=cfg.github_user)
        return status.state == "MERGED"
    except Exception:
        log.debug("check_pr_merged.failed", pr_number=pr_number, exc_info=True)
        return False


async def _check_issue_budget(issue: str, project_dir: Path) -> dict | None:
    """Check if the issue has exceeded its cumulative budget across all runs.

    Returns an error dict if over budget, None if clear.

    Fail-open: if the check itself fails (DB error, config error, etc.),
    returns None (clear to proceed) so budget infrastructure issues don't
    block all agent operations. The failure is logged as a warning.
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

            current = lifecycle.total_cost_usd
            if current >= max_budget:
                return {
                    "error": (
                        f"Issue #{issue} has exceeded the per-issue budget "
                        f"(${lifecycle.total_cost_usd:.2f} / ${max_budget:.2f}). "
                        f"Use --force to bypass."
                    ),
                    "total_cost_usd": decimal_to_json(current),
                    "max_issue_budget": decimal_to_json(max_budget),
                }
    except Exception:
        log.warning("issue_budget_check.failed", issue=issue, exc_info=True)

    return None


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
