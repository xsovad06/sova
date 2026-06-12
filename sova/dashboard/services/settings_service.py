"""Settings service -- config viewing/editing, invariants, personas."""

from __future__ import annotations

from pathlib import Path

from sova.utils.logging import get_logger

log = get_logger(component="dashboard.settings")


def get_config(project_dir: Path | None = None) -> dict:
    """Load project config as a flat dict for the settings page."""
    from sova.config.loader import load_config

    try:
        cfg = load_config(project_dir)
    except Exception:
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


def update_config(project_dir: Path | None = None, *, key: str, value: str) -> dict:
    """Update a single config key in sova.toml using tomlkit for round-trip.

    Falls back to simple string replacement if tomlkit is not available.
    """
    toml_path = get_config_file_path(project_dir)
    if not toml_path.exists():
        return {"error": "sova.toml not found"}

    try:
        import tomlkit

        doc = tomlkit.parse(toml_path.read_text())

        # Handle dotted keys (e.g., "task_source.type")
        parts = key.split(".")
        target = doc
        for part in parts[:-1]:
            if part not in target:
                target[part] = tomlkit.table()
            target = target[part]

        # Try to cast the value appropriately
        target[parts[-1]] = _cast_value(value)
        toml_path.write_text(tomlkit.dumps(doc))
        return {"status": "ok", "key": key, "value": value}
    except ImportError:
        log.warning("tomlkit not available, skipping config update")
        return {"error": "tomlkit not installed -- cannot update config"}
    except Exception as e:
        return {"error": str(e)}


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
    except Exception:
        return None
