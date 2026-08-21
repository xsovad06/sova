"""Failure analysis service for accurate failure rate calculation."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sova.db.models import StepExecution, TaskRun


@dataclass
class FailureBreakdown:
    """Breakdown of failures by category."""

    total_runs: int
    done_runs: int
    failed_runs: int
    interrupted_runs: int
    rejected_runs: int
    operational_failures: int
    true_pipeline_failures: int
    pipeline_failure_rate: float
    top_step_failures: list[tuple[str, int]]
    top_error_patterns: list[tuple[str, int]]


_OPERATIONAL_PATTERNS = (
    "Dismissed%",
    "Stale run%",
)


async def analyze_failures(
    session: AsyncSession,
    project_slug: str | None = None,
) -> FailureBreakdown:
    """
    Analyze failure rates with accurate categorization.

    Excludes operational failures (user dismissals, stale run recovery)
    from the true pipeline failure rate.
    """
    base_query = select(TaskRun)
    if project_slug:
        base_query = base_query.where(TaskRun.project_slug == project_slug)

    # Total counts
    total_runs = await session.scalar(select(func.count()).select_from(base_query.subquery()))
    done_runs = await session.scalar(
        select(func.count()).select_from(base_query.where(TaskRun.status == "done").subquery())
    )
    failed_runs = await session.scalar(
        select(func.count()).select_from(base_query.where(TaskRun.status == "failed").subquery())
    )
    interrupted_runs = await session.scalar(
        select(func.count()).select_from(base_query.where(TaskRun.status == "interrupted").subquery())
    )
    rejected_runs = await session.scalar(
        select(func.count()).select_from(base_query.where(TaskRun.status == "rejected").subquery())
    )

    # Operational failures (dismissed, stale recovery)
    operational_query = base_query.where(
        TaskRun.status == "failed",
        or_(*[TaskRun.error_message.like(p) for p in _OPERATIONAL_PATTERNS]),
    )
    operational_failures = await session.scalar(select(func.count()).select_from(operational_query.subquery()))

    # True pipeline failures
    true_failures = failed_runs - operational_failures

    # Failure rate (excluding interrupted, rejected, operational)
    effective_denominator = total_runs - interrupted_runs - rejected_runs - operational_failures
    pipeline_failure_rate = (true_failures / effective_denominator * 100) if effective_denominator > 0 else 0.0

    # Top failing steps
    step_failures_query = (
        select(StepExecution.step_name, func.count().label("count"))
        .where(StepExecution.status.in_(["failed", "interrupted"]))
        .group_by(StepExecution.step_name)
        .order_by(func.count().desc())
        .limit(10)
    )
    if project_slug:
        step_failures_query = step_failures_query.join(TaskRun).where(TaskRun.project_slug == project_slug)

    step_failures = await session.execute(step_failures_query)
    top_step_failures = [(row.step_name, row.count) for row in step_failures]

    # Top error patterns (exclude operational)
    error_query = (
        select(TaskRun.error_message, func.count().label("count"))
        .where(
            TaskRun.status == "failed",
            TaskRun.error_message.isnot(None),
            ~or_(*[TaskRun.error_message.like(p) for p in _OPERATIONAL_PATTERNS]),
        )
        .group_by(TaskRun.error_message)
        .order_by(func.count().desc())
        .limit(10)
    )
    if project_slug:
        error_query = error_query.where(TaskRun.project_slug == project_slug)

    errors = await session.execute(error_query)
    top_error_patterns = [(row.error_message, row.count) for row in errors]

    return FailureBreakdown(
        total_runs=total_runs or 0,
        done_runs=done_runs or 0,
        failed_runs=failed_runs or 0,
        interrupted_runs=interrupted_runs or 0,
        rejected_runs=rejected_runs or 0,
        operational_failures=operational_failures or 0,
        true_pipeline_failures=true_failures,
        pipeline_failure_rate=pipeline_failure_rate,
        top_step_failures=top_step_failures,
        top_error_patterns=top_error_patterns,
    )


async def get_failure_category_counts(
    session: AsyncSession,
    project_slug: str | None = None,
) -> dict[str, int]:
    """
    Get counts by failure category.

    Returns:
        Dict with keys: rebase_failures, no_op_commands, pipeline_bypasses,
        non_substantive_output, spec_issues, llm_failures.
    """
    base_query = select(TaskRun).where(TaskRun.status == "failed")
    if project_slug:
        base_query = base_query.where(TaskRun.project_slug == project_slug)

    # Rebase failures
    rebase_failures = await session.scalar(
        select(func.count()).select_from(
            base_query.where(
                or_(
                    TaskRun.error_message.like("Rebase could not%"),
                    TaskRun.error_message.like("%Unresolved conflicts%"),
                )
            ).subquery()
        )
    )

    # No-op commands (completed without doing work)
    no_op_commands = await session.scalar(
        select(func.count()).select_from(base_query.where(TaskRun.error_message.like("%completed without%")).subquery())
    )

    # Pipeline bypasses
    pipeline_bypasses = await session.scalar(
        select(func.count()).select_from(base_query.where(TaskRun.error_message.like("Pipeline bypassed%")).subquery())
    )

    # Non-substantive output (only lockfiles/metadata changed, no real code)
    non_substantive_output = await session.scalar(
        select(func.count()).select_from(
            base_query.where(TaskRun.error_message.like("%no substantive code changes%")).subquery()
        )
    )

    # Spec issues (forward-looking: no current code path sets these error messages)
    spec_issues = await session.scalar(
        select(func.count()).select_from(
            base_query.where(
                or_(
                    TaskRun.error_message.like("Expected .claude/specs%"),
                    TaskRun.error_message.like("%stale spec%"),
                )
            ).subquery()
        )
    )

    # LLM failures
    llm_failures = await session.scalar(
        select(func.count()).select_from(base_query.where(TaskRun.error_message.like("Claude CLI failed%")).subquery())
    )

    return {
        "rebase_failures": rebase_failures or 0,
        "no_op_commands": no_op_commands or 0,
        "pipeline_bypasses": pipeline_bypasses or 0,
        "non_substantive_output": non_substantive_output or 0,
        "spec_issues": spec_issues or 0,
        "llm_failures": llm_failures or 0,
    }
