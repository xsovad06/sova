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

import asyncio
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from sova.core.context import ExecutionContext
from sova.core.output import OutputWriter
from sova.core.state import TaskStatus
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.db.models import CostRecord, FailureRecord, StepExecution, TaskRun
from sova.db.session import get_session
from sova.ipc.notifications import notify
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
    "commit": TaskStatus.COMMITTING,
    "validate": TaskStatus.COMMITTING,
    "rebase": TaskStatus.ADDRESSING_REVIEW,
    "push": TaskStatus.PUSHING,
    "create_pr": TaskStatus.PR_CREATED,
    "monitor_ci": TaskStatus.CI_MONITORING,
    "address_review": TaskStatus.ADDRESSING_REVIEW,
    "handoff_to_reviewer": TaskStatus.DONE,
    "handoff_to_user": TaskStatus.DONE,
}


@dataclass
class StepRecord:
    """In-memory record of a single step execution."""

    step_name: str
    status: str  # "done", "failed", "skipped"
    result: StepResult | None = None
    gate: GateCheckResult | None = None
    duration_ms: int = 0
    retries: int = 0
    step_exec_id: int | None = None


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
        self._output_writer: OutputWriter | None = None

    async def run(self) -> WorkflowResult:
        """Execute all steps in order, respecting gates and retries."""
        if self._ctx.task_run_id is not None:
            self._task_run_id = self._ctx.task_run_id
            await self._adopt_task_run()
        else:
            self._task_run_id = await self._create_task_run()
            self._ctx.task_run_id = self._task_run_id

        self._output_writer = OutputWriter(self._ctx.project_dir, self._task_run_id)
        self._ctx.output_writer = self._output_writer
        self._output_writer.write_line(f"=== Workflow started: {self._ctx.display_label}, role={self._ctx.role} ===")
        await self._output_writer.flush()

        result = WorkflowResult(
            success=False,
            final_status=TaskStatus.PENDING,
            task_run_id=self._task_run_id,
        )

        log.info(
            "workflow.start",
            issue=self._ctx.issue_number or "",
            label=self._ctx.display_label,
            run_id=self._task_run_id,
        )

        for step in self._steps:
            if not await self._execute_step(step, result):
                return result

        await self._finalize(result)
        return result

    async def _execute_step(self, step: BaseStep, result: WorkflowResult) -> bool:
        """Execute a single pipeline step. Returns False to abort the pipeline."""
        if self._ctx.is_budget_exceeded:
            log.warning("workflow.budget_exceeded", cost=str(self._ctx.cost_usd))
            result.final_status = TaskStatus.PAUSED
            result.error = f"Budget exceeded: ${self._ctx.cost_usd}"
            await self._write_output(f"PAUSED: {result.error}")
            await self._close_output()
            await self._record_failure(step.name, "budget_exceeded", result.error)
            await self._update_task_run_status(TaskStatus.PAUSED, error=result.error)
            await self._sync_task_run_context()
            return False

        if await step.can_skip(self._ctx):
            log.info("workflow.step.skip", step=step.name)
            result.steps_skipped += 1
            result.step_records.append(StepRecord(step_name=step.name, status="skipped"))
            try:
                now = datetime.now(timezone.utc)
                await self._create_step_execution(
                    step.name,
                    status="skipped",
                    duration_ms=0,
                    started_at=now,
                    ended_at=now,
                )
            except Exception:
                log.warning("workflow.step_exec.skip_create_failed", step=step.name, exc_info=True)
            return True

        await self._set_current_step(step.name)
        await self._write_output(f"\n--- Step: {step.name} ---")

        record = await self._execute_with_retries(step)
        result.step_records.append(record)

        if record.result and record.result.summary:
            await self._write_output(record.result.summary)

        if record.status == "failed":
            await self._handle_step_failure(step, record, result)
            return False

        result.steps_completed += 1
        result.total_cost_usd += record.result.cost_usd if record.result else Decimal("0")
        await self._sync_task_run_context()

        if record.result and record.result.awaiting_approval:
            await self._handle_step_approval(step, record, result)
            return False

        step_status = _STEP_STATUS_MAP.get(step.name)
        if step_status:
            result.final_status = step_status

        return True

    async def _handle_step_failure(self, step: BaseStep, record: StepRecord, result: WorkflowResult) -> None:
        """Handle a failed step: record failure, notify, close output."""
        result.steps_failed += 1
        result.error = record.result.error if record.result else "Unknown error"

        if record.gate and not record.gate.passed:
            result.final_status = TaskStatus.PAUSED
            result.error = record.gate.reason
            failure_type = "gate_check"
        else:
            result.final_status = TaskStatus.FAILED
            failure_type = "exception"

        await self._record_failure(step.name, failure_type, result.error or "Unknown error")
        await self._update_task_run_status(result.final_status, error=result.error)
        await self._sync_task_run_context()

        await self._write_output(f"FAILED: {result.error}")
        await self._close_output()

        role_label = self._ctx.role.capitalize()
        project_name = self._ctx.project_dir.name
        label = self._ctx.display_label
        notify(
            self._ctx.config.notification,
            "SOVA",
            f"{project_name} | Step '{step.name}' failed: {result.error or 'Unknown error'}",
            subtitle=f"{role_label} failed {label}",
            group=self._ctx.notification_group,
        )

        log.error(
            "workflow.step.failed",
            step=step.name,
            error=result.error,
            status=result.final_status,
        )

    async def _handle_step_approval(self, step: BaseStep, record: StepRecord, result: WorkflowResult) -> None:
        """Handle a step requesting human approval: pause pipeline, notify.

        Two notification patterns exist for steps that pause for approval:
        (A) Step calls write_step_handoff(..., awaiting_approval=True) and handles its own
            handoff file + notification. Return StepResult with awaiting_approval=True but
            no handoff_actions -- the engine only sets DB status and sends its own notification.
        (B) Step returns StepResult(awaiting_approval=True, handoff_actions=[...]) and lets
            the engine write the handoff file via _write_approval_handoff below.
        SpecStep uses pattern (A); generic approval steps use pattern (B).
        """
        result.final_status = TaskStatus.AWAITING_APPROVAL
        await self._write_output(f"AWAITING APPROVAL: {record.result.summary}")
        await self._update_step_execution_status(record.step_exec_id, TaskStatus.AWAITING_APPROVAL.value)
        await self._update_task_run_status(TaskStatus.AWAITING_APPROVAL)

        if record.result.handoff_actions:
            self._write_approval_handoff(step.name, record.result)

        await self._close_output()

        role_label = self._ctx.role.capitalize()
        project_name = self._ctx.project_dir.name
        label = self._ctx.display_label
        notify(
            self._ctx.config.notification,
            "SOVA",
            f"{project_name} | Step '{step.name}' awaiting approval",
            subtitle=f"{role_label} paused {label}",
            group=self._ctx.notification_group,
        )

        log.info("workflow.step.awaiting_approval", step=step.name)

    async def _finalize(self, result: WorkflowResult) -> None:
        """Write final state after all steps complete successfully."""
        result.success = True
        result.final_status = TaskStatus.DONE
        await self._write_output(f"\n=== Workflow completed: ${result.total_cost_usd} ===")
        await self._close_output()
        await self._update_task_run_status(TaskStatus.DONE)
        await self._finalize_task_run()

        role_label = self._ctx.role.capitalize()
        project_name = self._ctx.project_dir.name
        label = self._ctx.display_label
        notify(
            self._ctx.config.notification,
            "SOVA",
            f"{project_name} | ${result.total_cost_usd}",
            subtitle=f"{role_label} finished {label}",
            group=self._ctx.notification_group,
        )

        log.info("workflow.done", label=label, cost=str(result.total_cost_usd))

    async def _execute_with_retries(self, step: BaseStep) -> StepRecord:
        """Execute a step, retrying on failure up to max_retries."""
        record = StepRecord(step_name=step.name, status="pending")
        attempts = 0
        max_attempts = step.max_retries + 1

        while attempts < max_attempts:
            start = time.monotonic()

            try:
                step_exec_id = await self._create_step_execution(step.name, retry_count=attempts)
                record.step_exec_id = step_exec_id
            except Exception as exc:
                log.warning("workflow.step_exec.create_failed", step=step.name, error=str(exc), exc_info=True)
                step_result = StepResult(
                    success=False,
                    summary=f"DB error creating step execution for {step.name}",
                    error=str(exc),
                )
                attempts += 1
                record.retries = attempts - 1
                record.result = step_result
                if attempts < max_attempts:
                    continue
                record.status = "failed"
                return record

            timeout_seconds = self._step_timeout(step.name)
            try:
                async with asyncio.timeout(timeout_seconds):
                    step_result = await step.execute(self._ctx)
            except TimeoutError:
                step_result = StepResult(
                    success=False,
                    summary=f"Step '{step.name}' exceeded hard timeout ({timeout_seconds}s)",
                    error="step_hard_timeout",
                )
            except Exception as exc:
                step_result = StepResult(success=False, summary=f"Exception in {step.name}", error=str(exc))

            elapsed_ms = int((time.monotonic() - start) * 1000)
            record.duration_ms = elapsed_ms
            record.result = step_result
            attempts += 1
            record.retries = attempts - 1

            try:
                await self._update_step_execution(step_exec_id, step_result, elapsed_ms)
            except Exception as exc:
                log.warning("workflow.step_exec.update_failed", step=step.name, error=str(exc), exc_info=True)

            if not step_result.success:
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

            record.status = "done"
            log.info("workflow.step.done", step=step.name, duration_ms=record.duration_ms)
            return record

        record.status = "failed"
        return record

    def _step_timeout(self, step_name: str) -> int:
        """Return the hard timeout in seconds for a given step.

        monitor_ci gets ci.max_wait + a 120s grace period;
        all other steps use agent.step_timeout.
        """
        if step_name == "monitor_ci":
            return self._ctx.config.ci.max_wait + 120
        return self._ctx.config.agent.step_timeout

    # -- Output helpers --

    async def _write_output(self, text: str) -> None:
        if self._output_writer:
            self._output_writer.write_line(text)
            if self._output_writer.should_flush():
                await self._output_writer.flush()

    async def _close_output(self) -> None:
        if self._output_writer:
            await self._output_writer.close()
            self._output_writer = None

    # -- DB persistence --

    async def _create_task_run(self) -> int:
        """Create the initial TaskRun record and return its ID."""
        async with await get_session() as session, session.begin():
            task_run = TaskRun(
                issue_number=self._ctx.issue_number or None,
                run_label=self._ctx.run_label,
                role=self._ctx.role,
                status=TaskStatus.PENDING.value,
                branch_name=self._ctx.branch_name,
                resumed_from_id=self._ctx.resume_run_id,
                pid=os.getpid(),
            )
            session.add(task_run)
            await session.flush()
            return task_run.id

    async def _adopt_task_run(self) -> None:
        """Adopt an existing TaskRun created by the dashboard.

        Clears the "agent" sentinel and preserves "running" status so the
        dashboard correctly reflects that steps are executing. Preserves the
        PID field (the dashboard uses the subprocess PID for process management).
        """
        async with await get_session() as session, session.begin():
            task_run = await session.get(TaskRun, self._task_run_id)
            if task_run:
                task_run.status = TaskStatus.RUNNING.value
                task_run.current_step = None
                task_run.resumed_from_id = self._ctx.resume_run_id

    async def _set_current_step(self, step_name: str) -> None:
        """Update the current_step field on the TaskRun."""
        async with await get_session() as session, session.begin():
            task_run = await session.get(TaskRun, self._task_run_id)
            if task_run:
                task_run.current_step = step_name

    async def _update_task_run_status(self, status: TaskStatus, *, error: str | None = None) -> None:
        """Update the TaskRun status and optional error message."""
        async with await get_session() as session, session.begin():
            task_run = await session.get(TaskRun, self._task_run_id)
            if task_run:
                task_run.status = status.value
                task_run.total_cost_usd = self._ctx.cost_usd
                if error:
                    task_run.error_message = error
                if status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.PAUSED, TaskStatus.AWAITING_APPROVAL):
                    task_run.ended_at = datetime.now(timezone.utc)

    async def _sync_task_run_context(self) -> None:
        """Persist mutable context fields to the TaskRun so they survive crashes and are available on resume."""
        async with await get_session() as session, session.begin():
            task_run = await session.get(TaskRun, self._task_run_id)
            if task_run:
                task_run.branch_name = self._ctx.branch_name
                task_run.total_cost_usd = Decimal(str(self._ctx.cost_usd))
                if self._ctx.worktree_dir:
                    task_run.worktree_path = str(self._ctx.worktree_dir)
                if self._ctx.pr_number:
                    task_run.pr_number = self._ctx.pr_number

    async def _finalize_task_run(self) -> None:
        """Write final state to the TaskRun after successful completion."""
        async with await get_session() as session, session.begin():
            task_run = await session.get(TaskRun, self._task_run_id)
            if task_run:
                task_run.total_cost_usd = self._ctx.cost_usd
                task_run.branch_name = self._ctx.branch_name
                task_run.pr_number = self._ctx.pr_number
                if self._ctx.worktree_dir:
                    task_run.worktree_path = str(self._ctx.worktree_dir)
                if not task_run.ended_at:
                    task_run.ended_at = datetime.now(timezone.utc)

    async def _update_step_execution_status(self, step_exec_id: int | None, status: str) -> None:
        """Update a StepExecution's status by ID."""
        if step_exec_id is None:
            return
        try:
            async with await get_session() as session, session.begin():
                step_exec = await session.get(StepExecution, step_exec_id)
                if step_exec:
                    step_exec.status = status
        except Exception as exc:
            log.warning(
                "workflow.step_exec.status_update_failed", step_exec_id=step_exec_id, error=str(exc), exc_info=True
            )

    def _write_approval_handoff(self, step_name: str, result: StepResult) -> None:
        """Write a DashboardHandoff with approval actions from a paused step."""
        try:
            from sova.ipc.handoff import DashboardHandoff, write_handoff_file

            dashboard_handoff = DashboardHandoff(
                source=self._ctx.role,
                status="awaiting_action",
                issue=self._ctx.issue_number or "",
                pr_number=self._ctx.pr_number,
                branch=self._ctx.branch_name,
                summary=f"Step '{step_name}' awaiting approval: {result.summary}",
                details={
                    "step": step_name,
                    "cost_usd": str(self._ctx.cost_usd),
                    "task_run_id": self._task_run_id,
                },
                next_actions=result.handoff_actions or [],
            )
            write_handoff_file(self._ctx.project_dir, dashboard_handoff)
        except Exception:
            log.warning("workflow.approval_handoff.write_failed", step=step_name, exc_info=True)

    async def _create_step_execution(
        self,
        step_name: str,
        retry_count: int = 0,
        *,
        status: str = "running",
        duration_ms: int | None = None,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
    ) -> int:
        """Create a StepExecution record and return its ID."""
        async with await get_session() as session, session.begin():
            step_exec = StepExecution(
                task_run_id=self._task_run_id,
                step_name=step_name,
                status=status,
                retry_count=retry_count,
            )
            if started_at is not None:
                step_exec.started_at = started_at
            if duration_ms is not None:
                step_exec.duration_ms = duration_ms
            if ended_at is not None:
                step_exec.ended_at = ended_at
            session.add(step_exec)
            await session.flush()
            return step_exec.id

    async def _update_step_execution(self, step_exec_id: int, result: StepResult, elapsed_ms: int) -> None:
        """Update a StepExecution after step completion."""
        async with await get_session() as session, session.begin():
            record = await session.get(StepExecution, step_exec_id)
            if record:
                record.status = "done" if result.success else "failed"
                record.duration_ms = elapsed_ms
                record.cost_usd = result.cost_usd
                record.output_summary = result.summary
                record.error_message = result.error if not result.success else None
                record.ended_at = datetime.now(timezone.utc)

                if result.cost_usd > 0:
                    cost_record = CostRecord(
                        task_run_id=self._task_run_id,
                        phase=record.step_name,
                        issue=self._ctx.issue_number or self._ctx.run_label or "",
                        model=self._ctx.resolved_model or "claude",
                        cost_usd=result.cost_usd,
                        duration_ms=elapsed_ms,
                        model_selection_reason=self._ctx.model_selection_reason,
                    )
                    session.add(cost_record)

    async def _update_step_execution_gate(self, step_exec_id: int, gate: GateCheckResult) -> None:
        """Record gate check result on the StepExecution."""
        async with await get_session() as session, session.begin():
            record = await session.get(StepExecution, step_exec_id)
            if record:
                record.gate_check_result = gate.reason
                record.status = "gate_failed"

    async def _record_failure(self, step_name: str, failure_type: str, message: str) -> None:
        """Create a FailureRecord for dashboard observability."""
        async with await get_session() as session, session.begin():
            failure = FailureRecord(
                task_run_id=self._task_run_id,
                step_name=step_name,
                failure_type=failure_type,
                message=message,
                context={
                    "issue": self._ctx.issue_number or "",
                    "label": self._ctx.display_label,
                    "branch": self._ctx.branch_name,
                    "cost_usd": str(self._ctx.cost_usd),
                },
            )
            session.add(failure)
