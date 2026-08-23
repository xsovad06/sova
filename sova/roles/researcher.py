"""Researcher role -- investigate issues and prepare them for development.

Reads TRIAGED issues, runs the /research command for interactive codebase
exploration, and transitions them to RESEARCHED. Uses the step pipeline
pattern consistent with DeveloperRole.
"""

from __future__ import annotations

from sova.adapters.base import Task, TaskState
from sova.core.context import ExecutionContext
from sova.core.state import TaskStatus
from sova.core.steps import get_researcher_steps
from sova.core.steps.base import BaseStep
from sova.core.workflow import WorkflowEngine
from sova.roles.base import AgentRole, RoleResult, TaskAssessment
from sova.utils.logging import get_logger

log = get_logger(component="role.researcher")


class ResearcherRole(AgentRole):
    """Investigate triaged issues via interactive codebase exploration.

    Delegates research to the /research command via invoke_command(),
    which spawns a Claude Code session with full tool access (file reading,
    grep, code exploration). The command writes a structured research
    assessment back to the issue tracker.
    """

    name = "researcher"
    description = "Investigate triaged issues and prepare them for development"
    allowed_input_states = frozenset({TaskState.TRIAGED})
    output_state = TaskState.RESEARCHED

    async def assess_task(self, task: Task) -> TaskAssessment:
        """Assess research suitability using heuristics."""
        if not task.body or not task.body.strip():
            return TaskAssessment(
                suitability="needs_spec",
                confidence=0.8,
                reasoning="Issue has no description; needs specification before research.",
                missing_context=["description", "acceptance criteria"],
                estimated_complexity="moderate",
                suggested_role="triage",
            )

        if task.labels and "agent:human-only" in task.labels:
            return TaskAssessment(
                suitability="human_only",
                confidence=0.85,
                reasoning="Issue is marked as human-only.",
                estimated_complexity="complex",
                suggested_role="researcher",
            )

        if "## Research" in task.body:
            return TaskAssessment(
                suitability="ready",
                confidence=0.9,
                reasoning="Issue already has a research section; ready for development.",
                estimated_complexity="moderate",
                suggested_role="developer",
            )

        return TaskAssessment(
            suitability="needs_research",
            confidence=0.6,
            reasoning="Task needs codebase exploration before development can begin.",
            estimated_complexity="moderate",
            suggested_role="researcher",
        )

    def get_steps(self) -> list[BaseStep]:
        return get_researcher_steps()

    async def execute(self, ctx: ExecutionContext) -> RoleResult:
        log.info("researcher.start", issue=ctx.issue_number)

        ctx.allowed_input_states = self.allowed_input_states

        steps = self.get_steps()
        engine = WorkflowEngine(steps=steps, ctx=ctx)
        workflow_result = await engine.run()

        spec_awaiting = workflow_result.final_status == TaskStatus.AWAITING_APPROVAL

        if workflow_result.success or spec_awaiting:
            # Update label even when spec awaits approval: prevents supervisor re-spawn on next poll
            await ctx.adapter.transition_state(ctx.issue_number, TaskState.RESEARCHED)

        if workflow_result.success:
            log.info("researcher.done", issue=ctx.issue_number)
            return RoleResult(
                success=True,
                summary=f"Issue #{ctx.issue_number} researched",
                output_state=TaskState.RESEARCHED,
            )

        if spec_awaiting:
            log.info("researcher.spec_awaiting_approval", issue=ctx.issue_number)
            return RoleResult(
                success=False,
                summary=f"Spec for #{ctx.issue_number} awaiting human approval",
            )

        log.error("researcher.failed", issue=ctx.issue_number, error=workflow_result.error)
        return RoleResult(
            success=False,
            summary=f"Research failed at step: {workflow_result.final_status}",
            error=workflow_result.error,
        )
