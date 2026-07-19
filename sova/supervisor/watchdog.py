"""Agent watchdog: detects stuck, zombie, and bypassed agent processes.

Runs as a background asyncio task inside the dashboard lifespan.
Periodically scans active TaskRun records, detects four anomaly types
(pipeline not adopted, no output, step timeout, zombie process), and
takes corrective action (warn via feed event, kill via stop_agent).

Killed agents flow through the existing _wait_and_finalize -> _schedule_retry()
path for auto-retry. The watchdog never independently retries.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from sova.config.models import WatchdogConfig
from sova.core.state import TASK_RUN_TERMINAL
from sova.dashboard.services.agent_recovery import _is_process_alive
from sova.dashboard.services.feed_service import FeedEventSeverity, emit_safe
from sova.db.models import OutputLine, TaskRun
from sova.db.session import get_session
from sova.utils.logging import get_logger

log = get_logger(component="supervisor.watchdog")


def _as_utc(dt: datetime) -> datetime:
    """Return dt with UTC tzinfo, adding it when SQLite returns naive datetimes."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class AnomalySignal(StrEnum):
    PIPELINE_NOT_ADOPTED = "pipeline_not_adopted"
    NO_OUTPUT_WARN = "no_output_warn"
    NO_OUTPUT_KILL = "no_output_kill"
    STEP_TIMEOUT_WARN = "step_timeout_warn"
    ZOMBIE_PROCESS = "zombie_process"


class WatchdogAction(StrEnum):
    WARN = "warn"
    KILL = "kill"


@dataclass(frozen=True, slots=True)
class WatchdogFinding:
    run_id: int
    issue_number: str | None
    signal: AnomalySignal
    action: WatchdogAction
    detail: str
    metadata: dict[str, Any]


class AgentWatchdog:
    """Background watchdog that detects stuck and zombie agent processes."""

    def __init__(
        self,
        config: WatchdogConfig,
        project_dir: Path,
    ) -> None:
        self._config = config
        self._project_dir = project_dir
        self._cooldowns: dict[tuple[int, str], float] = {}
        # (run_id, step_name) -> monotonic time when that step was first seen
        self._step_started_at: dict[tuple[int, str], float] = {}
        self._task: asyncio.Task | None = None

    def start(self) -> asyncio.Task:
        """Start the watchdog background loop. Returns the task for cancellation."""
        self._task = asyncio.create_task(self._run_loop())
        log.info("watchdog.started", interval=self._config.check_interval_seconds)
        return self._task

    async def stop(self) -> None:
        """Cancel the watchdog background task."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
            log.info("watchdog.stopped")

    async def _run_loop(self) -> None:
        """Main loop: scan immediately, then sleep between scans."""
        try:
            # Initial scan to catch runs stuck before the watchdog started
            await self._scan_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("watchdog.scan_error", exc_info=True)

        while True:
            await asyncio.sleep(self._config.check_interval_seconds)
            try:
                await self._scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("watchdog.scan_error", exc_info=True)

    async def _scan_once(self) -> list[WatchdogFinding]:
        """Single scan pass: query active runs, detect anomalies, execute actions."""
        findings: list[WatchdogFinding] = []

        async with await get_session(project_dir=self._project_dir) as session:
            active_runs = await self._get_active_runs(session)
            if not active_runs:
                self._prune_cooldowns(set())
                return findings

            run_ids = [r.id for r in active_runs]
            last_output_times = await self._get_last_output_times(session, run_ids)

        now = datetime.now(timezone.utc)
        active_run_ids = set(run_ids)

        for run in active_runs:
            run_findings = self._detect_anomalies(run, now, last_output_times)
            for finding in run_findings:
                if self._is_on_cooldown(finding):
                    continue
                self._record_cooldown(finding)
                findings.append(finding)

        self._prune_cooldowns(active_run_ids)

        if findings:
            await asyncio.gather(*(self._execute_finding(f) for f in findings))
        if findings:
            log.info("watchdog.scan_complete", findings_count=len(findings))

        return findings

    async def _get_active_runs(self, session: AsyncSession) -> list[TaskRun]:
        """Query all non-terminal TaskRun records with a PID."""
        stmt = (
            select(TaskRun)
            .options(defer(TaskRun.handoff_json), defer(TaskRun.error_message))
            .where(
                TaskRun.status.notin_(TASK_RUN_TERMINAL),
                TaskRun.pid.isnot(None),
            )
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def _get_last_output_times(self, session: AsyncSession, run_ids: list[int]) -> dict[int, datetime]:
        """Get the most recent output timestamp for each run."""
        if not run_ids:
            return {}

        stmt = (
            select(OutputLine.task_run_id, func.max(OutputLine.created_at))
            .where(OutputLine.task_run_id.in_(run_ids))
            .group_by(OutputLine.task_run_id)
        )
        result = await session.execute(stmt)
        return {row[0]: row[1] for row in result.all()}

    def _detect_anomalies(
        self,
        run: TaskRun,
        now: datetime,
        last_output_times: dict[int, datetime],
    ) -> list[WatchdogFinding]:
        """Check a single run for all anomaly types."""
        findings: list[WatchdogFinding] = []

        # 1. Pipeline not adopted: current_step is still "agent" sentinel
        if run.current_step == "agent":
            adopt_finding = self._check_pipeline_not_adopted(run, now)
            if adopt_finding:
                findings.append(adopt_finding)
                return findings  # skip other checks if pipeline was never adopted

        # 2. Zombie process: PID is dead but run is not terminal
        if run.pid is not None and not _is_process_alive(run.pid):
            findings.append(
                WatchdogFinding(
                    run_id=run.id,
                    issue_number=run.issue_number,
                    signal=AnomalySignal.ZOMBIE_PROCESS,
                    action=WatchdogAction.WARN,
                    detail=f"Run {run.id} has dead PID {run.pid} but status '{run.status}'",
                    metadata={"pid": run.pid, "status": run.status},
                )
            )
            return findings  # zombie: liveness sweep will handle, just emit event

        # 3. No output: check time since last output (or started_at as baseline)
        last_output = last_output_times.get(run.id)
        raw_baseline = last_output or run.started_at
        if raw_baseline:
            baseline = _as_utc(raw_baseline)
            minutes_silent = (now - baseline).total_seconds() / 60.0
            no_output_finding = self._check_no_output(run, minutes_silent)
            if no_output_finding:
                findings.append(no_output_finding)

        # 4. Step timeout: track when each step was first seen, warn when too long
        if run.current_step and run.current_step != "agent":
            step_key = (run.id, run.current_step)
            if step_key not in self._step_started_at:
                self._step_started_at[step_key] = time.monotonic()
        step_finding = self._check_step_timeout(run)
        if step_finding:
            findings.append(step_finding)

        return findings

    def _check_pipeline_not_adopted(self, run: TaskRun, now: datetime) -> WatchdogFinding | None:
        """Kill if pipeline was never adopted within timeout."""
        if run.started_at is None:
            return None
        minutes_elapsed = (now - _as_utc(run.started_at)).total_seconds() / 60.0
        if minutes_elapsed < self._config.pipeline_adopt_timeout_minutes:
            return None

        return WatchdogFinding(
            run_id=run.id,
            issue_number=run.issue_number,
            signal=AnomalySignal.PIPELINE_NOT_ADOPTED,
            action=WatchdogAction.KILL,
            detail=(
                f"Run {run.id} has not adopted the pipeline after "
                f"{minutes_elapsed:.0f}m (threshold: {self._config.pipeline_adopt_timeout_minutes}m)"
            ),
            metadata={"minutes_elapsed": round(minutes_elapsed, 1)},
        )

    def _check_no_output(self, run: TaskRun, minutes_silent: float) -> WatchdogFinding | None:
        """Warn at threshold, kill at higher threshold."""
        if minutes_silent >= self._config.no_output_kill_minutes:
            return WatchdogFinding(
                run_id=run.id,
                issue_number=run.issue_number,
                signal=AnomalySignal.NO_OUTPUT_KILL,
                action=WatchdogAction.KILL,
                detail=(
                    f"Run {run.id} has produced no output for "
                    f"{minutes_silent:.0f}m (kill threshold: {self._config.no_output_kill_minutes}m)"
                ),
                metadata={"minutes_silent": round(minutes_silent, 1)},
            )
        if minutes_silent >= self._config.no_output_warn_minutes:
            return WatchdogFinding(
                run_id=run.id,
                issue_number=run.issue_number,
                signal=AnomalySignal.NO_OUTPUT_WARN,
                action=WatchdogAction.WARN,
                detail=(
                    f"Run {run.id} has produced no output for "
                    f"{minutes_silent:.0f}m (warn threshold: {self._config.no_output_warn_minutes}m)"
                ),
                metadata={"minutes_silent": round(minutes_silent, 1)},
            )
        return None

    def _check_step_timeout(self, run: TaskRun) -> WatchdogFinding | None:
        """Warn if stuck on the same step for too long.

        Uses per-step first-seen timestamps so long-running healthy runs (with
        many sequential steps) are not false-positively flagged.
        """
        if not run.current_step or run.current_step == "agent":
            return None
        step_key = (run.id, run.current_step)
        step_first_seen = self._step_started_at.get(step_key)
        if step_first_seen is None:
            return None  # step just registered this scan; wait for next cycle
        minutes_on_step = (time.monotonic() - step_first_seen) / 60.0
        if minutes_on_step < self._config.step_warn_minutes:
            return None
        return WatchdogFinding(
            run_id=run.id,
            issue_number=run.issue_number,
            signal=AnomalySignal.STEP_TIMEOUT_WARN,
            action=WatchdogAction.WARN,
            detail=(
                f"Run {run.id} has been on step '{run.current_step}' for "
                f"{minutes_on_step:.0f}m (threshold: {self._config.step_warn_minutes}m)"
            ),
            metadata={
                "current_step": run.current_step,
                "minutes_on_step": round(minutes_on_step, 1),
            },
        )

    def _is_on_cooldown(self, finding: WatchdogFinding) -> bool:
        """Check if this (run_id, signal) pair is within cooldown window."""
        key = (finding.run_id, finding.signal.value)
        last_alert = self._cooldowns.get(key)
        if last_alert is None:
            return False
        cooldown_seconds = self._config.cooldown_minutes * 60
        return (time.monotonic() - last_alert) < cooldown_seconds

    def _record_cooldown(self, finding: WatchdogFinding) -> None:
        """Record the current time for cooldown tracking."""
        key = (finding.run_id, finding.signal.value)
        self._cooldowns[key] = time.monotonic()

    def _prune_cooldowns(self, active_run_ids: set[int]) -> None:
        """Remove cooldown and step-tracking entries for runs that are no longer active."""
        stale_keys = [k for k in self._cooldowns if k[0] not in active_run_ids]
        for k in stale_keys:
            del self._cooldowns[k]
        stale_step_keys = [k for k in self._step_started_at if k[0] not in active_run_ids]
        for k in stale_step_keys:
            del self._step_started_at[k]

    async def _execute_finding(self, finding: WatchdogFinding) -> None:
        """Take action on a finding: emit feed event, optionally kill."""
        severity = FeedEventSeverity.error if finding.action == WatchdogAction.KILL else FeedEventSeverity.warning
        issue_label = f" (#{finding.issue_number})" if finding.issue_number else ""
        emit_safe(
            f"Watchdog: {finding.signal.value}{issue_label}",
            severity=severity,
            detail=finding.detail,
            category="watchdog",
            metadata={
                "run_id": finding.run_id,
                "signal": finding.signal.value,
                "action": finding.action.value,
                **finding.metadata,
            },
        )

        if finding.action == WatchdogAction.KILL:
            await self._kill_agent(finding)

    async def _kill_agent(self, finding: WatchdogFinding) -> None:
        """Kill an agent process. Re-query status to guard against races."""
        from sova.dashboard.services.agent_lifecycle import stop_agent

        # Re-query to guard against race with _wait_and_finalize
        async with await get_session(project_dir=self._project_dir) as session:
            stmt = select(TaskRun.status).where(TaskRun.id == finding.run_id)
            result = await session.execute(stmt)
            row = result.first()
            if row is None:
                return
            current_status = row[0]

        if current_status in TASK_RUN_TERMINAL:
            log.debug(
                "watchdog.skip_kill_terminal",
                run_id=finding.run_id,
                status=current_status,
            )
            return

        log.warning(
            "watchdog.killing_agent",
            run_id=finding.run_id,
            signal=finding.signal.value,
            detail=finding.detail,
        )
        try:
            await stop_agent(run_id=finding.run_id)
        except Exception:
            log.warning("watchdog.kill_failed", run_id=finding.run_id, exc_info=True)
