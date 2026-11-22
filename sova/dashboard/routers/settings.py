"""Settings router: config, invariants, detected stack, installation status."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from sova.config.context import get_project_dir, resolve_project_dir, resolve_project_dir_or_none
from sova.config.loader import load_config
from sova.config.models import ProjectConfig
from sova.dashboard.config_errors import load_config_or_http
from sova.dashboard.security import require_same_origin_csrf
from sova.dashboard.services import github_labels_service, model_profile_service, settings_service
from sova.dashboard.services.agent_pool import get_default_project_dir
from sova.dashboard.services.settings_service import (
    apply_config_update,
    config_value_to_str,
    reload_pinned,
)
from sova.dashboard.settings_meta import get_meta
from sova.db.session import get_session
from sova.llm.errors import ComponentMisconfiguredError
from sova.utils.logging import get_logger

router = APIRouter(tags=["settings"])
log = get_logger(component="dashboard.settings.router")


def _validate_github_config(cfg: ProjectConfig) -> None:
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
    # Scopes a keyring-backed secret write (e.g. llm.api_key) to the
    # provider the key is *for*, e.g. the Connections page's per-provider
    # card. Empty falls back to the currently-active provider (the
    # settings page's generic secret edit, with no per-card context).
    provider: str = ""


class SecretKeyRequest(BaseModel):
    key: str


def _settings_project_dir() -> Path | None:
    """The project every route in this router acts on: the request's project in multi-project mode, else the startup
    project in single-project mode. In single-project mode this is the startup project, never ``None``, which
    ``load_config()`` reads as the server's working directory while a save goes to the startup project's database:
    a control's read-back would show defaults. It is ``None`` only in multi-project mode with no project selected."""
    return resolve_project_dir_or_none(get_project_dir(), default_project_dir=get_default_project_dir())


def _load_cfg(project_dir: Path | None = None) -> ProjectConfig:
    """The (cached) config of *project_dir*, else of the project this router acts on."""
    return load_config(project_dir if project_dir is not None else _settings_project_dir())


@router.get("/settings/config")
async def get_config() -> dict:
    """Get the current project configuration (flat, for backward compat).

    A config that cannot be loaded is not an error here: ``settings_service`` answers it with its ``_error``
    payload, so anything that still raises is a bug and reaches the catch-all handler as a 500.
    """
    return {"config": settings_service.get_config(_settings_project_dir())}


@router.get("/settings/config/grouped")
async def get_config_grouped() -> dict:
    """Get configuration organized into labeled groups with descriptions.

    Each setting also carries its default, whether a value is stored for this project and the ``SOVA_`` variable
    overriding it, and ``leftover`` lists stored rows no setting reads (see ``get_grouped_settings()``).
    """
    from sova.llm import keyring_store

    payload = settings_service.get_grouped_settings(_settings_project_dir())
    payload["keyring_available"] = keyring_store.is_keyring_available()
    return payload


@router.post(
    "/settings/config/migrate-secret",
    responses={500: {"description": "Internal error"}},
    dependencies=[Depends(require_same_origin_csrf)],
)
async def migrate_secret_to_keyring(req: SecretKeyRequest) -> dict:
    """Move an already plaintext-stored secret into the OS keyring.

    Explicit action only, triggered by the settings page "Move to keychain"
    button; never run automatically.
    """
    project_dir = _settings_project_dir()
    return await settings_service.migrate_secret_to_keyring(project_dir, req.key)


class _AvailabilityCheckable(Protocol):
    """Anything exposing the ``(available, detail)`` readiness probe contract."""

    async def check_available(self) -> tuple[bool, str]: ...


async def _check_component_availability(
    result_key: str,
    log_prefix: str,
    resolve_name: Callable[[ProjectConfig], str],
    build: Callable[[ProjectConfig], _AvailabilityCheckable],
    component_label: str,
) -> dict:
    """Shared body for the LLM-provider and agent-runtime connection tests.

    Both load the project config, build a component from it, and report that
    component's ``check_available()`` result. A component that reports itself
    unavailable is a normal answer (``{ok: false, <result_key>: ..., detail: ...}``).
    A failure to run the check is reported by category, each with a fixed detail except a misconfiguration (the
    real exception is logged). An unloadable project config is answered as ``config_errors.config_http_error()``
    does (422 naming the fields, 400, or 503). A component the config cannot build is a 400 carrying the
    ``ComponentMisconfiguredError`` message (author-written, such as an unknown provider), or a 502 for any other
    build failure. A probe that fails is a 502 naming the component unreachable.
    """
    cfg = load_config_or_http(_load_cfg, event=f"{log_prefix}.config_error")

    name = resolve_name(cfg)
    try:
        component = build(cfg)
    except ComponentMisconfiguredError as exc:
        log.info(f"{log_prefix}.create_failed", name=name, exc_info=True)
        raise HTTPException(status_code=400, detail=exc.user_message) from exc
    except Exception as exc:  # noqa: BLE001 (any other construction failure becomes the fixed 502 below, logged)
        log.info(f"{log_prefix}.create_failed", name=name, exc_info=True)
        raise HTTPException(status_code=502, detail=f"{component_label} check failed") from exc

    try:
        available, detail = await component.check_available()
    except Exception as exc:  # noqa: BLE001 (any probe failure becomes the fixed 502 below, logged)
        log.info(f"{log_prefix}.check_failed", name=name, exc_info=True)
        raise HTTPException(status_code=502, detail=f"{component_label} unreachable") from exc

    return {"ok": bool(available), result_key: name, "detail": detail}


@router.post(
    "/settings/llm/test-connection",
    responses={
        400: {"description": "LLM provider is misconfigured (the detail says what to fix)"},
        422: {"description": "The project configuration failed validation"},
        502: {"description": "LLM provider unreachable or its check failed"},
        503: {"description": "Project configuration could not be loaded"},
    },
)
async def test_llm_connection() -> dict:
    """Validate the configured LLM provider credentials.

    Routes through the provider abstraction (``create_provider`` +
    ``check_available``) so it stays provider-agnostic. A failure is reported by
    category (see ``_check_component_availability``).
    """
    from sova.llm.provider import create_provider

    return await _check_component_availability(
        "provider",
        "settings.llm.test",
        lambda cfg: cfg.llm.provider,
        lambda cfg: create_provider(cfg.llm),
        "LLM provider",
    )


@router.post(
    "/settings/agent-runtime/test-connection",
    responses={
        400: {"description": "Agent runtime is misconfigured (the detail says what to fix)"},
        422: {"description": "The project configuration failed validation"},
        502: {"description": "Agent runtime unreachable or its check failed"},
        503: {"description": "Project configuration could not be loaded"},
    },
)
async def test_agent_runtime_connection() -> dict:
    """Validate the configured agent runtime CLI (claude-code, aider, or codex).

    Mirrors ``/settings/llm/test-connection``: routes through the runtime
    abstraction (``create_runtime`` + ``check_available``) so it stays
    runtime-agnostic, and a failure is reported by category (see ``_check_component_availability``).
    """
    from sova.ipc.runtime import create_runtime

    return await _check_component_availability(
        "runtime",
        "settings.runtime.test",
        lambda cfg: cfg.agent.runtime,
        lambda cfg: create_runtime(cfg.agent.runtime, codex=cfg.codex),
        "Agent runtime",
    )


def _decimal_to_float(obj: object) -> object:
    """Convert Decimal values to float for JSON serialization."""
    from decimal import Decimal

    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _decimal_to_float(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimal_to_float(item) for item in obj]
    return obj


@router.get("/settings/llm/cost-projection")
async def llm_cost_projection() -> dict:
    """Return a lightweight monthly cost projection from recent usage.

    A database failure degrades to ``{"insufficient_data": true}`` (logged), so the widget always renders.
    """
    from sova.dashboard.services import cost_service

    project_dir = _settings_project_dir()
    try:
        async with await get_session(project_dir=project_dir) as session:
            projection = await cost_service.get_monthly_projection(session)
    except (SQLAlchemyError, OSError):
        # A widget on the settings page: a database failure costs it its numbers, never the page a 500.
        log.warning("settings.llm.cost_projection.error", exc_info=True)
        return {"insufficient_data": True}

    return _decimal_to_float(
        {
            "insufficient_data": projection["insufficient_data"],
            "window_days": projection["window_days"],
            "observed_days": projection["observed_days"],
            "window_total_usd": projection["window_total_usd"],
            "daily_avg_usd": projection["daily_avg_usd"],
            "projected_monthly_usd": projection["projected_monthly_usd"],
        }
    )


@router.post(
    "/settings/config",
    responses={
        404: {"description": "No such setting"},
        409: {"description": "A SOVA_ environment variable sets this value"},
        422: {"description": "The value was rejected, or the configured database cannot be read back"},
        503: {"description": "The project database could not be written; retry"},
    },
    dependencies=[Depends(require_same_origin_csrf)],
)
async def update_config(req: ConfigUpdateRequest) -> dict:
    """Update a single configuration key."""
    project_dir = _settings_project_dir()
    value_str = config_value_to_str(req.value)
    result = await settings_service.update_config(project_dir, key=req.key, value=value_str, provider=req.provider)
    if result.get("unreadable_database"):
        # Kept as a 422 (not the helper's 503): the stored values cannot be read back, retrying does not help.
        raise HTTPException(status_code=422, detail=result["error"])
    _raise_for_write_error(result)
    if result.get("status") == "ok":
        await apply_config_update(project_dir, settings_service.reload_key(req.key), result)
    return result


class ConfigResetRequest(BaseModel):
    key: str


@router.post(
    "/settings/config/reset",
    responses={
        404: {"description": "No such setting and no stored value under that key"},
        409: {"description": "A SOVA_ environment variable sets this value, so there is nothing to reset here"},
        422: {"description": "Not reset: the default would make the project config invalid"},
        500: {"description": "Internal error"},
        503: {"description": "The project database could not be read or written, so nothing was removed; retry"},
    },
    dependencies=[Depends(require_same_origin_csrf)],
)
async def reset_config(req: ConfigResetRequest) -> dict:
    """Delete the value stored for one setting, so its default applies again (Settings, "Changed only")."""
    project_dir = _settings_project_dir()
    result = await settings_service.reset_config(project_dir, key=req.key)
    _raise_for_write_error(result)
    # One route (``llm.routing.<step>``) reloads what the whole table does, as the Models view's step clear does.
    setting = settings_service.reload_key(req.key)
    if get_meta(setting) is not None:
        await apply_config_update(project_dir, setting, result)
    return result


@router.get(
    "/settings/models/map",
    responses={
        400: {"description": "The project config is missing or unparsable"},
        422: {"description": "The project configuration failed validation (the detail names the fields)"},
        500: {"description": "Internal error"},
        503: {"description": "Project configuration could not be loaded"},
    },
)
async def get_model_map() -> dict:
    """Which model each role and step runs on and why, with fallbacks, and the settings stored as a short name
    (Settings, Models view). An invalid config is a 422 naming the fields, so the view shows what to fix."""
    # The service loads the (cached) config itself; loading it here first turns a failure into the shared answer.
    await asyncio.to_thread(load_config_or_http, _load_cfg, event="settings.models_map.config_load_failed")
    return await settings_service.get_model_map_payload(_settings_project_dir())


class StepModelRequest(BaseModel):
    step: str
    # Empty deletes the step's row, so it uses its role's model again.
    model: str = ""


@router.post(
    "/settings/models/step",
    responses={
        404: {"description": "Not a task type or an issue size"},
        409: {"description": "A SOVA_ environment variable sets llm.routing"},
        422: {"description": "The step's model was not saved"},
        503: {"description": "The database could not be written or read back; retry"},
        500: {"description": "Internal error"},
    },
    dependencies=[Depends(require_same_origin_csrf)],
)
async def set_step_model(req: StepModelRequest) -> dict:
    """Set the model one step runs on (writes ``llm.routing.<step>``), or with an empty model delete that row."""
    project_dir = _settings_project_dir()
    result = await settings_service.set_step_model(project_dir, step=req.step, model=req.model)
    _raise_for_write_error(result)
    await apply_config_update(project_dir, "llm.routing", result)
    return result


class PinRequest(BaseModel):
    # The settings to pin; None pins every setting stored as a short name (Pin all).
    keys: list[str] | None = None


@router.post(
    "/settings/models/pin",
    responses={
        409: {"description": "A setting changed since it was read; reload and retry"},
        422: {"description": "Nothing was pinned"},
        503: {"description": "The database could not be written or read back; retry"},
        500: {"description": "Internal error"},
    },
    dependencies=[Depends(require_same_origin_csrf)],
)
async def pin_short_names(req: PinRequest) -> dict:
    """Replace settings stored as a short model name with the exact ID each runs as today (Pin, Pin all)."""
    project_dir = _settings_project_dir()
    catalog = await settings_service.pin_catalog(project_dir)
    result = await settings_service.pin_short_names(project_dir, keys=req.keys, catalog=catalog)
    try:
        _raise_for_write_error(result)
    except HTTPException:
        # A routing write that failed after the other pins were stored: those reach their components anyway.
        await reload_pinned(project_dir, result.get("committed", ()), result)
        raise
    await reload_pinned(project_dir, result.get("pinned", {}), result)
    return result


@router.post(
    "/settings/models/profile",
    responses={
        409: {"description": "The saved profile file exists but cannot be read, so it was not replaced"},
        422: {"description": "The project stores no model settings to save"},
        503: {"description": "The project database or the profile file could not be read or written"},
        500: {"description": "Internal error"},
    },
    dependencies=[Depends(require_same_origin_csrf)],
)
async def save_model_profile() -> dict:
    """Save this project's model settings as the default for new projects (``~/.config/sova/model-profile.json``)."""
    result = await asyncio.to_thread(model_profile_service.save_model_profile, _settings_project_dir())
    _raise_for_write_error(result)
    return result


def _raise_for_write_error(result: dict) -> None:
    """Answer a refused write with a client error carrying its message as ``detail``."""
    if result.get("unknown"):
        raise HTTPException(status_code=404, detail=result["error"])
    if result.get("env_var") or result.get("conflict") or result.get("unreadable_profile"):
        raise HTTPException(status_code=409, detail=result["error"])
    if result.get("unavailable") or result.get("unreadable_database"):
        raise HTTPException(status_code=503, detail=result["error"])
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])


@router.get(
    "/settings/installation/status",
    responses={
        400: {"description": "The project config is missing or unparsable"},
        422: {"description": "The project configuration failed validation"},
        500: {"description": "Internal error"},
        503: {"description": "Project configuration could not be loaded"},
    },
)
async def installation_status() -> dict[str, object]:
    """Check for available SOVA command and guideline updates."""
    from sova.agents.claude_code import ClaudeCodeAdapter
    from sova.commands.catalog import get_canonical_dir
    from sova.commands.distribution import diff_commands

    project_dir = resolve_project_dir(default_project_dir=get_default_project_dir())

    cfg = load_config_or_http(lambda: _load_cfg(project_dir), event="settings.installation.config_load_failed")

    from sova.commands.distribution import DiffResult
    from sova.commands.manifest import read_manifest

    claude_adapter = ClaudeCodeAdapter()
    canonical_dir = get_canonical_dir()
    commands_dir = claude_adapter.commands_dir(project_dir)
    rules_dir = project_dir / ".claude" / "rules"

    cmd_diff = await asyncio.to_thread(diff_commands, canonical_dir, commands_dir, cfg, adapter=claude_adapter)

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


@router.get(
    "/settings/installation/diff",
    responses={
        400: {"description": "No active project, or the project config is missing or unparsable"},
        422: {"description": "The project configuration failed validation"},
        500: {"description": "Internal error"},
        503: {"description": "Project configuration could not be loaded"},
    },
)
async def installation_diff() -> dict[str, object]:
    """Build a per-file diff (forward + reverse) for the review-changes modal."""
    project_dir = _settings_project_dir()
    if not project_dir or not project_dir.is_dir():
        raise HTTPException(status_code=400, detail="No active project")

    cfg = load_config_or_http(lambda: _load_cfg(project_dir), event="settings.installation_diff.config_load_failed")

    diff = await asyncio.to_thread(settings_service.build_installation_diff, project_dir, cfg)

    return {
        "source_available": diff.source_available,
        "files": [asdict(f) for f in diff.files],
    }


@router.get("/settings/invariants", responses={500: {"description": "Internal error"}})
async def list_invariants() -> dict:
    """List invariant scripts."""
    project_dir = _settings_project_dir()
    return {"invariants": settings_service.list_invariants(project_dir)}


@router.get("/settings/detected-stack", responses={500: {"description": "Internal error"}})
async def get_detected_stack() -> dict:
    """Return the tech stack detected from the project's marker files."""
    project_dir = _settings_project_dir()
    return {"detected": settings_service.get_detected_persona(project_dir)}


@router.get("/settings/persona", responses={500: {"description": "Internal error"}})
async def get_operations_persona():
    """Get the operations persona content and metadata."""
    from sova.oversight.persona import get_persona_info

    cfg = load_config_or_http(_load_cfg, event="settings.persona.config_load_failed")
    return get_persona_info(cfg.oversight.persona_path)


@router.post(
    "/settings/persona/open",
    responses={400: {"description": "Cannot open editor"}, 500: {"description": "Internal error"}},
)
async def open_persona_in_editor():
    """Open the operations persona file in the OS default editor.

    Fire-and-forget: spawns the editor process without waiting for it to exit.
    The response confirms the process was spawned, not that the editor opened
    successfully.
    """
    from sova.dashboard.services.editor_service import open_path_in_editor
    from sova.oversight.persona import ensure_persona_exists

    cfg = load_config_or_http(_load_cfg, event="settings.persona_open.config_load_failed")
    return await open_path_in_editor(ensure_persona_exists(cfg.oversight.persona_path))


@router.get(
    "/settings/labels/audit",
    responses={
        400: {"description": "GitHub adapter not configured, or the project config is unreadable"},
        422: {"description": "The project configuration failed validation"},
        502: {"description": "Failed to list labels"},
        503: {"description": "Project configuration could not be loaded"},
    },
)
async def audit_labels() -> dict:
    """Audit repository labels against SOVA's required set.

    Returns the missing labels, or a 502 when the labels cannot be listed (GitHub CLI missing or failing).
    """
    cfg = load_config_or_http(_load_cfg, event="settings.labels.config_load_failed")
    _validate_github_config(cfg)

    result = await github_labels_service.compute_missing_labels(cfg)
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    return result


@router.post(
    "/settings/labels/create",
    responses={
        400: {"description": "GitHub adapter not configured, or the project config is unreadable"},
        422: {"description": "The project configuration failed validation"},
        502: {"description": "Failed to list labels, or at least one label could not be created (see detail.errors)"},
        503: {"description": "Project configuration could not be loaded"},
    },
)
async def create_missing_labels() -> dict:
    """Create all missing SOVA labels in the repository.

    Returns the count of labels created. When the labels cannot be listed, or any creation fails, the answer is a
    502: the list failure carries its fixed message as ``detail``, a creation failure a ``detail`` of
    ``{message, created, errors, errors_truncated}`` (the labels that did get created stay created, so a client
    reloads its status; each error is one of the service's fixed per-label reasons, never ``gh`` output).
    """
    cfg = load_config_or_http(_load_cfg, event="settings.labels.config_load_failed")
    _validate_github_config(cfg)

    result = await github_labels_service.create_missing_labels(cfg)
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    if result.get("errors"):
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Some labels could not be created",
                "created": result["created"],
                "errors": result["errors"],
                "errors_truncated": result.get("errors_truncated", 0),
            },
        )
    return result
