"""Tests for sova.cli -- CLI commands and Typer app."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from sova.adapters.base import Task, TaskState
from sova.config.models import ProjectConfig
from sova.db.session import close_db, init_db

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

    @patch("sova.knowledge.memory.semantic_search")
    @patch("sova.db.session.init_db")
    def test_memory_search_semantic_no_results(self, mock_init, mock_sem_search) -> None:
        from sova.cli.app import app

        mock_init.return_value = None
        mock_sem_search.return_value = []

        result = runner.invoke(app, ["memory", "search", "--semantic", "test query"])
        assert result.exit_code == 0
        assert "No memories found" in result.output

    @patch("sova.knowledge.memory.semantic_search")
    @patch("sova.db.session.init_db")
    def test_memory_search_semantic_with_results(self, mock_init, mock_sem_search) -> None:
        from sova.cli.app import app

        mock_mem = MagicMock()
        mock_mem.id = 1
        mock_mem.category = "learning"
        mock_mem.title = "Bash quoting patterns"
        mock_mem.tier = "project"
        mock_init.return_value = None
        mock_sem_search.return_value = [(mock_mem, 0.95)]

        result = runner.invoke(app, ["memory", "search", "--semantic", "bash"])
        assert result.exit_code == 0
        assert "Bash quoting" in result.output
        assert "0.950" in result.output
        assert "1 result(s)" in result.output

    def test_memory_backfill_embeddings_help(self) -> None:
        from sova.cli.app import app

        result = runner.invoke(app, ["memory", "backfill-embeddings", "--help"])
        assert result.exit_code == 0

    @patch("sova.knowledge.embeddings.is_available", return_value=False)
    @patch("sova.knowledge.embeddings.embed_text")
    @patch("sova.db.session.init_db")
    def test_backfill_embeddings_unavailable(self, mock_init, _mock_embed, _mock_avail) -> None:
        from sova.cli.app import app

        mock_init.return_value = None

        result = runner.invoke(app, ["memory", "backfill-embeddings"])
        assert result.exit_code == 1
        assert "sentence-transformers is not installed" in result.output

    @patch("sova.knowledge.embeddings.is_available", return_value=True)
    @patch("sova.knowledge.embeddings.embed_text", return_value=[0.1, 0.2])
    @patch("sova.db.session.init_db")
    def test_backfill_embeddings_no_memories_to_update(self, mock_init, _mock_embed, _mock_avail) -> None:
        """All memories already have embeddings -- nothing to do."""
        from sova.cli.app import app

        mock_init.return_value = None
        # The autouse setup_db fixture has already created a real in-memory DB,
        # which is empty -- so there are no memories without embeddings.
        result = runner.invoke(app, ["memory", "backfill-embeddings"])
        assert result.exit_code == 0
        assert "All memories already have embeddings" in result.output


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
        _scaffold_install_artifacts(tmp_path)

        with (
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

    async def test_install_creates_all_artifacts(self, tmp_path: Path) -> None:
        """Successful install creates commands dir, agent-memory, and sova.toml."""
        from sova.cli.commands.project import _install

        def _install_cmds_side_effect(_canonical_dir, commands_dir, _cfg):
            commands_dir.mkdir(parents=True, exist_ok=True)
            (commands_dir / "dummy.md").write_text("---\nname: dummy\n---\n")
            return MagicMock(installed=1)

        with (
            patch("sova.db.session.init_db", new_callable=AsyncMock),
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

        assert (tmp_path / "sova.toml").exists()
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
# Doctor install completeness checks
# ---------------------------------------------------------------------------


class TestDoctorInstallChecks:
    def test_check_install_completeness_all_present(self, tmp_path: Path) -> None:
        from sova.cli.commands.doctor import _check_install_completeness

        _scaffold_install_artifacts(tmp_path)
        (tmp_path / ".claude" / "sova.db").write_text("")
        checks = _check_install_completeness(tmp_path)
        assert all(check[1] for check in checks)

    def test_check_install_completeness_missing_commands(self, tmp_path: Path) -> None:
        from sova.cli.commands.doctor import _check_install_completeness

        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "sova.db").write_text("")
        checks = _check_install_completeness(tmp_path)
        cmd_check = next(c for c in checks if c[0] == "commands installed")
        assert cmd_check[1] is False

    def test_check_install_completeness_missing_db(self, tmp_path: Path) -> None:
        from sova.cli.commands.doctor import _check_install_completeness

        _scaffold_install_artifacts(tmp_path)
        checks = _check_install_completeness(tmp_path)
        db_check = next(c for c in checks if c[0] == "database")
        assert db_check[1] is False

    def test_check_install_completeness_includes_permissions(self, tmp_path: Path) -> None:
        """Doctor check reports agent permissions status."""
        import json

        from sova.cli.commands.doctor import _check_install_completeness

        _scaffold_install_artifacts(tmp_path)
        (tmp_path / ".claude" / "sova.db").write_text("")
        settings_path = tmp_path / ".claude" / "settings.json"
        settings_path.write_text(json.dumps({"permissions": {"allow": ["Bash(*)", "Read(*)", "Edit(*)", "Write(*)"]}}))

        checks = _check_install_completeness(tmp_path)
        perm_check = next(c for c in checks if c[0] == "agent permissions")
        assert perm_check[1] is True
        assert perm_check[2] == "configured"

    def test_check_install_completeness_warns_missing_permissions(self, tmp_path: Path) -> None:
        """Doctor check warns when agent permissions are missing."""
        from sova.cli.commands.doctor import _check_install_completeness

        _scaffold_install_artifacts(tmp_path)
        (tmp_path / ".claude" / "sova.db").write_text("")
        # Remove the permissions that _scaffold_install_artifacts creates
        (tmp_path / ".claude" / "settings.json").unlink()

        checks = _check_install_completeness(tmp_path)
        perm_check = next(c for c in checks if c[0] == "agent permissions")
        assert perm_check[1] is False
        assert "missing" in perm_check[2].lower()
        assert "sova install --update" in perm_check[2]


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
    def test_uninstall_help(self) -> None:
        from sova.cli.app import app

        result = runner.invoke(app, ["uninstall", "--help"])
        assert result.exit_code == 0

    async def test_uninstall_default_keeps_optional(self, tmp_path: Path) -> None:
        """Default uninstall keeps commands, rules, memory, and config."""
        from sova.cli.commands.project import _uninstall

        _scaffold_full_install(tmp_path)

        with patch("sova.config.registry.list_projects", return_value={}):
            await _uninstall(path=tmp_path)

        assert (tmp_path / ".claude" / "commands" / "dummy.md").exists()
        assert (tmp_path / ".claude" / "rules" / "managed-rule.md").exists()
        assert (tmp_path / ".claude" / "agent-memory").is_dir()
        assert (tmp_path / "sova.toml").exists()
        assert not (tmp_path / ".claude" / "sova.db").exists()
        assert not (tmp_path / ".claude" / "worktrees").exists()

    async def test_uninstall_remove_commands_flag(self, tmp_path: Path) -> None:
        """--remove-commands removes managed commands, keeps local ones."""
        from sova.cli.commands.project import _uninstall

        _scaffold_full_install(tmp_path)
        commands_dir = tmp_path / ".claude" / "commands"
        (commands_dir / "local-cmd.md").write_text("---\nname: local\n---\n")

        with patch("sova.config.registry.list_projects", return_value={}):
            await _uninstall(path=tmp_path, remove_commands=True)

        assert not (commands_dir / "dummy.md").exists()
        assert (commands_dir / "local-cmd.md").exists()
        assert not (commands_dir / ".sova-manifest.json").exists()

    async def test_uninstall_remove_rules_flag(self, tmp_path: Path) -> None:
        """--remove-rules removes managed rules/guidelines."""
        from sova.cli.commands.project import _uninstall

        _scaffold_full_install(tmp_path)
        rules_dir = tmp_path / ".claude" / "rules"
        (rules_dir / "local-rule.md").write_text("# Local rule")

        with patch("sova.config.registry.list_projects", return_value={}):
            await _uninstall(path=tmp_path, remove_rules=True)

        assert not (rules_dir / "managed-rule.md").exists()
        assert (rules_dir / "local-rule.md").exists()
        assert not (rules_dir / ".sova-manifest.json").exists()

    async def test_uninstall_removes_db_always(self, tmp_path: Path) -> None:
        """Database is always removed even without opt-in flags."""
        from sova.cli.commands.project import _uninstall

        _scaffold_full_install(tmp_path)

        with patch("sova.config.registry.list_projects", return_value={}):
            await _uninstall(path=tmp_path)

        assert not (tmp_path / ".claude" / "sova.db").exists()
        assert not (tmp_path / ".claude" / "sova.db.bak").exists()
        assert (tmp_path / ".claude" / "agent-memory").is_dir()

    async def test_uninstall_remove_memory_flag(self, tmp_path: Path) -> None:
        """--remove-memory removes agent-memory directory."""
        from sova.cli.commands.project import _uninstall

        _scaffold_full_install(tmp_path)

        with patch("sova.config.registry.list_projects", return_value={}):
            await _uninstall(path=tmp_path, remove_memory=True)

        assert not (tmp_path / ".claude" / "agent-memory").exists()
        assert (tmp_path / "sova.toml").exists()

    async def test_uninstall_remove_config_flag(self, tmp_path: Path) -> None:
        """--remove-config removes sova.toml."""
        from sova.cli.commands.project import _uninstall

        _scaffold_full_install(tmp_path)

        with patch("sova.config.registry.list_projects", return_value={}):
            await _uninstall(path=tmp_path, remove_config=True)

        assert not (tmp_path / "sova.toml").exists()
        assert not (tmp_path / ".claude" / "sova.db").exists()

    async def test_uninstall_ephemeral_dirs(self, tmp_path: Path) -> None:
        """Worktrees and agent-control dirs are always removed."""
        from sova.cli.commands.project import _uninstall

        _scaffold_full_install(tmp_path)

        with patch("sova.config.registry.list_projects", return_value={}):
            await _uninstall(path=tmp_path)

        assert not (tmp_path / ".claude" / "worktrees").exists()
        assert not (tmp_path / ".claude" / "agent-control").exists()

    async def test_uninstall_unregisters_project(self, tmp_path: Path) -> None:
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
            await _uninstall(path=tmp_path)

        mock_unreg.assert_called_once_with("myproj")

    def test_remove_managed_commands_no_manifest(self, tmp_path: Path) -> None:
        """Without a manifest, no commands are removed."""
        from sova.cli.commands.project import _remove_managed_commands

        tmp_path.mkdir(exist_ok=True)
        (tmp_path / "some-cmd.md").write_text("# test")
        count = _remove_managed_commands(tmp_path)
        assert count == 0
        assert (tmp_path / "some-cmd.md").exists()

    async def test_uninstall_no_artifacts(self, tmp_path: Path) -> None:
        """Running uninstall on a project without SOVA is a no-op."""
        from sova.cli.commands.project import _uninstall

        with patch("sova.config.registry.list_projects", return_value={}):
            await _uninstall(path=tmp_path)

    async def test_uninstall_db_failure_continues_to_next(self, tmp_path: Path) -> None:
        """If one db file fails to delete, remaining files are still attempted."""
        from sova.cli.commands.project import _uninstall

        _scaffold_full_install(tmp_path)
        (tmp_path / ".claude" / "sova.db.bak").write_text("backup")

        with (
            patch("sova.config.registry.list_projects", return_value={}),
            patch.object(Path, "unlink", side_effect=[OSError("locked"), None]) as mock_unlink,
        ):
            failures = await _uninstall(path=tmp_path)

        assert any("sova.db" in f for f in failures)
        assert mock_unlink.call_count >= 2

    async def test_uninstall_registry_error_is_non_fatal(self, tmp_path: Path) -> None:
        """Registry errors are captured, not propagated."""
        from sova.cli.commands.project import _uninstall

        _scaffold_full_install(tmp_path)

        with patch("sova.config.registry.list_projects", side_effect=OSError("corrupt")):
            failures = await _uninstall(path=tmp_path)

        assert any("registry" in f for f in failures)

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

    async def test_check_git_identity_configured(self) -> None:
        from sova.cli.commands.doctor import _check_git_identity
        from sova.utils.shell import GitIdentityResult

        identity = GitIdentityResult(name="Test User", email="test@example.com")
        with patch("sova.cli.commands.doctor.check_git_identity", new_callable=AsyncMock, return_value=identity):
            checks = await _check_git_identity(Path("/tmp/test"))
        assert len(checks) == 1
        assert checks[0][0] == "git identity"
        assert checks[0][1] is True
        assert "Test User" in checks[0][2]

    async def test_check_git_identity_missing(self) -> None:
        from sova.cli.commands.doctor import _check_git_identity
        from sova.utils.shell import GitIdentityResult

        identity = GitIdentityResult(name="", email="")
        with patch("sova.cli.commands.doctor.check_git_identity", new_callable=AsyncMock, return_value=identity):
            checks = await _check_git_identity(Path("/tmp/test"))
        assert len(checks) == 1
        assert checks[0][0] == "git identity"
        assert checks[0][1] is False
        assert "user.name" in checks[0][2]
        assert checks[0][3] is True

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


# ---------------------------------------------------------------------------
# _detect_github_repo / _detect_github_user / _detect_test_command
# ---------------------------------------------------------------------------


class TestDetectGithubRepo:
    async def test_returns_repo_from_ssh_url(self, tmp_path: Path) -> None:
        from sova.cli.commands.project import _detect_github_repo

        mock_result = MagicMock(success=True, stdout="git@github.com:owner/repo.git\n")
        with patch("sova.cli.commands.project.run", new_callable=AsyncMock, return_value=mock_result):
            assert await _detect_github_repo(tmp_path) == "owner/repo"

    async def test_returns_repo_from_https_url(self, tmp_path: Path) -> None:
        from sova.cli.commands.project import _detect_github_repo

        mock_result = MagicMock(success=True, stdout="https://github.com/owner/repo.git\n")
        with patch("sova.cli.commands.project.run", new_callable=AsyncMock, return_value=mock_result):
            assert await _detect_github_repo(tmp_path) == "owner/repo"

    async def test_returns_empty_when_git_fails(self, tmp_path: Path) -> None:
        from sova.cli.commands.project import _detect_github_repo

        mock_result = MagicMock(success=False, stdout="")
        with patch("sova.cli.commands.project.run", new_callable=AsyncMock, return_value=mock_result):
            assert await _detect_github_repo(tmp_path) == ""

    async def test_returns_empty_when_not_github(self, tmp_path: Path) -> None:
        from sova.cli.commands.project import _detect_github_repo

        mock_result = MagicMock(success=True, stdout="git@gitlab.com:owner/repo.git\n")
        with patch("sova.cli.commands.project.run", new_callable=AsyncMock, return_value=mock_result):
            assert await _detect_github_repo(tmp_path) == ""

    async def test_handles_ssh_alias(self, tmp_path: Path) -> None:
        from sova.cli.commands.project import _detect_github_repo

        mock_result = MagicMock(success=True, stdout="git@github.com-personal:owner/repo.git\n")
        with patch("sova.cli.commands.project.run", new_callable=AsyncMock, return_value=mock_result):
            assert await _detect_github_repo(tmp_path) == "owner/repo"


class TestDetectGithubUser:
    async def test_returns_user_when_authenticated(self) -> None:
        from sova.cli.commands.project import _detect_github_user

        mock_result = MagicMock(success=True, stdout="ghp_token123\n")
        with patch("sova.cli.commands.project.run", new_callable=AsyncMock, return_value=mock_result):
            assert await _detect_github_user("owner/repo") == "owner"

    async def test_returns_empty_when_no_repo(self) -> None:
        from sova.cli.commands.project import _detect_github_user

        assert await _detect_github_user("") == ""

    async def test_returns_empty_when_no_slash(self) -> None:
        from sova.cli.commands.project import _detect_github_user

        assert await _detect_github_user("justrepo") == ""

    async def test_returns_empty_when_auth_fails(self) -> None:
        from sova.cli.commands.project import _detect_github_user

        mock_result = MagicMock(success=False, stdout="")
        with patch("sova.cli.commands.project.run", new_callable=AsyncMock, return_value=mock_result):
            assert await _detect_github_user("owner/repo") == ""

    async def test_returns_empty_when_token_empty(self) -> None:
        from sova.cli.commands.project import _detect_github_user

        mock_result = MagicMock(success=True, stdout="  \n")
        with patch("sova.cli.commands.project.run", new_callable=AsyncMock, return_value=mock_result):
            assert await _detect_github_user("owner/repo") == ""


class TestDetectTestCommand:
    def test_default_make_test(self, tmp_path: Path) -> None:
        from sova.cli.commands.project import _detect_test_command

        assert _detect_test_command(tmp_path) == "make test"

    def test_detects_npm(self, tmp_path: Path) -> None:
        from sova.cli.commands.project import _detect_test_command

        (tmp_path / "package.json").write_text("{}")
        assert _detect_test_command(tmp_path) == "npm test"

    def test_detects_cargo(self, tmp_path: Path) -> None:
        from sova.cli.commands.project import _detect_test_command

        (tmp_path / "Cargo.toml").write_text("")
        assert _detect_test_command(tmp_path) == "cargo test"

    def test_detects_go(self, tmp_path: Path) -> None:
        from sova.cli.commands.project import _detect_test_command

        (tmp_path / "go.mod").write_text("")
        assert _detect_test_command(tmp_path) == "go test ./..."


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


class TestSetupFunction:
    _MOD = "sova.cli.commands.project"

    async def test_setup_calls_helpers_and_offer_milestones(self, tmp_path: Path) -> None:
        """_setup calls detect helpers, writes toml, installs, and offers milestones."""
        from sova.cli.commands.project import _setup

        with (
            patch(f"{self._MOD}._detect_github_repo", new_callable=AsyncMock, return_value="owner/repo"),
            patch(f"{self._MOD}._detect_github_user", new_callable=AsyncMock, return_value="owner"),
            patch(f"{self._MOD}._detect_test_command", return_value="make test"),
            patch(f"{self._MOD}._install", new_callable=AsyncMock),
            patch(f"{self._MOD}._offer_starter_milestones", new_callable=AsyncMock),
        ):
            await _setup(path=tmp_path)

        assert (tmp_path / "sova.toml").exists()
