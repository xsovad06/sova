"""Base adapter protocol for task source integrations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum


class TaskState(StrEnum):
    """Issue lifecycle states managed by agents on the tracker."""

    BACKLOG = "backlog"
    TRIAGED = "triaged"
    RESEARCHED = "researched"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"
    NEEDS_SPEC = "needs_spec"
    HUMAN_ONLY = "human_only"


@dataclass
class Task:
    """A task retrieved from a tracker."""

    id: str
    title: str
    body: str = ""
    state: TaskState = TaskState.BACKLOG
    labels: list[str] = field(default_factory=list)
    assignees: list[str] = field(default_factory=list)
    url: str = ""
    milestone: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class TaskFilters:
    """Filters for listing tasks."""

    milestone: str = ""
    labels: list[str] = field(default_factory=list)
    state: str = "open"


class TaskAdapter(ABC):
    """Abstract base for task source adapters.

    Each adapter connects to a tracker (currently GitHub only) and provides
    both read access and state management. Agents own the issue lifecycle
    on the tracker -- every state transition is visible to humans.
    """

    def __init__(self, repo: str, github_user: str = "") -> None:
        self.repo = repo
        self.github_user = github_user

    @abstractmethod
    async def list_tasks(self, filters: TaskFilters | None = None) -> list[Task]:
        """List open tasks, optionally filtered."""

    @abstractmethod
    async def get_task(self, task_id: str) -> Task:
        """Get full task details by ID."""

    @abstractmethod
    async def transition_state(self, task_id: str, new_state: TaskState) -> None:
        """Move a task to a new lifecycle state on the tracker."""

    @abstractmethod
    async def assign(self, task_id: str, agent_role: str) -> None:
        """Assign the task to an agent role."""

    @abstractmethod
    async def add_label(self, task_id: str, label: str) -> None:
        """Add a label to the task."""

    @abstractmethod
    async def remove_label(self, task_id: str, label: str) -> None:
        """Remove a label from the task."""

    @abstractmethod
    async def post_comment(self, task_id: str, body: str) -> None:
        """Post a comment on the task."""

    @abstractmethod
    async def post_pr_comment(self, pr_number: int, body: str) -> None:
        """Post a comment on a pull request."""

    @abstractmethod
    async def post_pr_review(
        self,
        pr_number: int,
        body: str,
        event: str,
        comments: list[dict],
    ) -> None:
        """Post a review on a pull request with optional inline comments."""

    @abstractmethod
    async def edit_body(self, task_id: str, body: str) -> None:
        """Update the issue body/description on the tracker."""

    @abstractmethod
    async def get_state(self, task_id: str) -> TaskState:
        """Read the current lifecycle state from the tracker."""

    @abstractmethod
    async def link_pr(self, task_id: str, pr_url: str) -> None:
        """Associate a pull request with the task."""
