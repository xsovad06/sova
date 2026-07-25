"""Tests for the supervisor dashboard API router and service."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from sova.dashboard.services.supervisor_service import get_decision_counts, get_recent_decisions
from sova.db.models import SupervisorDecision
from sova.db.session import close_db, get_session, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


@pytest.fixture
async def seed_decisions():
    session = await get_session()
    async with session.begin():
        session.add_all(
            [
                SupervisorDecision(
                    component="progression",
                    event_type="decision",
                    action="spawn_developer",
                    issue_number="42",
                    detail="Dependencies met",
                    project_slug="test/repo",
                ),
                SupervisorDecision(
                    component="quota",
                    event_type="status",
                    action="ok",
                    detail="2/4 reviews in window",
                    project_slug="test/repo",
                ),
                SupervisorDecision(
                    component="health",
                    event_type="health",
                    action="error",
                    detail="Adapter check failed",
                    project_slug="test/repo",
                ),
            ]
        )


class TestSupervisorService:
    async def test_get_recent_decisions_empty(self) -> None:
        result = await get_recent_decisions(Path.cwd())
        assert result == []

    async def test_get_recent_decisions_returns_data(self, seed_decisions) -> None:
        result = await get_recent_decisions(Path.cwd())
        assert len(result) == 3
        # Newest first
        assert result[0]["component"] == "health"

    async def test_get_recent_decisions_filter_component(self, seed_decisions) -> None:
        result = await get_recent_decisions(Path.cwd(), component="quota")
        assert len(result) == 1
        assert result[0]["component"] == "quota"

    async def test_get_recent_decisions_filter_event_type(self, seed_decisions) -> None:
        result = await get_recent_decisions(Path.cwd(), event_type="health")
        assert len(result) == 1
        assert result[0]["event_type"] == "health"

    async def test_get_recent_decisions_limit(self, seed_decisions) -> None:
        result = await get_recent_decisions(Path.cwd(), limit=1)
        assert len(result) == 1

    async def test_get_decision_counts(self, seed_decisions) -> None:
        counts = await get_decision_counts(Path.cwd())
        assert counts["progression"] == 1
        assert counts["quota"] == 1
        assert counts["health"] == 1

    async def test_get_recent_decisions_filter_project_slug(self, seed_decisions) -> None:
        result = await get_recent_decisions(Path.cwd(), project_slug="test/repo")
        assert len(result) == 3
        result_other = await get_recent_decisions(Path.cwd(), project_slug="other/repo")
        assert len(result_other) == 0

    async def test_get_decision_counts_filter_project_slug(self, seed_decisions) -> None:
        counts = await get_decision_counts(Path.cwd(), project_slug="test/repo")
        assert counts["progression"] == 1
        counts_other = await get_decision_counts(Path.cwd(), project_slug="other/repo")
        assert counts_other == {}


class TestSupervisorRouter:
    @pytest.fixture
    def app(self):
        from sova.dashboard.app import create_app

        with patch("sova.dashboard.app.recover_stale_runs", new_callable=AsyncMock):
            with patch("sova.dashboard.app.list_projects", return_value={}):
                return create_app(project_dir=Path.cwd())

    async def test_get_status_no_daemon(self, app) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/supervisor/status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["enabled"] is False
            assert data["running"] is False

    async def test_get_decisions_empty(self, app) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/supervisor/decisions")
            assert resp.status_code == 200
            assert resp.json()["decisions"] == []

    async def test_poll_without_daemon_returns_404(self, app) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/supervisor/poll")
            assert resp.status_code == 404

    async def test_supervisor_page_loads(self, app) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/supervisor")
            assert resp.status_code == 200
            assert "Supervisor" in resp.text

    async def test_get_status_with_daemon(self, app) -> None:
        from sova.dashboard.routers.supervisor import set_daemon_registry

        mock_daemon = MagicMock()
        mock_daemon.get_status.return_value = {
            "enabled": True,
            "running": True,
            "poll_interval_seconds": 60,
            "log_retention_days": 14,
            "project_dir": str(Path.cwd()),
        }
        project_dir = Path.cwd().resolve()
        set_daemon_registry({str(project_dir): mock_daemon})

        try:
            with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                    resp = await client.get("/api/supervisor/status")
                    assert resp.status_code == 200
                    data = resp.json()
                    assert data["enabled"] is True
                    assert data["running"] is True
        finally:
            set_daemon_registry({})

    async def test_trigger_poll_with_daemon(self, app) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.routers.supervisor import set_daemon_registry

        mock_daemon = MagicMock()
        mock_daemon.poll_once = AsyncMock(return_value={"progression": {"decisions": 0}})
        project_dir = Path.cwd().resolve()
        set_daemon_registry({str(project_dir): mock_daemon})

        try:
            with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                    resp = await client.post("/api/supervisor/poll")
                    assert resp.status_code == 202
                    assert resp.json()["status"] == "accepted"
        finally:
            set_daemon_registry({})

    async def test_get_counts_empty(self, app) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/supervisor/counts")
            assert resp.status_code == 200
            assert "counts" in resp.json()

    async def test_get_counts_with_data(self, app, seed_decisions) -> None:
        mock_cfg = MagicMock()
        mock_cfg.github_repo = "test/repo"
        with patch("sova.config.loader.load_config", return_value=mock_cfg):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/supervisor/counts")
                assert resp.status_code == 200
                counts = resp.json()["counts"]
                assert counts.get("progression") == 1
                assert counts.get("quota") == 1

    async def test_get_decisions_with_filters(self, app, seed_decisions) -> None:
        mock_cfg = MagicMock()
        mock_cfg.github_repo = "test/repo"
        with patch("sova.config.loader.load_config", return_value=mock_cfg):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/supervisor/decisions?component=progression&limit=10")
                assert resp.status_code == 200
                data = resp.json()["decisions"]
                assert all(d["component"] == "progression" for d in data)

    async def test_start_supervisor_disabled_in_config(self, app) -> None:
        """POST /supervisor/start returns 409 when supervisor.enabled is false."""
        mock_cfg = MagicMock()
        mock_cfg.supervisor.enabled = False
        project_dir = Path.cwd().resolve()
        with (
            patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir),
            patch("sova.config.loader.load_config", return_value=mock_cfg),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/supervisor/start")
                assert resp.status_code == 409

    async def test_start_supervisor_already_running(self, app) -> None:
        """POST /supervisor/start returns started=False when daemon is already running."""
        from sova.dashboard.routers.supervisor import set_daemon_registry

        mock_daemon = MagicMock()
        mock_daemon.running = True
        mock_daemon.get_status.return_value = {
            "enabled": True,
            "running": True,
            "poll_interval_seconds": 120,
            "log_retention_days": 7,
            "project_dir": str(Path.cwd()),
        }
        project_dir = Path.cwd().resolve()
        set_daemon_registry({str(project_dir): mock_daemon})

        try:
            with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                    resp = await client.post("/api/supervisor/start")
                    assert resp.status_code == 200
                    data = resp.json()
                    assert data["started"] is False
                    assert data["reason"] == "already running"
        finally:
            set_daemon_registry({})

    async def test_start_supervisor_success(self, app) -> None:
        """POST /supervisor/start creates and starts a new daemon when config has it enabled."""
        from sova.dashboard.routers.supervisor import set_daemon_registry

        mock_cfg = MagicMock()
        mock_cfg.supervisor.enabled = True
        mock_daemon = MagicMock()
        mock_daemon.running = False
        mock_daemon.get_status.return_value = {
            "enabled": True,
            "running": True,
            "poll_interval_seconds": 120,
            "log_retention_days": 7,
            "project_dir": str(Path.cwd()),
        }
        project_dir = Path.cwd().resolve()
        set_daemon_registry({})

        try:
            with (
                patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=project_dir),
                patch("sova.config.loader.load_config", return_value=mock_cfg),
                patch("sova.db.session.get_session_factory", new_callable=AsyncMock, return_value=MagicMock()),
                patch("sova.supervisor.daemon.SupervisorDaemon", return_value=mock_daemon),
            ):
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                    resp = await client.post("/api/supervisor/start")
                    assert resp.status_code == 200
                    data = resp.json()
                    assert data["started"] is True
                    assert data["running"] is True
                    mock_daemon.start.assert_called_once()
        finally:
            set_daemon_registry({})

    async def test_start_supervisor_no_project_context(self, app) -> None:
        """POST /supervisor/start returns 503 when no project context is set."""
        with patch("sova.dashboard.routers.supervisor.get_project_dir", return_value=None):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/supervisor/start")
                assert resp.status_code == 503

    async def test_get_decisions_load_config_error(self, app) -> None:
        """GET /supervisor/decisions falls back to project_slug=None when load_config raises."""
        with patch("sova.config.loader.load_config", side_effect=Exception("no config")):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/supervisor/decisions")
                assert resp.status_code == 200
                assert resp.json()["decisions"] == []

    async def test_get_counts_load_config_error(self, app) -> None:
        """GET /supervisor/counts falls back to project_slug=None when load_config raises."""
        with patch("sova.config.loader.load_config", side_effect=Exception("no config")):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/supervisor/counts")
                assert resp.status_code == 200
                assert "counts" in resp.json()
