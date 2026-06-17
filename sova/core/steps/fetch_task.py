"""Step: Fetch Task -- populate execution context with task data.

Simple step that fetches the task from the adapter and stores it
in the execution context for downstream steps.
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

        return StepResult(
            success=True,
            summary=f"Fetched: {task.title}",
        )

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        if ctx.task is not None or not ctx.has_issue:
            return GateCheckResult(passed=True)
        return GateCheckResult(passed=False, reason="Task not populated in context")
