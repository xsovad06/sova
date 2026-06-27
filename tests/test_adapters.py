"""Tests for SOVA task adapters."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from sova.adapters.base import Task, TaskAdapter, TaskFilters, TaskState
from sova.adapters.github import GitHubAdapter

# ---------------------------------------------------------------------------
# TaskState enum
# ---------------------------------------------------------------------------


class TestTaskState:
    def test_all_states_exist(self) -> None:
        states = {s.value for s in TaskState}
        assert states == {
            "backlog",
            "triaged",
            "researched",
            "in_progress",
            "in_review",
            "done",
            "needs_spec",
            "human_only",
        }

    def test_string_value(self) -> None:
        assert TaskState.IN_PROGRESS == "in_progress"
        assert str(TaskState.DONE) == "done"


# ---------------------------------------------------------------------------
# Task dataclass
# ---------------------------------------------------------------------------


class TestTask:
    def test_create_task(self) -> None:
        task = Task(
            id="42",
            title="Fix login bug",
            body="The login page crashes",
            state=TaskState.BACKLOG,
            labels=["bug", "critical"],
            assignees=[],
            url="https://github.com/user/repo/issues/42",
        )
        assert task.id == "42"
        assert task.title == "Fix login bug"
        assert task.state == TaskState.BACKLOG
        assert "bug" in task.labels

    def test_task_defaults(self) -> None:
        task = Task(id="1", title="Test")
        assert task.body == ""
        assert task.state == TaskState.BACKLOG
        assert task.labels == []
        assert task.assignees == []
        assert task.url == ""
        assert task.milestone == ""
        assert task.metadata == {}
        assert task.issue_type == ""
        assert task.story_points is None
        assert task.sprint == ""
        assert task.components == []
        assert task.fix_versions == []

    def test_task_rich_metadata(self) -> None:
        task = Task(
            id="1",
            title="Test",
            issue_type="Bug",
            story_points=5.0,
            sprint="Sprint 3",
            components=["Backend", "API"],
            fix_versions=["v1.0", "v1.1"],
        )
        assert task.issue_type == "Bug"
        assert task.story_points == 5.0
        assert task.sprint == "Sprint 3"
        assert task.components == ["Backend", "API"]
        assert task.fix_versions == ["v1.0", "v1.1"]


# ---------------------------------------------------------------------------
# TaskAdapter (abstract -- verify interface)
# ---------------------------------------------------------------------------


class TestTaskAdapterInterface:
    def test_cannot_instantiate_abstract(self) -> None:
        with pytest.raises(TypeError):
            TaskAdapter(repo="user/repo")  # type: ignore[abstract]

    def test_subclass_must_implement_methods(self) -> None:
        required_methods = [
            "list_tasks",
            "get_task",
            "transition_state",
            "assign",
            "add_label",
            "remove_label",
            "post_comment",
            "post_pr_comment",
            "edit_body",
            "get_state",
            "link_pr",
            "get_pr_reviews",
            "create_issue",
            "get_available_transitions",
        ]

        class IncompleteAdapter(TaskAdapter):
            pass

        with pytest.raises(TypeError):
            IncompleteAdapter(repo="user/repo")  # type: ignore[abstract]

        # Verify the ABC has the methods defined
        for method in required_methods:
            assert hasattr(TaskAdapter, method)


# ---------------------------------------------------------------------------
# GitHubAdapter -- unit tests with mocked shell calls
# ---------------------------------------------------------------------------


# Helpers for creating mock shell results
def _shell_result(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Create a mock ShellResult."""
    from sova.utils.shell import ShellResult

    return ShellResult(returncode=returncode, stdout=stdout, stderr=stderr)


def _gh_issues_json() -> str:
    """Sample gh issue list JSON output."""
    return json.dumps(
        [
            {
                "number": 42,
                "title": "Fix login bug",
                "body": "Login crashes on submit",
                "state": "OPEN",
                "labels": [{"name": "bug"}, {"name": "agent:ready"}],
                "assignees": [{"login": "dev-bot"}],
                "milestone": {"title": "v1.0"},
                "url": "https://github.com/user/repo/issues/42",
            },
            {
                "number": 15,
                "title": "Add dark mode",
                "body": "Implement dark theme",
                "state": "OPEN",
                "labels": [{"name": "feature"}],
                "assignees": [],
                "milestone": None,
                "url": "https://github.com/user/repo/issues/15",
            },
        ]
    )


def _gh_issue_view_json() -> str:
    """Sample gh issue view JSON output."""
    return json.dumps(
        {
            "number": 42,
            "title": "Fix login bug",
            "body": "Login crashes on submit\n\n## Acceptance Criteria\n- Fix the bug",
            "state": "OPEN",
            "labels": [{"name": "bug"}, {"name": "agent:ready"}],
            "assignees": [{"login": "dev-bot"}],
            "milestone": {"title": "v1.0"},
            "url": "https://github.com/user/repo/issues/42",
        }
    )


class TestGitHubAdapter:
    def setup_method(self) -> None:
        self.adapter = GitHubAdapter(repo="user/repo")

    @pytest.fixture
    def mock_run(self):
        with (
            patch("sova.adapters.github.run", new_callable=AsyncMock) as mock,
            patch("sova.adapters.github.resolve_gh_env", new_callable=AsyncMock, return_value=None),
        ):
            yield mock

    # -- list_tasks --

    async def test_list_tasks_basic(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = _shell_result(stdout=_gh_issues_json())

        tasks = await self.adapter.list_tasks()

        assert len(tasks) == 2
        assert tasks[0].id == "42"
        assert tasks[0].title == "Fix login bug"
        assert "bug" in tasks[0].labels
        assert tasks[0].assignees == ["dev-bot"]
        assert tasks[0].milestone == "v1.0"
        assert tasks[1].id == "15"
        assert tasks[1].milestone == ""

    async def test_list_tasks_with_milestone_filter(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = _shell_result(stdout="[]")

        await self.adapter.list_tasks(filters=TaskFilters(milestone="v1.0"))

        call_args = mock_run.call_args[0]
        assert "--milestone" in call_args
        assert "v1.0" in call_args

    async def test_list_tasks_with_labels_filter(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = _shell_result(stdout="[]")

        await self.adapter.list_tasks(filters=TaskFilters(labels=["bug", "critical"]))

        call_args = mock_run.call_args[0]
        assert "--label" in call_args
        assert "bug,critical" in call_args

    async def test_list_tasks_empty(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = _shell_result(stdout="[]")

        tasks = await self.adapter.list_tasks()
        assert tasks == []

    async def test_list_tasks_gh_failure(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = _shell_result(returncode=1, stderr="API error")

        tasks = await self.adapter.list_tasks()
        assert tasks == []

    # -- get_task --

    async def test_get_task(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = _shell_result(stdout=_gh_issue_view_json())

        task = await self.adapter.get_task("42")

        assert task.id == "42"
        assert task.title == "Fix login bug"
        assert "Acceptance Criteria" in task.body

    async def test_get_task_not_found(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = _shell_result(returncode=1, stderr="not found")

        with pytest.raises(RuntimeError, match="Failed to fetch"):
            await self.adapter.get_task("999")

    # -- transition_state --

    async def test_transition_state_done_closes_issue(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = _shell_result()

        await self.adapter.transition_state("42", TaskState.DONE)

        call_args = mock_run.call_args[0]
        assert "issue" in call_args
        assert "close" in call_args
        assert "42" in call_args

    async def test_transition_state_in_progress_adds_label(self, mock_run: AsyncMock) -> None:
        # First call: _clear_state_labels fetches current labels
        # Second call: _clear_state_labels is a no-op (no state labels)
        # Third call: add the new label
        mock_run.side_effect = [
            _shell_result(stdout='{"labels": []}'),
            _shell_result(),
        ]

        await self.adapter.transition_state("42", TaskState.IN_PROGRESS)

        last_call_args = mock_run.call_args[0]
        assert "edit" in last_call_args
        assert "--add-label" in last_call_args

    async def test_transition_state_needs_spec(self, mock_run: AsyncMock) -> None:
        mock_run.side_effect = [
            _shell_result(stdout='{"labels": []}'),
            _shell_result(),
        ]

        await self.adapter.transition_state("42", TaskState.NEEDS_SPEC)

        last_call_args = mock_run.call_args[0]
        assert "agent:needs-spec" in last_call_args

    # -- assign --

    async def test_assign(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = _shell_result()

        await self.adapter.assign("42", "developer")

        call_args = mock_run.call_args[0]
        assert "issue" in call_args
        assert "edit" in call_args

    # -- add_label / remove_label --

    async def test_add_label(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = _shell_result()

        await self.adapter.add_label("42", "agent:ready")

        call_args = mock_run.call_args[0]
        assert "--add-label" in call_args
        assert "agent:ready" in call_args

    async def test_add_label_auto_creates_missing_label(self, mock_run: AsyncMock) -> None:
        """When label doesn't exist, auto-create it and retry."""
        mock_run.side_effect = [
            _shell_result(returncode=1, stderr="label 'agent:custom' not found"),
            _shell_result(),  # label create succeeds
            _shell_result(),  # retry add-label succeeds
        ]

        await self.adapter.add_label("42", "agent:custom")

        assert mock_run.call_count == 3
        # Second call should be label create
        create_args = mock_run.call_args_list[1][0]
        assert "label" in create_args
        assert "create" in create_args
        assert "agent:custom" in create_args

    async def test_add_label_auto_create_fails_raises(self, mock_run: AsyncMock) -> None:
        """When label create itself fails, raise RuntimeError."""
        mock_run.side_effect = [
            _shell_result(returncode=1, stderr="label 'x' not found"),
            _shell_result(returncode=1, stderr="Permission denied"),
        ]

        with pytest.raises(RuntimeError, match="Failed to create label"):
            await self.adapter.add_label("42", "x")

    async def test_add_label_retry_after_create_fails_raises(self, mock_run: AsyncMock) -> None:
        """When retry after label create also fails, raise RuntimeError."""
        mock_run.side_effect = [
            _shell_result(returncode=1, stderr="label 'x' not found"),
            _shell_result(),  # label create succeeds
            _shell_result(returncode=1, stderr="Unexpected error"),
        ]

        with pytest.raises(RuntimeError, match="Failed to add label"):
            await self.adapter.add_label("42", "x")

    async def test_add_label_non_notfound_error_raises(self, mock_run: AsyncMock) -> None:
        """When first add-label fails with non-'not found' error, raise immediately."""
        mock_run.return_value = _shell_result(returncode=1, stderr="Network timeout")

        with pytest.raises(RuntimeError, match="Failed to add label"):
            await self.adapter.add_label("42", "agent:ready")

        assert mock_run.call_count == 1

    async def test_remove_label(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = _shell_result()

        await self.adapter.remove_label("42", "agent:ready")

        call_args = mock_run.call_args[0]
        assert "--remove-label" in call_args
        assert "agent:ready" in call_args

    # -- edit_body --

    async def test_edit_body(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = _shell_result()

        await self.adapter.edit_body("42", "Updated body content")

        call_args = mock_run.call_args[0]
        assert "issue" in call_args
        assert "edit" in call_args
        assert "42" in call_args
        assert "--body" in call_args
        assert "Updated body content" in call_args

    async def test_edit_body_failure_raises(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = _shell_result(returncode=1, stderr="Permission denied")

        with pytest.raises(RuntimeError, match="Failed to edit body"):
            await self.adapter.edit_body("42", "content")

    # -- transition_state error handling --

    async def test_transition_state_done_failure_raises(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = _shell_result(returncode=1, stderr="Not found")

        with pytest.raises(RuntimeError, match="Failed to close issue"):
            await self.adapter.transition_state("42", TaskState.DONE)

    # -- post_comment --

    async def test_post_comment(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = _shell_result()

        await self.adapter.post_comment("42", "Assessment complete.")

        call_args = mock_run.call_args[0]
        assert "issue" in call_args
        assert "comment" in call_args
        assert "42" in call_args

    # -- post_pr_comment --

    async def test_post_pr_comment(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = _shell_result()

        await self.adapter.post_pr_comment(82, "Review findings here.")

        call_args = mock_run.call_args[0]
        assert "pr" in call_args
        assert "comment" in call_args
        assert "82" in call_args

    # -- post_pr_review --

    async def test_post_pr_review(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = _shell_result(stdout='{"id": 123}')

        comments = [{"path": "foo.py", "line": 10, "side": "RIGHT", "body": "Issue here"}]
        await self.adapter.post_pr_review(82, "Summary", "COMMENT", comments)

        call_args = mock_run.call_args[0]
        assert "api" in call_args
        assert "repos/user/repo/pulls/82/reviews" in call_args
        assert "--method" in call_args
        assert "POST" in call_args
        assert "--input" in call_args
        # Verify JSON was passed via stdin kwarg
        stdin_data = mock_run.call_args[1].get("stdin", "")
        assert "Summary" in stdin_data
        assert "COMMENT" in stdin_data
        assert "foo.py" in stdin_data

    async def test_post_pr_review_fails_immediately_without_comments(self, mock_run: AsyncMock) -> None:
        """When no inline comments, don't retry -- fail immediately."""
        mock_run.return_value = _shell_result(returncode=1, stderr="Validation Failed")

        with pytest.raises(RuntimeError, match="Failed to post PR review"):
            await self.adapter.post_pr_review(82, "body", "COMMENT", [])

        assert mock_run.call_count == 1

    async def test_post_pr_review_retries_without_comments_on_failure(self, mock_run: AsyncMock) -> None:
        """When inline comments cause a 422, retry with body-only review."""
        comments = [{"path": "foo.py", "line": 999, "side": "RIGHT", "body": "Bad line ref"}]
        mock_run.side_effect = [
            _shell_result(returncode=1, stderr="Validation Failed: line 999 not in diff"),
            _shell_result(stdout='{"id": 456}'),
        ]

        await self.adapter.post_pr_review(82, "Summary", "COMMENT", comments)

        assert mock_run.call_count == 2
        retry_stdin = mock_run.call_args_list[1][1].get("stdin", "")
        retry_payload = json.loads(retry_stdin)
        assert retry_payload["comments"] == []
        assert retry_payload["body"] == "Summary"
        assert retry_payload["event"] == "COMMENT"

    async def test_post_pr_review_raises_when_retry_also_fails(self, mock_run: AsyncMock) -> None:
        """When both inline and body-only attempts fail, raise RuntimeError."""
        comments = [{"path": "foo.py", "line": 10, "side": "RIGHT", "body": "Issue"}]
        mock_run.side_effect = [
            _shell_result(returncode=1, stderr="Inline failed"),
            _shell_result(returncode=1, stderr="Body-only also failed"),
        ]

        with pytest.raises(RuntimeError, match="Failed to post PR review"):
            await self.adapter.post_pr_review(82, "body", "COMMENT", comments)

        assert mock_run.call_count == 2

    # -- get_state --

    async def test_get_state_from_labels(self, mock_run: AsyncMock) -> None:
        issue_json = json.dumps(
            {
                "state": "OPEN",
                "labels": [{"name": "agent:in-progress"}],
            }
        )
        mock_run.return_value = _shell_result(stdout=issue_json)

        state = await self.adapter.get_state("42")
        assert state == TaskState.IN_PROGRESS

    async def test_get_state_closed_is_done(self, mock_run: AsyncMock) -> None:
        issue_json = json.dumps({"state": "CLOSED", "labels": []})
        mock_run.return_value = _shell_result(stdout=issue_json)

        state = await self.adapter.get_state("42")
        assert state == TaskState.DONE

    async def test_get_state_open_no_labels_is_backlog(self, mock_run: AsyncMock) -> None:
        issue_json = json.dumps({"state": "OPEN", "labels": []})
        mock_run.return_value = _shell_result(stdout=issue_json)

        state = await self.adapter.get_state("42")
        assert state == TaskState.BACKLOG

    # -- link_pr --

    async def test_link_pr(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = _shell_result()

        await self.adapter.link_pr("42", "https://github.com/user/repo/pull/99")

        call_args = mock_run.call_args[0]
        assert "comment" in call_args

    # -- create_issue --

    async def test_create_issue(self, mock_run: AsyncMock) -> None:
        view_json = json.dumps(
            {
                "number": 99,
                "title": "New issue",
                "body": "Issue body",
                "state": "OPEN",
                "labels": [{"name": "bug"}],
                "assignees": [],
                "milestone": None,
                "url": "https://github.com/user/repo/issues/99",
            }
        )
        # First call: gh issue create returns the issue URL
        # Second call: gh issue view returns full JSON
        mock_run.side_effect = [
            _shell_result(stdout="https://github.com/user/repo/issues/99\n"),
            _shell_result(stdout=view_json),
        ]

        task = await self.adapter.create_issue("New issue", "Issue body", ["bug"])

        assert task.id == "99"
        assert task.title == "New issue"
        create_args = mock_run.call_args_list[0][0]
        assert "issue" in create_args
        assert "create" in create_args
        assert "--title" in create_args
        assert "New issue" in create_args
        assert "--label" in create_args
        assert "bug" in create_args

    async def test_create_issue_failure_raises(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = _shell_result(returncode=1, stderr="Permission denied")

        with pytest.raises(RuntimeError, match="Failed to create issue"):
            await self.adapter.create_issue("Test", "body")

    # -- get_available_transitions --

    async def test_get_available_transitions_returns_empty(self, mock_run: AsyncMock) -> None:
        result = await self.adapter.get_available_transitions("42")
        assert result == []
        mock_run.assert_not_called()

    # -- get_pr_reviews --

    async def test_get_pr_reviews_parses_reviews(self, mock_run: AsyncMock) -> None:
        reviews_json = json.dumps(
            [
                {
                    "id": 1,
                    "user": {"login": "alice", "type": "User"},
                    "state": "APPROVED",
                    "body": "LGTM",
                    "submitted_at": "2026-01-01T10:00:00Z",
                },
                {
                    "id": 2,
                    "user": {"login": "coderabbit[bot]", "type": "Bot"},
                    "state": "CHANGES_REQUESTED",
                    "body": "Found issues",
                    "submitted_at": "2026-01-01T11:00:00Z",
                },
            ]
        )
        mock_run.return_value = _shell_result(stdout=reviews_json)

        from sova.adapters.base import PRReview

        reviews = await self.adapter.get_pr_reviews(99)

        assert len(reviews) == 2
        assert reviews[0] == PRReview(
            reviewer="alice",
            state="APPROVED",
            body="LGTM",
            submitted_at="2026-01-01T10:00:00Z",
            is_bot=False,
        )
        assert reviews[1].reviewer == "coderabbit[bot]"
        assert reviews[1].is_bot is True
        assert reviews[1].state == "CHANGES_REQUESTED"

    async def test_get_pr_reviews_empty_on_failure(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = _shell_result(stderr="API error", returncode=1)

        reviews = await self.adapter.get_pr_reviews(99)
        assert reviews == []

    async def test_get_pr_reviews_empty_on_bad_json(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = _shell_result(stdout="not json")

        reviews = await self.adapter.get_pr_reviews(99)
        assert reviews == []

    async def test_get_pr_reviews_calls_paginate(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = _shell_result(stdout="[]")

        await self.adapter.get_pr_reviews(42)

        call_args = mock_run.call_args[0]
        assert "repos/user/repo/pulls/42/reviews" in " ".join(str(a) for a in call_args)
        assert "--paginate" in call_args

    async def test_get_pr_reviews_non_list_response(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = _shell_result(stdout='{"error": "not a list"}')

        reviews = await self.adapter.get_pr_reviews(99)
        assert reviews == []

    async def test_get_pr_reviews_skips_non_dict_items(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = _shell_result(
            stdout=json.dumps(
                [
                    "not-a-dict",
                    {
                        "user": {"login": "alice", "type": "User"},
                        "state": "APPROVED",
                        "body": "ok",
                        "submitted_at": "2026-01-01T10:00:00Z",
                    },
                ]
            )
        )

        reviews = await self.adapter.get_pr_reviews(99)
        assert len(reviews) == 1
        assert reviews[0].reviewer == "alice"

    async def test_get_pr_reviews_skips_malformed_entries(self, mock_run: AsyncMock) -> None:
        ts = "2026-01-01T10:00:00Z"
        mock_run.return_value = _shell_result(
            stdout=json.dumps(
                [
                    {"user": {"login": "", "type": "User"}, "state": "APPROVED", "submitted_at": ts},
                    {"user": {"login": "bob", "type": "User"}, "state": "", "submitted_at": ts},
                    {"user": {"login": "carol", "type": "User"}, "state": "APPROVED", "submitted_at": ""},
                ]
            )
        )

        reviews = await self.adapter.get_pr_reviews(99)
        assert reviews == []


# ---------------------------------------------------------------------------
# GitHub auth threading
# ---------------------------------------------------------------------------


class TestGitHubAdapterAuth:
    @pytest.mark.asyncio
    async def test_gh_passes_env_from_resolve(self) -> None:
        """Verify _gh() calls resolve_gh_env and passes the result to run()."""
        adapter = GitHubAdapter(repo="user/repo", github_user="xsovad06")
        fake_env = {"GH_TOKEN": "test_token", "PATH": "/usr/bin"}

        with (
            patch("sova.adapters.github.resolve_gh_env", new_callable=AsyncMock, return_value=fake_env) as mock_resolve,
            patch("sova.adapters.github.run", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = _shell_result(stdout="[]")
            await adapter._gh("issue", "list")

            mock_resolve.assert_called_once_with("xsovad06")
            mock_run.assert_called_once_with("gh", "issue", "list", env=fake_env)

    @pytest.mark.asyncio
    async def test_gh_no_user_passes_none_env(self) -> None:
        """When github_user is empty, resolve_gh_env returns None."""
        adapter = GitHubAdapter(repo="user/repo")

        with (
            patch("sova.adapters.github.resolve_gh_env", new_callable=AsyncMock, return_value=None) as mock_resolve,
            patch("sova.adapters.github.run", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.return_value = _shell_result(stdout="[]")
            await adapter._gh("issue", "list")

            mock_resolve.assert_called_once_with("")
            mock_run.assert_called_once_with("gh", "issue", "list", env=None)


# ---------------------------------------------------------------------------
# Adapter factory
# ---------------------------------------------------------------------------


class TestAdapterFactory:
    def test_create_github_adapter(self) -> None:
        from sova.adapters import create_adapter
        from sova.config.models import ProjectConfig

        config = ProjectConfig(github_repo="user/repo")
        adapter = create_adapter(config)
        assert isinstance(adapter, GitHubAdapter)
        assert adapter.project_number == 0

    def test_create_github_adapter_with_user(self) -> None:
        from sova.adapters import create_adapter
        from sova.config.models import ProjectConfig

        config = ProjectConfig(github_repo="user/repo", github_user="testuser")
        adapter = create_adapter(config)
        assert isinstance(adapter, GitHubAdapter)
        assert adapter.github_user == "testuser"

    def test_create_github_adapter_with_project_number(self) -> None:
        from sova.adapters import create_adapter
        from sova.config.models import ProjectConfig, TaskSourceConfig

        config = ProjectConfig(
            github_repo="user/repo",
            task_source=TaskSourceConfig(github_project_number=1),
        )
        adapter = create_adapter(config)
        assert isinstance(adapter, GitHubAdapter)
        assert adapter.project_number == 1

    def test_create_jira_adapter(self) -> None:
        from sova.adapters import create_adapter
        from sova.adapters.jira import JiraAdapter
        from sova.config.models import ProjectConfig, TaskSourceConfig

        config = ProjectConfig(
            task_source=TaskSourceConfig(
                type="jira",
                jira_base_url="https://test.atlassian.net",
                jira_email="test@example.com",
                jira_api_token="token",
                jira_project_key="TEST",
            ),
        )
        adapter = create_adapter(config)
        assert isinstance(adapter, JiraAdapter)
        assert adapter.project_key == "TEST"

    def test_create_jira_adapter_missing_config_raises(self) -> None:
        from sova.adapters import create_adapter
        from sova.config.models import ProjectConfig, TaskSourceConfig

        config = ProjectConfig(task_source=TaskSourceConfig(type="jira"))
        with pytest.raises(ValueError, match="jira_base_url"):
            create_adapter(config)

    def test_create_unknown_adapter_raises(self) -> None:
        from sova.adapters import create_adapter
        from sova.config.models import ProjectConfig, TaskSourceConfig

        ts = TaskSourceConfig()
        ts.type = "unknown"  # type: ignore[assignment]
        config = ProjectConfig(task_source=ts)
        with pytest.raises(ValueError, match="Unknown adapter type"):
            create_adapter(config)


# ---------------------------------------------------------------------------
# Project board integration
# ---------------------------------------------------------------------------


class TestProjectBoard:
    def setup_method(self) -> None:
        self.adapter = GitHubAdapter(repo="user/repo", project_number=1)

    @pytest.fixture
    def mock_run(self):
        with (
            patch("sova.adapters.github.run", new_callable=AsyncMock) as mock,
            patch("sova.adapters.github.resolve_gh_env", new_callable=AsyncMock, return_value=None),
        ):
            yield mock

    def test_resolve_board_option_matches_in_progress(self) -> None:
        from sova.adapters.github import _ProjectBoardMeta

        meta = _ProjectBoardMeta(
            project_id="PVT_123",
            status_field_id="PVTSSF_456",
            options={"todo": "opt1", "in progress": "opt2", "done": "opt3"},
        )
        result = GitHubAdapter._resolve_board_option(TaskState.IN_PROGRESS, meta)
        assert result == "opt2"

    def test_resolve_board_option_matches_verification_for_in_review(self) -> None:
        from sova.adapters.github import _ProjectBoardMeta

        meta = _ProjectBoardMeta(
            project_id="PVT_123",
            status_field_id="PVTSSF_456",
            options={"todo": "opt1", "in progress": "opt2", "verification": "opt3", "done": "opt4"},
        )
        result = GitHubAdapter._resolve_board_option(TaskState.IN_REVIEW, meta)
        assert result == "opt3"

    def test_resolve_board_option_returns_none_for_no_match(self) -> None:
        from sova.adapters.github import _ProjectBoardMeta

        meta = _ProjectBoardMeta(
            project_id="PVT_123",
            status_field_id="PVTSSF_456",
            options={"custom column": "opt1"},
        )
        result = GitHubAdapter._resolve_board_option(TaskState.IN_PROGRESS, meta)
        assert result is None

    async def test_move_on_board_skipped_when_project_number_zero(self, mock_run: AsyncMock) -> None:
        adapter = GitHubAdapter(repo="user/repo", project_number=0)
        await adapter._move_on_board("42", TaskState.IN_PROGRESS)
        mock_run.assert_not_called()

    async def test_move_on_board_calls_graphql_mutation(self, mock_run: AsyncMock) -> None:
        from sova.adapters.github import _ProjectBoardMeta

        self.adapter._board_meta = _ProjectBoardMeta(
            project_id="PVT_123",
            status_field_id="PVTSSF_456",
            options={"in progress": "opt_ip"},
        )
        item_response = json.dumps(
            {
                "data": {
                    "repository": {
                        "issue": {"projectItems": {"nodes": [{"id": "PVTI_789", "project": {"id": "PVT_123"}}]}}
                    }
                }
            }
        )
        mock_run.side_effect = [
            _shell_result(stdout=item_response),
            _shell_result(),
        ]

        await self.adapter._move_on_board("42", TaskState.IN_PROGRESS)

        assert mock_run.call_count == 2
        mutation_call_args = mock_run.call_args[0]
        assert "api" in mutation_call_args
        assert "graphql" in mutation_call_args

    async def test_get_board_meta_parses_response(self, mock_run: AsyncMock) -> None:
        response = json.dumps(
            {
                "data": {
                    "user": {
                        "projectV2": {
                            "id": "PVT_abc",
                            "field": {
                                "id": "PVTSSF_def",
                                "options": [
                                    {"id": "opt1", "name": "Todo"},
                                    {"id": "opt2", "name": "In Progress"},
                                    {"id": "opt3", "name": "Done"},
                                ],
                            },
                        }
                    }
                }
            }
        )
        mock_run.return_value = _shell_result(stdout=response)

        meta = await self.adapter._get_board_meta()

        assert meta is not None
        assert meta.project_id == "PVT_abc"
        assert meta.status_field_id == "PVTSSF_def"
        assert meta.options == {"todo": "opt1", "in progress": "opt2", "done": "opt3"}

    async def test_get_board_meta_caches_result(self, mock_run: AsyncMock) -> None:
        from sova.adapters.github import _ProjectBoardMeta

        cached = _ProjectBoardMeta(project_id="PVT_cached", status_field_id="F_1", options={})
        self.adapter._board_meta = cached

        result = await self.adapter._get_board_meta()

        assert result is cached
        mock_run.assert_not_called()

    async def test_transition_state_calls_move_on_board(self, mock_run: AsyncMock) -> None:
        mock_run.side_effect = [
            _shell_result(stdout='{"labels": []}'),
            _shell_result(),
        ]

        with patch.object(self.adapter, "_move_on_board", new_callable=AsyncMock) as mock_move:
            await self.adapter.transition_state("42", TaskState.IN_PROGRESS)
            mock_move.assert_called_once_with("42", TaskState.IN_PROGRESS)

    async def test_transition_state_done_calls_move_on_board(self, mock_run: AsyncMock) -> None:
        mock_run.return_value = _shell_result()

        with patch.object(self.adapter, "_move_on_board", new_callable=AsyncMock) as mock_move:
            await self.adapter.transition_state("42", TaskState.DONE)
            mock_move.assert_called_once_with("42", TaskState.DONE)
