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
from sova.utils.logging import get_logger

log = get_logger(component="scheduler.server")

# PID file location for server status/stop commands
_DEFAULT_PID_DIR = Path.home() / ".config" / "sova"


class SOVAServer:
    """Combined dashboard + scheduler daemon.

    ``sova server start`` creates an instance and calls ``run()``,
    which starts the FastAPI dashboard and the watch loop together.
    """

    def __init__(
        self,
        *,
        config: ProjectConfig,
        project_dir: Path | None = None,
        host: str = "127.0.0.1",
        port: int = 8111,
    ) -> None:
        self._config = config
        self._project_dir = project_dir
        self.host = host
        self.port = port
        self._running = False
        self._watch_task: asyncio.Task[None] | None = None
        self._start_time = time.monotonic()

    @property
    def is_running(self) -> bool:
        return self._running

    def create_app(self) -> FastAPI:
        """Create the FastAPI app with scheduler lifecycle hooks and status endpoint."""
        from sova.dashboard.app import create_app as create_dashboard_app

        # Build the dashboard app
        dashboard_app = create_dashboard_app(project_dir=self._project_dir)

        # Replace lifespan to add scheduler startup/shutdown
        original_lifespan = dashboard_app.router.lifespan_context

        server = self

        @asynccontextmanager
        async def combined_lifespan(app: FastAPI) -> AsyncIterator[None]:
            # Run original dashboard lifespan (DB init)
            async with original_lifespan(app):
                # Start scheduler if enabled
                if server._config.server.scheduler_enabled:
                    server._start_scheduler()
                try:
                    yield
                finally:
                    await server._stop_scheduler()

        dashboard_app.router.lifespan_context = combined_lifespan

        # Add scheduler status endpoint
        from fastapi import APIRouter

        scheduler_router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])

        @scheduler_router.get("/status")
        async def scheduler_status() -> dict:
            return {
                "running": server._running,
                "active_tasks": 0,
                "scheduler_enabled": server._config.server.scheduler_enabled,
                "watch_interval": server._config.watch.interval_active,
                "idle_interval": server._config.watch.interval_idle,
                "max_parallel": server._config.max_parallel_agents,
            }

        dashboard_app.include_router(scheduler_router)

        @dashboard_app.get("/api/health")
        async def health_check() -> dict:
            uptime = time.monotonic() - server._start_time
            agents_active = 0
            try:
                from sova.dashboard.services.control_service import _projects

                for pa in _projects.values():
                    agents_active += len(pa.agents)
            except Exception:
                pass
            return {
                "status": "ok",
                "uptime_s": round(uptime, 1),
                "scheduler_running": server._running,
                "agents_active": agents_active,
            }

        return dashboard_app

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

        adapter = create_adapter(
            self._config.task_source.type,
            self._config.github_repo,
            self._config.github_user,
            self._config.task_source.github_project_number,
        )
        executor = ParallelExecutor(
            config=self._config,
            project_dir=self._project_dir,
        )
        watch = WatchLoop(
            config=self._config,
            adapter=adapter,
            executor=executor,
            project_dir=str(self._project_dir) if self._project_dir else None,
        )

        try:
            await watch.run()
        except asyncio.CancelledError:
            watch.stop()
        except Exception:
            log.error("scheduler.crash", exc_info=True)
        finally:
            self._running = False

    async def _stop_scheduler(self) -> None:
        """Stop the watch loop background task."""
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
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
        _DEFAULT_PID_DIR.mkdir(parents=True, exist_ok=True)
        return _DEFAULT_PID_DIR / "sova-server.pid"

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


def read_pid_file(config: ProjectConfig | None = None) -> int | None:
    """Read the server PID from the PID file.

    Returns the PID if the file exists and the process is alive, else None.
    """
    if config and config.server.pid_file:
        pid_path = Path(config.server.pid_file)
    else:
        pid_path = _DEFAULT_PID_DIR / "sova-server.pid"

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


def stop_server(config: ProjectConfig | None = None) -> bool:
    """Send SIGTERM to the running server process.

    Returns True if a signal was sent, False if no server was running.
    """
    pid = read_pid_file(config)
    if pid is None:
        return False

    try:
        os.kill(pid, signal.SIGTERM)
        log.info("server.stopped", pid=pid)
        return True
    except OSError:
        log.warning("server.stop_failed", pid=pid, exc_info=True)
        return False
