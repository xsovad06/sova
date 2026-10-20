"""Health service: dashboard wrapper around sova.utils.diagnostics's checks.

Mirrors setup_service.py's get_auth_status() caching pattern (#933): a
module-level cache keyed by project dir with a matching per-project
asyncio.Lock for single-flight de-duplication, so concurrent requests for the
same project within the TTL share one probe instead of each spawning
duplicate `claude --version`/`gh auth status`/etc. subprocess calls.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from sova.utils.diagnostics import DiagnosticCheck, run_all_checks
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.health")

# Matches setup_service.py's _AUTH_STATUS_CACHE_TTL: long enough that a page
# load doesn't re-run every CLI/network probe on every poll.
_HEALTH_CHECKS_CACHE_TTL = 60.0
_health_checks_cache: dict[str, tuple[float, dict]] = {}
_health_checks_locks: dict[str, asyncio.Lock] = {}


async def get_health_checks(project_dir: Path) -> dict:
    """Run every diagnostic check and return a JSON-serializable summary.

    Never raises: run_all_checks() already guards each individual check, so
    this is a backstop for a batch-level failure (something outside any one
    check), converted into a single failed DiagnosticCheck row rather than
    propagating, so the panel never blanks and the endpoint never 500s.
    """
    cache_key = str(project_dir.resolve())
    now = time.monotonic()
    cached = _health_checks_cache.get(cache_key)
    if cached and (now - cached[0]) < _HEALTH_CHECKS_CACHE_TTL:
        return {**cached[1], "cached": True}

    lock = _health_checks_locks.setdefault(cache_key, asyncio.Lock())
    async with lock:
        # Recheck after acquiring: another task may have refreshed while we waited.
        now = time.monotonic()
        cached = _health_checks_cache.get(cache_key)
        if cached and (now - cached[0]) < _HEALTH_CHECKS_CACHE_TTL:
            return {**cached[1], "cached": True}

        try:
            checks = await run_all_checks(project_dir)
        except Exception as exc:  # noqa: BLE001 (HTTP/UI boundary; a batch failure is one failed row, not a 500)
            log.warning("health.run_all_checks_failed", exc_info=True)
            checks = [DiagnosticCheck("health checks", False, str(exc)[:80], False)]

        result = {
            "checks": [asdict(check) for check in checks],
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

        _health_checks_cache[cache_key] = (time.monotonic(), result)
        return {**result, "cached": False}
