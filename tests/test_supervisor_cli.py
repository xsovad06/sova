"""Tests for sova.cli.commands.supervisor: status and poll CLI commands."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from sova.cli.commands.supervisor import app as supervisor_app
from sova.config.models import ProjectConfig, SupervisorConfig
from sova.db.session import close_db, init_db

runner = CliRunner()

# load_config is imported inside the command functions, so patch at the source module.
_PATCH_LOAD_CONFIG = "sova.config.loader.load_config"
# asyncio is imported at module level, so patch on the commands module.
_PATCH_ASYNCIO = "sova.cli.commands.supervisor.asyncio"


@pytest.fixture(autouse=True)
async def setup_db():
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


def _supervisor_config(enabled: bool = True) -> ProjectConfig:
    return ProjectConfig(
        supervisor=SupervisorConfig(
            enabled=enabled,
            poll_interval_seconds=60,
            log_retention_days=14,
        ),
        github_repo="test/repo",
    )


class TestSupervisorStatusCommand:
    def test_status_help(self) -> None:
        result = runner.invoke(supervisor_app, ["status", "--help"])
        assert result.exit_code == 0
        assert "status" in result.output.lower() or "daemon" in result.output.lower()

    @patch(_PATCH_LOAD_CONFIG)
    def test_status_json_enabled(self, mock_config) -> None:
        mock_config.return_value = _supervisor_config(enabled=True)
        result = runner.invoke(supervisor_app, ["status", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["enabled"] is True
        assert data["poll_interval_seconds"] == 60
        assert data["log_retention_days"] == 14

    @patch(_PATCH_LOAD_CONFIG)
    def test_status_json_disabled(self, mock_config) -> None:
        mock_config.return_value = _supervisor_config(enabled=False)
        result = runner.invoke(supervisor_app, ["status", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["enabled"] is False

    @patch(_PATCH_LOAD_CONFIG)
    def test_status_disabled_shows_message(self, mock_config) -> None:
        mock_config.return_value = _supervisor_config(enabled=False)
        result = runner.invoke(supervisor_app, ["status"])
        assert result.exit_code == 0
        assert "disabled" in result.output.lower()

    @patch(_PATCH_ASYNCIO)
    @patch(_PATCH_LOAD_CONFIG)
    def test_status_enabled_shows_config(self, mock_config, mock_asyncio) -> None:
        mock_config.return_value = _supervisor_config(enabled=True)
        mock_asyncio.run.return_value = None
        result = runner.invoke(supervisor_app, ["status"])
        assert result.exit_code == 0
        assert "60" in result.output
        assert "14" in result.output
        mock_asyncio.run.assert_called_once()

    @patch(_PATCH_ASYNCIO)
    @patch(_PATCH_LOAD_CONFIG)
    def test_status_enabled_with_project_path(self, mock_config, mock_asyncio, tmp_path) -> None:
        (tmp_path / "sova.toml").write_text("[task_source]\ntype = 'github'\n")
        mock_config.return_value = _supervisor_config(enabled=True)
        mock_asyncio.run.return_value = None
        result = runner.invoke(supervisor_app, ["status", "--project", str(tmp_path)])
        assert result.exit_code == 0
        mock_asyncio.run.assert_called_once()


class TestSupervisorPollCommand:
    def test_poll_help(self) -> None:
        result = runner.invoke(supervisor_app, ["poll", "--help"])
        assert result.exit_code == 0

    @patch(_PATCH_LOAD_CONFIG)
    def test_poll_disabled_exits_with_error(self, mock_config) -> None:
        mock_config.return_value = _supervisor_config(enabled=False)
        result = runner.invoke(supervisor_app, ["poll"])
        assert result.exit_code == 1
        assert "disabled" in result.output.lower()

    @patch(_PATCH_ASYNCIO)
    @patch(_PATCH_LOAD_CONFIG)
    def test_poll_success_shows_output(self, mock_config, mock_asyncio) -> None:
        mock_config.return_value = _supervisor_config(enabled=True)
        mock_asyncio.run.return_value = {
            "progression": {"decisions": 2, "executed": 1},
            "quota": {"enabled": False},
            "health": {"db": "ok", "adapter": "ok (5 tasks)"},
        }
        result = runner.invoke(supervisor_app, ["poll"])
        assert result.exit_code == 0
        assert "completed" in result.output.lower()
        assert "progression" in result.output

    @patch(_PATCH_ASYNCIO)
    @patch(_PATCH_LOAD_CONFIG)
    def test_poll_json_output(self, mock_config, mock_asyncio) -> None:
        mock_config.return_value = _supervisor_config(enabled=True)
        poll_result = {
            "progression": {"decisions": 0, "executed": 0},
            "quota": {"enabled": False},
            "health": {"db": "ok"},
        }
        mock_asyncio.run.return_value = poll_result
        result = runner.invoke(supervisor_app, ["poll", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "progression" in data
        assert data["progression"]["decisions"] == 0

    @patch(_PATCH_ASYNCIO)
    @patch(_PATCH_LOAD_CONFIG)
    def test_poll_with_error_component(self, mock_config, mock_asyncio) -> None:
        mock_config.return_value = _supervisor_config(enabled=True)
        mock_asyncio.run.return_value = {
            "progression": {"error": "adapter failed"},
            "quota": {"enabled": False},
            "health": {"db": "ok"},
        }
        result = runner.invoke(supervisor_app, ["poll"])
        assert result.exit_code == 0
        assert "adapter failed" in result.output

    @patch(_PATCH_ASYNCIO)
    @patch(_PATCH_LOAD_CONFIG)
    def test_poll_with_project_path(self, mock_config, mock_asyncio, tmp_path) -> None:
        (tmp_path / "sova.toml").write_text("[task_source]\ntype = 'github'\n")
        mock_config.return_value = _supervisor_config(enabled=True)
        mock_asyncio.run.return_value = {"progression": {"decisions": 0, "executed": 0}}
        result = runner.invoke(supervisor_app, ["poll", "--project", str(tmp_path)])
        assert result.exit_code == 0
        mock_asyncio.run.assert_called_once()


class TestSupervisorStatusAsyncPath:
    """Tests that exercise the async _show_recent() path without mocking asyncio.run."""

    @patch("sova.dashboard.services.supervisor_service.get_recent_decisions", new_callable=AsyncMock, return_value=[])
    @patch("sova.db.session.init_db", new_callable=AsyncMock)
    @patch(_PATCH_LOAD_CONFIG)
    def test_status_enabled_no_decisions(self, mock_config, mock_init_db, mock_decisions) -> None:
        mock_config.return_value = _supervisor_config(enabled=True)
        result = runner.invoke(supervisor_app, ["status"])
        assert result.exit_code == 0
        assert "No recent decisions" in result.output

    @patch(
        "sova.dashboard.services.supervisor_service.get_recent_decisions",
        new_callable=AsyncMock,
        return_value=[
            {
                "created_at": "2026-07-24T10:00:00+00:00",
                "component": "progression",
                "action": "spawn_developer",
                "issue_number": "42",
                "detail": "Dependencies met",
            }
        ],
    )
    @patch("sova.db.session.init_db", new_callable=AsyncMock)
    @patch(_PATCH_LOAD_CONFIG)
    def test_status_enabled_with_decisions(self, mock_config, mock_init_db, mock_decisions) -> None:
        mock_config.return_value = _supervisor_config(enabled=True)
        result = runner.invoke(supervisor_app, ["status"])
        assert result.exit_code == 0
        assert "progression" in result.output
        assert "42" in result.output

    @patch(
        "sova.dashboard.services.supervisor_service.get_recent_decisions",
        new_callable=AsyncMock,
        return_value=[
            {
                "created_at": "2026-07-24T10:00:00+00:00",
                "component": "health",
                "action": "error",
                "issue_number": None,
                "detail": "Adapter check failed",
            }
        ],
    )
    @patch("sova.db.session.init_db", new_callable=AsyncMock)
    @patch(_PATCH_LOAD_CONFIG)
    def test_status_enabled_with_error_decision(self, mock_config, mock_init_db, mock_decisions) -> None:
        mock_config.return_value = _supervisor_config(enabled=True)
        result = runner.invoke(supervisor_app, ["status"])
        assert result.exit_code == 0
        assert "health" in result.output


class TestSupervisorPollAsyncPath:
    """Tests that exercise the async _run_poll() path without mocking asyncio.run."""

    @patch(
        "sova.supervisor.daemon.SupervisorDaemon.poll_once",
        new_callable=AsyncMock,
        return_value={"progression": {"decisions": 1, "executed": 0}},
    )
    @patch("sova.db.session.get_session_factory", new_callable=AsyncMock)
    @patch("sova.db.session.init_db", new_callable=AsyncMock)
    @patch(_PATCH_LOAD_CONFIG)
    def test_poll_runs_full_async_path(self, mock_config, mock_init_db, mock_sf, mock_poll) -> None:
        mock_config.return_value = _supervisor_config(enabled=True)
        result = runner.invoke(supervisor_app, ["poll"])
        assert result.exit_code == 0
        assert "completed" in result.output.lower()
