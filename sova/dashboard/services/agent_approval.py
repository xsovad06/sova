"""Agent approval: spec approval/rejection, lifecycle integration.

Handles the spec approval flow (resume_from_approval, reject_spec) and
lifecycle phase linking for issue-based runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from sova.utils.logging import get_logger

log = get_logger(component="dashboard.control")


async def _claim_awaiting_approval(run_id: int, target_status: str) -> tuple[dict | None, dict | None]:
    """Validate and atomically claim an awaiting_approval TaskRun.

    Loads the run, checks it exists and has status ``awaiting_approval``,
    then performs a CAS update to ``target_status``.

    Returns ``(run_data, None)`` on success where ``run_data`` contains
    ``issue_number``, ``role``, and ``pr_number``.
    Returns ``(None, error_dict)`` on validation or CAS failure.
    """
    from sqlalchemy import update

    from sova.core.state import TaskStatus
    from sova.db.models import TaskRun
    from sova.db.session import get_session

    async with await get_session() as session, session.begin():
        task_run = await session.get(TaskRun, run_id)

    if task_run is None:
        return None, {"error": "not_found", "detail": f"TaskRun #{run_id} not found"}

    if task_run.status != TaskStatus.AWAITING_APPROVAL:
        return None, {
            "error": "conflict",
            "detail": f"Run #{run_id} has status '{task_run.status}', expected 'awaiting_approval'",
        }

    async with await get_session() as session, session.begin():
        result = await session.execute(
            update(TaskRun)
            .where(TaskRun.id == run_id, TaskRun.status == TaskStatus.AWAITING_APPROVAL)
            .values(status=target_status)
        )
        if result.rowcount == 0:
            return None, {
                "error": "conflict",
                "detail": f"Run #{run_id} was already claimed by another request",
            }

    return {
        "issue_number": task_run.issue_number,
        "role": task_run.role,
        "pr_number": task_run.pr_number,
    }, None


def _clear_handoff_for_issue(issue: str, caller: str) -> None:
    """Clear the handoff file for an issue, logging failures."""
    if not issue:
        return
    try:
        from sova.dashboard.services import handoff_service

        handoff_service.clear_handoff(issue=issue)
    except Exception:
        log.debug(f"{caller}.clear_handoff_failed", issue=issue, exc_info=True)


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

    run_data, error = await _claim_awaiting_approval(run_id, TaskStatus.PENDING)
    if error:
        return error

    issue = run_data["issue_number"] or ""
    role = run_data["role"] or "developer"
    pr_number = run_data["pr_number"]

    from sova.dashboard.services.agent_lifecycle import start_agent

    # Spawn first, clear state second: _skip_handoff_clear prevents start_agent
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
        async with await get_session() as session, session.begin():
            await session.execute(
                update(TaskRun)
                .where(TaskRun.id == run_id, TaskRun.status == TaskStatus.PENDING)
                .values(status=TaskStatus.AWAITING_APPROVAL)
            )
        return result

    _clear_handoff_for_issue(issue, "resume_from_approval")

    return {
        "run_id": result["run_id"],
        "resumed_from": run_id,
        "issue": issue,
        "role": role,
    }


async def complete_awaiting_approval_by_issue(
    issue_number: str, target_status: Literal["done", "rejected"] = "done"
) -> int | None:
    """Find and transition the most recent awaiting_approval TaskRun for an issue.

    Returns the run ID that was updated, or None if no matching run was found.
    Non-fatal: logs warnings on errors but never raises.
    """
    from datetime import datetime, timezone

    from sqlalchemy import select, update

    from sova.core.state import TaskStatus
    from sova.db.models import TaskRun
    from sova.db.session import get_session

    try:
        async with await get_session() as session, session.begin():
            stmt = (
                select(TaskRun.id)
                .where(
                    TaskRun.issue_number == issue_number.lstrip("#").strip(),
                    TaskRun.role == "researcher",
                    TaskRun.status == TaskStatus.AWAITING_APPROVAL,
                )
                .order_by(TaskRun.started_at.desc(), TaskRun.id.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            run_id = result.scalar_one_or_none()

        if run_id is None:
            return None

        async with await get_session() as session, session.begin():
            cas = await session.execute(
                update(TaskRun)
                .where(TaskRun.id == run_id, TaskRun.status == TaskStatus.AWAITING_APPROVAL)
                .values(status=target_status, ended_at=datetime.now(timezone.utc))
            )
            if cas.rowcount == 0:
                log.debug("complete_awaiting_approval.cas_failed", run_id=run_id, issue=issue_number)
                return None

        log.info("complete_awaiting_approval.done", run_id=run_id, issue=issue_number, target=target_status)
        return run_id
    except Exception:
        log.warning("complete_awaiting_approval.failed", issue=issue_number, exc_info=True)
        return None


async def reject_spec(run_id: int) -> dict:
    """Reject a spec and mark the awaiting_approval run as rejected.

    Validates that the TaskRun exists and has status ``awaiting_approval``,
    then transitions it to ``rejected``. Clears the handoff file on success.

    Returns a dict with ``run_id``, ``issue``, and ``status``.
    """
    from sova.core.state import TaskStatus

    run_data, error = await _claim_awaiting_approval(run_id, TaskStatus.REJECTED)
    if error:
        return error

    issue = run_data["issue_number"] or ""
    _clear_handoff_for_issue(issue, "reject_spec")

    return {"run_id": run_id, "issue": issue, "status": "rejected"}


# Lifecycle integration -------------------------------------------------------


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
