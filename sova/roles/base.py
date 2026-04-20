"""Base role abstraction for SOVA agents.

Each role defines what tracker states it reads from, what state it moves
issues to, and how it executes its workflow. Roles enforce the mandatory
pipeline: Triage -> Research -> Develop.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from sova.adapters.base import Task, TaskState
from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep


@dataclass
class RoleResult:
    """Outcome of a role execution."""

    success: bool
    summary: str
    error: str | None = None
    output_state: TaskState | None = None
    findings: list[str] = field(default_factory=list)


class AgentRole(ABC):
    """Abstract base for all agent roles.

    Each role specifies which tracker states it can pick up issues from
    (allowed_input_states) and what state it moves them to on success
    (output_state). The validate_preconditions method enforces these
    constraints before execution begins.
    """

    name: str = ""
    description: str = ""
    allowed_input_states: frozenset[TaskState] = frozenset()
    output_state: TaskState = TaskState.DONE

    @abstractmethod
    async def execute(self, ctx: ExecutionContext) -> RoleResult:
        """Run the role's workflow for the given context."""

    def validate_preconditions(self, task: Task, force: bool = False) -> bool:
        """Check that the task is in an allowed state for this role.

        Returns True if the task can be picked up, False otherwise.
        When force=True, always returns True (bypasses state checks).
        """
        if force:
            return True
        return task.state in self.allowed_input_states

    def get_steps(self) -> list[BaseStep]:
        """Return role-specific step pipeline for the WorkflowEngine.

        Override in roles that use the WorkflowEngine. Roles with
        simpler workflows can implement execute() directly.
        """
        return []
