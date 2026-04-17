"""Task source adapter plugins for SOVA."""

from __future__ import annotations

from sova.adapters.base import Task, TaskAdapter, TaskFilters, TaskState


def create_adapter(adapter_type: str, repo: str) -> TaskAdapter:
    """Create a task adapter by type name.

    Args:
        adapter_type: One of "github", "jira", "linear", "manual".
        repo: Repository identifier (e.g., "owner/repo").

    Raises:
        ValueError: If the adapter type is unknown.
    """
    if adapter_type == "github":
        from sova.adapters.github import GitHubAdapter

        return GitHubAdapter(repo=repo)

    raise ValueError(f"Unknown adapter type: {adapter_type!r}. Available: github")


__all__ = [
    "Task",
    "TaskAdapter",
    "TaskFilters",
    "TaskState",
    "create_adapter",
]
