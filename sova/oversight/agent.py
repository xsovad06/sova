"""OversightAgent: background daemon that wakes on a configurable interval.

Records each wake cycle to the OversightRun DB table. The cycle body is
intentionally a no-op in this initial implementation; subsequent issues
(#445, #446, #447) add observation, LLM analysis, and issue creation hooks.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone

from sova.config.models import OversightConfig
from sova.db.models import OversightRun, OversightRunStatus
from sova.utils.logging import get_logger

log = get_logger(component="oversight.agent")


class OversightAgent:
    """Server-global background daemon for autonomous project monitoring."""

    def __init__(self, config: OversightConfig) -> None:
        self._config = config
        self._task: asyncio.Task | None = None
        self._cycle_number: int = 0

    def start(self) -> asyncio.Task:
        """Start the oversight background loop. Returns the task for cancellation."""
        self._task = asyncio.create_task(self._run_loop())
        log.info(
            "oversight.started",
            interval_minutes=self._config.wake_interval_minutes,
        )
        return self._task

    async def stop(self) -> None:
        """Cancel the oversight background task."""
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
            log.info("oversight.stopped")

    async def _run_loop(self) -> None:
        """Main loop: execute a wake cycle, then sleep for the configured interval."""
        interval_seconds = self._config.wake_interval_minutes * 60
        while True:
            run_id = str(uuid.uuid4())
            self._cycle_number += 1
            cycle = self._cycle_number
            started_at = datetime.now(timezone.utc)
            t0 = time.monotonic()
            try:
                log.debug("oversight.cycle_start", cycle=cycle, run_id=run_id)
                # Future hooks (#445, #446, #447) execute here.
                duration_ms = int((time.monotonic() - t0) * 1000)
                await self._record_run(run_id, cycle, OversightRunStatus.DONE, duration_ms, started_at=started_at)
                log.debug("oversight.cycle_done", cycle=cycle, run_id=run_id, duration_ms=duration_ms)
            except asyncio.CancelledError:
                duration_ms = int((time.monotonic() - t0) * 1000)
                try:
                    await self._record_run(
                        run_id,
                        cycle,
                        OversightRunStatus.ERROR,
                        duration_ms,
                        started_at=started_at,
                        error="cancelled",
                    )
                except Exception:
                    log.warning("oversight.cancelled_record_failed", exc_info=True)
                raise
            except Exception as exc:
                duration_ms = int((time.monotonic() - t0) * 1000)
                log.warning("oversight.cycle_error", cycle=cycle, run_id=run_id, exc_info=True)
                try:
                    await self._record_run(
                        run_id,
                        cycle,
                        OversightRunStatus.ERROR,
                        duration_ms,
                        started_at=started_at,
                        error=str(exc),
                    )
                except Exception:
                    log.warning("oversight.error_record_failed", exc_info=True)
            await asyncio.sleep(interval_seconds)

    async def _record_run(
        self,
        run_id: str,
        cycle: int,
        status: OversightRunStatus,
        duration_ms: int,
        *,
        started_at: datetime | None = None,
        error: str | None = None,
    ) -> None:
        """Persist a wake cycle record to the DB."""
        from sova.db.session import get_session

        try:
            async with await get_session() as session:
                async with session.begin():
                    now = datetime.now(timezone.utc)
                    record = OversightRun(
                        id=run_id,
                        status=status,
                        cycle_number=cycle,
                        duration_ms=duration_ms,
                        error=error,
                        started_at=started_at or now,
                        ended_at=now,
                    )
                    session.add(record)
        except Exception:
            log.warning("oversight.record_failed", run_id=run_id, cycle=cycle, exc_info=True)
