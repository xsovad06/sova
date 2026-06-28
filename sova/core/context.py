"""Execution context for a task run.

Replaces the bash agent's global variables with a typed, validated context
object that is threaded through every step of the workflow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from sova.adapters.base import Task, TaskAdapter
from sova.config.models import ProjectConfig
from sova.core.planning import PlanResult


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
    session_id: str | None = None
    cost_usd: Decimal = Decimal("0")
    force: bool = False
    task_run_id: int | None = None
    plan_result: PlanResult | None = None

    # Resume checkpoint (populated when --resume is used)
    resume_run_id: int | None = None
    completed_steps: frozenset[str] = field(default_factory=frozenset)

    # Accumulated during the run
    files_changed: list[str] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)

    def add_cost(self, amount: Decimal) -> None:
        """Accumulate cost from an LLM invocation."""
        self.cost_usd += amount

    @property
    def is_budget_exceeded(self) -> bool:
        """Check if the accumulated cost exceeds the configured budget."""
        return self.cost_usd > self.config.agent.max_budget

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
