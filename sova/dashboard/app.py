"""SOVA Dashboard -- FastAPI app factory.

Supports two modes:
- Single-project: `create_app(project_dir=Path(...))` -- current behavior
- Multi-project: `create_app(multi_project=True)` -- uses project registry
- Auto-detect: `create_app()` -- multi if registry has projects, else single (CWD)
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from sova.config.loader import ProjectConfig
    from sova.monitoring.cross_project import MetricsSnapshotWriter
    from sova.oversight.agent import OversightAgent
    from sova.supervisor.daemon import SupervisorDaemon
    from sova.supervisor.watchdog import AgentWatchdog

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from sova.config.registry import has_projects, list_projects
from sova.dashboard.routers import (
    agents,
    control,
    costs,
    dependencies,
    failure_analysis,
    feed,
    fleet_insights,
    handoff,
    lifecycle,
    logs,
    memory,
    oversight,
    overview,
    prs,
    queue,
    quota,
    resources,
    roles,
    runs,
    settings,
    setup,
    spec,
    supervisor,
    tasks,
    work,
)
from sova.dashboard.services import control_service, handoff_service
from sova.dashboard.services.control_service import recover_stale_runs
from sova.dashboard.services.work_service import _TERMINAL
from sova.db.session import close_db, init_db, init_db_for_project
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.app")


class RegisterRequest(BaseModel):
    path: str
    slug: str | None = None


class UnregisterRequest(BaseModel):
    slug: str


class UninstallRequest(BaseModel):
    slug: str
    remove_files: bool = True
    remove_commands: bool = False
    remove_rules: bool = False
    remove_memory: bool = False
    remove_config: bool = False


BASE = Path(__file__).parent

_AGENTS_URL = "/agents"
_SWEEP_INTERVAL = 5  # seconds
_RECOVERY_INTERVAL = 300  # 5 minutes


def _try_load_config(project_path: Path) -> ProjectConfig | None:
    """Load config for a project, returning None on failure."""
    from sova.config.loader import load_config

    try:
        return load_config(project_path)
    except Exception:
        log.warning("supervisor.config_load_failed", project=str(project_path), exc_info=True)
        return None


def _collect_supervisor_configs(project_dirs: dict[str, str]) -> list[tuple[Path, ProjectConfig]]:
    """Load configs for registered projects, skipping broken or non-supervisor ones."""
    result: list[tuple[Path, ProjectConfig]] = []
    for path_str in project_dirs.values():
        p = Path(path_str)
        if not p.is_dir():
            continue
        cfg = _try_load_config(p)
        if cfg is None:
            continue
        if not cfg.supervisor.enabled:
            continue
        result.append((p, cfg))
    return result


def _collect_sweep_dirs(project_dir: Path | None, *, is_multi: bool) -> list[Path]:
    """Build the list of project directories to sweep."""
    dirs: list[Path] = []
    if is_multi:
        for path_str in list_projects().values():
            p = Path(path_str)
            if p.is_dir():
                dirs.append(p)
    else:
        dirs.append((project_dir or Path.cwd()).resolve())
    return dirs


class _DeadRunRecord(TypedDict):
    run_id: int
    pr_number: int | None
    pid: int
    final_status: str
    error_msg: str
    needs_merge_check: bool


async def _liveness_sweep_once(project_dir: Path | None, *, is_multi: bool) -> None:
    """Single pass: check for dead agent processes and mark their TaskRuns.

    Three-phase approach to prevent GitHub API calls from blocking DB write locks:

      1. Read-only query per directory (no write lock).
      2. Concurrent GitHub merge checks outside any session, bounded by a total timeout.
         Mirrors the pattern used in recover_stale_runs() in agent_recovery.py.
      3. Write transaction per directory with terminal-status re-check to handle
         concurrent finalizations (e.g. _wait_and_finalize completing between phases).
    """
    from datetime import datetime, timezone

    from sqlalchemy import select

    from sova.dashboard.services.agent_lifecycle import _MERGE_ROLES, _check_pr_merged_on_failure
    from sova.dashboard.services.agent_recovery import _MERGE_CHECK_TIMEOUT, _RECOVERY_TOTAL_TIMEOUT
    from sova.dashboard.services.control_service import _is_process_alive, _projects
    from sova.db.models import TaskRun
    from sova.db.session import get_session

    managed_run_ids_by_dir: dict[Path, set[int]] = {}
    for pa in _projects.values():
        managed_run_ids_by_dir.setdefault(pa.project_dir.resolve(), set()).update(pa.agents.keys())

    for d in _collect_sweep_dirs(project_dir, is_multi=is_multi):
        managed = managed_run_ids_by_dir.get(d, set())

        # Phase 1: read-only query, no write lock held.
        async with await get_session(project_dir=d) as session:
            stmt = select(TaskRun).where(
                TaskRun.status.notin_(_TERMINAL),
                TaskRun.pid.isnot(None),
            )
            result = await session.execute(stmt)
            runs = result.scalars().all()

        dead_runs: list[_DeadRunRecord] = []
        for run in runs:
            if run.id in managed or _is_process_alive(run.pid):
                continue
            cmd_parts = (run.role or "").removeprefix("command:").removeprefix("/").split()
            needs_merge_check = run.pr_number is not None and bool(cmd_parts) and cmd_parts[0] in _MERGE_ROLES
            dead_runs.append(
                {
                    "run_id": run.id,
                    "pr_number": run.pr_number,
                    "pid": run.pid,
                    "final_status": "interrupted",
                    "error_msg": "Agent process died unexpectedly",
                    "needs_merge_check": needs_merge_check,
                }
            )

        if not dead_runs:
            continue

        # Phase 2: concurrent GitHub merge checks, outside any session.
        # _d is bound via default argument so each closure captures the correct
        # directory even if the loop advances before the gather completes.
        merge_candidates = [r for r in dead_runs if r["needs_merge_check"]]
        if merge_candidates:

            async def _check_one(rec: _DeadRunRecord, _d: Path = d) -> tuple[int, bool]:
                try:
                    merged = await asyncio.wait_for(
                        _check_pr_merged_on_failure(rec["pr_number"], _d),
                        timeout=_MERGE_CHECK_TIMEOUT,
                    )
                    return rec["run_id"], merged
                except Exception:
                    log.debug("sweep.merge_check_skipped", run_id=rec["run_id"], exc_info=True)
                    return rec["run_id"], False

            dead_by_id = {r["run_id"]: r for r in dead_runs}
            try:
                merge_results = await asyncio.wait_for(
                    asyncio.gather(*(_check_one(r) for r in merge_candidates)),
                    timeout=_RECOVERY_TOTAL_TIMEOUT,
                )
                for rid, merged in merge_results:
                    if merged:
                        rec = dead_by_id[rid]
                        rec["final_status"] = "done"
                        rec["error_msg"] = f"Agent process died but PR #{rec['pr_number']} was merged successfully"
                        log.info("sweep.merged_despite_crash", run_id=rid, pr=rec["pr_number"])
            except asyncio.TimeoutError:
                log.warning("sweep.merge_checks_timed_out", total_timeout=_RECOVERY_TOTAL_TIMEOUT, exc_info=True)

        # Phase 3: single write transaction with terminal-status re-check.
        # Re-reading inside the transaction ensures we see any status change
        # committed by _wait_and_finalize between Phase 1 and now.
        now = datetime.now(timezone.utc)
        async with await get_session(project_dir=d) as session:
            async with session.begin():
                stmt = select(TaskRun).where(TaskRun.id.in_([r["run_id"] for r in dead_runs]))
                result = await session.execute(stmt)
                runs_by_id = {r.id: r for r in result.scalars().all()}

                for rec in dead_runs:
                    run = runs_by_id.get(rec["run_id"])
                    if run is None:
                        continue
                    if run.status in _TERMINAL:
                        continue  # finalized by concurrent writer between Phase 1 and Phase 3
                    run.status = rec["final_status"]
                    run.error_message = rec["error_msg"]
                    run.ended_at = now


async def _liveness_sweep_loop(project_dir: Path | None, is_multi: bool) -> None:
    """Periodically check for dead agent processes and mark their TaskRuns."""
    while True:
        await asyncio.sleep(_SWEEP_INTERVAL)
        try:
            await _liveness_sweep_once(project_dir, is_multi=is_multi)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("sweep.error", exc_info=True)


async def _periodic_recovery_loop(project_dir: Path | None, is_multi: bool) -> None:
    """Run recover_stale_runs periodically (every 5 minutes) to catch zombie runs.

    Complements the fast liveness sweep (5s) which only handles the simple dead-PID
    case. recover_stale_runs additionally checks handoff files and PR merge status
    to decide between "done" and "interrupted" outcomes.
    """
    while True:
        await asyncio.sleep(_RECOVERY_INTERVAL)
        try:
            for d in _collect_sweep_dirs(project_dir, is_multi=is_multi):
                await recover_stale_runs(d)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("periodic_recovery.error", exc_info=True)


async def _startup_gc(project_dir: Path) -> None:
    """Run issue-aware GC in the background after startup."""
    try:
        from sova.git.worktree import cleanup_by_issue_state

        gc = await cleanup_by_issue_state(project_dir=project_dir)
        if gc.worktrees_removed or gc.branches_removed:
            log.info(
                "lifespan.gc_complete",
                worktrees=gc.worktrees_removed,
                branches=gc.branches_removed,
                stashes=len(gc.stashes_found),
            )
    except Exception:
        log.warning("lifespan.gc_failed", exc_info=True)


async def _shutdown_tasks(
    sweep_task: asyncio.Task,
    pr_throttle_tasks: list[asyncio.Task],
    pr_monitor_tasks: list[asyncio.Task],
    metrics_writer: MetricsSnapshotWriter | None,
    watchdog: AgentWatchdog | None = None,
    recovery_task: asyncio.Task | None = None,
    oversight_agent: OversightAgent | None = None,
    supervisor_daemons: list[SupervisorDaemon] | None = None,
    gc_task: asyncio.Task | None = None,
    merge_queue_tasks: list[asyncio.Task] | None = None,
) -> None:
    """Cancel all background tasks during lifespan shutdown."""
    from sova.dashboard.routers.agents import _ws_manager
    from sova.dashboard.routers.oversight import cancel_run_now_tasks
    from sova.dashboard.services.agent_lifecycle import cancel_background_tasks
    from sova.dashboard.services.batch_service import cancel_all_batches

    await cancel_background_tasks()
    await _ws_manager.cancel_all()
    await cancel_all_batches()
    await cancel_run_now_tasks()
    if metrics_writer is not None:
        await metrics_writer.stop()
    if oversight_agent is not None:
        await oversight_agent.stop()
    if watchdog is not None:
        await watchdog.stop()
    for daemon in supervisor_daemons or []:
        await daemon.stop()
    bg_tasks = pr_throttle_tasks + pr_monitor_tasks + (merge_queue_tasks or [])
    for t in bg_tasks:
        t.cancel()
    if bg_tasks:
        await asyncio.gather(*bg_tasks, return_exceptions=True)
    cancel_tasks = [sweep_task]
    if recovery_task is not None:
        cancel_tasks.append(recovery_task)
    if gc_task is not None:
        cancel_tasks.append(gc_task)
    for t in cancel_tasks:
        t.cancel()
    await asyncio.gather(*cancel_tasks, return_exceptions=True)


def create_app(
    *,
    project_dir: Path | None = None,
    multi_project: bool | None = None,
) -> FastAPI:
    """Create and configure the SOVA dashboard FastAPI app.

    Args:
        project_dir: Explicit project directory (forces single-project mode).
        multi_project: Force multi-project mode. None = auto-detect.
    """
    if project_dir is None:
        env_project = os.environ.get("SOVA_DASHBOARD_PROJECT")
        if env_project:
            project_dir = Path(env_project)

    # Auto-detect mode
    if multi_project is None:
        if project_dir is not None:
            multi_project = False
        else:
            multi_project = has_projects()

    is_multi = multi_project

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        from sova.utils.logging import setup_logging

        resolved = (project_dir or Path.cwd()).resolve()
        log_file = resolved / ".claude" / "sova.log"
        setup_logging(log_file=log_file)

        # Initialize the global LLM provider from project config.
        # NOTE: in multi_project mode this uses the first resolved dir's config.
        # Per-project provider selection requires threading through ExecutionContext.
        from sova.config.loader import load_config
        from sova.ipc.runtime import create_runtime, set_runtime
        from sova.llm.client import set_provider
        from sova.llm.provider import create_provider

        cfg = load_config(resolved)
        set_provider(
            create_provider(
                cfg.llm.provider,
                model=cfg.llm.model,
                fallback_model=cfg.llm.fallback_model,
                api_base=cfg.llm.api_base,
            )
        )
        set_runtime(create_runtime(cfg.agent.runtime))

        from sova.core.output import cleanup_old_output
        from sova.dashboard.services.agent_recovery import _kill_terminal_zombies

        gc_task: asyncio.Task | None = None
        if is_multi:
            log.warning("multi_project.shared_runtime", runtime=cfg.agent.runtime)

            for path_str in list_projects().values():
                p = Path(path_str)
                if p.is_dir():
                    await init_db_for_project(p)
                    await recover_stale_runs(p)
                    await _kill_terminal_zombies(p)
                    await cleanup_old_output(p, cfg.output.retention_days)
        else:
            await init_db(resolved)
            await recover_stale_runs(resolved)
            await _kill_terminal_zombies(resolved)
            await cleanup_old_output(resolved, cfg.output.retention_days)

            if cfg.dashboard.gc_on_startup:
                gc_task = asyncio.create_task(_startup_gc(resolved))

        # Recover stale PR queue entries and start background processor
        pr_throttle_tasks: list[asyncio.Task] = []
        if cfg.coderabbit_quota.enabled:
            from sova.db.session import get_session
            from sova.supervisor.pr_throttle import process_queue_loop, recover_creating_entries

            if is_multi:
                from sova.config.loader import load_config as _load_cfg

                for path_str in list_projects().values():
                    p = Path(path_str)
                    if not p.is_dir():
                        continue
                    pcfg = _load_cfg(p)
                    if not pcfg.coderabbit_quota.enabled:
                        continue

                    async with await get_session(project_dir=p) as session:
                        async with session.begin():
                            await recover_creating_entries(session)

                    def _make_factory(proj_dir: Path) -> Callable[[], Awaitable[AsyncSession]]:
                        async def _factory() -> AsyncSession:
                            return await get_session(project_dir=proj_dir)

                        return _factory

                    pr_throttle_tasks.append(
                        asyncio.create_task(
                            process_queue_loop(
                                _make_factory(p),
                                pcfg.coderabbit_quota,
                                project_slug=pcfg.github_repo,
                                project_dir=p,
                            )
                        )
                    )
            else:
                async with await get_session(project_dir=resolved) as session:
                    async with session.begin():
                        await recover_creating_entries(session)

                async def _pr_session_factory() -> AsyncSession:
                    return await get_session(project_dir=resolved)

                pr_throttle_tasks.append(
                    asyncio.create_task(
                        process_queue_loop(
                            _pr_session_factory,
                            cfg.coderabbit_quota,
                            project_slug=cfg.github_repo,
                            project_dir=resolved,
                        )
                    )
                )

        sweep_task = asyncio.create_task(_liveness_sweep_loop(project_dir, is_multi))
        recovery_task = asyncio.create_task(_periodic_recovery_loop(project_dir, is_multi))

        # PR monitor background loop
        pr_monitor_tasks: list[asyncio.Task] = []
        if is_multi:
            from sova.supervisor.pr_monitor import create_monitors_for_projects

            for monitor in create_monitors_for_projects():
                pr_monitor_tasks.append(asyncio.create_task(monitor.run_loop()))
        elif cfg.pr_monitor.enabled and cfg.github_repo:
            from sova.supervisor.pr_monitor import PRMonitor

            monitor = PRMonitor(
                project_dir=resolved,
                monitor_config=cfg.pr_monitor,
                notification_config=cfg.notification,
                repo=cfg.github_repo,
                github_user=cfg.github_user,
            )
            pr_monitor_tasks.append(asyncio.create_task(monitor.run_loop()))

        # Merge queue monitor background loop
        merge_queue_tasks: list[asyncio.Task] = []
        if is_multi:
            from sova.dashboard.services.merge_queue_monitor import create_monitors_for_merge_queue

            for mq_monitor in create_monitors_for_merge_queue():
                merge_queue_tasks.append(asyncio.create_task(mq_monitor.run_loop()))
        elif cfg.integration.merge_queue_enabled != "false" and cfg.github_repo:
            from sova.dashboard.services.merge_queue_monitor import MergeQueueMonitor

            mq_monitor = MergeQueueMonitor(
                project_dir=resolved,
                repo=cfg.github_repo,
                github_user=cfg.github_user,
                integration_config=cfg.integration,
                notification_config=cfg.notification,
            )
            merge_queue_tasks.append(asyncio.create_task(mq_monitor.run_loop()))

        # Start agent watchdog
        from sova.supervisor.watchdog import AgentWatchdog as _AgentWatchdog

        watchdog: AgentWatchdog | None = None
        if cfg.watchdog.enabled and not is_multi:
            watchdog = _AgentWatchdog(config=cfg.watchdog, project_dir=resolved)
            watchdog.start()

        # Start cross-project metrics snapshot writer
        from sova.monitoring.cross_project import MetricsSnapshotWriter

        metrics_writer: MetricsSnapshotWriter | None = None
        if not is_multi:
            project_name = cfg.github_repo or resolved.name
            metrics_writer = MetricsSnapshotWriter(
                project_dir=resolved,
                project_name=project_name,
                dashboard_port=cfg.dashboard.port,
            )
            metrics_writer.start()

        # Start oversight agent
        from sova.dashboard.routers.oversight import set_oversight_agent
        from sova.oversight.agent import OversightAgent as _OversightAgent
        from sova.oversight.persona import ensure_persona_exists

        ensure_persona_exists(cfg.oversight.persona_path)

        oversight_agent: OversightAgent | None = None
        if cfg.oversight.enabled:
            oversight_agent = _OversightAgent(config=cfg.oversight)
            oversight_agent.start()
        set_oversight_agent(oversight_agent)

        # Start supervisor daemons
        from sova.dashboard.routers.supervisor import set_daemon_registry
        from sova.supervisor.daemon import SupervisorDaemon as _SupervisorDaemon

        supervisor_daemons: list[SupervisorDaemon] = []
        daemon_registry: dict[str, _SupervisorDaemon] = {}
        if is_multi:
            from sova.db.session import get_session_factory

            for p, sv_cfg in _collect_supervisor_configs(dict(list_projects())):
                sf = await get_session_factory(p)
                daemon = _SupervisorDaemon(config=sv_cfg, project_dir=p, session_factory=sf)
                daemon.start()
                supervisor_daemons.append(daemon)
                daemon_registry[str(p.resolve())] = daemon
        elif cfg.supervisor.enabled:
            from sova.db.session import get_session_factory

            sf = await get_session_factory(resolved)
            daemon = _SupervisorDaemon(config=cfg, project_dir=resolved, session_factory=sf)
            daemon.start()
            supervisor_daemons.append(daemon)
            daemon_registry[str(resolved)] = daemon

        set_daemon_registry(daemon_registry)

        try:
            yield
        finally:
            try:
                await asyncio.wait_for(
                    _shutdown_tasks(
                        sweep_task,
                        pr_throttle_tasks,
                        pr_monitor_tasks,
                        metrics_writer,
                        watchdog,
                        recovery_task=recovery_task,
                        oversight_agent=oversight_agent,
                        supervisor_daemons=supervisor_daemons,
                        gc_task=gc_task,
                        merge_queue_tasks=merge_queue_tasks,
                    ),
                    timeout=5.0,
                )
            except TimeoutError:
                log.warning("lifespan.shutdown_timeout", exc_info=True)
            await close_db()

    app = FastAPI(title="SOVA Dashboard", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness probe. Returns 200 OK if the app is running."""
        return {"status": "ok"}

    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

    @app.middleware("http")
    async def static_cache_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/static/") and response.status_code < 400:
            response.headers["Cache-Control"] = "public, max-age=3600"
        return response

    templates = Jinja2Templates(directory=BASE / "templates")

    if is_multi:
        _setup_multi_project(app, templates)
    else:
        _setup_single_project(app, templates, project_dir)

    return app


def _setup_single_project(
    app: FastAPI,
    templates: Jinja2Templates,
    project_dir: Path | None,
) -> None:
    """Configure routes for single-project mode (backward compatible)."""
    resolved = (project_dir or Path.cwd()).resolve()
    control_service.set_project_dir(resolved)
    handoff_service.set_project_dir(resolved)

    # -- Page routes --
    @app.get("/")
    async def home(request: Request) -> RedirectResponse:
        return RedirectResponse(url="/dashboard")

    _register_page_routes(app, templates)
    _register_api_routers(app, prefix="/api")
    _register_telemetry_router(app)
    _register_a2a_routes(app)


def _setup_multi_project(app: FastAPI, templates: Jinja2Templates) -> None:
    """Configure routes for multi-project mode."""
    from sova.config.registry import register_project, unregister_project
    from sova.dashboard.middleware import ProjectContextMiddleware
    from sova.dashboard.routers import fleet_manager

    app.add_middleware(ProjectContextMiddleware)

    # -- Fleet Manager API (global, not project-scoped) --
    app.include_router(fleet_manager.router, prefix="/api")

    # -- Home page (Fleet Manager command center) --
    @app.get("/")
    async def home(request: Request) -> Response:
        projects = list_projects()
        return templates.TemplateResponse(request, "home.html", {"projects": projects})

    # -- Project API --
    @app.get("/api/projects")
    async def api_list_projects() -> dict:
        return {"projects": list_projects()}

    @app.post("/api/projects/register")
    async def api_register_project(req: RegisterRequest) -> dict:
        p = Path(req.path)
        result_slug = register_project(p, req.slug)
        await init_db_for_project(p)
        return {"slug": result_slug, "path": str(p)}

    @app.post("/api/projects/unregister")
    async def api_unregister_project(req: UnregisterRequest) -> dict:
        removed = unregister_project(req.slug)
        return {"removed": removed}

    @app.post("/api/projects/uninstall")
    async def api_uninstall_project(req: UninstallRequest) -> dict[str, bool]:
        from sova.config.registry import get_project_path

        slug = req.slug.lower()
        project_path = get_project_path(slug)
        if project_path is None:
            removed = unregister_project(slug)
            return {"removed": removed, "files_cleaned": False}

        if req.remove_files:
            from sova.cli.commands.project import _uninstall

            try:
                failures = await _uninstall(
                    path=project_path,
                    remove_commands=req.remove_commands,
                    remove_rules=req.remove_rules,
                    remove_memory=req.remove_memory,
                    remove_config=req.remove_config,
                )
            except SystemExit:
                removed = unregister_project(slug)
                return {"removed": removed, "files_cleaned": False}
            except Exception:
                log.exception("Uninstall failed for %s", slug)
                raise HTTPException(status_code=500, detail="Failed to uninstall project") from None
            return {"removed": True, "files_cleaned": len(failures) == 0}

        removed = unregister_project(slug)
        return {"removed": removed, "files_cleaned": False}

    # -- Setup page (global, not project-scoped) --
    @app.get("/setup")
    async def setup_page(request: Request) -> Response:
        return templates.TemplateResponse(request, "setup.html", {"page": "setup"})

    # -- Project-scoped page routes (new) --
    @app.get("/p/{slug}")
    async def project_redirect(slug: str) -> RedirectResponse:
        return RedirectResponse(url=f"/p/{slug}/dashboard")

    @app.get("/p/{slug}/dashboard")
    async def project_dashboard(request: Request, slug: str) -> Response:
        return _project_page(request, templates, slug, "dashboard.html", "dashboard")

    @app.get("/p/{slug}/agents")
    async def project_agents(request: Request, slug: str) -> Response:
        return _project_page(request, templates, slug, "agents.html", "agents")

    @app.get("/p/{slug}/work")
    async def project_work_redirect(slug: str) -> RedirectResponse:
        return RedirectResponse(url=f"/p/{slug}/agents", status_code=301)

    @app.get("/p/{slug}/work/{run_id}")
    async def project_work_detail(request: Request, slug: str, run_id: int) -> Response:
        return _project_page(request, templates, slug, "run_detail.html", "agents", run_id=run_id)

    # -- Old routes as redirects --
    @app.get("/p/{slug}/overview")
    async def project_overview_redirect(slug: str) -> RedirectResponse:
        return RedirectResponse(url=f"/p/{slug}/dashboard", status_code=301)

    @app.get("/p/{slug}/control")
    async def project_control_redirect(slug: str) -> RedirectResponse:
        return RedirectResponse(url=f"/p/{slug}/agents", status_code=301)

    @app.get("/p/{slug}/tasks")
    async def project_tasks_redirect(slug: str) -> RedirectResponse:
        return RedirectResponse(url=f"/p/{slug}/agents", status_code=301)

    @app.get("/p/{slug}/runs")
    async def project_runs_redirect(slug: str) -> RedirectResponse:
        return RedirectResponse(url=f"/p/{slug}/agents", status_code=301)

    @app.get("/p/{slug}/runs/{run_id}")
    async def project_run_detail_redirect(slug: str, run_id: int) -> RedirectResponse:
        return RedirectResponse(url=f"/p/{slug}/work/{run_id}", status_code=301)

    # -- Unchanged project pages --
    @app.get("/p/{slug}/costs")
    async def project_costs(request: Request, slug: str) -> Response:
        return _project_page(request, templates, slug, "costs.html", "costs")

    @app.get("/p/{slug}/queue")
    async def project_queue(request: Request, slug: str) -> Response:
        return _project_page(request, templates, slug, "queue.html", "queue")

    @app.get("/p/{slug}/specs")
    async def project_specs(request: Request, slug: str) -> Response:
        return _project_page(request, templates, slug, "specs.html", "specs")

    @app.get("/p/{slug}/logs")
    async def project_logs(request: Request, slug: str) -> Response:
        return _project_page(request, templates, slug, "logs.html", "logs")

    @app.get("/p/{slug}/settings")
    async def project_settings(request: Request, slug: str) -> Response:
        return _project_page(request, templates, slug, "settings.html", "settings")

    @app.get("/p/{slug}/memory")
    async def project_memory(request: Request, slug: str) -> Response:
        return _project_page(request, templates, slug, "memory.html", "memory")

    @app.get("/p/{slug}/lifecycle/{issue_number}")
    async def project_lifecycle(request: Request, slug: str, issue_number: int) -> Response:
        return _project_page(request, templates, slug, "lifecycle.html", "agents", issue_number=issue_number)

    @app.get("/p/{slug}/roles")
    async def project_roles(request: Request, slug: str) -> Response:
        return _project_page(request, templates, slug, "roles.html", "roles")

    @app.get("/p/{slug}/roles/{name}")
    async def project_role_detail(request: Request, slug: str, name: str) -> Response:
        return _project_page(request, templates, slug, "role_editor.html", "roles", role_name=name)

    @app.get("/p/{slug}/spec/{issue_number}")
    async def project_spec(request: Request, slug: str, issue_number: str) -> Response:
        return _project_page(request, templates, slug, "spec.html", "agents", issue_number=issue_number)

    @app.get("/p/{slug}/supervisor")
    async def project_supervisor(request: Request, slug: str) -> Response:
        from sova.config.registry import get_project_path

        github_repo = ""
        proj_path = get_project_path(slug)
        if proj_path:
            cfg = _try_load_config(proj_path)
            if cfg is not None:
                github_repo = cfg.github_repo or ""
        return _project_page(request, templates, slug, "supervisor.html", "supervisor", github_repo=github_repo)

    @app.get("/p/{slug}/fleet")
    async def project_fleet(request: Request, slug: str) -> Response:
        return _project_page(request, templates, slug, "fleet.html", "fleet")

    @app.get("/p/{slug}/oversight")
    async def project_oversight(request: Request, slug: str) -> Response:
        return _project_page(request, templates, slug, "oversight.html", "oversight")

    @app.get("/p/{slug}/pr-metrics")
    async def project_pr_metrics(request: Request, slug: str) -> Response:
        return _project_page(request, templates, slug, "pr_metrics.html", "pr-metrics")

    @app.get("/p/{slug}/style-guide")
    async def project_style_guide(request: Request, slug: str) -> Response:
        return _project_page(request, templates, slug, "style_guide.html", "style-guide")

    # -- Project-scoped API --
    _register_api_routers(app, prefix="/p/{slug}/api")

    # Also keep non-prefixed API for backward compat / fallback
    _register_api_routers(app, prefix="/api")
    _register_telemetry_router(app)
    _register_a2a_routes(app)


def _project_page(
    request: Request,
    templates: Jinja2Templates,
    slug: str,
    template: str,
    page: str,
    **extra: object,
) -> object:
    """Render a project-scoped page with context."""
    from sova.config.registry import get_project_path

    project_path = get_project_path(slug)
    ctx = {
        "page": page,
        "project_slug": slug,
        "project_name": project_path.name if project_path else slug,
        "prefix": f"/p/{slug}",
        **extra,
    }
    return templates.TemplateResponse(request, template, ctx)


def _register_page_routes(app: FastAPI, templates: Jinja2Templates) -> None:
    """Register non-prefixed page routes (single-project mode)."""

    # -- New pages --
    @app.get("/dashboard")
    async def dashboard_page(request: Request) -> Response:
        return templates.TemplateResponse(request, "dashboard.html", {"page": "dashboard"})

    @app.get("/agents")
    async def agents_page(request: Request) -> Response:
        return templates.TemplateResponse(request, "agents.html", {"page": "agents"})

    @app.get("/work")
    async def work_redirect() -> RedirectResponse:
        return RedirectResponse(url=_AGENTS_URL, status_code=301)

    @app.get("/work/{run_id}")
    async def work_detail_page(request: Request, run_id: int) -> Response:
        return templates.TemplateResponse(request, "run_detail.html", {"page": "agents", "run_id": run_id})

    # -- Old pages kept as redirects --
    @app.get("/overview")
    async def overview_redirect() -> RedirectResponse:
        return RedirectResponse(url="/dashboard", status_code=301)

    @app.get("/control")
    async def control_redirect() -> RedirectResponse:
        return RedirectResponse(url=_AGENTS_URL, status_code=301)

    @app.get("/tasks")
    async def tasks_redirect() -> RedirectResponse:
        return RedirectResponse(url=_AGENTS_URL, status_code=301)

    @app.get("/runs")
    async def runs_redirect() -> RedirectResponse:
        return RedirectResponse(url=_AGENTS_URL, status_code=301)

    @app.get("/runs/{run_id}")
    async def run_detail_redirect(run_id: int) -> RedirectResponse:
        return RedirectResponse(url=f"/work/{run_id}", status_code=301)

    # -- Unchanged pages --
    @app.get("/costs")
    async def costs_page(request: Request) -> Response:
        return templates.TemplateResponse(request, "costs.html", {"page": "costs"})

    @app.get("/pr-metrics")
    async def pr_metrics_page(request: Request) -> Response:
        return templates.TemplateResponse(request, "pr_metrics.html", {"page": "pr-metrics"})

    @app.get("/queue")
    async def queue_page(request: Request) -> Response:
        return templates.TemplateResponse(request, "queue.html", {"page": "queue"})

    @app.get("/specs")
    async def specs_page(request: Request) -> Response:
        return templates.TemplateResponse(request, "specs.html", {"page": "specs"})

    @app.get("/logs")
    async def logs_page(request: Request) -> Response:
        return templates.TemplateResponse(request, "logs.html", {"page": "logs"})

    @app.get("/settings")
    async def settings_page(request: Request) -> Response:
        return templates.TemplateResponse(request, "settings.html", {"page": "settings"})

    @app.get("/memory")
    async def memory_page(request: Request) -> Response:
        return templates.TemplateResponse(request, "memory.html", {"page": "memory"})

    @app.get("/lifecycle/{issue_number}")
    async def lifecycle_page(request: Request, issue_number: int) -> Response:
        return templates.TemplateResponse(request, "lifecycle.html", {"page": "agents", "issue_number": issue_number})

    @app.get("/roles")
    async def roles_page(request: Request) -> Response:
        return templates.TemplateResponse(request, "roles.html", {"page": "roles"})

    @app.get("/roles/{name}")
    async def role_detail_page(request: Request, name: str) -> Response:
        return templates.TemplateResponse(request, "role_editor.html", {"page": "roles", "role_name": name})

    @app.get("/spec/{issue_number}")
    async def spec_page(request: Request, issue_number: str) -> Response:
        return templates.TemplateResponse(request, "spec.html", {"page": "agents", "issue_number": issue_number})

    @app.get("/supervisor")
    async def supervisor_page(request: Request) -> Response:
        try:
            from sova.config.loader import load_config
            from sova.dashboard.project_context import get_project_dir

            sup_cfg = load_config(get_project_dir())
            github_repo = sup_cfg.github_repo or ""
        except Exception:
            log.debug("Failed to load github_repo for supervisor page", exc_info=True)
            github_repo = ""
        return templates.TemplateResponse(
            request, "supervisor.html", {"page": "supervisor", "github_repo": github_repo}
        )

    @app.get("/fleet")
    async def fleet_page(request: Request) -> Response:
        return templates.TemplateResponse(request, "fleet.html", {"page": "fleet"})

    @app.get("/oversight")
    async def oversight_page(request: Request) -> Response:
        return templates.TemplateResponse(request, "oversight.html", {"page": "oversight"})

    @app.get("/style-guide")
    async def style_guide_page(request: Request) -> Response:
        return templates.TemplateResponse(request, "style_guide.html", {"page": "style-guide"})


def _register_api_routers(app: FastAPI, *, prefix: str) -> None:
    """Register API routers under the given prefix."""
    app.include_router(overview.router, prefix=prefix)
    app.include_router(runs.router, prefix=prefix)
    app.include_router(costs.router, prefix=prefix)
    app.include_router(control.router, prefix=prefix)
    app.include_router(handoff.router, prefix=prefix)
    app.include_router(memory.router, prefix=prefix)
    app.include_router(logs.router, prefix=prefix)
    app.include_router(tasks.router, prefix=prefix)
    app.include_router(queue.router, prefix=prefix)
    app.include_router(settings.router, prefix=prefix)
    app.include_router(setup.router, prefix=prefix)
    app.include_router(agents.router, prefix=prefix)
    app.include_router(work.router, prefix=prefix)
    app.include_router(lifecycle.router, prefix=prefix)
    app.include_router(roles.router, prefix=prefix)
    app.include_router(spec.router, prefix=prefix)
    app.include_router(prs.router, prefix=prefix)
    app.include_router(quota.router, prefix=prefix)
    app.include_router(dependencies.router, prefix=prefix)
    app.include_router(resources.router, prefix=prefix)
    app.include_router(supervisor.router, prefix=prefix)
    app.include_router(oversight.router, prefix=prefix)
    app.include_router(feed.router, prefix=prefix)
    app.include_router(fleet_insights.router, prefix=prefix)
    app.include_router(failure_analysis.router, prefix=prefix)


def _register_telemetry_router(app: FastAPI) -> None:
    """Register telemetry ingest router under global /api prefix.

    Registered separately from _register_api_routers because the telemetry
    endpoint is project-agnostic (project identity is in the payload body).
    """
    from sova.dashboard.routers import telemetry

    app.include_router(telemetry.router, prefix="/api")


def _register_a2a_routes(app: FastAPI) -> None:
    """Register A2A protocol endpoints: Agent Card discovery + task management.

    The /.well-known/agent.json endpoint is at the root (A2A spec).
    Task management endpoints are under /a2a/.
    Both are gated behind the a2a.enabled config flag.
    """
    from sova.dashboard.routers import a2a as a2a_router

    app.include_router(a2a_router.router, prefix="/a2a")

    @app.get("/.well-known/agent.json")
    async def well_known_agent_card(request: Request) -> Response:
        from sova.a2a.agent_card import generate_agent_card
        from sova.config.context import get_project_dir
        from sova.config.loader import load_config
        from sova.dashboard.services.agent_pool import get_default_project_dir

        project_dir = get_project_dir() or get_default_project_dir()
        if project_dir is None:
            endpoint_base = str(request.base_url).rstrip("/")
            card = generate_agent_card(endpoint_base=endpoint_base)
            return JSONResponse(content=card)

        cfg = load_config(project_dir)
        if not cfg.a2a.enabled:
            raise HTTPException(status_code=404, detail="A2A protocol is disabled")

        endpoint_base = cfg.a2a.endpoint_base or str(request.base_url).rstrip("/")
        card = generate_agent_card(endpoint_base=endpoint_base)
        return JSONResponse(content=card)
