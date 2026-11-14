"""Step: Extract Memory. Placeholder slot kept in the pipelines; there is no memory store to write to."""

from __future__ import annotations

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult


class ExtractMemoryStep(BaseStep):
    name = "extract_memory"

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        """Always skipped: the engine records a `skipped` execution, so the step still shows in the run history."""
        return True

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        return StepResult(success=True, summary="Memory extraction is removed; nothing to do")

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        return GateCheckResult(passed=True)
