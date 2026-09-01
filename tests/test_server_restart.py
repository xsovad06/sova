"""Tests for server restart functionality."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from sova.dashboard.app import create_app


@pytest.fixture
def mock_project_dir(tmp_path: Path) -> Path:
    """Create a minimal project directory."""
    project_dir = tmp_path / "test_project"
    project_dir.mkdir(parents=True)
    (project_dir / ".claude").mkdir()
    (project_dir / ".claude" / "agent-control").mkdir()

    # Create minimal sova.toml
    config = project_dir / "sova.toml"
    config.write_text("""
[project]
github_repo = "test/repo"
github_user = "testuser"
base_branch = "main"
""")

    return project_dir


@pytest.fixture
async def client(mock_project_dir: Path):
    """Create test client with mocked app."""
    app = create_app(project_dir=mock_project_dir)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


class TestRestartMetadata:
    """Tests for requires_restart metadata."""

    def test_setting_meta_has_requires_restart_field(self):
        """SettingMeta dataclass should include requires_restart field."""
        from sova.dashboard.settings_meta import SettingMeta

        meta = SettingMeta(
            key="test.key",
            label="Test",
            description="Test setting",
            group="test",
            requires_restart=True,
        )

        assert meta.requires_restart is True

    def test_restart_required_settings_marked(self):
        """Settings requiring restart should have requires_restart=True."""
        from sova.dashboard.settings_meta import _REGISTRY

        restart_keys = {
            "server.host",
            "server.port",
            "server.scheduler_enabled",
            "watch.interval_active",
            "watch.interval_idle",
            "pr_monitor.enabled",
            "pr_monitor.poll_interval",
            "supervisor.enabled",
        }

        for meta in _REGISTRY:
            if meta.key in restart_keys:
                assert meta.requires_restart is True, f"{meta.key} should require restart"
            elif meta.key.startswith("supervisor.") and meta.key != "supervisor.enabled":
                assert not getattr(meta, "requires_restart", False), (
                    f"{meta.key} should not require restart (hot-reloaded)"
                )

    def test_grouped_config_includes_restart_flag(self):
        """get_grouped_config should include requires_restart in settings."""
        from sova.dashboard.settings_meta import get_grouped_config

        flat_config = {
            "server.host": "localhost",
            "server.port": 8111,
            "agent.model": "sonnet",
        }

        groups = get_grouped_config(flat_config)
        server_group = next(g for g in groups if g["id"] == "server")

        host_setting = next(s for s in server_group["settings"] if s["key"] == "server.host")
        assert host_setting.get("requires_restart") is True


class TestRestartEndpoint:
    """Tests for POST /api/server/restart endpoint."""

    @pytest.mark.skip(reason="Causes SIGHUP in CI (issue #891)")
    @pytest.mark.asyncio
    async def test_restart_endpoint_exists(self, client: AsyncClient):
        """POST /api/server/restart should exist."""
        response = await client.post("/api/server/restart", json={})
        # Should not be 404
        assert response.status_code != 404

    @pytest.mark.asyncio
    async def test_restart_preflight_counts_agents(self, client: AsyncClient, mock_project_dir: Path):
        """GET /api/server/restart/preflight should count active agents."""
        # Mock active agents
        from sova.dashboard.services.agent_pool import _get_project_agents

        pa = _get_project_agents()
        pa.agents = {
            1: Mock(run_id=1, issue="42", role="developer"),
            2: Mock(run_id=2, issue="43", role="reviewer"),
        }

        response = await client.get("/api/server/restart/preflight")
        assert response.status_code == 200

        data = response.json()
        assert data["active_agents"] == 2
        assert data["supervisor_running"] in {True, False}

    @pytest.mark.asyncio
    @patch("sova.dashboard.routers.server._write_restart_marker")
    @patch("sova.dashboard.routers.server._send_sighup")
    async def test_restart_writes_marker_and_signals(
        self,
        mock_sighup: Mock,
        mock_marker: Mock,
        client: AsyncClient,
    ):
        """POST /api/server/restart should write marker and send SIGHUP."""
        mock_sighup.return_value = True

        response = await client.post("/api/server/restart", json={})
        assert response.status_code == 200

        mock_marker.assert_called_once()
        mock_sighup.assert_called_once()

    @pytest.mark.asyncio
    @patch("sova.dashboard.routers.server._write_restart_marker")
    @patch("sova.dashboard.routers.server._send_sighup")
    async def test_restart_fallback_when_signal_fails(
        self,
        mock_sighup: Mock,
        mock_marker: Mock,
        client: AsyncClient,
    ):
        """When SIGHUP fails, should return manual instruction."""
        mock_sighup.return_value = False

        response = await client.post("/api/server/restart", json={})
        assert response.status_code == 200

        data = response.json()
        assert data["action"] == "restart_required"
        assert "sova server restart" in data["instruction"]

    @pytest.mark.asyncio
    async def test_restart_drain_waits_for_agents(self, client: AsyncClient):
        """POST /api/server/restart with drain=true should wait for agents."""
        from sova.dashboard.services.agent_pool import _get_project_agents

        pa = _get_project_agents()

        # Mock one agent
        mock_agent = Mock(run_id=1, issue="42", role="developer")
        pa.agents = {1: mock_agent}

        # Mock agent completing after 0.1s
        async def simulate_completion():
            await asyncio.sleep(0.1)
            pa.agents.clear()

        asyncio.create_task(simulate_completion())

        with patch("sova.dashboard.routers.server._send_sighup", return_value=True):
            response = await client.post("/api/server/restart", json={"drain": True})
            assert response.status_code == 200
            assert response.json()["action"] == "restarted"


class TestDrainMode:
    """Tests for drain mode functionality."""

    @pytest.mark.asyncio
    async def test_drain_disables_supervisor(self, mock_project_dir: Path):
        """Drain should disable supervisor temporarily."""
        from sova.dashboard.routers.server import _disable_supervisor

        with patch("sova.dashboard.routers.supervisor._get_daemon") as mock_get_daemon:
            mock_daemon = Mock()
            mock_daemon.running = True
            mock_daemon.stop = AsyncMock()
            mock_get_daemon.return_value = mock_daemon

            await _disable_supervisor(mock_project_dir)
            mock_daemon.stop.assert_called_once()

    @pytest.mark.skip(reason="Hangs in test environment (issue #891)")
    @pytest.mark.asyncio
    async def test_drain_timeout_after_30_minutes(self, client: AsyncClient):
        """Drain should timeout after 30 minutes."""
        from sova.dashboard.services.agent_pool import _get_project_agents

        pa = _get_project_agents()
        # Agent never completes
        pa.agents = {1: Mock(run_id=1, issue="42", role="developer")}

        with patch("sova.dashboard.routers.server._DRAIN_POLL_INTERVAL", 0.02):
            with patch("sova.dashboard.routers.server._DRAIN_TIMEOUT_SECONDS", 0.1):
                with patch("sova.dashboard.routers.server._send_sighup", return_value=True):
                    response = await client.post("/api/server/restart", json={"drain": True})

                    assert response.status_code == 200
                    data = response.json()
                    assert data["action"] == "drain_timeout"
                    assert data["remaining_agents"] == 1

    @pytest.mark.asyncio
    async def test_drain_detects_new_agents_during_wait(self):
        """Drain should warn if agent count increases during drain."""
        from sova.dashboard.routers.server import _wait_for_agents
        from sova.dashboard.services.agent_pool import _get_project_agents

        pa = _get_project_agents()
        pa.agents = {1: Mock(run_id=1, issue="42", role="developer")}

        # Simulate new agent starting during drain
        async def add_agent():
            await asyncio.sleep(0.05)
            pa.agents[2] = Mock(run_id=2, issue="43", role="reviewer")

        asyncio.create_task(add_agent())

        with patch("sova.dashboard.routers.server._DRAIN_POLL_INTERVAL", 0.02):
            with patch("sova.dashboard.routers.server._DRAIN_TIMEOUT_SECONDS", 0.2):
                result = await _wait_for_agents(None, initial_count=1, timeout=0.2)
                assert result["action"] == "drain_timeout"
                assert result.get("warning") is not None


class TestMultiProjectMode:
    """Tests for multi-project agent counting."""

    @pytest.mark.asyncio
    async def test_count_agents_across_all_projects(self):
        """count_active_agents should sum across all projects."""
        from sova.dashboard.routers.server import count_active_agents
        from sova.dashboard.services import agent_pool

        # Clear any existing pools from other tests
        agent_pool._projects.clear()

        # Create agents in multiple projects
        pa1 = agent_pool._get_project_agents("project1")
        pa1.agents = {1: Mock(), 2: Mock()}

        pa2 = agent_pool._get_project_agents("project2")
        pa2.agents = {3: Mock()}

        total = count_active_agents()
        assert total == 3

        # Cleanup
        agent_pool._projects.clear()


class TestRestartMarker:
    """Tests for restart marker file."""

    def test_marker_file_written_to_agent_control(self, mock_project_dir: Path):
        """Restart marker should be written to .claude/agent-control/restart-requested."""
        from sova.dashboard.routers.server import _write_restart_marker

        _write_restart_marker(mock_project_dir)

        marker = mock_project_dir / ".claude" / "agent-control" / "restart-requested"
        assert marker.exists()
        assert marker.read_text().strip() != ""

    def test_marker_includes_pid(self, mock_project_dir: Path):
        """Marker file should include the server PID."""
        from sova.dashboard.routers.server import _write_restart_marker

        _write_restart_marker(mock_project_dir)

        marker = mock_project_dir / ".claude" / "agent-control" / "restart-requested"
        content = marker.read_text()
        assert str(os.getpid()) in content

    def test_marker_write_failure_does_not_crash(self, mock_project_dir: Path):
        """Marker write failure should be logged but not crash restart."""
        from sova.dashboard.routers.server import _write_restart_marker

        # Make directory read-only
        control_dir = mock_project_dir / ".claude" / "agent-control"
        control_dir.chmod(0o444)

        # Should not raise
        try:
            _write_restart_marker(mock_project_dir)
        finally:
            # Restore permissions for cleanup
            control_dir.chmod(0o755)

    @patch("sova.dashboard.routers.server.signal.SIGHUP", new=None)
    def test_sighup_fallback_on_windows(self):
        """SIGHUP unavailable on Windows should return False."""
        from sova.dashboard.routers.server import _send_sighup

        result = _send_sighup()
        assert result is False
