"""Triage role -- assess issues and classify them for the pipeline.

Reads BACKLOG issues, evaluates them for agent suitability, applies
labels, posts an assessment comment, and moves them to TRIAGED.
"""

from __future__ import annotations

from sova.adapters.base import Task, TaskState
from sova.core.context import ExecutionContext
from sova.roles.base import AgentRole, RoleResult, TaskAssessment
from sova.utils.logging import get_logger

log = get_logger(component="role.triage")


class TriageRole(AgentRole):
    name = "triage"
    description = "Assess issues for agent suitability and classify them"
    allowed_input_states = frozenset({TaskState.BACKLOG})
    output_state = TaskState.TRIAGED

    # Maps assessment suitability to tracker labels
    SUITABILITY_LABELS: dict[str, str] = {
        "ready": "agent:ready",
        "needs_spec": "agent:needs-spec",
        "needs_research": "agent:needs-research",
        "human_only": "agent:human-only",
    }

    async def assess_task(self, task: Task) -> TaskAssessment:
        has_body = bool(task.body and task.body.strip())
        if not has_body:
            return TaskAssessment(
                suitability="needs_spec",
                confidence=0.7,
                reasoning="Issue has no description; needs specification before work can begin.",
                missing_context=["description", "acceptance criteria"],
                estimated_complexity="moderate",
                suggested_role="triage",
            )
        return TaskAssessment(
            suitability="ready",
            confidence=0.8,
            reasoning="Issue has a title and description; ready for research.",
            estimated_complexity="moderate",
            suggested_role="researcher",
        )

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

        # Assess the task
        assessment = await self.assess_task(task)

        # Apply suitability label
        label = self.SUITABILITY_LABELS[assessment.suitability]
        await ctx.adapter.add_label(ctx.issue_number, label)

        # Post assessment comment
        comment = self._build_assessment_comment(task, assessment)
        await ctx.adapter.post_comment(ctx.issue_number, comment)

        # Transition to triaged
        await ctx.adapter.transition_state(ctx.issue_number, TaskState.TRIAGED)

        log.info("triage.done", issue=ctx.issue_number)
        return RoleResult(
            success=True,
            summary=f"Issue #{ctx.issue_number} triaged as {assessment.suitability}",
            output_state=TaskState.TRIAGED,
        )

    def _build_assessment_comment(self, task: Task, assessment: TaskAssessment) -> str:
        """Build a triage assessment comment for the issue."""
        has_body = bool(task.body and task.body.strip())
        missing = ", ".join(assessment.missing_context) if assessment.missing_context else "none"
        return (
            f"## Triage Assessment\n\n"
            f"**Title**: {task.title}\n"
            f"**Has description**: {'yes' if has_body else 'no'}\n"
            f"**Suitability**: {assessment.suitability}\n"
            f"**Confidence**: {assessment.confidence:.0%}\n"
            f"**Complexity**: {assessment.estimated_complexity}\n"
            f"**Missing context**: {missing}\n"
            f"**Labels**: {', '.join(task.labels) if task.labels else 'none'}\n\n"
            f"{assessment.reasoning}"
        )
