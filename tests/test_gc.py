"""Tests for issue-aware garbage collection in sova/git/worktree.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sova.git.worktree import (
    cleanup_by_issue_state,
    extract_issue_from_branch,
)
from sova.utils.shell import ShellResult


def _ok(stdout: str = "", stderr: str = "") -> ShellResult:
    return ShellResult(returncode=0, stdout=stdout, stderr=stderr)


def _fail(stderr: str = "error") -> ShellResult:
    return ShellResult(returncode=1, stdout="", stderr=stderr)


class TestExtractIssueFromBranch:
    def test_github_issue_branch(self) -> None:
        assert extract_issue_from_branch("feat/issue-42-login") == ("42", None)

    def test_fix_github_branch(self) -> None:
        assert extract_issue_from_branch("fix/issue-100") == ("100", None)

    def test_jira_branch(self) -> None:
        assert extract_issue_from_branch("feat/RHCLOUD-123-fix-auth") == (None, "RHCLOUD-123")

    def test_refactor_jira(self) -> None:
        assert extract_issue_from_branch("refactor/PROJ-7-cleanup") == (None, "PROJ-7")

    def test_no_match(self) -> None:
        assert extract_issue_from_branch("main") == (None, None)

    def test_no_match_random_branch(self) -> None:
        assert extract_issue_from_branch("feature/login-page") == (None, None)

    def test_chore_branch(self) -> None:
        assert extract_issue_from_branch("chore/issue-5-deps") == ("5", None)

    def test_docs_jira(self) -> None:
        assert extract_issue_from_branch("docs/ABC-99-readme") == (None, "ABC-99")

    def test_prefixed_branch_not_matched(self) -> None:
        assert extract_issue_from_branch("wip/feat/issue-42") == (None, None)

    def test_archive_branch_not_matched(self) -> None:
        assert extract_issue_from_branch("backup/feat/RHCLOUD-9") == (None, None)


class TestCleanupByIssueState:
    @pytest.fixture
    def project(self, tmp_path: Path) -> Path:
        wt_dir = tmp_path / ".claude" / "worktrees"
        wt_dir.mkdir(parents=True)
        return tmp_path

    async def test_closed_issue_triggers_worktree_removal(self, project: Path) -> None:
        (project / ".claude" / "worktrees" / "42").mkdir()
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                _ok(stdout="42\n99\n"),
                _ok(stdout=""),
                _ok(stdout=""),
            ]
            with (
                patch("sova.git.worktree.cleanup_worktree", new_callable=AsyncMock) as mock_cw,
                patch("sova.git.worktree._check_worktree_active_agent", new_callable=AsyncMock, return_value=None),
                patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=[]),
                patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            ):
                gc = await cleanup_by_issue_state(project_dir=project)
        mock_cw.assert_awaited_once()
        assert gc.worktrees_removed == 1

    async def test_open_issue_skips_cleanup(self, project: Path) -> None:
        (project / ".claude" / "worktrees" / "42").mkdir()
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                _ok(stdout="99\n100\n"),
                _ok(stdout=""),
            ]
            with (
                patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=[]),
                patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            ):
                gc = await cleanup_by_issue_state(project_dir=project)
        assert gc.worktrees_removed == 0

    async def test_gh_cli_failure_falls_back_gracefully(self, project: Path) -> None:
        (project / ".claude" / "worktrees" / "42").mkdir()
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                _fail(stderr="gh: not logged in"),
                _ok(stdout=""),
            ]
            with (
                patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=[]),
                patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            ):
                gc = await cleanup_by_issue_state(project_dir=project)
        assert gc.worktrees_removed == 0
        assert gc.branches_removed == 0

    async def test_closed_issue_worktree_removal_by_id(self, project: Path) -> None:
        orphan = project / ".claude" / "worktrees" / "55"
        orphan.mkdir()
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                _ok(stdout="55\n"),
                _ok(stdout=""),
                _ok(stdout=""),
            ]
            with (
                patch("sova.git.worktree.cleanup_worktree", new_callable=AsyncMock),
                patch("sova.git.worktree._check_worktree_active_agent", new_callable=AsyncMock, return_value=None),
                patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=[]),
                patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            ):
                gc = await cleanup_by_issue_state(project_dir=project)
        assert gc.worktrees_removed == 1

    async def test_jira_branch_cleaned_when_upstream_gone(self, project: Path) -> None:
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                _ok(stdout=""),
                _ok(stdout=""),
                _ok(),
            ]
            with (
                patch(
                    "sova.git.worktree._list_local_branches",
                    new_callable=AsyncMock,
                    return_value=["feat/RHCLOUD-42-auth"],
                ),
                patch("sova.git.worktree._has_gone_upstream", new_callable=AsyncMock, return_value=True),
                patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            ):
                gc = await cleanup_by_issue_state(project_dir=project)
        assert gc.branches_removed == 1

    async def test_stash_reporting(self, project: Path) -> None:
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                _ok(stdout=""),
                _ok(stdout=""),
            ]
            with (
                patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=[]),
                patch(
                    "sova.git.worktree._list_stashes",
                    new_callable=AsyncMock,
                    return_value=["stash@{0}: On feat/issue-42: WIP"],
                ),
            ):
                gc = await cleanup_by_issue_state(project_dir=project)
        assert len(gc.stashes_found) == 1
        assert "stash@{0}" in gc.stashes_found[0]

    async def test_active_worktree_branch_skipped(self, project: Path) -> None:
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                _ok(stdout="42\n"),
                _ok(stdout="worktree /repo\nbranch refs/heads/main\n\nworktree /wt\nbranch refs/heads/feat/issue-42\n"),
            ]
            with (
                patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=["feat/issue-42"]),
                patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            ):
                gc = await cleanup_by_issue_state(project_dir=project)
        assert gc.branches_removed == 0

    async def test_dry_run_does_not_remove(self, project: Path) -> None:
        (project / ".claude" / "worktrees" / "42").mkdir()
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                _ok(stdout="42\n"),
                _ok(stdout=""),
                _ok(stdout=""),
            ]
            with (
                patch("sova.git.worktree._check_worktree_active_agent", new_callable=AsyncMock, return_value=None),
                patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=[]),
                patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            ):
                gc = await cleanup_by_issue_state(project_dir=project, dry_run=True)
        assert gc.worktrees_removed == 1
        assert (project / ".claude" / "worktrees" / "42").exists()

    async def test_branch_delete_failure_recorded(self, project: Path) -> None:
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                _ok(stdout="42\n"),
                _ok(stdout=""),
                _fail(stderr="error: cannot delete"),
            ]
            with (
                patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=["feat/issue-42"]),
                patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            ):
                gc = await cleanup_by_issue_state(project_dir=project)
        assert gc.branches_removed == 0
        assert len(gc.errors) == 1
        delete_call = mock_run.call_args_list[-1]
        assert delete_call.args == ("git", "branch", "-d", "feat/issue-42")

    async def test_worktree_cleanup_error_recorded(self, project: Path) -> None:
        (project / ".claude" / "worktrees" / "42").mkdir()
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                _ok(stdout="42\n"),
                _ok(stdout=""),
                _ok(stdout=""),
            ]
            with (
                patch("sova.git.worktree.cleanup_worktree", new_callable=AsyncMock, side_effect=RuntimeError("locked")),
                patch("sova.git.worktree._check_worktree_active_agent", new_callable=AsyncMock, return_value=None),
                patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=[]),
                patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            ):
                gc = await cleanup_by_issue_state(project_dir=project)
        assert gc.worktrees_removed == 0
        assert len(gc.errors) == 1

    async def test_no_worktrees_dir(self, tmp_path: Path) -> None:
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                _ok(stdout=""),
                _ok(stdout=""),
            ]
            with (
                patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=[]),
                patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            ):
                gc = await cleanup_by_issue_state(project_dir=tmp_path)
        assert gc.worktrees_removed == 0


class TestGCSafetyChecks:
    """Tests for worktree GC safety guards (active agent, dirty status)."""

    @pytest.fixture
    def project(self, tmp_path: Path) -> Path:
        wt_dir = tmp_path / ".claude" / "worktrees"
        wt_dir.mkdir(parents=True)
        return tmp_path

    async def test_active_agent_skips_worktree_removal(self, project: Path) -> None:
        (project / ".claude" / "worktrees" / "42").mkdir()
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                _ok(stdout="42\n"),
                _ok(stdout=""),
            ]
            with (
                patch("sova.git.worktree._check_worktree_active_agent", new_callable=AsyncMock, return_value=12345),
                patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=[]),
                patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            ):
                gc = await cleanup_by_issue_state(project_dir=project)
        assert gc.worktrees_removed == 0

    async def test_dirty_worktree_skips_removal(self, project: Path) -> None:
        (project / ".claude" / "worktrees" / "42").mkdir()
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                _ok(stdout="42\n"),
                _ok(stdout=""),
                _ok(stdout="M  sova/cli/app.py\n"),
            ]
            with (
                patch("sova.git.worktree._check_worktree_active_agent", new_callable=AsyncMock, return_value=None),
                patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=[]),
                patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            ):
                gc = await cleanup_by_issue_state(project_dir=project)
        assert gc.worktrees_removed == 0

    async def test_status_failure_skips_worktree_removal(self, project: Path) -> None:
        (project / ".claude" / "worktrees" / "42").mkdir()
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                _ok(stdout="42\n"),
                _ok(stdout=""),
                _fail(stderr="index.lock contention"),
            ]
            with (
                patch("sova.git.worktree._check_worktree_active_agent", new_callable=AsyncMock, return_value=None),
                patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=[]),
                patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            ):
                gc = await cleanup_by_issue_state(project_dir=project)
        assert gc.worktrees_removed == 0


class TestHelperFunctions:
    """Direct tests for private helper functions to improve coverage."""

    async def test_fetch_closed_issues_success(self) -> None:
        from sova.git.worktree import _fetch_closed_issues

        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _ok(stdout="10\n20\n30\n")
            result = await _fetch_closed_issues(Path("/repo"))
        assert result == {"10", "20", "30"}

    async def test_fetch_closed_issues_failure(self) -> None:
        from sova.git.worktree import _fetch_closed_issues

        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _fail(stderr="not logged in")
            result = await _fetch_closed_issues(Path("/repo"))
        assert result == set()

    async def test_fetch_closed_issues_empty(self) -> None:
        from sova.git.worktree import _fetch_closed_issues

        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _ok(stdout="")
            result = await _fetch_closed_issues(Path("/repo"))
        assert result == set()

    async def test_list_local_branches_success(self) -> None:
        from sova.git.worktree import _list_local_branches

        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                _ok(stdout="main\nfeat/issue-42\nfix/bug-99\n"),
                _ok(stdout="main"),
            ]
            result = await _list_local_branches(Path("/repo"))
        assert "main" not in result
        assert "feat/issue-42" in result
        assert "fix/bug-99" in result

    async def test_list_local_branches_failure(self) -> None:
        from sova.git.worktree import _list_local_branches

        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _fail()
            result = await _list_local_branches(Path("/repo"))
        assert result == []

    async def test_list_local_branches_current_branch_fail(self) -> None:
        from sova.git.worktree import _list_local_branches

        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                _ok(stdout="main\nfeat/issue-42\n"),
                _fail(),
            ]
            result = await _list_local_branches(Path("/repo"))
        assert result == []

    async def test_list_stashes_success(self) -> None:
        from sova.git.worktree import _list_stashes

        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _ok(stdout="stash@{0}: On main: WIP\nstash@{1}: On feat: save\n")
            result = await _list_stashes(Path("/repo"))
        assert len(result) == 2

    async def test_list_stashes_failure(self) -> None:
        from sova.git.worktree import _list_stashes

        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _fail()
            result = await _list_stashes(Path("/repo"))
        assert result == []

    async def test_list_stashes_empty(self) -> None:
        from sova.git.worktree import _list_stashes

        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _ok(stdout="")
            result = await _list_stashes(Path("/repo"))
        assert result == []

    async def test_cleanup_github_branch_for_closed_issue(self, tmp_path: Path) -> None:
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                _ok(stdout="42\n"),
                _ok(stdout=""),
                _ok(),
            ]
            with (
                patch(
                    "sova.git.worktree._list_local_branches",
                    new_callable=AsyncMock,
                    return_value=["feat/issue-42-login"],
                ),
                patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            ):
                gc = await cleanup_by_issue_state(project_dir=tmp_path)
        assert gc.branches_removed == 1

    async def test_cleanup_jira_branch_with_remote(self, tmp_path: Path) -> None:
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                _ok(stdout=""),
                _ok(stdout=""),
            ]
            with (
                patch(
                    "sova.git.worktree._list_local_branches",
                    new_callable=AsyncMock,
                    return_value=["feat/PROJ-99-fix"],
                ),
                patch("sova.git.worktree._has_gone_upstream", new_callable=AsyncMock, return_value=False),
                patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            ):
                gc = await cleanup_by_issue_state(project_dir=tmp_path)
        assert gc.branches_removed == 0

    async def test_cleanup_non_matching_branch_skipped(self, tmp_path: Path) -> None:
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                _ok(stdout=""),
                _ok(stdout=""),
            ]
            with (
                patch(
                    "sova.git.worktree._list_local_branches",
                    new_callable=AsyncMock,
                    return_value=["some-random-branch"],
                ),
                patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            ):
                gc = await cleanup_by_issue_state(project_dir=tmp_path)
        assert gc.branches_removed == 0

    async def test_cleanup_dry_run_branches(self, tmp_path: Path) -> None:
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                _ok(stdout="42\n"),
                _ok(stdout=""),
            ]
            with (
                patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=["feat/issue-42"]),
                patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            ):
                gc = await cleanup_by_issue_state(project_dir=tmp_path, dry_run=True)
        assert gc.branches_removed == 1

    async def test_worktree_non_dir_entry_skipped(self, tmp_path: Path) -> None:
        wt_dir = tmp_path / ".claude" / "worktrees"
        wt_dir.mkdir(parents=True)
        (wt_dir / "not-a-dir.txt").write_text("file")
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                _ok(stdout="not-a-dir.txt\n"),
                _ok(stdout=""),
            ]
            with (
                patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=[]),
                patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            ):
                gc = await cleanup_by_issue_state(project_dir=tmp_path)
        assert gc.worktrees_removed == 0


class TestStartupGC:
    """Tests for dashboard startup GC integration."""

    async def test_startup_gc_logs_on_cleanup(self, tmp_path: Path) -> None:
        from sova.dashboard.app import _startup_gc
        from sova.git.worktree import GCResult

        gc = GCResult(worktrees_removed=2, branches_removed=1, stashes_found=["stash@{0}"])
        with (
            patch("sova.git.worktree.cleanup_by_issue_state", new_callable=AsyncMock, return_value=gc),
            patch("sova.dashboard.app.log") as mock_log,
        ):
            await _startup_gc(tmp_path)
            mock_log.info.assert_called_once_with(
                "lifespan.gc_complete",
                worktrees=2,
                branches=1,
                stashes=1,
            )

    async def test_startup_gc_no_log_when_nothing_removed(self, tmp_path: Path) -> None:
        from sova.dashboard.app import _startup_gc
        from sova.git.worktree import GCResult

        gc = GCResult()
        with (
            patch("sova.git.worktree.cleanup_by_issue_state", new_callable=AsyncMock, return_value=gc),
            patch("sova.dashboard.app.log") as mock_log,
        ):
            await _startup_gc(tmp_path)
            mock_log.info.assert_not_called()

    async def test_startup_gc_handles_failure(self, tmp_path: Path) -> None:
        from sova.dashboard.app import _startup_gc

        with (
            patch(
                "sova.git.worktree.cleanup_by_issue_state",
                new_callable=AsyncMock,
                side_effect=RuntimeError("gh not found"),
            ),
            patch("sova.dashboard.app.log") as mock_log,
        ):
            await _startup_gc(tmp_path)
            mock_log.warning.assert_called_once()
            assert mock_log.warning.call_args[0][0] == "lifespan.gc_failed"
