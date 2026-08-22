"""Address-review circuit breaker gate: blocks after too many review cycles."""

from __future__ import annotations

from pathlib import Path

from sova.supervisor.gates import BlockReason
from sova.supervisor.gates.utils import count_address_review_runs
from sova.utils.logging import get_logger

log = get_logger(component="supervisor.gates.circuit_breaker")


async def check_address_review_circuit_breaker_gate(
    issue: int,
    pr_number: int | None,
    max_cycles: int,
    project_dir: Path,
) -> BlockReason | None:
    """Block SPAWN_ADDRESS_REVIEW when the address-review cycle limit is reached."""
    if pr_number is None:
        return None

    try:
        if max_cycles <= 0:
            return None

        count = await count_address_review_runs(str(issue), pr_number, project_dir)
        if count >= max_cycles:
            return BlockReason(
                gate="circuit_breaker",
                detail=f"Address-review circuit breaker: {count}/{max_cycles} cycles completed for PR #{pr_number}",
            )
    except Exception:
        log.debug("circuit_breaker_gate.check_failed", issue=issue, exc_info=True)

    return None
