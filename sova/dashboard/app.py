"""SOVA Dashboard -- FastAPI app factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sova.dashboard.routers import control, costs, handoff, memory, overview, runs
from sova.dashboard.services import control_service, handoff_service
from sova.db.session import close_db, init_db

BASE = Path(__file__).parent


def create_app(*, project_dir: Path | None = None) -> FastAPI:
    """Create and configure the SOVA dashboard FastAPI app."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await init_db(project_dir)
        yield
        await close_db()

    app = FastAPI(title="SOVA Dashboard", lifespan=lifespan)

    # Store project_dir so control service can spawn agents in the right directory
    resolved_project_dir = (project_dir or Path.cwd()).resolve()
    control_service.set_project_dir(resolved_project_dir)
    handoff_service.set_project_dir(resolved_project_dir)

    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    templates = Jinja2Templates(directory=BASE / "templates")

    # -- Page routes ----------------------------------------------------------

    @app.get("/")
    async def home(request: Request):
        return RedirectResponse(url="/overview")

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

    @app.get("/memory")
    async def memory_page(request: Request):
        return templates.TemplateResponse(request, "memory.html", {"page": "memory"})

    # -- API routes -----------------------------------------------------------

    app.include_router(overview.router, prefix="/api")
    app.include_router(runs.router, prefix="/api")
    app.include_router(costs.router, prefix="/api")
    app.include_router(control.router, prefix="/api")
    app.include_router(handoff.router, prefix="/api")
    app.include_router(memory.router, prefix="/api")

    return app
