"""Auth API: read-only LLM provider authentication status."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from sova.config.context import get_project_dir
from sova.dashboard.services import setup_service
from sova.utils.logging import get_logger

router = APIRouter()
log = get_logger(component="dashboard.auth")


@router.get("/auth/status", responses={503: {"description": "Provider auth status unavailable"}})
async def auth_status() -> dict:
    project_dir = get_project_dir() or Path.cwd()
    try:
        return await setup_service.get_auth_status(project_dir)
    except Exception as exc:  # noqa: BLE001 (HTTP boundary: any internal failure becomes a 503)
        log.warning("auth.status.error", exc_info=True)
        raise HTTPException(status_code=503, detail="Failed to fetch provider auth status") from exc
