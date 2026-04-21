"""Base step abstraction with gate checks.

Every workflow step inherits from BaseStep and implements:
- execute(): do the work
- validate_output(): gate check -- did the step produce valid output?
- can_skip(): should this step be skipped for this context?
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from sova.core.context import ExecutionContext


@dataclass
class StepResult:
    """Outcome of a step execution."""

    success: bool
    summary: str
    error: str | None = None
    cost_usd: float = 0.0


@dataclass
class GateCheckResult:
    """Outcome of a post-step validation gate."""

    passed: bool
    reason: str | None = None


class BaseStep(ABC):
    """Abstract base for all workflow steps."""

    name: str = ""
    max_retries: int = 0

    @abstractmethod
    async def execute(self, ctx: ExecutionContext) -> StepResult:
        """Execute the step and return a result."""

    @abstractmethod
    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Validate that the step produced acceptable output."""

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        """Whether this step can be skipped for the given context.

        Default: skip if this step already passed in a prior run (resume).
        Override in subclasses for additional skip logic.
        """
        return self.name in ctx.completed_steps
