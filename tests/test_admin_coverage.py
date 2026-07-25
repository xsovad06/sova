"""Tests covering uncovered paths in sova/cli/commands/admin.py."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer

from sova.cli.commands.admin import (
    _cleanup,
    _costs,
    _preview_stale_worktrees,
    _remove_worktrees,
    _status,
)
from sova.utils.shell import ShellResult


def _shell_ok(stdout: str = "", stderr: str = "") -> ShellResult:
    return ShellResult(returncode=0, stdout=stdout, stderr=stderr)


def _shell_fail(stderr: str = "error") -> ShellResult:
    return ShellResult(returncode=1, stdout="", stderr=stderr)


def _mock_session_ctx(execute_results):
    """Build a mock async session context manager with given execute results."""
    mock_session = AsyncMock()
    side = execute_results if isinstance(execute_results, list) else [execute_results]
    mock_session.execute = AsyncMock(side_effect=side)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock()
    mock_session.begin.return_value.__aenter__ = AsyncMock(return_value=None)
    mock_session.begin.return_value.__aexit__ = AsyncMock(return_value=False)
    mock_get = AsyncMock(return_value=mock_session)
    return mock_get


class TestStatusCommand:
    async def test_status_no_runs(self, tmp_path: Path) -> None:
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_get = _mock_session_ctx(mock_result)

        with (
            patch("sova.db.session.init_db", new_callable=AsyncMock),
            patch("sova.db.session.get_session", mock_get),
            patch("sova.cli.commands.admin.console") as mock_console,
        ):
            await _status(project_dir=tmp_path)
            assert any("No task runs" in str(c) for c in mock_console.print.call_args_list)

    async def test_status_with_runs(self, tmp_path: Path) -> None:
        mock_run = MagicMock()
        mock_run.id = 1
        mock_run.issue_number = 42
        mock_run.role = "developer"
        mock_run.status = "done"
        mock_run.total_cost_usd = Decimal("0.05")
        mock_run.started_at = datetime(2026, 1, 1, 12, 0)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_run]
        mock_get = _mock_session_ctx(mock_result)

        with (
            patch("sova.db.session.init_db", new_callable=AsyncMock),
            patch("sova.db.session.get_session", mock_get),
            patch("sova.cli.commands.admin.console") as mock_console,
        ):
            await _status(project_dir=tmp_path)
            assert mock_console.print.called


class TestCostsCommand:
    async def test_costs_empty(self, tmp_path: Path) -> None:
        total_result = MagicMock()
        total_result.scalar.return_value = None
        model_result = MagicMock()
        model_result.all.return_value = []
        recent_result = MagicMock()
        recent_result.scalars.return_value.all.return_value = []

        mock_get = _mock_session_ctx([total_result, model_result, recent_result])

        with (
            patch("sova.db.session.init_db", new_callable=AsyncMock),
            patch("sova.db.session.get_session", mock_get),
            patch("sova.cli.commands.admin.console") as mock_console,
        ):
            await _costs(project_dir=tmp_path)
            assert mock_console.print.called

    async def test_costs_with_data(self, tmp_path: Path) -> None:
        total_result = MagicMock()
        total_result.scalar.return_value = Decimal("1.2345")
        model_result = MagicMock()
        model_result.all.return_value = [("claude-sonnet", Decimal("1.0"), 5)]
        rec = MagicMock()
        rec.phase = "develop"
        rec.issue = "42"
        rec.model = "claude-sonnet"
        rec.cost_usd = Decimal("0.25")
        rec.input_tokens = 1000
        rec.output_tokens = 500
        recent_result = MagicMock()
        recent_result.scalars.return_value.all.return_value = [rec]

        mock_get = _mock_session_ctx([total_result, model_result, recent_result])

        with (
            patch("sova.db.session.init_db", new_callable=AsyncMock),
            patch("sova.db.session.get_session", mock_get),
            patch("sova.cli.commands.admin.console") as mock_console,
        ):
            await _costs(project_dir=tmp_path)
            assert mock_console.print.call_count >= 2


class TestCleanupCommand:
    async def test_cleanup_no_stale(self, tmp_path: Path) -> None:
        with (
            patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run,
            patch("sova.cli.commands.admin.console") as mock_console,
        ):
            mock_run.return_value = _shell_ok(stdout="worktree /repo\nbranch refs/heads/main\n")
            await _cleanup(project_dir=tmp_path, dry_run=False, clean_logs=False)
            assert any("No stale" in str(c) for c in mock_console.print.call_args_list)

    async def test_cleanup_dry_run(self, tmp_path: Path) -> None:
        with (
            patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run,
            patch("sova.cli.commands.admin.console") as mock_console,
        ):
            mock_run.return_value = _shell_ok(
                stdout="worktree /repo/.claude/worktrees/42\nbranch refs/heads/feat/issue-42\n"
            )
            await _cleanup(project_dir=tmp_path, dry_run=True, clean_logs=False)
            assert any("Would remove" in str(c) for c in mock_console.print.call_args_list)

    async def test_cleanup_git_failure(self, tmp_path: Path) -> None:
        with (
            patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run,
            patch("sova.cli.commands.admin.console"),
        ):
            mock_run.return_value = _shell_fail()
            with pytest.raises(typer.Exit):
                await _cleanup(project_dir=tmp_path, dry_run=False, clean_logs=False)


class TestPreviewStaleWorktrees:
    def test_preview_output(self) -> None:
        with patch("sova.cli.commands.admin.console") as mock_console:
            stale = [
                {"path": "/repo/.claude/worktrees/42", "branch": "refs/heads/feat/issue-42"},
                {"path": "/repo/.claude/worktrees/99"},
            ]
            _preview_stale_worktrees(stale)
            assert mock_console.print.call_count == 3


class TestRemoveWorktrees:
    async def test_remove_success(self) -> None:
        with (
            patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run,
            patch("sova.cli.commands.admin.console") as mock_console,
        ):
            mock_run.return_value = _shell_ok()
            stale = [{"path": "/repo/.claude/worktrees/42"}]
            await _remove_worktrees(stale, Path("/repo"))
            assert any("Removed:" in str(c) for c in mock_console.print.call_args_list)

    async def test_remove_failure(self) -> None:
        with (
            patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run,
            patch("sova.cli.commands.admin.console") as mock_console,
        ):
            mock_run.return_value = _shell_fail(stderr="locked")
            stale = [{"path": "/repo/.claude/worktrees/42"}]
            await _remove_worktrees(stale, Path("/repo"))
            assert any("Failed" in str(c) for c in mock_console.print.call_args_list)


class TestCleanupRemoveAndLogs:
    async def test_cleanup_removes_stale_worktrees(self, tmp_path: Path) -> None:
        remove_result = _shell_ok()
        call_count = 0

        async def fake_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _shell_ok(stdout="worktree /repo/.claude/worktrees/42\nbranch refs/heads/feat/issue-42\n")
            return remove_result

        with (
            patch("sova.utils.shell.run", side_effect=fake_run),
            patch("sova.cli.commands.admin.console"),
        ):
            await _cleanup(project_dir=tmp_path, dry_run=False, clean_logs=False)
        assert call_count == 2

    async def test_cleanup_with_clean_logs(self, tmp_path: Path) -> None:
        with (
            patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run,
            patch("sova.cli.commands.admin.console"),
            patch("sova.db.session.init_db", new_callable=AsyncMock),
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.core.output.cleanup_old_output", new_callable=AsyncMock, return_value=5) as mock_cleanup,
        ):
            mock_cfg.return_value.output.retention_days = 30
            mock_run.return_value = _shell_ok(stdout="worktree /repo\nbranch refs/heads/main\n")
            await _cleanup(project_dir=tmp_path, dry_run=False, clean_logs=True)
        mock_cleanup.assert_awaited_once_with(tmp_path, 30)


class TestCleanupRunAll:
    async def test_run_all_dry_run(self, tmp_path: Path) -> None:
        from sova.git.worktree import GCResult

        gc = GCResult(worktrees_removed=2, branches_removed=1)
        with (
            patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run,
            patch("sova.cli.commands.admin.console") as mock_console,
            patch("sova.git.worktree.cleanup_by_issue_state", new_callable=AsyncMock, return_value=gc) as mock_gc,
        ):
            mock_run.return_value = _shell_ok(stdout="worktree /repo\nbranch refs/heads/main\n")
            await _cleanup(project_dir=tmp_path, dry_run=True, clean_logs=False, run_all=True)
            mock_gc.assert_awaited_once_with(project_dir=tmp_path, dry_run=True)
            output = " ".join(str(c) for c in mock_console.print.call_args_list)
            assert "Would remove 2 worktree(s)" in output

    async def test_run_all_removes_items(self, tmp_path: Path) -> None:
        from sova.git.worktree import GCResult

        gc = GCResult(worktrees_removed=1, branches_removed=3)
        with (
            patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run,
            patch("sova.cli.commands.admin.console") as mock_console,
            patch("sova.git.worktree.cleanup_by_issue_state", new_callable=AsyncMock, return_value=gc),
        ):
            mock_run.return_value = _shell_ok(stdout="worktree /repo\nbranch refs/heads/main\n")
            await _cleanup(project_dir=tmp_path, dry_run=False, clean_logs=False, run_all=True)
            output = " ".join(str(c) for c in mock_console.print.call_args_list)
            assert "Removed 1 worktree(s)" in output

    async def test_run_all_nothing_to_clean(self, tmp_path: Path) -> None:
        from sova.git.worktree import GCResult

        gc = GCResult()
        with (
            patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run,
            patch("sova.cli.commands.admin.console") as mock_console,
            patch("sova.git.worktree.cleanup_by_issue_state", new_callable=AsyncMock, return_value=gc),
        ):
            mock_run.return_value = _shell_ok(stdout="worktree /repo\nbranch refs/heads/main\n")
            await _cleanup(project_dir=tmp_path, dry_run=False, clean_logs=False, run_all=True)
            output = " ".join(str(c) for c in mock_console.print.call_args_list)
            assert "No stale worktrees or branches for closed issues" in output

    async def test_run_all_with_stashes(self, tmp_path: Path) -> None:
        from sova.git.worktree import GCResult

        gc = GCResult(stashes_found=["stash@{0}: On main: WIP", "stash@{1}: On feat: save"])
        with (
            patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run,
            patch("sova.cli.commands.admin.console") as mock_console,
            patch("sova.git.worktree.cleanup_by_issue_state", new_callable=AsyncMock, return_value=gc),
        ):
            mock_run.return_value = _shell_ok(stdout="worktree /repo\nbranch refs/heads/main\n")
            await _cleanup(project_dir=tmp_path, dry_run=False, clean_logs=False, run_all=True)
            output = " ".join(str(c) for c in mock_console.print.call_args_list)
            assert "2 stash(es)" in output

    async def test_run_all_with_errors(self, tmp_path: Path) -> None:
        from sova.git.worktree import GCResult

        gc = GCResult(errors=["Failed to delete branch feat/issue-42: locked"])
        with (
            patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run,
            patch("sova.cli.commands.admin.console") as mock_console,
            patch("sova.git.worktree.cleanup_by_issue_state", new_callable=AsyncMock, return_value=gc),
        ):
            mock_run.return_value = _shell_ok(stdout="worktree /repo\nbranch refs/heads/main\n")
            with pytest.raises(typer.Exit):
                await _cleanup(project_dir=tmp_path, dry_run=False, clean_logs=False, run_all=True)
            output = " ".join(str(c) for c in mock_console.print.call_args_list)
            assert "Failed to delete branch" in output


class TestCleanupCLIWiring:
    def test_all_flag_wires_to_cleanup_by_issue_state(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from sova.cli.app import app
        from sova.git.worktree import GCResult

        runner = CliRunner()
        gc = GCResult(worktrees_removed=1, branches_removed=2)
        with (
            patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run,
            patch("sova.cli.commands.admin.console"),
            patch("sova.git.worktree.cleanup_by_issue_state", new_callable=AsyncMock, return_value=gc) as mock_gc,
        ):
            mock_run.return_value = _shell_ok(stdout="worktree /repo\nbranch refs/heads/main\n")
            result = runner.invoke(app, ["cleanup", "--all", "--dry-run", "--project", str(tmp_path)])
            assert result.exit_code == 0
            mock_gc.assert_awaited_once_with(project_dir=tmp_path, dry_run=True)
