"""OversightAgent: background daemon that wakes on a configurable interval.

Records each wake cycle to the OversightRun DB table. The persona is loaded
fresh each cycle and available via ``get_system_prompt()`` for LLM context
injection. Each cycle runs the observation phase (#445) to collect a
cross-project health snapshot, then the analysis phase (#446) sends the
snapshot to the LLM to produce structured findings.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone

from sova.config.models import OversightConfig
from sova.db.models import OversightRun, OversightRunStatus
from sova.oversight.persona import load_persona
from sova.utils.logging import get_logger

log = get_logger(component="oversight.agent")


class OversightAgent:
    """Server-global background daemon for autonomous project monitoring."""

    def __init__(self, config: OversightConfig) -> None:
        self._config = config
        self._task: asyncio.Task | None = None
        self._cycle_number: int = 0
        self._persona: str = ""

    def start(self) -> asyncio.Task:
        """Start the oversight background loop. Returns the task for cancellation."""
        self._task = asyncio.create_task(self._run_loop())
        log.info(
            "oversight.started",
            interval_minutes=self._config.wake_interval_minutes,
        )
        return self._task

    def get_system_prompt(self) -> str:
        """Return the current system prompt with persona context for LLM calls.

        The persona is loaded fresh each wake cycle (no caching) so edits to the
        persona file take effect immediately. Hooks (#445, #446, #447) call this
        method to inject persona guidance into their LLM requests.
        """
        if not self._persona:
            return ""
        return f"# Operations Persona (user-defined oversight guidance)\n\n{self._persona}\n\n---\n\n"

    async def stop(self) -> None:
        """Cancel the oversight background task."""
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
            log.info("oversight.stopped")

    @staticmethod
    def _determine_outcome(
        snapshot: dict | None,
        analysis_error: str | None,
    ) -> tuple[OversightRunStatus, str | None]:
        """Determine the run status and error message from cycle results."""
        if snapshot is None:
            return OversightRunStatus.ERROR, "observation_failed"
        if analysis_error is not None:
            return OversightRunStatus.ERROR, analysis_error
        return OversightRunStatus.DONE, None

    async def _record_error_safe(
        self,
        run_id: str,
        cycle: int,
        duration_ms: int,
        started_at: datetime,
        error: str,
    ) -> None:
        """Record an error run, swallowing DB failures."""
        try:
            await self._record_run(
                run_id,
                cycle,
                OversightRunStatus.ERROR,
                duration_ms,
                started_at=started_at,
                error=error,
            )
        except Exception:
            log.warning("oversight.error_record_failed", exc_info=True)

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
                self._persona = load_persona(self._config.persona_path)
                snapshot = await self._observe()
                analysis_error: str | None = None
                if snapshot is not None:
                    analysis_error = await self._analyze(snapshot, run_id)
                # Future hooks (#447) execute here.
                duration_ms = int((time.monotonic() - t0) * 1000)
                status, error = self._determine_outcome(snapshot, analysis_error)
                await self._record_run(
                    run_id,
                    cycle,
                    status,
                    duration_ms,
                    started_at=started_at,
                    snapshot=snapshot,
                    error=error,
                )
                log.debug("oversight.cycle_done", cycle=cycle, run_id=run_id, duration_ms=duration_ms)
            except asyncio.CancelledError:
                duration_ms = int((time.monotonic() - t0) * 1000)
                await self._record_error_safe(run_id, cycle, duration_ms, started_at, "cancelled")
                raise
            except Exception as exc:
                duration_ms = int((time.monotonic() - t0) * 1000)
                log.warning("oversight.cycle_error", cycle=cycle, run_id=run_id, exc_info=True)
                await self._record_error_safe(run_id, cycle, duration_ms, started_at, str(exc))
            await asyncio.sleep(interval_seconds)

    async def _analyze(self, snapshot: dict, run_id: str) -> str | None:
        """Run the analysis phase: send snapshot to LLM, persist findings.

        Returns:
            Error message if analysis failed, None if successful.
        """
        from sova.llm.client import get_provider
        from sova.oversight.analysis import analyze_snapshot

        try:
            provider = get_provider()
            _, error = await analyze_snapshot(
                snapshot,
                run_id,
                self._persona,
                provider,
                model=self._config.analysis_model,
                dedup_window_days=self._config.dedup_window_days,
                analysis_timeout=self._config.analysis_timeout_seconds,
            )
            return error
        except Exception as exc:
            log.warning("oversight.analyze_failed", run_id=run_id, exc_info=True)
            return f"analyze_failed: {exc}"

    async def _observe(self) -> dict | None:
        """Run the observation phase and return the snapshot as a dict."""
        from sova.oversight.observation import build_snapshot

        try:
            snapshot = await build_snapshot()
            return snapshot.to_dict()
        except Exception:
            log.warning("oversight.observe_failed", exc_info=True)
            return None

    async def _record_run(
        self,
        run_id: str,
        cycle: int,
        status: OversightRunStatus,
        duration_ms: int,
        *,
        started_at: datetime | None = None,
        error: str | None = None,
        snapshot: dict | None = None,
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
                        snapshot_json=snapshot,
                        started_at=started_at or now,
                        ended_at=now,
                    )
                    session.add(record)
        except Exception:
            log.warning("oversight.record_failed", run_id=run_id, cycle=cycle, exc_info=True)
