"""Project registry: maps slugs to project paths.

Stores registered projects in ~/.config/sova/projects.json.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from sova.utils.files import read_json_or_none, write_text_atomic

_REGISTRY_DIR = Path.home() / ".config" / "sova"
_REGISTRY_FILE = _REGISTRY_DIR / "projects.json"


class RegistryUnreadableError(RuntimeError):
    """The project registry file exists but cannot be read as a JSON object.

    A ``RuntimeError`` subclass so existing ``except RuntimeError`` callers keep working. It carries
    the file's path so a caller (the dashboard routes) can name the file the user has to fix.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(f"Project registry {path} is unreadable or malformed; fix or remove it, then retry")
        self.path = path


# One text for every refusal (hub startup, register_project(), the dashboard's 409), so each says the same thing.
SHARED_DATABASE_MESSAGE = (
    "SOVA_DATABASE_URL is set and more than one project is registered, so every project would share one "
    "database. Unset SOVA_DATABASE_URL for the hub, or run one single-project server per project, each with "
    "its own SOVA_DATABASE_URL (sova server start --project <dir>)."
)


class SharedDatabaseError(RuntimeError):
    """``SOVA_DATABASE_URL`` is set while more than one project is (or would be) registered.

    The variable takes precedence over each project's own ``.claude/sova.db`` (``_get_database_url()``), so every
    project would read and write one shared database. Raised by ``check_database_isolation()`` for the hub's
    startup and for ``register_project()``, so both refuse with the same message.
    """


def check_database_isolation(project_count: int | None = None) -> None:
    """Refuse a registry of more than one project while ``SOVA_DATABASE_URL`` is set.

    ``project_count`` defaults to the number registered now; ``register_project()`` passes the count after the
    registration it is about to make. An empty variable counts as unset, as ``_get_database_url()`` reads it.
    """
    if not os.environ.get("SOVA_DATABASE_URL"):
        return
    count = project_count if project_count is not None else len(_load())
    if count > 1:
        raise SharedDatabaseError(SHARED_DATABASE_MESSAGE)


@dataclass
class ProjectEntry:
    """Extended project registry entry with fleet metadata."""

    path: str
    fleet_priority: int = 0


# Held across a whole load-modify-save sequence, so two registrations (the dashboard runs one in a
# worker thread) cannot each load the same file and have the later save drop the other's entry.
# Never taken inside _load_entries() or _save_entries(), so it is never acquired twice in one thread.
_REGISTRY_WRITE_LOCK = threading.Lock()


def _load_raw() -> dict[str, str | dict]:
    """Load raw registry data (may contain old or new format entries)."""
    data = read_json_or_none(_REGISTRY_FILE)
    return data if isinstance(data, dict) else {}


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


def registry_readable() -> bool:
    """True when the registry file is absent or holds a JSON object; False when it exists but cannot be read.

    ``list_projects()`` reads an unreadable file as empty, which is right for a lookup but cannot tell "no project
    registered" from "the registry is corrupt". The hub's ``/api/health`` asks this to report the second case.
    """
    return not _REGISTRY_FILE.exists() or isinstance(read_json_or_none(_REGISTRY_FILE), dict)


def _load_entries_for_update() -> dict[str, ProjectEntry]:
    """Load entries ahead of a rewrite, refusing to replace a registry that exists but cannot be read.

    ``_load_entries()`` reads an unreadable file (non-UTF-8, truncated, or not a JSON object) as
    empty, which is right for a lookup but would make the following save drop every existing
    registration.
    """
    if not registry_readable():
        raise RegistryUnreadableError(_REGISTRY_FILE)
    return _load_entries()


def _save_entries(entries: dict[str, ProjectEntry]) -> None:
    """Save registry in the new format with full entry data."""
    _REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    data: dict[str, dict] = {}
    for slug, entry in entries.items():
        data[slug] = {"path": entry.path, "fleet_priority": entry.fleet_priority}
    # Atomic, not write_text(): a process killed mid-write would leave a truncated file, which the
    # refusal in _load_entries_for_update() would then turn into a failure on every later mutation.
    write_text_atomic(_REGISTRY_FILE, json.dumps(data, indent=2) + "\n")


class InvalidSlugError(ValueError):
    """A requested project slug holds no safe character, so no slug can be derived from it."""


def _validate_slug(slug: str) -> str:
    """Validate a slug contains only safe characters (alphanumeric, hyphens)."""
    sanitized = re.sub(r"[^a-z0-9-]", "", slug.lower())
    if not sanitized:
        raise InvalidSlugError(f"Invalid slug: {slug!r}")
    return sanitized


def _slugify(name: str) -> str:
    """Convert a directory name to a URL-safe slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "project"


def _validate_project_path(path: Path) -> Path:
    """Resolve and validate a project path is a real directory (no traversal)."""
    # resolve() IS the validation; is_dir() check follows
    resolved = path.resolve()  # NOSONAR
    # loopback-only dashboard (architecture.md); disclosed only to the submitter
    if not resolved.is_dir():  # NOSONAR
        raise ValueError(f"Not a directory: {resolved}")
    return resolved


def register_project(path: Path, slug: str | None = None) -> str:
    """Register a project. Returns the slug used.

    If slug is None, auto-generates from directory name. A slug already taken by a different
    path gets a numeric suffix. Raises InvalidSlugError for a slug with no safe character, and
    ValueError for a path that is not a directory. Raises ``SharedDatabaseError`` when the new entry
    would make a second project while ``SOVA_DATABASE_URL`` is set; re-registering an already-registered
    path stays a no-op.
    """
    path = _validate_project_path(path)

    with _REGISTRY_WRITE_LOCK:
        entries = _load_entries_for_update()
        slug = slug or _slugify(path.name)
        slug = _validate_slug(slug)

        if slug in entries:
            # path was validated at registration time
            existing = Path(entries[slug].path).resolve()  # NOSONAR
            if existing == path:
                return slug  # preserve existing entry (fleet_priority, etc.)
            base = slug
            n = 2
            while f"{base}-{n}" in entries:
                n += 1
            slug = f"{base}-{n}"

        check_database_isolation(len(entries) + 1)
        entries[slug] = ProjectEntry(path=str(path))
        _save_entries(entries)
        return slug


def unregister_project(slug: str) -> bool:
    """Remove a project from the registry. Returns True if it existed."""
    with _REGISTRY_WRITE_LOCK:
        entries = _load_entries_for_update()
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
    # path was validated at registration time; is_dir() check follows
    resolved = Path(path_str).resolve()  # NOSONAR
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
    with _REGISTRY_WRITE_LOCK:
        entries = _load_entries_for_update()
        if slug not in entries:
            return False
        entries[slug].fleet_priority = priority
        _save_entries(entries)
        return True
