"""GitHub Issues adapter for SOVA."""

from __future__ import annotations

import json

from sova.adapters.base import Task, TaskAdapter, TaskFilters, TaskState
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="adapter.github")

# Maps TaskState to the GitHub label used to represent it.
_STATE_LABELS: dict[TaskState, str] = {
    TaskState.TRIAGED: "agent:triaged",
    TaskState.RESEARCHED: "agent:researched",
    TaskState.IN_PROGRESS: "agent:in-progress",
    TaskState.IN_REVIEW: "agent:in-review",
    TaskState.NEEDS_SPEC: "agent:needs-spec",
    TaskState.HUMAN_ONLY: "agent:human-only",
}

# Reverse map: label -> state (for reading state from labels).
_LABEL_TO_STATE: dict[str, TaskState] = {v: k for k, v in _STATE_LABELS.items()}


class GitHubAdapter(TaskAdapter):
    """Task adapter backed by GitHub Issues and the ``gh`` CLI."""

    async def list_tasks(self, filters: TaskFilters | None = None) -> list[Task]:
        filters = filters or TaskFilters()

        args = [
            "gh",
            "issue",
            "list",
            "--repo",
            self.repo,
            "--state",
            filters.state,
            "--json",
            "number,title,body,state,labels,assignees,milestone,url",
            "--limit",
            "50",
        ]

        if filters.milestone:
            args.extend(["--milestone", filters.milestone])
        if filters.labels:
            args.extend(["--label", ",".join(filters.labels)])

        result = await run(*args)
        if not result.success:
            log.warning("list_tasks.failed", stderr=result.stderr[:200])
            return []

        try:
            issues = json.loads(result.stdout)
        except json.JSONDecodeError:
            log.warning("list_tasks.bad_json", stdout=result.stdout[:200])
            return []

        return [_parse_issue(issue) for issue in issues]

    async def get_task(self, task_id: str) -> Task:
        result = await run(
            "gh",
            "issue",
            "view",
            task_id,
            "--repo",
            self.repo,
            "--json",
            "number,title,body,state,labels,assignees,milestone,url",
        )
        if not result.success:
            raise RuntimeError(f"Failed to fetch issue #{task_id}: {result.stderr[:200]}")

        issue = json.loads(result.stdout)
        return _parse_issue(issue)

    async def transition_state(self, task_id: str, new_state: TaskState) -> None:
        if new_state == TaskState.DONE:
            await run("gh", "issue", "close", task_id, "--repo", self.repo)
            return

        label = _STATE_LABELS.get(new_state)
        if label:
            # Remove any existing agent state labels first, then add the new one.
            await self._clear_state_labels(task_id)
            await run(
                "gh",
                "issue",
                "edit",
                task_id,
                "--repo",
                self.repo,
                "--add-label",
                label,
            )

    async def assign(self, task_id: str, agent_role: str) -> None:
        # GitHub assignees are users, not roles. We use the configured
        # github_user from ProjectConfig. For now, add the role as a label.
        await run(
            "gh",
            "issue",
            "edit",
            task_id,
            "--repo",
            self.repo,
            "--add-label",
            f"role:{agent_role}",
        )

    async def add_label(self, task_id: str, label: str) -> None:
        await run(
            "gh",
            "issue",
            "edit",
            task_id,
            "--repo",
            self.repo,
            "--add-label",
            label,
        )

    async def remove_label(self, task_id: str, label: str) -> None:
        await run(
            "gh",
            "issue",
            "edit",
            task_id,
            "--repo",
            self.repo,
            "--remove-label",
            label,
        )

    async def post_comment(self, task_id: str, body: str) -> None:
        await run(
            "gh",
            "issue",
            "comment",
            task_id,
            "--repo",
            self.repo,
            "--body",
            body,
        )

    async def edit_body(self, task_id: str, body: str) -> None:
        await run(
            "gh",
            "issue",
            "edit",
            task_id,
            "--repo",
            self.repo,
            "--body",
            body,
        )

    async def get_state(self, task_id: str) -> TaskState:
        result = await run(
            "gh",
            "issue",
            "view",
            task_id,
            "--repo",
            self.repo,
            "--json",
            "state,labels",
        )
        if not result.success:
            raise RuntimeError(f"Failed to get state for issue #{task_id}: {result.stderr[:200]}")

        data = json.loads(result.stdout)

        if data.get("state") == "CLOSED":
            return TaskState.DONE

        labels = [lbl["name"] for lbl in data.get("labels", [])]
        for label in labels:
            if label in _LABEL_TO_STATE:
                return _LABEL_TO_STATE[label]

        return TaskState.BACKLOG

    async def link_pr(self, task_id: str, pr_url: str) -> None:
        # GitHub auto-links via "Closes #N" in PR body. Post a comment
        # as a secondary reference for visibility.
        await run(
            "gh",
            "issue",
            "comment",
            task_id,
            "--repo",
            self.repo,
            "--body",
            f"Linked PR: {pr_url}",
        )

    async def _clear_state_labels(self, task_id: str) -> None:
        """Remove all agent state labels from an issue."""
        result = await run(
            "gh",
            "issue",
            "view",
            task_id,
            "--repo",
            self.repo,
            "--json",
            "labels",
        )
        if not result.success:
            return

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return
        current_labels = [lbl["name"] for lbl in data.get("labels", [])]

        for label in current_labels:
            if label in _LABEL_TO_STATE:
                await run(
                    "gh",
                    "issue",
                    "edit",
                    task_id,
                    "--repo",
                    self.repo,
                    "--remove-label",
                    label,
                )


def _parse_issue(data: dict) -> Task:
    """Parse a GitHub issue JSON object into a Task."""
    labels = [lbl["name"] for lbl in data.get("labels", [])]
    assignees = [a["login"] for a in data.get("assignees", [])]

    milestone_data = data.get("milestone")
    milestone = milestone_data["title"] if milestone_data else ""

    # Infer state from labels
    state = TaskState.BACKLOG
    for label in labels:
        if label in _LABEL_TO_STATE:
            state = _LABEL_TO_STATE[label]
            break

    return Task(
        id=str(data["number"]),
        title=data.get("title", ""),
        body=data.get("body", "") or "",
        state=state,
        labels=labels,
        assignees=assignees,
        url=data.get("url", ""),
        milestone=milestone,
    )
