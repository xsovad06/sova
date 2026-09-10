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

from sova.core.context import ExecutionContext, TokenUsage
from sova.core.output import OutputWriter
from sova.core.state import TaskStatus
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.db.models import CostRecord, FailureRecord, StepExecution, TaskRun
from sova.db.session import get_session
from sova.ipc.notifications import notify
from sova.llm.client import start_call_counter
from sova.llm.errors import is_billing_failure
from sova.utils.logging import get_logger

log = get_logger(component="workflow")


# Delegation alias: sova.llm.errors owns both the pattern table and the typed
# isinstance dispatch. The exception form is forward-looking: steps stringify
# exceptions into StepResult.error before this ever runs, so the pattern table
# stays the classifier that actually fires today.
_is_billing_failure = is_billing_failure

# Prefix sova.llm.client raises a plain RuntimeError with when
# runaway.max_llm_calls is already exhausted at the invocation boundary (see
# _check_runaway_call_limit), matching _check_runaway_guard's own message
# format. Detected here the same way billing failures are: by the time a step
# raises through to WorkflowEngine, the exception has already been
# stringified into StepResult.error.
_RUNAWAY_GUARD_PREFIX = "Runaway guard:"


def _is_runaway_failure(error: str | None) -> bool:
    """Return True if *error* is a runaway-guard failure raised through the LLM call boundary."""
    return bool(error) and error.startswith(_RUNAWAY_GUARD_PREFIX)


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
    # Cumulative attempts at this step across every pass through
    # _execute_with_retries' outer fallback-switch loop, not just the current
    # model's inner retry loop. The record is reused across fallback switches,
    # so this persists where `retries` (reset per model) does not.
    total_attempts: int = 0


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
        self._run_started_at: float = 0.0

    async def run(self) -> WorkflowResult:
        """Execute all steps in order, respecting gates and retries."""
        # Set once, never re-stamped: the wall-clock guard must not be resettable
        # by re-entering run() on the same engine. The conditional is also what
        # lets a test preset the start to simulate an already-long-running pipeline
        # (time.monotonic() cannot be patched globally without breaking asyncio).
        if not self._run_started_at:
            self._run_started_at = time.monotonic()
            # Fresh counter per run: a resumed run's own retry/fallback loops
            # must not inherit a stale count from whatever call happened to run
            # earlier in this process or asyncio task tree.
            start_call_counter()
        self._ctx.run_started_at = self._run_started_at
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

        await self._check_per_issue_budget(result)
        if result.error:
            return result

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
        # Check worktree existence before any step logic
        if self._ctx.worktree_dir is not None:
            if not self._ctx.worktree_dir.exists():
                error_msg = f"Worktree does not exist: {self._ctx.worktree_dir}"
                log.error("workflow.worktree_deleted", path=str(self._ctx.worktree_dir), step=step.name)
                result.final_status = TaskStatus.FAILED
                result.error = error_msg
                await self._write_output(f"FAILED: {error_msg}")
                await self._close_output()
                await self._record_failure(step.name, "worktree_deleted", error_msg)
                await self._update_task_run_status(TaskStatus.FAILED, error=error_msg)
                return False

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

        if await self._check_runaway_guard(step, result):
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

        if record.status == "runaway":
            await self._pause_for_runaway(step, record, result)
            return False

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

    async def _check_runaway_guard(self, step: BaseStep, result: WorkflowResult) -> bool:
        """Cost-independent runaway guard: wall-clock, step-count, and LLM call-count limits.

        Backstops the dollar-based budget checks (is_budget_exceeded,
        _check_per_issue_budget), which go blind against a provider that
        always reports cost_usd=0 (docs/model-selection-risk-assessment.md,
        R6). Each limit set to 0 disables that check. Returns True if the
        pipeline was paused and should abort.

        The LLM call count (sova.llm.client.get_call_count) is the limit that
        actually catches retry/fix loops burning calls inside a single step's
        execute() (MonitorCIStep's CI-fix loop, AddressReviewStep's consensus
        loop) without ever incrementing steps_completed: invisible to the
        step-count guard alone. Wall clock is scaled by task complexity (the
        same multiplier _step_timeout applies) so a legitimate EPIC run isn't
        paused by a limit sized for the default tier. ``steps_completed`` is
        bounded by ``len(self._steps)`` within a single run (retries produce one
        record, and the engine never loops), so ``max_run_steps`` is a ceiling
        for future longer pipelines rather than an active guard on today's
        16-step developer pipeline.

        A single ``failure_type="runaway_guard"`` is recorded for every trip
        (mirroring the single "budget_exceeded" type), with the specific
        dimension and its value carried in the message so downstream
        consumers (dashboard filters, retry classification) only need to match
        one value.
        """
        from sova.llm.client import get_call_count
        from sova.llm.complexity import complexity_multiplier

        runaway = self._ctx.config.runaway
        elapsed = time.monotonic() - self._run_started_at
        scaled_wall_clock = runaway.max_run_wall_clock_seconds * complexity_multiplier(self._ctx.complexity)
        call_count = get_call_count()

        if runaway.max_run_wall_clock_seconds and elapsed > scaled_wall_clock:
            message = (
                f"Runaway guard: wall clock exceeded {scaled_wall_clock:.0f}s "
                f"(base {runaway.max_run_wall_clock_seconds}s, elapsed {elapsed:.0f}s)"
            )
        elif runaway.max_run_steps and result.steps_completed >= runaway.max_run_steps:
            message = f"Runaway guard: step count exceeded {runaway.max_run_steps} steps"
        elif runaway.max_llm_calls and call_count >= runaway.max_llm_calls:
            message = f"Runaway guard: LLM call count exceeded {runaway.max_llm_calls} calls"
        else:
            return False

        await self._pause_run(step.name, "runaway_guard", message, result)
        return True

    async def _pause_run(self, step_name: str, failure_type: str, message: str, result: WorkflowResult) -> None:
        """Pause the run: record a failure, close output, and persist state.

        Shared by three trip points that must all produce the same PAUSED +
        failure_type="runaway_guard" contract: the pre-step check above, a
        step aborted mid-execution by the wall-clock deadline
        (``_run_step_with_timeout``), and step-attempt-limit exhaustion
        (``_try_step_with_retries``). Without a single helper, the latter two
        would fall through to ``_handle_step_failure``'s generic FAILED +
        "exception" path, which is indistinguishable from an ordinary bug.
        """
        log.warning("workflow.runaway_guard", message=message, step=step_name)
        result.final_status = TaskStatus.PAUSED
        result.error = message
        await self._write_output(f"PAUSED: {message}")
        await self._close_output()
        await self._record_failure(step_name, failure_type, message)
        await self._update_task_run_status(TaskStatus.PAUSED, error=message)
        await self._sync_task_run_context()

    async def _pause_for_runaway(self, step: BaseStep, record: StepRecord, result: WorkflowResult) -> None:
        """Pause the run for a runaway trip discovered inside the retry loop.

        Covers the two trip points ``_check_runaway_guard`` cannot see because
        they fire mid-attempt rather than between steps: a step aborted by the
        wall-clock deadline (``record.result.runaway_triggered``) and
        step-attempt-limit exhaustion (``runaway.max_step_attempts``). Both
        paths set ``record.status = "runaway"`` and stash the message on
        ``record.result.error`` before returning here.
        """
        message = (record.result.error if record.result else None) or f"Runaway guard tripped in step '{step.name}'"
        await self._pause_run(step.name, "runaway_guard", message, result)

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
        """Execute a step, retrying on failure up to max_retries.

        When all retries are exhausted with a billing/rate-limit failure,
        advances to the next model in the configured fallback chain and
        resets the retry counter.
        """
        record = StepRecord(step_name=step.name, status="pending")
        fallback_chain = self._ctx.config.agent.fallback_models

        while True:
            result = await self._try_step_with_retries(step, record)
            if result != "billing_exhausted":
                return record

            next_model = self._advance_fallback(fallback_chain)
            if next_model is None:
                log.warning(
                    "workflow.fallback.exhausted",
                    step=step.name,
                    chain=[self._ctx.config.agent.model, *fallback_chain],
                )
                record.status = "failed"
                return record

            log.info(
                "workflow.fallback.switch",
                step=step.name,
                from_model=self._ctx.resolved_model,
                to_model=next_model,
            )
            self._ctx.resolved_model = next_model
            self._ctx.model_selection_reason = f"fallback (billing failure) -> {next_model}"

    async def _try_step_with_retries(self, step: BaseStep, record: StepRecord) -> str:
        """Run the step retry loop for the current model.

        Returns:
            "done" if step succeeded, "failed" for non-billing failure,
            "billing_exhausted" if all retries failed with billing errors,
            "runaway" if a runaway guard tripped (attempt limit or a
            wall-clock/call-count deadline caught mid-attempt); never retried.
        """
        attempts = 0
        max_attempts = step.max_retries + 1
        max_total_attempts = self._ctx.config.runaway.max_step_attempts
        last_was_billing = False

        while attempts < max_attempts:
            # Incremented here (per attempt, cumulative across every fallback
            # switch via _execute_with_retries' outer loop reusing `record`),
            # not in the outer loop: each billing_exhausted advance resets
            # `attempts` to 0 for the new model, so without a counter that
            # survives model switches a long fallback chain could retry a
            # single step an unbounded number of times. 0 disables the cap.
            if max_total_attempts and record.total_attempts >= max_total_attempts:
                message = f"Runaway guard: step '{step.name}' exceeded {max_total_attempts} attempts"
                log.warning(
                    "workflow.step.max_attempts_exceeded",
                    step=step.name,
                    total_attempts=record.total_attempts,
                    limit=max_total_attempts,
                )
                # Routed as a runaway trip (PAUSED), not a generic failure
                # (FAILED): an attempt-limit exhaustion is the runaway guard's
                # own contract, not an ordinary step bug, and must be
                # resumable the same way the pre-step wall-clock/call-count
                # checks are.
                record.result = StepResult(success=False, summary=message, error=message)
                record.status = "runaway"
                return "runaway"
            record.total_attempts += 1

            attempt_result = await self._execute_single_attempt(step, record, attempts)
            attempts += 1
            record.retries = attempts - 1

            if attempt_result == "continue":
                continue
            if attempt_result == "runaway":
                record.status = "runaway"
                return "runaway"
            if attempt_result == "done":
                record.status = "done"
                log.info("workflow.step.done", step=step.name, duration_ms=record.duration_ms)
                return "done"
            if attempt_result in ("failed", "billing_exhausted"):
                last_was_billing = attempt_result == "billing_exhausted"
                if attempts < max_attempts and attempt_result != "billing_exhausted":
                    log.info("workflow.step.retry", step=step.name, attempt=attempts)
                    continue
                record.status = "failed"
                return attempt_result

        record.status = "failed"
        return "billing_exhausted" if last_was_billing and self._has_fallback_models() else "failed"

    async def _execute_single_attempt(self, step: BaseStep, record: StepRecord, attempt: int) -> str:
        """Execute a single attempt of a step.

        Returns:
            "done" if succeeded, "failed" for non-billing failure,
            "billing_exhausted" for billing failure with fallbacks available,
            "runaway" if the runaway guard tripped mid-attempt (wall-clock
            deadline expiry, or an LLM call rejected at the invocation
            boundary once runaway.max_llm_calls was already reached),
            "continue" to retry with same model.
        """
        step_exec_id = await self._prepare_step_execution(step, record, attempt)
        if step_exec_id is None:
            return "continue"

        start = time.monotonic()
        step_result = await self._run_step_with_timeout(step)
        elapsed_ms = int((time.monotonic() - start) * 1000)

        record.duration_ms = elapsed_ms
        record.result = step_result

        await self._persist_step_result(step_exec_id, step_result, elapsed_ms, step.name)

        if not step_result.success:
            if step_result.runaway_triggered:
                return "runaway"
            return self._handle_step_failure_result(step_result)

        return await self._validate_step_gate(step, step_exec_id, record)

    async def _prepare_step_execution(self, step: BaseStep, record: StepRecord, attempt: int) -> int | None:
        """Create StepExecution record. Returns None on failure (caller should continue retry loop)."""
        try:
            step_exec_id = await self._create_step_execution(step.name, retry_count=attempt)
            record.step_exec_id = step_exec_id
            return step_exec_id
        except Exception as exc:
            log.warning("workflow.step_exec.create_failed", step=step.name, error=str(exc), exc_info=True)
            record.result = StepResult(
                success=False,
                summary=f"DB error creating step execution for {step.name}",
                error=str(exc),
            )
            return None

    async def _run_step_with_timeout(self, step: BaseStep) -> StepResult:
        """Execute step with timeout and exception handling.

        On timeout: if the worktree has staged changes, commits them with a
        WIP message so partial work is not lost. Sets partial_work=True in the
        returned StepResult so the dashboard can surface it.
        """
        timeout_seconds, runaway_deadline = self._effective_step_timeout(step.name)
        usage_before = self._ctx.usage_snapshot()
        cost_before = self._ctx.cost_usd
        try:
            async with asyncio.timeout(timeout_seconds):
                result = await step.execute(self._ctx)
        except TimeoutError:
            partial_work = await self._preserve_partial_work_on_timeout(step.name)
            if runaway_deadline:
                # The scaled runaway.max_run_wall_clock_seconds deadline, not
                # the step's own configured timeout, was the binding
                # constraint: _check_runaway_guard only checks between steps,
                # so a long-running attempt could otherwise keep going past
                # the deadline until its own (longer) timeout fired.
                message = (
                    f"Runaway guard: wall clock exceeded during step '{step.name}' "
                    f"(step aborted after {timeout_seconds}s of remaining budget)"
                )
                result = StepResult(
                    success=False,
                    summary=message,
                    error=message,
                    partial_work=partial_work,
                    cost_usd=self._ctx.cost_usd - cost_before,
                    runaway_triggered=True,
                )
            else:
                result = StepResult(
                    success=False,
                    summary=f"Step '{step.name}' exceeded hard timeout ({timeout_seconds}s)",
                    error="step_hard_timeout",
                    partial_work=partial_work,
                    cost_usd=self._ctx.cost_usd - cost_before,
                )
        except Exception as exc:
            log.exception("workflow.step.unhandled_exception", step=step.name, error=str(exc))
            result = StepResult(
                success=False,
                summary=f"Exception in {step.name}",
                error=str(exc),
                cost_usd=self._ctx.cost_usd - cost_before,
            )
        # Spend before a timeout or exception is still billed. The synthetic
        # results above carry it explicitly because _update_step_execution only
        # writes a CostRecord when cost_usd > 0, so leaving it at the default
        # would discard both the cost and the usage attached below.
        result.usage = self._ctx.usage_snapshot() - usage_before
        return result

    async def _preserve_partial_work_on_timeout(self, step_name: str) -> bool:
        """Commit staged changes on timeout to preserve partial work.

        Returns True if partial work was committed, False otherwise.
        """
        from sova.utils.shell import run

        # Guard: ensure we're in a valid git repo
        work_dir = self._ctx.worktree_dir or self._ctx.working_dir
        if not work_dir:
            log.debug("workflow.timeout.no_working_dir")
            return False

        if not (work_dir / ".git").exists():
            log.debug("workflow.timeout.not_a_git_repo")
            return False

        # Check for staged changes (git add -u only stages tracked files)
        try:
            add_result = await run("git", "add", "-u", cwd=work_dir)
            if not add_result.success:
                log.debug("workflow.timeout.add_failed", error=add_result.stderr)
                return False

            # Check if there's anything to commit
            diff_result = await run("git", "diff", "--cached", "--quiet", cwd=work_dir)
            if diff_result.returncode == 0:
                # Exit code 0 means no staged changes
                log.debug("workflow.timeout.no_staged_changes")
                return False

            # Commit partial work
            commit_msg = f"wip: partial work from {step_name} (timeout)"
            commit_result = await run("git", "commit", "-m", commit_msg, cwd=work_dir)
            if commit_result.success:
                log.info("workflow.timeout.partial_work_committed", step=step_name)
                return True
            else:
                log.warning("workflow.timeout.commit_failed", error=commit_result.stderr)
                return False
        except Exception:
            log.warning("workflow.timeout.partial_work_failed", step=step_name, exc_info=True)
            return False

    async def _persist_step_result(
        self, step_exec_id: int, result: StepResult, elapsed_ms: int, step_name: str
    ) -> None:
        """Update StepExecution record with result (non-fatal on failure)."""
        try:
            await self._update_step_execution(step_exec_id, result, elapsed_ms)
        except Exception as exc:
            log.warning("workflow.step_exec.update_failed", step=step_name, error=str(exc), exc_info=True)

    def _handle_step_failure_result(self, step_result: StepResult) -> str:
        """Classify step failure and determine retry strategy."""
        if _is_runaway_failure(step_result.error):
            return "runaway"
        is_billing = _is_billing_failure(step_result.error)
        if is_billing and self._has_fallback_models():
            return "billing_exhausted"
        return "failed"

    async def _validate_step_gate(self, step: BaseStep, step_exec_id: int, record: StepRecord) -> str:
        """Run structural gate check, then heavyweight verification if it passes.

        Wraps validate_output in a configurable timeout (gate_timeout) and
        verify_output in a separate timeout (verify_timeout or step timeout).
        """
        gate_cap = self._ctx.config.validation.gate_timeout
        timeout_seconds = min(self._step_timeout(step.name), gate_cap)
        try:
            async with asyncio.timeout(timeout_seconds):
                gate = await step.validate_output(self._ctx)
        except TimeoutError:
            log.warning("workflow.gate.timeout", step=step.name, timeout_seconds=timeout_seconds)
            gate = GateCheckResult(passed=False, reason=f"Gate check timed out after {timeout_seconds}s")
        except Exception as exc:
            log.warning("workflow.gate.exception", step=step.name, error=str(exc), exc_info=True)
            gate = GateCheckResult(
                passed=False, reason=f"Gate check failed with exception: {type(exc).__name__}: {exc}"
            )

        record.gate = gate

        if not gate.passed:
            log.warning("workflow.gate.failed", step=step.name, reason=gate.reason)
            await self._update_step_execution_gate(step_exec_id, gate)
            return "failed"

        return await self._verify_step_output(step, step_exec_id, record)

    async def _verify_step_output(self, step: BaseStep, step_exec_id: int, record: StepRecord) -> str:
        """Run heavyweight verification after the structural gate passes.

        Uses verify_timeout (0 means full step timeout) clamped to the step
        timeout. Failure is persisted through the same gate path.
        """
        step_timeout = self._step_timeout(step.name)
        verify_timeout = self._ctx.config.validation.verify_timeout
        if verify_timeout > 0:
            timeout_seconds = min(step_timeout, verify_timeout)
        else:
            timeout_seconds = step_timeout

        try:
            async with asyncio.timeout(timeout_seconds):
                gate = await step.verify_output(self._ctx)
        except TimeoutError:
            log.warning("workflow.verify.timeout", step=step.name, timeout_seconds=timeout_seconds)
            gate = GateCheckResult(passed=False, reason=f"Verification timed out after {timeout_seconds}s")
        except Exception as exc:
            log.warning("workflow.verify.exception", step=step.name, error=str(exc), exc_info=True)
            gate = GateCheckResult(
                passed=False, reason=f"Verification failed with exception: {type(exc).__name__}: {exc}"
            )

        if not gate.passed:
            record.gate = gate
            log.warning("workflow.verify.failed", step=step.name, reason=gate.reason)
            await self._update_step_execution_gate(step_exec_id, gate)
            return "failed"

        # Record that verification ran and passed (audit trail).
        record.gate = gate
        return "done"

    def _has_fallback_models(self) -> bool:
        """Check if there are remaining fallback models to try.

        Returns False unless ``llm.engine_owned_fallback`` is set: the fallback
        chain is owned by sova/llm/client.py, and letting the engine advance too
        would nest a second chain walk inside every client-owned one. Flipping
        the flag on restores the legacy engine-driven advance as a rollback path.

        Note: with the flag off (default), a client-owned fallback that recovers
        mid-step never updates ``ctx.resolved_model`` (see the comment on that
        field in sova/core/context.py and docs/model-selection-architecture.md
        Q5). This method's False return is unrelated to and does not fix that
        gap, it only concerns the engine's own retry-then-advance path.
        """
        if not self._ctx.config.llm.engine_owned_fallback:
            return False
        chain = self._ctx.config.agent.fallback_models
        return self._ctx.fallback_model_index < len(chain)

    def _advance_fallback(self, chain: list[str]) -> str | None:
        """Advance to the next fallback model, skipping duplicates.

        Returns the next model name or None if exhausted.
        """
        current = self._ctx.resolved_model or self._ctx.config.agent.model
        while self._ctx.fallback_model_index < len(chain):
            candidate = chain[self._ctx.fallback_model_index]
            self._ctx.fallback_model_index += 1
            if candidate != current:
                return candidate
        return None

    def _step_timeout(self, step_name: str) -> int:
        """Return the hard timeout in seconds for a given step.

        monitor_ci gets ci.max_wait + a 120s grace period;
        develop uses develop.step_timeout;
        all other steps use agent.step_timeout.

        Complexity multiplier (shared with the wall-clock runaway guard via
        sova.llm.complexity.complexity_multiplier): COMPLEX issues get 1.5x
        timeout, EPIC get 2.0x, capped at 3.0x (max multiplier). Applied to
        the final computed value so all paths benefit.
        """
        from sova.llm.complexity import complexity_multiplier

        if step_name == "monitor_ci":
            base = self._ctx.config.ci.max_wait + 120
        elif step_name == "develop":
            base = min(self._ctx.config.develop.step_timeout, self._ctx.config.agent.step_timeout)
        else:
            base = self._ctx.config.agent.step_timeout

        return int(base * complexity_multiplier(self._ctx.complexity))

    def _effective_step_timeout(self, step_name: str) -> tuple[int, bool]:
        """Return ``(timeout_seconds, capped_by_runaway)`` for the active attempt.

        ``_check_runaway_guard`` only checks the scaled wall-clock deadline
        between steps, so a step whose own ``_step_timeout`` is longer than
        what remains of that deadline could otherwise keep running past it.
        Caps the step's timeout to whatever remains of
        ``runaway.max_run_wall_clock_seconds`` (scaled by complexity, same as
        the pre-step guard), so a step never runs past the deadline just
        because its own timeout is longer. ``capped_by_runaway`` is True when
        the runaway deadline (not the step's own timeout) is the binding
        constraint, so the caller can attribute a subsequent TimeoutError to
        the runaway guard's PAUSED path rather than the generic
        FAILED/step_hard_timeout path.
        """
        step_timeout = self._step_timeout(step_name)
        runaway = self._ctx.config.runaway
        if not runaway.max_run_wall_clock_seconds or not self._run_started_at:
            return step_timeout, False

        from sova.llm.complexity import complexity_multiplier

        scaled = runaway.max_run_wall_clock_seconds * complexity_multiplier(self._ctx.complexity)
        remaining = max(0, int(scaled - (time.monotonic() - self._run_started_at)))
        if remaining < step_timeout:
            return remaining, True
        return step_timeout, False

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

    async def _check_per_issue_budget(self, result: WorkflowResult) -> None:
        """Check if the per-issue budget has been exceeded by prior runs.

        Queries all TaskRuns for this issue and sums their cost. If the total
        exceeds max_issue_budget, sets result.error and result.final_status to
        abort the pipeline before any steps execute.

        Skipped when ``self._ctx.budget_override`` is True (budget override
        explicitly confirmed via the dashboard modal or ``--budget-override``
        CLI flag). The generic ``force`` flag does NOT skip this check on its
        own, so callers that pass ``force=True`` for unrelated reasons (e.g.
        ``resume_from_approval``) still get the hard stop when over budget.
        """
        if self._ctx.budget_override:
            return

        if not self._ctx.issue_number:
            return

        max_issue_budget = self._ctx.config.agent.max_issue_budget
        async with await get_session() as session:
            from sqlalchemy import func, select

            _TERMINAL = ("done", "failed", "rejected", "interrupted", "paused")
            stmt = (
                select(func.coalesce(func.sum(TaskRun.total_cost_usd), Decimal("0")))
                .where(TaskRun.issue_number == self._ctx.issue_number)
                .where(TaskRun.id != self._task_run_id)
                .where(TaskRun.status.in_(_TERMINAL))
            )
            prior_cost = await session.scalar(stmt)
            prior_cost = prior_cost or Decimal("0")

            if prior_cost >= max_issue_budget:
                result.final_status = TaskStatus.PAUSED
                result.error = (
                    f"Per-issue budget exceeded: ${prior_cost:.2f} spent on issue "
                    f"#{self._ctx.issue_number} (limit: ${max_issue_budget})"
                )
                try:
                    await self._write_output(f"PAUSED: {result.error}")
                    await self._close_output()
                    await self._record_failure("budget_check", "per_issue_budget_exceeded", result.error)
                    await self._update_task_run_status(TaskStatus.PAUSED, error=result.error)
                except Exception as exc:
                    log.error("workflow.budget_check_cleanup_failed", error=str(exc), exc_info=True)
                log.warning(
                    "workflow.per_issue_budget_exceeded",
                    issue=self._ctx.issue_number,
                    prior_cost=str(prior_cost),
                    limit=str(max_issue_budget),
                )

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

        Raises RuntimeError if the TaskRun does not exist in the DB,
        which surfaces DB path mismatches instead of silently proceeding
        with no step tracking.
        """
        async with await get_session() as session, session.begin():
            task_run = await session.get(TaskRun, self._task_run_id)
            if not task_run:
                db_url = os.environ.get("SOVA_DATABASE_URL", str(self._ctx.project_dir / ".claude" / "sova.db"))
                log.error(
                    "workflow.adopt_missing_task_run",
                    run_id=self._task_run_id,
                    project_dir=str(self._ctx.project_dir),
                    db_url=db_url,
                )
                raise RuntimeError(
                    f"TaskRun {self._task_run_id} not found in DB (project_dir={self._ctx.project_dir}, db={db_url})"
                )
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
                if self._ctx.confidence_score is not None:
                    task_run.assessment_json = {
                        "confidence_score": self._ctx.confidence_score,
                        "confidence_details": self._ctx.confidence_details,
                    }

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
                    usage = result.usage or TokenUsage()
                    cost_record = CostRecord(
                        task_run_id=self._task_run_id,
                        phase=record.step_name,
                        issue=self._ctx.issue_number or self._ctx.run_label or "",
                        model=self._ctx.resolved_model or "claude",
                        cost_usd=result.cost_usd,
                        duration_ms=elapsed_ms,
                        model_selection_reason=self._ctx.model_selection_reason,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        cache_tokens=usage.cache_read_tokens + usage.cache_write_tokens,
                        cache_read_tokens=usage.cache_read_tokens,
                        cache_write_tokens=usage.cache_write_tokens,
                        tokens_saved=usage.tokens_saved,
                        pre_compression_input_tokens=(
                            usage.input_tokens + usage.tokens_saved if usage.tokens_saved is not None else None
                        ),
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
