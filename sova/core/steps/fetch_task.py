"""Step: Fetch Task -- populate execution context with task data.

Simple step that fetches the task from the adapter and stores it
in the execution context for downstream steps. When the context
carries allowed_input_states, validates the issue state as a
precondition (replaces pre-engine checks that bypassed step tracking).
"""

from __future__ import annotations

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.utils.logging import get_logger

log = get_logger(component="step.fetch_task")


class FetchTaskStep(BaseStep):
    name = "fetch_task"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.fetch_task", label=ctx.display_label)

        if not ctx.has_issue:
            return StepResult(success=True, summary="Skipped: no issue for this run")

        task = await ctx.adapter.get_task(ctx.issue_number)
        ctx.task = task

        if ctx.allowed_input_states and not ctx.force:
            if task.state not in ctx.allowed_input_states:
                allowed = ", ".join(sorted(ctx.allowed_input_states))
                log.warning(
                    "step.fetch_task.state_rejected",
                    issue=ctx.issue_number,
                    current_state=task.state,
                    allowed=allowed,
                )
                return StepResult(
                    success=False,
                    summary=f"Issue #{ctx.issue_number} in {task.state}, expected one of: {allowed}",
                    error=f"Precondition failed: issue state {task.state} not in allowed states ({allowed})",
                )

        return StepResult(
            success=True,
            summary=f"Fetched: {task.title}",
        )

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        if ctx.task is not None or not ctx.has_issue:
            return GateCheckResult(passed=True)
        return GateCheckResult(passed=False, reason="Task not populated in context")
