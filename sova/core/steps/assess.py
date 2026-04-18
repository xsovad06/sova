"""Step 2: Assess -- verify the issue is ready for development.

Gate 3: The Developer agent refuses to pick up any issue not in
"Researched" state. This prevents the old failure mode where the agent
blindly started work on underspecified issues.
"""

from __future__ import annotations

from sova.adapters.base import TaskState
from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.utils.logging import get_logger

log = get_logger(component="step.assess")

_READY_STATES = frozenset({TaskState.RESEARCHED, TaskState.IN_PROGRESS})


class AssessStep(BaseStep):
    name = "assess"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        task = await ctx.adapter.get_task(ctx.issue_number)
        ctx.task = task
        state = await ctx.adapter.get_state(ctx.issue_number)

        log.info("step.assess", issue=ctx.issue_number, tracker_state=state)

        if state in _READY_STATES:
            return StepResult(success=True, summary=f"Issue #{ctx.issue_number} is in {state} state")

        return StepResult(
            success=False,
            summary=f"Issue #{ctx.issue_number} is in {state} state, not ready for development",
            error=f"Issue must be in {', '.join(_READY_STATES)} state (current: {state})",
        )

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        return GateCheckResult(passed=True)

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        return ctx.force
