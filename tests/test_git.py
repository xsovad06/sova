"""Tests for SOVA git operations module."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sova.git.operations import (
    CheckConclusion,
    CheckStatus,
    CICheck,
    PRInfo,
    PRStatus,
    assign_pr,
    commit,
    create_branch,
    create_pr,
    find_pr_for_issue,
    get_ci_checks,
    get_current_branch,
    get_pr_diff,
    get_pr_files,
    get_pr_status,
    push,
    rebase,
    sync_branch,
)
from sova.git.worktree import (
    WorktreeInfo,
    _copy_worktree_files,
    _ensure_compose_project_name,
    cleanup_worktree,
    create_worktree,
    list_worktrees,
)
from sova.utils.shell import ShellResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _shell_ok(stdout: str = "", stderr: str = "") -> ShellResult:
    return ShellResult(returncode=0, stdout=stdout, stderr=stderr)


def _shell_fail(stderr: str = "error", returncode: int = 1) -> ShellResult:
    return ShellResult(returncode=returncode, stdout="", stderr=stderr)


# ---------------------------------------------------------------------------
# Git Operations -- branch management
# ---------------------------------------------------------------------------


class TestGetCurrentBranch:
    async def test_returns_branch_name(self) -> None:
        with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout="feat/my-feature\n")

            branch = await get_current_branch(cwd=Path("/repo"))

            assert branch == "feat/my-feature"
            mock_run.assert_called_once()

    async def test_strips_whitespace(self) -> None:
        with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout="  main  \n")

            branch = await get_current_branch()
            assert branch == "main"

    async def test_raises_on_failure(self) -> None:
        with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_fail(stderr="not a git repo")

            with pytest.raises(RuntimeError, match="Failed to get current branch"):
                await get_current_branch()


class TestCreateBranch:
    async def test_creates_branch_from_base(self) -> None:
        with patch("sova.git.operations.run_checked", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok()

            await create_branch("feat/login", base="main", cwd=Path("/repo"))

            calls = [c[0] for c in mock_run.call_args_list]
            # Should checkout base first, then create new branch
            assert any("main" in args for args in calls)
            assert any("feat/login" in args for args in calls)

    async def test_creates_branch_default_cwd(self) -> None:
        with patch("sova.git.operations.run_checked", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok()

            await create_branch("fix/bug", base="main")
            assert mock_run.called


class TestSyncBranch:
    async def test_fetches_and_pulls(self) -> None:
        with patch("sova.git.operations.run_checked", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok()

            await sync_branch("main", cwd=Path("/repo"))

            calls = [c[0] for c in mock_run.call_args_list]
            assert any("fetch" in args for args in calls)


class TestRebase:
    async def test_rebases_onto_base(self) -> None:
        with patch("sova.git.operations.run_checked", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok()

            await rebase("main", cwd=Path("/repo"))

            call_args = mock_run.call_args[0]
            assert "rebase" in call_args
            assert "main" in call_args


# ---------------------------------------------------------------------------
# Git Operations -- commit and push
# ---------------------------------------------------------------------------


class TestCommit:
    async def test_commits_with_message(self) -> None:
        with patch("sova.git.operations.run_checked", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok()
            with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run_soft:
                mock_run_soft.return_value = _shell_ok(stdout="")

                await commit("feat: add login page", cwd=Path("/repo"))

            calls = [c[0] for c in mock_run.call_args_list]
            commit_call = [args for args in calls if "commit" in args]
            assert len(commit_call) >= 1
            assert "feat: add login page" in commit_call[0]

    async def test_commits_specific_files(self) -> None:
        with patch("sova.git.operations.run_checked", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok()
            with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run_soft:
                mock_run_soft.return_value = _shell_ok(stdout="src/app.py\ntests/test_app.py\n")

                await commit("fix: typo", files=["src/app.py", "tests/test_app.py"], cwd=Path("/repo"))

            calls = [c[0] for c in mock_run.call_args_list]
            add_call = [args for args in calls if "add" in args]
            assert len(add_call) >= 1
            assert "src/app.py" in add_call[0]

    async def test_commits_all_when_no_files(self) -> None:
        with patch("sova.git.operations.run_checked", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok()
            with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run_soft:
                mock_run_soft.return_value = _shell_ok(stdout="")

                await commit("chore: update", cwd=Path("/repo"))

            calls = [c[0] for c in mock_run.call_args_list]
            add_call = [args for args in calls if "add" in args]
            assert len(add_call) >= 1
            # Should use -A when no specific files
            assert "-A" in add_call[0]

    async def test_warns_on_suspicious_staged_files(self) -> None:
        with patch("sova.git.operations.run_checked", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok()
            with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run_soft:
                mock_run_soft.return_value = _shell_ok(stdout=".venv\n.env\napp.py\n")

                with pytest.raises(RuntimeError, match="Refusing to commit suspicious files"):
                    await commit("chore: update", cwd=Path("/repo"))

    async def test_catches_suspicious_files_in_subdirectories(self) -> None:
        with patch("sova.git.operations.run_checked", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok()
            with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run_soft:
                mock_run_soft.return_value = _shell_ok(stdout="src/.env\nvendor/credentials.json\napp.py\n")

                with pytest.raises(RuntimeError, match="Refusing to commit suspicious files"):
                    await commit("chore: update", cwd=Path("/repo"))

    async def test_no_error_on_clean_staged_files(self) -> None:
        with patch("sova.git.operations.run_checked", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok()
            with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run_soft:
                mock_run_soft.return_value = _shell_ok(stdout="src/app.py\ntests/test.py\n")

                await commit("feat: add app", cwd=Path("/repo"))

            calls = [c[0] for c in mock_run.call_args_list]
            commit_call = [args for args in calls if "commit" in args]
            assert len(commit_call) >= 1


class TestPush:
    async def test_pushes_branch(self) -> None:
        with patch("sova.git.operations.run_checked", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok()

            await push("feat/login", cwd=Path("/repo"))

            call_args = mock_run.call_args[0]
            assert "push" in call_args
            assert "feat/login" in call_args

    async def test_pushes_with_force(self) -> None:
        with patch("sova.git.operations.run_checked", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok()

            await push("feat/login", force=True, cwd=Path("/repo"))

            call_args = mock_run.call_args[0]
            assert "--force-with-lease" in call_args

    async def test_pushes_with_set_upstream(self) -> None:
        with patch("sova.git.operations.run_checked", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok()

            await push("feat/login", set_upstream=True, cwd=Path("/repo"))

            call_args = mock_run.call_args[0]
            assert "-u" in call_args


# ---------------------------------------------------------------------------
# Git Operations -- PR management
# ---------------------------------------------------------------------------


class TestCreatePR:
    async def test_creates_pr_returns_info(self) -> None:
        with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout="https://github.com/user/repo/pull/42\n")

            pr = await create_pr(
                title="feat: add login",
                body="Closes #10",
                base="main",
                head="feat/login",
                repo="user/repo",
            )

            assert pr.number == 42
            assert pr.url == "https://github.com/user/repo/pull/42"

    async def test_creates_pr_raises_on_failure(self) -> None:
        with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_fail(stderr="already exists")

            with pytest.raises(RuntimeError, match="Failed to create PR"):
                await create_pr(
                    title="feat: add login",
                    body="body",
                    base="main",
                    head="feat/login",
                    repo="user/repo",
                )


class TestAssignPR:
    async def test_assigns_pr_to_user(self) -> None:
        with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok()

            await assign_pr(42, assignee="xsovad06", repo="user/repo")

            call_args = mock_run.call_args[0]
            assert "pr" in call_args
            assert "edit" in call_args
            assert "--add-assignee" in call_args
            assert "xsovad06" in call_args

    async def test_assign_pr_logs_warning_on_failure(self) -> None:
        with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_fail(stderr="not found")

            # Should not raise, just log
            await assign_pr(42, assignee="xsovad06", repo="user/repo")


class TestFindPRForIssue:
    async def test_finds_pr_by_closes_keyword(self) -> None:
        pr_json = json.dumps(
            [
                {
                    "number": 82,
                    "url": "https://github.com/user/repo/pull/82",
                    "body": "## Summary\n\nCloses #73",
                    "headRefName": "feat/issue-73",
                }
            ]
        )
        with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout=pr_json)

            result = await find_pr_for_issue("73", repo="user/repo")

            assert result is not None
            assert result.number == 82
            call_args = mock_run.call_args[0]
            assert "--search" in call_args

    async def test_finds_pr_by_branch_name(self) -> None:
        pr_json = json.dumps(
            [
                {
                    "number": 90,
                    "url": "https://github.com/user/repo/pull/90",
                    "body": "Some changes",
                    "headRefName": "feat/issue-73",
                }
            ]
        )
        with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout=pr_json)

            result = await find_pr_for_issue("73", repo="user/repo")

            assert result is not None
            assert result.number == 90

    async def test_skips_unrelated_pr(self) -> None:
        pr_json = json.dumps(
            [
                {
                    "number": 82,
                    "url": "https://github.com/user/repo/pull/82",
                    "body": "Updated 73 modules",
                    "headRefName": "feat/refactor-modules",
                }
            ]
        )
        with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout=pr_json)

            result = await find_pr_for_issue("73", repo="user/repo")

            assert result is None

    async def test_returns_none_when_no_pr_found(self) -> None:
        with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout="[]")

            result = await find_pr_for_issue("73", repo="user/repo")

            assert result is None

    async def test_returns_none_on_failure(self) -> None:
        with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_fail(stderr="error")

            result = await find_pr_for_issue("73", repo="user/repo")

            assert result is None

    async def test_returns_first_matching_pr(self) -> None:
        pr_json = json.dumps(
            [
                {
                    "number": 82,
                    "url": "https://github.com/user/repo/pull/82",
                    "body": "Fixes #73",
                    "headRefName": "feat/issue-73",
                },
                {
                    "number": 80,
                    "url": "https://github.com/user/repo/pull/80",
                    "body": "Closes #73",
                    "headRefName": "feat/issue-73-v2",
                },
            ]
        )
        with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout=pr_json)

            result = await find_pr_for_issue("73", repo="user/repo")

            assert result is not None
            assert result.number == 82


class TestGetPRStatus:
    async def test_returns_pr_status(self) -> None:
        pr_json = json.dumps(
            {
                "number": 42,
                "state": "OPEN",
                "mergeable": "MERGEABLE",
                "reviewDecision": "APPROVED",
                "url": "https://github.com/user/repo/pull/42",
                "title": "feat: add login",
            }
        )
        with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout=pr_json)

            status = await get_pr_status(42, repo="user/repo")

            assert status.number == 42
            assert status.state == "OPEN"
            assert status.mergeable == "MERGEABLE"
            assert status.review_decision == "APPROVED"

    async def test_raises_on_failure(self) -> None:
        with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_fail()

            with pytest.raises(RuntimeError, match="Failed to get PR"):
                await get_pr_status(999, repo="user/repo")


class TestGetCIChecks:
    async def test_returns_ci_checks(self) -> None:
        checks_json = json.dumps(
            [
                {
                    "name": "tests",
                    "state": "SUCCESS",
                    "link": "https://github.com/runs/1",
                },
                {
                    "name": "lint",
                    "state": "FAILURE",
                    "link": "https://github.com/runs/2",
                },
            ]
        )
        with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout=checks_json)

            checks = await get_ci_checks(42, repo="user/repo")

            assert len(checks) == 2
            assert checks[0].name == "tests"
            assert checks[0].status == CheckStatus.COMPLETED
            assert checks[0].conclusion == CheckConclusion.SUCCESS
            assert checks[1].status == CheckStatus.COMPLETED
            assert checks[1].conclusion == CheckConclusion.FAILURE

    async def test_pending_state_maps_to_in_progress(self) -> None:
        checks_json = json.dumps([{"name": "build", "state": "PENDING", "link": ""}])
        with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout=checks_json)

            checks = await get_ci_checks(42, repo="user/repo")

            assert len(checks) == 1
            assert checks[0].status == CheckStatus.IN_PROGRESS
            assert checks[0].conclusion is None

    async def test_returns_empty_on_no_checks(self) -> None:
        with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout="[]")

            checks = await get_ci_checks(42, repo="user/repo")
            assert checks == []

    async def test_returns_empty_on_failure(self) -> None:
        with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_fail()

            checks = await get_ci_checks(42, repo="user/repo")
            assert checks == []


# ---------------------------------------------------------------------------
# Worktree management
# ---------------------------------------------------------------------------


class TestCreateWorktree:
    async def test_creates_worktree(self) -> None:
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok()
            with patch("sova.git.worktree.Path.mkdir"):
                with patch("sova.git.worktree.Path.exists", return_value=False):
                    with patch("sova.git.worktree._copy_worktree_files"):
                        with patch("sova.git.worktree._ensure_compose_project_name"):
                            info = await create_worktree(
                                issue_id="42",
                                branch="feat/login",
                                base_branch="main",
                                project_dir=Path("/repo"),
                            )

                            assert isinstance(info, WorktreeInfo)
                            assert info.issue_id == "42"
                            assert info.branch == "feat/login"
                            assert ".claude/worktrees" in str(info.path)
                            mock_run.assert_called()

    async def test_worktree_path_includes_issue_id(self) -> None:
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok()
            with patch("sova.git.worktree.Path.mkdir"):
                with patch("sova.git.worktree.Path.exists", return_value=False):
                    with patch("sova.git.worktree._copy_worktree_files"):
                        with patch("sova.git.worktree._ensure_compose_project_name"):
                            info = await create_worktree(
                                issue_id="42",
                                branch="feat/login",
                                base_branch="main",
                                project_dir=Path("/repo"),
                            )

                            assert "42" in str(info.path)

    async def test_reuses_existing_branch(self) -> None:
        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.worktree.run_checked", new_callable=AsyncMock) as mock_run_checked,
        ):
            # First call (git worktree add -b) fails with "already exists"
            mock_run.return_value = _shell_fail(stderr="fatal: a branch named 'feat/login' already exists")
            mock_run_checked.return_value = _shell_ok()
            with patch("sova.git.worktree.Path.mkdir"):
                with patch("sova.git.worktree.Path.exists", return_value=False):
                    with patch("sova.git.worktree._copy_worktree_files"):
                        with patch("sova.git.worktree._ensure_compose_project_name"):
                            info = await create_worktree(
                                issue_id="42",
                                branch="feat/login",
                                base_branch="main",
                                project_dir=Path("/repo"),
                            )

                            assert isinstance(info, WorktreeInfo)
                            assert info.branch == "feat/login"
                            # run_checked should have been called for the fallback (without -b)
                            mock_run_checked.assert_called_once()


class TestCreateWorktreePathValidation:
    async def test_rejects_dotdot_in_issue_id(self) -> None:
        with pytest.raises(ValueError, match="path traversal"):
            await create_worktree(
                issue_id="../escape",
                branch="feat/x",
                base_branch="main",
                project_dir=Path("/repo"),
            )

    async def test_rejects_slash_in_issue_id(self) -> None:
        with pytest.raises(ValueError, match="path traversal"):
            await create_worktree(
                issue_id="foo/bar",
                branch="feat/x",
                base_branch="main",
                project_dir=Path("/repo"),
            )

    async def test_rejects_absolute_issue_id(self) -> None:
        with pytest.raises(ValueError, match="path traversal"):
            await create_worktree(
                issue_id="/etc/passwd",
                branch="feat/x",
                base_branch="main",
                project_dir=Path("/repo"),
            )


class TestCopyWorktreeFilesValidation:
    def test_skips_path_traversal_in_source(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        (project / "safe.txt").write_text("ok")

        _copy_worktree_files(project, worktree, ["../escape.txt", "safe.txt"])

        assert (worktree / "safe.txt").exists()
        assert not (worktree / "escape.txt").exists()

    def test_skips_path_traversal_in_dest(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        (project / "safe.txt").write_text("ok")

        _copy_worktree_files(project, worktree, ["safe.txt"])

        assert (worktree / "safe.txt").exists()


class TestCleanupWorktree:
    async def test_removes_worktree(self) -> None:
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok()

            await cleanup_worktree(Path("/repo/.claude/worktrees/42"), cwd=Path("/repo"))

            call_args = mock_run.call_args[0]
            assert "worktree" in call_args
            assert "remove" in call_args


class TestListWorktrees:
    async def test_lists_worktrees(self) -> None:
        worktree_output = "/repo 0000000 [main]\n/repo/.claude/worktrees/42 abc1234 [feat/login]\n"
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout=worktree_output)

            worktrees = await list_worktrees(cwd=Path("/repo"))

            assert len(worktrees) == 2

    async def test_returns_empty_on_failure(self) -> None:
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_fail()

            worktrees = await list_worktrees(cwd=Path("/repo"))
            assert worktrees == []


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class TestPRInfo:
    def test_pr_info_fields(self) -> None:
        pr = PRInfo(number=42, url="https://github.com/user/repo/pull/42")
        assert pr.number == 42
        assert pr.url == "https://github.com/user/repo/pull/42"


class TestPRStatus:
    def test_pr_status_fields(self) -> None:
        status = PRStatus(
            number=42,
            state="OPEN",
            mergeable="MERGEABLE",
            review_decision="APPROVED",
            url="https://github.com/user/repo/pull/42",
            title="feat: add login",
        )
        assert status.number == 42
        assert status.is_open
        assert status.is_mergeable
        assert status.is_approved

    def test_closed_pr(self) -> None:
        status = PRStatus(number=1, state="CLOSED", mergeable="", review_decision="", url="", title="")
        assert not status.is_open

    def test_not_mergeable(self) -> None:
        status = PRStatus(number=1, state="OPEN", mergeable="CONFLICTING", review_decision="", url="", title="")
        assert not status.is_mergeable

    def test_not_approved(self) -> None:
        status = PRStatus(
            number=1, state="OPEN", mergeable="MERGEABLE", review_decision="CHANGES_REQUESTED", url="", title=""
        )
        assert not status.is_approved


class TestCICheck:
    def test_check_fields(self) -> None:
        check = CICheck(
            name="tests",
            status=CheckStatus.COMPLETED,
            conclusion=CheckConclusion.SUCCESS,
            details_url="https://example.com",
        )
        assert check.name == "tests"
        assert check.is_passed

    def test_failed_check(self) -> None:
        check = CICheck(
            name="lint",
            status=CheckStatus.COMPLETED,
            conclusion=CheckConclusion.FAILURE,
            details_url="",
        )
        assert not check.is_passed

    def test_in_progress_check(self) -> None:
        check = CICheck(
            name="build",
            status=CheckStatus.IN_PROGRESS,
            conclusion=None,
            details_url="",
        )
        assert not check.is_passed
        assert not check.is_completed


class TestCheckStatus:
    def test_all_statuses(self) -> None:
        statuses = {s.value for s in CheckStatus}
        assert "COMPLETED" in statuses
        assert "IN_PROGRESS" in statuses
        assert "QUEUED" in statuses


class TestCheckConclusion:
    def test_all_conclusions(self) -> None:
        conclusions = {c.value for c in CheckConclusion}
        assert "SUCCESS" in conclusions
        assert "FAILURE" in conclusions
        assert "NEUTRAL" in conclusions


class TestWorktreeInfo:
    def test_worktree_info_fields(self) -> None:
        info = WorktreeInfo(
            path=Path("/repo/.claude/worktrees/42"),
            branch="feat/login",
            issue_id="42",
        )
        assert info.path == Path("/repo/.claude/worktrees/42")
        assert info.branch == "feat/login"
        assert info.issue_id == "42"


class TestEnsureComposeProjectName:
    """Test Docker Compose project name injection to prevent port collisions."""

    def test_injects_into_env_when_compose_has_no_name(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "my_project"
        project_dir.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        (worktree / "docker-compose.yml").write_text("services:\n  db:\n    image: postgres\n")
        (worktree / ".env").write_text("DEBUG=True\n")

        _ensure_compose_project_name(project_dir, worktree)

        env_content = (worktree / ".env").read_text()
        assert "COMPOSE_PROJECT_NAME=my_project" in env_content

    def test_skips_when_compose_already_has_name(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "my_project"
        project_dir.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        (worktree / "docker-compose.yml").write_text("name: my_project\n\nservices:\n  db:\n    image: postgres\n")
        (worktree / ".env").write_text("DEBUG=True\n")

        _ensure_compose_project_name(project_dir, worktree)

        env_content = (worktree / ".env").read_text()
        assert "COMPOSE_PROJECT_NAME" not in env_content

    def test_skips_when_no_compose_file(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "my_project"
        project_dir.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        (worktree / ".env").write_text("DEBUG=True\n")

        _ensure_compose_project_name(project_dir, worktree)

        env_content = (worktree / ".env").read_text()
        assert "COMPOSE_PROJECT_NAME" not in env_content

    def test_no_duplicate_injection(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "my_project"
        project_dir.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        (worktree / "docker-compose.yml").write_text("services:\n  db:\n    image: postgres\n")
        (worktree / ".env").write_text("COMPOSE_PROJECT_NAME=existing\n")

        _ensure_compose_project_name(project_dir, worktree)

        env_content = (worktree / ".env").read_text()
        assert env_content.count("COMPOSE_PROJECT_NAME") == 1

    def test_normalizes_project_name(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "My-Project"
        project_dir.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        (worktree / "docker-compose.yml").write_text("services:\n  db:\n    image: postgres\n")
        (worktree / ".env").write_text("")

        _ensure_compose_project_name(project_dir, worktree)

        env_content = (worktree / ".env").read_text()
        assert "COMPOSE_PROJECT_NAME=my_project" in env_content


# ---------------------------------------------------------------------------
# PR diff and files
# ---------------------------------------------------------------------------

SAMPLE_DIFF = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,4 @@
+import os
 def hello():
     pass
"""


class TestGetPrDiff:
    async def test_returns_diff(self) -> None:
        with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout=SAMPLE_DIFF)
            result = await get_pr_diff(42, repo="user/repo", github_user="test")

        assert "diff --git" in result
        mock_run.assert_called_once()
        args = mock_run.call_args[0]
        assert "diff" in args
        assert "42" in args

    async def test_raises_on_failure(self) -> None:
        with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_fail(stderr="not found")
            with pytest.raises(RuntimeError, match="Failed to get diff"):
                await get_pr_diff(42, repo="user/repo")


class TestGetPrFiles:
    async def test_returns_file_list(self) -> None:
        with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout="foo.py\nbar.py\n")
            result = await get_pr_files(42, repo="user/repo")

        assert result == ["foo.py", "bar.py"]

    async def test_filters_empty_lines(self) -> None:
        with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout="foo.py\n\nbar.py\n\n")
            result = await get_pr_files(42, repo="user/repo")

        assert result == ["foo.py", "bar.py"]

    async def test_raises_on_failure(self) -> None:
        with patch("sova.git.operations.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_fail(stderr="not found")
            with pytest.raises(RuntimeError, match="Failed to get files"):
                await get_pr_files(42, repo="user/repo")
