"""Work service -- unified view of active tasks and run history.

Merges task_service + run_service into a single coherent service
for the Work page (Active / History / Failed tabs).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from sova.core.state import STEP_DONE_STATUSES, TASK_RUN_TERMINAL
from sova.dashboard.services.agent_progress import (
    _ADDRESS_REVIEW_ONLY,
    _RESEARCHER_ONLY,
    ADDRESS_REVIEW_PIPELINE,
    DEVELOPER_PIPELINE,
    RESEARCHER_PIPELINE,
    get_step_progress,
)
from sova.db.models import StepExecution, TaskRun
from sova.utils.formatting import decimal_to_json, iso_utc

_TERMINAL = TASK_RUN_TERMINAL

_PIPELINE_LENGTHS: dict[str, int] = {
    "developer": len(DEVELOPER_PIPELINE),
    "address_review": len(ADDRESS_REVIEW_PIPELINE),
    "researcher": len(RESEARCHER_PIPELINE),
    "command": 1,
}

_PIPELINE_ROLES = frozenset({"developer", "researcher"})


def _init_step_positions() -> dict[tuple[str, str], tuple[str, int]]:
    """Map (step_name, variant) tuples to (pipeline_name, position).

    Steps shared across pipelines (e.g., 'commit' in developer and
    address_review) get separate entries keyed by variant so they
    appear as distinct kanban columns.
    """
    positions: dict[tuple[str, str], tuple[str, int]] = {}
    offset = 0
    for pipeline_name, steps in [
        ("developer", DEVELOPER_PIPELINE),
        ("address_review", ADDRESS_REVIEW_PIPELINE),
        ("researcher", RESEARCHER_PIPELINE),
    ]:
        for i, step in enumerate(steps):
            positions[(step, pipeline_name)] = (pipeline_name, offset + i)
        offset += len(steps)
    return positions


# Unified step ordering across all pipelines (computed once at import).
# "pending" is a synthetic column for runs with no step yet (None/"agent").
_STEP_POSITIONS: dict[tuple[str, str], tuple[str, int]] = {
    **_init_step_positions(),
    ("pending", "pending"): ("pending", -1),
}


def _build_run_summary(r: TaskRun, now: datetime) -> dict[str, Any]:
    """Build a summary dict for an active (non-terminal) TaskRun."""
    started = r.started_at or now
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    elapsed = now - started
    progress = get_step_progress(r.current_step, role=r.role, pr_number=r.pr_number)
    return {
        "id": r.id,
        "issue_number": r.issue_number,
        "role": r.role,
        "status": r.status,
        "current_step": r.current_step,
        "pipeline_variant": progress.get("pipeline_variant", "developer"),
        "step_index": progress.get("step_index", 0),
        "total_steps": progress.get("total_steps", 0),
        "started_at": iso_utc(started),
        "elapsed_seconds": int(elapsed.total_seconds()),
        "elapsed_formatted": _format_duration(elapsed),
        "total_cost_usd": decimal_to_json(r.total_cost_usd),
        "run_label": r.run_label or "",
    }


async def get_active_work(session: AsyncSession) -> list[dict]:
    """Get non-terminal task runs with step progress info."""
    stmt = select(TaskRun).where(TaskRun.status.notin_(_TERMINAL)).order_by(TaskRun.started_at.desc())
    result = await session.execute(stmt)
    runs = result.scalars().all()

    now = datetime.now(timezone.utc)
    items = []
    for r in runs:
        d = _build_run_summary(r, now)
        d["branch_name"] = r.branch_name
        d["pr_number"] = r.pr_number
        items.append(d)
    return items


async def get_work_history(
    session: AsyncSession,
    *,
    status: str | None = None,
    role: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Get completed/failed/interrupted task runs with step counts.

    Returns ``{"tasks": [...], "total": N}`` for pagination support.
    """
    base = select(TaskRun).where(TaskRun.status.in_(_TERMINAL))

    if status:
        base = base.where(TaskRun.status == status)
    if role:
        base = base.where(TaskRun.role == role)

    total = await session.scalar(select(func.count()).select_from(base.subquery()))

    stmt = base.order_by(TaskRun.ended_at.desc()).limit(min(limit, 200)).offset(max(offset, 0))
    result = await session.execute(stmt)
    runs = result.scalars().all()

    run_ids = [r.id for r in runs]
    steps_by_run = await _batch_step_names(session, run_ids)

    items = []
    for r in runs:
        step_count = await session.scalar(select(func.count(StepExecution.id)).where(StepExecution.task_run_id == r.id))
        completed_steps = await session.scalar(
            select(func.count(StepExecution.id)).where(
                StepExecution.task_run_id == r.id,
                StepExecution.status.in_(STEP_DONE_STATUSES),
            )
        )

        duration_ms = _calculate_duration_ms(r.started_at, r.ended_at)

        step_names = steps_by_run.get(r.id, set())
        if step_names & _RESEARCHER_ONLY:
            variant = "researcher"
        elif step_names & _ADDRESS_REVIEW_ONLY:
            variant = "address_review"
        else:
            variant = _detect_variant(r.current_step, role=r.role, pr_number=r.pr_number)

        items.append(
            {
                "id": r.id,
                "issue_number": r.issue_number,
                "role": r.role,
                "status": r.status,
                "current_step": r.current_step,
                "pipeline_variant": variant,
                "steps_completed": completed_steps or 0,
                "steps_total": step_count or 0,
                "total_steps_possible": (_PIPELINE_LENGTHS.get(variant) if r.role in _PIPELINE_ROLES else None),
                "branch_name": r.branch_name,
                "pr_number": r.pr_number,
                "total_cost_usd": decimal_to_json(r.total_cost_usd),
                "duration_ms": duration_ms,
                "duration_formatted": _format_duration_ms(duration_ms) if duration_ms else None,
                "error_message": r.error_message,
                "started_at": iso_utc(r.started_at),
                "ended_at": iso_utc(r.ended_at),
            }
        )
    return {"tasks": items, "total": total or 0}


async def get_work_detail(session: AsyncSession, run_id: int) -> dict | None:
    """Get a single run with its step executions and pipeline progress."""
    run = await session.get(TaskRun, run_id)
    if run is None:
        return None

    all_steps = await _get_all_steps(session, run_id)
    deduped = _dedupe_steps(all_steps)

    variant_from_steps = _detect_variant_from_steps(deduped, run.current_step, role=run.role)
    progress = get_step_progress(run.current_step, role=run.role, pr_number=run.pr_number)
    is_specific = variant_from_steps in ("address_review", "researcher", "command")
    variant = variant_from_steps if is_specific else progress["pipeline_variant"]
    progress["pipeline_variant"] = variant

    run_dict = _run_to_dict(run)
    run_dict["pipeline_variant"] = variant

    return {
        "run": run_dict,
        "steps": [_step_to_dict(s) for s in deduped],
        "pipeline": progress,
    }


async def get_work_summary(session: AsyncSession) -> dict:
    """Aggregate counts for overview cards."""
    total = await session.scalar(select(func.count(TaskRun.id))) or 0
    done = await session.scalar(select(func.count(TaskRun.id)).where(TaskRun.status == "done")) or 0
    failed = await session.scalar(select(func.count(TaskRun.id)).where(TaskRun.status == "failed")) or 0
    active_groups = await get_active_work_grouped(session)
    active = len(active_groups)

    return {"total": total, "done": done, "failed": failed, "active": active}


async def get_active_work_grouped(session: AsyncSession) -> list[dict]:
    """Get non-terminal runs grouped by issue, with latest run first.

    Excludes issues whose most recent run (by ID) is already terminal --
    older "paused" runs are superseded once a later run completes the work.

    Returns a list of issue groups:
      [{"issue_number": "42", "latest_run": {...}, "previous_runs": [...], "run_count": 3}]
    """
    items = await get_active_work(session)
    if not items:
        return []

    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(item["issue_number"], []).append(item)

    superseded = await _find_superseded_issues(session, list(groups.keys()))

    result = []
    for issue, runs in groups.items():
        if issue in superseded:
            continue
        runs.sort(key=lambda r: r["started_at"] or "", reverse=True)
        result.append(
            {
                "issue_number": issue,
                "latest_run": runs[0],
                "previous_runs": runs[1:],
                "run_count": len(runs),
            }
        )

    result.sort(key=lambda g: g["latest_run"]["started_at"] or "", reverse=True)
    return result


async def _find_superseded_issues(session: AsyncSession, issue_numbers: list[str]) -> set[str]:
    """Find issues where the most recent run is already terminal.

    When an issue has older "paused" runs but the latest run completed
    successfully, those paused runs are superseded and shouldn't show as active.
    """
    if not issue_numbers:
        return set()

    stmt = (
        select(TaskRun.issue_number, func.max(TaskRun.id).label("latest_id"))
        .where(TaskRun.issue_number.in_(issue_numbers))
        .group_by(TaskRun.issue_number)
    )
    rows = (await session.execute(stmt)).all()
    latest_ids = [r.latest_id for r in rows]

    if not latest_ids:
        return set()

    stmt2 = select(TaskRun.issue_number, TaskRun.status).where(TaskRun.id.in_(latest_ids))
    rows2 = (await session.execute(stmt2)).all()
    return {r.issue_number for r in rows2 if r.status in _TERMINAL}


async def get_runs_for_issue(session: AsyncSession, issue_number: str) -> list[dict]:
    """Get all runs for a specific issue, most recent first."""
    issue_number = issue_number.lstrip("#").strip()
    stmt = select(TaskRun).where(TaskRun.issue_number == issue_number).order_by(TaskRun.started_at.desc())
    result = await session.execute(stmt)
    return [_run_to_dict(r) for r in result.scalars().all()]


async def list_runs(
    session: AsyncSession,
    *,
    limit: int = 50,
    status: str | None = None,
) -> list[dict]:
    """List task runs, most recent first. Unified replacement for run_service.list_runs."""
    stmt = select(TaskRun).options(selectinload(TaskRun.resource_summary)).order_by(TaskRun.started_at.desc())
    if status:
        stmt = stmt.where(TaskRun.status == status)
    stmt = stmt.limit(min(limit, 200))

    result = await session.execute(stmt)
    return [_run_to_dict(r) for r in result.scalars().all()]


async def get_run(session: AsyncSession, run_id: int) -> dict | None:
    """Get a single task run by ID."""
    run = await session.get(TaskRun, run_id)
    return _run_to_dict(run) if run else None


async def get_run_steps(
    session: AsyncSession,
    run_id: int,
    *,
    deduplicate: bool = True,
) -> list[dict]:
    """Get step executions for a run.

    Args:
        deduplicate: When True (default), returns only the latest attempt
            per step name. Set to False to get all records including
            failed retry attempts.
    """
    all_steps = await _get_all_steps(session, run_id)
    steps = _dedupe_steps(all_steps) if deduplicate else all_steps
    return [_step_to_dict(s) for s in steps]


async def mark_run_failed(session: AsyncSession, run_id: int, reason: str = "Manually abandoned") -> dict | None:
    """Mark a non-terminal run as failed."""
    run = await session.get(TaskRun, run_id)
    if run is None:
        return None
    if run.status in _TERMINAL:
        return {"error": f"Run is already {run.status}"}
    run.status = "failed"
    run.error_message = reason
    if not run.ended_at:
        run.ended_at = datetime.now(timezone.utc)
    await session.flush()
    return _run_to_dict(run)


def _run_to_dict(run: TaskRun) -> dict:
    result = {
        "id": run.id,
        "issue_number": run.issue_number,
        "role": run.role,
        "status": run.status,
        "current_step": run.current_step,
        "pipeline_variant": _detect_variant(run.current_step, role=run.role, pr_number=run.pr_number),
        "branch_name": run.branch_name,
        "pr_number": run.pr_number,
        "total_cost_usd": decimal_to_json(run.total_cost_usd),
        "error_message": run.error_message,
        "project_slug": run.project_slug,
        "resumed_from_id": run.resumed_from_id,
        "started_at": iso_utc(run.started_at),
        "ended_at": iso_utc(run.ended_at),
    }
    # Include resource summary if eagerly loaded
    try:
        summary = run.resource_summary
        if summary is not None:
            result["peak_cpu_percent"] = float(summary.peak_cpu_percent)
            result["peak_memory_rss_bytes"] = summary.peak_memory_rss_bytes
    except Exception:
        pass
    return result


async def _batch_step_names(session: AsyncSession, run_ids: list[int]) -> dict[int, set[str]]:
    """Batch-fetch step names for variant detection across multiple runs."""
    if not run_ids:
        return {}
    step_stmt = select(StepExecution.task_run_id, StepExecution.step_name).where(StepExecution.task_run_id.in_(run_ids))
    rows = (await session.execute(step_stmt)).all()
    result: dict[int, set[str]] = {}
    for row in rows:
        result.setdefault(row.task_run_id, set()).add(row.step_name)
    return result


def _detect_variant(current_step: str | None, *, role: str | None = None, pr_number: int | None = None) -> str:
    """Detect pipeline variant from current step name and spawn context.

    Uses role+pr_number only when current_step is None or "agent" (the
    dashboard outer-process sentinel). WorkflowEngine TaskRuns acquire
    pr_number mid-pipeline, so gating avoids false positives.
    """
    if role is not None and (role.startswith("command:") or role == "reviewer"):
        return "command"
    if role == "researcher" or (current_step is not None and current_step in _RESEARCHER_ONLY):
        return "researcher"
    if current_step in (None, "agent") and role == "developer" and pr_number is not None:
        return "address_review"
    if current_step in _ADDRESS_REVIEW_ONLY:
        return "address_review"
    return "developer"


def _detect_variant_from_steps(step_executions: list, current_step: str | None, *, role: str | None = None) -> str:
    """Detect pipeline variant from step execution history (more reliable)."""
    if role is not None and (role.startswith("command:") or role == "reviewer"):
        return "command"
    step_names = {s.step_name for s in step_executions}
    if step_names & _RESEARCHER_ONLY:
        return "researcher"
    if current_step in _RESEARCHER_ONLY:
        return "researcher"
    if step_names & _ADDRESS_REVIEW_ONLY:
        return "address_review"
    if current_step in _ADDRESS_REVIEW_ONLY:
        return "address_review"
    return "developer"


async def _get_all_steps(session: AsyncSession, run_id: int) -> list[StepExecution]:
    """Fetch all step executions for a run, ordered by start time."""
    stmt = (
        select(StepExecution)
        .where(StepExecution.task_run_id == run_id)
        .order_by(StepExecution.started_at, StepExecution.id)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


def _dedupe_steps(steps: list[StepExecution]) -> list[StepExecution]:
    """Keep only the latest (highest id) execution per step_name, preserving order."""
    latest: dict[str, StepExecution] = {}
    for s in steps:
        if s.step_name not in latest or s.id > latest[s.step_name].id:
            latest[s.step_name] = s
    return [s for s in steps if latest.get(s.step_name) is not None and s.id == latest[s.step_name].id]


def _step_to_dict(step: StepExecution) -> dict:
    return {
        "id": step.id,
        "step_name": step.step_name,
        "status": step.status,
        "cost_usd": decimal_to_json(step.cost_usd),
        "duration_ms": step.duration_ms,
        "duration_formatted": _format_duration_ms(step.duration_ms) if step.duration_ms else None,
        "output_summary": step.output_summary,
        "gate_check_result": step.gate_check_result,
        "error_message": step.error_message,
        "retry_count": step.retry_count,
        "started_at": iso_utc(step.started_at),
        "ended_at": iso_utc(step.ended_at),
    }


# Positions for all role-based kanban columns. "backlog" and "done" are included
# for sort ordering but won't appear in practice: the DB query filters to
# non-terminal runs, so no run reaches "done", and no active run maps to "backlog".
_ROLE_COLUMN_POSITIONS: dict[str, int] = {
    "backlog": 0,
    "triaged": 1,
    "researched": 2,
    "developing": 3,
    "in_review": 4,
    "done": 5,
}


def _classify_run_role_based(run: TaskRun, variant: str) -> str:
    """Classify a TaskRun into a role-based kanban column."""
    role = run.role or ""

    if role == "researcher":
        return "researched"
    if role.startswith("command:review") or role == "reviewer":
        return "in_review"
    if variant == "address_review":
        return "in_review"
    if role == "triage":
        return "triaged"
    if role.startswith(("command:integrate", "command:approve")):
        return "in_review"
    if role in ("developer", "") or role.startswith("command:"):
        return "developing"
    return "developing"


async def _fetch_active_runs_with_variants(
    session: AsyncSession,
) -> list[tuple[TaskRun, dict, str]]:
    """Fetch non-terminal runs with summaries and resolved variants.

    Returns a list of (run, summary_dict, variant) tuples.
    """
    stmt = select(TaskRun).where(TaskRun.status.notin_(_TERMINAL)).order_by(TaskRun.started_at.desc())
    result = await session.execute(stmt)
    runs = result.scalars().all()

    if not runs:
        return []

    now = datetime.now(timezone.utc)
    run_ids = [r.id for r in runs]
    steps_by_run = await _batch_step_names(session, run_ids)

    items: list[tuple[TaskRun, dict, str]] = []
    for r in runs:
        summary = _build_run_summary(r, now)
        step_names = steps_by_run.get(r.id, set())
        if step_names & _RESEARCHER_ONLY:
            variant = "researcher"
        elif step_names & _ADDRESS_REVIEW_ONLY:
            variant = "address_review"
        else:
            variant = summary["pipeline_variant"]
        summary["pipeline_variant"] = variant
        items.append((r, summary, variant))
    return items


async def get_kanban_columns(
    session: AsyncSession,
    *,
    per_column: int = 10,
    mode: Literal["step_based", "role_based"] = "step_based",
) -> list[dict]:
    """Get non-terminal TaskRuns grouped into Kanban columns.

    Args:
        per_column: Max runs per column in the response.
        mode: Grouping mode -- "step_based" (by pipeline step) or
              "role_based" (by role/status category).

    Each column represents a pipeline step or role category with runs
    currently at that step. Columns are ordered by position. Empty
    columns are omitted.
    """
    items = await _fetch_active_runs_with_variants(session)
    if not items:
        return []

    if mode == "role_based":
        return _group_role_based(items, per_column)
    return _group_step_based(items, per_column)


def _group_step_based(items: list[tuple[TaskRun, dict, str]], per_column: int) -> list[dict]:
    """Group runs by (step, variant) for step-based kanban columns."""
    columns: dict[tuple[str, str], list[dict]] = {}
    for r, summary, variant in items:
        step = r.current_step
        if step is None or step == "agent":
            step = "pending"
            col_variant = "pending"
        else:
            col_variant = variant

        columns.setdefault((step, col_variant), []).append(summary)

    result_columns = []
    for (col_step, col_variant), col_runs in columns.items():
        pipeline_name, position = _STEP_POSITIONS.get((col_step, col_variant), ("unknown", 999))
        limited_runs = col_runs[:per_column]
        result_columns.append(
            {
                "name": col_step,
                "pipeline": pipeline_name,
                "position": position,
                "count": len(col_runs),
                "runs": limited_runs,
            }
        )

    result_columns.sort(key=lambda c: c["position"])
    return result_columns


def _group_role_based(items: list[tuple[TaskRun, dict, str]], per_column: int) -> list[dict]:
    """Group runs by role category for role-based kanban columns."""
    columns: dict[str, list[dict]] = {}
    for r, summary, variant in items:
        col_name = _classify_run_role_based(r, variant)
        columns.setdefault(col_name, []).append(summary)

    result_columns = []
    for col_name, col_runs in columns.items():
        position = _ROLE_COLUMN_POSITIONS.get(col_name, 999)
        limited_runs = col_runs[:per_column]
        result_columns.append(
            {
                "name": col_name,
                "pipeline": "role_based",
                "position": position,
                "count": len(col_runs),
                "runs": limited_runs,
            }
        )

    result_columns.sort(key=lambda c: c["position"])
    return result_columns


async def get_recent_failed_runs(
    session: AsyncSession,
    *,
    hours: int = 24,
    limit: int = 10,
) -> list[dict]:
    """Get recently failed runs for the kanban 'Recently Failed' column."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    # Use COALESCE so crashed runs with NULL ended_at fall back to started_at
    effective_time = func.coalesce(TaskRun.ended_at, TaskRun.started_at)
    stmt = (
        select(TaskRun)
        .where(TaskRun.status == "failed", effective_time >= cutoff)
        .order_by(effective_time.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    runs = result.scalars().all()

    items = []
    for r in runs:
        duration_ms = _calculate_duration_ms(r.started_at, r.ended_at)

        items.append(
            {
                "id": r.id,
                "issue_number": r.issue_number,
                "role": r.role,
                "status": r.status,
                "pipeline_variant": _detect_variant(r.current_step, role=r.role, pr_number=r.pr_number),
                "run_label": r.run_label or "",
                "total_cost_usd": decimal_to_json(r.total_cost_usd),
                "error_message": r.error_message,
                "pr_number": r.pr_number,
                "duration_ms": duration_ms,
                "duration_formatted": _format_duration_ms(duration_ms) if duration_ms else None,
                "ended_at": iso_utc(r.ended_at),
            }
        )
    return items


def _calculate_duration_ms(started_at: datetime | None, ended_at: datetime | None) -> int | None:
    """Calculate duration in milliseconds between two datetimes, normalizing naive datetimes to UTC."""
    if not started_at or not ended_at:
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    if ended_at.tzinfo is None:
        ended_at = ended_at.replace(tzinfo=timezone.utc)
    return int((ended_at - started_at).total_seconds() * 1000)


def _format_duration(td) -> str:
    """Format a timedelta as human-readable string."""
    total = int(td.total_seconds())
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60}s"
    hours = total // 3600
    mins = (total % 3600) // 60
    return f"{hours}h {mins}m"


def _format_duration_ms(ms: int | None) -> str | None:
    """Format milliseconds as human-readable string."""
    if ms is None:
        return None
    total = ms // 1000
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60}s"
    hours = total // 3600
    mins = (total % 3600) // 60
    return f"{hours}h {mins}m"
