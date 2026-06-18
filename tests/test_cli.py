"""Tests for sova.cli -- CLI commands and Typer app."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from sova.adapters.base import Task, TaskState
from sova.config.models import ProjectConfig
from sova.db.session import close_db, init_db

runner = CliRunner()


@pytest.fixture(autouse=True)
async def setup_db():
    """Initialize an in-memory DB for CLI tests."""
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


def _mock_adapter(state: TaskState = TaskState.BACKLOG) -> AsyncMock:
    adapter = AsyncMock()
    adapter.get_state.return_value = state
    adapter.get_task.return_value = Task(
        id="42",
        title="Test issue",
        body="Some description",
        state=state,
    )
    adapter.list_tasks.return_value = [
        Task(id="1", title="First issue", body="Body 1", state=TaskState.BACKLOG),
        Task(id="2", title="Second issue", body="", state=TaskState.BACKLOG),
    ]
    return adapter


# ---------------------------------------------------------------------------
# App-level tests
# ---------------------------------------------------------------------------


class TestAppHelp:
    def test_help_shows_all_commands(self) -> None:
        from sova.cli.app import app

        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "run" in result.output
        assert "triage" in result.output
        assert "status" in result.output
        assert "costs" in result.output
        assert "cleanup" in result.output
        assert "memory" in result.output

    def test_version(self) -> None:
        from sova.cli.app import app

        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "sova" in result.output

    def test_no_args_shows_help(self) -> None:
        from sova.cli.app import app

        result = runner.invoke(app, [])
        # Typer returns exit code 0 or 2 for no_args_is_help depending on version
        assert result.exit_code in (0, 2)
        assert "run" in result.output or "Usage" in result.output


# ---------------------------------------------------------------------------
# Triage command
# ---------------------------------------------------------------------------


class TestTriageCommand:
    def test_triage_help(self) -> None:
        from sova.cli.app import app

        result = runner.invoke(app, ["triage", "--help"])
        assert result.exit_code == 0
        assert "triage" in result.output.lower() or "issue" in result.output.lower()

    @patch("sova.cli.commands.triage.create_adapter")
    @patch("sova.cli.commands.triage.load_config")
    def test_triage_single_issue(self, mock_config, mock_adapter_factory) -> None:
        from sova.cli.app import app

        mock_config.return_value = ProjectConfig(github_repo="owner/repo")
        adapter = _mock_adapter(TaskState.BACKLOG)
        mock_adapter_factory.return_value = adapter

        result = runner.invoke(app, ["triage", "--issue", "42"])
        assert result.exit_code == 0
        adapter.get_task.assert_called()

    @patch("sova.cli.commands.triage.create_adapter")
    @patch("sova.cli.commands.triage.load_config")
    def test_triage_all_issues(self, mock_config, mock_adapter_factory) -> None:
        from sova.cli.app import app

        mock_config.return_value = ProjectConfig(github_repo="owner/repo")
        adapter = _mock_adapter(TaskState.BACKLOG)
        mock_adapter_factory.return_value = adapter

        result = runner.invoke(app, ["triage"])
        assert result.exit_code == 0
        adapter.list_tasks.assert_called()


# ---------------------------------------------------------------------------
# PR commands
# ---------------------------------------------------------------------------


class TestPRCommands:
    def test_address_pr_help(self) -> None:
        from sova.cli.app import app

        result = runner.invoke(app, ["address-pr", "--help"])
        assert result.exit_code == 0

    def test_maintain_pr_help(self) -> None:
        from sova.cli.app import app

        result = runner.invoke(app, ["maintain-pr", "--help"])
        assert result.exit_code == 0

    def test_review_pr_help(self) -> None:
        from sova.cli.app import app

        result = runner.invoke(app, ["review-pr", "--help"])
        assert result.exit_code == 0

    def test_learn_from_pr_help(self) -> None:
        from sova.cli.app import app

        result = runner.invoke(app, ["learn-from-pr", "--help"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Memory commands
# ---------------------------------------------------------------------------


class TestMemoryCommands:
    def test_memory_search_help(self) -> None:
        from sova.cli.app import app

        result = runner.invoke(app, ["memory", "search", "--help"])
        assert result.exit_code == 0

    def test_memory_prune_help(self) -> None:
        from sova.cli.app import app

        result = runner.invoke(app, ["memory", "prune", "--help"])
        assert result.exit_code == 0

    @patch("sova.knowledge.memory.search")
    @patch("sova.db.session.init_db")
    def test_memory_search_runs(self, mock_init, mock_search) -> None:
        from sova.cli.app import app

        mock_init.return_value = None
        mock_search.return_value = []

        result = runner.invoke(app, ["memory", "search", "test query"])
        assert result.exit_code == 0

    @patch("sova.knowledge.memory.search")
    @patch("sova.db.session.init_db")
    def test_memory_prune_runs(self, mock_init, mock_search) -> None:
        from sova.cli.app import app

        mock_init.return_value = None
        mock_search.return_value = []

        result = runner.invoke(app, ["memory", "prune"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Admin commands
# ---------------------------------------------------------------------------


class TestAdminCommands:
    def test_status_help(self) -> None:
        from sova.cli.app import app

        result = runner.invoke(app, ["status", "--help"])
        assert result.exit_code == 0

    def test_costs_help(self) -> None:
        from sova.cli.app import app

        result = runner.invoke(app, ["costs", "--help"])
        assert result.exit_code == 0

    def test_cleanup_help(self) -> None:
        from sova.cli.app import app

        result = runner.invoke(app, ["cleanup", "--help"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Project commands
# ---------------------------------------------------------------------------


class TestProjectCommands:
    def test_install_help(self) -> None:
        from sova.cli.app import app

        result = runner.invoke(app, ["install", "--help"])
        assert result.exit_code == 0

    def test_setup_help(self) -> None:
        from sova.cli.app import app

        result = runner.invoke(app, ["setup", "--help"])
        assert result.exit_code == 0

    async def test_install_configures_githooks(self, tmp_path: Path) -> None:
        from sova.cli.commands.project import _install

        (tmp_path / ".githooks").mkdir()

        with (
            patch("sova.cli.commands.project.run", new_callable=AsyncMock) as mock_run,
            patch("sova.db.session.init_db", new_callable=AsyncMock),
            patch("sova.commands.distribution.install_commands") as mock_install_cmds,
            patch("sova.commands.distribution.install_guidelines") as mock_install_guides,
            patch("sova.commands.catalog.get_canonical_dir", return_value=tmp_path),
            patch("sova.commands.catalog.get_guidelines_dir", return_value=tmp_path),
            patch("sova.config.loader.load_config"),
        ):
            mock_install_cmds.return_value = MagicMock(installed=0)
            mock_install_guides.return_value = MagicMock(installed=0)
            mock_run.side_effect = [
                MagicMock(success=False, stdout=""),  # git config --get
                MagicMock(success=True),  # git config set
            ]
            await _install(path=tmp_path, no_dashboard=True, update=False)

        set_call = mock_run.call_args_list[1]
        assert set_call[0] == ("git", "config", "core.hooksPath", ".githooks")

    async def test_install_skips_when_hooks_configured(self, tmp_path: Path) -> None:
        from sova.cli.commands.project import _install

        (tmp_path / ".githooks").mkdir()

        with (
            patch("sova.cli.commands.project.run", new_callable=AsyncMock) as mock_run,
            patch("sova.db.session.init_db", new_callable=AsyncMock),
            patch("sova.commands.distribution.install_commands") as mock_install_cmds,
            patch("sova.commands.distribution.install_guidelines") as mock_install_guides,
            patch("sova.commands.catalog.get_canonical_dir", return_value=tmp_path),
            patch("sova.commands.catalog.get_guidelines_dir", return_value=tmp_path),
            patch("sova.config.loader.load_config"),
        ):
            mock_install_cmds.return_value = MagicMock(installed=0)
            mock_install_guides.return_value = MagicMock(installed=0)
            mock_run.return_value = MagicMock(success=True, stdout=".githooks\n")
            await _install(path=tmp_path, no_dashboard=True, update=False)

        assert mock_run.call_count == 1


# ---------------------------------------------------------------------------
# Run command enhancements
# ---------------------------------------------------------------------------


class TestRunCommand:
    def test_run_help(self) -> None:
        from sova.cli.app import app

        result = runner.invoke(app, ["run", "--help"])
        assert result.exit_code == 0
        assert "issue" in result.output.lower()

    def test_watch_help(self) -> None:
        from sova.cli.app import app

        result = runner.invoke(app, ["watch", "--help"])
        assert result.exit_code == 0

    def test_parallel_help(self) -> None:
        from sova.cli.app import app

        result = runner.invoke(app, ["parallel", "--help"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Doctor helper functions
# ---------------------------------------------------------------------------


class TestDoctorHelpers:
    """Tests for extracted helper functions in doctor.py."""

    def test_check_python_version(self) -> None:
        from sova.cli.commands.doctor import _check_python_version

        name, passed, detail, required = _check_python_version()
        assert name == "Python >= 3.12"
        assert isinstance(passed, bool)
        assert "." in detail
        assert required is True

    def test_extract_auth_detail_authenticated(self) -> None:
        from sova.cli.commands.doctor import _extract_auth_detail
        from sova.utils.shell import ShellResult

        result = ShellResult(returncode=0, stdout="Logged in to github.com as testuser\n", stderr="")
        detail = _extract_auth_detail(result, auth_ok=True)
        assert "Logged in" in detail

    def test_extract_auth_detail_not_authenticated(self) -> None:
        from sova.cli.commands.doctor import _extract_auth_detail
        from sova.utils.shell import ShellResult

        result = ShellResult(returncode=1, stdout="", stderr="")
        detail = _extract_auth_detail(result, auth_ok=False)
        assert "not authenticated" in detail

    def test_check_terminal_notifier_non_darwin(self) -> None:
        from unittest.mock import patch

        from sova.cli.commands.doctor import _check_terminal_notifier

        with patch("sova.cli.commands.doctor.platform") as mock_platform:
            mock_platform.system.return_value = "Linux"
            checks = _check_terminal_notifier()
            assert checks == []

    async def test_check_git_found(self) -> None:
        from unittest.mock import patch

        from sova.cli.commands.doctor import _check_git

        with patch("sova.cli.commands.doctor.shutil.which", return_value="/usr/bin/git"):
            with patch("sova.cli.commands.doctor.run", new_callable=AsyncMock) as mock_run:
                from sova.utils.shell import ShellResult

                mock_run.return_value = ShellResult(returncode=0, stdout="git version 2.43.0\n", stderr="")
                checks = await _check_git()
                assert len(checks) == 1
                assert checks[0][0] == "git"
                assert checks[0][1] is True

    async def test_check_git_not_found(self) -> None:
        from unittest.mock import patch

        from sova.cli.commands.doctor import _check_git

        with patch("sova.cli.commands.doctor.shutil.which", return_value=None):
            checks = await _check_git()
            assert len(checks) == 1
            assert checks[0][1] is False
            assert "not found" in checks[0][2]

    async def test_check_gh_cli_not_found(self) -> None:
        from unittest.mock import patch

        from sova.cli.commands.doctor import _check_gh_cli

        with patch("sova.cli.commands.doctor.shutil.which", return_value=None):
            checks = await _check_gh_cli()
            assert len(checks) == 2
            assert checks[0][1] is False
            assert checks[1][1] is False

    async def test_check_claude_cli_not_found(self) -> None:
        from unittest.mock import patch

        from sova.cli.commands.doctor import _check_claude_cli

        with patch("sova.cli.commands.doctor.shutil.which", return_value=None):
            checks = await _check_claude_cli()
            assert len(checks) == 1
            assert checks[0][1] is False

    async def test_check_git_hooks(self, tmp_path: Path) -> None:
        from sova.cli.commands.doctor import _check_git_hooks

        check = await _check_git_hooks(tmp_path)
        assert check[0] == "git hooks"
        assert isinstance(check[1], bool)

    async def test_check_git_hooks_no_githooks_dir(self, tmp_path: Path) -> None:
        from sova.cli.commands.doctor import _check_git_hooks

        check = await _check_git_hooks(tmp_path)
        assert check[0] == "git hooks"
        assert check[1] is True
        assert "not applicable" in check[2]
        assert check[3] is False

    async def test_check_git_hooks_misconfigured(self, tmp_path: Path) -> None:
        from sova.cli.commands.doctor import _check_git_hooks

        (tmp_path / ".githooks").mkdir()

        with patch("sova.cli.commands.doctor.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=False, stdout="")
            check = await _check_git_hooks(tmp_path)

        assert check[0] == "git hooks"
        assert check[1] is False
        assert "not set" in check[2]
        assert check[3] is True

    async def test_check_git_hooks_configured(self, tmp_path: Path) -> None:
        from sova.cli.commands.doctor import _check_git_hooks

        (tmp_path / ".githooks").mkdir()

        with patch("sova.cli.commands.doctor.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=True, stdout=".githooks\n")
            check = await _check_git_hooks(tmp_path)

        assert check[0] == "git hooks"
        assert check[1] is True
        assert check[2] == ".githooks"

    async def test_check_sova_config_no_toml(self, tmp_path: Path) -> None:
        from sova.cli.commands.doctor import _check_sova_config

        checks = await _check_sova_config(tmp_path)
        assert len(checks) == 1
        assert checks[0][0] == "sova.toml"
        assert checks[0][1] is False

    def test_check_github_config(self) -> None:
        from unittest.mock import MagicMock

        from sova.cli.commands.doctor import _check_github_config

        cfg = MagicMock()
        cfg.github_repo = "owner/repo"
        cfg.github_user = "user"
        checks = _check_github_config(cfg)
        assert len(checks) == 2
        assert checks[0][1] is True
        assert checks[1][1] is True

    def test_render_results_all_pass(self) -> None:
        from sova.cli.commands.doctor import _render_results

        checks = [("test", True, "ok", True)]
        _render_results(checks)

    def test_render_results_required_failure(self) -> None:
        from typer import Exit

        from sova.cli.commands.doctor import _render_results

        checks = [("test", False, "fail", True)]
        with pytest.raises(Exit):
            _render_results(checks)

    def test_render_results_optional_warning(self) -> None:
        from sova.cli.commands.doctor import _render_results

        checks = [("test", False, "warn", False)]
        _render_results(checks)

    async def test_check_agent_runtime_available(self, tmp_path: Path) -> None:
        from sova.cli.commands.doctor import _check_agent_runtime

        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.ipc.runtime.create_runtime") as mock_create,
        ):
            mock_cfg.return_value.agent.runtime = "claude-code"
            mock_rt = MagicMock()
            mock_rt.check_available = AsyncMock(return_value=(True, "1.0.0"))
            mock_create.return_value = mock_rt

            checks = await _check_agent_runtime(tmp_path)

        assert len(checks) == 1
        assert checks[0][1] is True
        assert "claude-code" in checks[0][2]

    async def test_check_agent_runtime_not_available(self, tmp_path: Path) -> None:
        from sova.cli.commands.doctor import _check_agent_runtime

        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.ipc.runtime.create_runtime") as mock_create,
        ):
            mock_cfg.return_value.agent.runtime = "aider"
            mock_rt = MagicMock()
            mock_rt.check_available = AsyncMock(return_value=(False, "not found"))
            mock_create.return_value = mock_rt

            checks = await _check_agent_runtime(tmp_path)

        assert len(checks) == 1
        assert checks[0][1] is False

    async def test_check_agent_runtime_unknown_type(self, tmp_path: Path) -> None:
        from sova.cli.commands.doctor import _check_agent_runtime

        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.ipc.runtime.create_runtime", side_effect=ValueError("Unknown")),
        ):
            mock_cfg.return_value.agent.runtime = "bogus"

            checks = await _check_agent_runtime(tmp_path)

        assert len(checks) == 1
        assert checks[0][1] is False
        assert "Unknown" in checks[0][2]

    async def test_check_agent_runtime_generic_exception(self, tmp_path: Path) -> None:
        from sova.cli.commands.doctor import _check_agent_runtime

        with patch("sova.config.loader.load_config", side_effect=FileNotFoundError("no config")):
            checks = await _check_agent_runtime(tmp_path)

        assert len(checks) == 1
        assert checks[0][1] is False


# ---------------------------------------------------------------------------
# Admin helper functions
# ---------------------------------------------------------------------------


class TestAdminHelpers:
    """Tests for extracted helper functions in admin.py."""

    def test_parse_worktree_output_empty(self) -> None:
        from sova.cli.commands.admin import _parse_worktree_output

        result = _parse_worktree_output("")
        assert result == []

    def test_parse_worktree_output_single(self) -> None:
        from sova.cli.commands.admin import _parse_worktree_output

        output = "worktree /path/to/wt\nbranch refs/heads/feat/test\n\n"
        result = _parse_worktree_output(output)
        assert len(result) == 1
        assert result[0]["path"] == "/path/to/wt"
        assert result[0]["branch"] == "refs/heads/feat/test"

    def test_parse_worktree_output_multiple(self) -> None:
        from sova.cli.commands.admin import _parse_worktree_output

        output = "worktree /a\nbranch refs/heads/main\n\nworktree /b\nbranch refs/heads/feat/x\n\n"
        result = _parse_worktree_output(output)
        assert len(result) == 2

    def test_filter_stale_worktrees(self) -> None:
        from sova.cli.commands.admin import _filter_stale_worktrees

        worktrees = [
            {"path": "/a", "branch": "refs/heads/main"},
            {"path": "/b", "branch": "refs/heads/feat/my-feature"},
            {"path": "/c", "branch": "refs/heads/fix/a-bug"},
            {"path": "/d", "branch": "refs/heads/refactor/cleanup"},
            {"path": "/e", "branch": "refs/heads/chore/deps"},
        ]
        stale = _filter_stale_worktrees(worktrees)
        assert len(stale) == 3
        paths = {wt["path"] for wt in stale}
        assert paths == {"/b", "/c", "/d"}

    def test_filter_stale_worktrees_no_branch(self) -> None:
        from sova.cli.commands.admin import _filter_stale_worktrees

        worktrees = [{"path": "/a"}]
        stale = _filter_stale_worktrees(worktrees)
        assert stale == []


# ---------------------------------------------------------------------------
# Triage helper functions
# ---------------------------------------------------------------------------


class TestTriageHelpers:
    """Tests for extracted helper functions in triage.py."""

    def test_apply_config_overrides_no_overrides(self) -> None:
        from sova.cli.commands.triage import _apply_config_overrides
        from sova.config.models import TriageConfig

        cfg = TriageConfig()
        result = _apply_config_overrides(cfg, None, None)
        assert result.mode == cfg.mode
        assert result.auto_label == cfg.auto_label

    def test_apply_config_overrides_mode(self) -> None:
        from sova.cli.commands.triage import _apply_config_overrides
        from sova.config.models import TriageConfig

        cfg = TriageConfig(mode="full")
        result = _apply_config_overrides(cfg, "dry_run", None)
        assert result.mode == "dry_run"

    def test_apply_config_overrides_label(self) -> None:
        from sova.cli.commands.triage import _apply_config_overrides
        from sova.config.models import TriageConfig

        cfg = TriageConfig(auto_label=False)
        result = _apply_config_overrides(cfg, None, True)
        assert result.auto_label is True

    async def test_fetch_triage_tasks_single_issue(self) -> None:
        from sova.cli.commands.triage import _fetch_triage_tasks

        adapter = AsyncMock()
        task = Task(id="42", title="Test", state=TaskState.BACKLOG, labels=[])
        adapter.get_task.return_value = task

        result = await _fetch_triage_tasks(adapter, "42")
        assert len(result) == 1
        assert result[0].id == "42"

    async def test_fetch_triage_tasks_backlog_filter(self) -> None:
        from sova.cli.commands.triage import _fetch_triage_tasks

        adapter = AsyncMock()
        adapter.list_tasks.return_value = [
            Task(id="1", title="Backlog", state=TaskState.BACKLOG, labels=[]),
            Task(id="2", title="In Progress", state=TaskState.IN_PROGRESS, labels=[]),
            Task(id="3", title="Triaged", state=TaskState.TRIAGED, labels=[]),
        ]

        result = await _fetch_triage_tasks(adapter, None)
        assert len(result) == 1
        assert result[0].id == "1"


# ---------------------------------------------------------------------------
# Harden helper functions
# ---------------------------------------------------------------------------


class TestHardenHelpers:
    """Tests for extracted helper functions in harden.py."""

    async def test_resolve_harden_tasks_single(self) -> None:
        from sova.cli.commands.harden import _resolve_harden_tasks

        adapter = AsyncMock()
        task = Task(id="10", title="Test", state=TaskState.BACKLOG, labels=[])
        adapter.get_task.return_value = task

        result = await _resolve_harden_tasks(adapter, "10", [])
        assert len(result) == 1
        assert result[0].id == "10"

    async def test_resolve_harden_tasks_eligible_states(self) -> None:
        from sova.cli.commands.harden import _resolve_harden_tasks

        adapter = AsyncMock()
        all_open = [
            Task(id="1", title="Backlog", state=TaskState.BACKLOG, labels=[]),
            Task(id="2", title="Triaged", state=TaskState.TRIAGED, labels=[]),
            Task(id="3", title="In Progress", state=TaskState.IN_PROGRESS, labels=[]),
            Task(id="4", title="Needs Spec", state=TaskState.NEEDS_SPEC, labels=[]),
        ]

        result = await _resolve_harden_tasks(adapter, None, all_open)
        ids = {t.id for t in result}
        assert ids == {"1", "2", "4"}
