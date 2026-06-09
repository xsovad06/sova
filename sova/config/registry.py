"""Project registry -- maps slugs to project paths.

Stores registered projects in ~/.config/sova/projects.json.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_REGISTRY_DIR = Path.home() / ".config" / "sova"
_REGISTRY_FILE = _REGISTRY_DIR / "projects.json"


def _load() -> dict[str, str]:
    if not _REGISTRY_FILE.exists():
        return {}
    try:
        return json.loads(_REGISTRY_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict[str, str]) -> None:
    _REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
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
    resolved = path.resolve()
    if not resolved.is_dir():
        raise ValueError(f"Not a directory: {resolved}")
    return resolved


def register_project(path: Path, slug: str | None = None) -> str:
    """Register a project. Returns the slug used.

    If slug is None, auto-generates from directory name.
    Raises ValueError if slug is already taken by a different path.
    """
    path = _validate_project_path(path)

    data = _load()
    slug = slug or _slugify(path.name)
    slug = _validate_slug(slug)

    if slug in data:
        existing = Path(data[slug]).resolve()
        if existing != path:
            base = slug
            n = 2
            while f"{base}-{n}" in data:
                n += 1
            slug = f"{base}-{n}"

    data[slug] = str(path)
    _save(data)
    return slug


def unregister_project(slug: str) -> bool:
    """Remove a project from the registry. Returns True if it existed."""
    data = _load()
    if slug not in data:
        return False
    del data[slug]
    _save(data)
    return True


def list_projects() -> dict[str, str]:
    """Return all registered projects as {slug: path}."""
    return _load()


def get_project_path(slug: str) -> Path | None:
    """Get the path for a registered project slug."""
    slug = _validate_slug(slug)
    data = _load()
    path_str = data.get(slug)
    if path_str is None:
        return None
    resolved = Path(path_str).resolve()
    if not resolved.is_dir():
        return None
    return resolved


def has_projects() -> bool:
    """Check if any projects are registered."""
    return bool(_load())
