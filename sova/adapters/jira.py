"""Jira Cloud adapter for SOVA task management."""

from __future__ import annotations

import base64
import re

import httpx

from sova.adapters.base import PRReview, Task, TaskAdapter, TaskFilters, TaskState
from sova.utils.logging import get_logger

log = get_logger(component="adapter.jira")

_STATE_LABELS: dict[TaskState, str] = {
    TaskState.TRIAGED: "agent:triaged",
    TaskState.RESEARCHED: "agent:researched",
    TaskState.IN_PROGRESS: "agent:in-progress",
    TaskState.IN_REVIEW: "agent:in-review",
    TaskState.NEEDS_SPEC: "agent:needs-spec",
    TaskState.HUMAN_ONLY: "agent:human-only",
}

_LABEL_TO_STATE: dict[str, TaskState] = {v: k for k, v in _STATE_LABELS.items()}

_JIRA_STATUS_TO_STATE: dict[str, TaskState] = {
    "To Do": TaskState.BACKLOG,
    "Backlog": TaskState.BACKLOG,
    "Open": TaskState.BACKLOG,
    "Refinement": TaskState.NEEDS_SPEC,
    "New": TaskState.NEEDS_SPEC,
    "In Progress": TaskState.IN_PROGRESS,
    "Code Review": TaskState.IN_REVIEW,
    "Review": TaskState.IN_REVIEW,
}

_DEFAULT_TRANSITIONS: dict[TaskState, list[str]] = {
    TaskState.BACKLOG: ["To Do", "Backlog", "Open"],
    TaskState.IN_PROGRESS: ["In Progress", "Start Progress"],
    TaskState.IN_REVIEW: ["In Review", "Review"],
    TaskState.DONE: ["Done", "Closed", "Resolved", "Close"],
}


class JiraAdapter(TaskAdapter):
    """Task adapter backed by Jira Cloud REST API v3."""

    def __init__(
        self,
        base_url: str,
        email: str,
        api_token: str,
        project_key: str,
        component: str = "",
        jql_filter: str = "",
        state_transitions: dict[str, str] | None = None,
        status_mapping: dict[str, str] | None = None,
    ) -> None:
        # jql_filter is appended verbatim to queries -- callers own validation.
        # component is sanitized; jql_filter is not because it may contain
        # operators, functions, and nested clauses that sanitization would break.
        super().__init__(repo="", github_user="")
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.project_key = project_key
        self.component = component
        self.jql_filter = jql_filter
        self._state_transitions = state_transitions or {}

        effective: dict[str, TaskState] = dict(_JIRA_STATUS_TO_STATE)
        if status_mapping:
            for jira_status, state_str in status_mapping.items():
                effective[jira_status] = TaskState(state_str)
        self._status_mapping = effective

        credentials = base64.b64encode(f"{email}:{api_token}".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._client: httpx.AsyncClient | None = None

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=f"{self.base_url}/rest/api/3",
                headers=self._headers,
                timeout=30.0,
            )
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _sanitize_jql_value(value: str) -> str:
        """Remove characters that could break JQL quoting."""
        return re.sub(r'["\\\x00-\x1f]', "", value)

    async def list_tasks(self, filters: TaskFilters | None = None) -> list[Task]:
        filters = filters or TaskFilters()
        safe_key = self._sanitize_jql_value(self.project_key)
        jql_parts = [f"project = {safe_key}"]

        if filters.state == "open":
            jql_parts.append("statusCategory != Done")
        elif filters.state == "closed":
            jql_parts.append("statusCategory = Done")

        if self.component:
            jql_parts.append(f'component = "{self._sanitize_jql_value(self.component)}"')

        if self.jql_filter:
            jql_parts.append(f"({self.jql_filter})")

        if filters.labels:
            for label in filters.labels:
                jql_parts.append(f'labels = "{self._sanitize_jql_value(label)}"')

        jql = " AND ".join(jql_parts)
        response = await self._http.post(
            "/search/jql",
            json={
                "jql": jql,
                "maxResults": 50,
                "fields": [
                    "summary",
                    "description",
                    "status",
                    "labels",
                    "assignee",
                    "fixVersions",
                    "key",
                    "issuetype",
                    "priority",
                    "created",
                ],
            },
        )
        if response.status_code != 200:
            log.warning("list_tasks.failed", status=response.status_code, body=response.text[:200])
            return []

        data = response.json()
        return [self._parse_issue(issue) for issue in data.get("issues", [])]

    async def get_task(self, task_id: str) -> Task:
        issue_key = self._resolve_key(task_id)
        response = await self._http.get(f"/issue/{issue_key}")
        if response.status_code != 200:
            msg = f"Failed to fetch issue {issue_key}: {response.text[:200]}"
            raise RuntimeError(msg)
        return self._parse_issue(response.json())

    def _resolve_state(self, labels: list[str], status: dict, issue_key: str) -> TaskState:
        state = TaskState.BACKLOG
        for label in labels:
            if label in _LABEL_TO_STATE:
                state = _LABEL_TO_STATE[label]
                break

        is_done_category = status.get("statusCategory", {}).get("name") == "Done"
        if is_done_category:
            state = TaskState.DONE
        elif state == TaskState.BACKLOG:
            status_name = status.get("name", "")
            if status_name in self._status_mapping:
                state = self._status_mapping[status_name]
            elif status_name:
                log.warning(
                    "status.unmapped",
                    status=status_name,
                    issue=issue_key,
                    hint="Add to [task_source] jira_status_mapping in sova.toml",
                )

        return state

    async def get_state(self, task_id: str) -> TaskState:
        issue_key = self._resolve_key(task_id)
        response = await self._http.get(
            f"/issue/{issue_key}",
            params={"fields": "status,labels"},
        )
        if response.status_code != 200:
            msg = f"Failed to get state for {issue_key}: {response.text[:200]}"
            raise RuntimeError(msg)

        fields = response.json().get("fields", {})
        return self._resolve_state(
            fields.get("labels", []),
            fields.get("status", {}),
            issue_key,
        )

    async def transition_state(self, task_id: str, new_state: TaskState) -> None:
        issue_key = self._resolve_key(task_id)

        await self._clear_state_labels(issue_key)
        if new_state != TaskState.DONE:
            label = _STATE_LABELS.get(new_state)
            if label:
                await self.add_label(task_id, label)

        await self._trigger_transition(issue_key, new_state)

    async def assign(self, task_id: str, agent_role: str) -> None:
        await self.add_label(task_id, f"role:{agent_role}")

    async def add_label(self, task_id: str, label: str) -> None:
        issue_key = self._resolve_key(task_id)
        response = await self._http.put(
            f"/issue/{issue_key}",
            json={"update": {"labels": [{"add": label}]}},
        )
        if response.status_code not in (200, 204):
            log.warning("add_label.failed", issue=issue_key, label=label, status=response.status_code)

    async def remove_label(self, task_id: str, label: str) -> None:
        issue_key = self._resolve_key(task_id)
        response = await self._http.put(
            f"/issue/{issue_key}",
            json={"update": {"labels": [{"remove": label}]}},
        )
        if response.status_code not in (200, 204):
            log.warning("remove_label.failed", issue=issue_key, label=label, status=response.status_code)

    async def post_comment(self, task_id: str, body: str) -> None:
        issue_key = self._resolve_key(task_id)
        adf_body = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": body}]}],
            },
        }
        response = await self._http.post(f"/issue/{issue_key}/comment", json=adf_body)
        if response.status_code not in (200, 201):
            log.warning("post_comment.failed", issue=issue_key, status=response.status_code)

    async def post_pr_comment(self, pr_number: int, body: str) -> None:
        log.info("post_pr_comment.no_op_for_jira", pr=pr_number)

    async def post_pr_review(
        self,
        pr_number: int,
        body: str,
        event: str,
        comments: list[dict],
    ) -> None:
        log.info("post_pr_review.no_op_for_jira", pr=pr_number)

    async def edit_body(self, task_id: str, body: str) -> None:
        issue_key = self._resolve_key(task_id)
        adf_body = {
            "fields": {
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": body}]}],
                },
            },
        }
        response = await self._http.put(f"/issue/{issue_key}", json=adf_body)
        if response.status_code not in (200, 204):
            msg = f"Failed to edit body for {issue_key}: {response.text[:200]}"
            raise RuntimeError(msg)

    async def get_pr_reviews(self, pr_number: int) -> list[PRReview]:
        log.info("get_pr_reviews.no_op_for_jira", pr=pr_number)
        return []

    async def link_pr(self, task_id: str, pr_url: str) -> None:
        issue_key = self._resolve_key(task_id)
        pr_id = pr_url.rstrip("/").split("/")[-1]
        response = await self._http.post(
            f"/issue/{issue_key}/remotelink",
            json={
                "object": {
                    "url": pr_url,
                    "title": f"Pull Request: #{pr_id}",
                },
            },
        )
        if response.status_code not in (200, 201):
            log.warning("link_pr.failed", issue=issue_key, status=response.status_code)

    def _resolve_key(self, task_id: str) -> str:
        if "-" in task_id:
            return task_id
        return f"{self.project_key}-{task_id}"

    def _parse_issue(self, data: dict) -> Task:
        fields = data.get("fields", {})
        labels = fields.get("labels", [])
        assignee = fields.get("assignee")
        fix_versions = fields.get("fixVersions", [])

        key = data.get("key", "")
        status = fields.get("status", {})
        state = self._resolve_state(labels, status, key or "?")

        issue_type = (fields.get("issuetype") or {}).get("name", "")
        priority_name = (fields.get("priority") or {}).get("name", "")
        created = fields.get("created", "")

        milestone = fix_versions[0]["name"] if fix_versions else ""
        number = key.split("-")[-1] if "-" in key else key

        return Task(
            id=number,
            title=fields.get("summary", ""),
            body=self._extract_text(fields.get("description")),
            state=state,
            labels=labels,
            assignees=[assignee["displayName"]] if assignee else [],
            url=f"{self.base_url}/browse/{key}",
            milestone=milestone,
            metadata={
                "key": key,
                "status": status.get("name", ""),
                "issue_type": issue_type,
                "jira_priority": priority_name,
                "created_at": created,
            },
        )

    @staticmethod
    def _extract_text(description: dict | None) -> str:
        if not description:
            return ""
        parts: list[str] = []
        for block in description.get("content", []):
            for inline in block.get("content", []):
                if inline.get("type") == "text":
                    parts.append(inline.get("text", ""))
            parts.append("\n")
        return "".join(parts).strip()

    async def _clear_state_labels(self, issue_key: str) -> None:
        response = await self._http.get(
            f"/issue/{issue_key}",
            params={"fields": "labels"},
        )
        if response.status_code != 200:
            return

        labels = response.json().get("fields", {}).get("labels", [])
        removals = [{"remove": label} for label in labels if label in _LABEL_TO_STATE]
        if removals:
            resp = await self._http.put(
                f"/issue/{issue_key}",
                json={"update": {"labels": removals}},
            )
            if resp.status_code not in (200, 204):
                log.warning("clear_labels.failed", issue=issue_key, status=resp.status_code)

    async def _trigger_transition(self, issue_key: str, target_state: TaskState) -> None:
        response = await self._http.get(f"/issue/{issue_key}/transitions")
        if response.status_code != 200:
            log.warning("transition.fetch_failed", issue=issue_key, status=response.status_code)
            return

        transitions = response.json().get("transitions", [])

        target_names = list(_DEFAULT_TRANSITIONS.get(target_state, []))
        config_name = self._state_transitions.get(target_state.value)
        if config_name:
            target_names.insert(0, config_name)

        for transition in transitions:
            if transition["name"] in target_names:
                resp = await self._http.post(
                    f"/issue/{issue_key}/transitions",
                    json={"transition": {"id": transition["id"]}},
                )
                if resp.status_code not in (200, 204):
                    log.warning(
                        "transition.post_failed",
                        issue=issue_key,
                        transition=transition["name"],
                        status=resp.status_code,
                    )
                    return
                log.info("transition.triggered", issue=issue_key, transition=transition["name"])
                return

        log.debug(
            "transition.no_match",
            issue=issue_key,
            state=str(target_state),
            available=[t["name"] for t in transitions],
        )
