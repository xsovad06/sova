"""Settings service -- config viewing/editing, invariants, personas."""

from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from sova.utils.logging import get_logger

log = get_logger(component="dashboard.settings")

# One lock per project, serializing the validate-then-persist sequence in
# update_config() within this process. Without it, two concurrent saves (e.g.
# llm.provider="ollama" and llm.model="") can both validate against the same
# stale snapshot and both pass, leaving an unloadable combination persisted.
# This closes the race for the common case of one dashboard process serving
# concurrent requests; it does not provide cross-process serialization.
_update_locks: dict[str, asyncio.Lock] = {}


def _get_update_lock(project_dir: Path | None) -> asyncio.Lock:
    """Return the per-project lock guarding update_config's validate+persist sequence."""
    key = str((project_dir or Path.cwd()).resolve())
    lock = _update_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _update_locks[key] = lock
    return lock


def get_config(project_dir: Path | None = None) -> dict:
    """Load project config as a flat dict for the settings page."""
    from sova.config.loader import load_config

    try:
        cfg = load_config(project_dir)
    except Exception:  # noqa: BLE001 (config may fail for many reasons (missing file, bad TOML, import errors))
        log.warning("settings.config_load_failed", project_dir=str(project_dir), exc_info=True)
        return {"_error": "No configuration found"}

    # Flatten the config into displayable key-value pairs
    result: dict = {}
    _flatten_dict("", cfg.model_dump(), result)
    return result


def _flatten_dict(prefix: str, obj: dict, result: dict, registered: frozenset[str] | None = None) -> None:
    """Recursively flatten a nested dict into dotted keys.

    Stops recursing when ``full_key`` is a registered setting leaf (e.g.
    ``triage.labels``, ``roles.nicknames``) so object-valued settings stay
    intact.  Only intermediate containers (e.g. ``external_reviews.sonarcloud``)
    are expanded further.
    """
    if registered is None:
        from sova.dashboard.settings_meta import _META_BY_KEY

        registered = frozenset(_META_BY_KEY)

    for key, value in obj.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and full_key not in registered:
            _flatten_dict(full_key, value, result, registered)
        else:
            result[full_key] = value


def get_config_file_path(project_dir: Path | None = None) -> Path:
    """Get the path to sova.toml for this project."""
    if project_dir is None:
        project_dir = Path.cwd()
    return project_dir / "sova.toml"


async def update_config(project_dir: Path | None = None, *, key: str, value: str) -> dict:
    """Update a single config key in the DB and sova.toml.

    Writes to the DB first (authoritative store read by load_config),
    then best-effort updates sova.toml for human readability.
    Only registered settings (present in settings_meta) can be updated.
    """
    from sova.dashboard.settings_meta import _META_BY_KEY

    meta = _META_BY_KEY.get(key)
    if meta is None:
        return {"error": f"Unknown setting: '{key}'"}

    if meta.value_type == "secret" and _is_masked_secret(value):
        # The UI submitted the masked placeholder unchanged; do not overwrite
        # the stored key with the mask string.
        return {"status": "ok", "key": key, "value": value, "unchanged": True}

    validation_error = _validate_value_type(key, value)
    if validation_error:
        return {"error": validation_error}

    cast = _cast_value(value)

    async with _get_update_lock(project_dir):
        consistency_error = _validate_config_consistency(project_dir, key, cast)
        if consistency_error:
            return {"error": consistency_error}

        db_ok = await _save_setting_to_db(project_dir, key, cast)
        if not db_ok:
            log.warning("settings.db_write_failed", key=key)

    # Secrets are DB-only: never written to sova.toml in plaintext.
    if meta.value_type == "secret":
        if not db_ok:
            return {"error": "Failed to persist setting (DB unavailable)"}
    else:
        toml_ok = _save_setting_to_toml(project_dir, key, cast)
        if not toml_ok:
            log.debug("settings.toml_write_skipped", key=key)
        if not db_ok and not toml_ok:
            return {"error": "Failed to persist setting (neither DB nor TOML available)"}

    return {"status": "ok", "key": key, "value": value}


async def _save_setting_to_db(project_dir: Path | None, key: str, value: object) -> bool:
    """Persist a setting to the database. Returns True on success."""
    try:
        from sova.config.db_loader import save_setting
        from sova.db.session import get_session

        async with await get_session(project_dir=project_dir) as session, session.begin():
            await save_setting(session, key, value)
        return True
    except (OSError, RuntimeError, SQLAlchemyError):
        log.warning("settings.db_save_failed", key=key, exc_info=True)
        return False


def _save_setting_to_toml(project_dir: Path | None, key: str, value: object) -> bool:
    """Best-effort update of sova.toml. Returns True on success."""
    toml_path = get_config_file_path(project_dir)
    if not toml_path.exists():
        return False

    try:
        import tomlkit

        doc = tomlkit.parse(toml_path.read_text())
        parts = key.split(".")
        target = doc
        for part in parts[:-1]:
            if part not in target:
                target[part] = tomlkit.table()
            target = target[part]

        target[parts[-1]] = value
        tmp_path = toml_path.with_suffix(".toml.tmp")
        tmp_path.write_text(tomlkit.dumps(doc))
        tmp_path.replace(toml_path)
        return True
    except ImportError:
        log.debug("tomlkit not available")
        return False
    except Exception:  # noqa: BLE001 (tomlkit surfaces arbitrary parse errors; the write is reported as failed)
        log.warning("settings.toml_write_failed", exc_info=True)
        return False


_SECRET_MASK_CHARS = frozenset("*•·")


def _is_masked_secret(value: str) -> bool:
    """Return True when a secret value is only mask characters (bullets/asterisks).

    Such a value means the UI round-tripped the masked placeholder without the
    user typing a new secret, so it must not overwrite the stored value.
    """
    stripped = value.strip()
    return bool(stripped) and all(ch in _SECRET_MASK_CHARS for ch in stripped)


def _validate_value_type(key: str, value: str) -> str | None:
    """Validate the value against the expected type from settings metadata.

    Returns an error message string if invalid, None if valid.
    """
    from sova.dashboard.settings_meta import _META_BY_KEY

    meta = _META_BY_KEY.get(key)
    if meta is None:
        return None

    # Validate against allowed options if present
    if meta.options and value not in meta.options:
        return f"'{key}' must be one of {meta.options}, got '{value}'"

    if meta.value_type == "number":
        stripped = value.strip()
        if not stripped:
            return f"'{key}' expects a number, got '{value}'"
        try:
            float(stripped)
        except ValueError:
            return f"'{key}' expects a number, got '{value}'"
    elif meta.value_type == "boolean":
        if value.lower() not in ("true", "false"):
            return f"'{key}' expects true or false, got '{value}'"

    return None


def _validate_config_consistency(project_dir: Path | None, key: str, value: object) -> str | None:
    """Reject a value that would make the whole project config unloadable.

    _validate_value_type only inspects the edited field in isolation, so a
    cross-field rule (llm.provider="ollama" requires an explicit llm.model)
    passes it, gets persisted, and then every load_config() call for the
    project raises: agents stop spawning and the settings page can no longer
    render the field needed to undo it.

    Fails open. Only errors whose location touches the edited section are
    reported, so an unrelated pre-existing config problem can never block an
    unrelated save.
    """
    from pydantic import ValidationError

    from sova.config.loader import load_config
    from sova.config.models import ProjectConfig

    try:
        data = load_config(project_dir).model_dump()
    except Exception:  # noqa: BLE001 (fails open: any load error skips consistency check)
        return None

    section, _, field = key.partition(".")
    if field:
        target = data.get(section)
        if not isinstance(target, dict) or field not in target:
            return None
        target[field] = value
    elif key in data:
        data[key] = value
    else:
        return None

    try:
        ProjectConfig(**data)
    except ValidationError as exc:
        related = [
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
            for err in exc.errors()
            if err.get("loc") and str(err["loc"][0]) == section
        ]
        if related:
            return f"'{key}' rejected: {'; '.join(related)}"
    except Exception:  # noqa: BLE001 (fails open: any construction error skips consistency check)
        return None
    return None


def _cast_value(value: str) -> object:
    """Try to cast a string value to the appropriate type."""
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def list_invariants(project_dir: Path | None = None) -> list[dict]:
    """List invariant scripts in the project."""
    if project_dir is None:
        project_dir = Path.cwd()

    inv_dir = project_dir / "invariants"
    if not inv_dir.is_dir():
        return []

    result = []
    for f in sorted(inv_dir.iterdir()):
        if f.is_file() and not f.name.startswith("."):
            result.append(
                {
                    "name": f.name,
                    "path": str(f),
                    "executable": f.stat().st_mode & 0o111 != 0,
                }
            )
    return result


def list_personas(project_dir: Path | None = None) -> list[dict]:
    """List available personas for the project."""
    if project_dir is None:
        project_dir = Path.cwd()

    personas_dir = project_dir / "personas"
    if not personas_dir.is_dir():
        return []

    result = []
    for f in sorted(personas_dir.iterdir()):
        if f.suffix == ".md" and not f.name.startswith("."):
            result.append(
                {
                    "name": f.stem,
                    "path": str(f),
                }
            )
    return result


def get_detected_persona(project_dir: Path | None = None) -> str | None:
    """Detect the project's persona based on tech stack."""
    if project_dir is None:
        project_dir = Path.cwd()

    try:
        from sova.knowledge.personas import detect_persona

        return detect_persona(project_dir)
    except (OSError, ValueError):
        log.warning("settings.persona_detect_failed", project_dir=str(project_dir), exc_info=True)
        return None
