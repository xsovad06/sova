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

from sova.adapters.base import TaskAdapter
from sova.config.models import ProjectConfig
from sova.db.models import SupervisorDecision
from sova.utils.logging import get_logger

log = get_logger(component="supervisor.daemon")

_MIN_POLL_INTERVAL = 60


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
        self._config_reloaded = asyncio.Event()

    def reload_config(self, config: ProjectConfig) -> None:
        """Hot-reload config (called by settings router after TOML update)."""
        self._config = config
        self._config_reloaded.set()

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

    async def _interruptible_sleep(self, delay: float) -> None:
        """Sleep for *delay* seconds, but wake early if config is reloaded."""
        try:
            await asyncio.wait_for(self._config_reloaded.wait(), timeout=delay)
        except (TimeoutError, asyncio.TimeoutError):
            pass
        finally:
            self._config_reloaded.clear()

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
        """Main daemon loop: purge old logs on start, then poll periodically.

        Applies exponential backoff on consecutive failures (capped at 600s).
        """
        await self._purge_old_logs()
        consecutive_failures = 0
        max_backoff = 600
        while self._running:
            try:
                result = await self.poll_once()
                has_error = any(isinstance(v, dict) and "error" in v for v in result.values())
                if has_error:
                    consecutive_failures += 1
                    log.warning("daemon.poll_partial_error", consecutive=consecutive_failures, result=result)
                else:
                    consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                consecutive_failures += 1
                log.warning("daemon.poll_error", consecutive=consecutive_failures, exc_info=True)
            base_interval = max(self._config.supervisor.poll_interval_seconds, _MIN_POLL_INTERVAL)
            if consecutive_failures > 0:
                backoff = min(base_interval * (2**consecutive_failures), max_backoff)
                log.info("daemon.backoff", sleep_s=backoff, consecutive=consecutive_failures)
                await self._interruptible_sleep(backoff)
            else:
                await self._interruptible_sleep(base_interval)

    async def _poll_once(self) -> dict:
        """Single ordered poll: quota -> progression -> health.

        Snapshots config at entry so a mid-poll reload_config() call
        cannot mix old and new settings within one cycle.
        """
        from sova.adapters import create_adapter

        cfg = self._config

        try:
            adapter = create_adapter(cfg)
        except Exception as exc:
            log.warning("poll.adapter_creation_failed", exc_info=True)
            err = str(exc)
            return {"progression": {"error": err}, "quota": {"error": err}, "health": {"adapter": f"error: {err}"}}

        results: dict = {}

        # Phase 1: CodeRabbit quota sync (must run before progression so the
        # progression engine reads fresh data, not last cycle's cached counts).
        results["quota"] = await self._poll_quota(cfg)

        # Phase 1.5: Queue maintenance (prune done issues, discover ready ones).
        # Runs before progression so _resolve_task_ids() reads the freshly maintained queue.
        if cfg.supervisor.auto_queue:
            results["queue_maintenance"] = await self._poll_queue_maintenance(adapter, cfg)
        else:
            results["queue_maintenance"] = {"skipped": "auto_queue disabled"}

        # Phase 2: Progression engine (evaluates against freshly synced quota)
        progression_engine = None
        results["progression"], progression_engine = await self._poll_progression(adapter, cfg)

        # Phase 2.5: Epic auto-close (reuses the graph from progression to avoid a redundant API call)
        results["epic_close"] = await self._poll_epic_close(adapter, cfg, engine=progression_engine)

        # Phase 3: Health check
        results["health"] = await self._poll_health()

        self._last_poll_at = datetime.now(timezone.utc)
        return results

    async def _poll_progression(self, adapter: TaskAdapter, cfg: ProjectConfig) -> tuple[dict, object]:
        """Run the task progression engine and log decisions.

        Returns (result_dict, engine) so the caller can reuse the engine's
        cached dependency graph for subsequent phases (e.g. epic close).

        When ``require_approval`` is True the actionable decisions are stored in
        the pending plan (visible on the supervisor dashboard) and execution is
        deferred until the user approves them via the UI.  When False the
        existing auto-execute behaviour is preserved.
        """
        try:
            from sova.supervisor.progression import NON_ACTIONABLE_ACTIONS, TaskProgressionEngine

            plan = None
            if cfg.supervisor.llm_planning:
                from sova.supervisor.planner import SupervisorPlanner

                planner = SupervisorPlanner(
                    config=cfg,
                    project_dir=self._project_dir,
                    session_factory=self._session_factory,
                )
                plan = await planner.plan(adapter)

                if cfg.supervisor.auto_queue and plan and (plan.queue_removals or plan.queue_reorder):
                    from sova.supervisor.queue_maintenance import apply_planner_queue_changes

                    apply_planner_queue_changes(
                        cfg.supervisor,
                        self._project_dir,
                        removals=list(plan.queue_removals),
                        reorder=list(plan.queue_reorder),
                    )

            engine = TaskProgressionEngine(
                config=cfg.supervisor,
                adapter=adapter,
                project_dir=self._project_dir,
                session_factory=self._session_factory,
            )
            decisions = await engine.evaluate_all(plan=plan)

            records = [
                SupervisorDecision(
                    project_slug=cfg.github_repo,
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

            actionable = [d for d in decisions if d.action not in NON_ACTIONABLE_ACTIONS]

            if cfg.supervisor.require_approval:
                from sova.dashboard.services.supervisor_service import set_pending_plan

                reasoning = plan.reasoning if plan else None
                deferred = (
                    [{"action": d.action, "issue": d.issue, "reason": d.reason} for d in plan.deferred]
                    if plan
                    else None
                )
                set_pending_plan(actionable, reasoning=reasoning, deferred=deferred)
                log.info("poll.progression_pending_approval", count=len(actionable))
                return {"decisions": len(decisions), "pending": len(actionable), "executed": 0}, engine

            executed = await engine.execute_decisions(decisions)
            return {"decisions": len(decisions), "executed": executed, "pending": 0}, engine
        except Exception as exc:
            await self._log_decision(
                component="progression",
                event_type="health",
                action="error",
                detail=str(exc),
            )
            log.warning("poll.progression_error", exc_info=True)
            return {"error": str(exc)}, None

    async def _poll_epic_close(self, adapter: TaskAdapter, cfg: ProjectConfig, *, engine: object = None) -> dict:
        """Auto-close epic issues when all children are DONE."""
        try:
            from sova.supervisor.progression import TaskProgressionEngine

            if engine is None or not isinstance(engine, TaskProgressionEngine):
                engine = TaskProgressionEngine(
                    config=cfg.supervisor,
                    adapter=adapter,
                    project_dir=self._project_dir,
                    session_factory=self._session_factory,
                )
            results = await engine.auto_close_epics()

            # Log each closed epic as a decision
            for result in results:
                if result.get("closed"):
                    await self._log_decision(
                        component="epic_close",
                        event_type="auto_close",
                        issue_number=str(result["issue"]),
                        action="closed",
                        detail=f"All children complete: {result.get('title', '')}",
                    )

            return {"checked": True, "closed": len([r for r in results if r.get("closed")])}
        except Exception as exc:
            await self._log_decision(
                component="epic_close",
                event_type="health",
                action="error",
                detail=str(exc),
            )
            log.warning("poll.epic_close_error", exc_info=True)
            return {"error": str(exc)}

    async def _poll_quota(self, cfg: ProjectConfig) -> dict:
        """Sync CodeRabbit quota from GitHub and log status."""
        if not cfg.coderabbit_quota.enabled:
            return {"enabled": False}

        try:
            from sova.supervisor.coderabbit_quota import get_quota_status, sync_from_github

            async with self._session_factory() as session:
                async with session.begin():
                    await sync_from_github(
                        session,
                        repo=cfg.github_repo,
                        config=cfg.coderabbit_quota,
                        project_slug=cfg.github_repo,
                    )

            async with self._session_factory() as session:
                status = await get_quota_status(
                    session,
                    cfg.coderabbit_quota,
                    project_slug=cfg.github_repo,
                )

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

    async def _poll_queue_maintenance(self, adapter: TaskAdapter, cfg: ProjectConfig) -> dict:
        """Run deterministic queue maintenance: prune done, discover ready, persist."""
        try:
            from sova.supervisor.queue_maintenance import maintain_queue

            result = await maintain_queue(adapter, cfg.supervisor, self._project_dir)
            return {
                "changed": result.changed,
                "removed": list(result.removed),
                "added": list(result.added),
                "queue_size": len(result.current),
            }
        except Exception as exc:
            log.warning("poll.queue_maintenance_error", exc_info=True)
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
