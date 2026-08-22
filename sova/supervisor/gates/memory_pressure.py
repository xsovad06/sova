"""Memory pressure gate: blocks when system memory is too low."""

from __future__ import annotations

from sova.config.models import MemoryGuardConfig
from sova.supervisor.gates import BlockReason
from sova.utils.logging import get_logger

try:
    import psutil

    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False

log = get_logger(component="supervisor.gates.memory_pressure")


def check_memory_pressure_gate(memory_guard: MemoryGuardConfig) -> BlockReason | None:
    """Check system memory pressure. Fail-open if psutil is unavailable."""
    if not _PSUTIL_AVAILABLE:
        return None

    try:
        if not memory_guard.enabled:
            return None
        mem = psutil.virtual_memory()
        available_gb = mem.available / (1024**3)
        block_threshold = memory_guard.block_threshold_gb
        warn_threshold = memory_guard.warn_threshold_gb

        if available_gb < block_threshold:
            return BlockReason(
                gate="memory",
                detail=(
                    f"System memory pressure: {available_gb:.2f} GB available < {block_threshold:.2f} GB threshold"
                ),
            )
        if available_gb < warn_threshold:
            log.warning(
                "memory_pressure.warn",
                available_gb=round(available_gb, 2),
                warn_threshold_gb=warn_threshold,
            )
    except Exception:
        log.debug("memory_gate.check_failed", exc_info=True)

    return None
