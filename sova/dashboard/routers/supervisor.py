"""Supervisor API router: status, manual poll trigger, decision log queries, approval plan, and persona."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from sova.config.context import get_project_dir, resolve_project_dir
from sova.config.db_loader import task_queue_lock
from sova.config.loader import load_config
from sova.dashboard.services import daemon_registry, supervisor_service
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.config.models import ProjectConfig

log = get_logger(component="dashboard.api.supervisor")


class ApproveRequest(BaseModel):
    issue_numbers: list[int] | None = None


class QueueSetRequest(BaseModel):
    issue_numbers: list[int]
    # The queue the new order was computed from. When present, the write is rebased onto the stored queue so a
    # concurrent add or removal is kept; absent, the stored queue is replaced outright.
    based_on: list[int] | None = None

    @field_validator("issue_numbers", "based_on")
    @classmethod
    def _all_positive(cls, v: list[int] | None) -> list[int] | None:
        if v is not None and any(n < 1 for n in v):
            raise ValueError("All issue numbers must be positive integers")
        return v


class QueueAddRequest(BaseModel):
    issue_number: int = Field(gt=0)


router = APIRouter(prefix="/supervisor", tags=["supervisor"])

_background_tasks: set[asyncio.Task] = set()
_queue_lock = task_queue_lock


def _load_cfg(project_dir: Path | None = None) -> ProjectConfig:
    """The (cached) project config for *project_dir*, else for the request's project."""
    return load_config(project_dir if project_dir is not None else get_project_dir())


def _resolve_project_dir() -> Path:
    """Return the active project directory or fall back to the sole registered daemon's path.

    In single-project mode the context var is unset, so we infer from the
    daemon registry (exactly one entry) or fall back to cwd.
    """
    from sova.dashboard.services.agent_pool import get_default_project_dir

    return resolve_project_dir(
        daemon_registry=daemon_registry.get_registry(), default_project_dir=get_default_project_dir()
    )


def _project_dir_and_slug() -> tuple[Path, str | None]:
    """Return the active project dir and its github_repo slug (None on any config load failure)."""
    project_dir = get_project_dir()
    try:
        project_slug = _load_cfg(project_dir).github_repo or None
    except Exception:  # noqa: BLE001 (config may fail for many reasons (missing file, bad TOML, import errors))
        log.warning("supervisor.config_load_failed", project_dir=str(project_dir), exc_info=True)
        project_slug = None
    return project_dir, project_slug


@router.get("/status")
async def get_status() -> dict:
    """Return supervisor daemon status."""
    daemon = daemon_registry.get_daemon()
    if daemon is None:
        return {"enabled": False, "running": False}
    return daemon.get_status()


@router.post("/poll", status_code=202, responses={404: {"description": "Supervisor daemon is not running"}})
async def trigger_poll() -> dict:
    """Trigger a manual poll cycle (fire-and-forget)."""
    daemon = daemon_registry.get_daemon()
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
    project_dir, project_slug = _project_dir_and_slug()
    decisions = await supervisor_service.get_recent_decisions(
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
    from sova.db.session import get_session_factory
    from sova.supervisor.daemon import SupervisorDaemon

    project_dir = _resolve_project_dir()

    async with daemon_registry.get_lifecycle_lock(project_dir):
        daemon = daemon_registry.get_daemon(project_dir)
        if daemon is not None and daemon.running:
            return {"started": False, "reason": "already running", **daemon.get_status()}

        cfg = _load_cfg(project_dir)
        if not cfg.supervisor.enabled:
            raise HTTPException(status_code=409, detail="supervisor.enabled is false in config, enable it first")

        session_factory = await get_session_factory(project_dir)
        new_daemon = SupervisorDaemon(config=cfg, project_dir=project_dir, session_factory=session_factory)
        new_daemon.start()
        daemon_registry.register_daemon(project_dir, new_daemon)
        log.info("supervisor.started_via_api", project_dir=str(project_dir))
        return {"started": True, **new_daemon.get_status()}


@router.post("/stop", responses={404: {"description": "Supervisor daemon is not running"}})
async def stop_supervisor() -> dict[str, Any]:
    """Stop the supervisor daemon for the current project.

    Cancels the polling loop and removes the daemon from the registry.
    Config is not modified; re-enable via POST /supervisor/start.
    """
    project_dir = _resolve_project_dir()

    async with daemon_registry.get_lifecycle_lock(project_dir):
        daemon = daemon_registry.get_daemon(project_dir)
        if daemon is None or not daemon.running:
            raise HTTPException(status_code=404, detail="Supervisor daemon is not running")
        await daemon.stop()
        daemon_registry.unregister_daemon(project_dir)
        log.info("supervisor.stopped_via_api", project_dir=str(project_dir))
        return {"stopped": True, "running": False}


@router.get("/ci-budget")
async def get_ci_budget() -> dict:
    """Return current GitHub Actions CI minutes usage and threshold status."""
    return await supervisor_service.get_ci_budget_payload(_load_cfg())


@router.get("/counts")
async def get_counts() -> dict:
    """Return per-component decision counts."""
    project_dir, project_slug = _project_dir_and_slug()
    counts = await supervisor_service.get_decision_counts(project_dir, project_slug=project_slug)
    return {"counts": counts}


@router.get("/planner-health")
async def get_planner_health_endpoint() -> dict:
    """Return planner success/failure counts and last-seen timestamps for the dashboard widget."""
    project_dir = get_project_dir()
    try:
        cfg = _load_cfg(project_dir)
        project_slug = supervisor_service.resolve_project_slug(cfg.github_repo, project_dir)
    except (FileNotFoundError, PermissionError, ValueError):
        log.exception("Config load failed for project dir %s", project_dir)
        return supervisor_service.get_planner_health(None)

    return supervisor_service.get_planner_health(project_slug)


@router.get("/plan")
async def get_plan() -> dict:
    """Return the current pending approval plan.

    When ``supervisor.require_approval`` is True, the daemon stores actionable
    decisions here instead of executing them immediately.  The frontend polls
    this endpoint to render the "Pending Actions" panel.
    """
    project_dir = get_project_dir()
    try:
        cfg = _load_cfg(project_dir)
        project_slug = supervisor_service.resolve_project_slug(cfg.github_repo, project_dir)
    except (FileNotFoundError, PermissionError, ValueError):
        log.exception("Config load failed for project dir %s", project_dir)
        return {"reasoning": None, "pending": [], "deferred": []}

    plan = supervisor_service.get_pending_plan(project_slug)
    return {
        "reasoning": supervisor_service.get_plan_reasoning(project_slug),
        "pending": [
            {
                "issue_number": d.issue_number,
                "action": d.action.value,
                "role": d.role,
                "reason": d.reason,
            }
            for d in plan
        ],
        "deferred": supervisor_service.get_plan_deferred(project_slug),
    }


@router.post(
    "/plan/approve",
    responses={
        502: {"description": "Some approved decisions failed to execute (the plan is already consumed)"},
        503: {"description": "The decisions could not be prepared; nothing ran and the plan is unchanged"},
    },
)
async def approve_plan(body: ApproveRequest) -> dict:
    """Execute approved decisions from the pending plan.

    When ``issue_numbers`` is provided, only those issues are approved and
    executed; the rest remain in the plan.  When omitted or empty, all pending
    decisions are approved and the plan is cleared.
    """
    project_dir = get_project_dir()
    project_slug = supervisor_service.resolve_project_slug(_load_cfg(project_dir).github_repo, project_dir)

    plan = supervisor_service.get_pending_plan(project_slug)
    if not plan:
        return {"approved": 0, "results": []}

    # Kept so the plan can be put back whole if the engine cannot be built, before any decision has run.
    reasoning = supervisor_service.get_plan_reasoning(project_slug)
    deferred = supervisor_service.get_plan_deferred(project_slug)

    if body.issue_numbers:
        to_execute = supervisor_service.remove_plan_items(set(body.issue_numbers), project_slug)
    else:
        to_execute = plan
        supervisor_service.set_pending_plan([], project_slug=project_slug)

    try:
        results = await supervisor_service.execute_plan_decisions(to_execute, project_dir)
    except supervisor_service.PlanSetupError as exc:
        # Merged back, not replaced: the daemon or another request may have written the plan while the engine was built.
        supervisor_service.restore_plan_items(
            to_execute, project_slug=project_slug, reasoning=reasoning, deferred=deferred
        )
        raise HTTPException(status_code=503, detail="Failed to prepare the decisions; the plan is unchanged") from exc
    # Every result carries an explicit ``ok`` (execute_plan_decisions() guarantees it), so only a False is a failure.
    if any(r.get("ok") is False for r in results):
        # The plan items are already consumed, so the detail carries every per-decision result for the caller.
        raise HTTPException(
            status_code=502,
            detail={"message": "Some approved decisions failed to execute", "results": results},
        )
    return {"approved": len(to_execute), "results": results}


@router.post("/plan/skip/{issue_number}", responses={404: {"description": "Issue not in pending plan"}})
async def skip_plan_item(issue_number: int) -> dict:
    """Remove a single issue from the pending plan without executing it."""
    project_dir = get_project_dir()
    project_slug = supervisor_service.resolve_project_slug(_load_cfg(project_dir).github_repo, project_dir)

    removed = supervisor_service.remove_plan_items({issue_number}, project_slug)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Issue #{issue_number} is not in the pending plan")
    return {"skipped": issue_number}


# Task Queue CRUD


async def _persist_queue(project_dir: Path | None, queue: list[int], *, based_on: list[int] | None = None) -> list[int]:
    """Save the queue, answering a concurrent write with a 409 and a persistence failure with a 503."""
    try:
        return await supervisor_service.persist_queue(project_dir, queue, based_on=based_on)
    except supervisor_service.QueueConflictError as exc:
        raise HTTPException(status_code=409, detail="The queue changed; reload and retry") from exc
    except supervisor_service.QueuePersistError as exc:
        raise HTTPException(status_code=503, detail="Failed to persist queue to database") from exc


async def _board_shows_the_write(project_dir: Path | None) -> None:
    """Wait for the board rebuild a queue write requested, once the queue lock is released.

    The Work page reloads its board right after the write returns; every other tab keeps its board until the
    rebuild lands and then hears ``work_items_changed`` if it differs. The queue is already saved, so a failed
    wait (a build failing while GitHub is down, an unresolvable project) is logged, never answered as an error.
    """
    from sova.dashboard.services.work_board_service import board_rebuilds_settled

    try:
        await board_rebuilds_settled(project_dir)
    except Exception:  # noqa: BLE001 (the write succeeded; the board catches up on its rebuild cadence)
        log.warning("supervisor.queue.board_wait_failed", exc_info=True)


@router.get("/queue")
async def get_queue() -> dict:
    """Return the current task queue."""
    project_dir = _resolve_project_dir()
    async with _queue_lock:
        await supervisor_service.maybe_migrate_queue_from_toml(project_dir)
    return {"queue": await supervisor_service.read_queue(project_dir)}


@router.put(
    "/queue",
    responses={
        409: {"description": "The queue changed since it was read; reload and retry"},
        503: {"description": "Failed to save queue to database"},
    },
)
async def set_queue(body: QueueSetRequest) -> dict:
    """Replace the entire task queue (supports reordering)."""
    async with _queue_lock:
        project_dir = _resolve_project_dir()
        queue = supervisor_service.deduplicate(body.issue_numbers)
        based_on = supervisor_service.deduplicate(body.based_on) if body.based_on is not None else None
        saved = await _persist_queue(project_dir, queue, based_on=based_on)
        await supervisor_service.notify_daemon_queue_change(project_dir)
    await _board_shows_the_write(project_dir)
    return {"queue": saved}


@router.post(
    "/queue",
    status_code=201,
    responses={
        409: {"description": "Issue already in queue, or the queue changed since it was read"},
        503: {"description": "Failed to save queue to database"},
    },
)
async def add_to_queue(body: QueueAddRequest) -> dict:
    """Add a single issue to the end of the queue."""
    async with _queue_lock:
        project_dir = _resolve_project_dir()
        queue = await supervisor_service.read_queue(project_dir)

        if body.issue_number in queue:
            raise HTTPException(status_code=409, detail=f"Issue #{body.issue_number} is already in the queue")

        queue = await _persist_queue(project_dir, [*queue, body.issue_number], based_on=queue)
        await supervisor_service.notify_daemon_queue_change(project_dir)
    await _board_shows_the_write(project_dir)
    return {"queue": queue}


@router.delete(
    "/queue/{issue_number}",
    responses={
        404: {"description": "Issue not in queue"},
        409: {"description": "The queue changed since it was read; reload and retry"},
        503: {"description": "Failed to save queue to database"},
    },
)
async def remove_from_queue(issue_number: int) -> dict:
    """Remove a single issue from the queue."""
    async with _queue_lock:
        project_dir = _resolve_project_dir()
        queue = await supervisor_service.read_queue(project_dir)

        if issue_number not in queue:
            raise HTTPException(status_code=404, detail=f"Issue #{issue_number} is not in the queue")

        queue = await _persist_queue(project_dir, [i for i in queue if i != issue_number], based_on=queue)
        await supervisor_service.notify_daemon_queue_change(project_dir)
    await _board_shows_the_write(project_dir)
    return {"queue": queue}


@router.delete(
    "/queue",
    responses={
        409: {"description": "The queue changed since it was read; reload and retry"},
        503: {"description": "Failed to save queue to database"},
    },
)
async def clear_queue() -> dict:
    """Clear the entire task queue."""
    async with _queue_lock:
        project_dir = _resolve_project_dir()
        await _persist_queue(project_dir, [])
        await supervisor_service.notify_daemon_queue_change(project_dir)
    await _board_shows_the_write(project_dir)
    return {"queue": []}


# Supervisor Persona


@router.get("/persona", responses={500: {"description": "Internal error"}})
async def get_supervisor_persona() -> dict:
    """Get the supervisor persona content and metadata."""
    from sova.supervisor.persona import get_persona_info

    return get_persona_info(_load_cfg().supervisor.persona_path)


@router.post(
    "/persona/open",
    responses={400: {"description": "Cannot open editor"}, 500: {"description": "Internal error"}},
)
async def open_supervisor_persona_in_editor() -> dict:
    """Open the supervisor persona file in the OS default editor.

    Fire-and-forget: spawns the editor process without waiting for it to exit.
    """
    from sova.dashboard.services.editor_service import open_path_in_editor
    from sova.supervisor.persona import ensure_persona_exists

    return await open_path_in_editor(ensure_persona_exists(_load_cfg().supervisor.persona_path))
