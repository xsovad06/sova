"""Agent pool -- data models, slot management, and project-scoped agent collections.

Owns the in-memory registry of running agents per project.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from sova.dashboard.project_context import get_project_dir, get_project_slug
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.dashboard.services.output_service import OutputWriter
    from sova.ipc.control import AgentProcess

log = get_logger(component="dashboard.pool")

_DEFAULT_SLUG = "__default__"

MAX_RECENTLY_COMPLETED = 5
RECENTLY_COMPLETED_TTL = 30.0


@dataclass
class AgentState:
    """Per-agent process state (one per running agent)."""

    run_id: int
    issue: str
    role: str
    process: AgentProcess
    output_lines: deque[str] = field(default_factory=lambda: deque(maxlen=5000))
    output_writer: OutputWriter | None = None
    reader_task: asyncio.Task | None = None
    stderr_task: asyncio.Task | None = None
    started_at: float = field(default_factory=time.monotonic)
    started_at_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_result_cost: float | None = None
    pr_number: int | None = None
    project_dir: Path = field(default_factory=Path.cwd)


@dataclass
class CompletedAgent:
    """Recently completed agent kept briefly for UI transition."""

    run_id: int
    issue: str
    role: str
    status: str
    cost: float
    completed_at: float = field(default_factory=time.monotonic)


@dataclass
class ProjectAgents:
    """Per-project collection of running agents."""

    agents: dict[int, AgentState] = field(default_factory=dict)
    recently_completed: deque[CompletedAgent] = field(
        default_factory=lambda: deque(maxlen=MAX_RECENTLY_COMPLETED),
    )
    max_concurrent: int = 3
    project_dir: Path = field(default_factory=Path.cwd)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_projects: dict[str, ProjectAgents] = {}
_default_project_dir: Path | None = None


def _get_project_agents(slug: str | None = None) -> ProjectAgents:
    """Get or create agent collection for a project slug."""
    if slug is None:
        slug = get_project_slug() or _DEFAULT_SLUG

    pa = _projects.get(slug)
    if pa is None:
        project_dir = get_project_dir()
        if project_dir is None:
            project_dir = _default_project_dir or Path.cwd()
        pa = _projects.setdefault(slug, ProjectAgents(project_dir=project_dir.resolve()))

    return pa


def set_project_dir(path: Path) -> None:
    """Set the default project directory (single-project mode)."""
    global _default_project_dir
    _default_project_dir = path
    pa = _get_project_agents(_DEFAULT_SLUG)
    pa.project_dir = path.resolve()


def _prune_completed(pa: ProjectAgents, now: float | None = None) -> None:
    """Remove expired entries from recently_completed."""
    if now is None:
        now = time.monotonic()
    while pa.recently_completed and (now - pa.recently_completed[0].completed_at) > RECENTLY_COMPLETED_TTL:
        pa.recently_completed.popleft()


def _evict_completed_for_issue(pa: ProjectAgents, issue: str) -> None:
    """Remove completed entries for *issue* so they don't linger when a new run starts.

    Must be called inside ``pa._lock``.
    """
    pa.recently_completed = deque(
        (ca for ca in pa.recently_completed if ca.issue != issue),
        maxlen=MAX_RECENTLY_COMPLETED,
    )
