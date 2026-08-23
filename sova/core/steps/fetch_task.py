"""Step: Fetch Task -- populate execution context with task data.

Simple step that fetches the task from the adapter and stores it
in the execution context for downstream steps. Also validates
preconditions when allowed_input_states is set.
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

        try:
            task = await ctx.adapter.get_task(ctx.issue_number)
        except Exception as exc:
            return StepResult(success=False, summary="Adapter error", error=str(exc))

        ctx.task = task

        if ctx.allowed_input_states and not ctx.force:
            if task.state not in ctx.allowed_input_states:
                expected_states = ", ".join(sorted(ctx.allowed_input_states))
                return StepResult(
                    success=False,
                    summary=f"Precondition failed: issue is in {task.state}",
                    error=f"Issue #{ctx.issue_number} is in {task.state}, expected one of: {expected_states}",
                )

        return StepResult(
            success=True,
            summary=f"Fetched: {task.title}",
        )

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        if ctx.task is not None or not ctx.has_issue:
            return GateCheckResult(passed=True)
        return GateCheckResult(passed=False, reason="Task not populated in context")
