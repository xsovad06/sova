"""Developer role -- implement features and fixes via TDD workflow.

Enforces Gate 3: only picks up RESEARCHED or IN_PROGRESS issues.
Uses the full developer step pipeline via the WorkflowEngine.
"""

from __future__ import annotations

from sova.adapters.base import Task, TaskState
from sova.core.context import ExecutionContext
from sova.core.steps import get_developer_steps
from sova.core.steps.base import BaseStep
from sova.core.workflow import WorkflowEngine
from sova.roles.base import AgentRole, RoleResult, TaskAssessment
from sova.utils.logging import get_logger

log = get_logger(component="role.developer")


class DeveloperRole(AgentRole):
    name = "developer"
    description = "Develop features and fixes using TDD workflow"
    allowed_input_states = frozenset({TaskState.RESEARCHED, TaskState.IN_PROGRESS})
    output_state = TaskState.DONE

    async def assess_task(self, task: Task) -> TaskAssessment:
        return TaskAssessment(
            suitability="ready",
            confidence=0.7,
            reasoning="Task is ready for development.",
            estimated_complexity="moderate",
            suggested_role="developer",
        )

    def get_steps(self) -> list[BaseStep]:
        return get_developer_steps()

    async def execute(self, ctx: ExecutionContext) -> RoleResult:
        task = await ctx.adapter.get_task(ctx.issue_number)

        if not self.validate_preconditions(task, force=ctx.force):
            return RoleResult(
                success=False,
                summary=f"Issue #{ctx.issue_number} not ready for development",
                error=f"Gate 3: issue must be in researched or in_progress state "
                f"(current: {task.state}). Run triage and research first, "
                f"or use --force to bypass.",
            )

        log.info("developer.start", issue=ctx.issue_number)

        steps = self.get_steps()
        engine = WorkflowEngine(steps=steps, ctx=ctx)
        workflow_result = await engine.run()

        if workflow_result.success:
            log.info("developer.done", issue=ctx.issue_number)
            return RoleResult(
                success=True,
                summary=f"Issue #{ctx.issue_number} developed and PR created",
                output_state=TaskState.DONE,
            )

        log.error("developer.failed", issue=ctx.issue_number, error=workflow_result.error)
        return RoleResult(
            success=False,
            summary=f"Development failed at step: {workflow_result.final_status}",
            error=workflow_result.error,
        )
