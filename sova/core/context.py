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

# Budget degradation thresholds (fraction of max_budget remaining).
# Below each threshold, the developer pipeline degrades gracefully rather
# than risking being killed mid-critical-step.
BUDGET_SKIP_OPTIONAL_THRESHOLD = 0.40
BUDGET_STOP_RETRY_THRESHOLD = 0.20
BUDGET_SKIP_HOOKS_THRESHOLD = 0.08


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
    resolved_model: str | None = None
    model_selection_reason: str | None = None

    # Model fallback chain (index into config.agent.fallback_models)
    fallback_model_index: int = 0

    def add_cost(self, amount: Decimal) -> None:
        """Accumulate cost from an LLM invocation."""
        self.cost_usd += amount

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
        fraction = 1.0 - float(self.cost_usd / max_budget)
        return max(0.0, min(1.0, fraction))

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
