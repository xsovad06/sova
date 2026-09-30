"""Tests for the reliability API router and page (issue #979)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from sova.db.models import TaskRun
from sova.db.session import close_db, get_session, init_db

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
async def setup_db():
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


async def _seed_runs() -> None:
    async with await get_session() as session:
        for _ in range(6):
            session.add(
                TaskRun(
                    role="developer", status="done", started_at=NOW - timedelta(days=1), total_cost_usd=Decimal("1")
                )
            )
        for _ in range(4):
            session.add(
                TaskRun(
                    role="developer",
                    status="failed",
                    started_at=NOW - timedelta(days=1),
                    total_cost_usd=Decimal("0.5"),
                    error_message="step_hard_timeout",
                )
            )
        await session.commit()


class TestReliabilityAPI:
    @pytest.fixture
    async def client(self):
        from sova.dashboard.app import create_app

        app = create_app(multi_project=False)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    async def test_success_by_role_endpoint(self, client: AsyncClient) -> None:
        await _seed_runs()
        resp = await client.get("/api/reliability/success-by-role")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["role"] == "developer"
        assert data[0]["total_runs"] == 10

    async def test_success_by_role_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/reliability/success-by-role")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_failure_taxonomy_endpoint(self, client: AsyncClient) -> None:
        await _seed_runs()
        resp = await client.get("/api/reliability/failure-taxonomy")
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["cause"] == "step_timeout"
        assert data[0]["count"] == 4

    async def test_spend_by_outcome_endpoint(self, client: AsyncClient) -> None:
        await _seed_runs()
        resp = await client.get("/api/reliability/spend-by-outcome")
        assert resp.status_code == 200
        data = resp.json()
        assert data["completing"]["run_count"] == 6
        assert data["non_completing"]["run_count"] == 4

    async def test_days_validation(self, client: AsyncClient) -> None:
        resp = await client.get("/api/reliability/success-by-role?days=0")
        assert resp.status_code == 422

    async def test_reliability_page(self, client: AsyncClient) -> None:
        resp = await client.get("/reliability")
        assert resp.status_code == 200
        assert b"Reliability" in resp.content


class TestReliabilityProjectScoped:
    async def test_project_scoped_page_and_api(self, tmp_path) -> None:
        from sova.config.registry import register_project, unregister_project
        from sova.dashboard.app import create_app

        app = create_app(multi_project=True)
        transport = ASGITransport(app=app)
        project_dir = tmp_path / "test-project"
        project_dir.mkdir()
        slug = register_project(project_dir)
        try:
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                page_resp = await client.get(f"/p/{slug}/reliability")
                assert page_resp.status_code == 200
                assert b"Reliability" in page_resp.content

                api_resp = await client.get(f"/p/{slug}/api/reliability/success-by-role")
                assert api_resp.status_code == 200
                assert api_resp.json() == []
        finally:
            unregister_project(slug)
