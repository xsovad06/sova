"""SOVA Dashboard -- FastAPI app factory.

Supports two modes:
- Single-project: `create_app(project_dir=Path(...))` -- current behavior
- Multi-project: `create_app(multi_project=True)` -- uses project registry
- Auto-detect: `create_app()` -- multi if registry has projects, else single (CWD)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from sova.config.registry import has_projects, list_projects
from sova.dashboard.routers import control, costs, handoff, logs, memory, overview, queue, runs, settings, setup, tasks
from sova.dashboard.services import control_service, handoff_service
from sova.db.session import close_db, init_db, init_db_for_project

BASE = Path(__file__).parent


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
    # Auto-detect mode
    if multi_project is None:
        if project_dir is not None:
            multi_project = False
        else:
            multi_project = has_projects()

    is_multi = multi_project

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if is_multi:
            # Initialize DB for each registered project
            for _slug, path_str in list_projects().items():
                p = Path(path_str)
                if p.is_dir():
                    await init_db_for_project(p)
        else:
            await init_db(project_dir)
        yield
        await close_db()

    app = FastAPI(title="SOVA Dashboard", lifespan=lifespan)

    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
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
    async def home(request: Request):
        return RedirectResponse(url="/overview")

    _register_page_routes(app, templates)
    _register_api_routers(app, prefix="/api")


def _setup_multi_project(app: FastAPI, templates: Jinja2Templates) -> None:
    """Configure routes for multi-project mode."""
    from sova.config.registry import register_project, unregister_project
    from sova.dashboard.middleware import ProjectContextMiddleware

    app.add_middleware(ProjectContextMiddleware)

    # -- Home page (project list) --
    @app.get("/")
    async def home(request: Request):
        projects = list_projects()
        return templates.TemplateResponse(request, "home.html", {"projects": projects})

    # -- Project API --
    @app.get("/api/projects")
    async def api_list_projects():
        return {"projects": list_projects()}

    class RegisterRequest(BaseModel):
        path: str
        slug: str | None = None

    class UnregisterRequest(BaseModel):
        slug: str

    @app.post("/api/projects/register")
    async def api_register_project(req: RegisterRequest):
        p = Path(req.path)
        result_slug = register_project(p, req.slug)
        await init_db_for_project(p)
        return {"slug": result_slug, "path": str(p)}

    @app.post("/api/projects/unregister")
    async def api_unregister_project(req: UnregisterRequest):
        removed = unregister_project(req.slug)
        return {"removed": removed}

    # -- Setup page (global, not project-scoped) --
    @app.get("/setup")
    async def setup_page(request: Request):
        return templates.TemplateResponse(request, "setup.html", {"page": "setup"})

    # -- Project-scoped page routes --
    @app.get("/p/{slug}")
    async def project_redirect(slug: str):
        return RedirectResponse(url=f"/p/{slug}/overview")

    @app.get("/p/{slug}/overview")
    async def project_overview(request: Request, slug: str):
        return _project_page(request, templates, slug, "overview.html", "overview")

    @app.get("/p/{slug}/runs")
    async def project_runs(request: Request, slug: str):
        return _project_page(request, templates, slug, "runs.html", "runs")

    @app.get("/p/{slug}/runs/{run_id}")
    async def project_run_detail(request: Request, slug: str, run_id: int):
        return _project_page(request, templates, slug, "run_detail.html", "runs", run_id=run_id)

    @app.get("/p/{slug}/costs")
    async def project_costs(request: Request, slug: str):
        return _project_page(request, templates, slug, "costs.html", "costs")

    @app.get("/p/{slug}/control")
    async def project_control(request: Request, slug: str):
        return _project_page(request, templates, slug, "control.html", "control")

    @app.get("/p/{slug}/tasks")
    async def project_tasks(request: Request, slug: str):
        return _project_page(request, templates, slug, "tasks.html", "tasks")

    @app.get("/p/{slug}/queue")
    async def project_queue(request: Request, slug: str):
        return _project_page(request, templates, slug, "queue.html", "queue")

    @app.get("/p/{slug}/logs")
    async def project_logs(request: Request, slug: str):
        return _project_page(request, templates, slug, "logs.html", "logs")

    @app.get("/p/{slug}/settings")
    async def project_settings(request: Request, slug: str):
        return _project_page(request, templates, slug, "settings.html", "settings")

    @app.get("/p/{slug}/memory")
    async def project_memory(request: Request, slug: str):
        return _project_page(request, templates, slug, "memory.html", "memory")

    # -- Project-scoped API --
    _register_api_routers(app, prefix="/p/{slug}/api")

    # Also keep non-prefixed API for backward compat / fallback
    _register_api_routers(app, prefix="/api")


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

    @app.get("/overview")
    async def overview_page(request: Request):
        return templates.TemplateResponse(request, "overview.html", {"page": "overview"})

    @app.get("/runs")
    async def runs_page(request: Request):
        return templates.TemplateResponse(request, "runs.html", {"page": "runs"})

    @app.get("/runs/{run_id}")
    async def run_detail_page(request: Request, run_id: int):
        return templates.TemplateResponse(request, "run_detail.html", {"page": "runs", "run_id": run_id})

    @app.get("/costs")
    async def costs_page(request: Request):
        return templates.TemplateResponse(request, "costs.html", {"page": "costs"})

    @app.get("/control")
    async def control_page(request: Request):
        return templates.TemplateResponse(request, "control.html", {"page": "control"})

    @app.get("/tasks")
    async def tasks_page(request: Request):
        return templates.TemplateResponse(request, "tasks.html", {"page": "tasks"})

    @app.get("/queue")
    async def queue_page(request: Request):
        return templates.TemplateResponse(request, "queue.html", {"page": "queue"})

    @app.get("/logs")
    async def logs_page(request: Request):
        return templates.TemplateResponse(request, "logs.html", {"page": "logs"})

    @app.get("/settings")
    async def settings_page(request: Request):
        return templates.TemplateResponse(request, "settings.html", {"page": "settings"})

    @app.get("/memory")
    async def memory_page(request: Request):
        return templates.TemplateResponse(request, "memory.html", {"page": "memory"})


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
