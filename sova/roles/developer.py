"""Developer role -- implement features and fixes via TDD workflow.

Enforces Gate 3: only picks up RESEARCHED or IN_PROGRESS issues.
Uses the full developer step pipeline via the WorkflowEngine.

Two pipeline variants:
- Development pipeline: full TDD cycle, ends with handoff to Reviewer
- Address-review pipeline: fix review findings, ends with handoff to user
"""

from __future__ import annotations

from sova.adapters.base import Task, TaskState
from sova.core.context import ExecutionContext
from sova.core.steps import get_address_review_steps, get_developer_steps
from sova.core.steps.base import BaseStep
from sova.core.workflow import WorkflowEngine
from sova.roles.base import AgentRole, RoleResult, TaskAssessment
from sova.utils.logging import get_logger

log = get_logger(component="role.developer")


class DeveloperRole(AgentRole):
    name = "developer"
    description = "Develop features and fixes using TDD workflow"
    allowed_input_states = frozenset({TaskState.RESEARCHED, TaskState.IN_PROGRESS, TaskState.IN_REVIEW})
    output_state = TaskState.IN_REVIEW

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
                error=f"Gate 3: issue must be in researched, in_progress, or in_review state "
                f"(current: {task.state}). Run triage and research first, "
                f"or use --force to bypass.",
            )

        # Address-review mode: respawned to fix reviewer findings
        if task.state == TaskState.IN_REVIEW and ctx.pr_number:
            return await self._execute_address_review(ctx)

        return await self._execute_development(ctx)

    async def _execute_development(self, ctx: ExecutionContext) -> RoleResult:
        log.info("developer.start", issue=ctx.issue_number)

        await ctx.adapter.transition_state(ctx.issue_number, TaskState.IN_PROGRESS)

        steps = get_developer_steps()
        engine = WorkflowEngine(steps=steps, ctx=ctx)
        workflow_result = await engine.run()

        if workflow_result.success:
            log.info("developer.done", issue=ctx.issue_number)
            return RoleResult(
                success=True,
                summary=f"Issue #{ctx.issue_number} developed, PR created, handed off to Reviewer",
                output_state=TaskState.IN_REVIEW,
            )

        log.error("developer.failed", issue=ctx.issue_number, error=workflow_result.error)
        return RoleResult(
            success=False,
            summary=f"Development failed at step: {workflow_result.final_status}",
            error=workflow_result.error,
        )

    async def _execute_address_review(self, ctx: ExecutionContext) -> RoleResult:
        log.info("developer.address_review.start", issue=ctx.issue_number, pr=ctx.pr_number)

        steps = get_address_review_steps()
        engine = WorkflowEngine(steps=steps, ctx=ctx)
        workflow_result = await engine.run()

        if workflow_result.success:
            log.info("developer.address_review.done", issue=ctx.issue_number)
            return RoleResult(
                success=True,
                summary=f"Review findings addressed for PR #{ctx.pr_number}, handed off to user",
                output_state=TaskState.IN_REVIEW,
            )

        log.error("developer.address_review.failed", issue=ctx.issue_number, error=workflow_result.error)
        return RoleResult(
            success=False,
            summary=f"Address review failed at step: {workflow_result.final_status}",
            error=workflow_result.error,
        )
