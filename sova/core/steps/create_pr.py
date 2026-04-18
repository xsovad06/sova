"""Step 7: Create PR -- create a pull request and update tracker state."""

from __future__ import annotations

from sova.adapters.base import TaskState
from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.git import operations as git_ops
from sova.utils.logging import get_logger

log = get_logger(component="step.create_pr")


class CreatePRStep(BaseStep):
    name = "create_pr"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.create_pr", issue=ctx.issue_number, branch=ctx.branch_name)

        task_title = ctx.task.title if ctx.task else ctx.branch_name
        title = f"feat(#{ctx.issue_number}): {task_title}"
        body = f"Closes #{ctx.issue_number}\n\nAutonomous development by SOVA agent."

        try:
            pr_info = await git_ops.create_pr(
                title=title,
                body=body,
                base=ctx.base_branch,
                head=ctx.branch_name,
                repo=ctx.repo,
            )
            ctx.pr_number = pr_info.number
            ctx.pr_url = pr_info.url

            # Move issue to In Review on the tracker
            try:
                await ctx.adapter.transition_state(ctx.issue_number, TaskState.IN_REVIEW)
            except Exception as exc:
                log.warning("step.create_pr.tracker_update_failed", error=str(exc))

            return StepResult(success=True, summary=f"Created PR #{pr_info.number}")
        except RuntimeError as exc:
            return StepResult(success=False, summary="Failed to create PR", error=str(exc))

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Gate: PR number must have been extracted."""
        if ctx.pr_number is not None:
            return GateCheckResult(passed=True)
        return GateCheckResult(passed=False, reason="No PR number after create_pr step")

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        # Skip if PR already exists (resumed run)
        return ctx.pr_number is not None
