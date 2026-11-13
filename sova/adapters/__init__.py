"""Task source adapter plugins for SOVA."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sova.adapters.base import AdapterError, Milestone, PRReview, Task, TaskAdapter, TaskFilters, TaskState
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.config.models import ProjectConfig

log = get_logger(component="adapters")


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
            raise ValueError(
                "Jira adapter requires jira_base_url. "
                "Set the SOVA_TASK_JIRA_BASE_URL env var or configure it via the dashboard settings page."
            )
        if not ts.jira_email:
            raise ValueError(
                "Jira adapter requires jira_email. "
                "Set the SOVA_TASK_JIRA_EMAIL env var or configure it via the dashboard settings page."
            )
        if not ts.jira_api_token:
            raise ValueError(
                "Jira adapter requires jira_api_token. "
                "Set the SOVA_TASK_JIRA_API_TOKEN env var or configure it via the dashboard settings page."
            )
        if not ts.jira_project_key:
            raise ValueError(
                "Jira adapter requires jira_project_key. "
                "Set the SOVA_TASK_JIRA_PROJECT_KEY env var or configure it via the dashboard settings page."
            )
        return JiraAdapter(
            base_url=ts.jira_base_url,
            email=ts.jira_email,
            api_token=ts.jira_api_token,
            project_key=ts.jira_project_key,
            component=ts.jira_component,
            jql_filter=ts.jira_jql_filter,
            state_transitions=ts.jira_state_transitions or None,
            status_mapping=ts.jira_status_mapping or None,
        )

    raise ValueError(f"Unknown adapter type: {adapter_type!r}. Available: github, jira")


async def close_adapter(adapter: TaskAdapter) -> None:
    """Release what an adapter built for one call holds (Jira's HTTP client); a failure to close is only logged.

    Safe on every exit path: an adapter with nothing to release (GitHub) is a no-op, and a close error never
    replaces the result or the exception the caller is already handling.
    """
    close = getattr(adapter, "close", None)
    if close is None:
        return
    try:
        await close()
    except Exception:  # noqa: BLE001 (cleanup must not replace the result or the error being raised)
        log.warning("adapters.adapter_close_failed", exc_info=True)


__all__ = [
    "AdapterError",
    "Milestone",
    "PRReview",
    "Task",
    "TaskAdapter",
    "TaskFilters",
    "TaskState",
    "close_adapter",
    "create_adapter",
]
