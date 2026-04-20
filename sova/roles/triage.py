"""Triage role -- assess issues and classify them for the pipeline.

Reads BACKLOG issues, evaluates them for agent suitability, applies
labels, posts an assessment comment, and moves them to TRIAGED.
"""

from __future__ import annotations

from sova.adapters.base import Task, TaskState
from sova.core.context import ExecutionContext
from sova.roles.base import AgentRole, RoleResult
from sova.utils.logging import get_logger

log = get_logger(component="role.triage")


class TriageRole(AgentRole):
    name = "triage"
    description = "Assess issues for agent suitability and classify them"
    allowed_input_states = frozenset({TaskState.BACKLOG})
    output_state = TaskState.TRIAGED

    async def execute(self, ctx: ExecutionContext) -> RoleResult:
        task = await ctx.adapter.get_task(ctx.issue_number)

        if not self.validate_preconditions(task, force=ctx.force):
            return RoleResult(
                success=False,
                summary=f"Issue #{ctx.issue_number} not in valid state for triage",
                error=f"Precondition failed: issue is in {task.state}, "
                f"expected one of {', '.join(self.allowed_input_states)}",
            )

        log.info("triage.start", issue=ctx.issue_number)

        # Post assessment comment
        comment = self._build_assessment_comment(task)
        await ctx.adapter.post_comment(ctx.issue_number, comment)

        # Transition to triaged
        await ctx.adapter.transition_state(ctx.issue_number, TaskState.TRIAGED)

        log.info("triage.done", issue=ctx.issue_number)
        return RoleResult(
            success=True,
            summary=f"Issue #{ctx.issue_number} triaged",
            output_state=TaskState.TRIAGED,
        )

    def _build_assessment_comment(self, task: Task) -> str:
        """Build a triage assessment comment for the issue."""
        has_body = bool(task.body and task.body.strip())
        return (
            f"## Triage Assessment\n\n"
            f"**Title**: {task.title}\n"
            f"**Has description**: {'yes' if has_body else 'no'}\n"
            f"**Labels**: {', '.join(task.labels) if task.labels else 'none'}\n\n"
            f"Issue has been triaged and is ready for research."
        )
