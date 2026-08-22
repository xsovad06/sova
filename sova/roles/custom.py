"""Custom role -- user-defined workflow backed by a WorkflowDefinition DAG."""

from __future__ import annotations

from sova.adapters.base import Task, TaskState
from sova.core.context import ExecutionContext
from sova.core.dag import DAGExecutor
from sova.db.models import WorkflowDefinition
from sova.roles.base import AgentRole, RoleResult, TaskAssessment
from sova.utils.logging import get_logger

log = get_logger(component="role.custom")

_STATE_MAP: dict[str, TaskState] = {s.value: s for s in TaskState}


class CustomRole(AgentRole):
    """A role defined by a WorkflowDefinition DAG rather than hardcoded steps."""

    def __init__(self, definition: WorkflowDefinition) -> None:
        self._definition = definition
        self.name = definition.name
        self.description = definition.description or f"Custom role: {definition.name}"

        # Map input_states strings to TaskState enums
        input_states = set()
        for state_str in definition.input_states or []:
            ts = _STATE_MAP.get(state_str)
            if ts:
                input_states.add(ts)
        self.allowed_input_states = frozenset(input_states) if input_states else frozenset(TaskState)

        self.output_state = _STATE_MAP.get(definition.output_state) if definition.output_state else None

    async def assess_task(self, task: Task) -> TaskAssessment:
        return TaskAssessment(
            suitability="ready",
            confidence=0.6,
            reasoning=f"Custom role '{self.name}' will handle this task.",
            estimated_complexity="moderate",
            suggested_role=self.name,
        )

    async def execute(self, ctx: ExecutionContext) -> RoleResult:
        if ctx.has_issue:
            task = await ctx.adapter.get_task(ctx.issue_number)

            if not self.validate_preconditions(task, force=ctx.force):
                return RoleResult(
                    success=False,
                    summary=f"Issue #{ctx.issue_number} not ready for custom role '{self.name}'",
                    error=f"Issue must be in one of {[s.value for s in self.allowed_input_states]} "
                    f"(current: {task.state}). Use --force to bypass.",
                )

        log.info("custom.start", label=ctx.display_label, role=self.name)

        executor = DAGExecutor(self._definition, ctx)
        dag_result = await executor.execute()

        if dag_result.success:
            log.info("custom.done", label=ctx.display_label, role=self.name)
            return RoleResult(
                success=True,
                summary=dag_result.summary,
                output_state=self.output_state,
            )

        log.error("custom.failed", label=ctx.display_label, error=dag_result.error)
        return RoleResult(
            success=False,
            summary=dag_result.summary,
            error=dag_result.error,
        )
