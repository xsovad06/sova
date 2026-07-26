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


def _extract_validation_detail(exc: Exception) -> str:
    """Extract a user-readable message from a Pydantic ValidationError, or fall back to generic."""
    try:
        from pydantic import ValidationError

        if isinstance(exc, ValidationError) or (exc.__cause__ and isinstance(exc.__cause__, ValidationError)):
            ve = exc if isinstance(exc, ValidationError) else exc.__cause__
            fields = []
            for err in ve.errors():
                loc = ".".join(str(p) for p in err.get("loc", []))
                msg = err.get("msg", "")
                fields.append(f"{loc}: {msg}")
            if fields:
                return f"Invalid configuration: {'; '.join(fields[:3])}"
    except ImportError:
        pass
    return "Failed to fetch configuration"


class ConfigUpdateRequest(BaseModel):
    key: str
    value: str | bool | int | float


@router.get("/settings/config", responses={500: {"description": "Failed to fetch configuration"}})
async def get_config():
    """Get the current project configuration (flat, for backward compat)."""
    try:
        project_dir = get_project_dir()
        return {"config": settings_service.get_config(project_dir)}
    except Exception as exc:  # noqa: BLE001 - route boundary translates config/pydantic/IO errors to HTTP 500
        log.warning("settings.config.get.error", exc_info=True)
        detail = _extract_validation_detail(exc)
        raise HTTPException(status_code=500, detail=detail) from None


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
    except Exception as exc:  # noqa: BLE001 - route boundary translates config/pydantic/IO errors to HTTP 500
        log.warning("settings.config.grouped.error", exc_info=True)
        detail = _extract_validation_detail(exc)
        raise HTTPException(status_code=500, detail=detail) from None


@router.post("/settings/config", responses={500: {"description": "Failed to update configuration"}})
async def update_config(req: ConfigUpdateRequest):
    """Update a single configuration key."""
    try:
        project_dir = get_project_dir()
        # Normalize JSON booleans/numbers to strings; the service expects str.
        raw = req.value
        if isinstance(raw, bool):
            value_str = "true" if raw else "false"
        else:
            value_str = str(raw)
        result = settings_service.update_config(project_dir, key=req.key, value=value_str)
        if result.get("status") == "ok" and req.key == "max_parallel_agents":
            from sova.dashboard.services.agent_pool import sync_max_concurrent

            sync_max_concurrent(project_dir)
        return result
    except Exception:
        log.warning("settings.config.update.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update configuration")


@router.get(
    "/settings/installation/status",
    responses={500: {"description": "Failed to check installation status"}},
)
async def installation_status() -> dict[str, object]:
    """Check for available SOVA command and guideline updates."""
    from sova.commands.catalog import get_canonical_dir
    from sova.commands.distribution import diff_commands
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
        from sova.commands.distribution import DiffResult
        from sova.commands.manifest import read_manifest

        canonical_dir = get_canonical_dir()
        commands_dir = project_dir / ".claude" / "commands"
        rules_dir = project_dir / ".claude" / "rules"

        cmd_diff = await asyncio.to_thread(diff_commands, canonical_dir, commands_dir, cfg)

        # Only diff guidelines if they were previously installed (manifest exists)
        if rules_dir.is_dir() and read_manifest(rules_dir) is not None:
            from sova.commands.catalog import get_guidelines_dir
            from sova.commands.distribution import diff_guidelines

            guidelines_dir = get_guidelines_dir()
            guide_diff = await asyncio.to_thread(diff_guidelines, guidelines_dir, rules_dir, cfg)
        else:
            guide_diff = DiffResult()

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


@router.get("/settings/persona", responses={500: {"description": "Failed to fetch operations persona"}})
async def get_operations_persona():
    """Get the operations persona content and metadata."""
    try:
        from sova.config.loader import load_config

        project_dir = get_project_dir()
        cfg = load_config(project_dir)

        from sova.oversight.persona import get_persona_info

        return get_persona_info(cfg.oversight.persona_path)
    except Exception:
        log.warning("settings.persona.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch operations persona")


@router.post(
    "/settings/persona/open",
    responses={400: {"description": "Cannot open editor"}, 500: {"description": "Failed to open editor"}},
)
async def open_persona_in_editor():
    """Open the operations persona file in the OS default editor."""
    from sova.config.loader import load_config
    from sova.oversight.persona import ensure_persona_exists, get_open_command

    try:
        project_dir = get_project_dir()
        cfg = load_config(project_dir)
    except Exception:
        log.warning("settings.persona.open.config_error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to load project configuration")

    path = ensure_persona_exists(cfg.oversight.persona_path)
    cmd = get_open_command()
    if cmd is None:
        raise HTTPException(
            status_code=400,
            detail=f"No editor command found for this OS. Edit manually: {path}",
        )

    try:
        await asyncio.create_subprocess_exec(cmd, str(path))
        return {"status": "ok", "path": str(path)}
    except FileNotFoundError:
        raise HTTPException(
            status_code=400,
            detail=f"'{cmd}' not found. Edit the file manually: {path}",
        )
