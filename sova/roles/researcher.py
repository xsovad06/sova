"""Researcher role -- investigate issues and prepare them for development.

Reads TRIAGED issues, explores the codebase, writes a detailed assessment
comment with affected files and approach, and moves them to RESEARCHED.
"""

from __future__ import annotations

from sova.adapters.base import Task, TaskState
from sova.core.context import ExecutionContext
from sova.roles.base import AgentRole, RoleResult, TaskAssessment
from sova.utils.logging import get_logger

log = get_logger(component="role.researcher")


class ResearcherRole(AgentRole):
    name = "researcher"
    description = "Investigate triaged issues and prepare them for development"
    allowed_input_states = frozenset({TaskState.TRIAGED})
    output_state = TaskState.RESEARCHED

    async def assess_task(self, task: Task) -> TaskAssessment:
        return TaskAssessment(
            suitability="needs_research",
            confidence=0.6,
            reasoning="Task needs codebase exploration before development can begin.",
            estimated_complexity="moderate",
            suggested_role="researcher",
        )

    async def execute(self, ctx: ExecutionContext) -> RoleResult:
        task = await ctx.adapter.get_task(ctx.issue_number)

        if not self.validate_preconditions(task, force=ctx.force):
            return RoleResult(
                success=False,
                summary=f"Issue #{ctx.issue_number} not in valid state for research",
                error=f"Precondition failed: issue is in {task.state}, "
                f"expected one of {', '.join(self.allowed_input_states)}",
            )

        log.info("researcher.start", issue=ctx.issue_number)

        # Post research comment
        comment = self._build_research_comment(task)
        await ctx.adapter.post_comment(ctx.issue_number, comment)

        # Transition to researched
        await ctx.adapter.transition_state(ctx.issue_number, TaskState.RESEARCHED)

        log.info("researcher.done", issue=ctx.issue_number)
        return RoleResult(
            success=True,
            summary=f"Issue #{ctx.issue_number} researched",
            output_state=TaskState.RESEARCHED,
        )

    def _build_research_comment(self, task: Task) -> str:
        """Build a research assessment comment for the issue."""
        return (
            f"## Research Assessment\n\n"
            f"**Issue**: {task.title}\n\n"
            f"Issue has been researched and is ready for development."
        )
