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

# Maximum number of errors to include in label creation response
_MAX_LABEL_ERRORS = 10

# Maximum length of individual error messages to prevent unbounded response size
_MAX_ERROR_MESSAGE_LENGTH = 200

# Concurrency limit for parallel label creation requests
_LABEL_CREATE_CONCURRENCY = 5

# SOVA required labels with their colors (GitHub format)
_REQUIRED_LABELS = [
    # Type labels
    {"name": "type: feature", "color": "a2eeef", "description": "New feature or request"},
    {"name": "type: task", "color": "d4c5f9", "description": "General task"},
    {"name": "type: infra", "color": "0e8a16", "description": "Infrastructure or tooling"},
    {"name": "type: bug", "color": "d73a4a", "description": "Something isn't working"},
    {"name": "type: epic", "color": "bfd4f2", "description": "Multi-issue tracking container"},
    # Priority labels
    {"name": "priority: critical", "color": "b60205", "description": "Critical priority"},
    {"name": "priority: high", "color": "d93f0b", "description": "High priority"},
    {"name": "priority: medium", "color": "fbca04", "description": "Medium priority"},
    {"name": "priority: low", "color": "0e8a16", "description": "Low priority"},
    # Area labels
    {"name": "area: agent", "color": "c5def5", "description": "Agent core and roles"},
    {"name": "area: dashboard", "color": "c5def5", "description": "Web UI"},
    {"name": "area: commands", "color": "c5def5", "description": "Claude Code commands"},
    {"name": "area: personas", "color": "c5def5", "description": "Persona guidance files"},
    {"name": "area: invariants", "color": "c5def5", "description": "Pre-push checks"},
    {"name": "area: knowledge", "color": "c5def5", "description": "Knowledge system"},
    {"name": "area: docs", "color": "c5def5", "description": "Documentation"},
    # Agent state labels
    {"name": "agent:triaged", "color": "ededed", "description": "Triaged by agent"},
    {"name": "agent:researched", "color": "ededed", "description": "Research complete"},
    {"name": "agent:ready", "color": "ededed", "description": "Ready for development"},
    {"name": "agent:in-progress", "color": "ededed", "description": "Agent working"},
    {"name": "agent:in-review", "color": "ededed", "description": "Under review"},
    {"name": "agent:needs-spec", "color": "ededed", "description": "Needs specification"},
    {"name": "agent:human-only", "color": "ededed", "description": "Human intervention required"},
    # SOVA review verdict labels
    {"name": "sova:approved", "color": "0e8a16", "description": "SOVA review approved"},
    {"name": "sova:revise", "color": "fbca04", "description": "SOVA review requests changes"},
    {"name": "sova:block", "color": "d73a4a", "description": "SOVA review blocks merge"},
]


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


def _validate_github_config(cfg) -> None:
    """Validate GitHub task source configuration, raising HTTPException if invalid."""
    if cfg.task_source.type != "github":
        raise HTTPException(
            status_code=400,
            detail="Label operations are GitHub-only (Jira labels not yet supported)",
        )

    if not cfg.github_repo:
        raise HTTPException(
            status_code=400,
            detail="GitHub repository not configured",
        )


class ConfigUpdateRequest(BaseModel):
    key: str
    value: str | bool | int | float


@router.get("/settings/config", responses={500: {"description": "Failed to fetch configuration"}})
async def get_config() -> dict:
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
async def get_config_grouped() -> dict:
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
async def update_config(req: ConfigUpdateRequest) -> dict:
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
    except Exception as exc:
        log.warning("settings.config.update.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update configuration") from exc


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
async def list_invariants() -> dict:
    """List invariant scripts."""
    try:
        project_dir = get_project_dir()
        return {"invariants": settings_service.list_invariants(project_dir)}
    except Exception:
        log.warning("settings.invariants.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch invariants")


@router.get("/settings/personas", responses={500: {"description": "Failed to fetch personas"}})
async def list_personas() -> dict:
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
    """Open the operations persona file in the OS default editor.

    Fire-and-forget: spawns the editor process without waiting for it to exit.
    The response confirms the process was spawned, not that the editor opened
    successfully.
    """
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
        return {"status": "spawned", "path": str(path)}
    except FileNotFoundError:
        raise HTTPException(
            status_code=400,
            detail=f"'{cmd}' not found. Edit the file manually: {path}",
        )
    except Exception:
        log.warning("settings.persona.open.subprocess_error", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to open editor. Edit manually: {path}",
        )


async def _fetch_existing_labels(cfg) -> set[str] | dict:
    """Fetch existing label names from GitHub, returning a set of names or error dict."""
    from sova.utils.gh import resolve_gh_env
    from sova.utils.shell import run

    env = await resolve_gh_env(cfg.github_user)
    result = await run("gh", "label", "list", "--repo", cfg.github_repo, "--json", "name", env=env)

    if not result.success:
        error_msg = result.stderr.strip()
        if "not found" in error_msg or "not installed" in error_msg:
            return {"error": "GitHub CLI not available or not authenticated"}
        return {"error": f"Failed to fetch labels: {error_msg}"}

    import json

    try:
        existing_labels = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"error": "Failed to parse GitHub CLI output"}

    return {label["name"] for label in existing_labels}


async def _create_single_label(label: dict, cfg, env) -> tuple[bool, str | None]:
    """Create a single label, returning (success, error_msg)."""
    import re

    from sova.utils.shell import run

    color = label.get("color", "")
    if not re.match(r"^[0-9a-fA-F]{6}$", color):
        return (False, f"{label['name']}: Invalid color format (expected 6-char hex)")

    result = await run(
        "gh",
        "label",
        "create",
        label["name"],
        "--repo",
        cfg.github_repo,
        "--color",
        color,
        "--description",
        label.get("description", ""),
        env=env,
    )

    if result.success:
        return (True, None)

    error_msg = result.stderr.strip()[:_MAX_ERROR_MESSAGE_LENGTH]
    if "403" in error_msg or "permission" in error_msg.lower():
        return (False, f"{label['name']}: Permission denied (requires write access)")
    return (False, f"{label['name']}: {error_msg}")


def _build_label_response(results: list[tuple[bool, str | None]]) -> dict:
    """Build response dict from label creation results."""
    created = sum(1 for success, _ in results if success)
    errors = [err for success, err in results if not success and err]

    response = {"created": created}
    if errors:
        if len(errors) > _MAX_LABEL_ERRORS:
            response["errors"] = errors[:_MAX_LABEL_ERRORS]
            response["errors_truncated"] = len(errors) - _MAX_LABEL_ERRORS
        else:
            response["errors"] = errors

    return response


@router.get(
    "/settings/labels/audit",
    responses={
        400: {"description": "GitHub adapter not configured"},
        500: {"description": "Failed to audit labels"},
    },
)
async def audit_labels() -> dict:
    """Audit repository labels against SOVA's required set.

    Returns missing labels and error information if GitHub CLI is not available.
    """
    from sova.config.loader import load_config

    try:
        project_dir = get_project_dir()
        cfg = load_config(project_dir)
        _validate_github_config(cfg)

        existing_or_error = await _fetch_existing_labels(cfg)

        if isinstance(existing_or_error, dict):
            return {**existing_or_error, "missing": []}

        existing_names = existing_or_error
        missing = [label for label in _REQUIRED_LABELS if label["name"] not in existing_names]

        return {"missing": missing, "total_required": len(_REQUIRED_LABELS)}

    except HTTPException:
        raise
    except Exception as exc:
        log.warning("settings.labels.audit.error", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to audit labels: {exc}") from exc


@router.post(
    "/settings/labels/create",
    responses={
        400: {"description": "GitHub adapter not configured"},
        500: {"description": "Failed to create labels"},
    },
)
async def create_missing_labels() -> dict:
    """Create all missing SOVA labels in the repository.

    Returns the count of labels created and any errors encountered.
    """
    from sova.config.loader import load_config
    from sova.utils.gh import resolve_gh_env

    try:
        project_dir = get_project_dir()
        cfg = load_config(project_dir)
        _validate_github_config(cfg)

        # Fetch existing labels
        existing_or_error = await _fetch_existing_labels(cfg)
        if isinstance(existing_or_error, dict):
            raise HTTPException(status_code=500, detail=existing_or_error["error"])

        # Find missing labels
        existing_names = existing_or_error
        missing = [label for label in _REQUIRED_LABELS if label["name"] not in existing_names]

        if not missing:
            return {"created": 0, "message": "All required labels already exist"}

        # Create missing labels in parallel with controlled concurrency
        env = await resolve_gh_env(cfg.github_user)
        semaphore = asyncio.Semaphore(_LABEL_CREATE_CONCURRENCY)

        async def create_with_limit(label: dict) -> tuple[bool, str | None]:
            async with semaphore:
                return await _create_single_label(label, cfg, env)

        results = await asyncio.gather(*[create_with_limit(label) for label in missing])

        return _build_label_response(results)

    except HTTPException:
        raise
    except Exception as exc:
        log.warning("settings.labels.create.error", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to create labels: {exc}") from exc
