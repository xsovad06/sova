"""Utility functions for supervisor gates."""

from __future__ import annotations

from pathlib import Path

from sova.utils.logging import get_logger

log = get_logger(component="supervisor.gates.utils")


async def count_address_review_runs(issue: str, pr_number: int, project_dir: Path) -> int:
    """Count completed address cycles for the given PR.

    Two run shapes count, matching ``_address_cycle_completed_since()`` in
    ``agent_recovery.py``: a ``developer`` run that actually executed the
    address-review pipeline (identified by having an ``address_review``
    StepExecution; the initial developer run also acquires ``pr_number``
    mid-pipeline via ``_sync_task_run_context()`` after CreatePRStep, so
    filtering on ``pr_number`` alone would include it and trigger the
    breaker one cycle too early), and a ``command:address-pr`` run (the
    ``/address-pr`` command path, which handles external bot findings and
    re-triggers the bot's review each round, so an uncounted command cycle
    let a CodeRabbit ping-pong run unbounded).

    Runs are scoped on ``pr_number`` plus ``issue``, where a run whose
    ``issue_number`` is NULL still counts: a ``command:address-pr`` run
    started before the PR body linked its issue has no issue recorded
    (#1066) but is unmistakably a cycle on this PR, while a run recorded
    against a different issue on the same PR number stays out of scope.

    NOTE: This relies on ``StepExecution.step_name == "address_review"``
    matching the name used by ``AddressReviewStep`` in
    ``sova.core.steps.address_review``.  If that step is renamed, this
    query must be updated to match.
    """
    from sqlalchemy import and_, exists, func, or_, select

    from sova.core.state import TASK_RUN_TERMINAL
    from sova.db.models import StepExecution, TaskRun
    from sova.db.session import get_session

    pipeline_cycle = and_(
        TaskRun.role == "developer",
        exists(
            select(1).where(
                StepExecution.task_run_id == TaskRun.id,
                StepExecution.step_name == "address_review",
            )
        ),
    )
    command_cycle = TaskRun.role == "command:address-pr"

    async with await get_session(project_dir=project_dir) as session:
        stmt = (
            select(func.count(TaskRun.id))
            .select_from(TaskRun)
            .where(
                TaskRun.pr_number == pr_number,
                or_(TaskRun.issue_number == issue, TaskRun.issue_number.is_(None)),
                TaskRun.status.in_(TASK_RUN_TERMINAL),
                or_(pipeline_cycle, command_cycle),
            )
        )
        result = await session.execute(stmt)
        count = int(result.scalar_one())
        log.debug("count_address_review_runs", issue=issue, pr_number=pr_number, count=count)
        return count
