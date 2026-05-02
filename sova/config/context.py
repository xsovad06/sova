"""Per-request project context via contextvars.

Used by multi-project mode to scope services to the active project.
"""

from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path

_project_dir_var: ContextVar[Path | None] = ContextVar("project_dir", default=None)
_project_slug_var: ContextVar[str | None] = ContextVar("project_slug", default=None)


def set_project_context(project_dir: Path, slug: str) -> None:
    """Set the active project for the current request context."""
    _project_dir_var.set(project_dir)
    _project_slug_var.set(slug)


def get_project_dir() -> Path | None:
    """Get the project directory for the current request context."""
    return _project_dir_var.get()


def get_project_slug() -> str | None:
    """Get the project slug for the current request context."""
    return _project_slug_var.get()


def clear_project_context() -> None:
    """Reset the project context (call after request completes)."""
    _project_dir_var.set(None)
    _project_slug_var.set(None)
