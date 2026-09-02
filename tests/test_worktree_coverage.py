"""Tests covering uncovered paths in sova/git/worktree.py."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sova.git.worktree import (
    WORKTREE_DIR,
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

    def test_skips_claude_md_when_already_exists(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        (project / "CLAUDE.md").write_text("# Primary instructions")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "CLAUDE.md").write_text("# Worktree-specific")
        _copy_claude_artifacts(project, worktree)
        assert (worktree / "CLAUDE.md").read_text() == "# Worktree-specific"

    def test_copies_skills_directory(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        claude_dir = project / ".claude"
        skills_dir = claude_dir / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "testing-patterns").mkdir()
        (skills_dir / "testing-patterns" / "skill.md").write_text("test skill")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        _copy_claude_artifacts(project, worktree)
        wt_skill = worktree / ".claude" / "skills" / "testing-patterns" / "skill.md"
        assert wt_skill.exists()
        assert wt_skill.read_text() == "test skill"


class TestEnsureClaudeArtifactsAlias:
    def test_backward_compat_alias_exists(self) -> None:
        from sova.git.worktree import _copy_claude_artifacts, ensure_claude_artifacts

        assert _copy_claude_artifacts is ensure_claude_artifacts


class TestCopyWorktreeFilesTraversal:
    def test_rejects_source_path_traversal(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        escaped_file = tmp_path / "escaped.txt"
        escaped_file.write_text("sensitive data")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        with patch("sova.git.worktree.shutil.copy2") as mock_copy:
            _copy_worktree_files(project, worktree, ["../escaped.txt"])
        mock_copy.assert_not_called()

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


class TestCopyClaudeFileOSError:
    def test_copy_settings_local_oserror(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        claude_dir = project / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "settings.local.json").write_text("{}")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        original_copy2 = __import__("shutil").copy2

        def selective_copy2(src, dst, *a, **kw):
            if "settings.local.json" in str(src):
                raise OSError("permission denied")
            return original_copy2(src, dst, *a, **kw)

        with patch("sova.git.worktree.shutil.copy2", side_effect=selective_copy2) as mock_copy2:
            _copy_claude_artifacts(project, worktree)
        mock_copy2.assert_called_once_with(
            claude_dir / "settings.local.json",
            worktree / ".claude" / "settings.local.json",
        )


class TestCopyWorktreeFilesDestTraversal:
    def test_rejects_dest_path_traversal(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        src_file = project / "ok.txt"
        src_file.write_text("data")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        escape_target = tmp_path / "escaped.txt"
        symlink = worktree / "ok.txt"
        symlink.symlink_to(escape_target)
        with patch("sova.git.worktree.shutil.copy2") as mock_copy2:
            _copy_worktree_files(project, worktree, ["ok.txt"])
        mock_copy2.assert_not_called()


class TestCopyWorktreeFilesDirConflict:
    def test_raises_when_regular_file_blocks_dir_symlink(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        src_dir = project / "mydir"
        src_dir.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "mydir").write_text("i am a file")
        with pytest.raises(FileExistsError, match="regular file already exists"):
            _copy_worktree_files(project, worktree, ["mydir"])


class TestCheckActiveAgentImportError:
    async def test_import_error_returns_none(self) -> None:
        from sova.git.worktree import _check_worktree_active_agent

        original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

        def fake_import(name, *args, **kwargs):
            if name == "sqlalchemy":
                raise ImportError("no sqlalchemy")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            result = await _check_worktree_active_agent(Path("/fake/wt"))
        assert result is None


class TestCreateWorktreeCheckedOutElsewhere:
    @pytest.mark.parametrize(
        "stderr",
        [
            "fatal: 'feat/login' is already checked out at '/other/wt'",
            "fatal: 'feat/login' is already used by worktree at '/other/wt'",
        ],
    )
    async def test_raises_on_branch_conflict(self, stderr: str) -> None:
        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.worktree.Path.exists", return_value=False),
            patch("sova.git.worktree.Path.mkdir"),
        ):
            mock_run.return_value = _shell_fail(stderr=stderr)
            with pytest.raises(RuntimeError):
                await create_worktree(
                    issue_id="42",
                    branch="feat/login",
                    base_branch="main",
                    project_dir=Path("/repo"),
                )


class TestCreateWorktreeStaleRegistrationRecovery:
    async def test_prune_before_create_failure_is_logged_and_nonfatal(self) -> None:
        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.worktree.Path.exists", return_value=False),
            patch("sova.git.worktree.Path.mkdir"),
            patch("sova.git.worktree._copy_claude_artifacts"),
            patch("sova.git.worktree._ensure_compose_project_name"),
            patch("sova.git.worktree.log") as mock_log,
        ):
            mock_run.side_effect = [
                _shell_fail(stderr="fatal: transient prune error"),
                _shell_ok(),
            ]
            info = await create_worktree(
                issue_id="42",
                branch="feat/login",
                base_branch="main",
                project_dir=Path("/repo"),
            )
            assert info.branch == "feat/login"
            mock_log.warning.assert_any_call(
                "worktree.prune_before_create_failed", stderr="fatal: transient prune error"
            )

    async def test_retries_and_recovers_on_stale_registration(self) -> None:
        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.worktree.Path.exists", return_value=False),
            patch("sova.git.worktree.Path.mkdir"),
            patch("sova.git.worktree._copy_claude_artifacts"),
            patch("sova.git.worktree._ensure_compose_project_name"),
            patch("sova.git.worktree.log") as mock_log,
        ):
            mock_run.side_effect = [
                _shell_ok(),  # Layer 1 prune
                _shell_fail(
                    stderr="fatal: '/repo/.claude/worktrees/42' is missing but already registered"
                ),  # first add fails
                _shell_ok(),  # Layer 2 retry prune
                _shell_ok(),  # retried add succeeds
            ]
            info = await create_worktree(
                issue_id="42",
                branch="feat/login",
                base_branch="main",
                project_dir=Path("/repo"),
            )
            assert info.branch == "feat/login"
            assert mock_run.call_count == 4
            mock_log.info.assert_any_call(
                "worktree.stale_registration_recovered",
                path=str(Path("/repo") / WORKTREE_DIR / "42"),
                branch="feat/login",
            )

    async def test_retry_failure_falls_through_to_existing_error_handling(self) -> None:
        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.worktree.Path.exists", return_value=False),
            patch("sova.git.worktree.Path.mkdir"),
        ):
            mock_run.side_effect = [
                _shell_ok(),  # Layer 1 prune
                _shell_fail(stderr="fatal: is missing but already registered"),  # first add fails
                _shell_ok(),  # Layer 2 retry prune
                _shell_fail(stderr="fatal: is already checked out at '/other/wt'"),  # retry also fails
            ]
            with pytest.raises(RuntimeError):
                await create_worktree(
                    issue_id="42",
                    branch="feat/login",
                    base_branch="main",
                    project_dir=Path("/repo"),
                )
            assert mock_run.call_count == 4


class TestCreateWorktreeCopyFiles:
    async def test_copy_files_param_triggers_copy(self) -> None:
        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.worktree.Path.exists", return_value=False),
            patch("sova.git.worktree.Path.mkdir"),
            patch("sova.git.worktree._copy_worktree_files") as mock_copy,
            patch("sova.git.worktree._copy_claude_artifacts"),
            patch("sova.git.worktree._ensure_compose_project_name"),
        ):
            mock_run.return_value = _shell_ok()
            await create_worktree(
                issue_id="42",
                branch="feat/login",
                base_branch="main",
                project_dir=Path("/repo"),
                copy_files=["sova.toml"],
            )
            mock_copy.assert_called_once_with(
                Path("/repo"),
                Path("/repo") / WORKTREE_DIR / "42",
                ["sova.toml"],
            )
