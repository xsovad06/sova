"""Tests for sova.cli -- CLI commands and Typer app."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

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
    await init_db()
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


def _mock_adapter(state: TaskState = TaskState.BACKLOG) -> AsyncMock:
    adapter = AsyncMock()
    adapter.get_state.return_value = state
    adapter.get_task.return_value = Task(
        id="42", title="Test issue", body="Some description", state=state,
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
