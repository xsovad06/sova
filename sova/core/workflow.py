"""Workflow engine -- drives the state machine for a single task.

The WorkflowEngine coordinates step execution, gate checks, retries,
and DB persistence. It is the heart of the orchestrator.

Key responsibilities:
- Create/update TaskRun records in the database
- Record StepExecution for every step
- Record FailureRecord on failures (gate check or exception)
- Enforce budget limits
- Track total cost across all steps
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from sova.core.context import ExecutionContext
from sova.core.state import TaskStatus
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.db.models import CostRecord, FailureRecord, StepExecution, TaskRun
from sova.db.session import get_session
from sova.utils.logging import get_logger

log = get_logger(component="workflow")

# Maps step names to the TaskStatus they represent while executing
_STEP_STATUS_MAP: dict[str, TaskStatus] = {
    "sync": TaskStatus.PENDING,
    "assess": TaskStatus.ASSESSING,
    "create_worktree": TaskStatus.IN_PROGRESS,
    "develop": TaskStatus.DEVELOPING,
    "simplify": TaskStatus.SIMPLIFYING,
    "self_review": TaskStatus.REVIEWING,
    "push": TaskStatus.PUSHING,
    "create_pr": TaskStatus.PR_CREATED,
    "monitor_ci": TaskStatus.CI_MONITORING,
    "automated_review": TaskStatus.AUTOMATED_REVIEW,
    "address_review": TaskStatus.ADDRESSING_REVIEW,
    "complete": TaskStatus.DONE,
}


@dataclass
class StepRecord:
    """In-memory record of a single step execution."""

    step_name: str
    status: str  # "completed", "failed", "skipped"
    result: StepResult | None = None
    gate: GateCheckResult | None = None
    duration_ms: int = 0
    retries: int = 0


@dataclass
class WorkflowResult:
    """Final result of a workflow execution."""

    success: bool
    final_status: TaskStatus
    task_run_id: int | None = None
    steps_completed: int = 0
    steps_failed: int = 0
    steps_skipped: int = 0
    total_cost_usd: Decimal = Decimal("0")
    error: str | None = None
    step_records: list[StepRecord] = field(default_factory=list)


class WorkflowEngine:
    """Drives a sequence of steps for a single task run.

    Creates TaskRun, StepExecution, and FailureRecord DB entries
    as steps execute so the dashboard can observe progress in real time.
    """

    def __init__(self, *, steps: list[BaseStep], ctx: ExecutionContext) -> None:
        self._steps = steps
        self._ctx = ctx
        self._task_run_id: int | None = None

    async def run(self) -> WorkflowResult:
        """Execute all steps in order, respecting gates and retries."""
        self._task_run_id = await self._create_task_run()
        self._ctx.task_run_id = self._task_run_id

        result = WorkflowResult(
            success=False,
            final_status=TaskStatus.PENDING,
            task_run_id=self._task_run_id,
        )

        log.info("workflow.start", issue=self._ctx.issue_number, run_id=self._task_run_id)

        for step in self._steps:
            # Budget check before each step
            if self._ctx.is_budget_exceeded:
                log.warning("workflow.budget_exceeded", cost=str(self._ctx.cost_usd))
                result.final_status = TaskStatus.PAUSED
                result.error = f"Budget exceeded: ${self._ctx.cost_usd}"
                await self._record_failure(
                    step.name,
                    "budget_exceeded",
                    result.error,
                )
                await self._update_task_run_status(TaskStatus.PAUSED, error=result.error)
                return result

            # Check if step can be skipped
            if await step.can_skip(self._ctx):
                log.info("workflow.step.skip", step=step.name)
                result.steps_skipped += 1
                result.step_records.append(StepRecord(step_name=step.name, status="skipped"))
                continue

            # Update current step on the task run
            await self._set_current_step(step.name)

            # Execute with retries
            record = await self._execute_with_retries(step)
            result.step_records.append(record)

            if record.status == "failed":
                result.steps_failed += 1
                result.error = record.result.error if record.result else "Unknown error"

                # Gate failure -> pause, execution failure -> fail
                if record.gate and not record.gate.passed:
                    result.final_status = TaskStatus.PAUSED
                    result.error = record.gate.reason
                    failure_type = "gate_check"
                else:
                    result.final_status = TaskStatus.FAILED
                    failure_type = "exception"

                await self._record_failure(step.name, failure_type, result.error or "Unknown error")
                await self._update_task_run_status(result.final_status, error=result.error)

                log.error(
                    "workflow.step.failed",
                    step=step.name,
                    error=result.error,
                    status=result.final_status,
                )
                return result

            result.steps_completed += 1
            result.total_cost_usd += Decimal(str(record.result.cost_usd)) if record.result else Decimal("0")

            # Update the workflow status based on the step
            step_status = _STEP_STATUS_MAP.get(step.name)
            if step_status:
                result.final_status = step_status

        # All steps completed successfully
        result.success = True
        result.final_status = TaskStatus.DONE
        await self._update_task_run_status(TaskStatus.DONE)
        await self._finalize_task_run()

        log.info("workflow.done", issue=self._ctx.issue_number, cost=str(result.total_cost_usd))
        return result

    async def _execute_with_retries(self, step: BaseStep) -> StepRecord:
        """Execute a step, retrying on failure up to max_retries."""
        record = StepRecord(step_name=step.name, status="pending")
        attempts = 0
        max_attempts = step.max_retries + 1

        while attempts < max_attempts:
            start = time.monotonic()
            step_exec_id = await self._create_step_execution(step.name)

            try:
                step_result = await step.execute(self._ctx)
            except Exception as exc:
                step_result = StepResult(success=False, summary=f"Exception in {step.name}", error=str(exc))

            elapsed_ms = int((time.monotonic() - start) * 1000)
            record.duration_ms = elapsed_ms
            record.result = step_result
            attempts += 1

            await self._update_step_execution(step_exec_id, step_result, elapsed_ms)

            if not step_result.success:
                record.retries = attempts - 1
                if attempts < max_attempts:
                    log.info("workflow.step.retry", step=step.name, attempt=attempts)
                    continue
                record.status = "failed"
                return record

            # Step succeeded -- run gate check
            gate = await step.validate_output(self._ctx)
            record.gate = gate

            if not gate.passed:
                log.warning("workflow.gate.failed", step=step.name, reason=gate.reason)
                await self._update_step_execution_gate(step_exec_id, gate)
                record.status = "failed"
                return record

            record.status = "completed"
            log.info("workflow.step.done", step=step.name, duration_ms=record.duration_ms)
            return record

        record.status = "failed"
        return record

    # -- DB persistence --

    async def _create_task_run(self) -> int:
        """Create the initial TaskRun record and return its ID."""
        session = await get_session()
        async with session.begin():
            task_run = TaskRun(
                issue_number=self._ctx.issue_number,
                role=self._ctx.role,
                status=TaskStatus.PENDING.value,
                branch_name=self._ctx.branch_name,
            )
            session.add(task_run)
            await session.flush()
            return task_run.id

    async def _set_current_step(self, step_name: str) -> None:
        """Update the current_step field on the TaskRun."""
        session = await get_session()
        async with session.begin():
            task_run = await session.get(TaskRun, self._task_run_id)
            if task_run:
                task_run.current_step = step_name

    async def _update_task_run_status(self, status: TaskStatus, *, error: str | None = None) -> None:
        """Update the TaskRun status and optional error message."""
        session = await get_session()
        async with session.begin():
            task_run = await session.get(TaskRun, self._task_run_id)
            if task_run:
                task_run.status = status.value
                task_run.total_cost_usd = self._ctx.cost_usd
                if error:
                    task_run.error_message = error
                if status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.PAUSED):
                    task_run.ended_at = datetime.now(timezone.utc)

    async def _finalize_task_run(self) -> None:
        """Write final state to the TaskRun after successful completion."""
        session = await get_session()
        async with session.begin():
            task_run = await session.get(TaskRun, self._task_run_id)
            if task_run:
                task_run.total_cost_usd = self._ctx.cost_usd
                task_run.branch_name = self._ctx.branch_name
                task_run.pr_number = self._ctx.pr_number
                if self._ctx.worktree_dir:
                    task_run.worktree_path = str(self._ctx.worktree_dir)
                if not task_run.ended_at:
                    task_run.ended_at = datetime.now(timezone.utc)

    async def _create_step_execution(self, step_name: str) -> int:
        """Create a StepExecution record and return its ID."""
        session = await get_session()
        async with session.begin():
            step_exec = StepExecution(
                task_run_id=self._task_run_id,
                step_name=step_name,
                status="running",
            )
            session.add(step_exec)
            await session.flush()
            return step_exec.id

    async def _update_step_execution(self, step_exec_id: int, result: StepResult, elapsed_ms: int) -> None:
        """Update a StepExecution after step completion."""
        session = await get_session()
        async with session.begin():
            record = await session.get(StepExecution, step_exec_id)
            if record:
                record.status = "passed" if result.success else "failed"
                record.duration_ms = elapsed_ms
                record.cost_usd = Decimal(str(result.cost_usd))
                record.output_summary = result.summary
                record.error_message = result.error if not result.success else None
                record.ended_at = datetime.now(timezone.utc)

                # Create a CostRecord so per-phase/issue breakdowns stay accurate
                if result.cost_usd > 0:
                    cost_record = CostRecord(
                        task_run_id=self._task_run_id,
                        phase=record.step_name,
                        issue=self._ctx.issue_number,
                        model="claude",
                        cost_usd=Decimal(str(result.cost_usd)),
                        duration_ms=elapsed_ms,
                    )
                    session.add(cost_record)

    async def _update_step_execution_gate(self, step_exec_id: int, gate: GateCheckResult) -> None:
        """Record gate check result on the StepExecution."""
        session = await get_session()
        async with session.begin():
            record = await session.get(StepExecution, step_exec_id)
            if record:
                record.gate_check_result = gate.reason
                record.status = "gate_failed"

    async def _record_failure(self, step_name: str, failure_type: str, message: str) -> None:
        """Create a FailureRecord for dashboard observability."""
        session = await get_session()
        async with session.begin():
            failure = FailureRecord(
                task_run_id=self._task_run_id,
                step_name=step_name,
                failure_type=failure_type,
                message=message,
                context={
                    "issue": self._ctx.issue_number,
                    "branch": self._ctx.branch_name,
                    "cost_usd": str(self._ctx.cost_usd),
                },
            )
            session.add(failure)
