"""Jira Cloud adapter for SOVA task management."""

from __future__ import annotations

import base64
import re

import httpx

from sova.adapters.base import Milestone, PRReview, Task, TaskAdapter, TaskFilters, TaskState
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


def _build_adf_doc(text: str) -> dict:
    """Build an Atlassian Document Format (ADF) document from plain text.

    Splits on double newlines for multi-paragraph support.
    """
    paragraphs = text.split("\n\n") if text else [""]
    content = [{"type": "paragraph", "content": [{"type": "text", "text": p}]} for p in paragraphs if p.strip()]
    if not content:
        content = [{"type": "paragraph", "content": [{"type": "text", "text": ""}]}]
    return {"type": "doc", "version": 1, "content": content}


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
        self._version_cache: dict[str, str] | None = None

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
                    "story_points",
                    "customfield_10028",
                    "sprint",
                    "components",
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

    async def _do_post_comment(self, task_id: str, body: str) -> None:
        issue_key = self._resolve_key(task_id)
        response = await self._http.post(
            f"/issue/{issue_key}/comment",
            json={"body": _build_adf_doc(body)},
        )
        if response.status_code not in (200, 201):
            log.warning("post_comment.failed", issue=issue_key, status=response.status_code)

    async def _do_post_pr_comment(self, pr_number: int, body: str) -> None:
        log.info("post_pr_comment.no_op_for_jira", pr=pr_number)

    async def _do_post_pr_review(
        self,
        pr_number: int,
        body: str,
        event: str,
        comments: list[dict],
    ) -> None:
        log.info("post_pr_review.no_op_for_jira", pr=pr_number)

    async def _do_edit_body(self, task_id: str, body: str) -> None:
        issue_key = self._resolve_key(task_id)
        response = await self._http.put(
            f"/issue/{issue_key}",
            json={"fields": {"description": _build_adf_doc(body)}},
        )
        if response.status_code not in (200, 204):
            msg = f"Failed to edit body for {issue_key}: {response.text[:200]}"
            raise RuntimeError(msg)

    async def get_pr_reviews(self, pr_number: int) -> list[PRReview]:
        log.info("get_pr_reviews.no_op_for_jira", pr=pr_number)
        return []

    async def get_comments(self, task_id: str) -> list[str]:
        issue_key = self._resolve_key(task_id)
        response = await self._http.get(f"/issue/{issue_key}/comment", params={"orderBy": "-created"})
        if response.status_code != 200:
            log.warning("get_comments.failed", issue=issue_key, status=response.status_code)
            return []
        bodies: list[str] = []
        for comment in response.json().get("comments", []):
            text = self._extract_text(comment.get("body"))
            if text:
                bodies.append(text)
        return bodies

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

    async def list_milestones(self, state: str = "open") -> list[Milestone]:
        response = await self._http.get(f"/project/{self.project_key}/versions")
        if response.status_code != 200:
            log.warning("list_milestones.failed", status=response.status_code, body=response.text[:200])
            return []

        milestones: list[Milestone] = []
        for v in response.json():
            released = v.get("released", False)
            archived = v.get("archived", False)
            version_state = "closed" if released or archived else "open"
            if state != "all" and version_state != state:
                continue
            milestones.append(
                Milestone(
                    title=v.get("name", ""),
                    state=version_state,
                    description=v.get("description", "") or "",
                )
            )
        return milestones

    async def create_milestone(self, title: str, description: str = "") -> Milestone:
        payload: dict = {
            "name": title,
            "project": self.project_key,
        }
        if description:
            payload["description"] = description

        response = await self._http.post(
            "/version",
            json=payload,
        )
        if response.status_code == 403:
            raise PermissionError("Insufficient permissions to create versions. Requires project admin role.")
        if response.status_code not in (200, 201):
            raise RuntimeError(f"Failed to create version '{title}': {response.text[:200]}")

        self._version_cache = None  # invalidate cache after creation

        data = response.json()
        return Milestone(
            title=data.get("name", title),
            state="open",
            description=data.get("description", "") or "",
        )

    async def _get_version_map(self) -> dict[str, str]:
        """Return cached {name: id} map of project versions."""
        if self._version_cache is not None:
            return self._version_cache

        resp = await self._http.get(f"/project/{self.project_key}/versions")
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to list versions: {resp.text[:200]}")

        self._version_cache = {v.get("name", ""): v.get("id", "") for v in resp.json()}
        return self._version_cache

    async def set_milestone(self, task_id: str, milestone_title: str) -> None:
        issue_key = self._resolve_key(task_id)

        version_map = await self._get_version_map()
        version_id = version_map.get(milestone_title)

        if version_id is None:
            raise RuntimeError(f"Fix version '{milestone_title}' not found in project {self.project_key}")

        update_resp = await self._http.put(
            f"/issue/{issue_key}",
            json={"update": {"fixVersions": [{"add": {"id": version_id}}]}},
        )
        if update_resp.status_code not in (200, 204):
            raise RuntimeError(
                f"Failed to set milestone '{milestone_title}' on issue {issue_key}: {update_resp.text[:200]}"
            )

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

        milestone = fix_versions[0].get("name", "") if fix_versions else ""
        fix_version_names = [fv.get("name", "") for fv in fix_versions]
        number = key.split("-")[-1] if "-" in key else key

        # Story points: try standard field name, then common custom field
        story_points_raw = fields.get("story_points")
        if story_points_raw is None:
            story_points_raw = fields.get("customfield_10028")
        story_points: float | None = None
        if story_points_raw is not None:
            try:
                story_points = float(story_points_raw)
            except (TypeError, ValueError):
                pass

        sprint_data = fields.get("sprint")
        sprint_name = ""
        if isinstance(sprint_data, dict):
            sprint_name = sprint_data.get("name", "")

        components_data = fields.get("components", [])
        component_names = [c["name"] for c in components_data if isinstance(c, dict) and "name" in c]

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
                "jira_priority": priority_name,
                "created_at": created,
            },
            issue_type=issue_type,
            story_points=story_points,
            sprint=sprint_name,
            components=component_names,
            fix_versions=fix_version_names,
        )

    @staticmethod
    def _extract_text(description: dict | None) -> str:
        if not description:
            return ""
        parts: list[str] = []
        for block in description.get("content", []):
            if block.get("type") == "heading":
                level = block.get("attrs", {}).get("level", 1)
                parts.append("#" * level + " ")
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

    async def _do_create_issue(
        self,
        title: str,
        body: str = "",
        labels: list[str] | None = None,
        issue_type: str = "",
        parent_key: str = "",
    ) -> Task:
        effective_type = issue_type or "Task"
        fields: dict = {
            "project": {"key": self.project_key},
            "summary": title,
            "issuetype": {"name": effective_type},
            "description": _build_adf_doc(body),
        }
        if labels:
            fields["labels"] = labels
        if parent_key:
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,255}-\d+", parent_key):
                raise ValueError(f"Invalid parent key format: {parent_key!r}")
            fields["parent"] = {"key": parent_key}

        response = await self._http.post("/issue", json={"fields": fields})
        if response.status_code not in (200, 201):
            msg = f"Failed to create issue: {response.text[:200]}"
            raise RuntimeError(msg)

        created = response.json()
        issue_key = created.get("key")
        if not issue_key:
            raise RuntimeError(f"Jira API response missing issue key: {created}")
        return await self.get_task(issue_key)

    async def get_available_transitions(self, task_id: str) -> list[dict[str, str]]:
        issue_key = self._resolve_key(task_id)
        response = await self._http.get(f"/issue/{issue_key}/transitions")
        if response.status_code != 200:
            log.warning("transitions.fetch_failed", issue=issue_key, status=response.status_code)
            return []

        result: list[dict[str, str]] = []
        for t in response.json().get("transitions", []):
            to_status = t.get("to", {})
            result.append(
                {
                    "id": t.get("id", ""),
                    "name": t.get("name", ""),
                    "to_status": to_status.get("name", ""),
                }
            )
        return result

    async def _trigger_transition(self, issue_key: str, target_state: TaskState) -> None:
        transitions = await self.get_available_transitions(issue_key)
        if not transitions:
            return

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
