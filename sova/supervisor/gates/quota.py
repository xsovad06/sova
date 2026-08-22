"""CodeRabbit quota gate: blocks developer spawns when review quota is exhausted."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker

from sova.config.models import CodeRabbitQuotaConfig
from sova.supervisor.gates import BlockReason
from sova.utils.logging import get_logger

log = get_logger(component="supervisor.gates.quota")


async def check_quota_gate(
    is_developer: bool,
    quota_config: CodeRabbitQuotaConfig,
    session_factory: async_sessionmaker,
) -> BlockReason | None:
    """Check CodeRabbit quota headroom for actions that produce PRs."""
    if not is_developer:
        return None

    try:
        from sova.supervisor.coderabbit_quota import get_quota_status

        if not quota_config.enabled:
            return None

        async with session_factory() as session:
            status = await get_quota_status(session, quota_config)
            if not status.can_create_pr:
                wait_msg = ""
                if status.next_available_minutes is not None:
                    wait_msg = f" (available in {status.next_available_minutes:.0f}m)"
                return BlockReason(
                    gate="quota",
                    detail=f"CodeRabbit quota exhausted{wait_msg}",
                )
    except Exception:
        log.debug("quota_gate.check_failed", exc_info=True)

    return None
