"""Researcher role -- investigate issues and prepare them for development.

Reads TRIAGED issues, explores the codebase, appends a research assessment
to the issue body, and moves them to RESEARCHED.
"""

from __future__ import annotations

from sova.adapters.base import Task, TaskState
from sova.core.context import ExecutionContext
from sova.roles.base import AgentRole, RoleResult, TaskAssessment
from sova.utils.logging import get_logger

log = get_logger(component="role.researcher")


class ResearcherRole(AgentRole):
    """MVP stub -- produces a static research assessment without LLM analysis.

    A future version will use LLM-powered codebase exploration to generate
    meaningful research findings, dependency analysis, and implementation guidance.
    """

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
        log.warning("researcher.stub", issue=ctx.issue_number, msg="Using MVP stub -- no LLM research performed")
        task = await ctx.adapter.get_task(ctx.issue_number)

        if not self.validate_preconditions(task, force=ctx.force):
            return RoleResult(
                success=False,
                summary=f"Issue #{ctx.issue_number} not in valid state for research",
                error=f"Precondition failed: issue is in {task.state}, "
                f"expected one of {', '.join(self.allowed_input_states)}",
            )

        log.info("researcher.start", issue=ctx.issue_number)

        # Append research assessment to issue body
        research_section = self._build_research_comment(task)
        updated_body = (task.body or "").rstrip() + "\n\n" + research_section
        await ctx.adapter.edit_body(ctx.issue_number, updated_body)

        # Transition to researched
        await ctx.adapter.transition_state(ctx.issue_number, TaskState.RESEARCHED)

        log.info("researcher.done", issue=ctx.issue_number)
        return RoleResult(
            success=True,
            summary=f"Issue #{ctx.issue_number} researched",
            output_state=TaskState.RESEARCHED,
        )

    def _build_research_comment(self, task: Task) -> str:
        """Build a research assessment section. MVP stub: produces static boilerplate."""
        return (
            f"## Research Assessment\n\n"
            f"**Issue**: {task.title}\n\n"
            f"Issue has been researched and is ready for development."
        )
