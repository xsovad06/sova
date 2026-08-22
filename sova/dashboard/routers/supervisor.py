"""Supervisor API router: status, manual poll trigger, decision log queries, approval plan, and persona."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

if TYPE_CHECKING:
    from sova.supervisor.progression import ProgressionDecision

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from sova.dashboard.project_context import get_project_dir
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.api.supervisor")

_QUEUE_SETTING_KEY = "supervisor.task_queue"


class ApproveRequest(BaseModel):
    issue_numbers: list[int] | None = None


class QueueSetRequest(BaseModel):
    issue_numbers: list[int]

    @field_validator("issue_numbers")
    @classmethod
    def _all_positive(cls, v: list[int]) -> list[int]:
        if any(n < 1 for n in v):
            raise ValueError("All issue_numbers must be positive integers")
        return v


class QueueAddRequest(BaseModel):
    issue_number: int = Field(gt=0)


router = APIRouter(prefix="/supervisor", tags=["supervisor"])

_daemon_registry: dict = {}
_background_tasks: set[asyncio.Task] = set()
_start_lock = asyncio.Lock()
_queue_lock = asyncio.Lock()


def _get_daemon(project_dir: Path | None = None) -> Any | None:
    """Get the daemon instance for the current project, if any.

    In single-project mode get_project_dir() returns None because no
    multi-project middleware sets the context var.  Fall back to the sole
    registry entry when exactly one daemon is registered.
    """
    pd = project_dir or get_project_dir()
    if pd is not None:
        return _daemon_registry.get(str(pd.resolve()))
    if len(_daemon_registry) == 1:
        return next(iter(_daemon_registry.values()))
    return None


def set_daemon_registry(registry: dict) -> None:
    """Called by app.py lifespan to share the daemon registry."""
    global _daemon_registry
    _daemon_registry = registry


def _resolve_project_dir() -> Path:
    """Return the active project directory or fall back to the sole registered daemon's path.

    In single-project mode the context var is unset, so we infer from the
    daemon registry (exactly one entry) or fall back to cwd.
    """
    pd = get_project_dir()
    if pd is not None:
        return pd
    if len(_daemon_registry) == 1:
        return Path(next(iter(_daemon_registry)))
    from sova.dashboard.services.agent_pool import get_default_project_dir

    ctrl = get_default_project_dir()
    if ctrl is not None:
        return ctrl
    return Path.cwd().resolve()


@router.get("/status")
async def get_status() -> dict:
    """Return supervisor daemon status."""
    daemon = _get_daemon()
    if daemon is None:
        return {"enabled": False, "running": False}
    return daemon.get_status()


@router.post("/poll", status_code=202, responses={404: {"description": "Supervisor daemon is not running"}})
async def trigger_poll() -> dict:
    """Trigger a manual poll cycle (fire-and-forget)."""
    daemon = _get_daemon()
    if daemon is None:
        raise HTTPException(status_code=404, detail="Supervisor daemon is not running")
    task = asyncio.create_task(daemon.poll_once())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"status": "accepted"}


@router.get("/decisions")
async def get_decisions(
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    component: str | None = None,
    event_type: str | None = None,
) -> dict:
    """Return recent supervisor decisions."""
    from sova.config.loader import load_config
    from sova.dashboard.services.supervisor_service import get_recent_decisions

    project_dir = get_project_dir()
    try:
        cfg = load_config(project_dir)
        project_slug = cfg.github_repo or None
    except Exception:
        project_slug = None
    decisions = await get_recent_decisions(
        project_dir,
        project_slug=project_slug,
        limit=limit,
        component=component,
        event_type=event_type,
    )
    return {"decisions": decisions}


@router.post("/start", responses={409: {"description": "supervisor.enabled is false in config"}})
async def start_supervisor() -> dict[str, Any]:
    """Start the supervisor daemon for the current project.

    Safe to call after enabling supervisor.enabled in settings without a server restart.
    Returns the daemon status after starting (or the existing status if already running).
    """
    from sova.config.loader import load_config
    from sova.db.session import get_session_factory
    from sova.supervisor.daemon import SupervisorDaemon

    project_dir = _resolve_project_dir()

    async with _start_lock:
        daemon = _get_daemon(project_dir)
        if daemon is not None and daemon.running:
            return {"started": False, "reason": "already running", **daemon.get_status()}

        cfg = load_config(project_dir)
        if not cfg.supervisor.enabled:
            raise HTTPException(status_code=409, detail="supervisor.enabled is false in config, enable it first")

        session_factory = await get_session_factory(project_dir)
        new_daemon = SupervisorDaemon(config=cfg, project_dir=project_dir, session_factory=session_factory)
        new_daemon.start()
        _daemon_registry[str(project_dir.resolve())] = new_daemon
        log.info("supervisor.started_via_api", project_dir=str(project_dir))
        return {"started": True, **new_daemon.get_status()}


@router.post("/stop", responses={404: {"description": "Supervisor daemon is not running"}})
async def stop_supervisor() -> dict[str, Any]:
    """Stop the supervisor daemon for the current project.

    Cancels the polling loop and removes the daemon from the registry.
    Config is not modified; re-enable via POST /supervisor/start.
    """
    project_dir = _resolve_project_dir()

    async with _start_lock:
        daemon = _get_daemon(project_dir)
        if daemon is None or not daemon.running:
            raise HTTPException(status_code=404, detail="Supervisor daemon is not running")
        await daemon.stop()
        _daemon_registry.pop(str(project_dir.resolve()), None)
        log.info("supervisor.stopped_via_api", project_dir=str(project_dir))
        return {"stopped": True, "running": False}


_CI_BUDGET_ZERO = {
    "total": 0,
    "used": 0,
    "remaining": 0,
    "pct_used": 0.0,
    "warn": False,
    "block": False,
    "cached": False,
}


@router.get("/ci-budget")
async def get_ci_budget() -> dict:
    """Return current GitHub Actions CI minutes usage and threshold status."""
    from sova.config.loader import load_config
    from sova.supervisor.ci_budget import _UNLIMITED_SENTINEL, get_ci_budget_tracker

    project_dir = get_project_dir()
    cfg = load_config(project_dir)
    if not cfg.github_repo:
        return dict(_CI_BUDGET_ZERO)

    try:
        tracker = get_ci_budget_tracker(cfg.github_user)
        budget = await tracker.get_budget(cfg.github_repo, cfg.github_user)
    except Exception:
        log.warning("ci_budget.endpoint_failed", exc_info=True)
        return dict(_CI_BUDGET_ZERO)

    if budget.total == 0 and budget.remaining == 0:
        return dict(_CI_BUDGET_ZERO)

    warn = budget.remaining < cfg.supervisor.ci_warn_minutes and budget.remaining < _UNLIMITED_SENTINEL
    block = budget.remaining < cfg.supervisor.ci_block_minutes and budget.remaining < _UNLIMITED_SENTINEL
    return {
        "total": budget.total,
        "used": budget.used,
        "remaining": budget.remaining,
        "pct_used": budget.pct_used,
        "warn": warn,
        "block": block,
        "cached": bool(tracker._cache),
    }


@router.get("/counts")
async def get_counts() -> dict:
    """Return per-component decision counts."""
    from sova.config.loader import load_config
    from sova.dashboard.services.supervisor_service import get_decision_counts

    project_dir = get_project_dir()
    try:
        cfg = load_config(project_dir)
        project_slug = cfg.github_repo or None
    except Exception:
        project_slug = None
    counts = await get_decision_counts(project_dir, project_slug=project_slug)
    return {"counts": counts}


@router.get("/plan")
async def get_plan() -> dict:
    """Return the current pending approval plan.

    When ``supervisor.require_approval`` is True, the daemon stores actionable
    decisions here instead of executing them immediately.  The frontend polls
    this endpoint to render the "Pending Actions" panel.
    """
    from sova.config.loader import load_config
    from sova.dashboard.services.supervisor_service import (
        get_pending_plan,
        get_plan_deferred,
        get_plan_reasoning,
        resolve_project_slug,
    )

    project_dir = get_project_dir()
    try:
        cfg = load_config(project_dir)
        project_slug = resolve_project_slug(cfg.github_repo, project_dir)
    except (FileNotFoundError, PermissionError, ValueError):
        log.exception("Config load failed for project dir %s", project_dir)
        return {"reasoning": None, "pending": [], "deferred": []}

    plan = get_pending_plan(project_slug)
    return {
        "reasoning": get_plan_reasoning(project_slug),
        "pending": [
            {
                "issue_number": d.issue_number,
                "action": d.action.value,
                "role": d.role,
                "reason": d.reason,
            }
            for d in plan
        ],
        "deferred": get_plan_deferred(project_slug),
    }


async def _execute_plan_decisions(decisions: list[ProgressionDecision], project_dir: Path) -> list[dict]:
    """Execute a list of ProgressionDecision objects via the engine.

    Isolated into its own function so tests can patch it without touching
    the entire engine lifecycle.
    """
    from sova.adapters import create_adapter
    from sova.config.loader import load_config
    from sova.db.session import get_session_factory
    from sova.supervisor.progression import TaskProgressionEngine

    cfg = load_config(project_dir)
    adapter = create_adapter(cfg)
    session_factory = await get_session_factory(project_dir)
    engine = TaskProgressionEngine(
        config=cfg.supervisor,
        adapter=adapter,
        project_dir=project_dir,
        session_factory=session_factory,
    )
    results: list[dict] = []
    for decision in decisions:
        try:
            result = await engine.execute_decision(decision)
            results.append(result)
        except Exception as exc:
            log.warning("plan.execute_decision_failed", issue_number=decision.issue_number, exc_info=True)
            results.append({"error": str(exc), "issue_number": decision.issue_number})
    return results


@router.post("/plan/approve")
async def approve_plan(body: ApproveRequest) -> dict:
    """Execute approved decisions from the pending plan.

    When ``issue_numbers`` is provided, only those issues are approved and
    executed; the rest remain in the plan.  When omitted or empty, all pending
    decisions are approved and the plan is cleared.
    """
    from sova.config.loader import load_config
    from sova.dashboard.services.supervisor_service import (
        get_pending_plan,
        remove_plan_items,
        resolve_project_slug,
        set_pending_plan,
    )

    project_dir = get_project_dir()
    cfg = load_config(project_dir)
    project_slug = resolve_project_slug(cfg.github_repo, project_dir)

    plan = get_pending_plan(project_slug)
    if not plan:
        return {"approved": 0, "results": []}

    if body.issue_numbers:
        to_execute = remove_plan_items(set(body.issue_numbers), project_slug)
    else:
        to_execute = plan
        set_pending_plan([], project_slug=project_slug)

    results = await _execute_plan_decisions(to_execute, project_dir)
    errors = [r for r in results if "error" in r]
    return {"approved": len(to_execute), "results": results, "errors": errors}


@router.post("/plan/skip/{issue_number}", responses={404: {"description": "Issue not in pending plan"}})
async def skip_plan_item(issue_number: int) -> dict:
    """Remove a single issue from the pending plan without executing it."""
    from sova.config.loader import load_config
    from sova.dashboard.services.supervisor_service import remove_plan_items, resolve_project_slug

    project_dir = get_project_dir()
    cfg = load_config(project_dir)
    project_slug = resolve_project_slug(cfg.github_repo, project_dir)

    removed = remove_plan_items({issue_number}, project_slug)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Issue #{issue_number} is not in the pending plan")
    return {"skipped": issue_number}


# -- Task Queue CRUD --


def _deduplicate(items: list[int]) -> list[int]:
    """Deduplicate preserving first occurrence order."""
    return list(dict.fromkeys(items))


async def _persist_queue(project_dir: Path | None, queue: list[int]) -> None:
    """Persist task_queue via the shared DB helper.

    Catches DB-layer exceptions and raises HTTPException 503.
    """
    from sqlalchemy.exc import DBAPIError, SQLAlchemyError

    from sova.config.db_loader import save_task_queue

    try:
        await save_task_queue(project_dir, queue)
    except (SQLAlchemyError, DBAPIError):
        log.error("supervisor.queue.persist_failed", exc_info=True)
        raise HTTPException(status_code=503, detail="Failed to persist queue to database")


async def _read_queue(project_dir: Path | None) -> list[int]:
    """Read the current task queue from DB (async)."""
    from sova.config.db_loader import load_task_queue

    return await load_task_queue(project_dir)


_toml_migrated: set[str] = set()


async def _maybe_migrate_queue_from_toml(project_dir: Path | None) -> None:
    """One-time migration: copy task_queue from sova.toml to DB if DB is empty.

    Must be called under _queue_lock to prevent races with concurrent writes.
    """
    cache_key = str(project_dir) if project_dir else "__default__"
    if cache_key in _toml_migrated:
        return

    db_queue = await _read_queue(project_dir)
    if db_queue:
        _toml_migrated.add(cache_key)
        return

    try:
        from sova.config.loader import load_config

        cfg = load_config(project_dir)
        toml_queue = cfg.supervisor.task_queue
        if toml_queue:
            log.info("supervisor.queue.toml_migration", count=len(toml_queue))
            await _persist_queue(project_dir, toml_queue)
        _toml_migrated.add(cache_key)
    except Exception:
        log.debug("supervisor.queue.toml_migration_skipped", exc_info=True)


@router.get("/queue")
async def get_queue() -> dict:
    """Return the current task queue."""
    project_dir = get_project_dir()
    async with _queue_lock:
        await _maybe_migrate_queue_from_toml(project_dir)
    return {"queue": await _read_queue(project_dir)}


@router.put("/queue", responses={503: {"description": "Failed to save queue to database"}})
async def set_queue(body: QueueSetRequest) -> dict:
    """Replace the entire task queue (supports reordering)."""
    async with _queue_lock:
        project_dir = get_project_dir()
        queue = _deduplicate(body.issue_numbers)
        await _persist_queue(project_dir, queue)
        return {"queue": queue}


@router.post(
    "/queue",
    status_code=201,
    responses={
        409: {"description": "Issue already in queue"},
        503: {"description": "Failed to save queue to database"},
    },
)
async def add_to_queue(body: QueueAddRequest) -> dict:
    """Add a single issue to the end of the queue."""
    async with _queue_lock:
        project_dir = get_project_dir()
        queue = await _read_queue(project_dir)

        if body.issue_number in queue:
            raise HTTPException(status_code=409, detail=f"Issue #{body.issue_number} is already in the queue")

        queue.append(body.issue_number)
        await _persist_queue(project_dir, queue)
        return {"queue": queue}


@router.delete(
    "/queue/{issue_number}",
    responses={
        404: {"description": "Issue not in queue"},
        503: {"description": "Failed to save queue to database"},
    },
)
async def remove_from_queue(issue_number: int) -> dict:
    """Remove a single issue from the queue."""
    async with _queue_lock:
        project_dir = get_project_dir()
        queue = await _read_queue(project_dir)

        if issue_number not in queue:
            raise HTTPException(status_code=404, detail=f"Issue #{issue_number} is not in the queue")

        queue.remove(issue_number)
        await _persist_queue(project_dir, queue)
        return {"queue": queue}


@router.delete("/queue", responses={503: {"description": "Failed to save queue to database"}})
async def clear_queue() -> dict:
    """Clear the entire task queue."""
    async with _queue_lock:
        project_dir = get_project_dir()
        await _persist_queue(project_dir, [])
        return {"queue": []}


# -- Supervisor Persona --


@router.get("/persona", responses={500: {"description": "Failed to fetch supervisor persona"}})
async def get_supervisor_persona() -> dict:
    """Get the supervisor persona content and metadata."""
    try:
        from sova.config.loader import load_config
        from sova.supervisor.persona import get_persona_info

        project_dir = get_project_dir()
        cfg = load_config(project_dir)
        return get_persona_info(cfg.supervisor.persona_path)
    except Exception:
        log.warning("supervisor.persona.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch supervisor persona")


@router.post(
    "/persona/open",
    responses={400: {"description": "Cannot open editor"}, 500: {"description": "Failed to open editor"}},
)
async def open_supervisor_persona_in_editor() -> dict:
    """Open the supervisor persona file in the OS default editor.

    Fire-and-forget: spawns the editor process without waiting for it to exit.
    """
    from sova.config.loader import load_config
    from sova.oversight.persona import get_open_command
    from sova.supervisor.persona import ensure_persona_exists

    try:
        project_dir = get_project_dir()
        cfg = load_config(project_dir)
    except Exception:
        log.warning("supervisor.persona.open.config_error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to load project configuration")

    path = ensure_persona_exists(cfg.supervisor.persona_path)
    cmd = get_open_command()
    if cmd is None:
        raise HTTPException(
            status_code=400,
            detail=f"No editor command found for this OS. Edit manually: {path}",
        )

    try:
        await asyncio.create_subprocess_exec(cmd, str(path))
        return {"status": "spawned", "path": str(path)}
    except FileNotFoundError:
        raise HTTPException(
            status_code=400,
            detail=f"'{cmd}' not found. Edit the file manually: {path}",
        )
    except Exception:
        log.warning("supervisor.persona.open.subprocess_error", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to open editor. Edit manually: {path}",
        )
