"""Task source adapter plugins for SOVA."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sova.adapters.base import Task, TaskAdapter, TaskFilters, TaskState

if TYPE_CHECKING:
    from sova.config.models import ProjectConfig


def create_adapter(config: ProjectConfig) -> TaskAdapter:
    """Create a task adapter from project configuration.

    The adapter type is determined by ``config.task_source.type``.

    Raises:
        ValueError: If the adapter type is unknown or required config is missing.
    """
    adapter_type = config.task_source.type

    if adapter_type == "github":
        from sova.adapters.github import GitHubAdapter

        ts = config.task_source
        return GitHubAdapter(
            repo=config.github_repo,
            github_user=config.github_user,
            project_number=ts.github_project_number,
        )

    if adapter_type == "jira":
        from sova.adapters.jira import JiraAdapter

        ts = config.task_source
        if not ts.jira_base_url:
            raise ValueError("Jira adapter requires task_source.jira_base_url in sova.toml")
        if not ts.jira_email:
            raise ValueError("Jira adapter requires task_source.jira_email in sova.toml")
        if not ts.jira_api_token:
            raise ValueError(
                "Jira adapter requires jira_api_token. "
                "Set SOVA_TASK_JIRA_API_TOKEN env var or task_source.jira_api_token in sova.toml"
            )
        if not ts.jira_project_key:
            raise ValueError("Jira adapter requires task_source.jira_project_key in sova.toml")
        return JiraAdapter(
            base_url=ts.jira_base_url,
            email=ts.jira_email,
            api_token=ts.jira_api_token,
            project_key=ts.jira_project_key,
            state_transitions=ts.jira_state_transitions or None,
        )

    raise ValueError(f"Unknown adapter type: {adapter_type!r}. Available: github, jira")


__all__ = [
    "Task",
    "TaskAdapter",
    "TaskFilters",
    "TaskState",
    "create_adapter",
]
