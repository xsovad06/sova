"""Lifecycle API router -- issue lifecycle tracking and phase control."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from sova.core.state import LifecyclePhase
from sova.dashboard.services import lifecycle_service
from sova.db.session import get_session
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.api.lifecycle")

router = APIRouter(prefix="/lifecycle", tags=["lifecycle"])

_VALID_PHASES = {p.value for p in LifecyclePhase}


class AdvanceRequest(BaseModel):
    to_phase: str


# -- Read endpoints -----------------------------------------------------------


@router.get("/active")
async def list_active():
    """List all active lifecycles."""
    async with await get_session() as session:
        async with session.begin():
            lifecycles = await lifecycle_service.list_active_lifecycles(session)
            return {"lifecycles": [lifecycle_service.lifecycle_to_dict(lc) for lc in lifecycles]}


@router.get("/issue/{issue_number}")
async def get_by_issue(issue_number: str):
    """Get the lifecycle for an issue (real or reconstructed)."""
    github_repo = ""
    github_user = ""
    try:
        from sova.config.context import get_project_dir
        from sova.config.loader import load_config

        cfg = load_config(get_project_dir())
        github_repo = cfg.github_repo or ""
        github_user = cfg.github_user or ""
    except (OSError, ValueError, AttributeError, ImportError):
        log.debug("config.load_failed", exc_info=True)

    async with await get_session() as session:
        async with session.begin():
            result = await lifecycle_service.build_lifecycle_view(
                session, issue_number, github_repo=github_repo, github_user=github_user
            )
            if result is None:
                return {"error": "No lifecycle found", "issue_number": issue_number}
            return result


@router.get("/{lifecycle_id}")
async def get_lifecycle(lifecycle_id: int):
    """Get a lifecycle by ID with full phase detail."""
    async with await get_session() as session:
        async with session.begin():
            lc = await lifecycle_service.get_lifecycle(session, lifecycle_id)
            if lc is None:
                return {"error": "Lifecycle not found"}
            return lifecycle_service.lifecycle_to_dict(lc)


# -- Phase action endpoints ---------------------------------------------------


@router.post("/{lifecycle_id}/phase/{phase}/start")
async def start_phase(lifecycle_id: int, phase: str):
    """Start a phase within a lifecycle."""
    if phase not in _VALID_PHASES:
        return {"error": f"Invalid phase: {phase}"}
    async with await get_session() as session:
        async with session.begin():
            record = await lifecycle_service.start_phase(session, lifecycle_id, phase)
            if record is None:
                return {"error": "Failed to start phase"}
            return {"status": "started", "phase": phase, "attempt": record.attempt}


@router.post("/{lifecycle_id}/phase/{phase}/skip")
async def skip_phase(lifecycle_id: int, phase: str):
    """Skip a phase and advance."""
    if phase not in _VALID_PHASES:
        return {"error": f"Invalid phase: {phase}"}
    async with await get_session() as session:
        async with session.begin():
            ok = await lifecycle_service.skip_phase(session, lifecycle_id, phase)
            if not ok:
                return {"error": "Failed to skip phase"}
            lc = await lifecycle_service.get_lifecycle(session, lifecycle_id)
            return {"status": "skipped", "phase": phase, "current_phase": lc.current_phase if lc else None}


@router.post("/{lifecycle_id}/phase/{phase}/restart")
async def restart_phase(lifecycle_id: int, phase: str):
    """Restart a failed phase."""
    if phase not in _VALID_PHASES:
        return {"error": f"Invalid phase: {phase}"}
    async with await get_session() as session:
        async with session.begin():
            record = await lifecycle_service.restart_phase(session, lifecycle_id, phase)
            if record is None:
                return {"error": "No failed phase to restart"}
            return {"status": "restart_ready", "phase": phase}


@router.post("/{lifecycle_id}/advance")
async def force_advance(lifecycle_id: int, req: AdvanceRequest):
    """Force-advance the lifecycle to a specific phase."""
    async with await get_session() as session:
        async with session.begin():
            ok = await lifecycle_service.force_advance(session, lifecycle_id, req.to_phase)
            if not ok:
                return {"error": "Failed to advance"}
            return {"status": "advanced", "to_phase": req.to_phase}


@router.post("/{lifecycle_id}/abandon")
async def abandon(lifecycle_id: int):
    """Abandon a lifecycle."""
    async with await get_session() as session:
        async with session.begin():
            ok = await lifecycle_service.abandon_lifecycle(session, lifecycle_id)
            if not ok:
                return {"error": "Failed to abandon lifecycle"}
            return {"status": "abandoned"}
