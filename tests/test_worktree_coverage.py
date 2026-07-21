"""Tests covering uncovered paths in sova/git/worktree.py."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sova.git.worktree import (
    WorktreeInfo,
    _copy_claude_artifacts,
    _copy_worktree_files,
    cleanup_stale_worktrees,
    cleanup_worktree,
    create_worktree,
)
from sova.utils.shell import ShellResult


def _shell_ok(stdout: str = "", stderr: str = "") -> ShellResult:
    return ShellResult(returncode=0, stdout=stdout, stderr=stderr)


def _shell_fail(stderr: str = "error", returncode: int = 1) -> ShellResult:
    return ShellResult(returncode=returncode, stdout="", stderr=stderr)


class TestCreateWorktreeStaleRemoval:
    async def test_removes_stale_worktree_on_wrong_branch(self) -> None:
        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.worktree.Path.mkdir"),
            patch("sova.git.worktree.Path.exists", return_value=True),
            patch("sova.git.worktree._copy_claude_artifacts"),
            patch("sova.git.worktree._ensure_compose_project_name"),
        ):
            mock_run.side_effect = [
                _shell_ok(stdout="wrong-branch\n"),
                _shell_ok(),
                _shell_ok(),
            ]
            info = await create_worktree(
                issue_id="42",
                branch="feat/login",
                base_branch="main",
                project_dir=Path("/repo"),
            )
            assert info.branch == "feat/login"
            remove_calls = [c for c in mock_run.call_args_list if "remove" in str(c)]
            assert len(remove_calls) >= 1

    async def test_reuses_worktree_with_commits_ahead(self) -> None:
        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.worktree.Path.mkdir"),
            patch("sova.git.worktree.Path.exists", return_value=True),
            patch("sova.git.worktree._copy_claude_artifacts"),
            patch("sova.git.worktree._ensure_compose_project_name"),
        ):
            mock_run.side_effect = [
                _shell_ok(stdout="feat/login\n"),
                _shell_ok(stdout="3\n"),
            ]
            info = await create_worktree(
                issue_id="42",
                branch="feat/login",
                base_branch="main",
                project_dir=Path("/repo"),
            )
            assert isinstance(info, WorktreeInfo)
            assert info.branch == "feat/login"


class TestCleanupStaleWorktrees:
    async def test_removes_stale_entries(self, tmp_path: Path) -> None:
        worktrees_dir = tmp_path / ".claude" / "worktrees"
        worktrees_dir.mkdir(parents=True)
        stale = worktrees_dir / "old-42"
        stale.mkdir()
        old_time = time.time() - (5 * 86400)
        os.utime(stale, (old_time, old_time))
        fresh = worktrees_dir / "fresh-99"
        fresh.mkdir()
        with patch("sova.git.worktree.cleanup_worktree", new_callable=AsyncMock):
            removed = await cleanup_stale_worktrees(project_dir=tmp_path, ttl_days=3)
        assert removed == 1

    async def test_returns_zero_when_no_worktrees_dir(self, tmp_path: Path) -> None:
        removed = await cleanup_stale_worktrees(project_dir=tmp_path, ttl_days=3)
        assert removed == 0

    async def test_skips_files_in_worktrees_dir(self, tmp_path: Path) -> None:
        worktrees_dir = tmp_path / ".claude" / "worktrees"
        worktrees_dir.mkdir(parents=True)
        (worktrees_dir / "some-file.txt").write_text("not a dir")
        removed = await cleanup_stale_worktrees(project_dir=tmp_path, ttl_days=0)
        assert removed == 0


class TestCleanupWorktreeFallback:
    async def test_falls_back_to_rmtree_on_git_failure(self, tmp_path: Path) -> None:
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.worktree.shutil.rmtree") as mock_rmtree,
        ):
            mock_run.side_effect = [
                _shell_fail(stderr="error: not a worktree"),
                _shell_ok(),
            ]
            await cleanup_worktree(wt_path, cwd=tmp_path)
            mock_rmtree.assert_called_once_with(wt_path)


class TestCopyClaudeArtifacts:
    def test_copies_claude_md_file(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        (project / "CLAUDE.md").write_text("# Instructions")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        _copy_claude_artifacts(project, worktree)
        assert (worktree / "CLAUDE.md").read_text() == "# Instructions"

    def test_handles_claude_md_copy_error(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        (project / "CLAUDE.md").write_text("# Instructions")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        with patch("sova.git.worktree.shutil.copy2", side_effect=OSError("denied")):
            _copy_claude_artifacts(project, worktree)

    def test_handles_copytree_error(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        claude_dir = project / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "commands").mkdir()
        (claude_dir / "commands" / "dev.md").write_text("cmd")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        with patch("sova.git.worktree.shutil.copytree", side_effect=OSError("denied")):
            _copy_claude_artifacts(project, worktree)

    def test_handles_copy_file_error(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        claude_dir = project / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "settings.json").write_text("{}")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        with patch("sova.git.worktree.shutil.copy2", side_effect=OSError("denied")):
            _copy_claude_artifacts(project, worktree)

    def test_returns_early_when_no_claude_dir(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        _copy_claude_artifacts(project, worktree)
        assert not (worktree / ".claude").exists()


class TestCopyWorktreeFilesTraversal:
    def test_rejects_source_path_traversal(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        _copy_worktree_files(project, worktree, ["../../etc/passwd"])

    def test_skips_nonexistent_source(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        _copy_worktree_files(project, worktree, ["missing.txt"])

    def test_copies_valid_file(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        (project / "config.toml").write_text("key = true")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        _copy_worktree_files(project, worktree, ["config.toml"])
        assert (worktree / "config.toml").read_text() == "key = true"
