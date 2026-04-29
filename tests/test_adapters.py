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

        adapter = create_adapter(adapter_type="github", repo="user/repo")
        assert isinstance(adapter, GitHubAdapter)
        assert adapter.project_number == 0

    def test_create_github_adapter_with_user(self) -> None:
        from sova.adapters import create_adapter

        adapter = create_adapter(adapter_type="github", repo="user/repo", github_user="xsovad06")
        assert isinstance(adapter, GitHubAdapter)
        assert adapter.github_user == "xsovad06"

    def test_create_github_adapter_with_project_number(self) -> None:
        from sova.adapters import create_adapter

        adapter = create_adapter(adapter_type="github", repo="user/repo", project_number=1)
        assert isinstance(adapter, GitHubAdapter)
        assert adapter.project_number == 1

    def test_create_unknown_adapter_raises(self) -> None:
        from sova.adapters import create_adapter

        with pytest.raises(ValueError, match="Unknown adapter type"):
            create_adapter(adapter_type="unknown", repo="user/repo")


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
