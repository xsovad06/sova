"""Utility functions for supervisor gates."""

from __future__ import annotations

from pathlib import Path


async def count_address_review_runs(issue: str, pr_number: int, project_dir: Path) -> int:
    """Count completed address-review runs for the given issue+PR.

    Only counts runs that actually executed the address-review pipeline
    (identified by having an ``address_review`` StepExecution).  The
    initial developer run also acquires ``pr_number`` mid-pipeline via
    ``_sync_task_run_context()`` after CreatePRStep, so filtering on
    ``pr_number`` alone would include it and trigger the breaker one
    cycle too early.

    NOTE: This relies on ``StepExecution.step_name == "address_review"``
    matching the name used by ``AddressReviewStep`` in
    ``sova.core.steps.address_review``.  If that step is renamed, this
    query must be updated to match.
    """
    from sqlalchemy import func, select

    from sova.core.state import TASK_RUN_TERMINAL
    from sova.db.models import StepExecution, TaskRun
    from sova.db.session import get_session

    async with await get_session(project_dir=project_dir) as session:
        stmt = (
            select(func.count(TaskRun.id.distinct()))
            .join(StepExecution, StepExecution.task_run_id == TaskRun.id)
            .where(
                StepExecution.step_name == "address_review",
                TaskRun.issue_number == issue,
                TaskRun.role == "developer",
                TaskRun.pr_number == pr_number,
                TaskRun.status.in_(TASK_RUN_TERMINAL),
            )
        )
        result = await session.execute(stmt)
        return result.scalar_one()
