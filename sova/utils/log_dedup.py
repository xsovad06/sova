"""In-process deduplication for noisy repeated log warnings.

Long-running daemon processes (supervisor poll loop, dashboard) re-evaluate
the same external input (issue bodies, PR reviews) on every cycle. Without
dedup, a single persistently-flagged issue re-logs the same warning forever.
``warn_once`` suppresses repeats of the same (event, key) pair within a TTL
window, bounded by a max entry count so a long-uptime process cannot grow
this state unboundedly.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any

import structlog

_MAX_ENTRIES = 2000
_DEFAULT_TTL_SECONDS = 3600.0

_seen: OrderedDict[str, float] = OrderedDict()


def warn_once(
    log: structlog.stdlib.BoundLogger,
    event: str,
    key: str,
    *,
    ttl_seconds: float = _DEFAULT_TTL_SECONDS,
    **fields: Any,
) -> None:
    """Log ``event`` at warning level at most once per (event, key) within ttl_seconds.

    A repeat for the same key inside the window is silently dropped. Intended
    for warnings driven by external input that gets re-scanned every poll
    cycle without the underlying content changing.
    """
    dedup_key = f"{event}:{key}"
    now = time.monotonic()
    last = _seen.get(dedup_key)
    if last is not None and now - last < ttl_seconds:
        return

    _seen[dedup_key] = now
    _seen.move_to_end(dedup_key)
    while len(_seen) > _MAX_ENTRIES:
        _seen.popitem(last=False)

    log.warning(event, **fields)


def reset() -> None:
    """Clear all dedup state.

    Test-only: production relies on process lifetime + TTL to bound this,
    but tests that assert on repeated warnings need a clean slate between
    cases regardless of what an earlier test already logged.
    """
    _seen.clear()
