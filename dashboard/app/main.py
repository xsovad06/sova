"""Project Automation Kit — Dashboard (multi-project)."""

import asyncio
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from app import config
from app.routers import overview, costs, logs, tasks, memory, queue, agent, setup, settings
from app.services import process_service

app = FastAPI(title="Project Automation Kit — Dashboard")

BASE = Path(__file__).parent
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


# ── Middleware: set project context from /p/{slug}/ URLs ─────────────────────

class ProjectContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        slug = None

        # Match /p/{slug}/... paths
        if path.startswith("/p/"):
            parts = path.split("/", 3)  # ['', 'p', 'slug', ...]
            if len(parts) >= 3 and parts[2]:
                slug = parts[2]

        if slug:
            project_path = config.get_project_path(slug)
            if project_path:
                data_dir = project_path / ".claude"
                config.set_project_context(data_dir)
                request.state.project_slug = slug
                request.state.project_name = project_path.name
                request.state.project_path = str(project_path)
            else:
                request.state.project_slug = slug
                request.state.project_name = slug
                request.state.project_path = ""
        else:
            request.state.project_slug = ""
            request.state.project_name = ""
            request.state.project_path = ""

        response = await call_next(request)
        return response


app.add_middleware(ProjectContextMiddleware)


# ── Home page: project list ─────────────────────────────────────────────────

@app.get("/")
async def home(request: Request):
    projects = config.list_projects()
    if not projects:
        # No projects registered — go to setup
        return templates.TemplateResponse(request, "home.html", {
            "page": "home",
            "projects": {},
        })
    return templates.TemplateResponse(request, "home.html", {
        "page": "home",
        "projects": projects,
    })


# ── Project-scoped page routes (/p/{slug}/...) ──────────────────────────────

@app.get("/p/{slug}")
async def project_redirect(slug: str):
    return RedirectResponse(url=f"/p/{slug}/")


@app.get("/p/{slug}/")
async def project_overview(request: Request, slug: str):
    return templates.TemplateResponse(request, "overview.html", {
        "page": "overview",
        "project_slug": slug,
        "project_name": getattr(request.state, "project_name", slug),
    })


@app.get("/p/{slug}/costs")
async def project_costs(request: Request, slug: str):
    return templates.TemplateResponse(request, "costs.html", {
        "page": "costs",
        "project_slug": slug,
        "project_name": getattr(request.state, "project_name", slug),
    })


@app.get("/p/{slug}/logs")
async def project_logs(request: Request, slug: str):
    return templates.TemplateResponse(request, "logs.html", {
        "page": "logs",
        "project_slug": slug,
        "project_name": getattr(request.state, "project_name", slug),
    })


@app.get("/p/{slug}/tasks")
async def project_tasks(request: Request, slug: str):
    return templates.TemplateResponse(request, "tasks.html", {
        "page": "tasks",
        "project_slug": slug,
        "project_name": getattr(request.state, "project_name", slug),
    })


@app.get("/p/{slug}/memory")
async def project_memory(request: Request, slug: str):
    return templates.TemplateResponse(request, "memory.html", {
        "page": "memory",
        "project_slug": slug,
        "project_name": getattr(request.state, "project_name", slug),
    })


@app.get("/p/{slug}/queue")
async def project_queue(request: Request, slug: str):
    return templates.TemplateResponse(request, "queue.html", {
        "page": "queue",
        "project_slug": slug,
        "project_name": getattr(request.state, "project_name", slug),
    })


@app.get("/p/{slug}/control")
async def project_control(request: Request, slug: str):
    return templates.TemplateResponse(request, "control.html", {
        "page": "control",
        "project_slug": slug,
        "project_name": getattr(request.state, "project_name", slug),
    })


@app.get("/p/{slug}/settings")
async def project_settings(request: Request, slug: str):
    return templates.TemplateResponse(request, "settings.html", {
        "page": "settings",
        "project_slug": slug,
        "project_name": getattr(request.state, "project_name", slug),
    })


# Setup is global (not project-scoped) — it's where you add new projects
@app.get("/setup")
async def setup_page(request: Request):
    return templates.TemplateResponse(request, "setup.html", {"page": "setup"})


# ── Project-scoped API routes (/p/{slug}/api/...) ───────────────────────────

app.include_router(overview.router, prefix="/p/{slug}/api")
app.include_router(costs.router, prefix="/p/{slug}/api")
app.include_router(logs.router, prefix="/p/{slug}/api")
app.include_router(tasks.router, prefix="/p/{slug}/api")
app.include_router(memory.router, prefix="/p/{slug}/api")
app.include_router(queue.router, prefix="/p/{slug}/api")
app.include_router(agent.router, prefix="/p/{slug}/api")
app.include_router(settings.router, prefix="/p/{slug}/api")

# Setup API is global
app.include_router(setup.router, prefix="/api")


# ── Legacy routes (default project, for backward compat) ────────────────────

app.include_router(overview.router, prefix="/api")
app.include_router(costs.router, prefix="/api")
app.include_router(logs.router, prefix="/api")
app.include_router(tasks.router, prefix="/api")
app.include_router(memory.router, prefix="/api")
app.include_router(queue.router, prefix="/api")
app.include_router(agent.router, prefix="/api")
app.include_router(settings.router, prefix="/api")


# Legacy page routes (default project)
@app.get("/overview")
async def legacy_overview(request: Request):
    return templates.TemplateResponse(request, "overview.html", {
        "page": "overview", "project_slug": "", "project_name": "",
    })


@app.get("/costs")
async def legacy_costs(request: Request):
    return templates.TemplateResponse(request, "costs.html", {
        "page": "costs", "project_slug": "", "project_name": "",
    })


@app.get("/logs")
async def legacy_logs(request: Request):
    return templates.TemplateResponse(request, "logs.html", {
        "page": "logs", "project_slug": "", "project_name": "",
    })


@app.get("/tasks")
async def legacy_tasks(request: Request):
    return templates.TemplateResponse(request, "tasks.html", {
        "page": "tasks", "project_slug": "", "project_name": "",
    })


@app.get("/memory")
async def legacy_memory(request: Request):
    return templates.TemplateResponse(request, "memory.html", {
        "page": "memory", "project_slug": "", "project_name": "",
    })


@app.get("/queue")
async def legacy_queue(request: Request):
    return templates.TemplateResponse(request, "queue.html", {
        "page": "queue", "project_slug": "", "project_name": "",
    })


@app.get("/control")
async def legacy_control(request: Request):
    return templates.TemplateResponse(request, "control.html", {
        "page": "control", "project_slug": "", "project_name": "",
    })


@app.get("/settings")
async def legacy_settings(request: Request):
    return templates.TemplateResponse(request, "settings.html", {
        "page": "settings", "project_slug": "", "project_name": "",
    })


# ── Project registry API ────────────────────────────────────────────────────

from pydantic import BaseModel


class RegisterProjectRequest(BaseModel):
    path: str
    slug: str = ""


@app.get("/api/projects")
async def list_projects():
    return config.list_projects()


@app.post("/api/projects/register")
async def register_project(req: RegisterProjectRequest):
    slug = config.register_project(req.path, req.slug)
    return {"status": "ok", "slug": slug}


@app.post("/api/projects/unregister")
async def unregister_project(req: RegisterProjectRequest):
    config.unregister_project(req.slug)
    return {"status": "ok"}


# ── WebSocket routes ────────────────────────────────────────────────────────

@app.websocket("/p/{slug}/ws/logs")
async def ws_logs_project(websocket: WebSocket, slug: str):
    """WebSocket for real-time agent output — project-scoped."""
    # Set project context for this connection
    project_path = config.get_project_path(slug)
    if project_path:
        config.set_project_context(project_path / ".claude")
    await _ws_logs_handler(websocket)


@app.websocket("/ws/logs")
async def ws_logs_legacy(websocket: WebSocket):
    """WebSocket for real-time agent output — default project."""
    await _ws_logs_handler(websocket)


async def _ws_logs_handler(websocket: WebSocket):
    """Shared WebSocket handler for agent output streaming."""
    await websocket.accept()
    cursor = len(process_service.get_output())
    last_checkpoint_id = None
    notif_cursor = process_service.get_notification_count()
    try:
        while True:
            lines = process_service.get_output(cursor)
            if lines:
                cursor += len(lines)
                await websocket.send_json({"lines": lines, "cursor": cursor})
            req = process_service.get_pending_request()
            req_id = req.get("id") if req else None
            if req and req_id != last_checkpoint_id:
                last_checkpoint_id = req_id
                await websocket.send_json({"checkpoint": req})
            elif not req:
                last_checkpoint_id = None
            new_notifs = process_service.get_notifications(notif_cursor)
            for notif in new_notifs:
                await websocket.send_json({"notification": notif})
            notif_cursor += len(new_notifs)
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
