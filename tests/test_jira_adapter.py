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
    status_mapping: dict[str, str] | None = None,
) -> JiraAdapter:
    return JiraAdapter(
        base_url=base_url,
        email="test@example.com",
        api_token="test-token",
        project_key=project_key,
        state_transitions=state_transitions,
        status_mapping=status_mapping,
    )


def _issue_json(
    key: str = "TEST-42",
    summary: str = "Fix the login bug",
    status_name: str = "To Do",
    status_category: str = "To Do",
    labels: list[str] | None = None,
    description: dict | None = None,
    issue_type: str = "Task",
    priority: str = "Major",
    assignee_name: str | None = None,
    created: str = "2024-01-15T10:00:00.000+0000",
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
            "assignee": {"displayName": assignee_name} if assignee_name else None,
            "fixVersions": [],
            "issuetype": {"name": issue_type},
            "priority": {"name": priority},
            "created": created,
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

    def test_issue_type_field(self) -> None:
        adapter = _adapter()
        task = adapter._parse_issue(_issue_json(issue_type="Bug"))
        assert task.issue_type == "Bug"

    def test_jira_priority_in_metadata(self) -> None:
        adapter = _adapter()
        task = adapter._parse_issue(_issue_json(priority="Critical"))
        assert task.metadata["jira_priority"] == "Critical"

    def test_created_at_in_metadata(self) -> None:
        adapter = _adapter()
        task = adapter._parse_issue(_issue_json(created="2024-06-01T12:00:00.000+0000"))
        assert task.metadata["created_at"] == "2024-06-01T12:00:00.000+0000"

    def test_assignee_extracted(self) -> None:
        adapter = _adapter()
        task = adapter._parse_issue(_issue_json(assignee_name="Jane Doe"))
        assert task.assignees == ["Jane Doe"]

    def test_missing_issuetype_defaults_empty(self) -> None:
        adapter = _adapter()
        raw = _issue_json()
        del raw["fields"]["issuetype"]
        task = adapter._parse_issue(raw)
        assert task.issue_type == ""

    def test_missing_priority_defaults_empty(self) -> None:
        adapter = _adapter()
        raw = _issue_json()
        del raw["fields"]["priority"]
        task = adapter._parse_issue(raw)
        assert task.metadata["jira_priority"] == ""

    def test_null_priority_defaults_empty(self) -> None:
        adapter = _adapter()
        raw = _issue_json()
        raw["fields"]["priority"] = None
        task = adapter._parse_issue(raw)
        assert task.metadata["jira_priority"] == ""


class TestJiraStatusMapping:
    def test_refinement_maps_to_needs_spec(self) -> None:
        adapter = _adapter()
        task = adapter._parse_issue(_issue_json(status_name="Refinement"))
        assert task.state == TaskState.NEEDS_SPEC

    def test_new_maps_to_needs_spec(self) -> None:
        adapter = _adapter()
        task = adapter._parse_issue(_issue_json(status_name="New"))
        assert task.state == TaskState.NEEDS_SPEC

    def test_in_progress_maps_to_in_progress(self) -> None:
        adapter = _adapter()
        task = adapter._parse_issue(
            _issue_json(status_name="In Progress", status_category="In Progress"),
        )
        assert task.state == TaskState.IN_PROGRESS

    def test_code_review_maps_to_in_review(self) -> None:
        adapter = _adapter()
        task = adapter._parse_issue(
            _issue_json(status_name="Code Review", status_category="In Progress"),
        )
        assert task.state == TaskState.IN_REVIEW

    def test_agent_label_overrides_jira_status(self) -> None:
        adapter = _adapter()
        task = adapter._parse_issue(
            _issue_json(status_name="Refinement", labels=["agent:triaged"]),
        )
        assert task.state == TaskState.TRIAGED

    def test_done_overrides_jira_status_mapping(self) -> None:
        adapter = _adapter()
        task = adapter._parse_issue(
            _issue_json(status_name="Refinement", status_category="Done"),
        )
        assert task.state == TaskState.DONE

    def test_unknown_status_falls_to_backlog(self) -> None:
        adapter = _adapter()
        task = adapter._parse_issue(_issue_json(status_name="Custom Status"))
        assert task.state == TaskState.BACKLOG


class TestConfigurableStatusMapping:
    def test_custom_mapping_overrides_default(self) -> None:
        adapter = _adapter(status_mapping={"New": "triaged"})
        task = adapter._parse_issue(_issue_json(status_name="New"))
        assert task.state == TaskState.TRIAGED

    def test_custom_mapping_extends_defaults(self) -> None:
        adapter = _adapter(status_mapping={"ON_QA": "done"})
        task = adapter._parse_issue(_issue_json(status_name="ON_QA"))
        assert task.state == TaskState.DONE

    def test_defaults_still_work_without_config(self) -> None:
        adapter = _adapter()
        task = adapter._parse_issue(_issue_json(status_name="Refinement"))
        assert task.state == TaskState.NEEDS_SPEC

    def test_unmentioned_defaults_preserved(self) -> None:
        adapter = _adapter(status_mapping={"ON_QA": "done"})
        task = adapter._parse_issue(
            _issue_json(status_name="In Progress", status_category="In Progress"),
        )
        assert task.state == TaskState.IN_PROGRESS

    def test_done_category_overrides_custom_mapping(self) -> None:
        adapter = _adapter(status_mapping={"Closed": "backlog"})
        task = adapter._parse_issue(
            _issue_json(status_name="Closed", status_category="Done"),
        )
        assert task.state == TaskState.DONE

    def test_agent_label_wins_over_custom_mapping(self) -> None:
        adapter = _adapter(status_mapping={"In Progress": "done"})
        task = adapter._parse_issue(
            _issue_json(status_name="In Progress", labels=["agent:researched"]),
        )
        assert task.state == TaskState.RESEARCHED

    def test_unmapped_status_logs_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        adapter = _adapter()
        adapter._parse_issue(_issue_json(status_name="Weird Custom Status"))
        captured = capsys.readouterr()
        assert "Weird Custom Status" in captured.out
        assert "status.unmapped" in captured.out


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


class TestListTasksPagination:
    @respx.mock
    async def test_paginate_fetches_multiple_pages(self) -> None:
        adapter = _adapter()
        page1 = [_issue_json(key=f"TEST-{i}") for i in range(50)]
        page2 = [_issue_json(key=f"TEST-{50 + i}") for i in range(10)]
        route = respx.post("https://test.atlassian.net/rest/api/3/search/jql")
        route.side_effect = [
            Response(200, json={"issues": page1}),
            Response(200, json={"issues": page2}),
        ]
        tasks = await adapter.list_tasks(TaskFilters(paginate=True))
        assert len(tasks) == 60
        assert route.call_count == 2
        body1 = json.loads(route.calls[0].request.content)
        assert body1["startAt"] == 0
        body2 = json.loads(route.calls[1].request.content)
        assert body2["startAt"] == 50

    @respx.mock
    async def test_paginate_stops_on_partial_page(self) -> None:
        adapter = _adapter()
        page = [_issue_json(key=f"TEST-{i}") for i in range(30)]
        route = respx.post("https://test.atlassian.net/rest/api/3/search/jql").mock(
            return_value=Response(200, json={"issues": page}),
        )
        tasks = await adapter.list_tasks(TaskFilters(paginate=True))
        assert len(tasks) == 30
        assert route.call_count == 1

    @respx.mock
    async def test_paginate_caps_at_max_issues(self) -> None:
        adapter = _adapter()
        full_page = [_issue_json(key=f"TEST-{i}") for i in range(50)]
        route = respx.post("https://test.atlassian.net/rest/api/3/search/jql").mock(
            return_value=Response(200, json={"issues": full_page}),
        )
        tasks = await adapter.list_tasks(TaskFilters(paginate=True))
        # 2000 / 50 = 40 pages max
        assert route.call_count == 40
        assert len(tasks) == 2000


    @respx.mock
    async def test_paginate_returns_empty_on_mid_pagination_failure(self) -> None:
        adapter = _adapter()
        page1 = [_issue_json(key=f"TEST-{i}") for i in range(50)]
        route = respx.post("https://test.atlassian.net/rest/api/3/search/jql")
        route.side_effect = [
            Response(200, json={"issues": page1}),
            Response(500, json={"error": "Internal Server Error"}),
        ]
        tasks = await adapter.list_tasks(TaskFilters(paginate=True))
        assert tasks == []
        assert route.call_count == 2

    @respx.mock
    async def test_non_paginate_single_request(self) -> None:
        adapter = _adapter()
        route = respx.post("https://test.atlassian.net/rest/api/3/search/jql").mock(
            return_value=Response(200, json={"issues": [_issue_json()]}),
        )
        tasks = await adapter.list_tasks(TaskFilters(state="open"))
        assert len(tasks) == 1
        assert route.call_count == 1


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

    @respx.mock
    async def test_state_from_jira_status_refinement(self) -> None:
        adapter = _adapter()
        respx.get("https://test.atlassian.net/rest/api/3/issue/TEST-1").mock(
            return_value=Response(200, json=_issue_json(key="TEST-1", status_name="Refinement")),
        )
        state = await adapter.get_state("1")
        assert state == TaskState.NEEDS_SPEC

    @respx.mock
    async def test_state_from_jira_status_code_review(self) -> None:
        adapter = _adapter()
        respx.get("https://test.atlassian.net/rest/api/3/issue/TEST-1").mock(
            return_value=Response(
                200,
                json=_issue_json(key="TEST-1", status_name="Code Review", status_category="In Progress"),
            ),
        )
        state = await adapter.get_state("1")
        assert state == TaskState.IN_REVIEW

    @respx.mock
    async def test_state_done_overrides_jira_status(self) -> None:
        adapter = _adapter()
        respx.get("https://test.atlassian.net/rest/api/3/issue/TEST-1").mock(
            return_value=Response(
                200,
                json=_issue_json(key="TEST-1", status_name="Refinement", status_category="Done"),
            ),
        )
        state = await adapter.get_state("1")
        assert state == TaskState.DONE


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


class TestGetComments:
    @respx.mock
    async def test_get_comments_success(self) -> None:
        adapter = _adapter()
        respx.get("https://test.atlassian.net/rest/api/3/issue/TEST-1/comment").mock(
            return_value=Response(
                200,
                json={
                    "comments": [
                        {
                            "body": {
                                "type": "doc",
                                "version": 1,
                                "content": [
                                    {"type": "paragraph", "content": [{"type": "text", "text": "## Research"}]},
                                ],
                            }
                        },
                        {
                            "body": {
                                "type": "doc",
                                "version": 1,
                                "content": [
                                    {"type": "paragraph", "content": [{"type": "text", "text": "Just a note"}]},
                                ],
                            }
                        },
                    ]
                },
            ),
        )
        comments = await adapter.get_comments("1")
        assert len(comments) == 2
        assert "## Research" in comments[0]

    @respx.mock
    async def test_get_comments_empty(self) -> None:
        adapter = _adapter()
        respx.get("https://test.atlassian.net/rest/api/3/issue/TEST-1/comment").mock(
            return_value=Response(200, json={"comments": []}),
        )
        comments = await adapter.get_comments("1")
        assert comments == []

    @respx.mock
    async def test_get_comments_failure_returns_empty(self) -> None:
        adapter = _adapter()
        respx.get("https://test.atlassian.net/rest/api/3/issue/TEST-1/comment").mock(
            return_value=Response(500, text="Server error"),
        )
        comments = await adapter.get_comments("1")
        assert comments == []


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


class TestCreateIssue:
    @respx.mock
    async def test_create_issue_basic(self) -> None:
        adapter = _adapter()
        respx.post("https://test.atlassian.net/rest/api/3/issue").mock(
            return_value=Response(201, json={"id": "10042", "key": "TEST-42", "self": "..."}),
        )
        respx.get("https://test.atlassian.net/rest/api/3/issue/TEST-42").mock(
            return_value=Response(200, json=_issue_json(key="TEST-42", summary="New task")),
        )
        task = await adapter.create_issue("New task", "Description text")
        assert task.id == "42"
        assert task.title == "New task"

    @respx.mock
    async def test_create_issue_with_parent_key(self) -> None:
        adapter = _adapter()
        route = respx.post("https://test.atlassian.net/rest/api/3/issue").mock(
            return_value=Response(201, json={"id": "10043", "key": "TEST-43"}),
        )
        respx.get("https://test.atlassian.net/rest/api/3/issue/TEST-43").mock(
            return_value=Response(200, json=_issue_json(key="TEST-43", issue_type="Sub-task")),
        )
        task = await adapter.create_issue("Sub-task title", "body", issue_type="Sub-task", parent_key="TEST-42")
        assert task.id == "43"
        body = json.loads(route.calls[0].request.content)
        assert body["fields"]["parent"]["key"] == "TEST-42"
        assert body["fields"]["issuetype"]["name"] == "Sub-task"

    @respx.mock
    async def test_create_issue_with_labels(self) -> None:
        adapter = _adapter()
        route = respx.post("https://test.atlassian.net/rest/api/3/issue").mock(
            return_value=Response(201, json={"key": "TEST-44"}),
        )
        respx.get("https://test.atlassian.net/rest/api/3/issue/TEST-44").mock(
            return_value=Response(200, json=_issue_json(key="TEST-44")),
        )
        await adapter.create_issue("Title", labels=["bug", "priority:high"])
        body = json.loads(route.calls[0].request.content)
        assert body["fields"]["labels"] == ["bug", "priority:high"]

    @respx.mock
    async def test_create_issue_failure_raises(self) -> None:
        adapter = _adapter()
        respx.post("https://test.atlassian.net/rest/api/3/issue").mock(
            return_value=Response(400, text="Bad request"),
        )
        with pytest.raises(RuntimeError, match="Failed to create issue"):
            await adapter.create_issue("Bad issue")

    @respx.mock
    async def test_create_issue_multi_paragraph_body(self) -> None:
        adapter = _adapter()
        route = respx.post("https://test.atlassian.net/rest/api/3/issue").mock(
            return_value=Response(201, json={"key": "TEST-45"}),
        )
        respx.get("https://test.atlassian.net/rest/api/3/issue/TEST-45").mock(
            return_value=Response(200, json=_issue_json(key="TEST-45")),
        )
        await adapter.create_issue("Title", "Para 1\n\nPara 2\n\nPara 3")
        body = json.loads(route.calls[0].request.content)
        adf_content = body["fields"]["description"]["content"]
        assert len(adf_content) == 3
        assert adf_content[0]["content"][0]["text"] == "Para 1"
        assert adf_content[2]["content"][0]["text"] == "Para 3"


class TestGetAvailableTransitions:
    @respx.mock
    async def test_returns_transitions(self) -> None:
        adapter = _adapter()
        respx.get("https://test.atlassian.net/rest/api/3/issue/TEST-1/transitions").mock(
            return_value=Response(
                200,
                json={
                    "transitions": [
                        {"id": "11", "name": "To Do", "to": {"name": "To Do", "id": "1"}},
                        {"id": "21", "name": "In Progress", "to": {"name": "In Progress", "id": "2"}},
                        {"id": "31", "name": "Done", "to": {"name": "Done", "id": "3"}},
                    ],
                },
            ),
        )
        result = await adapter.get_available_transitions("1")
        assert len(result) == 3
        assert result[0] == {"id": "11", "name": "To Do", "to_status": "To Do"}
        assert result[1] == {"id": "21", "name": "In Progress", "to_status": "In Progress"}

    @respx.mock
    async def test_returns_empty_on_api_error(self) -> None:
        adapter = _adapter()
        respx.get("https://test.atlassian.net/rest/api/3/issue/TEST-1/transitions").mock(
            return_value=Response(403, text="Forbidden"),
        )
        result = await adapter.get_available_transitions("1")
        assert result == []


class TestParseIssueRichMetadata:
    def test_story_points_from_standard_field(self) -> None:
        adapter = _adapter()
        raw = _issue_json()
        raw["fields"]["story_points"] = 5.0
        task = adapter._parse_issue(raw)
        assert task.story_points == 5.0

    def test_story_points_from_custom_field(self) -> None:
        adapter = _adapter()
        raw = _issue_json()
        raw["fields"]["customfield_10028"] = 3
        task = adapter._parse_issue(raw)
        assert task.story_points == 3.0

    def test_story_points_zero_is_valid(self) -> None:
        adapter = _adapter()
        raw = _issue_json()
        raw["fields"]["story_points"] = 0
        task = adapter._parse_issue(raw)
        assert task.story_points == 0.0

    def test_story_points_zero_not_overridden_by_custom_field(self) -> None:
        adapter = _adapter()
        raw = _issue_json()
        raw["fields"]["story_points"] = 0
        raw["fields"]["customfield_10028"] = 5
        task = adapter._parse_issue(raw)
        assert task.story_points == 0.0

    def test_story_points_none_when_missing(self) -> None:
        adapter = _adapter()
        task = adapter._parse_issue(_issue_json())
        assert task.story_points is None

    def test_sprint_name_extracted(self) -> None:
        adapter = _adapter()
        raw = _issue_json()
        raw["fields"]["sprint"] = {"id": 1, "name": "Sprint 5", "state": "active"}
        task = adapter._parse_issue(raw)
        assert task.sprint == "Sprint 5"

    def test_sprint_empty_when_missing(self) -> None:
        adapter = _adapter()
        task = adapter._parse_issue(_issue_json())
        assert task.sprint == ""

    def test_components_extracted(self) -> None:
        adapter = _adapter()
        raw = _issue_json()
        raw["fields"]["components"] = [{"name": "Backend"}, {"name": "API"}]
        task = adapter._parse_issue(raw)
        assert task.components == ["Backend", "API"]

    def test_fix_versions_extracted(self) -> None:
        adapter = _adapter()
        raw = _issue_json()
        raw["fields"]["fixVersions"] = [{"name": "v1.0"}, {"name": "v1.1"}]
        task = adapter._parse_issue(raw)
        assert task.fix_versions == ["v1.0", "v1.1"]
        assert task.milestone == "v1.0"

    def test_issue_type_as_first_class_field(self) -> None:
        adapter = _adapter()
        task = adapter._parse_issue(_issue_json(issue_type="Bug"))
        assert task.issue_type == "Bug"


class TestStateLabelMappings:
    def test_all_mappable_states_have_labels(self) -> None:
        for state in (TaskState.TRIAGED, TaskState.RESEARCHED, TaskState.IN_PROGRESS, TaskState.IN_REVIEW):
            assert state in _STATE_LABELS

    def test_reverse_mapping_consistent(self) -> None:
        for state, label in _STATE_LABELS.items():
            assert _LABEL_TO_STATE[label] == state


# ---------------------------------------------------------------------------
# Milestone methods
# ---------------------------------------------------------------------------


class TestListMilestones:
    @respx.mock
    async def test_list_milestones_open(self) -> None:
        adapter = _adapter()
        respx.get("https://test.atlassian.net/rest/api/3/project/TEST/versions").mock(
            return_value=Response(
                200,
                json=[
                    {"name": "Phase 1: Now", "released": False, "archived": False, "description": "Current"},
                    {"name": "Phase 2: Next", "released": False, "archived": False, "description": ""},
                    {"name": "Old Release", "released": True, "archived": False, "description": "Done"},
                ],
            ),
        )

        from sova.adapters.base import Milestone

        milestones = await adapter.list_milestones(state="open")
        assert len(milestones) == 2
        assert milestones[0] == Milestone(title="Phase 1: Now", state="open", description="Current")
        assert milestones[1].title == "Phase 2: Next"

    @respx.mock
    async def test_list_milestones_all(self) -> None:
        adapter = _adapter()
        respx.get("https://test.atlassian.net/rest/api/3/project/TEST/versions").mock(
            return_value=Response(
                200,
                json=[
                    {"name": "Phase 1", "released": False, "archived": False},
                    {"name": "Old", "released": True, "archived": False},
                ],
            ),
        )
        milestones = await adapter.list_milestones(state="all")
        assert len(milestones) == 2

    @respx.mock
    async def test_list_milestones_api_error(self) -> None:
        adapter = _adapter()
        respx.get("https://test.atlassian.net/rest/api/3/project/TEST/versions").mock(
            return_value=Response(500, text="Internal error"),
        )
        milestones = await adapter.list_milestones()
        assert milestones == []


class TestCreateMilestone:
    @respx.mock
    async def test_create_milestone_success(self) -> None:
        adapter = _adapter()
        route = respx.post("https://test.atlassian.net/rest/api/3/version").mock(
            return_value=Response(201, json={"name": "Phase 1: Now", "id": "10001"}),
        )

        from sova.adapters.base import Milestone

        milestone = await adapter.create_milestone("Phase 1: Now", "Current work")
        assert milestone == Milestone(title="Phase 1: Now", state="open", description="")
        assert route.called
        body = json.loads(route.calls[0].request.content)
        assert body["name"] == "Phase 1: Now"
        assert body["project"] == "TEST"

    @respx.mock
    async def test_create_milestone_permission_error(self) -> None:
        adapter = _adapter()
        respx.post("https://test.atlassian.net/rest/api/3/version").mock(
            return_value=Response(403, text="Forbidden"),
        )
        with pytest.raises(PermissionError, match="Insufficient permissions"):
            await adapter.create_milestone("Phase 1: Now")

    @respx.mock
    async def test_create_milestone_other_error(self) -> None:
        adapter = _adapter()
        respx.post("https://test.atlassian.net/rest/api/3/version").mock(
            return_value=Response(400, text="Bad request"),
        )
        with pytest.raises(RuntimeError, match="Failed to create version"):
            await adapter.create_milestone("Bad")


class TestSetMilestone:
    _VERSION_JSON = [
        {"name": "Phase 1: Now", "id": "10001", "released": False, "archived": False},
    ]

    @respx.mock
    async def test_set_milestone_success(self) -> None:
        adapter = _adapter()
        respx.get("https://test.atlassian.net/rest/api/3/project/TEST/versions").mock(
            return_value=Response(200, json=self._VERSION_JSON),
        )
        route = respx.put("https://test.atlassian.net/rest/api/3/issue/TEST-42").mock(
            return_value=Response(204),
        )

        await adapter.set_milestone("42", "Phase 1: Now")
        assert route.called
        body = json.loads(route.calls[0].request.content)
        assert body["update"]["fixVersions"] == [{"add": {"id": "10001"}}]

    @respx.mock
    async def test_set_milestone_not_found(self) -> None:
        adapter = _adapter()
        respx.get("https://test.atlassian.net/rest/api/3/project/TEST/versions").mock(
            return_value=Response(200, json=[]),
        )
        with pytest.raises(RuntimeError, match="not found"):
            await adapter.set_milestone("42", "Nonexistent")

    @respx.mock
    async def test_set_milestone_list_versions_failure(self) -> None:
        adapter = _adapter()
        respx.get("https://test.atlassian.net/rest/api/3/project/TEST/versions").mock(
            return_value=Response(500, text="Internal server error"),
        )
        with pytest.raises(RuntimeError, match="Failed to list versions"):
            await adapter.set_milestone("42", "Phase 1: Now")

    @respx.mock
    async def test_set_milestone_update_failure_raises(self) -> None:
        adapter = _adapter()
        respx.get("https://test.atlassian.net/rest/api/3/project/TEST/versions").mock(
            return_value=Response(200, json=self._VERSION_JSON),
        )
        respx.put("https://test.atlassian.net/rest/api/3/issue/TEST-42").mock(
            return_value=Response(400, text="Bad request"),
        )
        with pytest.raises(RuntimeError, match="Failed to set milestone"):
            await adapter.set_milestone("42", "Phase 1: Now")
