"""Handoff protocol for inter-agent context passing.

Agents are ephemeral -- they spawn, work, write a handoff, and die.
The handoff carries enough context for a fresh agent to pick up
without re-reading the entire codebase.

Two handoff types:
- AgentHandoff: DB-backed, used by orchestrator/scheduler for history.
- DashboardHandoff: file-based, used by dashboard for actionable UI panels.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from sova.db.models import TaskRun
from sova.db.session import get_session
from sova.utils.logging import get_logger

log = get_logger(component="ipc.handoff")


# ---------------------------------------------------------------------------
# Action model (used by dashboard handoff panels)
# ---------------------------------------------------------------------------


class HandoffAction(BaseModel):
    """A single action button rendered in the dashboard handoff panel."""

    id: str
    label: str
    description: str = ""
    style: Literal["approve", "neutral", "danger"] = "neutral"
    mode: Literal["agent", "claude-command", "shell"] = "claude-command"
    command: str = ""
    args: dict = Field(default_factory=dict)
    auto_execute: bool = False


# ---------------------------------------------------------------------------
# Dashboard handoff (file-based, for actionable UI)
# ---------------------------------------------------------------------------


class DashboardHandoff(BaseModel):
    """Handoff written to disk for dashboard action panels.

    This is the file-based handoff that the dashboard polls and renders
    as action buttons. Separate from the DB-backed AgentHandoff which
    stores history for the orchestrator.
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    source: str
    status: Literal["awaiting_action", "completed", "failed"]
    issue: str = ""
    pr_number: int | None = None
    branch: str = ""
    summary: str
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    details: dict = Field(default_factory=dict)
    next_actions: list[HandoffAction] = Field(default_factory=list)


def write_handoff_file(project_dir: Path, handoff: DashboardHandoff) -> Path:
    """Write a DashboardHandoff to the project's agent-control directory."""
    control_dir = project_dir / ".claude" / "agent-control"
    control_dir.mkdir(parents=True, exist_ok=True)
    path = control_dir / "handoff.json"
    path.write_text(json.dumps(handoff.model_dump(), indent=2))
    log.info("handoff_file.written", path=str(path), source=handoff.source, status=handoff.status)
    return path


def read_handoff_file(project_dir: Path) -> DashboardHandoff | None:
    """Read a DashboardHandoff from the project's agent-control directory."""
    path = project_dir / ".claude" / "agent-control" / "handoff.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return DashboardHandoff.model_validate(data)
    except (json.JSONDecodeError, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Agent handoff (DB-backed, for orchestrator history)
# ---------------------------------------------------------------------------


class AgentHandoff(BaseModel):
    """Context passed between agent spawns."""

    # Who wrote this
    role: str
    phase: str

    # What happened
    summary: str
    key_decisions: list[str] = Field(default_factory=list)
    files_changed: list[str] = Field(default_factory=list)
    tests_added: list[str] = Field(default_factory=list)

    # What's next
    next_action: str
    pending_findings: list[dict] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    needs_human: bool = False
    human_message: str | None = None

    # References
    pr_number: int | None = None
    branch_name: str
    commit_shas: list[str] = Field(default_factory=list)


async def write_handoff(task_run_id: int, handoff: AgentHandoff) -> None:
    """Persist a handoff to the TaskRun record."""
    async with await get_session() as session:
        task_run = await session.get(TaskRun, task_run_id)
        if task_run:
            task_run.handoff_json = handoff.model_dump()
            await session.commit()
            log.info("handoff.written", run_id=task_run_id, role=handoff.role, next_action=handoff.next_action)


async def read_handoff(task_run_id: int) -> AgentHandoff | None:
    """Read the most recent handoff from a TaskRun record."""
    async with await get_session() as session:
        task_run = await session.get(TaskRun, task_run_id)
        if not task_run or not task_run.handoff_json:
            return None
        return AgentHandoff.model_validate(task_run.handoff_json)
