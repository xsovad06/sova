"""GitHub Issues adapter for SOVA."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sova.adapters.base import Task, TaskAdapter, TaskFilters, TaskState
from sova.utils.gh import resolve_gh_env
from sova.utils.logging import get_logger
from sova.utils.shell import ShellResult, run

log = get_logger(component="adapter.github")

_COL_TODO = "to do"  # with space; "todo" (no space) is a separate board column name
_COL_BACKLOG = "backlog"
_COL_READY = "ready"

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

# Maps TaskState to candidate board column names (case-insensitive match).
_STATE_TO_BOARD_NAMES: dict[TaskState, list[str]] = {
    TaskState.BACKLOG: [_COL_BACKLOG, "todo", _COL_TODO],
    TaskState.TRIAGED: ["triaged", _COL_READY, "todo", _COL_TODO],
    TaskState.RESEARCHED: ["researched", _COL_READY, "todo", _COL_TODO],
    TaskState.IN_PROGRESS: ["in progress", "doing"],
    TaskState.IN_REVIEW: ["in review", "review", "verification"],
    TaskState.DONE: ["done", "completed"],
    TaskState.NEEDS_SPEC: ["todo", _COL_TODO, _COL_BACKLOG],
    TaskState.HUMAN_ONLY: ["todo", _COL_TODO, _COL_BACKLOG],
}


@dataclass
class _ProjectBoardMeta:
    """Cached metadata for a GitHub Projects V2 board."""

    project_id: str
    status_field_id: str
    options: dict[str, str] = field(default_factory=dict)


class GitHubAdapter(TaskAdapter):
    """Task adapter backed by GitHub Issues and the ``gh`` CLI."""

    def __init__(self, repo: str, github_user: str = "", project_number: int = 0) -> None:
        super().__init__(repo=repo, github_user=github_user)
        self.project_number = project_number
        self._board_meta: _ProjectBoardMeta | None = None

    async def _gh(self, *args: str, **kwargs: object) -> ShellResult:
        """Run a ``gh`` CLI command with per-project auth."""
        env = await resolve_gh_env(self.github_user)
        return await run("gh", *args, env=env, **kwargs)

    async def list_tasks(self, filters: TaskFilters | None = None) -> list[Task]:
        filters = filters or TaskFilters()

        args = [
            "issue",
            "list",
            "--repo",
            self.repo,
            "--state",
            filters.state,
            "--json",
            "number,title,body,state,labels,assignees,milestone,url,createdAt",
            "--limit",
            "50",
        ]

        if filters.milestone:
            args.extend(["--milestone", filters.milestone])
        if filters.labels:
            args.extend(["--label", ",".join(filters.labels)])

        result = await self._gh(*args)
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
        result = await self._gh(
            "issue",
            "view",
            task_id,
            "--repo",
            self.repo,
            "--json",
            "number,title,body,state,labels,assignees,milestone,url,createdAt",
        )
        if not result.success:
            raise RuntimeError(f"Failed to fetch issue #{task_id}: {result.stderr[:200]}")

        issue = json.loads(result.stdout)
        return _parse_issue(issue)

    async def transition_state(self, task_id: str, new_state: TaskState) -> None:
        if new_state == TaskState.DONE:
            await self._gh("issue", "close", task_id, "--repo", self.repo)
            await self._move_on_board(task_id, new_state)
            return

        label = _STATE_LABELS.get(new_state)
        if label:
            await self._clear_state_labels(task_id)
            await self._gh(
                "issue",
                "edit",
                task_id,
                "--repo",
                self.repo,
                "--add-label",
                label,
            )

        await self._move_on_board(task_id, new_state)

    async def assign(self, task_id: str, agent_role: str) -> None:
        # GitHub assignees are users, not roles. We use the configured
        # github_user from ProjectConfig. For now, add the role as a label.
        await self._gh(
            "issue",
            "edit",
            task_id,
            "--repo",
            self.repo,
            "--add-label",
            f"role:{agent_role}",
        )

    async def add_label(self, task_id: str, label: str) -> None:
        await self._gh(
            "issue",
            "edit",
            task_id,
            "--repo",
            self.repo,
            "--add-label",
            label,
        )

    async def remove_label(self, task_id: str, label: str) -> None:
        await self._gh(
            "issue",
            "edit",
            task_id,
            "--repo",
            self.repo,
            "--remove-label",
            label,
        )

    async def post_comment(self, task_id: str, body: str) -> None:
        await self._gh(
            "issue",
            "comment",
            task_id,
            "--repo",
            self.repo,
            "--body",
            body,
        )

    async def post_pr_comment(self, pr_number: int, body: str) -> None:
        await self._gh(
            "pr",
            "comment",
            str(pr_number),
            "--repo",
            self.repo,
            "--body",
            body,
        )

    async def post_pr_review(
        self,
        pr_number: int,
        body: str,
        event: str,
        comments: list[dict],
    ) -> None:
        payload = json.dumps({"body": body, "event": event, "comments": comments})
        result = await self._gh(
            "api",
            f"repos/{self.repo}/pulls/{pr_number}/reviews",
            "--method",
            "POST",
            "--input",
            "-",
            stdin=payload,
        )
        if not result.success and comments:
            log.warning(
                "post_pr_review.inline_failed_retrying_body_only",
                pr=pr_number,
                comment_count=len(comments),
                stderr=result.stderr[:200],
            )
            body_payload = json.dumps({"body": body, "event": event, "comments": []})
            result = await self._gh(
                "api",
                f"repos/{self.repo}/pulls/{pr_number}/reviews",
                "--method",
                "POST",
                "--input",
                "-",
                stdin=body_payload,
            )
        if not result.success:
            raise RuntimeError(f"Failed to post PR review on #{pr_number}: {result.stderr[:300]}")

    async def edit_body(self, task_id: str, body: str) -> None:
        await self._gh(
            "issue",
            "edit",
            task_id,
            "--repo",
            self.repo,
            "--body",
            body,
        )

    async def get_state(self, task_id: str) -> TaskState:
        result = await self._gh(
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
        await self._gh(
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
        result = await self._gh(
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
                await self._gh(
                    "issue",
                    "edit",
                    task_id,
                    "--repo",
                    self.repo,
                    "--remove-label",
                    label,
                )

    # -- Project board integration -----------------------------------------------

    async def _move_on_board(self, task_id: str, new_state: TaskState) -> None:
        """Move an issue to the matching column on the GitHub Projects V2 board."""
        if not self.project_number:
            return

        meta = await self._get_board_meta()
        if meta is None:
            return

        option_id = self._resolve_board_option(new_state, meta)
        if option_id is None:
            log.debug("board.no_matching_option", state=str(new_state))
            return

        item_id = await self._get_issue_item_id(task_id, meta.project_id)
        if item_id is None:
            log.debug("board.issue_not_on_board", issue=task_id)
            return

        mutation = """
        mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
          updateProjectV2ItemFieldValue(input: {
            projectId: $projectId
            itemId: $itemId
            fieldId: $fieldId
            value: { singleSelectOptionId: $optionId }
          }) { projectV2Item { id } }
        }
        """
        result = await self._gh(
            "api",
            "graphql",
            "-f",
            f"query={mutation}",
            "-f",
            f"projectId={meta.project_id}",
            "-f",
            f"itemId={item_id}",
            "-f",
            f"fieldId={meta.status_field_id}",
            "-f",
            f"optionId={option_id}",
        )
        if result.success:
            log.info("board.moved", issue=task_id, state=str(new_state))
        else:
            log.warning("board.move_failed", issue=task_id, stderr=result.stderr[:200])

    async def _get_board_meta(self) -> _ProjectBoardMeta | None:
        """Fetch and cache project ID, status field ID, and option IDs."""
        if self._board_meta is not None:
            return self._board_meta

        owner = self.repo.split("/")[0] if "/" in self.repo else self.github_user
        if not owner:
            return None

        query = """
        query($owner: String!, $number: Int!) {
          user(login: $owner) {
            projectV2(number: $number) {
              id
              field(name: "Status") {
                ... on ProjectV2SingleSelectField {
                  id
                  options { id name }
                }
              }
            }
          }
        }
        """
        result = await self._gh(
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-f",
            f"owner={owner}",
            "-F",
            f"number={self.project_number}",
        )
        if not result.success:
            log.warning("board.meta_fetch_failed", stderr=result.stderr[:200])
            return None

        try:
            data = json.loads(result.stdout)
            project = data["data"]["user"]["projectV2"]
            field_data = project["field"]
            options = {opt["name"].lower(): opt["id"] for opt in field_data["options"]}
            self._board_meta = _ProjectBoardMeta(
                project_id=project["id"],
                status_field_id=field_data["id"],
                options=options,
            )
            return self._board_meta
        except (json.JSONDecodeError, KeyError, TypeError):
            log.warning("board.meta_parse_failed", exc_info=True)
            return None

    async def _get_issue_item_id(self, task_id: str, project_id: str) -> str | None:
        """Find the project item ID for an issue on the board."""
        owner, name = self.repo.split("/", 1) if "/" in self.repo else (self.github_user, self.repo)
        query = """
        query($owner: String!, $name: String!, $number: Int!) {
          repository(owner: $owner, name: $name) {
            issue(number: $number) {
              projectItems(first: 10) {
                nodes { id project { id } }
              }
            }
          }
        }
        """
        result = await self._gh(
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-F",
            f"number={task_id}",
        )
        if not result.success:
            return None

        try:
            data = json.loads(result.stdout)
            items = data["data"]["repository"]["issue"]["projectItems"]["nodes"]
            for item in items:
                if item["project"]["id"] == project_id:
                    return item["id"]
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
        return None

    @staticmethod
    def _resolve_board_option(state: TaskState, meta: _ProjectBoardMeta) -> str | None:
        """Match a TaskState to a board status option ID."""
        candidates = _STATE_TO_BOARD_NAMES.get(state, [])
        for name in candidates:
            option_id = meta.options.get(name)
            if option_id:
                return option_id
        return None


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

    metadata: dict = {}
    if data.get("createdAt"):
        metadata["created_at"] = data["createdAt"]

    return Task(
        id=str(data["number"]),
        title=data.get("title", ""),
        body=data.get("body", "") or "",
        state=state,
        labels=labels,
        assignees=assignees,
        url=data.get("url", ""),
        milestone=milestone,
        metadata=metadata,
    )
