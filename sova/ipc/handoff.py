"""Handoff protocol for inter-agent context passing.

Agents are ephemeral -- they spawn, work, write a handoff, and die.
The handoff carries enough context for a fresh agent to pick up
without re-reading the entire codebase.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sova.db.models import TaskRun
from sova.db.session import get_session
from sova.utils.logging import get_logger

log = get_logger(component="ipc.handoff")


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
