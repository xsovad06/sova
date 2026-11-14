"""Tests for sova.cli: CLI commands and Typer app."""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from sova.adapters.base import Task, TaskState
from sova.config.models import ProjectConfig

runner = CliRunner()


def _scaffold_install_artifacts(tmp_path: Path) -> None:
    """Create the minimum directory structure that _verify_install expects."""
    (tmp_path / "sova.toml").write_text("[task_source]\ntype = 'github'\n")
    commands_dir = tmp_path / ".claude" / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    (commands_dir / "dummy.md").write_text("---\nname: dummy\n---\n")
    memory_dir = tmp_path / ".claude" / "agent-memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    # Also create agent permissions
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.write_text(json.dumps({"permissions": {"allow": ["Bash(*)", "Read(*)", "Edit(*)", "Write(*)"]}}))


# Every test in this module runs against the shared per-worker in-memory DB
# (see ``tests/conftest.py:reset_db_rows``).
pytestmark = pytest.mark.usefixtures("reset_db_rows")


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
        # Command rows of the Commands panel ("| name  description"); a bare substring would also match help text.
        names = set(re.findall(r"^[│|]\s+([a-z][a-z-]*)\s", result.output, flags=re.MULTILINE))
        assert {"run", "triage"} <= names
        assert "memory" not in names
        assert "learn-from-pr" not in names

    @pytest.mark.parametrize(
        "args",
        [
            pytest.param(["address-pr"], id="address-pr"),
            pytest.param(["maintain-pr"], id="maintain-pr"),
            pytest.param(["review-pr"], id="review-pr"),
            pytest.param(["status"], id="status"),
            pytest.param(["costs"], id="costs"),
            pytest.param(["cleanup"], id="cleanup"),
            pytest.param(["install"], id="install"),
            pytest.param(["setup"], id="setup"),
            pytest.param(["uninstall"], id="uninstall"),
            pytest.param(["watch"], id="watch"),
            pytest.param(["parallel"], id="parallel"),
        ],
    )
    def test_subcommand_help_exits_cleanly(self, args: list[str]) -> None:
        from sova.cli.app import app

        result = runner.invoke(app, [*args, "--help"])
        assert result.exit_code == 0

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

    def test_config_error_exits_cleanly(self) -> None:
        """A bad config (e.g. unknown llm.provider) prints a clean error, not a traceback."""
        from sova.cli.app import app

        with patch(
            "sova.cli.app._init_llm_provider",
            side_effect=RuntimeError("Invalid configuration:\nllm.provider: bad value"),
        ):
            result = runner.invoke(app, ["status"])

        assert result.exit_code == 1
        assert "Configuration error" in result.output
        assert "llm.provider" in result.output
        assert "Traceback" not in result.output

    def test_missing_provider_extra_exits_cleanly(self) -> None:
        """A provider whose optional extra is missing is a config error, not a traceback."""
        from sova.cli.app import app

        with patch(
            "sova.cli.app._init_llm_provider",
            side_effect=ImportError("litellm is not installed. Install it with: pip install sova[litellm]"),
        ):
            result = runner.invoke(app, ["status"])

        assert result.exit_code == 1
        assert "Configuration error" in result.output
        assert "Traceback" not in result.output

    def test_doctor_still_runs_with_unloadable_config(self) -> None:
        """`sova doctor` diagnoses a broken config, so it must not be aborted by one."""
        from sova.cli.app import app

        with (
            patch("sova.cli.app._init_llm_provider", side_effect=RuntimeError("Invalid configuration")),
            patch("sova.cli.commands.doctor._doctor", new_callable=AsyncMock) as mock_doctor,
        ):
            result = runner.invoke(app, ["doctor"])

        assert result.exit_code == 0
        assert "Configuration error" in result.output
        assert mock_doctor.called


# ---------------------------------------------------------------------------
# Config command
# ---------------------------------------------------------------------------


class TestConfigCommand:
    """`sova config set` is the CLI half of DB-backed configuration.

    Five places in docs/ told users to run it years before it existed
    (docs/awareness-setup.md, docs/jira-configuration-guide.md); sova.toml was
    removed in #900, so without it the documented fallback for a list-typed
    setting was hand-editing .claude/sova.db with sqlite3.
    """

    def test_bare_config_still_shows_the_table(self) -> None:
        """Adding the subcommand must not break `sova config`."""
        from sova.cli.app import app

        with patch("sova.cli.commands.config.load_config", return_value=ProjectConfig(github_repo="owner/repo")):
            result = runner.invoke(app, ["config"])

        assert result.exit_code == 0
        assert "owner/repo" in result.output

    def test_set_persists_through_update_config(self, tmp_path: Path) -> None:
        from sova.cli.app import app

        with patch(
            "sova.dashboard.services.settings_service.update_config",
            new_callable=AsyncMock,
            return_value={"status": "ok", "key": "agent.max_budget", "value": "25"},
        ) as mock_update:
            result = runner.invoke(app, ["config", "set", "agent.max_budget", "25", "--project", str(tmp_path)])

        assert result.exit_code == 0
        assert mock_update.await_args.kwargs["key"] == "agent.max_budget"
        assert mock_update.await_args.kwargs["value"] == "25"

    def test_set_reports_error_and_exits_nonzero(self, tmp_path: Path) -> None:
        from sova.cli.app import app

        with patch(
            "sova.dashboard.services.settings_service.update_config",
            new_callable=AsyncMock,
            return_value={"error": "Unknown setting: 'nope.nope'"},
        ):
            result = runner.invoke(app, ["config", "set", "nope.nope", "x", "--project", str(tmp_path)])

        assert result.exit_code == 1
        assert "Unknown setting" in result.output

    def test_set_writes_a_list_value(self, tmp_path: Path, monkeypatch) -> None:
        """The end-to-end path a user follows from docs/awareness-setup.md.

        The module's in-memory SOVA_DATABASE_URL is dropped here: `set` runs
        its own asyncio.run(), whose fresh connection would get its own empty
        in-memory database rather than the one load_config() then reads.
        """
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)

        from sova.cli.app import app
        from sova.config.loader import load_config

        result = runner.invoke(app, ["config", "set", "awareness.providers", "gmail, gcal", "--project", str(tmp_path)])

        assert result.exit_code == 0
        assert load_config(tmp_path).awareness.providers == ["gmail", "gcal"]

    def test_set_runs_with_an_unloadable_config(self, tmp_path: Path) -> None:
        """`sova config set` is the repair tool, so a broken config must not abort it."""
        from sova.cli.app import app

        with (
            patch("sova.cli.app._init_llm_provider", side_effect=RuntimeError("Invalid configuration")),
            patch(
                "sova.dashboard.services.settings_service.update_config",
                new_callable=AsyncMock,
                return_value={"status": "ok", "key": "llm.provider", "value": "claude_code"},
            ) as mock_update,
        ):
            result = runner.invoke(app, ["config", "set", "llm.provider", "claude_code", "--project", str(tmp_path)])

        assert result.exit_code == 0
        assert mock_update.called

    def test_set_redacts_secret_value_in_output(self, tmp_path: Path) -> None:
        """A secret setting's value must never be echoed back to the console."""
        from sova.cli.app import app

        with patch(
            "sova.dashboard.services.settings_service.update_config",
            new_callable=AsyncMock,
            return_value={"status": "ok", "key": "llm.api_key", "value": "sk-super-secret-token"},
        ):
            result = runner.invoke(
                app, ["config", "set", "llm.api_key", "sk-super-secret-token", "--project", str(tmp_path)]
            )

        assert result.exit_code == 0
        assert "sk-super-secret-token" not in result.output
        assert "llm.api_key" in result.output

    def test_set_prints_keyring_fallback_warning(self, tmp_path: Path) -> None:
        """update_config()'s plaintext-fallback warning must reach the console."""
        from sova.cli.app import app

        with patch(
            "sova.dashboard.services.settings_service.update_config",
            new_callable=AsyncMock,
            return_value={
                "status": "ok",
                "key": "llm.api_key",
                "value": "sk-super-secret-token",
                "warning": "OS keyring unavailable; stored in the project database as plaintext",
            },
        ):
            result = runner.invoke(
                app, ["config", "set", "llm.api_key", "sk-super-secret-token", "--project", str(tmp_path)]
            )

        assert result.exit_code == 0
        assert "OS keyring unavailable" in result.output
        assert "sk-super-secret-token" not in result.output

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            # `sova config set` runs in its own process, so it cannot dispatch the live-reload a
            # running `sova server`/dashboard would apply for itself.
            pytest.param("llm.provider", "claude_code", id="live-reload-key"),
            pytest.param("server.port", "9000", id="server-prefixed-key"),
        ],
    )
    def test_set_warns_restart_required(self, tmp_path: Path, key: str, value: str) -> None:
        from sova.cli.app import app

        with patch(
            "sova.dashboard.services.settings_service.update_config",
            new_callable=AsyncMock,
            return_value={"status": "ok", "key": key, "value": value},
        ):
            result = runner.invoke(app, ["config", "set", key, value, "--project", str(tmp_path)])

        assert result.exit_code == 0
        assert "restart" in result.output.lower()

    def test_set_does_not_warn_for_settings_with_no_live_component(self, tmp_path: Path) -> None:
        """A plain DB-only setting with no reload target must not print a spurious warning."""
        from sova.cli.app import app

        with patch(
            "sova.dashboard.services.settings_service.update_config",
            new_callable=AsyncMock,
            return_value={"status": "ok", "key": "agent.max_budget", "value": "25"},
        ):
            result = runner.invoke(app, ["config", "set", "agent.max_budget", "25", "--project", str(tmp_path)])

        assert result.exit_code == 0
        assert "restart" not in result.output.lower()

    def test_bare_config_reports_a_broken_config_cleanly(self) -> None:
        """Being config-tolerant must not turn an unloadable config into a traceback."""
        from sova.cli.app import app

        with (
            patch("sova.cli.app._init_llm_provider", side_effect=RuntimeError("Invalid configuration")),
            patch("sova.cli.commands.config.load_config", side_effect=RuntimeError("Invalid configuration")),
        ):
            result = runner.invoke(app, ["config"])

        assert result.exit_code == 1
        assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# Dashboard command
# ---------------------------------------------------------------------------


class TestDashboardCommand:
    def test_dashboard_reload_dirs_only_includes_dashboard_and_cli(self, tmp_path: Path) -> None:
        """Verify dashboard command configures narrow reload_dirs to avoid killing agents."""
        from sova.cli.app import app

        _scaffold_install_artifacts(tmp_path)
        with patch("uvicorn.run") as mock_run:
            result = runner.invoke(app, ["dashboard", "--project", str(tmp_path), "--reload"])
            assert result.exit_code == 0

        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["reload"] is True
        reload_dirs = call_kwargs["reload_dirs"]
        assert len(reload_dirs) == 2
        assert any("dashboard" in d for d in reload_dirs)
        assert any("cli" in d for d in reload_dirs)
        assert not any("core" in d for d in reload_dirs)
        assert not any("ipc" in d for d in reload_dirs)
        assert not any("roles" in d for d in reload_dirs)

    def test_dashboard_non_reload_mode_omits_reload_dirs(self, tmp_path: Path) -> None:
        """Verify non-reload mode does not use reload_dirs."""
        from sova.cli.app import app

        _scaffold_install_artifacts(tmp_path)
        with patch("uvicorn.run") as mock_run:
            result = runner.invoke(app, ["dashboard", "--project", str(tmp_path)])
            assert result.exit_code == 0

        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args[1]
        # First arg should be the app instance, not a string
        assert "reload_dirs" not in call_kwargs
        assert call_kwargs.get("reload") is None or call_kwargs.get("reload") is False


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
    async def test_review_pr_without_linked_issue_reviews_pr_alone(self, tmp_path: Path) -> None:
        """An unlinked PR runs the reviewer with no issue, not with the PR number posing as one."""
        from sova.cli.commands.pr import _review_pr
        from sova.roles.base import RoleResult

        execute = AsyncMock(return_value=RoleResult(success=True, summary="Reviewed PR #60: 0 findings"))
        with (
            patch("sova.config.loader.load_config", return_value=ProjectConfig()),
            patch("sova.db.session.init_db", new_callable=AsyncMock),
            patch("sova.adapters.create_adapter", return_value=MagicMock()),
            patch("sova.utils.gh.resolve_linked_issue", new_callable=AsyncMock, return_value=None),
            patch("sova.roles.reviewer.ReviewerRole.execute", execute),
        ):
            await _review_pr(pr=60, project_dir=tmp_path)

        ctx = execute.await_args.args[0]
        assert ctx.issue_number == ""
        assert ctx.pr_number == 60

    async def test_review_pr_exits_when_linked_issue_lookup_fails(self, tmp_path: Path) -> None:
        """A failed lookup is not an unlinked PR: it must not silently review without the issue."""
        import typer

        from sova.cli.commands.pr import _review_pr

        execute = AsyncMock()
        with (
            patch("sova.config.loader.load_config", return_value=ProjectConfig()),
            patch("sova.db.session.init_db", new_callable=AsyncMock),
            patch("sova.adapters.create_adapter", return_value=MagicMock()),
            patch(
                "sova.utils.gh.resolve_linked_issue",
                new_callable=AsyncMock,
                side_effect=RuntimeError("Failed to fetch PR #60 body"),
            ),
            patch("sova.roles.reviewer.ReviewerRole.execute", execute),
            pytest.raises(typer.Exit) as exc_info,
        ):
            await _review_pr(pr=60, project_dir=tmp_path)

        assert exc_info.value.exit_code == 1
        execute.assert_not_called()


# ---------------------------------------------------------------------------
# Removed memory commands
# ---------------------------------------------------------------------------


class TestRemovedMemoryCommands:
    @pytest.mark.parametrize(
        "args", [["memory", "search", "q"], ["learn-from-pr", "1"]], ids=["memory", "learn-from-pr"]
    )
    def test_command_is_gone(self, args: list[str]) -> None:
        from sova.cli.app import app

        result = runner.invoke(app, args)

        assert result.exit_code == 2
        assert "No such command" in result.output


# ---------------------------------------------------------------------------
# Project commands
# ---------------------------------------------------------------------------


class TestProjectCommands:
    async def test_install_configures_githooks(self, tmp_path: Path) -> None:
        from sova.cli.commands.project import _install

        (tmp_path / ".githooks").mkdir()
        _scaffold_install_artifacts(tmp_path)

        with (
            patch("sova.cli.commands.project._exclude_runtime_files", new_callable=AsyncMock),
            patch("sova.cli.commands.project.run", new_callable=AsyncMock) as mock_run,
            patch("sova.db.session.init_db", new_callable=AsyncMock),
            patch("sova.commands.distribution.install_commands") as mock_install_cmds,
            patch("sova.commands.distribution.install_guidelines") as mock_install_guides,
            patch("sova.commands.catalog.get_canonical_dir", return_value=tmp_path),
            patch("sova.commands.catalog.get_guidelines_dir", return_value=tmp_path),
            patch("sova.config.loader.load_config"),
        ):
            mock_install_cmds.return_value = MagicMock(installed=1)
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
        _scaffold_install_artifacts(tmp_path)

        with (
            patch("sova.cli.commands.project._exclude_runtime_files", new_callable=AsyncMock),
            patch("sova.cli.commands.project.run", new_callable=AsyncMock) as mock_run,
            patch("sova.db.session.init_db", new_callable=AsyncMock),
            patch("sova.commands.distribution.install_commands") as mock_install_cmds,
            patch("sova.commands.distribution.install_guidelines") as mock_install_guides,
            patch("sova.commands.catalog.get_canonical_dir", return_value=tmp_path),
            patch("sova.commands.catalog.get_guidelines_dir", return_value=tmp_path),
            patch("sova.config.loader.load_config"),
        ):
            mock_install_cmds.return_value = MagicMock(installed=1)
            mock_install_guides.return_value = MagicMock(installed=0)
            mock_run.return_value = MagicMock(success=True, stdout=".githooks\n")
            await _install(path=tmp_path, no_dashboard=True, update=False)

        assert mock_run.call_count == 1

    async def test_install_db_failure_reports_error(self, tmp_path: Path) -> None:
        """init_db failure is caught and reported, but commands still install."""
        from sova.cli.commands.project import _install

        _scaffold_install_artifacts(tmp_path)

        with (
            patch("sova.db.session.init_db", new_callable=AsyncMock, side_effect=RuntimeError("disk full")),
            patch("sova.commands.distribution.install_commands") as mock_install_cmds,
            patch("sova.commands.distribution.install_guidelines") as mock_install_guides,
            patch("sova.commands.catalog.get_canonical_dir", return_value=tmp_path),
            patch("sova.commands.catalog.get_guidelines_dir", return_value=tmp_path),
            patch("sova.config.loader.load_config"),
        ):
            mock_install_cmds.return_value = MagicMock(installed=1)
            mock_install_guides.return_value = MagicMock(installed=0)
            # Should complete without raising (db failure is non-fatal if commands succeed)
            await _install(path=tmp_path, no_dashboard=True, update=False)

        mock_install_cmds.assert_called_once()
        mock_install_guides.assert_called_once()

    async def test_install_command_failure_exits_with_error(self, tmp_path: Path) -> None:
        """Command installation failure triggers verification failure."""
        from typer import Exit

        from sova.cli.commands.project import _install

        (tmp_path / "sova.toml").write_text("[task_source]\ntype = 'github'\n")
        (tmp_path / ".claude").mkdir(exist_ok=True)

        with (
            patch("sova.db.session.init_db", new_callable=AsyncMock),
            patch("sova.commands.distribution.install_commands", side_effect=OSError("permission denied")),
            patch("sova.commands.distribution.install_guidelines"),
            patch("sova.commands.catalog.get_canonical_dir", return_value=tmp_path),
            patch("sova.commands.catalog.get_guidelines_dir", return_value=tmp_path),
            patch("sova.config.loader.load_config"),
        ):
            with pytest.raises(Exit):
                await _install(path=tmp_path, no_dashboard=True, update=False)

    async def test_install_git_hooks_exception_is_non_fatal(self, tmp_path: Path) -> None:
        """A raising _configure_git_hooks is caught and logged, install still completes."""
        from sova.cli.commands.project import _install

        _scaffold_install_artifacts(tmp_path)

        with (
            patch("sova.cli.commands.project._configure_git_hooks", new_callable=AsyncMock) as mock_hooks,
            patch("sova.db.session.init_db", new_callable=AsyncMock),
            patch("sova.commands.distribution.install_commands") as mock_install_cmds,
            patch("sova.commands.distribution.install_guidelines") as mock_install_guides,
            patch("sova.commands.catalog.get_canonical_dir", return_value=tmp_path),
            patch("sova.commands.catalog.get_guidelines_dir", return_value=tmp_path),
            patch("sova.config.loader.load_config"),
        ):
            mock_hooks.side_effect = RuntimeError("git not found")
            mock_install_cmds.return_value = MagicMock(installed=1)
            mock_install_guides.return_value = MagicMock(installed=0)
            await _install(path=tmp_path, no_dashboard=True, update=False)

        mock_hooks.assert_awaited_once()

    async def test_install_backfill_config_failure_is_logged(self, tmp_path: Path) -> None:
        """If Stage 3's backfill save also fails, the narrower except still swallows it."""
        from sqlalchemy.exc import SQLAlchemyError

        from sova.cli.commands.project import _install

        _scaffold_install_artifacts(tmp_path)

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_begin = AsyncMock(__aenter__=AsyncMock(), __aexit__=AsyncMock(return_value=False))
        mock_session.begin = MagicMock(return_value=mock_begin)

        with (
            patch("sova.db.session.init_db", new_callable=AsyncMock),
            patch("sova.db.session.get_session", new_callable=AsyncMock, return_value=mock_session),
            patch("sova.config.db_loader._try_load_from_db", return_value=None),
            patch("sova.config.db_loader.save_config_to_db", side_effect=SQLAlchemyError("locked")),
            patch("sova.commands.distribution.install_commands") as mock_install_cmds,
            patch("sova.commands.distribution.install_guidelines") as mock_install_guides,
            patch("sova.commands.catalog.get_canonical_dir", return_value=tmp_path),
            patch("sova.commands.catalog.get_guidelines_dir", return_value=tmp_path),
            patch("sova.config.loader.load_config"),
        ):
            mock_install_cmds.return_value = MagicMock(installed=1)
            mock_install_guides.return_value = MagicMock(installed=0)
            # Neither the early Stage 1 attempt nor the Stage 3 backfill raises past _install.
            await _install(path=tmp_path, no_dashboard=True, update=False)

    async def test_install_agent_permissions_exception_is_non_fatal(self, tmp_path: Path) -> None:
        from sova.cli.commands.project import _install

        _scaffold_install_artifacts(tmp_path)

        with (
            patch("sova.cli.commands.project._configure_agent_permissions") as mock_perms,
            patch("sova.db.session.init_db", new_callable=AsyncMock),
            patch("sova.commands.distribution.install_commands") as mock_install_cmds,
            patch("sova.commands.distribution.install_guidelines") as mock_install_guides,
            patch("sova.commands.catalog.get_canonical_dir", return_value=tmp_path),
            patch("sova.commands.catalog.get_guidelines_dir", return_value=tmp_path),
            patch("sova.config.loader.load_config"),
        ):
            mock_perms.side_effect = OSError("settings.json locked")
            mock_install_cmds.return_value = MagicMock(installed=1)
            mock_install_guides.return_value = MagicMock(installed=0)
            await _install(path=tmp_path, no_dashboard=True, update=False)

        mock_perms.assert_called_once()

    async def test_install_rtk_exception_is_non_fatal(self, tmp_path: Path) -> None:
        from sova.cli.commands.project import _install

        _scaffold_install_artifacts(tmp_path)

        with (
            patch("sova.cli.commands.project._configure_rtk") as mock_rtk,
            patch("sova.db.session.init_db", new_callable=AsyncMock),
            patch("sova.commands.distribution.install_commands") as mock_install_cmds,
            patch("sova.commands.distribution.install_guidelines") as mock_install_guides,
            patch("sova.commands.catalog.get_canonical_dir", return_value=tmp_path),
            patch("sova.commands.catalog.get_guidelines_dir", return_value=tmp_path),
            patch("sova.config.loader.load_config"),
        ):
            mock_rtk.side_effect = RuntimeError("rtk hook injection failed")
            mock_install_cmds.return_value = MagicMock(installed=1)
            mock_install_guides.return_value = MagicMock(installed=0)
            await _install(path=tmp_path, no_dashboard=True, update=False)

        mock_rtk.assert_called_once()

    async def test_install_mcp_exception_is_non_fatal(self, tmp_path: Path) -> None:
        from sova.cli.commands.project import _install

        _scaffold_install_artifacts(tmp_path)

        with (
            patch("sova.cli.commands.project._configure_mcp_servers") as mock_mcp,
            patch("sova.db.session.init_db", new_callable=AsyncMock),
            patch("sova.commands.distribution.install_commands") as mock_install_cmds,
            patch("sova.commands.distribution.install_guidelines") as mock_install_guides,
            patch("sova.commands.catalog.get_canonical_dir", return_value=tmp_path),
            patch("sova.commands.catalog.get_guidelines_dir", return_value=tmp_path),
            patch("sova.config.loader.load_config"),
        ):
            mock_mcp.side_effect = RuntimeError("mcp config write failed")
            mock_install_cmds.return_value = MagicMock(installed=1)
            mock_install_guides.return_value = MagicMock(installed=0)
            await _install(path=tmp_path, no_dashboard=True, update=False)

        mock_mcp.assert_called_once()

    async def test_install_agent_memory_exception_is_non_fatal(self, tmp_path: Path) -> None:
        from sova.cli.commands.project import _install

        _scaffold_install_artifacts(tmp_path)

        with (
            patch("sova.cli.commands.project._create_agent_memory") as mock_memory,
            patch("sova.db.session.init_db", new_callable=AsyncMock),
            patch("sova.commands.distribution.install_commands") as mock_install_cmds,
            patch("sova.commands.distribution.install_guidelines") as mock_install_guides,
            patch("sova.commands.catalog.get_canonical_dir", return_value=tmp_path),
            patch("sova.commands.catalog.get_guidelines_dir", return_value=tmp_path),
            patch("sova.config.loader.load_config"),
        ):
            mock_memory.side_effect = OSError("disk full")
            mock_install_cmds.return_value = MagicMock(installed=1)
            mock_install_guides.return_value = MagicMock(installed=0)
            await _install(path=tmp_path, no_dashboard=True, update=False)

        mock_memory.assert_called_once()

    async def test_install_creates_all_artifacts(self, tmp_path: Path) -> None:
        """Successful install creates commands dir, agent-memory, and saves config to DB."""
        from sova.cli.commands.project import _install

        def _install_cmds_side_effect(_canonical_dir, commands_dir, _cfg, **_kwargs):
            commands_dir.mkdir(parents=True, exist_ok=True)
            (commands_dir / "dummy.md").write_text("---\nname: dummy\n---\n")
            return MagicMock(installed=1)

        saved_config: dict = {}

        async def _capture_save(session, config):
            from sova.config.db_loader import _flatten_config_dict

            saved_config.update(_flatten_config_dict(config))

        _try_load_calls = [0]

        def _try_load_side_effect(project_dir):
            _try_load_calls[0] += 1
            if _try_load_calls[0] == 1:
                return None
            return {"github_repo": ""} if not saved_config else {"github_repo": saved_config.get("github_repo", "")}

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_begin = AsyncMock(__aenter__=AsyncMock(), __aexit__=AsyncMock(return_value=False))
        mock_session.begin = MagicMock(return_value=mock_begin)

        with (
            patch("sova.db.session.init_db", new_callable=AsyncMock),
            patch("sova.db.session.get_session", new_callable=AsyncMock, return_value=mock_session),
            patch("sova.config.db_loader._try_load_from_db", side_effect=_try_load_side_effect),
            patch("sova.config.db_loader.save_config_to_db", side_effect=_capture_save),
            patch("sova.commands.distribution.install_commands") as mock_install_cmds,
            patch("sova.commands.distribution.install_guidelines") as mock_install_guides,
            patch("sova.commands.distribution.install_skills") as mock_install_sk,
            patch("sova.commands.catalog.get_canonical_dir", return_value=tmp_path),
            patch("sova.commands.catalog.get_guidelines_dir", return_value=tmp_path),
            patch("sova.commands.catalog.get_skills_dir", return_value=tmp_path),
            patch("sova.config.loader.load_config"),
        ):
            mock_install_cmds.side_effect = _install_cmds_side_effect
            mock_install_guides.return_value = MagicMock(installed=0)
            mock_install_sk.return_value = MagicMock(installed=0)
            await _install(path=tmp_path, no_dashboard=True, update=False)

        assert saved_config
        assert saved_config.get("base_branch") == "main"
        assert (tmp_path / ".claude" / "commands").is_dir()
        assert (tmp_path / ".claude" / "agent-memory").is_dir()
        assert (tmp_path / ".claude" / "agent-memory" / "MEMORY.md").exists()

    async def test_install_update_includes_skills(self, tmp_path: Path) -> None:
        """Install with update=True calls update_skills and prints result."""
        from sova.cli.commands.project import _install

        _scaffold_install_artifacts(tmp_path)

        with (
            patch("sova.db.session.init_db", new_callable=AsyncMock),
            patch("sova.commands.distribution.update_commands") as mock_up_cmds,
            patch("sova.commands.distribution.update_guidelines") as mock_up_guides,
            patch("sova.commands.distribution.update_skills") as mock_up_sk,
            patch("sova.commands.catalog.get_canonical_dir", return_value=tmp_path),
            patch("sova.commands.catalog.get_guidelines_dir", return_value=tmp_path),
            patch("sova.commands.catalog.get_skills_dir", return_value=tmp_path),
            patch("sova.config.loader.load_config"),
        ):
            mock_up_cmds.return_value = MagicMock(updated=1, skipped=0, conflicts=[])
            mock_up_guides.return_value = MagicMock(updated=0, skipped=0, conflicts=[])
            mock_up_sk.return_value = MagicMock(updated=2, skipped=1, conflicts=[])
            await _install(path=tmp_path, no_dashboard=True, update=True)

        mock_up_sk.assert_called_once()

    async def test_install_update_skills_with_conflicts(self, tmp_path: Path) -> None:
        """Install update path prints skills conflict warnings."""
        from sova.cli.commands.project import _install

        _scaffold_install_artifacts(tmp_path)

        with (
            patch("sova.db.session.init_db", new_callable=AsyncMock),
            patch("sova.commands.distribution.update_commands") as mock_up_cmds,
            patch("sova.commands.distribution.update_guidelines") as mock_up_guides,
            patch("sova.commands.distribution.update_skills") as mock_up_sk,
            patch("sova.commands.catalog.get_canonical_dir", return_value=tmp_path),
            patch("sova.commands.catalog.get_guidelines_dir", return_value=tmp_path),
            patch("sova.commands.catalog.get_skills_dir", return_value=tmp_path),
            patch("sova.config.loader.load_config"),
        ):
            mock_up_cmds.return_value = MagicMock(updated=0, skipped=0, conflicts=[])
            mock_up_guides.return_value = MagicMock(updated=0, skipped=0, conflicts=[])
            mock_up_sk.return_value = MagicMock(updated=0, skipped=0, conflicts=["testing-patterns"])
            await _install(path=tmp_path, no_dashboard=True, update=True)

        mock_up_sk.assert_called_once()

    def test_verify_install_all_present(self, tmp_path: Path) -> None:
        """Verification passes when all artifacts exist."""
        from sova.cli.commands.project import _verify_install

        _scaffold_install_artifacts(tmp_path)
        problems, warnings = _verify_install(tmp_path)
        assert problems == []
        assert warnings == []

    def test_verify_install_missing_commands(self, tmp_path: Path) -> None:
        """Verification catches missing commands directory."""
        from sova.cli.commands.project import _verify_install

        (tmp_path / "sova.toml").write_text("")
        (tmp_path / ".claude" / "agent-memory").mkdir(parents=True)
        problems, _warnings = _verify_install(tmp_path)
        assert any("commands" in p for p in problems)

    def test_verify_install_missing_memory(self, tmp_path: Path) -> None:
        """Verification catches missing agent-memory directory."""
        from sova.cli.commands.project import _verify_install

        (tmp_path / "sova.toml").write_text("")
        commands_dir = tmp_path / ".claude" / "commands"
        commands_dir.mkdir(parents=True)
        (commands_dir / "test.md").write_text("# test")
        problems, _warnings = _verify_install(tmp_path)
        assert any("agent-memory" in p for p in problems)

    def test_verify_install_missing_permissions_is_warning(self, tmp_path: Path) -> None:
        """Missing agent permissions are returned as warnings, not problems."""
        from sova.cli.commands.project import _verify_install

        _scaffold_install_artifacts(tmp_path)
        # Remove permissions so they are missing
        (tmp_path / ".claude" / "settings.json").unlink()
        problems, warnings = _verify_install(tmp_path)
        assert problems == []
        assert any("permissions" in w for w in warnings)


# ---------------------------------------------------------------------------
# Install git identity warning
# ---------------------------------------------------------------------------


class TestExcludeRuntimeFiles:
    """sova install writes SOVA's runtime paths into .git/info/exclude (issue #1402)."""

    @staticmethod
    def _init_repo(path: Path) -> Path:
        import subprocess

        subprocess.run(["git", "init", "-q", str(path)], check=True)
        return path / ".git" / "info" / "exclude"

    async def test_appends_runtime_entries(self, tmp_path: Path) -> None:
        from sova.cli.commands.project import _exclude_runtime_files

        exclude = self._init_repo(tmp_path)
        await _exclude_runtime_files(tmp_path)

        lines = exclude.read_text(encoding="utf-8").splitlines()
        assert "/.sova/" in lines
        assert "/.claude/sova.db*" in lines
        assert "/.claude/sova.log*" in lines
        assert "/.claude/agent-control/" in lines
        assert "/.claude/specs/" not in lines

    async def test_rerun_does_not_duplicate_and_keeps_user_entries(self, tmp_path: Path) -> None:
        from sova.cli.commands.project import _exclude_runtime_files

        exclude = self._init_repo(tmp_path)
        exclude.write_text("my-scratch/\n.sova/", encoding="utf-8")  # hand-added, no trailing newline

        await _exclude_runtime_files(tmp_path)
        first = exclude.read_text(encoding="utf-8")
        await _exclude_runtime_files(tmp_path)

        assert exclude.read_text(encoding="utf-8") == first
        lines = first.splitlines()
        assert lines[0] == "my-scratch/"
        assert lines.count(".sova/") == 1
        assert "/.sova/" not in lines  # the hand-added unanchored line already covers it
        assert "/.claude/sova.db*" in lines

    async def test_runtime_files_become_untracked_invisible(self, tmp_path: Path) -> None:
        import subprocess

        from sova.cli.commands.project import _exclude_runtime_files

        self._init_repo(tmp_path)
        await _exclude_runtime_files(tmp_path)
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".sova").mkdir()
        (tmp_path / ".claude" / "sova.db-wal").write_text("x", encoding="utf-8")
        (tmp_path / ".sova" / "baseline.json").write_text("x", encoding="utf-8")

        status = subprocess.run(
            ["git", "status", "--porcelain", "-uall"], cwd=tmp_path, check=True, capture_output=True, text=True
        )
        assert status.stdout.strip() == ""

    async def test_nested_project_gets_entries_relative_to_repo_root(self, tmp_path: Path) -> None:
        import subprocess

        from sova.cli.commands.project import _exclude_runtime_files

        exclude = self._init_repo(tmp_path)
        project = tmp_path / "services" / "api"
        project.mkdir(parents=True)

        await _exclude_runtime_files(project)

        lines = exclude.read_text(encoding="utf-8").splitlines()
        assert "/services/api/.claude/sova.db*" in lines
        assert "/services/api/.sova/" in lines
        (project / ".claude").mkdir()
        (project / ".claude" / "sova.db").write_text("x", encoding="utf-8")
        (project / ".claude" / "mcp_secret").write_text("x", encoding="utf-8")
        status = subprocess.run(
            ["git", "status", "--porcelain", "-uall"], cwd=tmp_path, check=True, capture_output=True, text=True
        )
        assert status.stdout.strip() == ""

    async def test_entries_are_anchored_to_the_project(self, tmp_path: Path) -> None:
        import subprocess

        from sova.cli.commands.project import _exclude_runtime_files

        self._init_repo(tmp_path)
        await _exclude_runtime_files(tmp_path)
        (tmp_path / "vendor" / "x" / ".sova").mkdir(parents=True)
        (tmp_path / "vendor" / "x" / ".sova" / "keep.txt").write_text("x", encoding="utf-8")

        status = subprocess.run(
            ["git", "status", "--porcelain", "-uall"], cwd=tmp_path, check=True, capture_output=True, text=True
        )
        assert "vendor/x/.sova/keep.txt" in status.stdout

    async def test_glob_metacharacters_in_project_path_are_escaped(self, tmp_path: Path) -> None:
        import subprocess

        from sova.cli.commands.project import _exclude_runtime_files

        exclude = self._init_repo(tmp_path)
        project = tmp_path / "a[1]*proj"
        project.mkdir()

        await _exclude_runtime_files(project)

        lines = exclude.read_text(encoding="utf-8").splitlines()
        assert "/a\\[1\\]\\*proj/.sova/" in lines
        (project / ".sova").mkdir()
        (project / ".sova" / "baseline.json").write_text("x", encoding="utf-8")
        (tmp_path / "a1proj").mkdir()
        (tmp_path / "a1proj" / ".sova").mkdir()
        (tmp_path / "a1proj" / ".sova" / "other.json").write_text("x", encoding="utf-8")
        status = subprocess.run(
            ["git", "status", "--porcelain", "-uall"], cwd=tmp_path, check=True, capture_output=True, text=True
        )
        assert "baseline.json" not in status.stdout
        assert "a1proj/.sova/other.json" in status.stdout

    async def test_unreadable_exclude_file_is_left_alone(self, tmp_path: Path) -> None:
        from sova.cli.commands.project import _exclude_runtime_files

        exclude = self._init_repo(tmp_path)
        original = b"\xff\xfe not utf-8\n"
        exclude.write_bytes(original)

        await _exclude_runtime_files(tmp_path)
        await _exclude_runtime_files(tmp_path)

        assert exclude.read_bytes() == original

    async def test_not_a_git_repo_warns_without_raising(self, tmp_path: Path) -> None:
        from sova.cli.commands.project import _exclude_runtime_files

        with patch("sova.cli.commands.project.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = MagicMock(success=False, stdout="", stderr="fatal: not a git repository")
            with patch("sova.cli.commands.project.console") as mock_console:
                await _exclude_runtime_files(tmp_path)

        printed = " ".join(str(c.args[0]) for c in mock_console.print.call_args_list)
        assert "not a git repository" in printed
        assert list(tmp_path.iterdir()) == []


class TestInstallGitIdentityWarning:
    async def test_install_warns_on_missing_git_identity(self, tmp_path: Path) -> None:
        from sova.cli.commands.project import _install
        from sova.utils.shell import GitIdentityResult

        _scaffold_install_artifacts(tmp_path)
        missing = GitIdentityResult(name="", email="test@example.com")
        mock_id = AsyncMock(return_value=missing)

        with (
            patch("sova.cli.commands.project.check_git_identity", mock_id),
            patch("sova.db.session.init_db", new_callable=AsyncMock),
            patch("sova.commands.distribution.install_commands") as mock_cmds,
            patch("sova.commands.distribution.install_guidelines") as mock_guides,
            patch("sova.commands.distribution.install_skills") as mock_sk,
            patch("sova.commands.catalog.get_canonical_dir", return_value=tmp_path),
            patch("sova.commands.catalog.get_guidelines_dir", return_value=tmp_path),
            patch("sova.commands.catalog.get_skills_dir", return_value=tmp_path),
            patch("sova.config.loader.load_config"),
        ):
            mock_cmds.return_value = MagicMock(installed=1)
            mock_guides.return_value = MagicMock(installed=0)
            mock_sk.return_value = MagicMock(installed=0)
            await _install(path=tmp_path, no_dashboard=True, update=False)

    async def test_install_swallows_identity_check_exception(self, tmp_path: Path) -> None:
        """The except Exception block (line 67-68) must not propagate."""
        from sova.cli.commands.project import _install

        _scaffold_install_artifacts(tmp_path)
        mock_id = AsyncMock(side_effect=RuntimeError("git not found"))

        with (
            patch("sova.cli.commands.project.check_git_identity", mock_id),
            patch("sova.db.session.init_db", new_callable=AsyncMock),
            patch("sova.commands.distribution.install_commands") as mock_cmds,
            patch("sova.commands.distribution.install_guidelines") as mock_guides,
            patch("sova.commands.distribution.install_skills") as mock_sk,
            patch("sova.commands.catalog.get_canonical_dir", return_value=tmp_path),
            patch("sova.commands.catalog.get_guidelines_dir", return_value=tmp_path),
            patch("sova.commands.catalog.get_skills_dir", return_value=tmp_path),
            patch("sova.config.loader.load_config"),
        ):
            mock_cmds.return_value = MagicMock(installed=1)
            mock_guides.return_value = MagicMock(installed=0)
            mock_sk.return_value = MagicMock(installed=0)
            # Should not raise despite check_git_identity failure
            await _install(path=tmp_path, no_dashboard=True, update=False)

    async def test_install_no_warning_when_identity_configured(self, tmp_path: Path) -> None:
        from sova.cli.commands.project import _install
        from sova.utils.shell import GitIdentityResult

        _scaffold_install_artifacts(tmp_path)
        valid = GitIdentityResult(name="User", email="u@e.com")

        with (
            patch("sova.cli.commands.project.check_git_identity", AsyncMock(return_value=valid)),
            patch("sova.db.session.init_db", new_callable=AsyncMock),
            patch("sova.commands.distribution.install_commands") as mock_cmds,
            patch("sova.commands.distribution.install_guidelines") as mock_guides,
            patch("sova.commands.distribution.install_skills") as mock_sk,
            patch("sova.commands.catalog.get_canonical_dir", return_value=tmp_path),
            patch("sova.commands.catalog.get_guidelines_dir", return_value=tmp_path),
            patch("sova.commands.catalog.get_skills_dir", return_value=tmp_path),
            patch("sova.config.loader.load_config"),
        ):
            mock_cmds.return_value = MagicMock(installed=1)
            mock_guides.return_value = MagicMock(installed=0)
            mock_sk.return_value = MagicMock(installed=0)
            await _install(path=tmp_path, no_dashboard=True, update=False)


# ---------------------------------------------------------------------------
# Agent permissions setup
# ---------------------------------------------------------------------------


class TestAgentPermissionsSetup:
    async def test_install_creates_permissions(self, tmp_path: Path) -> None:
        """Install creates agent permissions in .claude/settings.json."""
        import json

        from sova.cli.commands.project import _install

        # Manually create only the artifacts _verify_install needs,
        # WITHOUT pre-creating settings.json so the test verifies
        # that _install itself creates permissions from scratch.
        (tmp_path / "sova.toml").write_text("[task_source]\ntype = 'github'\n")
        commands_dir = tmp_path / ".claude" / "commands"
        commands_dir.mkdir(parents=True, exist_ok=True)
        (commands_dir / "dummy.md").write_text("---\nname: dummy\n---\n")
        memory_dir = tmp_path / ".claude" / "agent-memory"
        memory_dir.mkdir(parents=True, exist_ok=True)

        settings_path = tmp_path / ".claude" / "settings.json"
        assert not settings_path.exists()

        with (
            patch("sova.db.session.init_db", new_callable=AsyncMock),
            patch("sova.commands.distribution.install_commands") as mock_cmds,
            patch("sova.commands.distribution.install_guidelines") as mock_guides,
            patch("sova.commands.distribution.install_skills") as mock_sk,
            patch("sova.commands.catalog.get_canonical_dir", return_value=tmp_path),
            patch("sova.commands.catalog.get_guidelines_dir", return_value=tmp_path),
            patch("sova.commands.catalog.get_skills_dir", return_value=tmp_path),
            patch("sova.config.loader.load_config"),
        ):
            mock_cmds.return_value = MagicMock(installed=1)
            mock_guides.return_value = MagicMock(installed=0)
            mock_sk.return_value = MagicMock(installed=0)
            await _install(path=tmp_path, no_dashboard=True, update=False)

        assert settings_path.exists()
        data = json.loads(settings_path.read_text())
        assert "permissions" in data
        assert "allow" in data["permissions"]
        assert "Bash(*)" in data["permissions"]["allow"]
        assert "Read(*)" in data["permissions"]["allow"]
        assert "Edit(*)" in data["permissions"]["allow"]
        assert "Write(*)" in data["permissions"]["allow"]

    async def test_install_update_adds_missing_permissions(self, tmp_path: Path) -> None:
        """Install with update=True adds missing permissions."""
        import json

        from sova.cli.commands.project import _install

        _scaffold_install_artifacts(tmp_path)
        settings_path = tmp_path / ".claude" / "settings.json"
        settings_path.write_text(json.dumps({"permissions": {"allow": ["Bash(*)"]}}))

        with (
            patch("sova.db.session.init_db", new_callable=AsyncMock),
            patch("sova.commands.distribution.update_commands") as mock_up_cmds,
            patch("sova.commands.distribution.update_guidelines") as mock_up_guides,
            patch("sova.commands.distribution.update_skills") as mock_up_sk,
            patch("sova.commands.catalog.get_canonical_dir", return_value=tmp_path),
            patch("sova.commands.catalog.get_guidelines_dir", return_value=tmp_path),
            patch("sova.commands.catalog.get_skills_dir", return_value=tmp_path),
            patch("sova.config.loader.load_config"),
        ):
            mock_up_cmds.return_value = MagicMock(updated=0, skipped=0, conflicts=[])
            mock_up_guides.return_value = MagicMock(updated=0, skipped=0, conflicts=[])
            mock_up_sk.return_value = MagicMock(updated=0, skipped=0, conflicts=[])
            await _install(path=tmp_path, no_dashboard=True, update=True)

        data = json.loads(settings_path.read_text())
        assert sorted(data["permissions"]["allow"]) == sorted(["Bash(*)", "Read(*)", "Edit(*)", "Write(*)"])


# ---------------------------------------------------------------------------
# Uninstall command
# ---------------------------------------------------------------------------


def _scaffold_full_install(tmp_path: Path) -> None:
    """Create a complete SOVA installation for uninstall tests."""
    import json

    _scaffold_install_artifacts(tmp_path)
    claude_dir = tmp_path / ".claude"
    (claude_dir / "sova.db").write_text("")
    (claude_dir / "sova.db.bak").write_text("")
    (claude_dir / "agent-memory").mkdir(exist_ok=True)
    (claude_dir / "agent-memory" / "MEMORY.md").write_text("# Memory")
    (claude_dir / "worktrees").mkdir(exist_ok=True)
    (claude_dir / "agent-control").mkdir(exist_ok=True)

    commands_dir = claude_dir / "commands"
    manifest = {
        "version": 1,
        "commands": {
            "dummy.md": {"hash": "abc123", "managed": True},
        },
    }
    (commands_dir / ".sova-manifest.json").write_text(json.dumps(manifest))

    rules_dir = claude_dir / "rules"
    rules_dir.mkdir(exist_ok=True)
    (rules_dir / "managed-rule.md").write_text("# Rule")
    rules_manifest = {
        "version": 1,
        "commands": {
            "managed-rule.md": {"hash": "def456", "managed": True},
        },
    }
    (rules_dir / ".sova-manifest.json").write_text(json.dumps(rules_manifest))


class TestUninstallCommand:
    def test_uninstall_default_keeps_optional(self, tmp_path: Path) -> None:
        """Default uninstall keeps commands, rules, memory, and config."""
        from sova.cli.commands.project import _uninstall

        _scaffold_full_install(tmp_path)

        with patch("sova.config.registry.list_projects", return_value={}):
            _uninstall(path=tmp_path)

        assert (tmp_path / ".claude" / "commands" / "dummy.md").exists()
        assert (tmp_path / ".claude" / "rules" / "managed-rule.md").exists()
        assert (tmp_path / ".claude" / "agent-memory").is_dir()
        assert (tmp_path / "sova.toml").exists()
        assert not (tmp_path / ".claude" / "sova.db").exists()
        assert not (tmp_path / ".claude" / "worktrees").exists()

    def test_uninstall_remove_commands_flag(self, tmp_path: Path) -> None:
        """--remove-commands removes managed commands, keeps local ones."""
        from sova.cli.commands.project import _uninstall

        _scaffold_full_install(tmp_path)
        commands_dir = tmp_path / ".claude" / "commands"
        (commands_dir / "local-cmd.md").write_text("---\nname: local\n---\n")

        with patch("sova.config.registry.list_projects", return_value={}):
            _uninstall(path=tmp_path, remove_commands=True)

        assert not (commands_dir / "dummy.md").exists()
        assert (commands_dir / "local-cmd.md").exists()
        assert not (commands_dir / ".sova-manifest.json").exists()

    def test_uninstall_remove_rules_flag(self, tmp_path: Path) -> None:
        """--remove-rules removes managed rules/guidelines."""
        from sova.cli.commands.project import _uninstall

        _scaffold_full_install(tmp_path)
        rules_dir = tmp_path / ".claude" / "rules"
        (rules_dir / "local-rule.md").write_text("# Local rule")

        with patch("sova.config.registry.list_projects", return_value={}):
            _uninstall(path=tmp_path, remove_rules=True)

        assert not (rules_dir / "managed-rule.md").exists()
        assert (rules_dir / "local-rule.md").exists()
        assert not (rules_dir / ".sova-manifest.json").exists()

    def test_uninstall_removes_db_always(self, tmp_path: Path) -> None:
        """Database is always removed even without opt-in flags."""
        from sova.cli.commands.project import _uninstall

        _scaffold_full_install(tmp_path)

        with patch("sova.config.registry.list_projects", return_value={}):
            _uninstall(path=tmp_path)

        assert not (tmp_path / ".claude" / "sova.db").exists()
        assert not (tmp_path / ".claude" / "sova.db.bak").exists()
        assert (tmp_path / ".claude" / "agent-memory").is_dir()

    def test_uninstall_remove_memory_flag(self, tmp_path: Path) -> None:
        """--remove-memory removes agent-memory directory."""
        from sova.cli.commands.project import _uninstall

        _scaffold_full_install(tmp_path)

        with patch("sova.config.registry.list_projects", return_value={}):
            _uninstall(path=tmp_path, remove_memory=True)

        assert not (tmp_path / ".claude" / "agent-memory").exists()
        assert (tmp_path / "sova.toml").exists()

    def test_uninstall_remove_config_flag(self, tmp_path: Path) -> None:
        """--remove-config removes sova.toml."""
        from sova.cli.commands.project import _uninstall

        _scaffold_full_install(tmp_path)

        with patch("sova.config.registry.list_projects", return_value={}):
            _uninstall(path=tmp_path, remove_config=True)

        assert not (tmp_path / "sova.toml").exists()
        assert not (tmp_path / ".claude" / "sova.db").exists()

    def test_uninstall_ephemeral_dirs(self, tmp_path: Path) -> None:
        """Worktrees and agent-control dirs are always removed."""
        from sova.cli.commands.project import _uninstall

        _scaffold_full_install(tmp_path)

        with patch("sova.config.registry.list_projects", return_value={}):
            _uninstall(path=tmp_path)

        assert not (tmp_path / ".claude" / "worktrees").exists()
        assert not (tmp_path / ".claude" / "agent-control").exists()

    def test_uninstall_unregisters_project(self, tmp_path: Path) -> None:
        """Project is removed from the registry."""
        from sova.cli.commands.project import _uninstall

        _scaffold_full_install(tmp_path)

        with (
            patch(
                "sova.config.registry.list_projects",
                return_value={"myproj": str(tmp_path)},
            ),
            patch("sova.config.registry.unregister_project") as mock_unreg,
        ):
            _uninstall(path=tmp_path)

        mock_unreg.assert_called_once_with("myproj")

    def test_remove_managed_commands_no_manifest(self, tmp_path: Path) -> None:
        """Without a manifest, no commands are removed."""
        from sova.cli.commands.project import _remove_managed_commands

        tmp_path.mkdir(exist_ok=True)
        (tmp_path / "some-cmd.md").write_text("# test")
        count = _remove_managed_commands(tmp_path)
        assert count == 0
        assert (tmp_path / "some-cmd.md").exists()

    def test_uninstall_no_artifacts(self, tmp_path: Path) -> None:
        """Running uninstall on a project without SOVA is a no-op."""
        from sova.cli.commands.project import _uninstall

        with patch("sova.config.registry.list_projects", return_value={}):
            _uninstall(path=tmp_path)

    def test_uninstall_db_failure_continues_to_next(self, tmp_path: Path) -> None:
        """If one db file fails to delete, remaining files are still attempted."""
        from sova.cli.commands.project import _uninstall

        _scaffold_full_install(tmp_path)
        (tmp_path / ".claude" / "sova.db.bak").write_text("backup")

        with (
            patch("sova.config.registry.list_projects", return_value={}),
            patch.object(Path, "unlink", side_effect=[OSError("locked"), None]) as mock_unlink,
        ):
            failures = _uninstall(path=tmp_path)

        assert any("sova.db" in f for f in failures)
        assert mock_unlink.call_count >= 2

    def test_uninstall_registry_error_is_non_fatal(self, tmp_path: Path) -> None:
        """Registry errors are captured, not propagated."""
        from sova.cli.commands.project import _uninstall

        _scaffold_full_install(tmp_path)

        with patch("sova.config.registry.list_projects", side_effect=OSError("corrupt")):
            failures = _uninstall(path=tmp_path)

        assert any("registry" in f for f in failures)

    def test_uninstall_rtk_hook_failure_is_non_fatal(self, tmp_path: Path) -> None:
        """A raising remove_rtk_hook is captured and reported, not propagated."""
        from sova.cli.commands.project import _uninstall

        _scaffold_full_install(tmp_path)

        with (
            patch("sova.config.registry.list_projects", return_value={}),
            patch("sova.utils.rtk.remove_rtk_hook", side_effect=RuntimeError("settings.json corrupt")),
        ):
            failures = _uninstall(path=tmp_path)

        assert any("RTK hook" in f for f in failures)

    def test_uninstall_mcp_servers_failure_is_non_fatal(self, tmp_path: Path) -> None:
        """A raising remove_mcp_server is captured and reported, not propagated."""
        from sova.cli.commands.project import _uninstall

        _scaffold_full_install(tmp_path)

        with (
            patch("sova.config.registry.list_projects", return_value={}),
            patch("sova.utils.mcp_config.remove_mcp_server", side_effect=RuntimeError("settings.json corrupt")),
        ):
            failures = _uninstall(path=tmp_path)

        assert any("MCP servers" in f for f in failures)

    def test_uninstall_removes_agent_permissions_always(self, tmp_path: Path) -> None:
        """Agent permissions SOVA added are always removed, even without opt-in flags."""
        import json

        from sova.cli.commands.project import _uninstall
        from sova.utils.permissions import inject_agent_permissions

        _scaffold_full_install(tmp_path)
        claude_dir = tmp_path / ".claude"
        settings_path = claude_dir / "settings.json"
        settings_path.write_text(json.dumps({"permissions": {"allow": ["Custom(*)"]}}))
        inject_agent_permissions(claude_dir)

        with patch("sova.config.registry.list_projects", return_value={}):
            _uninstall(path=tmp_path)

        data = json.loads(settings_path.read_text())
        assert data["permissions"]["allow"] == ["Custom(*)"]

    def test_uninstall_preserves_permissions_predating_sova(self, tmp_path: Path) -> None:
        """A permission entry present before sova install was ever run survives sova uninstall."""
        import json

        from sova.cli.commands.project import _uninstall

        _scaffold_full_install(tmp_path)
        settings_path = tmp_path / ".claude" / "settings.json"
        # Bash(*) predates SOVA: it was never recorded as something SOVA added.
        settings_path.write_text(json.dumps({"permissions": {"allow": ["Bash(*)"]}}))

        with patch("sova.config.registry.list_projects", return_value={}):
            _uninstall(path=tmp_path)

        data = json.loads(settings_path.read_text())
        assert data["permissions"]["allow"] == ["Bash(*)"]

    def test_uninstall_permissions_failure_is_non_fatal(self, tmp_path: Path) -> None:
        """A raising remove_agent_permissions is captured and reported, not propagated."""
        from sova.cli.commands.project import _uninstall

        _scaffold_full_install(tmp_path)

        with (
            patch("sova.config.registry.list_projects", return_value={}),
            patch("sova.utils.permissions.remove_agent_permissions", side_effect=RuntimeError("settings.json corrupt")),
        ):
            failures = _uninstall(path=tmp_path)

        assert any("agent permissions" in f for f in failures)

    def test_remove_managed_commands_skips_path_traversal(self, tmp_path: Path) -> None:
        """Manifest entries that escape the managed directory are skipped."""
        import json

        from sova.commands.manifest import MANIFEST_FILENAME

        managed_dir = tmp_path / "commands"
        managed_dir.mkdir()
        safe_file = managed_dir / "safe.md"
        safe_file.write_text("# safe")
        outside_file = tmp_path / "outside.md"
        outside_file.write_text("# outside")

        manifest_data = {
            "version": 1,
            "commands": {
                "safe.md": {"hash": "abc", "managed": True},
                "../outside.md": {"hash": "def", "managed": True},
            },
        }
        manifest_file = managed_dir / MANIFEST_FILENAME
        manifest_file.write_text(json.dumps(manifest_data))

        from sova.cli.commands.project import _remove_managed_commands

        count = _remove_managed_commands(managed_dir)
        assert count == 1
        assert not safe_file.exists()
        assert outside_file.exists()


# ---------------------------------------------------------------------------
# Run command enhancements
# ---------------------------------------------------------------------------


class TestRunCommand:
    def test_run_help(self) -> None:
        from sova.cli.app import app

        result = runner.invoke(app, ["run", "--help"])
        assert result.exit_code == 0
        assert "issue" in result.output.lower()


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

    @pytest.mark.parametrize(
        ("suitability", "expected"),
        [
            # A one-line body fails the quality gate, which downgrades "ready" to needs_spec.
            ("ready", TaskState.NEEDS_SPEC),
            ("human_only", TaskState.HUMAN_ONLY),
            ("interactive", TaskState.TRIAGED),
        ],
    )
    async def test_apply_triage_actions_transitions_to_the_verdict_state(
        self, suitability: str, expected: TaskState
    ) -> None:
        from sova.cli.commands.triage import _apply_triage_actions
        from sova.config.models import TriageConfig
        from sova.roles.base import TaskAssessment
        from sova.roles.triage import TriageRole

        task = Task(id="42", title="Test", body="Short.", state=TaskState.BACKLOG, labels=[])
        assessment = TaskAssessment(suitability=suitability, confidence=0.9, reasoning="r")
        adapter = AsyncMock()

        await _apply_triage_actions(adapter, TriageRole(), task, assessment, TriageConfig(), force=False)

        adapter.transition_state.assert_awaited_once_with("42", expected)


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

    async def test_harden_list_tasks_failure_reports_and_returns(self, tmp_path: Path) -> None:
        """A failing list_tasks call is reported and the command exits cleanly."""
        from sova.cli.commands.harden import _harden

        adapter = AsyncMock()
        adapter.list_tasks.side_effect = RuntimeError("api unavailable")

        with (
            patch("sova.cli.commands.harden.load_config", return_value=MagicMock()),
            patch("sova.db.session.init_db", new_callable=AsyncMock),
            patch("sova.cli.commands.harden.create_adapter", return_value=adapter),
        ):
            await _harden(issue=None, project_dir=tmp_path, dry_run=True, skip_triage=True)

        adapter.list_tasks.assert_awaited_once()

    async def test_retriage_task_exception_returns_none(self) -> None:
        """A raising heuristic_assess is caught, logged, and yields no verdict."""
        from sova.cli.commands.harden import _retriage_task
        from sova.config.models import ProjectConfig

        task = Task(id="10", title="Test", body="", state=TaskState.TRIAGED, labels=[])
        config = ProjectConfig()

        with patch(
            "sova.roles.triage.TriageRole.heuristic_assess",
            side_effect=RuntimeError("assessment blew up"),
        ):
            verdict = await _retriage_task(task, "enriched body", config, AsyncMock(), skip_triage=False)

        assert verdict is None


# ---------------------------------------------------------------------------
# _detect_github_repo / _detect_github_user / _detect_test_command
# ---------------------------------------------------------------------------


class TestDetectGithubRepo:
    @pytest.mark.parametrize(
        ("success", "stdout", "expected"),
        [
            pytest.param(True, "git@github.com:owner/repo.git\n", "owner/repo", id="ssh-url"),
            pytest.param(True, "https://github.com/owner/repo.git\n", "owner/repo", id="https-url"),
            pytest.param(False, "", "", id="git-fails"),
            pytest.param(True, "git@gitlab.com:owner/repo.git\n", "", id="not-github"),
            pytest.param(True, "git@github.com-personal:owner/repo.git\n", "owner/repo", id="ssh-alias"),
        ],
    )
    async def test_detects_repo_from_remote_url(
        self, tmp_path: Path, success: bool, stdout: str, expected: str
    ) -> None:
        from sova.cli.commands.project import _detect_github_repo

        mock_result = MagicMock(success=success, stdout=stdout)
        with patch("sova.cli.commands.project.run", new_callable=AsyncMock, return_value=mock_result):
            assert await _detect_github_repo(tmp_path) == expected


class TestDetectGithubUser:
    @pytest.mark.parametrize(
        ("success", "stdout", "expected"),
        [
            pytest.param(True, "ghp_token123\n", "owner", id="authenticated"),
            pytest.param(False, "", "", id="auth-fails"),
            pytest.param(True, "  \n", "", id="token-empty"),
        ],
    )
    async def test_detects_user_from_auth_token(self, success: bool, stdout: str, expected: str) -> None:
        from sova.cli.commands.project import _detect_github_user

        mock_result = MagicMock(success=success, stdout=stdout)
        with patch("sova.cli.commands.project.run", new_callable=AsyncMock, return_value=mock_result):
            assert await _detect_github_user("owner/repo") == expected

    @pytest.mark.parametrize(
        "repo",
        [pytest.param("", id="no-repo"), pytest.param("justrepo", id="no-slash")],
    )
    async def test_returns_empty_for_unusable_repo(self, repo: str) -> None:
        from sova.cli.commands.project import _detect_github_user

        assert await _detect_github_user(repo) == ""


class TestDetectTestCommand:
    def test_default_make_test(self, tmp_path: Path) -> None:
        from sova.cli.commands.project import _detect_test_command

        assert _detect_test_command(tmp_path) == "make test"

    @pytest.mark.parametrize(
        ("marker_file", "content", "expected"),
        [
            pytest.param("package.json", "{}", "npm test", id="npm"),
            pytest.param("Cargo.toml", "", "cargo test", id="cargo"),
            pytest.param("go.mod", "", "go test ./...", id="go"),
        ],
    )
    def test_detects_from_marker_file(self, tmp_path: Path, marker_file: str, content: str, expected: str) -> None:
        from sova.cli.commands.project import _detect_test_command

        (tmp_path / marker_file).write_text(content)
        assert _detect_test_command(tmp_path) == expected


class TestOfferStarterMilestones:
    _MOD = "sova.cli.commands.project"

    async def test_skips_when_no_task_source(self, tmp_path: Path) -> None:
        """Returns early when task_source.type is empty."""
        from sova.cli.commands.project import _offer_starter_milestones

        mock_cfg = MagicMock()
        mock_cfg.task_source.type = ""

        with patch(f"{self._MOD}.load_config", return_value=mock_cfg):
            await _offer_starter_milestones(tmp_path)

    async def test_skips_when_config_not_found(self, tmp_path: Path) -> None:
        """Returns early when config cannot be loaded."""
        from sova.cli.commands.project import _offer_starter_milestones

        with patch(
            f"{self._MOD}.load_config",
            side_effect=FileNotFoundError("sova.toml not found"),
        ):
            await _offer_starter_milestones(tmp_path)

    async def test_skips_when_adapter_creation_fails(self, tmp_path: Path) -> None:
        """Returns early when create_adapter raises ValueError."""
        from sova.cli.commands.project import _offer_starter_milestones

        mock_cfg = MagicMock()
        mock_cfg.task_source.type = "github"

        with (
            patch(f"{self._MOD}.load_config", return_value=mock_cfg),
            patch(f"{self._MOD}.create_adapter", side_effect=ValueError("bad config")),
        ):
            await _offer_starter_milestones(tmp_path)

    async def test_skips_when_user_declines(self, tmp_path: Path) -> None:
        """Does not create milestones when user declines."""
        from sova.cli.commands.project import _offer_starter_milestones

        mock_cfg = MagicMock()
        mock_cfg.task_source.type = "github"
        mock_adapter = AsyncMock()

        with (
            patch(f"{self._MOD}.load_config", return_value=mock_cfg),
            patch(f"{self._MOD}.create_adapter", return_value=mock_adapter),
            patch("typer.confirm", return_value=False),
        ):
            await _offer_starter_milestones(tmp_path)

    async def test_creates_milestones_on_confirm(self, tmp_path: Path) -> None:
        """Creates milestones when user confirms."""
        from sova.cli.commands.project import _offer_starter_milestones

        mock_cfg = MagicMock()
        mock_cfg.task_source.type = "github"
        mock_adapter = AsyncMock()

        with (
            patch(f"{self._MOD}.load_config", return_value=mock_cfg),
            patch(f"{self._MOD}.create_adapter", return_value=mock_adapter),
            patch("typer.confirm", return_value=True),
            patch(
                f"{self._MOD}.create_starter_milestones",
                new_callable=AsyncMock,
                return_value={"status": "ok", "created": ["Phase 1: Now"], "skipped": [], "failed": []},
            ),
        ):
            await _offer_starter_milestones(tmp_path)

    async def test_handles_create_error(self, tmp_path: Path) -> None:
        """Prints error when milestone creation fails."""
        from sova.cli.commands.project import _offer_starter_milestones

        mock_cfg = MagicMock()
        mock_cfg.task_source.type = "github"
        mock_adapter = AsyncMock()

        with (
            patch(f"{self._MOD}.load_config", return_value=mock_cfg),
            patch(f"{self._MOD}.create_adapter", return_value=mock_adapter),
            patch("typer.confirm", return_value=True),
            patch(
                f"{self._MOD}.create_starter_milestones",
                new_callable=AsyncMock,
                return_value={"status": "error", "detail": "API failure"},
            ),
        ):
            await _offer_starter_milestones(tmp_path)

    async def test_reports_skipped_and_failed(self, tmp_path: Path) -> None:
        """Reports skipped and failed milestones."""
        from sova.cli.commands.project import _offer_starter_milestones

        mock_cfg = MagicMock()
        mock_cfg.task_source.type = "github"
        mock_adapter = AsyncMock()

        with (
            patch(f"{self._MOD}.load_config", return_value=mock_cfg),
            patch(f"{self._MOD}.create_adapter", return_value=mock_adapter),
            patch("typer.confirm", return_value=True),
            patch(
                f"{self._MOD}.create_starter_milestones",
                new_callable=AsyncMock,
                return_value={
                    "status": "ok",
                    "created": ["Phase 2: Next"],
                    "skipped": ["Phase 1: Now"],
                    "failed": [{"title": "Phase 3: Later", "error": "timeout"}],
                },
            ),
        ):
            await _offer_starter_milestones(tmp_path)


# ---------------------------------------------------------------------------
# Skills CLI commands
# ---------------------------------------------------------------------------


class TestSkillsCLICommands:
    _MOD = "sova.cli.commands.commands"

    def test_skills_list_no_dir(self, tmp_path: Path) -> None:
        """skills-list prints 'No skills installed' when dir missing."""
        from sova.cli.app import app

        result = runner.invoke(app, ["commands", "skills-list", "--project", str(tmp_path)])
        assert result.exit_code == 0
        assert "No skills installed" in result.output

    def test_skills_list_shows_installed(self, tmp_path: Path) -> None:
        """skills-list shows each installed skill name."""
        from sova.cli.app import app

        skills_dir = tmp_path / ".claude" / "skills"
        (skills_dir / "testing-patterns").mkdir(parents=True)
        (skills_dir / "testing-patterns" / "SKILL.md").write_text("# Testing")
        (skills_dir / "design-system").mkdir(parents=True)
        (skills_dir / "design-system" / "SKILL.md").write_text("# Design")
        # Directory without SKILL.md should be ignored
        (skills_dir / "empty-dir").mkdir()

        result = runner.invoke(app, ["commands", "skills-list", "--project", str(tmp_path)])
        assert result.exit_code == 0
        assert "design-system" in result.output
        assert "testing-patterns" in result.output
        assert "empty-dir" not in result.output

    def test_skills_diff_up_to_date(self, tmp_path: Path) -> None:
        """skills-diff reports 'up to date' when nothing changed."""
        from sova.cli.app import app
        from sova.commands.distribution import DiffResult

        with (
            patch(f"{self._MOD}.load_config"),
            patch(f"{self._MOD}.get_skills_dir", return_value=tmp_path),
            patch(f"{self._MOD}.diff_skills", return_value=DiffResult()),
        ):
            result = runner.invoke(app, ["commands", "skills-diff", "--project", str(tmp_path)])
        assert result.exit_code == 0
        assert "up to date" in result.output

    def test_skills_diff_shows_changes(self, tmp_path: Path) -> None:
        """skills-diff shows new, changed, and removed skills."""
        from sova.cli.app import app
        from sova.commands.distribution import DiffResult

        diff = DiffResult(new=["new-skill"], changed=["mod-skill"], removed=["old-skill"])
        with (
            patch(f"{self._MOD}.load_config"),
            patch(f"{self._MOD}.get_skills_dir", return_value=tmp_path),
            patch(f"{self._MOD}.diff_skills", return_value=diff),
        ):
            result = runner.invoke(app, ["commands", "skills-diff", "--project", str(tmp_path)])
        assert result.exit_code == 0
        assert "new-skill" in result.output
        assert "mod-skill" in result.output
        assert "old-skill" in result.output

    def test_skills_update_no_conflicts(self, tmp_path: Path) -> None:
        """skills-update prints updated/skipped counts."""
        from sova.cli.app import app
        from sova.commands.distribution import UpdateResult

        with (
            patch(f"{self._MOD}.load_config"),
            patch(f"{self._MOD}.get_skills_dir", return_value=tmp_path),
            patch(f"{self._MOD}.update_skills", return_value=UpdateResult(updated=2, skipped=1)),
        ):
            result = runner.invoke(app, ["commands", "skills-update", "--project", str(tmp_path)])
        assert result.exit_code == 0
        assert "Updated: 2" in result.output
        assert "Skipped (unchanged): 1" in result.output

    def test_skills_update_with_conflicts(self, tmp_path: Path) -> None:
        """skills-update shows conflict details."""
        from sova.cli.app import app
        from sova.commands.distribution import UpdateResult

        ur = UpdateResult(updated=0, skipped=0, conflicts=["testing-patterns"])
        with (
            patch(f"{self._MOD}.load_config"),
            patch(f"{self._MOD}.get_skills_dir", return_value=tmp_path),
            patch(f"{self._MOD}.update_skills", return_value=ur),
        ):
            result = runner.invoke(app, ["commands", "skills-update", "--project", str(tmp_path)])
        assert result.exit_code == 0
        assert "Conflicts (1)" in result.output
        assert "testing-patterns" in result.output
        assert "--force" in result.output

    def test_skills_sync_no_projects(self) -> None:
        """skills-sync tells the user to install first when no projects are registered."""
        from sova.cli.app import app

        with patch(f"{self._MOD}.list_projects", return_value={}):
            result = runner.invoke(app, ["commands", "skills-sync"])
        assert result.exit_code == 0
        assert "No projects registered" in result.output

    def test_skills_sync_across_projects(self, tmp_path: Path) -> None:
        """skills-sync updates each registered project and prints a summary."""
        from sova.cli.app import app
        from sova.commands.distribution import UpdateResult

        proj_a = tmp_path / "a"
        proj_a.mkdir()
        proj_b = tmp_path / "b"
        proj_b.mkdir()

        with (
            patch(f"{self._MOD}.list_projects", return_value={"a": str(proj_a), "b": str(proj_b)}),
            patch(f"{self._MOD}.load_config"),
            patch(f"{self._MOD}.get_skills_dir", return_value=tmp_path),
            patch(f"{self._MOD}.update_skills", return_value=UpdateResult(updated=1, skipped=2)),
        ):
            result = runner.invoke(app, ["commands", "skills-sync"])
        assert result.exit_code == 0
        assert "a: " in result.output
        assert "b: " in result.output
        assert "2 updated, 4 unchanged across 2 project(s)" in result.output

    def test_skills_sync_missing_project_dir(self, tmp_path: Path) -> None:
        """skills-sync skips a registered project whose directory no longer exists."""
        from sova.cli.app import app

        missing = tmp_path / "gone"
        with patch(f"{self._MOD}.list_projects", return_value={"gone": str(missing)}):
            result = runner.invoke(app, ["commands", "skills-sync"])
        assert result.exit_code == 0
        assert "directory not found" in result.output

    def test_skills_sync_load_config_failure(self, tmp_path: Path) -> None:
        """skills-sync skips a project whose config fails to load, without aborting the loop."""
        from sova.cli.app import app

        broken = tmp_path / "broken"
        broken.mkdir()
        with (
            patch(f"{self._MOD}.list_projects", return_value={"broken": str(broken)}),
            patch(f"{self._MOD}.load_config", side_effect=RuntimeError("bad config")),
        ):
            result = runner.invoke(app, ["commands", "skills-sync"])
        assert result.exit_code == 0
        assert "failed to load config" in result.output

    def test_skills_sync_zero_updates(self, tmp_path: Path) -> None:
        """skills-sync renders the dim '+0' status when a project has nothing to update."""
        from sova.cli.app import app
        from sova.commands.distribution import UpdateResult

        proj = tmp_path / "up-to-date"
        proj.mkdir()
        with (
            patch(f"{self._MOD}.list_projects", return_value={"up-to-date": str(proj)}),
            patch(f"{self._MOD}.load_config"),
            patch(f"{self._MOD}.get_skills_dir", return_value=tmp_path),
            patch(f"{self._MOD}.update_skills", return_value=UpdateResult(updated=0, skipped=3)),
        ):
            result = runner.invoke(app, ["commands", "skills-sync"])
        assert result.exit_code == 0
        assert "+0" in result.output
        assert "0 updated, 3 unchanged across 1 project(s)" in result.output


class TestSetupFunction:
    _MOD = "sova.cli.commands.project"

    async def test_setup_calls_helpers_and_offer_milestones(self, tmp_path: Path) -> None:
        """_setup calls detect helpers, saves config to DB, installs, and offers milestones."""
        from sova.cli.commands.project import _setup

        saved_config: dict = {}

        async def _capture_save(session, config):
            from sova.config.db_loader import _flatten_config_dict

            saved_config.update(_flatten_config_dict(config))

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_begin = AsyncMock(__aenter__=AsyncMock(), __aexit__=AsyncMock(return_value=False))
        mock_session.begin = MagicMock(return_value=mock_begin)

        with (
            patch(f"{self._MOD}._detect_github_repo", new_callable=AsyncMock, return_value="owner/repo"),
            patch(f"{self._MOD}._detect_github_user", new_callable=AsyncMock, return_value="owner"),
            patch(f"{self._MOD}._detect_test_command", return_value="make test"),
            patch(f"{self._MOD}._install", new_callable=AsyncMock),
            patch(f"{self._MOD}._offer_starter_milestones", new_callable=AsyncMock),
            patch("sova.db.session.init_db", new_callable=AsyncMock),
            patch("sova.db.session.get_session", new_callable=AsyncMock, return_value=mock_session),
            patch("sova.config.db_loader.save_config_to_db", side_effect=_capture_save),
        ):
            await _setup(path=tmp_path)

        assert saved_config.get("github_repo") == "owner/repo"
        assert saved_config.get("github_user") == "owner"

    async def test_setup_exits_when_init_db_fails(self, tmp_path: Path) -> None:
        """A failing init_db aborts the wizard with a clear error, not a traceback."""
        from sqlalchemy.exc import SQLAlchemyError
        from typer import Exit

        from sova.cli.commands.project import _setup

        with (
            patch(f"{self._MOD}._detect_github_repo", new_callable=AsyncMock, return_value="owner/repo"),
            patch(f"{self._MOD}._detect_github_user", new_callable=AsyncMock, return_value="owner"),
            patch(f"{self._MOD}._detect_test_command", return_value="make test"),
            patch("sova.db.session.init_db", new_callable=AsyncMock, side_effect=SQLAlchemyError("disk full")),
        ):
            with pytest.raises(Exit):
                await _setup(path=tmp_path)

    async def test_setup_exits_when_config_save_fails(self, tmp_path: Path) -> None:
        """A failing save_config_to_db aborts the wizard with a clear error, not a traceback."""
        from typer import Exit

        from sova.cli.commands.project import _setup

        with (
            patch(f"{self._MOD}._detect_github_repo", new_callable=AsyncMock, return_value="owner/repo"),
            patch(f"{self._MOD}._detect_github_user", new_callable=AsyncMock, return_value="owner"),
            patch(f"{self._MOD}._detect_test_command", return_value="make test"),
            patch("sova.db.session.init_db", new_callable=AsyncMock),
            patch("sova.db.session.get_session", side_effect=RuntimeError("session unavailable")),
        ):
            with pytest.raises(Exit):
                await _setup(path=tmp_path)


class TestDbCommand:
    """`sova db status`/`sova db backup` (issue #1205).

    Each test deletes SOVA_DATABASE_URL first: the module-level ``reset_db_rows``
    fixture pins it to the shared in-memory worker DB, which would otherwise take
    priority over the explicit ``--project`` directory these commands target.
    """

    def test_status_reports_not_initialized_for_a_fresh_project(self, tmp_path: Path, monkeypatch) -> None:
        from sova.cli.app import app

        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        result = runner.invoke(app, ["db", "status", "--project", str(tmp_path)])

        assert result.exit_code == 0
        assert "not initialized" in result.output.lower()

    def test_status_does_not_create_claude_dir_on_a_non_project_path(self, tmp_path: Path, monkeypatch) -> None:
        """A read-only diagnostic command must not mutate the filesystem it inspects."""
        from sova.cli.app import app

        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        result = runner.invoke(app, ["db", "status", "--project", str(tmp_path)])

        assert result.exit_code == 0
        assert not (tmp_path / ".claude").exists()

    def test_backup_does_not_create_claude_dir_on_a_non_project_path(self, tmp_path: Path, monkeypatch) -> None:
        from sova.cli.app import app

        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        result = runner.invoke(app, ["db", "backup", "--project", str(tmp_path)])

        assert result.exit_code == 1
        assert not (tmp_path / ".claude").exists()

    def test_status_reports_legacy_database_and_continues_diagnostics(self, tmp_path: Path, monkeypatch) -> None:
        """A database with real tables but no alembic_version must not be reported as empty."""
        import asyncio

        from sqlalchemy.ext.asyncio import create_async_engine

        from sova.cli.app import app
        from sova.db.session import _get_database_url

        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        url = _get_database_url(tmp_path)

        async def _create_legacy_db() -> None:
            from sova.db.models import Base

            engine = create_async_engine(url, connect_args={"check_same_thread": False})
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            await engine.dispose()

        asyncio.run(_create_legacy_db())

        result = runner.invoke(app, ["db", "status", "--project", str(tmp_path)])

        assert result.exit_code == 0
        assert "legacy" in result.output.lower()
        assert "missing orm columns" in result.output.lower()

    def test_status_reports_current_and_head_revision(self, tmp_path: Path, monkeypatch) -> None:
        import asyncio

        from sova.cli.app import app
        from sova.db.session import close_db, init_db

        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        asyncio.run(init_db(project_dir=tmp_path))
        asyncio.run(close_db())

        result = runner.invoke(app, ["db", "status", "--project", str(tmp_path)])

        assert result.exit_code == 0
        assert "Current revision" in result.output
        assert "yes" in result.output.lower()  # at head

    def test_status_reports_schema_drift(self, tmp_path: Path, monkeypatch) -> None:
        """A missing ORM column is surfaced as schema drift, not silently ignored."""
        import asyncio

        from sova.cli.app import app
        from sova.db.session import close_db, init_db

        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        asyncio.run(init_db(project_dir=tmp_path))
        asyncio.run(close_db())

        with patch("sova.db.session._find_missing_columns", return_value=["task_runs.bogus_column"]):
            result = runner.invoke(app, ["db", "status", "--project", str(tmp_path)])

        assert result.exit_code == 0
        assert "Schema drift" in result.output
        assert "task_runs.bogus_column" in result.output

    def test_backup_reports_unsupported_backend_for_postgres(self, tmp_path: Path, monkeypatch) -> None:
        """A Postgres deployment must get an explicit "not supported" message, not a false "nothing to back up"."""
        from sova.cli.app import app

        monkeypatch.setenv("SOVA_DATABASE_URL", "postgresql://user:pass@localhost/sova")
        result = runner.invoke(app, ["db", "backup", "--project", str(tmp_path)])

        assert result.exit_code == 1
        assert "only supported for sqlite" in result.output.lower()

    def test_backup_reports_nothing_to_back_up_for_a_fresh_project(self, tmp_path: Path, monkeypatch) -> None:
        from sova.cli.app import app

        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        result = runner.invoke(app, ["db", "backup", "--project", str(tmp_path)])

        assert result.exit_code == 1
        assert "nothing to back up" in result.output.lower()

    def test_backup_writes_a_file(self, tmp_path: Path, monkeypatch) -> None:
        import asyncio

        from sova.cli.app import app
        from sova.db.session import close_db, init_db

        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        asyncio.run(init_db(project_dir=tmp_path))
        asyncio.run(close_db())

        result = runner.invoke(app, ["db", "backup", "--project", str(tmp_path)])

        assert result.exit_code == 0
        backup_path = tmp_path / ".claude" / "sova.db.bak"
        assert backup_path.exists()
        assert "Backup written to" in result.output

    def test_backup_acquires_migration_lock(self, tmp_path: Path, monkeypatch) -> None:
        """A manual backup must be serialized with migration backup rotation, not race it."""
        import asyncio
        from contextlib import asynccontextmanager

        from sova.cli.app import app
        from sova.db import session as db_session_module
        from sova.db.session import close_db, init_db

        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        asyncio.run(init_db(project_dir=tmp_path))
        asyncio.run(close_db())

        calls: list[str] = []
        real_lock = db_session_module._migration_lock

        @asynccontextmanager
        async def _tracking_lock(url: str):
            calls.append("acquire")
            async with real_lock(url):
                yield
            calls.append("release")

        with patch("sova.db.session._migration_lock", _tracking_lock):
            result = runner.invoke(app, ["db", "backup", "--project", str(tmp_path)])

        assert result.exit_code == 0
        assert calls == ["acquire", "release"]


class TestPrVerdictCommand:
    """`sova pr-verdict` prints the head verdict /integrate-pr gates on."""

    def _invoke(self, *, head: str, review_data: object, handoffs: list | None = None, repo: str = "o/r"):
        from sova.cli.app import app

        cfg = MagicMock(github_repo=repo, github_user="sova-bot")
        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("sova.git.pr.get_pr_head_sha", new_callable=AsyncMock, return_value=head),
            patch("sova.git.pr.get_pr_review_data", new_callable=AsyncMock, return_value={7: review_data}),
            patch("sova.ipc.handoff.read_all_handoff_files", return_value=handoffs or []),
        ):
            return runner.invoke(app, ["pr-verdict", "7", "--project", "."])

    def test_prints_github_marker_verdict(self) -> None:
        history = [
            {
                "state": "COMMENTED",
                "body": "<!-- sova-review: revise sha=abc1234 -->",
                "submitted_at": "1",
                "author": "sova-bot",
            }
        ]
        result = self._invoke(
            head="abc1234", review_data=MagicMock(review_history=history, review_history_truncated=False)
        )

        assert result.exit_code == 0
        assert result.stdout.strip() == "revise"

    def test_local_handoff_covers_a_failed_post(self) -> None:
        from sova.ipc.handoff import DashboardHandoff

        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            pr_number=7,
            summary="review",
            details={"verdict": "block", "review_head_sha": "abc1234"},
        )
        result = self._invoke(
            head="abc1234", review_data=MagicMock(review_history=[], review_history_truncated=False), handoffs=[handoff]
        )

        assert result.stdout.strip() == "block"

    def test_unreadable_head_prints_unknown(self) -> None:
        result = self._invoke(head="", review_data=None)

        assert result.exit_code == 0
        assert result.stdout == "unknown\n"

    def test_gh_failure_prints_unknown_and_exits_zero(self) -> None:
        from sova.cli.app import app

        cfg = MagicMock(github_repo="o/r", github_user="sova-bot")
        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch(
                "sova.git.pr.get_pr_head_sha",
                new_callable=AsyncMock,
                side_effect=RuntimeError("gh pr view failed: network unreachable"),
            ),
        ):
            result = runner.invoke(app, ["pr-verdict", "7", "--project", "."])

        assert result.exit_code == 0
        assert result.stdout == "unknown\n"

    def test_no_repo_prints_unknown(self) -> None:
        result = self._invoke(head="abc1234", review_data=None, repo="")

        assert result.stdout == "unknown\n"
