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

    def __sub__(self, other: TokenUsage) -> TokenUsage:
        compressed_calls = self.compressed_calls - other.compressed_calls
        return TokenUsage(
            input_tokens=self.input_tokens - other.input_tokens,
            output_tokens=self.output_tokens - other.output_tokens,
            cache_read_tokens=self.cache_read_tokens - other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens - other.cache_write_tokens,
            tokens_saved=_delta_saved(self.tokens_saved, other.tokens_saved, compressed_calls),
            compressed_calls=compressed_calls,
        )


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
    force: bool = False
    budget_override: bool = False
    task_run_id: int | None = None
    plan_result: PlanResult | None = None

    # State validation (set by roles that restrict input states)
    allowed_input_states: frozenset[TaskState] | None = None

    # Resume checkpoint (populated when --resume is used)
    resume_run_id: int | None = None
    completed_steps: frozenset[str] = field(default_factory=frozenset)

    # Output writer (set by WorkflowEngine, used by steps for heartbeats)
    output_writer: OutputWriter | None = None

    # Set by CommitStep when working tree is clean but commits exist ahead of base.
    # MonitorCIStep uses this to check existing CI status instead of polling.
    no_new_commits: bool = False

    # Accumulated during the run
    files_changed: list[str] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)
    addressed_external_findings: list[dict] = field(default_factory=list)

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

    # Model fallback chain (index into config.agent.fallback_models)
    fallback_model_index: int = 0

    def add_cost(self, amount: Decimal) -> None:
        """Accumulate cost from an LLM invocation."""
        self.cost_usd += amount

    def add_usage(self, result: LLMResult) -> None:
        """Accumulate cost and token usage from one LLM invocation.

        Prefer this over add_cost() wherever an LLMResult is in hand. Recording
        cost alone leaves every CostRecord token column empty, which is what
        made pipeline spend unattributable to tokens.
        """
        self.cost_usd += result.cost_usd
        self.input_tokens += result.input_tokens
        self.output_tokens += result.output_tokens
        self.cache_read_tokens += result.cache_read_tokens
        self.cache_write_tokens += result.cache_creation_tokens
        if result.tokens_saved is not None:
            self.tokens_saved = (0 if self.tokens_saved is None else self.tokens_saved) + result.tokens_saved
            self.compressed_calls += 1

    def usage_snapshot(self) -> TokenUsage:
        """Accumulated token counters as an immutable snapshot."""
        return TokenUsage(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
            tokens_saved=self.tokens_saved,
            compressed_calls=self.compressed_calls,
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
