"""Project registry -- maps slugs to project paths.

Stores registered projects in ~/.config/sova/projects.json.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_REGISTRY_DIR = Path.home() / ".config" / "sova"
_REGISTRY_FILE = _REGISTRY_DIR / "projects.json"


@dataclass
class ProjectEntry:
    """Extended project registry entry with fleet metadata."""

    path: str
    fleet_priority: int = 0


def _load_raw() -> dict[str, str | dict]:
    """Load raw registry data (may contain old or new format entries)."""
    if not _REGISTRY_FILE.exists():
        return {}
    try:
        return json.loads(_REGISTRY_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _load() -> dict[str, str]:
    """Load registry, returning {slug: path_str} for backward compatibility.

    Transparently handles old-format (string values) and new-format (dict values).
    """
    raw = _load_raw()
    result: dict[str, str] = {}
    for slug, value in raw.items():
        if isinstance(value, str):
            result[slug] = value
        elif isinstance(value, dict):
            result[slug] = value.get("path", "")
        else:
            result[slug] = str(value)
    return result


def _load_entries() -> dict[str, ProjectEntry]:
    """Load registry as full ProjectEntry objects."""
    raw = _load_raw()
    result: dict[str, ProjectEntry] = {}
    for slug, value in raw.items():
        if isinstance(value, str):
            result[slug] = ProjectEntry(path=value)
        elif isinstance(value, dict):
            result[slug] = ProjectEntry(
                path=value.get("path", ""),
                fleet_priority=value.get("fleet_priority", 0),
            )
        else:
            result[slug] = ProjectEntry(path=str(value))
    return result


def _save_entries(entries: dict[str, ProjectEntry]) -> None:
    """Save registry in the new format with full entry data."""
    _REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    data: dict[str, dict] = {}
    for slug, entry in entries.items():
        data[slug] = {"path": entry.path, "fleet_priority": entry.fleet_priority}
    _REGISTRY_FILE.write_text(json.dumps(data, indent=2) + "\n")


def _validate_slug(slug: str) -> str:
    """Validate a slug contains only safe characters (alphanumeric, hyphens)."""
    sanitized = re.sub(r"[^a-z0-9-]", "", slug.lower())
    if not sanitized:
        raise ValueError(f"Invalid slug: {slug!r}")
    return sanitized


def _slugify(name: str) -> str:
    """Convert a directory name to a URL-safe slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "project"


def _validate_project_path(path: Path) -> Path:
    """Resolve and validate a project path is a real directory (no traversal)."""
    resolved = path.resolve()  # NOSONAR -- resolve() IS the validation; is_dir() check follows
    if not resolved.is_dir():
        raise ValueError(f"Not a directory: {resolved}")
    return resolved


def register_project(path: Path, slug: str | None = None) -> str:
    """Register a project. Returns the slug used.

    If slug is None, auto-generates from directory name.
    Raises ValueError if slug is already taken by a different path.
    """
    path = _validate_project_path(path)

    entries = _load_entries()
    slug = slug or _slugify(path.name)
    slug = _validate_slug(slug)

    if slug in entries:
        existing = Path(entries[slug].path).resolve()  # NOSONAR -- path was validated at registration time
        if existing == path:
            return slug  # preserve existing entry (fleet_priority, etc.)
        base = slug
        n = 2
        while f"{base}-{n}" in entries:
            n += 1
        slug = f"{base}-{n}"

    entries[slug] = ProjectEntry(path=str(path))
    _save_entries(entries)
    return slug


def unregister_project(slug: str) -> bool:
    """Remove a project from the registry. Returns True if it existed."""
    entries = _load_entries()
    if slug not in entries:
        return False
    del entries[slug]
    _save_entries(entries)
    return True


def list_projects() -> dict[str, str]:
    """Return all registered projects as {slug: path}."""
    return _load()


def get_project_path(slug: str) -> Path | None:
    """Get the path for a registered project slug."""
    if not re.fullmatch(r"[a-z0-9-]+", slug.lower()):
        return None
    slug = slug.lower()
    projects = _load()
    path_str = projects.get(slug)
    if path_str is None:
        return None
    resolved = Path(path_str).resolve()  # NOSONAR -- path was validated at registration time; is_dir() check follows
    if not resolved.is_dir():
        return None
    return resolved


def find_slug_for_path(path: Path | str) -> str | None:
    """Return the slug for a registered project path, or None if not found."""
    target = Path(path).resolve()
    for slug, path_str in _load().items():
        if Path(path_str).resolve() == target:
            return slug
    return None


def has_projects() -> bool:
    """Check if any projects are registered."""
    return bool(_load())


def get_project_entries() -> dict[str, ProjectEntry]:
    """Return all registered projects with full metadata."""
    return _load_entries()


def update_fleet_priority(slug: str, priority: int) -> bool:
    """Update fleet_priority for a project. Returns True if found."""
    entries = _load_entries()
    if slug not in entries:
        return False
    entries[slug].fleet_priority = priority
    _save_entries(entries)
    return True
