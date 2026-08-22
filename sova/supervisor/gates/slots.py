"""Agent slot gate: blocks when all agent slots are occupied."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from sova.core.state import TASK_RUN_TERMINAL
from sova.db.models import TaskRun
from sova.supervisor.gates import BlockReason
from sova.utils.logging import get_logger
from sova.utils.process import is_process_alive

log = get_logger(component="supervisor.gates.slots")


async def get_alive_count(session_factory: async_sessionmaker) -> int:
    """Count active agent reservations: alive processes plus pending (PID-less) runs."""
    try:
        async with session_factory() as session:
            stmt = select(TaskRun).where(TaskRun.status.notin_(TASK_RUN_TERMINAL))
            result = await session.execute(stmt)
            active_runs = result.scalars().all()
            return sum(1 for run in active_runs if run.pid is None or is_process_alive(run.pid))
    except Exception:
        log.debug("get_alive_count.failed", exc_info=True)
        return 0


async def check_slot_gate(
    session_factory: async_sessionmaker,
    max_concurrent: int,
) -> BlockReason | None:
    """Check agent slot availability against max_concurrent.

    Args:
        session_factory: Database session factory.
        max_concurrent: Maximum number of concurrent agents (must be > 0).

    Returns:
        BlockReason if slots are full, None otherwise. Fails open on invalid config or errors.
    """
    if max_concurrent <= 0:
        log.warning("slot_gate.invalid_max_concurrent", value=max_concurrent)
        return None

    try:
        alive_count = await get_alive_count(session_factory)
        if alive_count >= max_concurrent:
            return BlockReason(
                gate="slots",
                detail=f"All agent slots occupied ({alive_count}/{max_concurrent})",
            )
    except Exception:
        log.debug("slot_gate.check_failed", exc_info=True)

    return None
