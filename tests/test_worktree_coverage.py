"""Tests covering uncovered paths in sova/git/worktree.py."""

from __future__ import annotations

import os
import shutil
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
    ensure_worktree_usable,
    missing_claude_artifacts,
)
from sova.utils.shell import ShellResult
from sova.utils.shell import run as run_shell


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

    def test_reruns_cleanly_when_worktree_command_symlinks_same_global_target(self, tmp_path: Path) -> None:
        global_target = tmp_path / "global-commands"
        global_target.mkdir()
        shared_cmd = global_target / "optimize-knowledge.md"
        shared_cmd.write_text("shared command")

        project = tmp_path / "project"
        commands_dir = project / ".claude" / "commands"
        commands_dir.mkdir(parents=True)
        (commands_dir / "optimize-knowledge.md").symlink_to(shared_cmd)
        (commands_dir / "regular.md").write_text("regular command")

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        wt_commands = worktree / ".claude" / "commands"
        wt_commands.mkdir(parents=True)
        (wt_commands / "optimize-knowledge.md").symlink_to(shared_cmd)

        with patch("sova.git.worktree.log.warning") as mock_warning:
            _copy_claude_artifacts(project, worktree)

        assert (wt_commands / "regular.md").read_text() == "regular command"
        assert (wt_commands / "optimize-knowledge.md").resolve() == shared_cmd.resolve()
        mock_warning.assert_not_called()


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
        from sova.git.worktree import check_worktree_active_agent

        original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

        def fake_import(name, *args, **kwargs):
            if name == "sqlalchemy":
                raise ImportError("no sqlalchemy")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            result = await check_worktree_active_agent(Path("/fake/wt"))
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


class TestMissingClaudeArtifacts:
    def test_no_claude_dir_in_project_reports_nothing_missing(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        assert missing_claude_artifacts(project, worktree) == []

    def test_healthy_worktree_reports_nothing_missing(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        (project / ".claude" / "commands").mkdir(parents=True)
        (project / ".claude" / "commands" / "dev.md").write_text("cmd")
        (project / "CLAUDE.md").write_text("# Instructions")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        _copy_claude_artifacts(project, worktree)
        assert missing_claude_artifacts(project, worktree) == []

    def test_missing_commands_dir_is_reported(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        (project / ".claude" / "commands").mkdir(parents=True)
        (project / ".claude" / "commands" / "dev.md").write_text("cmd")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        assert missing_claude_artifacts(project, worktree) == [".claude/commands"]

    def test_empty_commands_dir_counts_as_missing(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        (project / ".claude" / "commands").mkdir(parents=True)
        (project / ".claude" / "commands" / "dev.md").write_text("cmd")
        worktree = tmp_path / "worktree"
        (worktree / ".claude" / "commands").mkdir(parents=True)
        assert missing_claude_artifacts(project, worktree) == [".claude/commands"]

    def test_project_without_optional_dir_is_never_flagged(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        (project / ".claude" / "commands").mkdir(parents=True)
        (project / ".claude" / "commands" / "dev.md").write_text("cmd")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        _copy_claude_artifacts(project, worktree)
        # Project never had .claude/skills; a worktree missing it is healthy.
        assert not (project / ".claude" / "skills").exists()
        assert missing_claude_artifacts(project, worktree) == []

    def test_broken_symlink_counts_as_missing(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        (project / "CLAUDE.md").write_text("# Instructions")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "CLAUDE.md").symlink_to(tmp_path / "does-not-exist.md")
        assert missing_claude_artifacts(project, worktree) == ["CLAUDE.md"]

    def test_valid_symlink_counts_as_present(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        (project / "CLAUDE.md").write_text("# Instructions")
        shared = tmp_path / "shared-CLAUDE.md"
        shared.write_text("# Shared")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "CLAUDE.md").symlink_to(shared)
        assert missing_claude_artifacts(project, worktree) == []

    def test_missing_settings_json_is_reported(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        (project / ".claude").mkdir(parents=True)
        (project / ".claude" / "settings.json").write_text("{}")
        worktree = tmp_path / "worktree"
        (worktree / ".claude").mkdir(parents=True)
        assert missing_claude_artifacts(project, worktree) == [".claude/settings.json"]

    def test_broken_symlink_inside_commands_dir_counts_as_missing(self, tmp_path: Path) -> None:
        """A broken symlink entry inside .claude/commands must not satisfy the subset check."""
        project = tmp_path / "project"
        (project / ".claude" / "commands").mkdir(parents=True)
        (project / ".claude" / "commands" / "dev.md").write_text("cmd")
        worktree = tmp_path / "worktree"
        (worktree / ".claude" / "commands").mkdir(parents=True)
        (worktree / ".claude" / "commands" / "dev.md").symlink_to(tmp_path / "does-not-exist.md")
        assert missing_claude_artifacts(project, worktree) == [".claude/commands"]

    def test_valid_symlink_inside_commands_dir_counts_as_present(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        (project / ".claude" / "commands").mkdir(parents=True)
        (project / ".claude" / "commands" / "dev.md").write_text("cmd")
        shared = tmp_path / "shared-dev.md"
        shared.write_text("cmd")
        worktree = tmp_path / "worktree"
        (worktree / ".claude" / "commands").mkdir(parents=True)
        (worktree / ".claude" / "commands" / "dev.md").symlink_to(shared)
        assert missing_claude_artifacts(project, worktree) == []


class TestEnsureWorktreeUsable:
    async def test_healthy_worktree_returns_unchanged(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        (project / ".claude" / "commands").mkdir(parents=True)
        (project / ".claude" / "commands" / "dev.md").write_text("cmd")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / ".git").touch()
        _copy_claude_artifacts(project, worktree)

        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock, return_value=_shell_ok()) as mock_run,
            patch("sova.git.worktree.create_worktree", new_callable=AsyncMock) as mock_create,
        ):
            result = await ensure_worktree_usable(project, worktree, branch="feat/issue-1")

        assert result == worktree
        mock_run.assert_awaited_once_with("git", "rev-parse", "--git-dir", cwd=worktree)
        mock_create.assert_not_awaited()

    async def test_missing_artifacts_are_repopulated(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        (project / ".claude" / "commands").mkdir(parents=True)
        (project / ".claude" / "commands" / "dev.md").write_text("cmd")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / ".git").touch()

        with patch("sova.git.worktree.run", new_callable=AsyncMock, return_value=_shell_ok()):
            result = await ensure_worktree_usable(project, worktree, branch="feat/issue-1")

        assert result == worktree
        assert (worktree / ".claude" / "commands" / "dev.md").read_text() == "cmd"

    async def test_missing_directory_recreates_worktree(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        worktree = project / ".claude" / "worktrees" / "42"
        fake_info = WorktreeInfo(path=worktree, branch="feat/issue-42", issue_id="42")

        with patch("sova.git.worktree.create_worktree", new_callable=AsyncMock, return_value=fake_info) as mock_create:
            result = await ensure_worktree_usable(project, worktree, branch="feat/issue-42")

        assert result == worktree
        mock_create.assert_awaited_once_with(
            issue_id="42",
            branch="feat/issue-42",
            base_branch="HEAD",
            project_dir=project,
        )

    async def test_broken_git_linkage_recreates_worktree(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        fake_info = WorktreeInfo(path=worktree, branch="feat/issue-42", issue_id="worktree")

        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock, return_value=_shell_fail()),
            patch("sova.git.worktree.create_worktree", new_callable=AsyncMock, return_value=fake_info) as mock_create,
        ):
            result = await ensure_worktree_usable(project, worktree, branch="feat/issue-42")

        assert result == worktree
        mock_create.assert_awaited_once()

    async def test_noncanonical_path_cleans_up_old_location_on_recreate(self, tmp_path: Path) -> None:
        """A worktree found via find_worktree_by_branch can live anywhere on disk.

        Recreating always targets the canonical .claude/worktrees/<name> path,
        so the non-canonical original must be explicitly cleaned up first
        rather than silently orphaned (never reclaimed by
        cleanup_stale_worktrees, which only scans the canonical directory).
        """
        project = tmp_path / "project"
        project.mkdir()
        # A worktree living outside the canonical .claude/worktrees/ layout,
        # e.g. one created manually or via Claude Code's EnterWorktree flow.
        worktree = tmp_path / "elsewhere" / "42"
        worktree.mkdir(parents=True)
        canonical_path = project / ".claude" / "worktrees" / "42"
        fake_info = WorktreeInfo(path=canonical_path, branch="feat/issue-42", issue_id="42")

        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock, return_value=_shell_fail()),
            patch("sova.git.worktree.cleanup_worktree", new_callable=AsyncMock) as mock_cleanup,
            patch("sova.git.worktree.create_worktree", new_callable=AsyncMock, return_value=fake_info) as mock_create,
        ):
            result = await ensure_worktree_usable(project, worktree, branch="feat/issue-42")

        assert result == canonical_path
        mock_cleanup.assert_awaited_once_with(worktree, cwd=project)
        mock_create.assert_awaited_once_with(
            issue_id="42",
            branch="feat/issue-42",
            base_branch="HEAD",
            project_dir=project,
        )

    async def test_canonical_path_does_not_trigger_cleanup_on_recreate(self, tmp_path: Path) -> None:
        """When the worktree already lives at the canonical path, no relocation cleanup is needed."""
        project = tmp_path / "project"
        project.mkdir()
        worktree = project / ".claude" / "worktrees" / "42"
        worktree.mkdir(parents=True)
        fake_info = WorktreeInfo(path=worktree, branch="feat/issue-42", issue_id="42")

        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock, return_value=_shell_fail()),
            patch("sova.git.worktree.cleanup_worktree", new_callable=AsyncMock) as mock_cleanup,
            patch("sova.git.worktree.create_worktree", new_callable=AsyncMock, return_value=fake_info),
        ):
            result = await ensure_worktree_usable(project, worktree, branch="feat/issue-42")

        assert result == worktree
        mock_cleanup.assert_not_awaited()

    async def test_unusable_with_unknown_branch_returns_none(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        worktree = project / ".claude" / "worktrees" / "42"

        with patch("sova.git.worktree.create_worktree", new_callable=AsyncMock) as mock_create:
            result = await ensure_worktree_usable(project, worktree, branch="")

        assert result is None
        mock_create.assert_not_awaited()

    async def test_recreate_failure_returns_none(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        worktree = project / ".claude" / "worktrees" / "42"

        with patch("sova.git.worktree.create_worktree", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
            result = await ensure_worktree_usable(project, worktree, branch="feat/issue-42")

        assert result is None

    async def test_unrepairable_missing_artifacts_returns_none(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        (project / ".claude" / "commands").mkdir(parents=True)
        (project / ".claude" / "commands" / "dev.md").write_text("cmd")
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / ".git").touch()

        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock, return_value=_shell_ok()),
            patch("sova.git.worktree.ensure_claude_artifacts"),
        ):
            result = await ensure_worktree_usable(project, worktree, branch="feat/issue-1")

        assert result is None

    async def test_unrepairable_noncritical_artifact_still_returns_worktree(self, tmp_path: Path) -> None:
        """A non-.claude/commands artifact that can't be repopulated must not discard the worktree."""
        project = tmp_path / "project"
        (project / ".claude" / "commands").mkdir(parents=True)
        (project / ".claude" / "commands" / "dev.md").write_text("cmd")
        (project / ".claude" / "rules").mkdir(parents=True)
        (project / ".claude" / "rules" / "arch.md").write_text("rules")
        worktree = tmp_path / "worktree"
        (worktree / ".claude" / "commands").mkdir(parents=True)
        (worktree / ".claude" / "commands" / "dev.md").write_text("cmd")
        (worktree / ".git").touch()
        # rules is entirely absent from the worktree and stays that way: the
        # repopulate attempt is mocked to a no-op, matching a real unrepairable
        # failure (e.g. permission denied) rather than a transient one.
        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock, return_value=_shell_ok()),
            patch("sova.git.worktree.ensure_claude_artifacts"),
        ):
            result = await ensure_worktree_usable(project, worktree, branch="feat/issue-1")

        assert result == worktree
        assert not (worktree / ".claude" / "rules").exists()

    async def test_is_dir_permission_error_triggers_recreate(self, tmp_path: Path) -> None:
        """An OSError from Path.is_dir() on the worktree path is treated as needing recreate, not raised."""
        project = tmp_path / "project"
        project.mkdir()
        worktree = project / ".claude" / "worktrees" / "42"
        fake_info = WorktreeInfo(path=worktree, branch="feat/issue-42", issue_id="42")
        real_is_dir = Path.is_dir

        def _raise_for_worktree(self: Path) -> bool:
            if self == worktree:
                raise PermissionError("denied")
            return real_is_dir(self)

        with (
            patch.object(Path, "is_dir", _raise_for_worktree),
            patch("sova.git.worktree.create_worktree", new_callable=AsyncMock, return_value=fake_info) as mock_create,
        ):
            result = await ensure_worktree_usable(project, worktree, branch="feat/issue-42")

        assert result == worktree
        mock_create.assert_awaited_once()


class TestResolveIssueWorktreeOSErrorHandling:
    """A filesystem probe failure inside _resolve_issue_worktree() must degrade to project_dir, never raise.

    Regression coverage: the issue-id-based worktree reuse block had no
    try/except around ``candidate.is_dir()`` or the ``ensure_worktree_usable()``
    call, so a PermissionError there would propagate out of
    ``_resolve_issue_worktree()`` and crash ``start_agent()``/``start_command()``
    instead of degrading to ``project_dir``.
    """

    async def test_candidate_is_dir_oserror_falls_through(self, tmp_path: Path) -> None:
        from sova.dashboard.services.agent_context import _resolve_issue_worktree

        project = tmp_path / "project"
        project.mkdir()
        worktree = project / ".claude" / "worktrees" / "42"
        real_is_dir = Path.is_dir

        def _raise_for_worktree(self: Path) -> bool:
            if self == worktree:
                raise PermissionError("denied")
            return real_is_dir(self)

        with patch.object(Path, "is_dir", _raise_for_worktree):
            result = await _resolve_issue_worktree("42", project)

        assert result == project

    async def test_ensure_worktree_usable_oserror_falls_through(self, tmp_path: Path) -> None:
        from sova.dashboard.services.agent_context import _resolve_issue_worktree

        project = tmp_path / "project"
        worktree = project / ".claude" / "worktrees" / "42"
        worktree.mkdir(parents=True)

        with patch(
            "sova.dashboard.services.agent_context.ensure_worktree_usable",
            new_callable=AsyncMock,
            side_effect=PermissionError("denied"),
        ):
            result = await _resolve_issue_worktree("42", project)

        assert result == project

    async def test_unusable_issue_id_worktree_is_not_reprobed_via_branch_lookup(self, tmp_path: Path) -> None:
        """When the branch-based lookup resolves to the same path already probed via the
        issue-id branch, it must not be probed a second time (duplicate git subprocess +
        possible repopulate attempt for a result already known).
        """
        from sova.dashboard.services.agent_context import _resolve_issue_worktree

        project = tmp_path / "project"
        worktree = project / ".claude" / "worktrees" / "42"
        worktree.mkdir(parents=True)

        with (
            patch(
                "sova.dashboard.services.agent_context.ensure_worktree_usable",
                new_callable=AsyncMock,
                return_value=None,
            ) as mock_usable,
            patch(
                "sova.dashboard.services.agent_context.find_worktree_by_branch",
                new_callable=AsyncMock,
                return_value=worktree,
            ),
        ):
            result = await _resolve_issue_worktree("42", project, branch_name="feat/issue-42")

        assert result == project
        mock_usable.assert_awaited_once()


class TestEnsureWorktreeUsableRealWorktree:
    """Integration-style tests against a real git repo and a real, git-linked worktree.

    Regression coverage for a bug where ensure_worktree_usable() mocked out at
    the unit-test level hid the caller's real fallthrough behavior: a real,
    still-checked-out worktree that ensure_worktree_usable() could not repair
    used to make _resolve_issue_worktree() (sova/dashboard/services/agent_context.py)
    fall through to creating a brand-new worktree under a different identity
    for a branch git already has checked out elsewhere, which git refuses.
    """

    async def _init_repo(self, project: Path) -> None:
        await run_shell("git", "init", "-q", "-b", "main", cwd=project)
        await run_shell("git", "config", "user.email", "test@example.com", cwd=project)
        await run_shell("git", "config", "user.name", "Test User", cwd=project)
        (project / ".claude" / "commands").mkdir(parents=True)
        (project / ".claude" / "commands" / "dev.md").write_text("cmd")
        (project / ".claude" / "rules").mkdir(parents=True)
        (project / ".claude" / "rules" / "arch.md").write_text("rules")
        (project / "README.md").write_text("hello")
        await run_shell("git", "add", "-A", cwd=project)
        await run_shell("git", "commit", "-q", "-m", "init", cwd=project)

    async def test_real_worktree_missing_unrepairable_noncritical_artifact_is_reused(self, tmp_path: Path) -> None:
        """A real, git-valid worktree missing only .claude/rules (unrepairable) is reused, not discarded."""
        project = tmp_path / "project"
        project.mkdir()
        await self._init_repo(project)

        info = await create_worktree(issue_id="42", branch="feat/issue-42", base_branch="HEAD", project_dir=project)
        worktree = info.path
        assert (worktree / ".claude" / "rules" / "arch.md").is_file()

        shutil.rmtree(worktree / ".claude" / "rules")
        real_copytree = shutil.copytree

        def _fail_on_rules(src: str, dst: str, **kwargs: object) -> str:
            if Path(src).name == "rules":
                raise OSError("permission denied")
            return real_copytree(src, dst, **kwargs)

        with patch("sova.git.worktree.shutil.copytree", side_effect=_fail_on_rules):
            result = await ensure_worktree_usable(project, worktree, branch="feat/issue-42")

        assert result == worktree
        assert not (worktree / ".claude" / "rules").exists()

    async def test_resolver_falls_back_to_project_dir_without_conflicting_create(self, tmp_path: Path) -> None:
        """_resolve_issue_worktree must not attempt a conflicting create_worktree() call.

        The branch is genuinely checked out at a real worktree whose
        .claude/commands (critical) cannot be repopulated. The resolver must
        fall back to project_dir directly rather than trying to create a
        second worktree for the same already-checked-out branch under a
        different identity, which git would refuse.
        """
        from sova.dashboard.services.agent_context import _resolve_issue_worktree

        project = tmp_path / "project"
        project.mkdir()
        await self._init_repo(project)

        info = await create_worktree(issue_id="pr-99", branch="feat/issue-42", base_branch="HEAD", project_dir=project)
        worktree = info.path
        shutil.rmtree(worktree / ".claude" / "commands")

        with patch("sova.git.worktree.ensure_claude_artifacts"):
            result = await _resolve_issue_worktree("", project, branch_name="feat/issue-42", pr_number=99)

        assert result == project

    async def test_missing_git_file_recreates_worktree_not_walk_up_false_positive(self, tmp_path: Path) -> None:
        """A worktree whose ``.git`` file was deleted entirely must be recreated, not reused.

        The canonical worktree location is nested directly under project_dir,
        which has its own ``.git``. With no ``.git`` marker of its own left
        behind, git's repository discovery would otherwise walk upward and
        resolve to the *primary* checkout's repository, making a naive
        ``git rev-parse --git-dir`` probe run from the worktree directory
        report success even though the directory has no independent worktree
        linkage. This must be caught before ensure_worktree_usable() reports
        the directory as usable.
        """
        project = tmp_path / "project"
        project.mkdir()
        await self._init_repo(project)

        info = await create_worktree(issue_id="55", branch="feat/issue-55", base_branch="HEAD", project_dir=project)
        worktree = info.path
        assert (worktree / ".git").exists()

        (worktree / ".git").unlink()

        # Confirm the walk-up would otherwise silently succeed against the
        # primary checkout's repository, which is exactly the false positive
        # this test guards against.
        walked_up = await run_shell("git", "rev-parse", "--git-dir", cwd=worktree)
        assert walked_up.success

        result = await ensure_worktree_usable(project, worktree, branch="feat/issue-55")

        assert result == worktree
        assert (worktree / ".git").exists()
        head = await run_shell("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=worktree)
        assert head.stdout.strip() == "feat/issue-55"
        # The original worktree (and its branch checkout) must be left alone --
        # no second worktree should have been created anywhere.
        list_result = await run_shell("git", "worktree", "list", "--porcelain", cwd=project)
        assert list_result.stdout.count("worktree ") == 2  # project_dir itself + the one real worktree

    async def test_directory_deleted_registration_intact_recreates_worktree(self, tmp_path: Path) -> None:
        """A worktree directory removed out from under git, with the registration still intact, is recreated.

        ``git worktree list --porcelain`` keeps reporting a worktree whose
        directory was deleted directly (``rm -rf``, a failed cleanup, disk
        recovery) rather than via ``git worktree remove``. This must go
        through the real recreate path (prune-then-add inside
        create_worktree()) end to end, with no mocking, unlike
        test_missing_directory_recreates_worktree above which only proves the
        caller's branching logic against a fully mocked create_worktree().
        """
        project = tmp_path / "project"
        project.mkdir()
        await self._init_repo(project)

        info = await create_worktree(issue_id="66", branch="feat/issue-66", base_branch="HEAD", project_dir=project)
        worktree = info.path
        assert worktree.is_dir()

        list_before = await run_shell("git", "worktree", "list", "--porcelain", cwd=project)
        assert str(worktree) in list_before.stdout

        shutil.rmtree(worktree)
        assert not worktree.exists()
        # The registration survives a direct directory removal: git only
        # forgets it on `git worktree remove`/`prune`.
        list_after_rm = await run_shell("git", "worktree", "list", "--porcelain", cwd=project)
        assert str(worktree) in list_after_rm.stdout

        result = await ensure_worktree_usable(project, worktree, branch="feat/issue-66")

        assert result == worktree
        assert worktree.is_dir()
        assert (worktree / ".git").exists()
        head = await run_shell("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=worktree)
        assert head.stdout.strip() == "feat/issue-66"
        list_result = await run_shell("git", "worktree", "list", "--porcelain", cwd=project)
        assert list_result.stdout.count("worktree ") == 2  # project_dir itself + the recreated worktree
