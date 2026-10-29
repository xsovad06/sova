"""Per-request project context via contextvars.

Used by multi-project mode to scope services to the active project.
"""

from __future__ import annotations

from collections.abc import Mapping
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


def resolve_project_dir_or_none(
    project_dir: Path | None = None,
    *,
    daemon_registry: Mapping[str, object] | None = None,
    default_project_dir: Path | None = None,
) -> Path | None:
    """Resolve the active project directory, or ``None`` when no step names one.

    The one fallback chain, in order:

    1. an explicit *project_dir* argument wins outright;
    2. the ambient per-request contextvar set by multi-project mode via
       ``set_project_context()``;
    3. if *daemon_registry* names exactly one project, that project's directory;
    4. *default_project_dir*, the single-project default set once at startup.

    The contextvar is second, not last, on purpose: in multi-project mode it carries the
    project a ``/p/{slug}/`` request is scoped to, and that has to beat the startup default,
    which names some other project. The convention that long-lived code (``WorkflowEngine``'s
    DB calls) resolves its project explicitly rather than through the ambient contextvar is
    step 1: such code passes *project_dir* and never reaches the contextvar step.

    The two optional keyword steps are opt-in so a caller with no registry or default to offer
    can still rely on the contextvar alone. Use this variant where "no project" is a state the
    caller handles (a 400, or a canonical fallback); use ``resolve_project_dir()`` where a
    usable path is always required.
    """
    if project_dir is not None:
        return project_dir
    ctx_dir = get_project_dir()
    if ctx_dir is not None:
        return ctx_dir
    if daemon_registry is not None and len(daemon_registry) == 1:
        return Path(next(iter(daemon_registry)))
    return default_project_dir


def resolve_project_dir(
    project_dir: Path | None = None,
    *,
    daemon_registry: Mapping[str, object] | None = None,
    default_project_dir: Path | None = None,
) -> Path:
    """Resolve the active project directory, always returning a usable path.

    Runs the chain documented on ``resolve_project_dir_or_none()``, then falls back to the
    current working directory as the final step when none of them names a project.
    """
    resolved = resolve_project_dir_or_none(
        project_dir, daemon_registry=daemon_registry, default_project_dir=default_project_dir
    )
    return resolved if resolved is not None else Path.cwd().resolve()
