"""Execution context for a task run.

Replaces the bash agent's global variables with a typed, validated context
object that is threaded through every step of the workflow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from sova.adapters.base import Task, TaskAdapter, TaskState
from sova.config.models import ProjectConfig
from sova.core.planning import PlanResult
from sova.llm.complexity import ComplexityTier
from sova.llm.models import CostSource, combine_cost_sources

if TYPE_CHECKING:
    from sova.core.output import OutputWriter
    from sova.llm.models import LLMResult

# Budget degradation thresholds (fraction of max_budget remaining).
# Below each threshold, the developer pipeline degrades gracefully rather
# than risking being killed mid-critical-step.
BUDGET_SKIP_OPTIONAL_THRESHOLD = 0.40
BUDGET_STOP_RETRY_THRESHOLD = 0.20
BUDGET_SKIP_HOOKS_THRESHOLD = 0.08


def _delta_saved(after: int | None, before: int | None, compressed_calls: int) -> int | None:
    """Compression saving attributable to one window.

    ``None`` means no invocation in the window ran compression, which is a
    different fact from running and saving nothing (``0``), and the CostRecord
    column carries that same distinction. Cumulative totals cannot express it
    alone: a window where compression never ran leaves the running total
    untouched, which subtracts to 0 and reads as a real saving of nothing.
    Hence the separate count of compressed invocations.
    """
    if compressed_calls <= 0:
        return None
    return (0 if after is None else after) - (0 if before is None else before)


@dataclass(frozen=True)
class TokenUsage:
    """Immutable snapshot of accumulated token counters.

    Subtracting two snapshots yields the usage attributable to the work done
    between them, which is how per-step attribution is derived.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    tokens_saved: int | None = None
    compressed_calls: int = 0
    # Count of accumulated LLMResults that reported an explicit cache-write
    # figure (cache_creation_tokens is not None). Zero means every call in
    # this window left the field unset (e.g. a Codex-only step), so
    # cache_write_tokens's running total of 0 is not a confirmed zero and
    # must not be written to CostRecord as one. Mirrors compressed_calls'
    # role in disambiguating tokens_saved.
    cache_write_reported_calls: int = 0
    # Codex-only reasoning-token total and how many calls reported it. Kept
    # separate from output_tokens (which already includes it, per the
    # OpenAI Responses API convention LLMResult.reasoning_output_tokens
    # documents) to avoid double counting.
    reasoning_tokens: int = 0
    reasoning_reported_calls: int = 0

    def __sub__(self, other: TokenUsage) -> TokenUsage:
        compressed_calls = self.compressed_calls - other.compressed_calls
        return TokenUsage(
            input_tokens=self.input_tokens - other.input_tokens,
            output_tokens=self.output_tokens - other.output_tokens,
            cache_read_tokens=self.cache_read_tokens - other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens - other.cache_write_tokens,
            tokens_saved=_delta_saved(self.tokens_saved, other.tokens_saved, compressed_calls),
            compressed_calls=compressed_calls,
            cache_write_reported_calls=self.cache_write_reported_calls - other.cache_write_reported_calls,
            reasoning_tokens=self.reasoning_tokens - other.reasoning_tokens,
            reasoning_reported_calls=self.reasoning_reported_calls - other.reasoning_reported_calls,
        )

    def __add__(self, other: TokenUsage) -> TokenUsage:
        # tokens_saved stays None only when neither side ran compression.
        tokens_saved = (
            None
            if self.tokens_saved is None and other.tokens_saved is None
            else (0 if self.tokens_saved is None else self.tokens_saved)
            + (0 if other.tokens_saved is None else other.tokens_saved)
        )
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            tokens_saved=tokens_saved,
            compressed_calls=self.compressed_calls + other.compressed_calls,
            cache_write_reported_calls=self.cache_write_reported_calls + other.cache_write_reported_calls,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            reasoning_reported_calls=self.reasoning_reported_calls + other.reasoning_reported_calls,
        )


@dataclass
class RoutedSpend:
    """One step-scoped provider spend bucket, for calls a step routed off ``resolved_model``.

    Carries everything ``WorkflowEngine._build_cost_record()`` needs to write
    that bucket as its own CostRecord: the model that ran, why it ran, the
    tokens, the dollars, and the provenance of those dollars.
    """

    model: str
    reason: str | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    cost_usd: Decimal = Decimal("0")
    cost_source: CostSource | None = None


@dataclass
class ExecutionContext:
    """Mutable context passed through every workflow step."""

    # Required
    project_dir: Path
    config: ProjectConfig
    adapter: TaskAdapter
    issue_number: str = ""
    role: str = "developer"
    run_label: str = ""

    # Populated during execution
    task: Task | None = None
    branch_name: str = ""
    worktree_dir: Path | None = None
    pr_number: int | None = None
    pr_url: str = ""
    test_baseline_path: Path | None = None
    session_id: str | None = None
    cost_usd: Decimal = Decimal("0")
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    tokens_saved: int | None = None
    compressed_calls: int = 0
    cache_write_reported_calls: int = 0
    reasoning_tokens: int = 0
    reasoning_reported_calls: int = 0
    force: bool = False
    budget_override: bool = False
    task_run_id: int | None = None
    plan_result: PlanResult | None = None

    # State validation (set by roles that restrict input states)
    allowed_input_states: frozenset[TaskState] | None = None

    # Resume checkpoint (populated when --resume is used)
    resume_run_id: int | None = None
    completed_steps: frozenset[str] = field(default_factory=frozenset)

    # Which step pipeline this run executes ("developer", "address_review",
    # "researcher", "planner"). Set by the role before the WorkflowEngine
    # starts; completed_steps is only populated on --resume, so a step that
    # behaves differently per pipeline (HandoffToReviewerStep) cannot infer
    # the variant from step history on a fresh run.
    pipeline_variant: str = "developer"

    # Output writer (set by WorkflowEngine, used by steps for heartbeats)
    output_writer: OutputWriter | None = None

    # Set by CommitStep when working tree is clean but commits exist ahead of base.
    # MonitorCIStep uses this to check existing CI status instead of polling.
    no_new_commits: bool = False

    # Accumulated during the run
    files_changed: list[str] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)
    addressed_external_findings: list[dict] = field(default_factory=list)
    # Set by AddressReviewStep once the LLM has addressed the reviewer's findings,
    # read by ResolveExternalReviewsStep to post the address summary on the PR
    # after the push, and by the handoff step to record what was addressed.
    addressed_review_findings: list[dict] = field(default_factory=list)

    # Set by ConfidenceScoreStep, read by HandoffToReviewerStep for score-based routing.
    confidence_score: int | None = None
    confidence_details: dict | None = None

    # Complexity-based routing (set by AssessStep, used by all LLM-invoking steps)
    complexity: ComplexityTier | None = None
    # Only WorkflowEngine._advance_fallback (llm.engine_owned_fallback=True) ever
    # writes a fallback winner back here. With the default client-owned fallback
    # loop (sova/llm/client.py:_invoke_with_fallback), a step that recovers via a
    # fallback candidate does NOT update this field: LLMResult.model echoes the
    # provider's own response, which is alias-consistent for claude-code but a
    # concrete API model ID for anthropic_api/litellm, so writing it back here
    # unconditionally would silently break alias-based comparisons elsewhere
    # (_advance_fallback, route_model, _ROLE_MODEL_FIELDS) for those providers.
    # Consequence: later steps keep retrying the original (possibly still-dead)
    # model until ModelAvailabilityCache's TTL skips it again, or forever if the
    # step interval exceeds the TTL. See docs/model-selection-architecture.md Q5.
    resolved_model: str | None = None
    model_selection_reason: str | None = None

    # Step-scoped override for model_selection_reason, for a step that made
    # its own per-attempt routing decision (today only the validate and
    # monitor_ci fix loops' escalation off llm.routing[task_type], issue
    # #780). model_selection_reason itself is run-wide: AssessStep writes it
    # once and every later step's CostRecord reads it, so a step writing its
    # own reason there would relabel every subsequent step's record too.
    # WorkflowEngine resets this before every step attempt and prefers it
    # over model_selection_reason when writing the provider CostRecord, the
    # same split runtime_model/runtime_model_reason use below.
    step_model_selection_reason: str | None = None

    # Step-scoped provider spend a step deliberately routed to a model other
    # than resolved_model, keyed by the model that actually ran (today only
    # the validate and monitor_ci fix loops' attempt-1 llm.routing[task_type]
    # route, issue #780). Mirrors the runtime_usage split: without it, a cheap
    # routed attempt and the dev-model attempt that escalated past it land in
    # one CostRecord labelled with resolved_model, so per-model spend (the
    # whole point of measuring a cheap route) is unattributable, and the
    # escalation reason describes a record holding both models' tokens.
    # WorkflowEngine writes one CostRecord per entry, subtracting each from
    # the step's provider record, and resets this before every step attempt.
    routed_spend: dict[str, RoutedSpend] = field(default_factory=dict)

    # Step-scoped: what the AgentRuntime itself reported running, for a
    # runtime whose honours_model_override is False (today only Codex),
    # whose spawn() ignores the model override entirely so resolved_model
    # (a Claude alias from agent.model, set once in AssessStep) would simply
    # be wrong for it. Kept separate from resolved_model rather than
    # overwriting it: resolved_model is read by every later text-only
    # LLMProvider step (create_pr, spec, research) to pick the model THEY
    # ask for, and those steps have no relationship to what a prior step's
    # AgentRuntime happened to run. WorkflowEngine resets both fields to None
    # before every step attempt (_run_step_with_timeout), so a stale value
    # from one step's runtime dispatch can never leak into a later step's
    # CostRecord attribution. Only the CostRecord writer in
    # WorkflowEngine._update_step_execution reads this, preferring it over
    # resolved_model. Issue #1128.
    runtime_model: str | None = None
    runtime_model_reason: str | None = None

    # Provenance of the dollars behind this step's spend, mirroring
    # runtime_model/resolved_model's split. add_usage() writes both: a result
    # tagged with LLMResult.runtime_name (a model-ignoring runtime's dispatch,
    # today Codex) feeds runtime_cost_source, every other result feeds
    # cost_source. Each is combined with combine_cost_sources(), never
    # overwritten, so the label cannot depend on call order: DevelopStep on
    # Codex adds a dispatch, then a priced implementation-notes call, then a
    # fix-loop dispatch, and the provider record must still read "priced".
    # Both reset to None before every step attempt, same as runtime_model.
    # Issue #1128.
    runtime_cost_source: CostSource | None = None
    cost_source: CostSource | None = None

    # Usage and cost of every runtime-tagged result add_usage() saw in this
    # step attempt, kept apart from the provider path's share. None means no
    # model-ignoring runtime dispatch happened. The step totals (cost_usd and
    # the token counters) still include it; WorkflowEngine._update_step_execution
    # writes it as the runtime's own CostRecord and subtracts it from the step
    # delta, so a step that dispatches to an unpriced runtime and also makes a
    # priced provider call never merges the runtime's tokens into a record
    # labelled with the provider's model. Reset before every step attempt.
    # Issue #1128.
    runtime_usage: TokenUsage | None = None
    runtime_cost_usd: Decimal = Decimal("0")

    # Model fallback chain (index into config.agent.fallback_models)
    fallback_model_index: int = 0

    # Wall-clock start of the current run (time.monotonic()), mirrored from
    # WorkflowEngine._run_started_at. 0.0 means "not set" (e.g. no
    # WorkflowEngine involved, such as reviewer/custom roles), in which case
    # the wall-clock component of resource_remaining_fraction contributes no
    # constraint.
    run_started_at: float = 0.0

    # Wall-clock start of the current step attempt and its own effective hard
    # timeout in seconds, both mirrored by WorkflowEngine._run_step_with_timeout
    # immediately before step.execute() runs (from the same
    # _effective_step_timeout() value that bounds the step's asyncio.timeout).
    # 0.0 / None mean "not set" (no WorkflowEngine involved, or a fresh
    # dataclass in a unit test), matching run_started_at's convention.
    step_started_at: float = 0.0
    step_deadline_seconds: int | None = None

    # True when step_deadline_seconds was capped by the run-wide
    # runaway.max_run_wall_clock_seconds guard rather than the step's own
    # configured timeout (mirrors _effective_step_timeout's capped_by_runaway
    # return value). A caller that preemptively bails out of a retry loop
    # based on step_time_remaining must not do so while this is True: the
    # outer asyncio.timeout is expected to fire and route the failure through
    # WorkflowEngine's runaway_triggered/_pause_for_runaway (resumable PAUSED)
    # path, not a plain failed StepResult, which would silently drop the
    # runaway guard's pause/resume semantics.
    step_deadline_is_runaway: bool = False

    def add_cost(self, amount: Decimal) -> None:
        """Accumulate cost from an LLM invocation."""
        self.cost_usd += amount

    def add_usage(self, result: LLMResult, *, routed_reason: str | None = None) -> None:
        """Accumulate cost and token usage from one LLM invocation.

        Prefer this over add_cost() wherever an LLMResult is in hand. Recording
        cost alone leaves every CostRecord token column empty, which is what
        made pipeline spend unattributable to tokens. A result tagged with
        ``runtime_name`` also accumulates into the step's runtime share
        (``runtime_usage``, ``runtime_cost_usd``, ``runtime_cost_source``);
        any other result's provenance goes to ``cost_source``.

        *routed_reason* is passed by a step that asked for a model other than
        ``resolved_model`` for this one call (the fix loops' task-type route).
        Its share goes to ``routed_spend`` only when ``result`` itself carries
        ``routed_task_type_model`` (set by ``sova.core.agent_dispatch`` only
        when a configured route actually survived the runtime-compatibility
        check and any role-level runtime pin), so the step's own CostRecord
        keeps only what ran on ``resolved_model``. Gating on that dispatch-side
        flag rather than on "did ``result.model`` differ from
        ``resolved_model``" avoids a false split: the reported id can differ
        from ``resolved_model`` in spelling alone (an undated alias vs. a
        dated id, a ``vertex_ai/`` prefix) even when the route was never
        actually honoured.
        """
        runtime_before = self.usage_snapshot() if result.runtime_name is not None else None
        self.cost_usd += result.cost_usd
        self.input_tokens += result.input_tokens
        self.output_tokens += result.output_tokens
        self.cache_read_tokens += result.cache_read_tokens
        if result.cache_creation_tokens is not None:
            self.cache_write_tokens += result.cache_creation_tokens
            self.cache_write_reported_calls += 1
        if result.reasoning_output_tokens is not None:
            self.reasoning_tokens += result.reasoning_output_tokens
            self.reasoning_reported_calls += 1
        if result.tokens_saved is not None:
            self.tokens_saved = (0 if self.tokens_saved is None else self.tokens_saved) + result.tokens_saved
            self.compressed_calls += 1

        if runtime_before is None:
            if routed_reason is not None and result.routed_task_type_model is not None:
                self._add_routed_spend(result, routed_reason)
                return
            self.cost_source = combine_cost_sources(self.cost_source, result.cost_source)
            return
        delta = self.usage_snapshot() - runtime_before
        self.runtime_usage = delta if self.runtime_usage is None else self.runtime_usage + delta
        self.runtime_cost_usd += result.cost_usd
        self.runtime_cost_source = combine_cost_sources(self.runtime_cost_source, result.cost_source)

    def _add_routed_spend(self, result: LLMResult, reason: str) -> None:
        """Bucket one off-``resolved_model`` provider result under the model that ran.

        Labelled with ``result.model`` (what the provider actually reported),
        falling back to ``routed_task_type_model`` (what was asked for) only
        when the provider reported nothing at all.
        """
        model_key = result.model or result.routed_task_type_model
        bucket = self.routed_spend.get(model_key)
        if bucket is None:
            bucket = RoutedSpend(model=model_key, reason=reason)
            self.routed_spend[model_key] = bucket
        bucket.usage = bucket.usage + TokenUsage(
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_read_tokens=result.cache_read_tokens,
            cache_write_tokens=result.cache_creation_tokens or 0,
            tokens_saved=result.tokens_saved,
            compressed_calls=1 if result.tokens_saved is not None else 0,
            cache_write_reported_calls=1 if result.cache_creation_tokens is not None else 0,
            reasoning_tokens=result.reasoning_output_tokens or 0,
            reasoning_reported_calls=1 if result.reasoning_output_tokens is not None else 0,
        )
        bucket.cost_usd += result.cost_usd
        bucket.cost_source = combine_cost_sources(bucket.cost_source, result.cost_source)

    def usage_snapshot(self) -> TokenUsage:
        """Accumulated token counters as an immutable snapshot."""
        return TokenUsage(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
            tokens_saved=self.tokens_saved,
            compressed_calls=self.compressed_calls,
            cache_write_reported_calls=self.cache_write_reported_calls,
            reasoning_tokens=self.reasoning_tokens,
            reasoning_reported_calls=self.reasoning_reported_calls,
        )

    @property
    def is_budget_exceeded(self) -> bool:
        """Check if the accumulated cost exceeds the configured budget."""
        return self.cost_usd > self.config.agent.max_budget

    @property
    def budget_remaining_fraction(self) -> float:
        """Remaining budget as a fraction of max_budget, clamped to [0.0, 1.0].

        Used for graceful degradation (skip optional steps, stop retrying,
        skip hooks) as the budget runs low, rather than the binary
        is_budget_exceeded cutoff. Falls back to 1.0 (no degradation) if
        max_budget is not meaningfully set.
        """
        max_budget = self.config.agent.max_budget
        if not max_budget:
            return 1.0
        fraction = Decimal("1") - (self.cost_usd / max_budget)
        return float(max(Decimal("0"), min(Decimal("1"), fraction)))

    @property
    def _wall_clock_remaining_fraction(self) -> float:
        """Remaining wall-clock budget as a fraction, scaled by task complexity.

        Falls back to 1.0 (no constraint) when the guard is disabled (limit 0)
        or when no run has started (``run_started_at`` unset, e.g. outside
        WorkflowEngine). Uses the same complexity multiplier as the wall-clock
        runaway guard (WorkflowEngine._check_runaway_guard) so a legitimate
        COMPLEX/EPIC run does not read as more resource-constrained than a run
        sized for it actually is.
        """
        from sova.llm.complexity import complexity_multiplier

        max_seconds = self.config.runaway.max_run_wall_clock_seconds
        if not max_seconds or not self.run_started_at:
            return 1.0

        import time

        scaled_max = max_seconds * complexity_multiplier(self.complexity)
        elapsed = time.monotonic() - self.run_started_at
        fraction = 1.0 - (elapsed / scaled_max)
        return max(0.0, min(1.0, fraction))

    @property
    def _call_count_remaining_fraction(self) -> float:
        """Remaining LLM-call budget as a fraction of ``runaway.max_llm_calls``.

        Falls back to 1.0 (no constraint) when the guard is disabled (limit 0).
        """
        max_calls = self.config.runaway.max_llm_calls
        if not max_calls:
            return 1.0

        from sova.llm.client import get_call_count

        fraction = 1.0 - (get_call_count() / max_calls)
        return max(0.0, min(1.0, fraction))

    @property
    def resource_remaining_fraction(self) -> float:
        """Composite remaining-resource signal: budget, wall clock, and LLM call count.

        The minimum (most-constrained) of the three, so graceful-degradation
        call sites (skip optional steps, stop retrying, skip hooks) react to
        whichever resource is closest to exhausted, not just dollar spend.
        Backstops the dollar-based budget_remaining_fraction, which reads as
        1.0 forever against a provider that always reports cost_usd=0
        (docs/model-selection-risk-assessment.md, R6).
        """
        return min(
            self.budget_remaining_fraction,
            self._wall_clock_remaining_fraction,
            self._call_count_remaining_fraction,
        )

    @property
    def step_time_remaining(self) -> float | None:
        """Seconds left before the current step's own hard timeout fires.

        None when no WorkflowEngine is driving this context (step_deadline_seconds
        unset), matching run_started_at's "0.0 means not set" convention. A
        caller should then treat the step deadline as unconstrained.
        Lets an inner fix loop (e.g. DevelopStep._check_loop_budget) stop
        itself cleanly before WorkflowEngine._run_step_with_timeout's outer
        asyncio.timeout kills the step mid-cycle and reports step_hard_timeout
        instead of the loop's own diagnostic summary.
        """
        if self.step_deadline_seconds is None or not self.step_started_at:
            return None

        import time

        elapsed = time.monotonic() - self.step_started_at
        return self.step_deadline_seconds - elapsed

    @property
    def working_dir(self) -> Path:
        """The directory where steps should execute (worktree or project root)."""
        return self.worktree_dir if self.worktree_dir else self.project_dir

    @property
    def display_label(self) -> str:
        """Human-readable label for this run (issue number, run_label, or run ID)."""
        if self.issue_number:
            return f"#{self.issue_number}"
        if self.run_label:
            return self.run_label
        if self.task_run_id:
            return f"run-{self.task_run_id}"
        return "issue-less"

    @property
    def has_issue(self) -> bool:
        """Whether this run is associated with a specific issue."""
        return bool(self.issue_number)

    @property
    def notification_group(self) -> str:
        """Notification group key for macOS notification grouping."""
        return f"sova-{self.issue_number or self.run_label or 'run'}"

    @property
    def repo(self) -> str:
        return self.config.github_repo

    @property
    def base_branch(self) -> str:
        return self.config.base_branch

    def routing_task_type(self, task_type: str) -> str | None:
        """Return *task_type* for an ``llm.routing`` lookup, or None to skip routing.

        A configured route outranks the model a step passes, so once a fallback
        is in flight the route would pin the step straight back to the model
        that just failed on every retry. Suppressing it there lets the engine's
        fallback winner survive. An untagged step (empty *task_type*) never
        routes.
        """
        if self.fallback_model_index > 0:
            return None
        return task_type or None

    def get_cli_fallback_model(self) -> str | None:
        """Get the next fallback model to pass to the Claude CLI via --fallback-model flag.

        Returns the model at fallback_models[fallback_model_index], or None if exhausted.
        This enables intra-session resilience: if the primary model hits billing/rate-limit,
        Claude can fall back internally before the step-level retry kicks in.
        """
        fallback_chain = self.config.agent.fallback_models
        if self.fallback_model_index < len(fallback_chain):
            return fallback_chain[self.fallback_model_index]
        return None
