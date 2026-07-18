"""GitHub Issues adapter for SOVA."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sova.adapters.base import Milestone, PRReview, Task, TaskAdapter, TaskFilters, TaskState
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

# Full set of agent labels SOVA expects to exist on any GitHub repo it manages.
# Includes state-transition labels (_STATE_LABELS) and triage outcome labels.
REQUIRED_AGENT_LABELS: frozenset[str] = frozenset(_STATE_LABELS.values()) | {
    "agent:ready",
    "agent:needs-research",
}

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
            result = await self._gh("issue", "close", task_id, "--repo", self.repo)
            if not result.success:
                raise RuntimeError(f"Failed to close issue #{task_id}: {result.stderr[:200]}")
            await self._move_on_board(task_id, new_state)
            return

        label = _STATE_LABELS.get(new_state)
        if label:
            await self._clear_state_labels(task_id)
            await self._add_label(task_id, label)

        await self._move_on_board(task_id, new_state)

    async def assign(self, task_id: str, agent_role: str) -> None:
        # GitHub assignees are users, not roles. We use the configured
        # github_user from ProjectConfig. For now, add the role as a label.
        await self._add_label(task_id, f"role:{agent_role}")

    async def add_label(self, task_id: str, label: str) -> None:
        await self._add_label(task_id, label)

    async def ensure_repo_labels(self, required: frozenset[str] | None = None) -> list[str]:
        """Verify required agent labels exist on the repo, creating any that are missing.

        Returns the names of labels that were created.
        Raises RuntimeError if labels cannot be listed or any missing label cannot be created.
        """
        to_check = required if required is not None else REQUIRED_AGENT_LABELS
        result = await self._gh("label", "list", "--repo", self.repo, "--json", "name", "--limit", "500")
        if not result.success:
            raise RuntimeError(f"Could not list repo labels: {result.stderr[:200]}")

        try:
            existing = {lbl["name"] for lbl in json.loads(result.stdout)}
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            raise RuntimeError(f"Could not parse label list: {exc}") from exc

        missing = sorted(to_check - existing)
        created: list[str] = []
        for label in missing:
            log.info("label.auto_create", label=label, repo=self.repo)
            create = await self._gh(
                "label",
                "create",
                label,
                "--repo",
                self.repo,
                "--description",
                "",
                "--color",
                "ededed",
            )
            if not create.success:
                # Treat "already exists" as success: another caller created it concurrently.
                stderr_lower = create.stderr.lower()
                if "already exists" in stderr_lower or "already been taken" in stderr_lower:
                    log.debug("label.already_exists", label=label, repo=self.repo)
                else:
                    raise RuntimeError(
                        f"Could not create label '{label}': {create.stderr[:200]}. "
                        f"Create it manually: gh label create {label!r} --repo {self.repo}"
                    )
            else:
                created.append(label)

        return created

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

    async def _do_post_comment(self, task_id: str, body: str) -> None:
        await self._gh(
            "issue",
            "comment",
            task_id,
            "--repo",
            self.repo,
            "--body",
            body,
        )

    async def _do_post_pr_comment(self, pr_number: int, body: str) -> None:
        await self._gh(
            "pr",
            "comment",
            str(pr_number),
            "--repo",
            self.repo,
            "--body",
            body,
        )

    async def _do_post_pr_review(
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

    async def _do_edit_body(self, task_id: str, body: str) -> None:
        result = await self._gh(
            "issue",
            "edit",
            task_id,
            "--repo",
            self.repo,
            "--body",
            body,
        )
        if not result.success:
            raise RuntimeError(f"Failed to edit body of issue #{task_id}: {result.stderr[:200]}")

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

    async def get_pr_reviews(self, pr_number: int) -> list[PRReview]:
        result = await self._gh(
            "api",
            f"repos/{self.repo}/pulls/{pr_number}/reviews",
            "--paginate",
        )
        if not result.success:
            log.warning("get_pr_reviews.failed", pr=pr_number, stderr=result.stderr[:200])
            return []

        try:
            reviews = json.loads(result.stdout)
        except json.JSONDecodeError:
            log.warning("get_pr_reviews.bad_json", pr=pr_number, stdout=result.stdout[:200], exc_info=True)
            return []

        if not isinstance(reviews, list):
            log.warning("get_pr_reviews.unexpected_type", pr=pr_number, type=type(reviews).__name__)
            return []

        from sova.llm.guard import sanitize_external_input

        parsed: list[PRReview] = []
        for r in reviews:
            if not isinstance(r, dict):
                continue
            reviewer = r.get("user", {}).get("login", "")
            state = r.get("state", "")
            submitted_at = r.get("submitted_at", "")
            if not reviewer or not state or not submitted_at:
                log.warning("get_pr_reviews.malformed_entry", pr=pr_number, reviewer=reviewer, state=state)
                continue
            parsed.append(
                PRReview(
                    reviewer=reviewer,
                    state=state,
                    body=sanitize_external_input(r.get("body", "") or "", source="github_pr_review"),
                    submitted_at=submitted_at,
                    is_bot=r.get("user", {}).get("type", "") == "Bot",
                )
            )
        return parsed

    async def get_comments(self, task_id: str) -> list[str]:
        result = await self._gh(
            "issue",
            "view",
            task_id,
            "--repo",
            self.repo,
            "--json",
            "comments",
        )
        if not result.success:
            log.warning("get_comments.failed", issue=task_id, stderr=result.stderr[:200])
            return []
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            log.warning("get_comments.json_decode_error", issue=task_id)
            return []
        from sova.llm.guard import sanitize_external_input

        comments = data.get("comments") or []
        return [
            sanitize_external_input(c.get("body", ""), source="github_comment")
            for c in reversed(comments)
            if c.get("body")
        ]

    async def _do_create_issue(
        self,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
        issue_type: str = "",
        parent_key: str = "",
    ) -> Task:
        args = [
            "issue",
            "create",
            "--repo",
            self.repo,
            "--title",
            title,
        ]
        if body:
            args.extend(["--body", body])
        if labels:
            args.extend(["--label", ",".join(labels)])

        result = await self._gh(*args)
        if not result.success:
            raise RuntimeError(f"Failed to create issue: {result.stderr[:200]}")

        # gh issue create outputs the issue URL; extract the number and fetch full details.
        url = result.stdout.strip()
        if not url or not url.startswith("https://github.com/"):
            raise RuntimeError(f"Failed to parse issue URL from gh output: {url!r}")
        issue_number = url.rstrip("/").split("/")[-1]
        if not issue_number.isdigit():
            raise RuntimeError(f"Failed to parse issue number from URL: {url!r}")
        return await self.get_task(issue_number)

    async def get_available_transitions(self, task_id: str) -> list[dict[str, str]]:
        # GitHub uses labels for state, not workflow transitions.
        return []

    async def list_milestones(self, state: str = "open") -> list[Milestone]:
        # GitHub API only supports state=open|closed, not "all".
        # For "all", fetch both open and closed milestones.
        if state == "all":
            open_ms = await self._fetch_milestones("open")
            closed_ms = await self._fetch_milestones("closed")
            return open_ms + closed_ms
        return await self._fetch_milestones(state)

    async def _fetch_milestones(self, state: str) -> list[Milestone]:
        result = await self._gh(
            "api",
            f"repos/{self.repo}/milestones",
            "--method",
            "GET",
            "-f",
            f"state={state}",
            "-f",
            "per_page=100",
            "--paginate",
        )
        if not result.success:
            log.warning("list_milestones.failed", stderr=result.stderr[:200])
            return []

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            log.warning("list_milestones.bad_json", stdout=result.stdout[:200])
            return []

        if not isinstance(data, list):
            return []

        return [
            Milestone(
                title=m.get("title", ""),
                state=m.get("state", "open"),
                description=m.get("description", "") or "",
            )
            for m in data
            if isinstance(m, dict) and m.get("title")
        ]

    async def create_milestone(self, title: str, description: str = "") -> Milestone:
        payload = json.dumps({"title": title, "description": description})
        result = await self._gh(
            "api",
            f"repos/{self.repo}/milestones",
            "--method",
            "POST",
            "--input",
            "-",
            stdin=payload,
        )
        if not result.success:
            raise RuntimeError(f"Failed to create milestone '{title}': {result.stderr[:200]}")

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"Failed to parse milestone creation response: {result.stdout[:200]}")

        return Milestone(
            title=data.get("title", title),
            state=data.get("state", "open"),
            description=data.get("description", "") or "",
        )

    async def set_milestone(self, task_id: str, milestone_title: str) -> None:
        result = await self._gh(
            "issue",
            "edit",
            task_id,
            "--repo",
            self.repo,
            "--milestone",
            milestone_title,
        )
        if not result.success:
            raise RuntimeError(
                f"Failed to set milestone '{milestone_title}' on issue #{task_id}: {result.stderr[:200]}"
            )

    async def _add_label(self, task_id: str, label: str) -> None:
        """Add a label to an issue, creating the label on the repo if it doesn't exist."""
        result = await self._gh(
            "issue",
            "edit",
            task_id,
            "--repo",
            self.repo,
            "--add-label",
            label,
        )
        if result.success:
            return

        # Match label-specific "not found" errors (e.g., "label 'X' not found").
        # Avoid matching issue-not-found or repo-not-found errors.
        stderr_lower = result.stderr.lower()
        if "label" in stderr_lower and "not found" in stderr_lower:
            log.info("label.auto_create", label=label, repo=self.repo)
            create = await self._gh(
                "label",
                "create",
                label,
                "--repo",
                self.repo,
                "--description",
                "",
                "--color",
                "ededed",
            )
            if not create.success:
                raise RuntimeError(f"Failed to create label '{label}': {create.stderr[:200]}")
            result = await self._gh(
                "issue",
                "edit",
                task_id,
                "--repo",
                self.repo,
                "--add-label",
                label,
            )
            if result.success:
                return

        raise RuntimeError(f"Failed to add label '{label}' to issue #{task_id}: {result.stderr[:200]}")

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
    from sova.llm.guard import sanitize_external_input

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

    body = sanitize_external_input(data.get("body", "") or "", source="github_issue")

    return Task(
        id=str(data["number"]),
        title=data.get("title", ""),
        body=body,
        state=state,
        labels=labels,
        assignees=assignees,
        url=data.get("url", ""),
        milestone=milestone,
        metadata=metadata,
    )
