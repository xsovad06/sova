"""Tests for issue-aware garbage collection in sova/git/worktree.py."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sova.git.worktree import (
    GCEntry,
    cleanup_by_issue_state,
    extract_issue_from_branch,
)
from sova.utils.shell import ShellResult


def _ok(stdout: str = "", stderr: str = "") -> ShellResult:
    return ShellResult(returncode=0, stdout=stdout, stderr=stderr)


def _fail(stderr: str = "error") -> ShellResult:
    return ShellResult(returncode=1, stdout="", stderr=stderr)


# A merged same-repository pull request whose head is the branch tip "abc123".
_MERGED_PR = '[{"headRefOid": "abc123", "isCrossRepository": false}]'


@pytest.fixture
def merged_branches() -> Iterator[AsyncMock]:
    """Every branch passes `git branch -d`'s merge check, which runs in a dry run and a real run alike."""
    with patch("sova.git.worktree._branch_merged", new_callable=AsyncMock, return_value=True) as merged:
        yield merged


def _worktree_entries(count: int) -> list[GCEntry]:
    return [GCEntry(path=f"/wt/{n}", branch=f"feat/issue-{n}", reason="issue is closed") for n in range(count)]


def _branch_entries(count: int) -> list[GCEntry]:
    return [GCEntry(path="", branch=f"feat/issue-{n}", reason="issue is closed") for n in range(count)]


class TestExtractIssueFromBranch:
    @pytest.mark.parametrize(
        ("branch", "expected"),
        [
            pytest.param("feat/issue-42-login", ("42", None), id="github-issue-branch"),
            pytest.param("fix/issue-100", ("100", None), id="fix-github-branch"),
            pytest.param("feat/PROJ-123-fix-auth", (None, "PROJ-123"), id="jira-branch"),
            pytest.param("refactor/PROJ-7-cleanup", (None, "PROJ-7"), id="refactor-jira"),
            pytest.param("main", (None, None), id="no-match"),
            pytest.param("feature/login-page", (None, None), id="no-match-random-branch"),
            pytest.param("chore/issue-5-deps", ("5", None), id="chore-branch"),
            pytest.param("docs/ABC-99-readme", (None, "ABC-99"), id="docs-jira"),
            pytest.param("wip/feat/issue-42", (None, None), id="prefixed-branch-not-matched"),
            pytest.param("backup/feat/PROJ-9", (None, None), id="archive-branch-not-matched"),
        ],
    )
    def test_extract_issue_from_branch(self, branch: str, expected: tuple[str | None, str | None]) -> None:
        assert extract_issue_from_branch(branch) == expected


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
                patch("sova.git.worktree.check_worktree_active_agent", new_callable=AsyncMock, return_value=None),
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
                patch("sova.git.worktree.check_worktree_active_agent", new_callable=AsyncMock, return_value=None),
                patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=[]),
                patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            ):
                gc = await cleanup_by_issue_state(project_dir=project)
        assert gc.worktrees_removed == 1

    @pytest.mark.usefixtures("merged_branches")
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
                    return_value=["feat/PROJ-42-auth"],
                ),
                patch("sova.git.worktree._has_gone_upstream", new_callable=AsyncMock, return_value=True),
                patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            ):
                gc = await cleanup_by_issue_state(project_dir=project)
        assert gc.branches_removed == 1

    async def test_jira_worktree_cleaned_when_upstream_gone(self, project: Path) -> None:
        (project / ".claude" / "worktrees" / "PROJ-42").mkdir()
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                _ok(stdout=""),
                _ok(stdout=""),
                _ok(stdout="feat/PROJ-42-auth\n"),
                _ok(stdout=""),
            ]
            with (
                patch("sova.git.worktree.cleanup_worktree", new_callable=AsyncMock) as mock_cw,
                patch("sova.git.worktree.check_worktree_active_agent", new_callable=AsyncMock, return_value=None),
                patch("sova.git.worktree._has_gone_upstream", new_callable=AsyncMock, return_value=True),
                patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=[]),
                patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            ):
                gc = await cleanup_by_issue_state(project_dir=project)
        mock_cw.assert_awaited_once()
        assert gc.worktrees_removed == 1

    async def test_jira_worktree_kept_when_upstream_tracked(self, project: Path) -> None:
        (project / ".claude" / "worktrees" / "PROJ-42").mkdir()
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                _ok(stdout=""),
                _ok(stdout=""),
                _ok(stdout="feat/PROJ-42-auth\n"),
            ]
            with (
                patch("sova.git.worktree.cleanup_worktree", new_callable=AsyncMock) as mock_cw,
                patch("sova.git.worktree._has_gone_upstream", new_callable=AsyncMock, return_value=False),
                patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=[]),
                patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            ):
                gc = await cleanup_by_issue_state(project_dir=project)
        mock_cw.assert_not_awaited()
        assert gc.worktrees_removed == 0

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
                patch("sova.git.worktree.check_worktree_active_agent", new_callable=AsyncMock, return_value=None),
                patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=[]),
                patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            ):
                gc = await cleanup_by_issue_state(project_dir=project, dry_run=True)
        assert gc.worktrees_removed == 1
        assert (project / ".claude" / "worktrees" / "42").exists()

    @pytest.mark.usefixtures("merged_branches")
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
                patch("sova.git.worktree.check_worktree_active_agent", new_callable=AsyncMock, return_value=None),
                patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=[]),
                patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            ):
                gc = await cleanup_by_issue_state(project_dir=project)
        assert gc.worktrees_removed == 0
        assert len(gc.errors) == 1

    async def test_include_branches_false_skips_branch_deletion(self, project: Path) -> None:
        """include_branches=False removes worktrees but leaves branches alone."""
        (project / ".claude" / "worktrees" / "42").mkdir()
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                _ok(stdout="42\n"),
                _ok(stdout=""),
                _ok(stdout=""),
            ]
            with (
                patch("sova.git.worktree.cleanup_worktree", new_callable=AsyncMock) as mock_cw,
                patch("sova.git.worktree.check_worktree_active_agent", new_callable=AsyncMock, return_value=None),
                patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock) as mock_branches,
                patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            ):
                gc = await cleanup_by_issue_state(project_dir=project, include_branches=False)
        mock_cw.assert_awaited_once()
        assert gc.worktrees_removed == 1
        assert gc.branches_removed == 0
        mock_branches.assert_not_awaited()

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
                patch("sova.git.worktree.check_worktree_active_agent", new_callable=AsyncMock, return_value=12345),
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
                patch("sova.git.worktree.check_worktree_active_agent", new_callable=AsyncMock, return_value=None),
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
                patch("sova.git.worktree.check_worktree_active_agent", new_callable=AsyncMock, return_value=None),
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

    @pytest.mark.usefixtures("merged_branches")
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
                patch("sova.git.worktree._branch_merged", new_callable=AsyncMock, return_value=True),
                patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            ):
                gc = await cleanup_by_issue_state(project_dir=tmp_path, dry_run=True)
        assert gc.branches_removed == 1

    async def test_dry_run_lists_an_unmerged_branch_as_skipped(self, tmp_path: Path) -> None:
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [_ok(stdout="42\n"), _ok(stdout="")]
            with (
                patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=["feat/issue-42"]),
                patch("sova.git.worktree._branch_merged", new_callable=AsyncMock, return_value=False),
                patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            ):
                gc = await cleanup_by_issue_state(project_dir=tmp_path, dry_run=True)
        assert gc.removed_branches == []
        assert gc.skipped == [
            GCEntry(
                path="",
                branch="feat/issue-42",
                reason="it is not merged where git branch -d checks (it may have been squash merged)",
            )
        ]

    async def test_failed_worktree_inventory_skips_the_branch_pass(self, tmp_path: Path) -> None:
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [_ok(stdout="42\n"), _fail(stderr="fatal: broken")]
            with (
                patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock) as branches,
                patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            ):
                gc = await cleanup_by_issue_state(project_dir=tmp_path)
        branches.assert_not_awaited()
        assert gc.errors == ["Could not list worktrees, so no branch was considered: fatal: broken"]

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

        gc = GCResult(
            removed_worktrees=_worktree_entries(2), removed_branches=_branch_entries(1), stashes_found=["stash@{0}"]
        )
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


class TestStashesFor:
    @pytest.mark.parametrize(
        ("line", "kept"),
        [
            ("stash@{0}: WIP on feat/issue-42: abc fix", True),
            ("stash@{1}: On feat/issue-42: saved", True),
            ("stash@{2}: On main: rebase on feat/issue-42: notes", False),
            ("stash@{3}: WIP on feat/issue-420: abc", False),
            ("not a stash line", False),
        ],
    )
    def test_matches_the_branch_field_only(self, line: str, kept: bool) -> None:
        from sova.git.worktree import _stashes_for

        assert _stashes_for([line], {"feat/issue-42"}) == ([line] if kept else [])


class TestBranchMerged:
    @pytest.mark.parametrize(
        ("results", "merged"),
        [
            ([ShellResult(0, "", "")], True),
            ([ShellResult(1, "", "")], False),
            ([ShellResult(128, "", "no upstream"), ShellResult(0, "", "")], True),
            ([ShellResult(128, "", "no upstream"), ShellResult(1, "", "")], False),
        ],
        ids=["merged-upstream", "unmerged-upstream", "head-merged", "head-unmerged"],
    )
    async def test_upstream_first_then_head(self, results: list[ShellResult], merged: bool) -> None:
        from sova.git.worktree import _branch_merged

        with patch("sova.git.worktree.run", new_callable=AsyncMock, side_effect=results):
            assert await _branch_merged("feat/issue-42", Path("/repo")) is merged


class TestIssueScopedCleanup:
    """cleanup_by_issue_state(issue=...) names each target it removes or skips and touches no other issue."""

    @pytest.fixture
    def project(self, tmp_path: Path) -> Path:
        for issue in ("42", "43"):
            (tmp_path / ".claude" / "worktrees" / issue).mkdir(parents=True)
        return tmp_path

    @staticmethod
    def _porcelain(project: Path) -> str:
        return "".join(
            f"worktree {project / '.claude' / 'worktrees' / n}\nbranch refs/heads/feat/issue-{n}\n"
            for n in ("42", "43")
        )

    @pytest.mark.usefixtures("merged_branches")
    async def test_removes_only_the_named_issue_and_reports_its_stashes(self, project: Path) -> None:
        wt42 = project / ".claude" / "worktrees" / "42"
        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.worktree.cleanup_worktree", new_callable=AsyncMock) as mock_cw,
            patch("sova.git.worktree.check_worktree_active_agent", new_callable=AsyncMock, return_value=None),
            patch(
                "sova.git.worktree._list_local_branches",
                new_callable=AsyncMock,
                return_value=["feat/issue-42", "feat/issue-43"],
            ),
            patch(
                "sova.git.worktree._list_stashes",
                new_callable=AsyncMock,
                return_value=["stash@{0}: WIP on feat/issue-42: abc fix", "stash@{1}: On main: other"],
            ),
        ):
            # issue view, worktree list, merged PR lookup (none), git status, git branch -d
            mock_run.side_effect = [_ok("CLOSED\n"), _ok(self._porcelain(project)), _ok("[]"), _ok(""), _ok("")]
            gc = await cleanup_by_issue_state(project_dir=project, issue="42")

        mock_cw.assert_awaited_once_with(wt42, cwd=project)
        assert gc.removed_worktrees == [GCEntry(path=str(wt42), branch="feat/issue-42", reason="issue #42 is closed")]
        assert gc.removed_branches == [GCEntry(path="", branch="feat/issue-42", reason="issue #42 is closed")]
        assert gc.skipped == []
        assert gc.stashes_found == ["stash@{0}: WIP on feat/issue-42: abc fix"]
        assert mock_run.await_args_list[-1].args == ("git", "branch", "-d", "feat/issue-42")

    @pytest.mark.parametrize(
        ("active_pid", "status_calls", "reason"),
        [
            pytest.param(777, [], "an agent (PID 777) is running in it", id="active-agent"),
            pytest.param(None, [_ok(" M app.py\n")], "it has uncommitted changes", id="dirty"),
        ],
    )
    async def test_unsafe_worktree_is_skipped_with_its_reason(
        self, project: Path, active_pid: int | None, status_calls: list[ShellResult], reason: str
    ) -> None:
        wt42 = project / ".claude" / "worktrees" / "42"
        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.worktree.cleanup_worktree", new_callable=AsyncMock) as mock_cw,
            patch("sova.git.worktree.check_worktree_active_agent", new_callable=AsyncMock, return_value=active_pid),
            patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=["feat/issue-42"]),
            patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
        ):
            mock_run.side_effect = [_ok("CLOSED\n"), _ok(self._porcelain(project)), _ok("[]"), *status_calls]
            gc = await cleanup_by_issue_state(project_dir=project, issue="42")

        mock_cw.assert_not_awaited()
        assert gc.removed_worktrees == []
        assert gc.removed_branches == []
        assert gc.skipped == [
            GCEntry(path=str(wt42), branch="feat/issue-42", reason=reason),
            GCEntry(path="", branch="feat/issue-42", reason="a kept worktree has it checked out"),
        ]

    async def test_unreadable_issue_state_is_an_error_not_an_empty_result(self, project: Path) -> None:
        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.worktree.cleanup_worktree", new_callable=AsyncMock) as mock_cw,
            patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=["feat/issue-42"]),
            patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
        ):
            mock_run.side_effect = [_fail("HTTP 502"), _ok(self._porcelain(project))]
            gc = await cleanup_by_issue_state(project_dir=project, issue="42")

        mock_cw.assert_not_awaited()
        assert gc.worktrees_removed == 0
        assert gc.errors == ["Could not read the state of issue #42: HTTP 502"]


class TestSkipRulesMatchInBothModes:
    async def test_an_unmerged_branch_is_a_skip_on_a_real_run_too(self, tmp_path: Path) -> None:
        with patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [_ok(stdout="42\n"), _ok(stdout="")]
            with (
                patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=["feat/issue-42"]),
                patch("sova.git.worktree._branch_merged", new_callable=AsyncMock, return_value=False),
                patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            ):
                gc = await cleanup_by_issue_state(project_dir=tmp_path)
        assert gc.errors == []
        assert gc.skipped == [
            GCEntry(
                path="",
                branch="feat/issue-42",
                reason="it is not merged where git branch -d checks (it may have been squash merged)",
            )
        ]
        assert all(call.args[:3] != ("git", "branch", "-d") for call in mock_run.await_args_list)


class TestScopedCleanupSources:
    @pytest.fixture
    def project(self, tmp_path: Path) -> Path:
        (tmp_path / ".claude" / "worktrees" / "42").mkdir(parents=True)
        return tmp_path

    @staticmethod
    def _porcelain(project: Path) -> str:
        return f"worktree {project / '.claude' / 'worktrees' / '42'}\nbranch refs/heads/feat/issue-42\n"

    async def test_issue_state_is_read_from_the_configured_repo_as_the_configured_user(self, project: Path) -> None:
        env = {"GH_TOKEN": "t"}
        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.worktree.resolve_gh_env", new_callable=AsyncMock, return_value=env) as gh_env,
            patch("sova.git.worktree.cleanup_worktree", new_callable=AsyncMock),
            patch("sova.git.worktree.check_worktree_active_agent", new_callable=AsyncMock, return_value=None),
            patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=[]),
            patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
        ):
            mock_run.side_effect = [_ok("CLOSED\n"), _ok(self._porcelain(project)), _ok("[]"), _ok("")]
            await cleanup_by_issue_state(project_dir=project, issue="42", repo="acme/widgets", github_user="octo")
        gh_env.assert_awaited_once_with("octo")
        view = mock_run.await_args_list[0]
        assert view.args[:6] == ("gh", "issue", "view", "42", "--repo", "acme/widgets")
        assert view.kwargs["env"] == env

    async def test_an_open_issue_whose_pr_merged_is_eligible(self, project: Path) -> None:
        wt42 = project / ".claude" / "worktrees" / "42"
        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.worktree.cleanup_worktree", new_callable=AsyncMock) as mock_cw,
            patch("sova.git.worktree.check_worktree_active_agent", new_callable=AsyncMock, return_value=None),
            patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=["feat/issue-42"]),
            patch("sova.git.worktree._branch_merged", new_callable=AsyncMock, return_value=False) as ancestry,
            patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
        ):
            # issue view (OPEN), worktree list, merged PR lookup, branch tip, git status, git branch -D
            mock_run.side_effect = [
                _ok("OPEN\n"), _ok(self._porcelain(project)), _ok(_MERGED_PR), _ok("abc123\n"), _ok(""), _ok(""),
            ]  # fmt: skip
            gc = await cleanup_by_issue_state(project_dir=project, issue="42")
        reason = "the pull request of feat/issue-42 is merged"
        mock_cw.assert_awaited_once_with(wt42, cwd=project)
        assert gc.removed_worktrees == [GCEntry(path=str(wt42), branch="feat/issue-42", reason=reason)]
        assert gc.removed_branches == [GCEntry(path="", branch="feat/issue-42", reason=reason)]
        assert mock_run.await_args_list[2].args[:3] == ("gh", "pr", "list")
        # The merged pull request's head is the proof: no ancestry check, and -D since a squash leaves none.
        ancestry.assert_not_awaited()
        assert mock_run.await_args_list[-1].args == ("git", "branch", "-D", "feat/issue-42")

    @pytest.mark.parametrize(
        "prs",
        [
            pytest.param('[{"headRefOid": "old999", "isCrossRepository": false}]', id="stale-pr-of-an-earlier-attempt"),
            pytest.param('[{"headRefOid": "abc123", "isCrossRepository": true}]', id="fork-pr-with-the-same-name"),
        ],
    )
    async def test_a_merged_pr_that_is_not_this_branch_tip_is_no_proof(self, project: Path, prs: str) -> None:
        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.worktree.cleanup_worktree", new_callable=AsyncMock) as mock_cw,
            patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=["feat/issue-42"]),
            patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
        ):
            mock_run.side_effect = [_ok("OPEN\n"), _ok(self._porcelain(project)), _ok(prs), _ok("abc123\n")]
            gc = await cleanup_by_issue_state(project_dir=project, issue="42")
        mock_cw.assert_not_awaited()
        assert (gc.removed_worktrees, gc.removed_branches, gc.errors) == ([], [], [])
        wt42 = str(project / ".claude" / "worktrees" / "42")
        reason = "its branch has local commits after its merged pull request"
        expected = [GCEntry(path=wt42, branch="feat/issue-42", reason=reason)] if "old999" in prs else []
        assert gc.skipped == expected

    async def test_an_open_issue_with_no_merged_pr_is_left_alone(self, project: Path) -> None:
        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.worktree.cleanup_worktree", new_callable=AsyncMock) as mock_cw,
            patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=["feat/issue-42"]),
            patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
        ):
            mock_run.side_effect = [_ok("OPEN\n"), _ok(self._porcelain(project)), _ok("[]")]
            gc = await cleanup_by_issue_state(project_dir=project, issue="42")
        mock_cw.assert_not_awaited()
        assert (gc.removed_worktrees, gc.removed_branches, gc.errors) == ([], [], [])

    async def test_a_failed_merged_pr_lookup_is_an_error(self, project: Path) -> None:
        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.worktree.cleanup_worktree", new_callable=AsyncMock) as mock_cw,
            patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=["feat/issue-42"]),
            patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
        ):
            mock_run.side_effect = [_ok("OPEN\n"), _ok(self._porcelain(project)), _fail("HTTP 502")]
            gc = await cleanup_by_issue_state(project_dir=project, issue="42")
        mock_cw.assert_not_awaited()
        assert gc.errors == ["Could not check for a merged pull request of feat/issue-42"]

    async def test_only_limits_removal_to_the_listed_targets(self, project: Path) -> None:
        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.worktree.cleanup_worktree", new_callable=AsyncMock) as mock_cw,
            patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=["feat/issue-42"]),
            patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
            patch("sova.git.worktree.check_worktree_active_agent", new_callable=AsyncMock, return_value=None),
            patch("sova.git.worktree._branch_merged", new_callable=AsyncMock, return_value=True),
        ):
            # issue view, worktree list, merged PR lookup, git status of the (eligible, clean) worktree
            mock_run.side_effect = [_ok("CLOSED\n"), _ok(self._porcelain(project)), _ok("[]"), _ok("")]
            gc = await cleanup_by_issue_state(project_dir=project, issue="42", only=set())
        mock_cw.assert_not_awaited()
        assert (gc.removed_worktrees, gc.removed_branches, gc.errors) == ([], [], [])
        assert all(call.args[:2] != ("git", "branch") for call in mock_run.await_args_list)

    @pytest.mark.parametrize("issue", ["\u00b2", "4\u00b2"])
    async def test_a_non_ascii_digit_is_not_an_issue_number(self, project: Path, issue: str) -> None:
        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.worktree._list_local_branches", new_callable=AsyncMock, return_value=[]),
            patch("sova.git.worktree._list_stashes", new_callable=AsyncMock, return_value=[]),
        ):
            mock_run.side_effect = [_ok("")]
            gc = await cleanup_by_issue_state(project_dir=project, issue=issue)
        assert gc.errors == [f"Not an issue number or JIRA key: {issue!r}"]
        assert all(call.args[0] != "gh" for call in mock_run.await_args_list)
