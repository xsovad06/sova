"""Settings router -- config, invariants, personas, installation status."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from sova.dashboard.project_context import get_project_dir
from sova.dashboard.services import settings_service
from sova.dashboard.settings_meta import get_grouped_config
from sova.utils.logging import get_logger

router = APIRouter(tags=["settings"])
log = get_logger(component="dashboard.settings.router")


class ConfigUpdateRequest(BaseModel):
    key: str
    value: str


@router.get("/settings/config", responses={500: {"description": "Failed to fetch configuration"}})
async def get_config():
    """Get the current project configuration (flat, for backward compat)."""
    try:
        project_dir = get_project_dir()
        return {"config": settings_service.get_config(project_dir)}
    except Exception:
        log.warning("settings.config.get.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch configuration")


@router.get(
    "/settings/config/grouped",
    responses={500: {"description": "Failed to fetch configuration"}},
)
async def get_config_grouped():
    """Get configuration organized into labeled groups with descriptions."""
    try:
        project_dir = get_project_dir()
        flat = settings_service.get_config(project_dir)
        return {"groups": get_grouped_config(flat)}
    except Exception:
        log.warning("settings.config.grouped.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch configuration")


@router.post("/settings/config", responses={500: {"description": "Failed to update configuration"}})
async def update_config(req: ConfigUpdateRequest):
    """Update a single configuration key."""
    try:
        project_dir = get_project_dir()
        return settings_service.update_config(project_dir, key=req.key, value=req.value)
    except Exception:
        log.warning("settings.config.update.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update configuration")


@router.get(
    "/settings/installation/status",
    responses={500: {"description": "Failed to check installation status"}},
)
async def installation_status() -> dict[str, object]:
    """Check for available SOVA command and guideline updates."""
    from sova.commands.catalog import get_canonical_dir, get_guidelines_dir
    from sova.commands.distribution import diff_commands, diff_guidelines
    from sova.config.loader import load_config

    project_dir = get_project_dir() or Path.cwd()

    try:
        cfg = load_config(project_dir)
    except (FileNotFoundError, ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to load project config: {exc}",
        ) from exc

    try:
        canonical_dir = get_canonical_dir()
        guidelines_dir = get_guidelines_dir()
        commands_dir = project_dir / ".claude" / "commands"
        rules_dir = project_dir / ".claude" / "rules"

        cmd_diff, guide_diff = await asyncio.to_thread(
            lambda: (
                diff_commands(canonical_dir, commands_dir, cfg),
                diff_guidelines(guidelines_dir, rules_dir, cfg),
            )
        )

        total = (
            len(cmd_diff.changed)
            + len(cmd_diff.new)
            + len(cmd_diff.removed)
            + len(guide_diff.changed)
            + len(guide_diff.new)
            + len(guide_diff.removed)
        )

        return {
            "commands": asdict(cmd_diff),
            "guidelines": asdict(guide_diff),
            "total_updates": total,
            "has_updates": total > 0,
        }
    except Exception as exc:  # noqa: BLE001 - route boundary translates unexpected failures to HTTP 500
        log.warning("settings.installation.status.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to check installation status") from exc


@router.get("/settings/invariants", responses={500: {"description": "Failed to fetch invariants"}})
async def list_invariants():
    """List invariant scripts."""
    try:
        project_dir = get_project_dir()
        return {"invariants": settings_service.list_invariants(project_dir)}
    except Exception:
        log.warning("settings.invariants.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch invariants")


@router.get("/settings/personas", responses={500: {"description": "Failed to fetch personas"}})
async def list_personas():
    """List available personas and detected persona."""
    try:
        project_dir = get_project_dir()
        return {
            "personas": settings_service.list_personas(project_dir),
            "detected": settings_service.get_detected_persona(project_dir),
        }
    except Exception:
        log.warning("settings.personas.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch personas")
