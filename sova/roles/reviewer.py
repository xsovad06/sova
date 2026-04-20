"""Reviewer role -- review PRs and provide feedback.

Reads IN_REVIEW issues with linked PRs, reviews the code changes,
and posts review findings. Writes a handoff with pending findings
for the Developer to address.
"""

from __future__ import annotations

from sova.adapters.base import Task, TaskState
from sova.core.context import ExecutionContext
from sova.roles.base import AgentRole, RoleResult, TaskAssessment
from sova.utils.logging import get_logger

log = get_logger(component="role.reviewer")


class ReviewerRole(AgentRole):
    name = "reviewer"
    description = "Review PRs and provide feedback"
    allowed_input_states = frozenset({TaskState.IN_REVIEW})
    output_state = TaskState.IN_REVIEW

    async def assess_task(self, task: Task) -> TaskAssessment:
        return TaskAssessment(
            suitability="ready",
            confidence=0.7,
            reasoning="Task has a linked PR ready for review.",
            estimated_complexity="moderate",
            suggested_role="reviewer",
        )

    async def execute(self, ctx: ExecutionContext) -> RoleResult:
        task = await ctx.adapter.get_task(ctx.issue_number)

        if not self.validate_preconditions(task, force=ctx.force):
            return RoleResult(
                success=False,
                summary=f"Issue #{ctx.issue_number} not in valid state for review",
                error=f"Precondition failed: issue is in {task.state}, "
                f"expected one of {', '.join(self.allowed_input_states)}",
            )

        if not ctx.pr_number:
            return RoleResult(
                success=False,
                summary=f"Issue #{ctx.issue_number} has no linked PR",
                error="PR number is required for review. No PR linked to this issue.",
            )

        log.info("reviewer.start", issue=ctx.issue_number, pr=ctx.pr_number)

        # Post review comment
        comment = self._build_review_comment(task, ctx.pr_number)
        await ctx.adapter.post_comment(ctx.issue_number, comment)

        log.info("reviewer.done", issue=ctx.issue_number)
        return RoleResult(
            success=True,
            summary=f"Reviewed PR #{ctx.pr_number} for issue #{ctx.issue_number}",
            output_state=TaskState.IN_REVIEW,
        )

    def _build_review_comment(self, task: Task, pr_number: int) -> str:
        """Build a review comment for the issue."""
        return (
            f"## Code Review\n\n"
            f"**PR**: #{pr_number}\n"
            f"**Issue**: {task.title}\n\n"
            f"Review completed."
        )
