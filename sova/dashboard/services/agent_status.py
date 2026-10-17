"""Agent status aggregator: single authoritative source for agent status.

Combines TaskRun and StepExecution data to compute real-time agent status
including progress percentage, time-in-step, stuck detection, and estimated
remaining time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from sova.core.state import STEP_DONE_STATUSES
from sova.dashboard.services.agent_db import _TERMINAL_STATUSES
from sova.utils.formatting import format_cost_usd
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from sova.dashboard.services.agent_pool import AgentState
    from sova.db.models import StepExecution, TaskRun

log = get_logger(component="dashboard.agent_status")

DEFAULT_STUCK_THRESHOLD_MS = 300_000  # 5 minutes

_HISTORY_LIMIT = 10
_MIN_HISTORY_RUNS = 2


@dataclass
class AgentStatus:
    """Rich status snapshot for a single agent run.

    ``current_step``/``issue``/``pid``/``elapsed_seconds``/``cost_usd``/
    ``pr_number``/``output_lines``/``cpu_percent``/``memory_rss_bytes``/
    ``source`` make this a strict superset of a single agent dict from
    ``get_unified_agents()`` (the ``/agents/active`` REST shape): each is
    populated with the same value and the same normalization (e.g.
    ``current_step`` defaulting to ``"agent"`` rather than ``None``, and
    ``cost_usd`` always the ``format_cost_usd()`` string form) so the
    WebSocket broadcast and the REST poll never disagree on what a field
    means.
    """

    run_id: int
    status: str
    role: str
    pipeline_variant: str
    current_step: str | None
    step_index: int
    total_steps: int
    step_progress_pct: float
    time_in_step_ms: int
    time_in_step_known: bool
    is_stuck: bool
    estimated_remaining_ms: int | None
    issue: str | None
    pid: int | None
    elapsed_seconds: int
    cost_usd: str
    pr_number: int | None
    completed_steps: list[str] = field(default_factory=list)
    error_message: str | None = None
    output_lines: int = 0
    cpu_percent: float | None = None
    memory_rss_bytes: int | None = None
    source: str = "dashboard"


async def get_agent_status(
    run_id: int,
    *,
    stuck_threshold_ms: int = DEFAULT_STUCK_THRESHOLD_MS,
    project_dir: Path | None = None,
) -> AgentStatus | None:
    """Compute rich status for a single TaskRun.

    Returns None if the run_id does not exist.
    """
    try:
        from sova.dashboard.services.agent_lifecycle import get_step_progress
        from sova.db.models import StepExecution, TaskRun
        from sova.db.session import get_session

        async with await get_session(project_dir=project_dir) as session:
            task_run = await session.get(TaskRun, run_id)
            if task_run is None:
                return None

            progress = await get_step_progress(
                task_run.current_step,
                role=task_run.role,
                pr_number=task_run.pr_number,
                project_dir=project_dir,
            )

            # Fetch step executions for this run
            result = await session.execute(
                select(StepExecution).where(StepExecution.task_run_id == run_id).order_by(StepExecution.started_at)
            )
            steps = result.scalars().all()

            completed = [s.step_name for s in steps if s.status in STEP_DONE_STATUSES]
            is_terminal = task_run.status in _TERMINAL_STATUSES

            # Progress percentage
            step_progress_pct = _compute_progress_pct(
                task_run.status, progress["step_index"], progress["total_steps"], len(completed)
            )

            # Time in step
            time_in_step_ms, time_in_step_known = _compute_time_in_step(task_run, steps, is_terminal=is_terminal)

            # Stuck detection
            is_stuck = not is_terminal and time_in_step_ms > stuck_threshold_ms

            # Estimated remaining time
            estimated_remaining_ms = await _estimate_remaining_ms(
                session,
                progress["steps"],
                completed,
            )

        now = datetime.now(timezone.utc)
        live_agents = _resolve_live_pool_agents(project_dir)
        live_fields = _compute_live_fields(task_run, live_agents)

        return AgentStatus(
            run_id=run_id,
            status=task_run.status,
            role=task_run.role,
            pipeline_variant=progress["pipeline_variant"],
            current_step=task_run.current_step or "agent",
            step_index=progress["step_index"],
            total_steps=progress["total_steps"],
            step_progress_pct=step_progress_pct,
            time_in_step_ms=time_in_step_ms,
            time_in_step_known=time_in_step_known,
            is_stuck=is_stuck,
            estimated_remaining_ms=estimated_remaining_ms,
            issue=task_run.issue_number,
            pid=task_run.pid,
            elapsed_seconds=_compute_elapsed_seconds(task_run, now),
            cost_usd=format_cost_usd(task_run.total_cost_usd),
            pr_number=task_run.pr_number,
            completed_steps=completed,
            error_message=task_run.error_message,
            output_lines=live_fields["output_lines"],
            cpu_percent=live_fields["cpu_percent"],
            memory_rss_bytes=live_fields["memory_rss_bytes"],
            source=live_fields["source"],
        )
    except (OSError, RuntimeError, SQLAlchemyError):
        log.warning(f"Failed to get agent status for run {run_id}", exc_info=True)
        return None


async def get_all_agent_statuses(
    *,
    stuck_threshold_ms: int = DEFAULT_STUCK_THRESHOLD_MS,
    project_dir: Path | None = None,
) -> list[AgentStatus]:
    """Return AgentStatus for all non-terminal TaskRuns.

    Uses at most 2 DB queries (TaskRuns + StepExecutions) to avoid N+1.
    """
    from sova.dashboard.services.agent_lifecycle import get_step_progress
    from sova.db.models import StepExecution, TaskRun
    from sova.db.session import get_session

    async with await get_session(project_dir=project_dir) as session:
        # Query 1: all non-terminal TaskRuns
        run_result = await session.execute(select(TaskRun).where(TaskRun.status.notin_(_TERMINAL_STATUSES)))
        runs = run_result.scalars().all()
        if not runs:
            return []

        run_ids = [r.id for r in runs]

        # Query 2: all StepExecutions for those runs
        step_result = await session.execute(
            select(StepExecution).where(StepExecution.task_run_id.in_(run_ids)).order_by(StepExecution.started_at)
        )
        all_steps = step_result.scalars().all()

        # Group steps by run_id
        steps_by_run: dict[int, list[StepExecution]] = {}
        for s in all_steps:
            steps_by_run.setdefault(s.task_run_id, []).append(s)

        # Cache estimation per pipeline variant
        estimation_cache: dict[str, dict[str, float]] = {}

        now = datetime.now(timezone.utc)
        live_agents = _resolve_live_pool_agents(project_dir)

        statuses: list[AgentStatus] = []
        for run in runs:
            try:
                progress = await get_step_progress(
                    run.current_step, role=run.role, pr_number=run.pr_number, project_dir=project_dir
                )
                run_steps = steps_by_run.get(run.id, [])
                completed = [s.step_name for s in run_steps if s.status in STEP_DONE_STATUSES]

                step_progress_pct = _compute_progress_pct(
                    run.status, progress["step_index"], progress["total_steps"], len(completed)
                )
                time_in_step_ms, time_in_step_known = _compute_time_in_step(run, run_steps, is_terminal=False)
                is_stuck = time_in_step_ms > stuck_threshold_ms

                # Estimation: fetch averages once per variant
                variant = progress["pipeline_variant"]
                if variant not in estimation_cache:
                    estimation_cache[variant] = await _fetch_step_averages(session, progress["steps"])

                estimated_remaining_ms = _compute_estimation(
                    estimation_cache[variant],
                    progress["steps"],
                    completed,
                )

                live_fields = _compute_live_fields(run, live_agents)

                statuses.append(
                    AgentStatus(
                        run_id=run.id,
                        status=run.status,
                        role=run.role,
                        pipeline_variant=variant,
                        current_step=run.current_step or "agent",
                        step_index=progress["step_index"],
                        total_steps=progress["total_steps"],
                        step_progress_pct=step_progress_pct,
                        time_in_step_ms=time_in_step_ms,
                        time_in_step_known=time_in_step_known,
                        is_stuck=is_stuck,
                        estimated_remaining_ms=estimated_remaining_ms,
                        issue=run.issue_number,
                        pid=run.pid,
                        elapsed_seconds=_compute_elapsed_seconds(run, now),
                        cost_usd=format_cost_usd(run.total_cost_usd),
                        pr_number=run.pr_number,
                        completed_steps=completed,
                        error_message=run.error_message,
                        output_lines=live_fields["output_lines"],
                        cpu_percent=live_fields["cpu_percent"],
                        memory_rss_bytes=live_fields["memory_rss_bytes"],
                        source=live_fields["source"],
                    )
                )
            except Exception:  # noqa: BLE001 (one malformed run must not break the status listing)
                log.warning(f"Failed to compute status for run {run.id}", exc_info=True)
                continue

    return statuses


# Private helpers ---------------------------------------------------------


def _resolve_live_pool_agents(project_dir: Path | None) -> dict[int, "AgentState"]:
    """Map run_id -> in-memory AgentState for the pool matching *project_dir*.

    Matched by resolved project_dir rather than the request-scoped project
    slug/contextvar: this runs from the WS producer's background task, which
    captures project_dir explicitly at connect time precisely because the
    ambient contextvar is not reliably scoped inside a background task in
    multi-project mode. Returns {} when no pool matches (e.g. a project with
    no dashboard-spawned agent yet).
    """
    from sova.dashboard.services.agent_pool import get_default_project_dir, list_all_pools

    target = (project_dir or get_default_project_dir() or Path.cwd()).resolve()
    for pa in list_all_pools().values():
        if pa.project_dir.resolve() == target:
            return pa.agents
    return {}


def _compute_live_fields(run: TaskRun, live_agents: dict[int, "AgentState"]) -> dict:
    """Resource and source fields, mirroring get_unified_agents()'s two sources.

    A run tracked in the in-memory dashboard pool reports its latest resource
    sample and is "dashboard"-sourced; a run with a live PID the pool doesn't
    know about is a CLI/externally-spawned agent; anything else (no live
    process found at all) defaults to "dashboard" per get_unified_agents()'s
    own default, with no resource sample to report.
    """
    from sova.utils.process import is_process_alive

    state = live_agents.get(run.id)
    if state is not None:
        cpu_percent = None
        memory_rss_bytes = None
        collector = state.resource_collector
        if collector is not None and collector.samples:
            latest = collector.samples[-1]
            cpu_percent = latest.cpu_percent
            memory_rss_bytes = latest.memory_rss_bytes
        return {
            "cpu_percent": cpu_percent,
            "memory_rss_bytes": memory_rss_bytes,
            "output_lines": len(state.output_lines),
            "source": "dashboard",
        }
    if run.pid is not None and is_process_alive(run.pid):
        return {"cpu_percent": None, "memory_rss_bytes": None, "output_lines": 0, "source": "external"}
    return {"cpu_percent": None, "memory_rss_bytes": None, "output_lines": 0, "source": "dashboard"}


def _as_utc(dt: datetime) -> datetime:
    """Attach UTC tzinfo to a naive datetime (SQLite returns naive even with timezone=True)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _compute_elapsed_seconds(task_run: TaskRun, now: datetime) -> int:
    """Seconds since the run started; 0 if it has no recorded start time."""
    started = task_run.started_at
    if started is None:
        return 0
    return max(0, round((now - _as_utc(started)).total_seconds()))


def _compute_progress_pct(status: str, step_index: int, total_steps: int, completed_count: int) -> float:
    """Compute progress percentage from completed step count."""
    if status == "done":
        return 100.0
    if step_index == 0 and completed_count == 0:
        return 0.0
    if total_steps == 0:
        return 0.0
    return completed_count / total_steps * 100


def _compute_time_in_step(task_run: TaskRun, steps: list[StepExecution], *, is_terminal: bool) -> tuple[int, bool]:
    """Compute (milliseconds the run has been in its current step, whether that figure is a
    genuine in-step measurement).

    The second element is True only when derived from an in-progress StepExecution's own
    started_at. A command-role run (e.g. ``command:integrate-pr``) never creates StepExecution
    rows at all, and any run has a window before its first one exists, so the TaskRun.started_at
    fallback below duplicates the adjacent run-elapsed value rather than measuring time in a
    step; callers must not present it as a "time in current step" figure.
    """
    if is_terminal:
        return 0, False

    now = datetime.now(timezone.utc)

    # Find the latest in-progress step (not done)
    in_progress = [s for s in steps if s.status not in STEP_DONE_STATUSES]
    if in_progress:
        latest = in_progress[-1]
        if latest.started_at:
            delta = now - _as_utc(latest.started_at)
            return max(0, int(delta.total_seconds() * 1000)), True

    # Fall back to TaskRun.started_at for is_stuck's purposes, but flag it as not authoritative.
    if task_run.started_at:
        delta = now - _as_utc(task_run.started_at)
        return max(0, int(delta.total_seconds() * 1000)), False

    return 0, False


async def _estimate_remaining_ms(
    session: AsyncSession,
    pipeline_steps: list[str],
    completed_steps: list[str],
) -> int | None:
    """Estimate remaining time based on historical step durations."""
    averages = await _fetch_step_averages(session, pipeline_steps)
    return _compute_estimation(averages, pipeline_steps, completed_steps)


async def _fetch_step_averages(
    session: AsyncSession,
    pipeline_steps: list[str],
) -> dict[str, float]:
    """Fetch average duration per step name from historical completed runs.

    Returns empty dict if fewer than _MIN_HISTORY_RUNS exist.
    """
    from sova.db.models import StepExecution, TaskRun

    if not pipeline_steps:
        return {}

    # Find completed runs that have steps matching this pipeline.
    # HAVING filter excludes runs from shorter pipelines that share step names.
    min_steps = max(1, int(len(pipeline_steps) * 0.5))
    subq = (
        select(StepExecution.task_run_id)
        .join(TaskRun, TaskRun.id == StepExecution.task_run_id)
        .where(
            TaskRun.status == "done",
            StepExecution.step_name.in_(pipeline_steps),
            StepExecution.status.in_(STEP_DONE_STATUSES),
        )
        .group_by(StepExecution.task_run_id)
        .having(func.count(StepExecution.step_name.distinct()) >= min_steps)
        .order_by(TaskRun.started_at.desc())
        .limit(_HISTORY_LIMIT)
        .subquery()
    )

    # Count distinct runs in the subquery
    count_result = await session.execute(select(func.count(subq.c.task_run_id.distinct())))
    run_count = count_result.scalar() or 0
    if run_count < _MIN_HISTORY_RUNS:
        return {}

    # Average duration per step name across those runs
    avg_result = await session.execute(
        select(
            StepExecution.step_name,
            func.avg(StepExecution.duration_ms),
        )
        .where(
            StepExecution.task_run_id.in_(select(subq.c.task_run_id)),
            StepExecution.status.in_(STEP_DONE_STATUSES),
            StepExecution.step_name.in_(pipeline_steps),
            StepExecution.duration_ms > 0,
        )
        .group_by(StepExecution.step_name)
    )
    rows = avg_result.all()
    if not rows:
        return {}

    return {row[0]: float(row[1]) for row in rows}


def _compute_estimation(
    averages: dict[str, float],
    pipeline_steps: list[str],
    completed_steps: list[str],
) -> int | None:
    """Sum average durations for remaining steps. None if no history."""
    if not averages:
        return None

    # Steps that are not yet completed
    completed_set = set(completed_steps)
    remaining = [s for s in pipeline_steps if s not in completed_set]
    if not remaining:
        return 0

    # If any remaining step has no historical data, we can't estimate reliably
    if any(s not in averages for s in remaining):
        return None

    total = sum(averages[s] for s in remaining)
    return int(total)


def format_status_update(statuses: list[AgentStatus]) -> dict:
    """Format a list of AgentStatus into a WebSocket message payload."""
    from dataclasses import asdict

    return {
        "type": "status_update",
        "runs": [asdict(s) for s in statuses],
    }
