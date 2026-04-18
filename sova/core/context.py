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


@dataclass
class ExecutionContext:
    """Mutable context passed through every workflow step."""

    # Required
    project_dir: Path
    config: ProjectConfig
    adapter: TaskAdapter
    issue_number: str
    role: str = "developer"

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
    def repo(self) -> str:
        return self.config.github_repo

    @property
    def base_branch(self) -> str:
        return self.config.base_branch
