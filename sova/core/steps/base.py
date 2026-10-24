"""Base step abstraction with gate checks.

Every workflow step inherits from BaseStep and implements:
- execute(): do the work
- validate_output(): structural gate check (fast, required)
- verify_output(): heavyweight verification (optional, e.g. test suite)
- can_skip(): should this step be skipped for this context?
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from sova.core.context import ExecutionContext, TokenUsage
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.ipc.handoff import HandoffAction
    from sova.llm.models import LLMResult

log = get_logger(component="step.base")


@dataclass
class StepResult:
    """Outcome of a step execution."""

    success: bool
    summary: str
    error: str | None = None
    cost_usd: Decimal = Decimal("0")
    awaiting_approval: bool = False
    handoff_actions: list[HandoffAction] | None = None
    partial_work: bool = False
    # Set by WorkflowEngine from the context delta, not by steps themselves.
    usage: TokenUsage | None = None
    # Set by WorkflowEngine._run_step_with_timeout when a step is aborted
    # because the runaway wall-clock deadline (not the step's own configured
    # timeout) expired mid-execution. Routes the failure through the same
    # PAUSED + failure_type="runaway_guard" path as the pre-step guard check,
    # instead of the generic FAILED/step_hard_timeout path.
    runaway_triggered: bool = False


@dataclass
class GateCheckResult:
    """Outcome of a post-step validation gate."""

    passed: bool
    reason: str | None = None


class BaseStep(ABC):
    """Abstract base for all workflow steps."""

    name: str = ""
    max_retries: int = 0
    # Routing category for llm.routing lookups, empty when the step is not
    # routable. Steps pass it to the LLM call via ctx.routing_task_type().
    TASK_TYPE: str = ""
    # True when this step edits the working tree and therefore needs a real
    # coding-agent runtime (sova.ipc.runtime.AgentRuntime), not a text-only
    # LLMProvider. Steps that set this must dispatch through
    # sova.core.agent_dispatch.dispatch_command() rather than calling
    # sova.llm.client.invoke_command() directly, so a text-only provider
    # fails fast instead of silently producing a no-op text response.
    requires_tools: bool = False

    @abstractmethod
    async def execute(self, ctx: ExecutionContext) -> StepResult:
        """Execute the step and return a result."""

    @abstractmethod
    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Validate that the step produced acceptable output."""

    async def verify_output(self, ctx: ExecutionContext) -> GateCheckResult:  # noqa: RUF029  # NOSONAR(S7503)
        """Heavyweight verification pass after the structural gate passes.

        Override in subclasses that need expensive verification (e.g., running
        the full test suite). The default is a passing no-op, but it stays a
        coroutine (python:S7503 accepted exception, issue #1193): every
        caller awaits it polymorphically through the ABC, and real overrides
        (e.g. ValidateStep) do await inside their body.
        """
        return GateCheckResult(passed=True)

    async def can_skip(self, ctx: ExecutionContext) -> bool:  # noqa: RUF029  # NOSONAR(S7503)
        """Whether this step can be skipped for the given context.

        Default: skip if this step already passed in a prior run (resume).
        Override in subclasses for additional skip logic. Stays a coroutine
        (python:S7503 accepted exception, issue #1193) for the same
        polymorphic-await reason as verify_output() above.
        """
        return self.name in ctx.completed_steps


def resolve_fix_attempt_route(ctx: ExecutionContext, task_type: str, attempt: int) -> tuple[str | None, str | None]:
    """Return the ``(model, task_type)`` pair a fix-loop attempt should dispatch with.

    Shared by ``ValidateStep`` and ``MonitorCIStep``, whose fix loops make the
    identical decision (issue #780): attempt 1 carries its ``task_type`` so a
    configured ``llm.routing[task_type]`` cheap model gets exactly one try,
    and every later attempt drops the task type so the step falls back to the
    model the rest of the developer pipeline uses.

    The model returned is always the step's dev model; dropping the task type
    is what changes which model actually runs, because
    ``client.prepare_invocation()`` resolves a configured route *ahead* of the
    passed model. Attempt 1 goes through ``ctx.routing_task_type()`` rather
    than passing *task_type* straight through, so a model fallback already in
    flight is not pinned back to the route that step-level retry is trying to
    escape.

    The escalation is recorded on ``ctx.step_model_selection_reason`` (step
    scoped, so it cannot relabel a later step's CostRecord) only when attempt
    1 is observed to have actually run a different model: ``ctx.routed_spend``
    is populated by ``ctx.add_usage()`` only when a routed call's reported
    model differed from ``resolved_model``, so gating on it (rather than on
    config alone) avoids a false escalation record in every case where
    config named a route but attempt 1 ran the dev model anyway: the default
    empty ``llm.routing``; a fallback in flight suppressing the route; a
    route ``_ensure_task_type_model_compatible()`` already found the runtime
    could not honour and swapped back to the dev model; a runtime whose
    ``honours_model_override`` is False (e.g. Codex), which never applies the
    route in the first place; and a route that pins back to ``agent.model``
    (same model, different spelling).
    """
    dev_model = ctx.resolved_model or ctx.config.agent.model
    routed_task_type = ctx.routing_task_type(task_type)
    if attempt <= 1:
        return dev_model, routed_task_type

    if ctx.routed_spend:
        log.info("step.escalate_to_dev_model", task_type=task_type, attempt=attempt, model=dev_model)
        ctx.step_model_selection_reason = (
            f"task_type:{task_type} fix attempt {attempt - 1} failed, escalated to dev model {dev_model}"
        )
    return dev_model, None


def fix_attempt_routed_reason(result: LLMResult, task_type: str, attempt_task_type: str | None) -> str | None:
    """Return the ``routed_reason`` a fix-loop attempt passes to ``ctx.add_usage()``, or ``None``.

    Non-``None`` only when *result* itself reports ``routed_task_type_model``:
    ``sova.core.agent_dispatch`` sets that field from the same freshly loaded
    config the dispatch actually used, only when a configured route for
    *task_type* survived both the runtime-compatibility check and any
    role-level runtime pin (issue #780). Reading it here rather than
    re-deriving the answer from ``ctx.config.llm.routing`` (the run-start
    snapshot) means a mid-run ``llm.routing`` edit is reflected identically to
    how dispatch itself saw it, and a route the dispatch swapped back to the
    dev model, or one that merely pins to it under a different spelling,
    never gets split off into its own CostRecord (issue #780 review).
    """
    if attempt_task_type is None or result.routed_task_type_model is None:
        return None
    return f"task_type:{task_type} fix attempt 1 routed to {result.routed_task_type_model}"
