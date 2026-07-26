"""Supervisor daemon: unified polling loop orchestrating all sub-components.

Coordinates TaskProgressionEngine, PR throttle, CodeRabbit quota, and health
checks in a single ordered poll cycle. Every decision is logged to the
SupervisorDecision DB table for diagnostics.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from sova.config.models import ProjectConfig
from sova.db.models import SupervisorDecision
from sova.utils.logging import get_logger

log = get_logger(component="supervisor.daemon")


class SupervisorDaemon:
    """Coordinated polling daemon for supervisor sub-components."""

    def __init__(
        self,
        config: ProjectConfig,
        project_dir: Path,
        session_factory: async_sessionmaker,
    ) -> None:
        self._config = config
        self._project_dir = project_dir
        self._session_factory = session_factory
        self._poll_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_poll_at: datetime | None = None

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> asyncio.Task:
        """Start the daemon polling loop as a background task."""
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        return self._task

    async def stop(self) -> None:
        """Stop the daemon."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def poll_once(self) -> dict:
        """Execute a single poll cycle (used by manual trigger and loop)."""
        async with self._poll_lock:
            return await self._poll_once()

    def get_status(self) -> dict:
        """Return current daemon status."""
        return {
            "enabled": self._config.supervisor.enabled,
            "running": self._running,
            "poll_interval_seconds": self._config.supervisor.poll_interval_seconds,
            "log_retention_days": self._config.supervisor.log_retention_days,
            "project_dir": str(self._project_dir),
            "last_poll_at": self._last_poll_at.isoformat().replace("+00:00", "Z") if self._last_poll_at else None,
        }

    async def _run_loop(self) -> None:
        """Main daemon loop: purge old logs on start, then poll periodically."""
        await self._purge_old_logs()
        while self._running:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("daemon.poll_error", exc_info=True)
            await asyncio.sleep(self._config.supervisor.poll_interval_seconds)

    async def _poll_once(self) -> dict:
        """Single ordered poll: quota -> progression -> health."""
        from sova.adapters import create_adapter

        try:
            adapter = create_adapter(self._config)
        except Exception as exc:
            log.warning("poll.adapter_creation_failed", exc_info=True)
            err = str(exc)
            return {"progression": {"error": err}, "quota": {"error": err}, "health": {"adapter": f"error: {err}"}}

        results: dict = {}

        # Phase 1: CodeRabbit quota sync (must run before progression so the
        # progression engine reads fresh data, not last cycle's cached counts).
        results["quota"] = await self._poll_quota()

        # Phase 2: Progression engine (evaluates against freshly synced quota)
        results["progression"] = await self._poll_progression(adapter)

        # Phase 3: Health check
        results["health"] = await self._poll_health()

        self._last_poll_at = datetime.now(timezone.utc)
        return results

    async def _poll_progression(self, adapter) -> dict:
        """Run the task progression engine and log decisions."""
        try:
            from sova.supervisor.progression import TaskProgressionEngine

            engine = TaskProgressionEngine(
                config=self._config.supervisor,
                adapter=adapter,
                project_dir=self._project_dir,
                session_factory=self._session_factory,
            )
            decisions = await engine.evaluate_all()

            records = [
                SupervisorDecision(
                    project_slug=self._config.github_repo,
                    component="progression",
                    event_type="decision",
                    issue_number=str(d.issue_number),
                    action=d.action.value,
                    detail=d.reason,
                    metadata_json={"blocked_by": [{"gate": b.gate, "detail": b.detail} for b in d.blocked_by]}
                    if d.blocked_by
                    else None,
                )
                for d in decisions
            ]
            if records:
                await self._log_decisions_batch(records)

            # Execute spawn decisions
            executed = await engine.execute_decisions(decisions)

            return {"decisions": len(decisions), "executed": executed}
        except Exception as exc:
            await self._log_decision(
                component="progression",
                event_type="health",
                action="error",
                detail=str(exc),
            )
            log.warning("poll.progression_error", exc_info=True)
            return {"error": str(exc)}

    async def _poll_quota(self) -> dict:
        """Sync CodeRabbit quota from GitHub and log status."""
        if not self._config.coderabbit_quota.enabled:
            return {"enabled": False}

        try:
            from sova.supervisor.coderabbit_quota import get_quota_status, sync_from_github

            async with self._session_factory() as session:
                async with session.begin():
                    await sync_from_github(
                        session,
                        repo=self._config.github_repo,
                        config=self._config.coderabbit_quota,
                    )

            async with self._session_factory() as session:
                status = await get_quota_status(session, self._config.coderabbit_quota)

            await self._log_decision(
                component="quota",
                event_type="status",
                action="ok" if status.can_create_pr else "throttled",
                detail=(f"{status.reviews_in_window}/{status.reviews_per_hour} reviews in window"),
                metadata={
                    "reviews_in_window": status.reviews_in_window,
                    "can_create_pr": status.can_create_pr,
                    "next_available_minutes": status.next_available_minutes,
                },
            )
            return {
                "reviews_in_window": status.reviews_in_window,
                "can_create_pr": status.can_create_pr,
            }
        except Exception as exc:
            await self._log_decision(
                component="quota",
                event_type="health",
                action="error",
                detail=str(exc),
            )
            log.warning("poll.quota_error", exc_info=True)
            return {"error": str(exc)}

    async def _poll_health(self) -> dict:
        """Basic health check: verify DB connectivity. Adapter errors surface through progression logs."""
        checks: dict = {}

        try:
            async with self._session_factory() as session:
                await session.execute(select(SupervisorDecision.id).limit(1))
            checks["db"] = "ok"
        except Exception as exc:
            checks["db"] = f"error: {exc}"

        return checks

    async def _log_decision(
        self,
        *,
        component: str,
        event_type: str,
        action: str = "",
        detail: str = "",
        issue_number: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Write a single decision record to the DB."""
        await self._log_decisions_batch(
            [
                SupervisorDecision(
                    project_slug=self._config.github_repo,
                    component=component,
                    event_type=event_type,
                    issue_number=issue_number,
                    action=action,
                    detail=detail,
                    metadata_json=metadata,
                )
            ]
        )

    async def _log_decisions_batch(self, records: list[SupervisorDecision]) -> None:
        """Write multiple decision records in a single transaction."""
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    session.add_all(records)
        except Exception:
            log.debug("log_decision.write_failed", exc_info=True)

    async def _purge_old_logs(self) -> None:
        """Delete decision logs older than retention period."""
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=self._config.supervisor.log_retention_days)
            async with self._session_factory() as session:
                async with session.begin():
                    await session.execute(delete(SupervisorDecision).where(SupervisorDecision.created_at < cutoff))
            log.info("daemon.purged_old_logs", retention_days=self._config.supervisor.log_retention_days)
        except Exception:
            log.warning("daemon.purge_failed", exc_info=True)
