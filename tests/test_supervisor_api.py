"""Tests for the supervisor dashboard API router and service."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

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
