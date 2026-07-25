"""Issue lifecycle service -- tracks the full journey of an issue through phases.

Manages IssueLifecycle and LifecyclePhaseRecord models, providing CRUD,
phase transitions, and backward-compatible reconstruction from existing TaskRuns.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from cachetools import TTLCache
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from sova.core.state import PHASE_ORDER, PHASE_TRANSITIONS, LifecyclePhase, PhaseStatus
from sova.db.models import IssueLifecycle, LifecyclePhaseRecord, TaskRun
from sova.git.pr import get_pr_status
from sova.utils.formatting import iso_utc
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.lifecycle")

# TTL cache for PR merge state: (pr_number, github_repo) -> state string
_pr_state_cache: TTLCache[tuple[int, str], str] = TTLCache(maxsize=256, ttl=60)

_LIFECYCLE_TERMINAL = frozenset({"done", "abandoned"})

# Maps role strings to lifecycle phases
_ROLE_TO_PHASE: dict[str, str] = {
    "developer": "development",
    "reviewer": "review",
    "command:address-pr": "address_review",
    "command:integrate-pr": "integrate",
    "command:after-merge": "post_merge",
    "command:agent-resume": "",  # determined by context
}


# -- CRUD ---------------------------------------------------------------------


async def get_or_create_lifecycle(
    session: AsyncSession,
    issue_number: str,
    project_slug: str = "",
) -> IssueLifecycle:
    """Find an active lifecycle for the issue or create one."""
    issue_number = issue_number.lstrip("#").strip()

    stmt = (
        select(IssueLifecycle)
        .where(
            IssueLifecycle.issue_number == issue_number,
            IssueLifecycle.project_slug == project_slug,
            IssueLifecycle.current_phase.notin_(_LIFECYCLE_TERMINAL),
        )
        .order_by(IssueLifecycle.created_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    lifecycle = result.scalar_one_or_none()

    if lifecycle is not None:
        return lifecycle

    lifecycle = IssueLifecycle(
        issue_number=issue_number,
        project_slug=project_slug,
        current_phase=LifecyclePhase.DEVELOPMENT,
        phase_status=PhaseStatus.PENDING,
    )
    session.add(lifecycle)
    await session.flush()
    log.info("lifecycle.created", issue=issue_number, lifecycle_id=lifecycle.id)
    return lifecycle


async def get_lifecycle(session: AsyncSession, lifecycle_id: int) -> IssueLifecycle | None:
    """Get a lifecycle by ID with its phases eagerly loaded."""
    stmt = select(IssueLifecycle).options(selectinload(IssueLifecycle.phases)).where(IssueLifecycle.id == lifecycle_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_lifecycle_for_issue(
    session: AsyncSession,
    issue_number: str,
    project_slug: str = "",
) -> IssueLifecycle | None:
    """Find the active lifecycle for an issue."""
    issue_number = issue_number.lstrip("#").strip()
    stmt = (
        select(IssueLifecycle)
        .options(selectinload(IssueLifecycle.phases))
        .where(
            IssueLifecycle.issue_number == issue_number,
            IssueLifecycle.project_slug == project_slug,
            IssueLifecycle.current_phase.notin_(_LIFECYCLE_TERMINAL),
        )
        .order_by(IssueLifecycle.created_at.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_active_lifecycles(
    session: AsyncSession,
    project_slug: str = "",
) -> list[IssueLifecycle]:
    """List all non-terminal lifecycles for a project."""
    stmt = (
        select(IssueLifecycle)
        .options(selectinload(IssueLifecycle.phases))
        .where(
            IssueLifecycle.project_slug == project_slug,
            IssueLifecycle.current_phase.notin_(_LIFECYCLE_TERMINAL),
        )
        .order_by(IssueLifecycle.updated_at.desc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


# -- Phase queries (direct DB, avoids identity map staleness) -----------------


async def _find_phase_records(
    session: AsyncSession,
    lifecycle_id: int,
    phase: str,
    status: str | None = None,
) -> list[LifecyclePhaseRecord]:
    """Query phase records directly to avoid stale relationship data."""
    stmt = select(LifecyclePhaseRecord).where(
        LifecyclePhaseRecord.lifecycle_id == lifecycle_id,
        LifecyclePhaseRecord.phase == phase,
    )
    if status:
        stmt = stmt.where(LifecyclePhaseRecord.status == status)
    stmt = stmt.order_by(LifecyclePhaseRecord.id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _max_attempt(session: AsyncSession, lifecycle_id: int, phase: str) -> int:
    """Get the max attempt number for a phase."""
    result = await session.scalar(
        select(func.max(LifecyclePhaseRecord.attempt)).where(
            LifecyclePhaseRecord.lifecycle_id == lifecycle_id,
            LifecyclePhaseRecord.phase == phase,
        )
    )
    return result or 0


def _advance_lifecycle(lifecycle: IssueLifecycle, from_phase: str, now: datetime) -> None:
    """Advance lifecycle to the next phase based on PHASE_TRANSITIONS."""
    transitions = PHASE_TRANSITIONS.get(from_phase, set())
    if not transitions:
        lifecycle.current_phase = "done"
        lifecycle.phase_status = PhaseStatus.COMPLETED
        lifecycle.completed_at = now
    else:
        for p in PHASE_ORDER:
            if p.value in transitions:
                lifecycle.current_phase = p.value
                lifecycle.phase_status = PhaseStatus.PENDING
                break


# -- Phase transitions --------------------------------------------------------


async def start_phase(
    session: AsyncSession,
    lifecycle_id: int,
    phase: str,
    task_run_id: int | None = None,
) -> LifecyclePhaseRecord | None:
    """Start a phase within a lifecycle."""
    lifecycle = await session.get(IssueLifecycle, lifecycle_id)
    if lifecycle is None:
        return None

    now = datetime.now(timezone.utc)

    existing = await _find_phase_records(session, lifecycle_id, phase, PhaseStatus.ACTIVE)
    if existing:
        log.warning("phase.already_active", lifecycle_id=lifecycle_id, phase=phase)
        record = existing[0]
        if task_run_id and not record.task_run_id:
            record.task_run_id = task_run_id
        return record

    max_att = await _max_attempt(session, lifecycle_id, phase)

    record = LifecyclePhaseRecord(
        lifecycle_id=lifecycle_id,
        phase=phase,
        status=PhaseStatus.ACTIVE,
        task_run_id=task_run_id,
        attempt=max_att + 1,
        started_at=now,
    )
    session.add(record)

    lifecycle.current_phase = phase
    lifecycle.phase_status = PhaseStatus.ACTIVE
    lifecycle.updated_at = now

    await session.flush()
    log.info("phase.started", lifecycle_id=lifecycle_id, phase=phase, attempt=record.attempt)
    return record


async def complete_phase(
    session: AsyncSession,
    lifecycle_id: int,
    phase: str,
    cost: Decimal | float = 0,
) -> bool:
    """Mark a phase as completed and advance the lifecycle."""
    lifecycle = await session.get(IssueLifecycle, lifecycle_id)
    if lifecycle is None:
        return False

    now = datetime.now(timezone.utc)

    active = await _find_phase_records(session, lifecycle_id, phase, PhaseStatus.ACTIVE)
    if not active:
        log.warning("phase.no_active_record", lifecycle_id=lifecycle_id, phase=phase)
        return False

    record = active[0]
    record.status = PhaseStatus.COMPLETED
    record.completed_at = now
    record.cost_usd = Decimal(str(cost))

    lifecycle.total_cost_usd += Decimal(str(cost))
    lifecycle.phase_status = PhaseStatus.COMPLETED
    lifecycle.updated_at = now

    _advance_lifecycle(lifecycle, phase, now)

    await session.flush()
    log.info("phase.completed", lifecycle_id=lifecycle_id, phase=phase, next=lifecycle.current_phase)
    return True


async def fail_phase(
    session: AsyncSession,
    lifecycle_id: int,
    phase: str,
    error: str | None = None,
) -> bool:
    """Mark a phase as failed."""
    lifecycle = await session.get(IssueLifecycle, lifecycle_id)
    if lifecycle is None:
        return False

    now = datetime.now(timezone.utc)

    active = await _find_phase_records(session, lifecycle_id, phase, PhaseStatus.ACTIVE)
    if not active:
        log.warning("phase.no_active_record_to_fail", lifecycle_id=lifecycle_id, phase=phase)
        return False

    record = active[0]
    record.status = PhaseStatus.FAILED
    record.error_message = error
    record.completed_at = now

    lifecycle.phase_status = PhaseStatus.FAILED
    lifecycle.updated_at = now

    await session.flush()
    log.info("phase.failed", lifecycle_id=lifecycle_id, phase=phase, error=error)
    return True


async def skip_phase(
    session: AsyncSession,
    lifecycle_id: int,
    phase: str,
) -> bool:
    """Skip a phase and advance to the next."""
    lifecycle = await session.get(IssueLifecycle, lifecycle_id)
    if lifecycle is None:
        return False

    now = datetime.now(timezone.utc)

    # Mark any pending/active records as skipped
    mutable = await _find_phase_records(session, lifecycle_id, phase)
    has_record = False
    for p in mutable:
        has_record = True
        if p.status in (PhaseStatus.PENDING, PhaseStatus.ACTIVE):
            p.status = PhaseStatus.SKIPPED
            p.completed_at = now

    if not has_record:
        record = LifecyclePhaseRecord(
            lifecycle_id=lifecycle_id,
            phase=phase,
            status=PhaseStatus.SKIPPED,
            completed_at=now,
        )
        session.add(record)

    _advance_lifecycle(lifecycle, phase, now)
    lifecycle.updated_at = now
    await session.flush()
    log.info("phase.skipped", lifecycle_id=lifecycle_id, phase=phase, next=lifecycle.current_phase)
    return True


async def restart_phase(
    session: AsyncSession,
    lifecycle_id: int,
    phase: str,
) -> LifecyclePhaseRecord | None:
    """Restart a failed phase with a new attempt."""
    lifecycle = await session.get(IssueLifecycle, lifecycle_id)
    if lifecycle is None:
        return None

    failed = await _find_phase_records(session, lifecycle_id, phase, PhaseStatus.FAILED)
    if not failed:
        log.warning("phase.no_failed_record_to_restart", lifecycle_id=lifecycle_id, phase=phase)
        return None

    lifecycle.current_phase = phase
    lifecycle.phase_status = PhaseStatus.PENDING
    lifecycle.updated_at = datetime.now(timezone.utc)
    await session.flush()

    log.info("phase.restart_ready", lifecycle_id=lifecycle_id, phase=phase)
    return failed[-1]


async def force_advance(
    session: AsyncSession,
    lifecycle_id: int,
    to_phase: str,
) -> bool:
    """Force-advance lifecycle to any phase (skip intermediate phases)."""
    lifecycle = await session.get(IssueLifecycle, lifecycle_id)
    if lifecycle is None:
        return False

    now = datetime.now(timezone.utc)

    valid_phases = {p.value for p in LifecyclePhase} | {"done"}
    if to_phase not in valid_phases:
        return False

    current_idx = -1
    target_idx = len(PHASE_ORDER)
    for i, p in enumerate(PHASE_ORDER):
        if p.value == lifecycle.current_phase:
            current_idx = i
        if p.value == to_phase:
            target_idx = i

    # Skip intermediate phase records
    if current_idx >= 0 and target_idx > current_idx:
        for i in range(current_idx, target_idx):
            skip_p = PHASE_ORDER[i].value
            records = await _find_phase_records(session, lifecycle_id, skip_p)
            for rec in records:
                if rec.status in (PhaseStatus.PENDING, PhaseStatus.ACTIVE):
                    rec.status = PhaseStatus.SKIPPED
                    rec.completed_at = now

    if to_phase == "done":
        lifecycle.current_phase = "done"
        lifecycle.phase_status = PhaseStatus.COMPLETED
        lifecycle.completed_at = now
    else:
        lifecycle.current_phase = to_phase
        lifecycle.phase_status = PhaseStatus.PENDING

    lifecycle.updated_at = now
    await session.flush()
    log.info("lifecycle.force_advanced", lifecycle_id=lifecycle_id, to=to_phase)
    return True


async def abandon_lifecycle(
    session: AsyncSession,
    lifecycle_id: int,
) -> bool:
    """Abandon a lifecycle entirely."""
    lifecycle = await session.get(IssueLifecycle, lifecycle_id)
    if lifecycle is None:
        return False

    now = datetime.now(timezone.utc)
    lifecycle.current_phase = "abandoned"
    lifecycle.phase_status = PhaseStatus.FAILED
    lifecycle.completed_at = now
    lifecycle.updated_at = now

    # Mark all non-terminal phase records as skipped
    stmt = select(LifecyclePhaseRecord).where(
        LifecyclePhaseRecord.lifecycle_id == lifecycle_id,
        LifecyclePhaseRecord.status.in_([PhaseStatus.PENDING, PhaseStatus.ACTIVE]),
    )
    result = await session.execute(stmt)
    for p in result.scalars().all():
        p.status = PhaseStatus.SKIPPED
        p.completed_at = now

    await session.flush()
    log.info("lifecycle.abandoned", lifecycle_id=lifecycle_id)
    return True


# -- PR merge state cache -----------------------------------------------------


async def _get_pr_merge_state(pr_number: int, github_repo: str, github_user: str) -> str | None:
    """Check if a PR is merged, with 60s TTL cache. Returns state string or None on error."""
    cache_key = (pr_number, github_repo)
    cached = _pr_state_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        status = await get_pr_status(pr_number, repo=github_repo, github_user=github_user)
        _pr_state_cache[cache_key] = status.state
        return status.state
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        log.debug("pr_merge_check.failed", pr_number=pr_number, exc_info=True)
        return None


def _synthesize_merge_phases(phases: list[dict], seen_phases: set[str]) -> None:
    """Append synthetic integrate and post_merge phases for a merged PR."""
    now = datetime.now(timezone.utc)
    offsets = {"integrate": timedelta(seconds=0), "post_merge": timedelta(seconds=1)}
    for phase_name in ("integrate", "post_merge"):
        if phase_name not in seen_phases:
            ts = iso_utc(now + offsets[phase_name])
            phases.append(
                {
                    "phase": phase_name,
                    "status": "completed",
                    "task_run_id": None,
                    "cost_usd": 0,
                    "attempt": 1,
                    "error_message": None,
                    "started_at": ts,
                    "completed_at": ts,
                    "source": "github",
                }
            )
            seen_phases.add(phase_name)


# -- Backward-compatible reconstruction --------------------------------------


async def build_lifecycle_view(
    session: AsyncSession,
    issue_number: str,
    project_slug: str = "",
    github_repo: str = "",
    github_user: str = "",
) -> dict | None:
    """Reconstruct a lifecycle view from existing TaskRun records.

    For issues processed before the lifecycle feature was added,
    this infers phases from role + status of TaskRun records.
    """
    issue_number = issue_number.lstrip("#").strip()

    # First try to find a real lifecycle
    lifecycle = await get_lifecycle_for_issue(session, issue_number, project_slug)
    if lifecycle is not None:
        view = lifecycle_to_dict(lifecycle)
        pr_num = view.get("pr_number")
        existing = {p["phase"] for p in view["phases"]}
        if pr_num and github_repo and github_user and "integrate" not in existing:
            state = await _get_pr_merge_state(pr_num, github_repo, github_user)
            if state == "MERGED":
                _synthesize_merge_phases(view["phases"], existing)
                if view.get("current_phase") not in _LIFECYCLE_TERMINAL:
                    view["current_phase"] = "done"
                    view["phase_status"] = "completed"
        return view

    # Reconstruct from TaskRuns
    stmt = select(TaskRun).where(TaskRun.issue_number == issue_number).order_by(TaskRun.started_at)
    result = await session.execute(stmt)
    runs = result.scalars().all()

    if not runs:
        return None

    phases: list[dict] = []
    seen_phases: set[str] = set()
    total_cost = Decimal("0")
    pr_number = None
    branch_name = ""

    for run in runs:
        phase = _ROLE_TO_PHASE.get(run.role, "development")
        if not phase:
            phase = "development"

        if run.pr_number:
            pr_number = run.pr_number
        if run.branch_name:
            branch_name = run.branch_name

        total_cost += run.total_cost_usd or Decimal("0")

        status = _infer_phase_status(run.status)
        if phase in seen_phases:
            # Update existing phase entry with latest run info
            for p in phases:
                if p["phase"] == phase:
                    p["status"] = status
                    p["task_run_id"] = run.id
                    p["cost_usd"] = float(run.total_cost_usd or 0)
                    p["attempt"] = p.get("attempt", 0) + 1
                    if run.error_message:
                        p["error_message"] = run.error_message
                    break
        else:
            seen_phases.add(phase)
            phases.append(
                {
                    "phase": phase,
                    "status": status,
                    "task_run_id": run.id,
                    "cost_usd": float(run.total_cost_usd or 0),
                    "attempt": 1,
                    "error_message": run.error_message,
                    "started_at": iso_utc(run.started_at),
                    "completed_at": iso_utc(run.ended_at),
                }
            )

    # Determine current phase from the latest phase record
    current_phase = phases[-1]["phase"] if phases else "development"
    phase_status = phases[-1]["status"] if phases else "pending"

    # If latest run is done and it's development, infer post_pr
    latest_run = runs[-1]
    if latest_run.role == "developer" and latest_run.status == "done" and latest_run.pr_number:
        if "post_pr" not in seen_phases:
            phases.append(
                {
                    "phase": "post_pr",
                    "status": "completed",
                    "task_run_id": None,
                    "cost_usd": 0,
                    "attempt": 1,
                    "error_message": None,
                    "started_at": iso_utc(latest_run.ended_at),
                    "completed_at": iso_utc(latest_run.ended_at),
                }
            )
            current_phase = "post_pr"
            phase_status = "completed"

    # Synthesize integrate/post_merge from GitHub if PR is merged
    if pr_number and github_repo and github_user and "integrate" not in seen_phases:
        state = await _get_pr_merge_state(pr_number, github_repo, github_user)
        if state == "MERGED":
            _synthesize_merge_phases(phases, seen_phases)
            if current_phase not in _LIFECYCLE_TERMINAL:
                current_phase = "done"
                phase_status = "completed"

    return {
        "id": None,
        "issue_number": issue_number,
        "project_slug": project_slug,
        "current_phase": current_phase,
        "phase_status": phase_status,
        "pr_number": pr_number,
        "branch_name": branch_name,
        "total_cost_usd": float(total_cost),
        "phases": phases,
        "reconstructed": True,
        "created_at": iso_utc(runs[0].started_at) if runs else None,
        "updated_at": iso_utc(runs[-1].started_at) if runs else None,
        "completed_at": None,
    }


# -- Integration hooks --------------------------------------------------------


def infer_phase_from_role(role: str) -> str | None:
    """Map a role string to a lifecycle phase."""
    return _ROLE_TO_PHASE.get(role)


async def link_task_run_to_lifecycle(
    session: AsyncSession,
    run: TaskRun,
    project_slug: str = "",
) -> int | None:
    """Create/find a lifecycle for the run's issue and link them.

    Called from start_agent() after creating the TaskRun.
    Returns the lifecycle_id or None on error.
    """
    if not run.issue_number:
        return None

    phase = infer_phase_from_role(run.role)
    if not phase:
        return None

    try:
        lifecycle = await get_or_create_lifecycle(session, run.issue_number, project_slug)
        run.lifecycle_id = lifecycle.id

        # Update lifecycle metadata from the run
        if run.pr_number:
            lifecycle.pr_number = run.pr_number
        if run.branch_name:
            lifecycle.branch_name = run.branch_name

        await start_phase(session, lifecycle.id, phase, task_run_id=run.id)
        await session.flush()
        return lifecycle.id
    except Exception:
        log.warning("lifecycle.link_failed", issue=run.issue_number, exc_info=True)
        return None


async def finalize_phase_from_run(
    session: AsyncSession,
    run_id: int,
    exit_code: int,
    cost: float = 0,
) -> None:
    """Called from _finalize_task_run() to update lifecycle phase status."""
    run = await session.get(TaskRun, run_id)
    if run is None or run.lifecycle_id is None:
        return

    phase = infer_phase_from_role(run.role)
    if phase is None:
        return

    # Update lifecycle PR/branch from run
    lifecycle = await get_lifecycle(session, run.lifecycle_id)
    if lifecycle:
        if run.pr_number:
            lifecycle.pr_number = run.pr_number
        if run.branch_name:
            lifecycle.branch_name = run.branch_name

    try:
        if exit_code == 0:
            await complete_phase(session, run.lifecycle_id, phase, cost)
        else:
            await fail_phase(session, run.lifecycle_id, phase, run.error_message)
    except Exception:
        log.warning("lifecycle.finalize_failed", run_id=run_id, exc_info=True)


# -- Serialization ------------------------------------------------------------


def lifecycle_to_dict(lifecycle: IssueLifecycle) -> dict:
    """Convert a lifecycle + phases to a dict for API responses."""
    phases = []
    for p in lifecycle.phases:
        phases.append(
            {
                "id": p.id,
                "phase": p.phase,
                "status": p.status,
                "task_run_id": p.task_run_id,
                "cost_usd": float(p.cost_usd or 0),
                "attempt": p.attempt,
                "error_message": p.error_message,
                "started_at": iso_utc(p.started_at),
                "completed_at": iso_utc(p.completed_at),
            }
        )

    return {
        "id": lifecycle.id,
        "issue_number": lifecycle.issue_number,
        "project_slug": lifecycle.project_slug,
        "current_phase": lifecycle.current_phase,
        "phase_status": lifecycle.phase_status,
        "pr_number": lifecycle.pr_number,
        "branch_name": lifecycle.branch_name,
        "total_cost_usd": float(lifecycle.total_cost_usd or 0),
        "phases": phases,
        "reconstructed": False,
        "created_at": iso_utc(lifecycle.created_at),
        "updated_at": iso_utc(lifecycle.updated_at),
        "completed_at": iso_utc(lifecycle.completed_at),
    }


def _infer_phase_status(run_status: str) -> str:
    """Map a TaskRun status to a PhaseStatus for reconstruction."""
    if run_status == "done":
        return PhaseStatus.COMPLETED
    if run_status in ("failed", "rejected", "interrupted"):
        return PhaseStatus.FAILED
    return PhaseStatus.ACTIVE
