"""Step 9: Complete -- cleanup, record learnings, move issue to Done."""

from __future__ import annotations

from sova.adapters.base import TaskState
from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.utils.logging import get_logger

log = get_logger(component="step.complete")


class CompleteStep(BaseStep):
    name = "complete"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.complete", issue=ctx.issue_number, pr=ctx.pr_number)

        # Move issue to Done on the tracker
        try:
            await ctx.adapter.transition_state(ctx.issue_number, TaskState.DONE)
        except Exception as exc:
            log.warning("step.complete.tracker_update_failed", error=str(exc))

        return StepResult(
            success=True,
            summary=f"Issue #{ctx.issue_number} completed (PR #{ctx.pr_number})",
        )

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        return GateCheckResult(passed=True)

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        return False
