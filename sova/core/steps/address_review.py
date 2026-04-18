"""Step 8c: Address review -- respawn Developer to fix review findings."""

from __future__ import annotations

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.utils.logging import get_logger

log = get_logger(component="step.address_review")


class AddressReviewStep(BaseStep):
    name = "address_review"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        # In the full implementation, this reads review findings from the PR
        # and spawns a Developer agent to address them. For now, it's a
        # placeholder that marks the step as completed.
        log.info("step.address_review", pr=ctx.pr_number)
        return StepResult(success=True, summary="Review addressing placeholder (no findings to address)")

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        return GateCheckResult(passed=True)

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        # Skip if review is disabled or no PR exists
        return not ctx.config.review.enabled or ctx.pr_number is None
