"""Repeated failure gate: blocks researcher spawn after too many consecutive failures."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from sova.db.models import TaskRun
from sova.supervisor.gates import BlockReason
from sova.utils.logging import get_logger

log = get_logger(component="supervisor.gates.repeated_failure")


async def check_repeated_failures_gate(
    issue: int,
    max_failures: int,
    session_factory: async_sessionmaker,
    role: str = "researcher",
) -> BlockReason | None:
    """Block agent spawn after too many failures since the last success. Fail-open.

    Args:
        issue: Issue number.
        max_failures: Maximum allowed consecutive failures.
        session_factory: Database session factory.
        role: Agent role to check (default: "researcher").

    Returns:
        BlockReason if threshold exceeded, None otherwise.
    """
    if max_failures == 0:
        return None
    try:
        async with session_factory() as session:
            last_success_subq = (
                select(func.coalesce(func.max(TaskRun.id), 0))
                .where(
                    TaskRun.issue_number == str(issue),
                    TaskRun.role == role,
                    TaskRun.status == "done",
                )
                .scalar_subquery()
            )
            stmt = (
                select(func.count())
                .select_from(TaskRun)
                .where(
                    TaskRun.issue_number == str(issue),
                    TaskRun.role == role,
                    TaskRun.status == "failed",
                    TaskRun.id > last_success_subq,
                )
            )
            result = await session.execute(stmt)
            count = result.scalar_one_or_none() or 0
            if count >= max_failures:
                role_label = role.capitalize()
                gate_name = "repeated_failure" if role == "researcher" else f"{role}_repeated_failure"
                return BlockReason(
                    gate=gate_name,
                    detail=(
                        f"{role_label} has failed {count} times for #{issue}; "
                        f"human review required (threshold: {max_failures})"
                    ),
                )
    except Exception:
        log.debug("repeated_failures.check_failed", issue=issue, role=role, exc_info=True)

    return None
