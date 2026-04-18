"""Step 8b: Automated review -- trigger the Reviewer agent via handoff."""

from __future__ import annotations

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.llm.client import invoke_command
from sova.utils.logging import get_logger

log = get_logger(component="step.automated_review")


class AutomatedReviewStep(BaseStep):
    name = "automated_review"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        if not ctx.config.review.enabled:
            return StepResult(success=True, summary="Automated review disabled")

        log.info("step.automated_review", pr=ctx.pr_number)

        try:
            result = await invoke_command(
                "/review",
                model=ctx.config.agent.model,
                cwd=ctx.working_dir,
                max_budget_usd=ctx.config.agent.max_budget - ctx.cost_usd,
            )
            ctx.add_cost(result.cost_usd)
            return StepResult(success=True, summary="Automated review completed", cost_usd=float(result.cost_usd))
        except RuntimeError as exc:
            return StepResult(success=False, summary="Automated review failed", error=str(exc))

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        return GateCheckResult(passed=True)

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        return not ctx.config.review.enabled
