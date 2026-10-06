"""Utility functions for supervisor gates."""

from __future__ import annotations

from pathlib import Path

from sova.utils.logging import get_logger

log = get_logger(component="supervisor.gates.utils")

# The only statuses a network-caused failure actually lands in; a `done` run
# normally has no error_message, and the outage exclusion below must not
# apply to one just because it happens to carry a stale message.
_FAILURE_STATUSES = frozenset({"failed", "interrupted"})


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

    A run whose failure was a network outage is excluded: it consumed no
    review, changed nothing on the PR, and re-triggered no bot. Counting it
    would spend the ``pipeline.max_address_review_cycles`` budget on the
    operator's internet connection, and two outages would wedge a PR's
    address budget permanently. Note that marking such a run "interrupted"
    is not sufficient on its own to exclude it, since "interrupted" is itself
    a member of ``TASK_RUN_TERMINAL``.

    NOTE: This relies on ``StepExecution.step_name == "address_review"``
    matching the name used by ``AddressReviewStep`` in
    ``sova.core.steps.address_review``.  If that step is renamed, this
    query must be updated to match.
    """
    from sqlalchemy import and_, exists, or_, select

    from sova.core.state import TASK_RUN_TERMINAL
    from sova.db.models import StepExecution, TaskRun
    from sova.db.session import get_session
    from sova.utils.network import looks_like_network_outage

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
            select(TaskRun.id, TaskRun.error_message, TaskRun.status)
            .select_from(TaskRun)
            .where(
                TaskRun.pr_number == pr_number,
                or_(TaskRun.issue_number == issue, TaskRun.issue_number.is_(None)),
                TaskRun.status.in_(TASK_RUN_TERMINAL),
                or_(pipeline_cycle, command_cycle),
            )
        )
        rows = (await session.execute(stmt)).all()
        # Filtered in Python rather than SQL: the predicate is a substring table
        # with a corroboration rule (sova/utils/network.py), not something a
        # portable LIKE clause can express across SQLite and PostgreSQL.
        # Restricted to failure statuses: a `done` run normally carries no
        # error_message, but a downgraded-then-recovered run or a stale
        # message on an otherwise successful run must not be excluded from
        # the count, or a completed address cycle would be dropped and the
        # breaker would allow extra cycles beyond the configured budget.
        outage_runs = [
            row.id for row in rows if row.status in _FAILURE_STATUSES and looks_like_network_outage(row.error_message)
        ]
        count = len(rows) - len(outage_runs)
        log.debug(
            "count_address_review_runs",
            issue=issue,
            pr_number=pr_number,
            count=count,
            excluded_outage_runs=outage_runs,
        )
        return count
