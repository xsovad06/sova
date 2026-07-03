"""Tests for SOVA git operations module."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sova.git.diff import parse_diff_lines
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
    get_ci_failure_logs,
    get_current_branch,
    get_pr_diff,
    get_pr_files,
    get_pr_status,
    push,
    rebase,
    sync_branch,
)
from sova.git.pr import _parse_run_id
from sova.git.worktree import (
    WorktreeInfo,
    _check_worktree_active_agent,
    _copy_claude_artifacts,
    _copy_worktree_files,
    _ensure_compose_project_name,
    cleanup_worktree,
    create_worktree,
    find_worktree_by_branch,
    list_worktrees,
    resolve_worktree_conflict,
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
        with patch("sova.git.branch.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout="feat/my-feature\n")

            branch = await get_current_branch(cwd=Path("/repo"))

            assert branch == "feat/my-feature"
            mock_run.assert_called_once()

    async def test_strips_whitespace(self) -> None:
        with patch("sova.git.branch.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout="  main  \n")

            branch = await get_current_branch()
            assert branch == "main"

    async def test_raises_on_failure(self) -> None:
        with patch("sova.git.branch.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_fail(stderr="not a git repo")

            with pytest.raises(RuntimeError, match="Failed to get current branch"):
                await get_current_branch()


class TestCreateBranch:
    async def test_creates_branch_from_base(self) -> None:
        with patch("sova.git.branch.run_checked", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok()

            await create_branch("feat/login", base="main", cwd=Path("/repo"))

            calls = [c[0] for c in mock_run.call_args_list]
            # Should checkout base first, then create new branch
            assert any("main" in args for args in calls)
            assert any("feat/login" in args for args in calls)

    async def test_creates_branch_default_cwd(self) -> None:
        with patch("sova.git.branch.run_checked", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok()

            await create_branch("fix/bug", base="main")
            assert mock_run.called


class TestSyncBranch:
    async def test_fetches_and_pulls(self) -> None:
        with (
            patch("sova.git.branch.run_checked", new_callable=AsyncMock) as mock_checked,
            patch("sova.git.branch.run", new_callable=AsyncMock) as mock_run,
        ):
            mock_checked.return_value = _shell_ok()
            mock_run.return_value = _shell_ok()  # checkout

            await sync_branch("main", cwd=Path("/repo"))

            checked_calls = [c[0] for c in mock_checked.call_args_list]
            assert any("fetch" in args for args in checked_calls)


class TestRebase:
    async def test_rebases_onto_base(self) -> None:
        with patch("sova.git.branch.run_checked", new_callable=AsyncMock) as mock_run:
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
        with patch("sova.git.branch.run_checked", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok()
            with patch("sova.git.branch.run", new_callable=AsyncMock) as mock_run_soft:
                mock_run_soft.return_value = _shell_ok(stdout="")

                await commit("feat: add login page", cwd=Path("/repo"))

            calls = [c[0] for c in mock_run.call_args_list]
            commit_call = [args for args in calls if "commit" in args]
            assert len(commit_call) >= 1
            assert "feat: add login page" in commit_call[0]

    async def test_commits_specific_files(self) -> None:
        with patch("sova.git.branch.run_checked", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok()
            with patch("sova.git.branch.run", new_callable=AsyncMock) as mock_run_soft:
                mock_run_soft.return_value = _shell_ok(stdout="src/app.py\ntests/test_app.py\n")

                await commit("fix: typo", files=["src/app.py", "tests/test_app.py"], cwd=Path("/repo"))

            calls = [c[0] for c in mock_run.call_args_list]
            add_call = [args for args in calls if "add" in args]
            assert len(add_call) >= 1
            assert "src/app.py" in add_call[0]

    async def test_commits_all_when_no_files(self) -> None:
        with patch("sova.git.branch.run_checked", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok()
            with patch("sova.git.branch.run", new_callable=AsyncMock) as mock_run_soft:
                mock_run_soft.return_value = _shell_ok(stdout="")

                await commit("chore: update", cwd=Path("/repo"))

            calls = [c[0] for c in mock_run.call_args_list]
            add_call = [args for args in calls if "add" in args]
            assert len(add_call) >= 1
            # Should use -A when no specific files
            assert "-A" in add_call[0]

    async def test_warns_on_suspicious_staged_files(self) -> None:
        with patch("sova.git.branch.run_checked", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok()
            with patch("sova.git.branch.run", new_callable=AsyncMock) as mock_run_soft:
                mock_run_soft.return_value = _shell_ok(stdout=".venv\n.env\napp.py\n")

                with pytest.raises(RuntimeError, match="Refusing to commit suspicious files"):
                    await commit("chore: update", cwd=Path("/repo"))

    async def test_catches_suspicious_files_in_subdirectories(self) -> None:
        with patch("sova.git.branch.run_checked", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok()
            with patch("sova.git.branch.run", new_callable=AsyncMock) as mock_run_soft:
                mock_run_soft.return_value = _shell_ok(stdout="src/.env\nvendor/credentials.json\napp.py\n")

                with pytest.raises(RuntimeError, match="Refusing to commit suspicious files"):
                    await commit("chore: update", cwd=Path("/repo"))

    async def test_no_error_on_clean_staged_files(self) -> None:
        with patch("sova.git.branch.run_checked", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok()
            with patch("sova.git.branch.run", new_callable=AsyncMock) as mock_run_soft:
                mock_run_soft.return_value = _shell_ok(stdout="src/app.py\ntests/test.py\n")

                await commit("feat: add app", cwd=Path("/repo"))

            calls = [c[0] for c in mock_run.call_args_list]
            commit_call = [args for args in calls if "commit" in args]
            assert len(commit_call) >= 1


class TestPush:
    async def test_pushes_branch(self) -> None:
        with patch("sova.git.branch.run_checked", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok()

            await push("feat/login", cwd=Path("/repo"))

            call_args = mock_run.call_args[0]
            assert "push" in call_args
            assert "feat/login" in call_args

    async def test_pushes_with_force(self) -> None:
        with patch("sova.git.branch.run_checked", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok()

            await push("feat/login", force=True, cwd=Path("/repo"))

            call_args = mock_run.call_args[0]
            assert "--force-with-lease" in call_args

    async def test_pushes_with_set_upstream(self) -> None:
        with patch("sova.git.branch.run_checked", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok()

            await push("feat/login", set_upstream=True, cwd=Path("/repo"))

            call_args = mock_run.call_args[0]
            assert "-u" in call_args


# ---------------------------------------------------------------------------
# Git Operations -- PR management
# ---------------------------------------------------------------------------


class TestCreatePR:
    async def test_creates_pr_returns_info(self) -> None:
        with patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run:
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
        with patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run:
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
        with patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok()

            await assign_pr(42, assignee="xsovad06", repo="user/repo")

            call_args = mock_run.call_args[0]
            assert "pr" in call_args
            assert "edit" in call_args
            assert "--add-assignee" in call_args
            assert "xsovad06" in call_args

    async def test_assign_pr_logs_warning_on_failure(self) -> None:
        with patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run:
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
        with patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run:
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
        with patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run:
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
        with patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout=pr_json)

            result = await find_pr_for_issue("73", repo="user/repo")

            assert result is None

    async def test_returns_none_when_no_pr_found(self) -> None:
        with patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout="[]")

            result = await find_pr_for_issue("73", repo="user/repo")

            assert result is None

    async def test_returns_none_on_failure(self) -> None:
        with patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run:
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
        with patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout=pr_json)

            result = await find_pr_for_issue("73", repo="user/repo")

            assert result is not None
            assert result.number == 82

    async def test_find_pr_populates_branch_from_head_ref(self) -> None:
        pr_json = json.dumps(
            [
                {
                    "number": 82,
                    "url": "https://github.com/user/repo/pull/82",
                    "body": "Closes #73",
                    "headRefName": "feat/issue-73",
                }
            ]
        )
        with patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout=pr_json)

            result = await find_pr_for_issue("73", repo="user/repo")

            assert result is not None
            assert result.branch == "feat/issue-73"


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
        with patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout=pr_json)

            status = await get_pr_status(42, repo="user/repo")

            assert status.number == 42
            assert status.state == "OPEN"
            assert status.mergeable == "MERGEABLE"
            assert status.review_decision == "APPROVED"

    async def test_raises_on_failure(self) -> None:
        with patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run:
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
        with patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run:
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
        with patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout=checks_json)

            checks = await get_ci_checks(42, repo="user/repo")

            assert len(checks) == 1
            assert checks[0].status == CheckStatus.IN_PROGRESS
            assert checks[0].conclusion is None

    async def test_returns_empty_on_no_checks(self) -> None:
        with patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout="[]")

            checks = await get_ci_checks(42, repo="user/repo")
            assert checks == []

    async def test_returns_none_on_failure(self) -> None:
        with patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_fail()

            checks = await get_ci_checks(42, repo="user/repo")
            assert checks is None

    async def test_returns_none_on_bad_json(self) -> None:
        with patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout="not json")

            checks = await get_ci_checks(42, repo="user/repo")
            assert checks is None

    async def test_skipped_state_maps_to_completed(self) -> None:
        checks_json = json.dumps([{"name": "Fork Gate", "state": "SKIPPED", "link": ""}])
        with patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout=checks_json)

            checks = await get_ci_checks(42, repo="user/repo")

            assert len(checks) == 1
            assert checks[0].status == CheckStatus.COMPLETED
            assert checks[0].conclusion == CheckConclusion.SKIPPED
            assert checks[0].is_completed
            assert not checks[0].is_passed
            assert not checks[0].is_failed

    async def test_timed_out_state_maps_to_failed(self) -> None:
        checks_json = json.dumps([{"name": "Slow Test", "state": "TIMED_OUT", "link": ""}])
        with patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout=checks_json)

            checks = await get_ci_checks(42, repo="user/repo")

            assert len(checks) == 1
            assert checks[0].status == CheckStatus.COMPLETED
            assert checks[0].conclusion == CheckConclusion.TIMED_OUT
            assert checks[0].is_completed
            assert not checks[0].is_passed
            assert checks[0].is_failed

    async def test_stale_state_maps_to_neutral(self) -> None:
        checks_json = json.dumps([{"name": "Old Check", "state": "STALE", "link": ""}])
        with patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout=checks_json)

            checks = await get_ci_checks(42, repo="user/repo")

            assert len(checks) == 1
            assert checks[0].status == CheckStatus.COMPLETED
            assert checks[0].conclusion == CheckConclusion.NEUTRAL
            assert not checks[0].is_passed
            assert not checks[0].is_failed

    async def test_unknown_state_logged_and_defaults_to_in_progress(self) -> None:
        checks_json = json.dumps([{"name": "Mystery", "state": "WEIRD_STATE", "link": ""}])
        with (
            patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.pr.log.warning") as mock_warning,
        ):
            mock_run.return_value = _shell_ok(stdout=checks_json)

            checks = await get_ci_checks(42, repo="user/repo")

            assert len(checks) == 1
            assert checks[0].status == CheckStatus.IN_PROGRESS
            assert checks[0].conclusion is None
            mock_warning.assert_called_once_with(
                "git.ci_checks.unknown_state",
                check="Mystery",
                state="WEIRD_STATE",
            )


# ---------------------------------------------------------------------------
# CI failure log fetching
# ---------------------------------------------------------------------------


class TestParseRunId:
    def test_extracts_run_id_from_actions_url(self) -> None:
        url = "https://github.com/owner/repo/actions/runs/123456/job/789"
        assert _parse_run_id(url) == "123456"

    def test_returns_none_for_non_actions_url(self) -> None:
        assert _parse_run_id("https://example.com/status") is None

    def test_returns_none_for_empty_url(self) -> None:
        assert _parse_run_id("") is None

    def test_handles_url_with_runs_at_end(self) -> None:
        url = "https://github.com/o/r/actions/runs"
        assert _parse_run_id(url) is None

    def test_strips_query_params_from_run_id(self) -> None:
        url = "https://github.com/o/r/actions/runs/12345?check_suite_focus=true"
        assert _parse_run_id(url) == "12345"

    def test_rejects_non_digit_run_id(self) -> None:
        url = "https://github.com/o/r/actions/runs/not-a-number/job/1"
        assert _parse_run_id(url) is None


class TestGetCIFailureLogs:
    async def test_fetches_logs_for_failed_checks(self) -> None:
        checks = [
            CICheck(
                name="Tests",
                status=CheckStatus.COMPLETED,
                conclusion=CheckConclusion.FAILURE,
                details_url="https://github.com/o/r/actions/runs/111/job/1",
            ),
            CICheck(
                name="Lint",
                status=CheckStatus.COMPLETED,
                conclusion=CheckConclusion.FAILURE,
                details_url="https://github.com/o/r/actions/runs/222/job/2",
            ),
        ]
        with (
            patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run,
            patch("sova.utils.gh.run", new_callable=AsyncMock, return_value=_shell_ok()),
        ):
            mock_run.side_effect = [
                _shell_ok(stdout="ERROR: test failed"),
                _shell_ok(stdout="WARNING: unused import"),
            ]
            result = await get_ci_failure_logs(checks, repo="o/r")
        assert "=== Tests (run 111) ===" in result
        assert "ERROR: test failed" in result
        assert "=== Lint (run 222) ===" in result
        assert mock_run.call_count == 2

    async def test_deduplicates_by_run_id(self) -> None:
        checks = [
            CICheck(
                name="A",
                status=CheckStatus.COMPLETED,
                conclusion=CheckConclusion.FAILURE,
                details_url="https://github.com/o/r/actions/runs/111/job/1",
            ),
            CICheck(
                name="B",
                status=CheckStatus.COMPLETED,
                conclusion=CheckConclusion.FAILURE,
                details_url="https://github.com/o/r/actions/runs/111/job/2",
            ),
        ]
        with (
            patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run,
            patch("sova.utils.gh.run", new_callable=AsyncMock, return_value=_shell_ok()),
        ):
            mock_run.return_value = _shell_ok(stdout="log output")
            await get_ci_failure_logs(checks, repo="o/r")
        assert mock_run.call_count == 1

    async def test_skips_checks_without_run_id(self) -> None:
        checks = [
            CICheck(
                name="X",
                status=CheckStatus.COMPLETED,
                conclusion=CheckConclusion.FAILURE,
                details_url="https://example.com/no-run-id",
            ),
        ]
        with (
            patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run,
            patch("sova.utils.gh.run", new_callable=AsyncMock, return_value=_shell_ok()),
        ):
            result = await get_ci_failure_logs(checks, repo="o/r")
        assert result == ""
        mock_run.assert_not_awaited()

    async def test_respects_max_log_chars(self) -> None:
        checks = [
            CICheck(
                name="T",
                status=CheckStatus.COMPLETED,
                conclusion=CheckConclusion.FAILURE,
                details_url="https://github.com/o/r/actions/runs/1/job/1",
            ),
        ]
        with (
            patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run,
            patch("sova.utils.gh.run", new_callable=AsyncMock, return_value=_shell_ok()),
        ):
            mock_run.return_value = _shell_ok(stdout="x" * 10000)
            result = await get_ci_failure_logs(checks, repo="o/r", max_log_chars=200)
        assert len(result) <= 200

    async def test_handles_fetch_failure_gracefully(self) -> None:
        checks = [
            CICheck(
                name="T",
                status=CheckStatus.COMPLETED,
                conclusion=CheckConclusion.FAILURE,
                details_url="https://github.com/o/r/actions/runs/1/job/1",
            ),
        ]
        with (
            patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run,
            patch("sova.utils.gh.run", new_callable=AsyncMock, return_value=_shell_ok()),
        ):
            mock_run.return_value = _shell_fail()
            result = await get_ci_failure_logs(checks, repo="o/r")
        assert result == ""


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
                        with patch("sova.git.worktree._copy_claude_artifacts"):
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
                        with patch("sova.git.worktree._copy_claude_artifacts"):
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
                        with patch("sova.git.worktree._copy_claude_artifacts"):
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


class TestCopyClaudeArtifacts:
    def test_copies_commands_and_rules(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        claude = project / ".claude"
        (claude / "commands").mkdir(parents=True)
        (claude / "commands" / "develop.md").write_text("# dev")
        (claude / "rules").mkdir()
        (claude / "rules" / "arch.md").write_text("# arch")

        _copy_claude_artifacts(project, worktree)

        assert (worktree / ".claude" / "commands" / "develop.md").read_text() == "# dev"
        assert (worktree / ".claude" / "rules" / "arch.md").read_text() == "# arch"

    def test_copies_claude_files(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        claude = project / ".claude"
        claude.mkdir(parents=True)
        (claude / "CLAUDE.md").write_text("project instructions")
        (claude / "settings.local.json").write_text("{}")

        _copy_claude_artifacts(project, worktree)

        assert (worktree / ".claude" / "CLAUDE.md").read_text() == "project instructions"
        assert (worktree / ".claude" / "settings.local.json").read_text() == "{}"

    def test_copies_root_claude_md(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        (project / "CLAUDE.md").write_text("root instructions")

        _copy_claude_artifacts(project, worktree)

        assert (worktree / "CLAUDE.md").read_text() == "root instructions"

    def test_noop_when_no_claude_dir(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        _copy_claude_artifacts(project, worktree)

        assert not (worktree / ".claude").exists()

    def test_skips_missing_subdirs(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        claude = project / ".claude"
        (claude / "commands").mkdir(parents=True)
        (claude / "commands" / "test.md").write_text("# test")

        _copy_claude_artifacts(project, worktree)

        assert (worktree / ".claude" / "commands" / "test.md").exists()
        assert not (worktree / ".claude" / "rules").exists()
        assert not (worktree / ".claude" / "agent-memory").exists()


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


class TestFindWorktreeByBranch:
    _PORCELAIN = (
        "worktree /repo\n"
        "HEAD abc1234\n"
        "branch refs/heads/main\n"
        "\n"
        "worktree /repo/.claude/worktrees/42\n"
        "HEAD def5678\n"
        "branch refs/heads/feat/login\n"
        "\n"
    )

    async def test_finds_existing_branch(self) -> None:
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout=self._PORCELAIN)
            result = await find_worktree_by_branch("feat/login", cwd=Path("/repo"))
            assert result == Path("/repo/.claude/worktrees/42")

    async def test_finds_main_branch(self) -> None:
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout=self._PORCELAIN)
            result = await find_worktree_by_branch("main", cwd=Path("/repo"))
            assert result == Path("/repo")

    async def test_returns_none_for_missing_branch(self) -> None:
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout=self._PORCELAIN)
            result = await find_worktree_by_branch("feat/nonexistent", cwd=Path("/repo"))
            assert result is None

    async def test_returns_none_on_git_failure(self) -> None:
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_fail()
            result = await find_worktree_by_branch("main", cwd=Path("/repo"))
            assert result is None

    async def test_handles_detached_head(self) -> None:
        porcelain = "worktree /repo/.claude/worktrees/99\nHEAD abc1234\ndetached\n\n"
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout=porcelain)
            result = await find_worktree_by_branch("feat/something", cwd=Path("/repo"))
            assert result is None

    @pytest.mark.parametrize(
        "branch,expected_path",
        [
            ("feat/FOO-123/add-feature", "/repo/.claude/worktrees/1"),
            ("user/name/WIP", "/repo/.claude/worktrees/2"),
            ("release/v1.0", "/repo/.claude/worktrees/3"),
        ],
    )
    async def test_special_branch_names(self, branch: str, expected_path: str) -> None:
        porcelain = f"worktree {expected_path}\nHEAD abc1234\nbranch refs/heads/{branch}\n\n"
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout=porcelain)
            result = await find_worktree_by_branch(branch, cwd=Path("/repo"))
            assert result == Path(expected_path)


class TestResolveWorktreeConflict:
    async def test_no_conflict(self) -> None:
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            # prune succeeds, porcelain shows no match
            mock_run.side_effect = [
                _shell_ok(),  # git worktree prune
                _shell_ok(stdout="worktree /repo\nHEAD abc\nbranch refs/heads/main\n\n"),
            ]
            result = await resolve_worktree_conflict("feat/login", cwd=Path("/repo"))
            assert result is None

    async def test_removes_stale_worktree(self) -> None:
        wt_path = Path("/repo/.claude/worktrees/42")
        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run,
            patch.object(Path, "exists", return_value=True),
            patch("sova.git.worktree._check_worktree_active_agent", new_callable=AsyncMock) as mock_check,
        ):
            porcelain = "worktree /repo/.claude/worktrees/42\nHEAD def5678\nbranch refs/heads/feat/login\n\n"
            mock_run.side_effect = [
                _shell_ok(),  # git worktree prune
                _shell_ok(stdout=porcelain),  # git worktree list --porcelain
                _shell_ok(stdout="/repo\n"),  # git rev-parse --show-toplevel
                _shell_ok(),  # git worktree remove --force
            ]
            mock_check.return_value = None  # no active agent
            result = await resolve_worktree_conflict("feat/login", cwd=Path("/repo"))
            assert result == wt_path
            mock_check.assert_awaited_once()

    async def test_raises_on_main_worktree(self) -> None:
        """Raise RuntimeError for main worktree conflicts -- cannot auto-resolve."""
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            porcelain = "worktree /repo\nHEAD abc1234\nbranch refs/heads/main\n\n"
            mock_run.side_effect = [
                _shell_ok(),  # git worktree prune
                _shell_ok(stdout=porcelain),  # git worktree list --porcelain
                _shell_ok(stdout="/repo\n"),  # git rev-parse --show-toplevel
            ]
            with pytest.raises(RuntimeError, match="main worktree"):
                await resolve_worktree_conflict("main", cwd=Path("/repo"))
            # Must NOT have called cleanup_worktree (no remove call)
            remove_calls = [c for c in mock_run.call_args_list if "remove" in c[0]]
            assert remove_calls == []

    async def test_raises_on_toplevel_failure(self) -> None:
        """Fail-closed: if rev-parse --show-toplevel fails, refuse to remove."""
        porcelain = "worktree /repo/.claude/worktrees/42\nHEAD def5678\nbranch refs/heads/feat/login\n\n"
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                _shell_ok(),  # git worktree prune
                _shell_ok(stdout=porcelain),  # git worktree list --porcelain
                _shell_fail(stderr="fatal: not a git repository"),  # git rev-parse --show-toplevel
            ]
            with pytest.raises(RuntimeError, match="Cannot verify repository root"):
                await resolve_worktree_conflict("feat/login", cwd=Path("/repo"))
            # Must NOT have called cleanup_worktree (no remove call)
            remove_calls = [c for c in mock_run.call_args_list if "remove" in str(c)]
            assert remove_calls == []

    async def test_handles_stale_ref_no_directory(self) -> None:
        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run,
            patch.object(Path, "exists", return_value=False),
        ):
            porcelain = "worktree /repo/.claude/worktrees/42\nHEAD def5678\nbranch refs/heads/feat/login\n\n"
            mock_run.side_effect = [
                _shell_ok(),  # git worktree prune
                _shell_ok(stdout=porcelain),  # git worktree list --porcelain
                _shell_ok(stdout="/repo\n"),  # git rev-parse --show-toplevel
            ]
            result = await resolve_worktree_conflict("feat/login", cwd=Path("/repo"))
            assert result == Path("/repo/.claude/worktrees/42")

    async def test_refuses_removal_when_agent_active(self) -> None:
        """Do not remove a worktree that an agent with a live PID is using."""
        import os

        porcelain = "worktree /repo/.claude/worktrees/42\nHEAD def5678\nbranch refs/heads/feat/login\n\n"

        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run,
            patch.object(Path, "exists", return_value=True),
            patch("sova.git.worktree._check_worktree_active_agent", new_callable=AsyncMock) as mock_check,
        ):
            mock_run.side_effect = [
                _shell_ok(),  # git worktree prune
                _shell_ok(stdout=porcelain),  # git worktree list --porcelain
                _shell_ok(stdout="/repo\n"),  # git rev-parse --show-toplevel
            ]
            mock_check.return_value = os.getpid()  # simulate live agent

            with pytest.raises(RuntimeError, match="actively using"):
                await resolve_worktree_conflict("feat/login", cwd=Path("/repo"))

            # cleanup_worktree must NOT have been called
            remove_calls = [c for c in mock_run.call_args_list if "remove" in c[0]]
            assert remove_calls == []

    async def test_raises_on_db_failure(self) -> None:
        """Fail-closed: DB query failure blocks worktree removal."""
        porcelain = "worktree /repo/.claude/worktrees/42\nHEAD def5678\nbranch refs/heads/feat/login\n\n"

        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run,
            patch.object(Path, "exists", return_value=True),
            patch(
                "sova.git.worktree._check_worktree_active_agent",
                new_callable=AsyncMock,
                side_effect=RuntimeError("DB query failed"),
            ),
        ):
            mock_run.side_effect = [
                _shell_ok(),  # git worktree prune
                _shell_ok(stdout=porcelain),  # git worktree list --porcelain
                _shell_ok(stdout="/repo\n"),  # git rev-parse --show-toplevel
            ]

            with pytest.raises(RuntimeError, match="DB query failed"):
                await resolve_worktree_conflict("feat/login", cwd=Path("/repo"))

            # cleanup_worktree must NOT have been called
            remove_calls = [c for c in mock_run.call_args_list if "remove" in c[0]]
            assert remove_calls == []


class TestCheckWorktreeActiveAgent:
    """Tests for _check_worktree_active_agent()."""

    @staticmethod
    def _make_session_mock(runs: list) -> AsyncMock:
        """Create a mock get_session that returns runs from execute()."""
        from unittest.mock import MagicMock

        mock_session = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = runs
        mock_exec_result = MagicMock()
        mock_exec_result.scalars.return_value = mock_scalars
        mock_session.execute = AsyncMock(return_value=mock_exec_result)

        # get_session is async, returns async context manager
        mock_get_session = AsyncMock()
        mock_get_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_get_session.return_value.__aexit__ = AsyncMock(return_value=False)
        return mock_get_session

    @staticmethod
    def _make_run_record(worktree_path: str, pid: int) -> object:
        from unittest.mock import MagicMock

        record = MagicMock()
        record.worktree_path = worktree_path
        record.pid = pid
        return record

    async def test_returns_none_when_no_matching_runs(self) -> None:
        mock_gs = self._make_session_mock([])
        with patch("sova.db.session.get_session", mock_gs):
            result = await _check_worktree_active_agent(Path("/repo/.claude/worktrees/42"))
        assert result is None

    async def test_returns_pid_when_active_agent_found(self) -> None:
        record = self._make_run_record("/repo/.claude/worktrees/42", 99999)
        mock_gs = self._make_session_mock([record])
        with (
            patch("sova.db.session.get_session", mock_gs),
            patch("os.kill") as mock_kill,
        ):
            mock_kill.return_value = None
            result = await _check_worktree_active_agent(Path("/repo/.claude/worktrees/42"))
        assert result == 99999

    async def test_skips_dead_pid(self) -> None:
        record = self._make_run_record("/repo/.claude/worktrees/42", 99999)
        mock_gs = self._make_session_mock([record])
        with (
            patch("sova.db.session.get_session", mock_gs),
            patch("os.kill", side_effect=ProcessLookupError),
        ):
            result = await _check_worktree_active_agent(Path("/repo/.claude/worktrees/42"))
        assert result is None

    async def test_returns_pid_on_permission_error(self) -> None:
        record = self._make_run_record("/repo/.claude/worktrees/42", 99999)
        mock_gs = self._make_session_mock([record])
        with (
            patch("sova.db.session.get_session", mock_gs),
            patch("os.kill", side_effect=PermissionError),
        ):
            result = await _check_worktree_active_agent(Path("/repo/.claude/worktrees/42"))
        assert result == 99999

    async def test_raises_on_db_failure(self) -> None:
        mock_gs = AsyncMock(side_effect=RuntimeError("DB down"))
        with patch("sova.db.session.get_session", mock_gs):
            with pytest.raises(RuntimeError, match="Cannot verify worktree safety"):
                await _check_worktree_active_agent(Path("/repo/.claude/worktrees/42"))

    async def test_returns_none_for_non_matching_path(self) -> None:
        record = self._make_run_record("/other/path", 99999)
        mock_gs = self._make_session_mock([record])
        with patch("sova.db.session.get_session", mock_gs):
            result = await _check_worktree_active_agent(Path("/repo/.claude/worktrees/42"))
        assert result is None


class TestResolveWorktreeConflictPruneFailed:
    async def test_logs_warning_on_prune_failure(self) -> None:
        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.worktree.find_worktree_by_branch", new_callable=AsyncMock) as mock_find,
        ):
            mock_run.return_value = _shell_fail(stderr="error: prune failed")
            mock_find.return_value = None

            result = await resolve_worktree_conflict("feat/test", cwd=Path("/repo"))

        assert result is None
        mock_run.assert_awaited_once()


class TestSyncBranchWorktreeConflict:
    async def test_retries_after_worktree_conflict(self) -> None:
        with (
            patch("sova.git.branch.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.branch.run_checked", new_callable=AsyncMock) as mock_checked,
            patch("sova.git.worktree.resolve_worktree_conflict", new_callable=AsyncMock) as mock_resolve,
        ):
            mock_checked.return_value = _shell_ok()  # fetch + pull
            mock_run.side_effect = [
                _shell_fail(stderr="fatal: 'feat/login' is already checked out at '/worktree'"),
                _shell_ok(),  # retry checkout
            ]
            mock_resolve.return_value = Path("/worktree")

            await sync_branch("feat/login", cwd=Path("/repo"))

            mock_resolve.assert_awaited_once_with("feat/login", cwd=Path("/repo"))
            assert mock_run.call_count == 2

    async def test_raises_if_retry_still_fails(self) -> None:
        with (
            patch("sova.git.branch.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.branch.run_checked", new_callable=AsyncMock),
            patch("sova.git.worktree.resolve_worktree_conflict", new_callable=AsyncMock),
        ):
            mock_run.side_effect = [
                _shell_fail(stderr="fatal: 'feat/x' is already checked out at '/wt'"),
                _shell_fail(stderr="fatal: still checked out"),
            ]

            with pytest.raises(RuntimeError):
                await sync_branch("feat/x", cwd=Path("/repo"))

    async def test_propagates_resolve_exception(self) -> None:
        """sync_branch() wraps and propagates errors from resolve_worktree_conflict()."""
        with (
            patch("sova.git.branch.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.branch.run_checked", new_callable=AsyncMock),
            patch(
                "sova.git.worktree.resolve_worktree_conflict",
                new_callable=AsyncMock,
                side_effect=RuntimeError("Worktree in use by PID 12345"),
            ),
        ):
            mock_run.return_value = _shell_fail(stderr="fatal: 'feat/x' is already checked out at '/wt'")

            with pytest.raises(RuntimeError, match="Failed to resolve worktree conflict"):
                await sync_branch("feat/x", cwd=Path("/repo"))


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class TestPRInfo:
    def test_pr_info_fields(self) -> None:
        pr = PRInfo(number=42, url="https://github.com/user/repo/pull/42")
        assert pr.number == 42
        assert pr.url == "https://github.com/user/repo/pull/42"
        assert pr.branch == ""

    def test_pr_info_with_branch(self) -> None:
        pr = PRInfo(number=42, url="https://github.com/user/repo/pull/42", branch="feat/issue-42")
        assert pr.branch == "feat/issue-42"


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
        with patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout=SAMPLE_DIFF)
            result = await get_pr_diff(42, repo="user/repo", github_user="test")

        assert "diff --git" in result
        mock_run.assert_called_once()
        args = mock_run.call_args[0]
        assert "diff" in args
        assert "42" in args

    async def test_raises_on_failure(self) -> None:
        with patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_fail(stderr="not found")
            with pytest.raises(RuntimeError, match="Failed to get diff"):
                await get_pr_diff(42, repo="user/repo")


class TestGetPrFiles:
    async def test_returns_file_list(self) -> None:
        with patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout="foo.py\nbar.py\n")
            result = await get_pr_files(42, repo="user/repo")

        assert result == ["foo.py", "bar.py"]

    async def test_filters_empty_lines(self) -> None:
        with patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_ok(stdout="foo.py\n\nbar.py\n\n")
            result = await get_pr_files(42, repo="user/repo")

        assert result == ["foo.py", "bar.py"]

    async def test_raises_on_failure(self) -> None:
        with patch("sova.git.pr.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell_fail(stderr="not found")
            with pytest.raises(RuntimeError, match="Failed to get files"):
                await get_pr_files(42, repo="user/repo")


# ---------------------------------------------------------------------------
# Diff line parser
# ---------------------------------------------------------------------------


class TestParseDiffLines:
    def test_single_file_addition(self) -> None:
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "new file mode 100644\n"
            "index 0000000..abc1234\n"
            "--- /dev/null\n"
            "+++ b/foo.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+line one\n"
            "+line two\n"
            "+line three\n"
        )
        result = parse_diff_lines(diff)
        assert result == {"foo.py": {1, 2, 3}}

    def test_modification_with_context(self) -> None:
        diff = (
            "diff --git a/bar.py b/bar.py\n"
            "index abc..def 100644\n"
            "--- a/bar.py\n"
            "+++ b/bar.py\n"
            "@@ -10,5 +10,6 @@ def func():\n"
            "     unchanged\n"
            "-    old line\n"
            "+    new line\n"
            "+    extra line\n"
            "     context\n"
            "     more context\n"
        )
        result = parse_diff_lines(diff)
        # Line 10 = context, 11 = new line (replaces old), 12 = extra, 13-14 = context
        assert result == {"bar.py": {10, 11, 12, 13, 14}}

    def test_multi_file_diff(self) -> None:
        diff = (
            "diff --git a/a.py b/a.py\n"
            "index 000..111 100644\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,2 +1,3 @@\n"
            " keep\n"
            "+added\n"
            " also keep\n"
            "diff --git a/b.py b/b.py\n"
            "index 222..333 100644\n"
            "--- a/b.py\n"
            "+++ b/b.py\n"
            "@@ -5,3 +5,3 @@\n"
            "     ctx\n"
            "-    removed\n"
            "+    replaced\n"
            "     ctx2\n"
        )
        result = parse_diff_lines(diff)
        assert result["a.py"] == {1, 2, 3}
        assert result["b.py"] == {5, 6, 7}

    def test_deleted_file(self) -> None:
        diff = (
            "diff --git a/gone.py b/gone.py\n"
            "deleted file mode 100644\n"
            "index abc..000 100644\n"
            "--- a/gone.py\n"
            "+++ /dev/null\n"
            "@@ -1,3 +0,0 @@\n"
            "-line one\n"
            "-line two\n"
            "-line three\n"
        )
        result = parse_diff_lines(diff)
        assert "gone.py" not in result

    def test_empty_diff(self) -> None:
        assert parse_diff_lines("") == {}

    def test_multiple_hunks(self) -> None:
        diff = (
            "diff --git a/multi.py b/multi.py\n"
            "index abc..def 100644\n"
            "--- a/multi.py\n"
            "+++ b/multi.py\n"
            "@@ -1,3 +1,4 @@\n"
            " ctx\n"
            "+new at 2\n"
            " ctx\n"
            " ctx\n"
            "@@ -20,3 +21,4 @@\n"
            " ctx\n"
            "+new at 22\n"
            " ctx\n"
            " ctx\n"
        )
        result = parse_diff_lines(diff)
        assert 2 in result["multi.py"]
        assert 22 in result["multi.py"]

    def test_new_file_metadata_does_not_leak_to_previous_file(self) -> None:
        diff = (
            "diff --git a/existing.py b/existing.py\n"
            "index abc..def 100644\n"
            "--- a/existing.py\n"
            "+++ b/existing.py\n"
            "@@ -1,2 +1,3 @@\n"
            " keep\n"
            "+added\n"
            " also keep\n"
            "diff --git a/brand_new.py b/brand_new.py\n"
            "new file mode 100644\n"
            "index 0000000..abc1234\n"
            "--- /dev/null\n"
            "+++ b/brand_new.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+line one\n"
            "+line two\n"
        )
        result = parse_diff_lines(diff)
        assert result["existing.py"] == {1, 2, 3}
        assert result["brand_new.py"] == {1, 2}


class TestListOpenPrs:
    async def test_success(self) -> None:
        from sova.git.pr import list_open_prs

        prs = [{"number": 1, "title": "PR1"}]
        mock_result = AsyncMock()
        mock_result.success = True
        mock_result.stdout = json.dumps(prs)

        with patch("sova.git.pr.run", return_value=mock_result), patch("sova.git.pr.resolve_gh_env", return_value={}):
            result = await list_open_prs(repo="owner/repo")
        assert result == prs

    async def test_cli_failure(self) -> None:
        from sova.git.pr import list_open_prs

        mock_result = AsyncMock()
        mock_result.success = False
        mock_result.stderr = "auth error"

        with patch("sova.git.pr.run", return_value=mock_result), patch("sova.git.pr.resolve_gh_env", return_value={}):
            result = await list_open_prs(repo="owner/repo")
        assert result == []

    async def test_json_decode_error(self) -> None:
        from sova.git.pr import list_open_prs

        mock_result = AsyncMock()
        mock_result.success = True
        mock_result.stdout = "not json"

        with patch("sova.git.pr.run", return_value=mock_result), patch("sova.git.pr.resolve_gh_env", return_value={}):
            result = await list_open_prs(repo="owner/repo")
        assert result == []
