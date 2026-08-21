"""Combined FastAPI dashboard + asyncio scheduler daemon.

Runs the SOVA dashboard and the watch loop in a single process,
sharing the same event loop. The scheduler runs as a background
asyncio task alongside the uvicorn ASGI server.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from sova.config.models import ProjectConfig
from sova.const import DEFAULT_SERVER_PORT
from sova.utils.logging import get_logger

log = get_logger(component="scheduler.server")

# PID file location for server status/stop commands
_DEFAULT_PID_DIR = Path.home() / ".config" / "sova"


async def query_digest_stats(
    session: object,
    cutoff: object,
) -> tuple[int, int, int, float]:
    """Query task and cost statistics for a time period.

    Shared by both the CLI ``digest`` command and the server API endpoint.
    Returns (completed, failed, in_progress, total_cost).
    """
    from sqlalchemy import func, select

    from sova.db.models import CostRecord, TaskRun

    completed_result = await session.execute(
        select(func.count(TaskRun.id)).where(
            TaskRun.status == "done",
            TaskRun.started_at >= cutoff,
        )
    )
    completed = completed_result.scalar() or 0

    failed_result = await session.execute(
        select(func.count(TaskRun.id)).where(
            TaskRun.status == "failed",
            TaskRun.started_at >= cutoff,
        )
    )
    failed = failed_result.scalar() or 0

    in_progress_result = await session.execute(
        select(func.count(TaskRun.id)).where(
            TaskRun.status.in_(["running", "pending"]),
            TaskRun.started_at >= cutoff,
        )
    )
    in_progress = in_progress_result.scalar() or 0

    cost_result = await session.execute(
        select(func.sum(CostRecord.cost_usd)).where(
            CostRecord.recorded_at >= cutoff,
        )
    )
    total_cost = cost_result.scalar() or 0.0

    return completed, failed, in_progress, total_cost


class SOVAServer:
    """Combined dashboard + scheduler daemon.

    ``sova server start`` creates an instance and calls ``run()``,
    which starts the FastAPI dashboard and, when ``multi_project`` is
    ``False``, the single-project watch loop.  In multi-project mode
    the watch loop is skipped (supervisor daemons are started by the
    dashboard lifespan instead).
    """

    def __init__(
        self,
        *,
        config: ProjectConfig,
        project_dir: Path | None = None,
        host: str = "127.0.0.1",
        port: int = DEFAULT_SERVER_PORT,
        multi_project: bool = False,
    ) -> None:
        self._config = config
        self._project_dir = project_dir
        self._multi_project = multi_project
        self.host = host
        self.port = port
        self._running = False
        self._watch_task: asyncio.Task[None] | None = None
        self._watch_loop = None
        self._start_time = time.monotonic()

    @property
    def is_running(self) -> bool:
        return self._running

    def create_app(self) -> FastAPI:
        """Create the FastAPI app with scheduler lifecycle hooks and status endpoint."""
        from sova.dashboard.app import create_app as create_dashboard_app

        # Build the dashboard app
        if self._multi_project:
            dashboard_app = create_dashboard_app(project_dir=None, multi_project=True)
        else:
            dashboard_app = create_dashboard_app(project_dir=self._project_dir, multi_project=False)

        # Replace lifespan to add scheduler startup/shutdown
        dashboard_app.router.lifespan_context = self._create_combined_lifespan(dashboard_app.router.lifespan_context)

        # Add scheduler endpoints
        self._add_scheduler_routes(dashboard_app)
        self._add_health_route(dashboard_app)

        return dashboard_app

    def _create_combined_lifespan(self, original_lifespan):
        """Create a combined lifespan context manager for scheduler and dashboard."""
        server = self

        @asynccontextmanager
        async def combined_lifespan(app: FastAPI) -> AsyncIterator[None]:
            async with original_lifespan(app):
                if server._config.server.scheduler_enabled and not server._multi_project:
                    server._start_scheduler()
                try:
                    yield
                finally:
                    await server._stop_scheduler()

        return combined_lifespan

    def _add_scheduler_routes(self, dashboard_app: FastAPI) -> None:
        """Add scheduler API routes to the dashboard app."""
        from fastapi import APIRouter

        scheduler_router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])

        @scheduler_router.get("/status")
        async def scheduler_status() -> dict:
            return self._get_scheduler_status()

        @scheduler_router.get("/health")
        async def scheduler_health() -> dict:
            return await self._get_scheduler_health()

        @scheduler_router.get("/digest")
        async def scheduler_digest(hours: int = 24) -> dict:
            return await self._get_scheduler_digest(hours)

        dashboard_app.include_router(scheduler_router)

    def _add_health_route(self, dashboard_app: FastAPI) -> None:
        """Add general health check route to the dashboard app."""

        @dashboard_app.get("/api/health")
        async def health_check() -> dict:
            return self._get_health_check()

    def _get_scheduler_status(self) -> dict:
        """Get basic scheduler status."""
        return {
            "running": self._running,
            "active_tasks": 0,
            "scheduler_enabled": self._config.server.scheduler_enabled,
            "watch_interval": self._config.watch.interval_active,
            "idle_interval": self._config.watch.interval_idle,
            "max_parallel": self._config.max_parallel_agents,
        }

    async def _get_scheduler_health(self) -> dict:
        """Get detailed scheduler health metrics."""
        uptime = time.monotonic() - self._start_time
        agents_active = self._count_active_agents()

        scheduler_health_data = self._get_watch_loop_health()
        db_connected = await self._check_db_connection()

        return {
            "status": "healthy" if db_connected else "degraded",
            "uptime_seconds": round(uptime, 1),
            "scheduler": scheduler_health_data,
            "agents": {
                "running": agents_active,
                "max_parallel": self._config.max_parallel_agents,
            },
            "db": {
                "connected": db_connected,
            },
        }

    def _get_watch_loop_health(self) -> dict:
        """Get watch loop health metrics."""
        health_data = {
            "running": self._running,
            "last_scan_at": None,
            "scans_total": 0,
            "errors_total": 0,
        }
        if self._watch_loop:
            watch_health = self._watch_loop.health()
            health_data.update(
                {
                    "last_scan_at": watch_health.get("last_scan_at"),
                    "scans_total": watch_health.get("scans_total", 0),
                    "errors_total": watch_health.get("errors_total", 0),
                }
            )
        return health_data

    async def _check_db_connection(self) -> bool:
        """Check if database is connected and responsive."""
        try:
            from sqlalchemy import text

            from sova.db.session import get_session

            session = await get_session(self._project_dir)
            try:
                await session.execute(text("SELECT 1"))
                return True
            finally:
                await session.close()
        except Exception:
            return False

    def _count_active_agents(self) -> int:
        """Count currently active agents across all projects."""
        try:
            from sova.dashboard.services.control_service import _projects

            return sum(len(pa.agents) for pa in _projects.values())
        except Exception:
            return 0

    async def _get_scheduler_digest(self, hours: int) -> dict:
        """Get task completion summary and cost for the specified period."""
        from datetime import UTC, datetime, timedelta

        from sova.db.session import get_session

        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        session = await get_session(self._project_dir)
        try:
            results = await self._query_digest_stats(session, cutoff)
            completed, failed, in_progress, total_cost = results
        finally:
            await session.close()

        cost_formatted = round(total_cost, 2)
        summary = f"{completed} tasks completed, {failed} failed, ${cost_formatted} spent in last {hours}h"

        return {
            "period_hours": hours,
            "tasks": {
                "completed": completed,
                "failed": failed,
                "in_progress": in_progress,
            },
            "cost_usd": cost_formatted,
            "summary": summary,
        }

    async def _query_digest_stats(self, session, cutoff):
        """Query task and cost statistics for digest."""
        return await query_digest_stats(session, cutoff)

    def _get_health_check(self) -> dict:
        """Get general health check status."""
        uptime = time.monotonic() - self._start_time
        agents_active = self._count_active_agents()
        return {
            "status": "ok",
            "uptime_s": round(uptime, 1),
            "scheduler_running": self._running,
            "agents_active": agents_active,
        }

    def _start_scheduler(self) -> None:
        """Start the watch loop as a background asyncio task."""
        if self._running:
            return

        self._running = True
        self._watch_task = asyncio.create_task(self._run_watch_loop())
        log.info("scheduler.started")

    async def _run_watch_loop(self) -> None:
        """Run the watch loop, creating adapter on demand."""
        from sova.adapters import create_adapter
        from sova.scheduler.parallel import ParallelExecutor
        from sova.scheduler.watch import WatchLoop

        adapter = create_adapter(self._config)
        executor = ParallelExecutor(
            config=self._config,
            project_dir=self._project_dir,
        )
        self._watch_loop = WatchLoop(
            config=self._config,
            adapter=adapter,
            executor=executor,
            project_dir=str(self._project_dir) if self._project_dir else None,
        )

        try:
            await self._watch_loop.run()
        except asyncio.CancelledError:
            self._watch_loop.stop()
            raise
        except Exception:
            log.error("scheduler.crash", exc_info=True)
        finally:
            self._running = False
            self._watch_loop = None

    async def _stop_scheduler(self) -> None:
        """Stop the watch loop background task."""
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
            await asyncio.gather(self._watch_task, return_exceptions=True)
        self._running = False
        log.info("scheduler.stopped")

    async def stop(self) -> None:
        """Public stop method for external callers."""
        await self._stop_scheduler()

    def run(self) -> None:
        """Start the combined server (blocking). Used by ``sova server start``."""
        import uvicorn

        app = self.create_app()
        self._write_pid_file()

        try:
            uvicorn.run(app, host=self.host, port=self.port, log_level="info")
        finally:
            self._remove_pid_file()

    def _pid_file_path(self) -> Path:
        """Resolve the PID file path."""
        if self._config.server.pid_file:
            return Path(self._config.server.pid_file)
        return _resolve_default_pid_path(self._project_dir)

    def _write_pid_file(self) -> None:
        """Write the current PID to the PID file."""
        pid_path = self._pid_file_path()
        pid_path.write_text(str(os.getpid()))
        log.info("pid.written", path=str(pid_path), pid=os.getpid())

    def _remove_pid_file(self) -> None:
        """Remove the PID file."""
        pid_path = self._pid_file_path()
        try:
            pid_path.unlink(missing_ok=True)
        except OSError:
            log.warning("pid.remove_failed", path=str(pid_path), exc_info=True)


def _resolve_default_pid_path(project_dir: Path | None = None) -> Path:
    """Derive the default PID file path, scoped to project_dir when available."""
    if project_dir is not None:
        claude_dir = project_dir / ".claude"
        if claude_dir.is_dir():
            return claude_dir / "sova-server.pid"
    _DEFAULT_PID_DIR.mkdir(parents=True, exist_ok=True)
    return _DEFAULT_PID_DIR / "sova-server.pid"


def read_pid_file(
    config: ProjectConfig | None = None,
    *,
    project_dir: Path | None = None,
) -> int | None:
    """Read the server PID from the PID file.

    Returns the PID if the file exists and the process is alive, else None.
    """
    if config and config.server.pid_file:
        pid_path = Path(config.server.pid_file)
    else:
        pid_path = _resolve_default_pid_path(project_dir)

    if not pid_path.exists():
        return None

    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return None

    # Check if process is alive
    try:
        os.kill(pid, 0)
        return pid
    except OSError:
        # Process is dead, clean up stale PID file
        try:
            pid_path.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def stop_server(
    config: ProjectConfig | None = None,
    *,
    project_dir: Path | None = None,
) -> bool:
    """Send SIGTERM to the running server process.

    Returns True if a signal was sent, False if no server was running.
    """
    pid = read_pid_file(config, project_dir=project_dir)
    if pid is None:
        return False

    try:
        os.kill(pid, signal.SIGTERM)
        log.info("server.stopped", pid=pid)
        return True
    except OSError:
        log.warning("server.stop_failed", pid=pid, exc_info=True)
        return False
