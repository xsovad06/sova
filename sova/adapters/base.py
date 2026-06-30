"""Base adapter protocol for task source integrations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum

from sova.llm.egress import EgressMode, filter_egress
from sova.utils.logging import get_logger

_log = get_logger(component="adapter.base")

_EGRESS_BLOCKED = "egress.blocked"


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
    # Rich metadata (optional, populated by adapters that support them)
    issue_type: str = ""
    story_points: float | None = None
    sprint: str = ""
    components: list[str] = field(default_factory=list)
    fix_versions: list[str] = field(default_factory=list)


@dataclass
class TaskFilters:
    """Filters for listing tasks."""

    milestone: str = ""
    labels: list[str] = field(default_factory=list)
    state: str = "open"


@dataclass
class PRReview:
    """A single review on a pull request."""

    reviewer: str
    state: str  # APPROVED | CHANGES_REQUESTED | COMMENTED | DISMISSED
    body: str
    submitted_at: str  # ISO 8601 UTC timestamp (YYYY-MM-DDTHH:MM:SSZ); lexicographic comparison assumed
    is_bot: bool


@dataclass
class Milestone:
    """A milestone (GitHub) or fix version (Jira) on the tracker."""

    title: str
    state: str = "open"  # open | closed
    description: str = ""


_cached_egress_mode: EgressMode | None = None


def _get_egress_mode() -> EgressMode:
    """Load egress mode from config (cached after first call)."""
    global _cached_egress_mode
    if _cached_egress_mode is not None:
        return _cached_egress_mode
    try:
        from sova.config.loader import load_config

        _cached_egress_mode = load_config().egress.mode
    except Exception:
        _log.warning("egress.config_load_failed", exc_info=True)
        _cached_egress_mode = "warn"
    return _cached_egress_mode


def _reset_egress_cache() -> None:
    """Clear the cached egress mode (for testing and config reload)."""
    global _cached_egress_mode
    _cached_egress_mode = None


class TaskAdapter(ABC):
    """Abstract base for task source adapters.

    Each adapter connects to a tracker (GitHub, Jira Cloud) and provides
    both read access and state management. Agents own the issue lifecycle
    on the tracker -- every state transition is visible to humans.

    Outbound text methods use the Template Method pattern: the public method
    runs the egress filter, then delegates to the ``_do_*`` abstract method.
    Subclasses implement ``_do_*`` instead of the public method.
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

    # -- Egress-filtered methods (Template Method pattern) -------------------

    async def post_comment(self, task_id: str, body: str) -> None:
        """Post a comment on the task (egress-filtered)."""
        filtered = filter_egress(body, mode=_get_egress_mode(), destination="post_comment")
        if filtered is None:
            _log.warning(_EGRESS_BLOCKED, method="post_comment", task_id=task_id)
            return
        await self._do_post_comment(task_id, filtered)

    @abstractmethod
    async def _do_post_comment(self, task_id: str, body: str) -> None:
        """Post a comment on the task (implementation)."""

    async def post_pr_comment(self, pr_number: int, body: str) -> None:
        """Post a comment on a pull request (egress-filtered)."""
        filtered = filter_egress(body, mode=_get_egress_mode(), destination="post_pr_comment")
        if filtered is None:
            _log.warning(_EGRESS_BLOCKED, method="post_pr_comment", pr=pr_number)
            return
        await self._do_post_pr_comment(pr_number, filtered)

    @abstractmethod
    async def _do_post_pr_comment(self, pr_number: int, body: str) -> None:
        """Post a comment on a pull request (implementation)."""

    async def post_pr_review(
        self,
        pr_number: int,
        body: str,
        event: str,
        comments: list[dict],
    ) -> None:
        """Post a review on a pull request with optional inline comments (egress-filtered)."""
        mode = _get_egress_mode()
        filtered_body = filter_egress(body, mode=mode, destination="post_pr_review.body")
        if filtered_body is None:
            _log.warning(_EGRESS_BLOCKED, method="post_pr_review", pr=pr_number)
            return

        filtered_comments = []
        for comment in comments:
            comment_body = comment.get("body", "")
            filtered_comment_body = filter_egress(comment_body, mode=mode, destination="post_pr_review.comment")
            if filtered_comment_body is None:
                _log.warning(_EGRESS_BLOCKED, method="post_pr_review.comment", pr=pr_number)
                return
            filtered_comments.append({**comment, "body": filtered_comment_body})

        await self._do_post_pr_review(pr_number, filtered_body, event, filtered_comments)

    @abstractmethod
    async def _do_post_pr_review(
        self,
        pr_number: int,
        body: str,
        event: str,
        comments: list[dict],
    ) -> None:
        """Post a review on a pull request (implementation)."""

    async def edit_body(self, task_id: str, body: str) -> None:
        """Update the issue body/description on the tracker (egress-filtered)."""
        filtered = filter_egress(body, mode=_get_egress_mode(), destination="edit_body")
        if filtered is None:
            _log.warning(_EGRESS_BLOCKED, method="edit_body", task_id=task_id)
            return
        await self._do_edit_body(task_id, filtered)

    @abstractmethod
    async def _do_edit_body(self, task_id: str, body: str) -> None:
        """Update the issue body/description (implementation)."""

    @abstractmethod
    async def get_state(self, task_id: str) -> TaskState:
        """Read the current lifecycle state from the tracker."""

    @abstractmethod
    async def link_pr(self, task_id: str, pr_url: str) -> None:
        """Associate a pull request with the task."""

    @abstractmethod
    async def get_pr_reviews(self, pr_number: int) -> list[PRReview]:
        """Fetch all reviews on a pull request."""

    async def create_issue(
        self,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
        issue_type: str = "",
        parent_key: str = "",
    ) -> Task:
        """Create a new issue/task on the tracker (egress-filtered).

        Args:
            title: Issue title/summary.
            body: Issue body/description (markdown for GitHub, plain text for Jira ADF).
            labels: Labels to apply to the new issue.
            issue_type: Issue type name (e.g. "Task", "Bug", "Sub-task"). Adapter-specific.
            parent_key: Parent issue key for sub-task creation (Jira only).

        Returns:
            The created Task with populated fields.
        """
        mode = _get_egress_mode()
        filtered_title = filter_egress(title, mode=mode, destination="create_issue.title")
        if filtered_title is None:
            raise RuntimeError("Egress filter blocked issue title")
        filtered_body = filter_egress(body, mode=mode, destination="create_issue.body") if body else body
        if filtered_body is None:
            _log.warning("egress.blocked_body_using_empty", method="create_issue")
            filtered_body = ""
        return await self._do_create_issue(filtered_title, filtered_body, labels, issue_type, parent_key)

    @abstractmethod
    async def _do_create_issue(
        self,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
        issue_type: str = "",
        parent_key: str = "",
    ) -> Task:
        """Create a new issue/task on the tracker (implementation)."""

    @abstractmethod
    async def get_available_transitions(self, task_id: str) -> list[dict[str, str]]:
        """Discover valid workflow transitions for a task.

        Returns a list of transition dicts, each with keys: id, name, to_status.
        For trackers without workflow transitions (e.g. GitHub), returns an empty list.
        """

    @abstractmethod
    async def list_milestones(self, state: str = "open") -> list[Milestone]:
        """List milestones/fix versions on the tracker."""

    @abstractmethod
    async def create_milestone(self, title: str, description: str = "") -> Milestone:
        """Create a milestone/fix version on the tracker."""

    @abstractmethod
    async def set_milestone(self, task_id: str, milestone_title: str) -> None:
        """Assign a milestone/fix version to a task by title."""
