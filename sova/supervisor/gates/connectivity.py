"""Connectivity gate: blocks when the network is unreachable.

Complements the pre-spawn check in ``agent_validation.check_network_connectivity``:
this one stops the supervisor *deciding* to spawn, so an outage produces no
decisions, no feed noise and no wasted slots, rather than a decision that the
spawn path then rejects.
"""

from __future__ import annotations

from sova.config.models import NetworkGuardConfig
from sova.supervisor.gates import BlockReason
from sova.utils.logging import get_logger

log = get_logger(component="supervisor.gates.connectivity")


def check_connectivity_gate(network_guard: NetworkGuardConfig) -> BlockReason | None:
    """Check network reachability. Fail-open."""
    try:
        if not network_guard.enabled or not network_guard.block_spawns:
            return None

        from sova.supervisor.network_health import get_connectivity_status

        status = get_connectivity_status()
        if status.is_down:
            return BlockReason(
                gate="connectivity",
                detail=f"Network unreachable (down for {round(status.down_for_seconds)}s)",
            )
    except Exception:  # noqa: BLE001 (gates fail open: an unevaluable gate must not block progression)
        log.debug("connectivity_gate.check_failed", exc_info=True)

    return None
