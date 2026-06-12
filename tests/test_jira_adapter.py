"""Tests for the Jira task source adapter."""

from __future__ import annotations

import json

import pytest
import respx
from httpx import Response

from sova.adapters.base import TaskFilters, TaskState
from sova.adapters.jira import (
    _LABEL_TO_STATE,
    _STATE_LABELS,
    JiraAdapter,
)


def _adapter(
    base_url: str = "https://test.atlassian.net",
    project_key: str = "TEST",
    state_transitions: dict[str, str] | None = None,
) -> JiraAdapter:
    return JiraAdapter(
        base_url=base_url,
        email="test@example.com",
        api_token="test-token",
        project_key=project_key,
        state_transitions=state_transitions,
    )


def _issue_json(
    key: str = "TEST-42",
    summary: str = "Fix the login bug",
    status_name: str = "To Do",
    status_category: str = "To Do",
    labels: list[str] | None = None,
    description: dict | None = None,
) -> dict:
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "description": description,
            "status": {
                "name": status_name,
                "statusCategory": {"name": status_category},
            },
            "labels": labels or [],
            "assignee": None,
            "fixVersions": [],
        },
    }


# ---------------------------------------------------------------------------
# Constructor and helpers
# ---------------------------------------------------------------------------


class TestJiraAdapterInit:
    def test_base_url_trailing_slash_stripped(self) -> None:
        adapter = _adapter(base_url="https://test.atlassian.net/")
        assert adapter.base_url == "https://test.atlassian.net"

    def test_resolve_key_with_full_key(self) -> None:
        adapter = _adapter()
        assert adapter._resolve_key("PROJ-123") == "PROJ-123"

    def test_resolve_key_with_number_only(self) -> None:
        adapter = _adapter(project_key="MYPROJ")
        assert adapter._resolve_key("42") == "MYPROJ-42"


class TestClose:
    async def test_close_clears_client(self) -> None:
        adapter = _adapter()
        _ = adapter._http  # initialize the client
        assert adapter._client is not None
        await adapter.close()
        assert adapter._client is None

    async def test_close_noop_when_no_client(self) -> None:
        adapter = _adapter()
        assert adapter._client is None
        await adapter.close()
        assert adapter._client is None


class TestExtractText:
    def test_none_description(self) -> None:
        assert JiraAdapter._extract_text(None) == ""

    def test_simple_paragraph(self) -> None:
        adf = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Hello world"}],
                },
            ],
        }
        assert JiraAdapter._extract_text(adf) == "Hello world"

    def test_multiple_paragraphs(self) -> None:
        adf = {
            "type": "doc",
            "version": 1,
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Line 1"}]},
                {"type": "paragraph", "content": [{"type": "text", "text": "Line 2"}]},
            ],
        }
        result = JiraAdapter._extract_text(adf)
        assert "Line 1" in result
        assert "Line 2" in result


class TestParseIssue:
    def test_basic_issue(self) -> None:
        adapter = _adapter()
        task = adapter._parse_issue(_issue_json())
        assert task.id == "42"
        assert task.title == "Fix the login bug"
        assert task.state == TaskState.BACKLOG

    def test_done_status_category(self) -> None:
        adapter = _adapter()
        task = adapter._parse_issue(_issue_json(status_category="Done"))
        assert task.state == TaskState.DONE

    def test_agent_label_determines_state(self) -> None:
        adapter = _adapter()
        task = adapter._parse_issue(_issue_json(labels=["agent:triaged"]))
        assert task.state == TaskState.TRIAGED

    def test_done_overrides_label(self) -> None:
        adapter = _adapter()
        task = adapter._parse_issue(
            _issue_json(labels=["agent:in-progress"], status_category="Done"),
        )
        assert task.state == TaskState.DONE

    def test_url_constructed(self) -> None:
        adapter = _adapter()
        task = adapter._parse_issue(_issue_json(key="TEST-99"))
        assert task.url == "https://test.atlassian.net/browse/TEST-99"

    def test_metadata_includes_key(self) -> None:
        adapter = _adapter()
        task = adapter._parse_issue(_issue_json(key="TEST-7"))
        assert task.metadata["key"] == "TEST-7"


# ---------------------------------------------------------------------------
# API methods
# ---------------------------------------------------------------------------


class TestListTasks:
    @respx.mock
    async def test_list_open_tasks(self) -> None:
        adapter = _adapter()
        route = respx.post(
            "https://test.atlassian.net/rest/api/3/search/jql",
        ).mock(
            return_value=Response(200, json={"issues": [_issue_json(), _issue_json(key="TEST-43")]}),
        )
        tasks = await adapter.list_tasks(TaskFilters(state="open"))
        assert len(tasks) == 2
        assert route.called
        body = json.loads(route.calls[0].request.content)
        assert "statusCategory" in body["jql"]
        assert "Done" in body["jql"]

    @respx.mock
    async def test_list_tasks_api_error(self) -> None:
        adapter = _adapter()
        respx.post("https://test.atlassian.net/rest/api/3/search/jql").mock(
            return_value=Response(500, text="Server error"),
        )
        tasks = await adapter.list_tasks()
        assert tasks == []


class TestListTasksFiltering:
    @respx.mock
    async def test_component_filter_in_jql(self) -> None:
        adapter = JiraAdapter(
            base_url="https://test.atlassian.net",
            email="test@example.com",
            api_token="test-token",
            project_key="TEST",
            component="RBAC",
        )
        route = respx.post("https://test.atlassian.net/rest/api/3/search/jql").mock(
            return_value=Response(200, json={"issues": []}),
        )
        await adapter.list_tasks(TaskFilters(state="open"))
        body = json.loads(route.calls[0].request.content)
        assert 'component = "RBAC"' in body["jql"]

    @respx.mock
    async def test_jql_filter_appended(self) -> None:
        adapter = JiraAdapter(
            base_url="https://test.atlassian.net",
            email="test@example.com",
            api_token="test-token",
            project_key="TEST",
            jql_filter="assignee = currentUser()",
        )
        route = respx.post("https://test.atlassian.net/rest/api/3/search/jql").mock(
            return_value=Response(200, json={"issues": []}),
        )
        await adapter.list_tasks(TaskFilters(state="open"))
        body = json.loads(route.calls[0].request.content)
        assert "(assignee = currentUser())" in body["jql"]


class TestGetTask:
    @respx.mock
    async def test_get_task_success(self) -> None:
        adapter = _adapter()
        respx.get("https://test.atlassian.net/rest/api/3/issue/TEST-42").mock(
            return_value=Response(200, json=_issue_json()),
        )
        task = await adapter.get_task("42")
        assert task.id == "42"
        assert task.title == "Fix the login bug"

    @respx.mock
    async def test_get_task_not_found(self) -> None:
        adapter = _adapter()
        respx.get("https://test.atlassian.net/rest/api/3/issue/TEST-999").mock(
            return_value=Response(404, text="Not found"),
        )
        with pytest.raises(RuntimeError, match="Failed to fetch"):
            await adapter.get_task("999")


class TestGetState:
    @respx.mock
    async def test_state_from_label(self) -> None:
        adapter = _adapter()
        respx.get("https://test.atlassian.net/rest/api/3/issue/TEST-1").mock(
            return_value=Response(200, json=_issue_json(key="TEST-1", labels=["agent:researched"])),
        )
        state = await adapter.get_state("1")
        assert state == TaskState.RESEARCHED

    @respx.mock
    async def test_state_done_from_category(self) -> None:
        adapter = _adapter()
        respx.get("https://test.atlassian.net/rest/api/3/issue/TEST-1").mock(
            return_value=Response(200, json=_issue_json(key="TEST-1", status_category="Done")),
        )
        state = await adapter.get_state("1")
        assert state == TaskState.DONE

    @respx.mock
    async def test_state_default_backlog(self) -> None:
        adapter = _adapter()
        respx.get("https://test.atlassian.net/rest/api/3/issue/TEST-1").mock(
            return_value=Response(200, json=_issue_json(key="TEST-1")),
        )
        state = await adapter.get_state("1")
        assert state == TaskState.BACKLOG


class TestLabels:
    @respx.mock
    async def test_add_label(self) -> None:
        adapter = _adapter()
        route = respx.put("https://test.atlassian.net/rest/api/3/issue/TEST-1").mock(
            return_value=Response(204),
        )
        await adapter.add_label("1", "agent:triaged")
        assert route.called
        body = route.calls[0].request.content
        assert b"add" in body
        assert b"agent:triaged" in body

    @respx.mock
    async def test_remove_label(self) -> None:
        adapter = _adapter()
        route = respx.put("https://test.atlassian.net/rest/api/3/issue/TEST-1").mock(
            return_value=Response(204),
        )
        await adapter.remove_label("1", "agent:triaged")
        assert route.called
        body = route.calls[0].request.content
        assert b"remove" in body


class TestPostComment:
    @respx.mock
    async def test_post_comment(self) -> None:
        adapter = _adapter()
        route = respx.post("https://test.atlassian.net/rest/api/3/issue/TEST-1/comment").mock(
            return_value=Response(201, json={}),
        )
        await adapter.post_comment("1", "SOVA analysis complete")
        assert route.called
        body = route.calls[0].request.content
        assert b"SOVA analysis complete" in body


class TestPrNoOps:
    @respx.mock
    async def test_post_pr_comment_is_noop(self) -> None:
        adapter = _adapter()
        await adapter.post_pr_comment(42, "review")

    @respx.mock
    async def test_post_pr_review_is_noop(self) -> None:
        adapter = _adapter()
        await adapter.post_pr_review(42, "body", "APPROVE", [])


class TestEditBody:
    @respx.mock
    async def test_edit_body_success(self) -> None:
        adapter = _adapter()
        respx.put("https://test.atlassian.net/rest/api/3/issue/TEST-1").mock(
            return_value=Response(204),
        )
        await adapter.edit_body("1", "Updated description")

    @respx.mock
    async def test_edit_body_failure(self) -> None:
        adapter = _adapter()
        respx.put("https://test.atlassian.net/rest/api/3/issue/TEST-1").mock(
            return_value=Response(400, text="Bad request"),
        )
        with pytest.raises(RuntimeError, match="Failed to edit body"):
            await adapter.edit_body("1", "bad")


class TestLinkPR:
    @respx.mock
    async def test_link_pr(self) -> None:
        adapter = _adapter()
        route = respx.post("https://test.atlassian.net/rest/api/3/issue/TEST-1/remotelink").mock(
            return_value=Response(201, json={}),
        )
        await adapter.link_pr("1", "https://github.com/org/repo/pull/42")
        assert route.called
        body = route.calls[0].request.content
        assert b"Pull Request" in body


class TestTransitionState:
    @respx.mock
    async def test_transition_clears_old_labels_and_sets_new(self) -> None:
        adapter = _adapter()
        respx.get("https://test.atlassian.net/rest/api/3/issue/TEST-1").mock(
            return_value=Response(200, json=_issue_json(key="TEST-1", labels=["agent:triaged"])),
        )
        respx.put("https://test.atlassian.net/rest/api/3/issue/TEST-1").mock(
            return_value=Response(204),
        )
        respx.get("https://test.atlassian.net/rest/api/3/issue/TEST-1/transitions").mock(
            return_value=Response(200, json={"transitions": [{"id": "31", "name": "In Progress"}]}),
        )
        respx.post("https://test.atlassian.net/rest/api/3/issue/TEST-1/transitions").mock(
            return_value=Response(204),
        )
        await adapter.transition_state("1", TaskState.IN_PROGRESS)

    @respx.mock
    async def test_transition_with_config_override(self) -> None:
        adapter = _adapter(state_transitions={"in_progress": "Start Work"})
        respx.get("https://test.atlassian.net/rest/api/3/issue/TEST-1").mock(
            return_value=Response(200, json=_issue_json(key="TEST-1")),
        )
        respx.put("https://test.atlassian.net/rest/api/3/issue/TEST-1").mock(
            return_value=Response(204),
        )
        respx.get("https://test.atlassian.net/rest/api/3/issue/TEST-1/transitions").mock(
            return_value=Response(
                200,
                json={"transitions": [{"id": "5", "name": "Start Work"}, {"id": "31", "name": "In Progress"}]},
            ),
        )
        route = respx.post("https://test.atlassian.net/rest/api/3/issue/TEST-1/transitions").mock(
            return_value=Response(204),
        )
        await adapter.transition_state("1", TaskState.IN_PROGRESS)
        assert route.called
        body = route.calls[0].request.content
        assert b'"5"' in body


class TestGetPrReviews:
    async def test_returns_empty_list(self) -> None:
        adapter = _adapter()
        result = await adapter.get_pr_reviews(42)
        assert result == []


class TestStateLabelMappings:
    def test_all_mappable_states_have_labels(self) -> None:
        for state in (TaskState.TRIAGED, TaskState.RESEARCHED, TaskState.IN_PROGRESS, TaskState.IN_REVIEW):
            assert state in _STATE_LABELS

    def test_reverse_mapping_consistent(self) -> None:
        for state, label in _STATE_LABELS.items():
            assert _LABEL_TO_STATE[label] == state
