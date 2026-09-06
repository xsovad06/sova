"""Base step abstraction with gate checks.

Every workflow step inherits from BaseStep and implements:
- execute(): do the work
- validate_output(): structural gate check (fast, required)
- verify_output(): heavyweight verification (optional, e.g. test suite)
- can_skip(): should this step be skipped for this context?
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from sova.core.context import ExecutionContext

if TYPE_CHECKING:
    from sova.ipc.handoff import HandoffAction


@dataclass
class StepResult:
    """Outcome of a step execution."""

    success: bool
    summary: str
    error: str | None = None
    cost_usd: Decimal = Decimal("0")
    awaiting_approval: bool = False
    handoff_actions: list[HandoffAction] | None = None
    partial_work: bool = False


@dataclass
class GateCheckResult:
    """Outcome of a post-step validation gate."""

    passed: bool
    reason: str | None = None


class BaseStep(ABC):
    """Abstract base for all workflow steps."""

    name: str = ""
    max_retries: int = 0
    # Routing category for llm.routing lookups, empty when the step is not
    # routable. Steps pass it to the LLM call via ctx.routing_task_type().
    TASK_TYPE: str = ""

    @abstractmethod
    async def execute(self, ctx: ExecutionContext) -> StepResult:
        """Execute the step and return a result."""

    @abstractmethod
    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Validate that the step produced acceptable output."""

    async def verify_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Heavyweight verification pass after the structural gate passes.

        Override in subclasses that need expensive verification (e.g., running
        the full test suite). The default is a passing no-op.
        """
        return GateCheckResult(passed=True)

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        """Whether this step can be skipped for the given context.

        Default: skip if this step already passed in a prior run (resume).
        Override in subclasses for additional skip logic.
        """
        return self.name in ctx.completed_steps
