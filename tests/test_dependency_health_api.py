"""Tests for the dependency health dashboard API router."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from sova.dashboard.services import dependency_health_service as svc
from sova.db.session import close_db, init_db


@pytest.fixture(autouse=True)
async def setup_db(monkeypatch):
    monkeypatch.setenv("SOVA_DATABASE_URL", "sqlite+aiosqlite://")
    await init_db(run_migrations=False)
    yield
    await close_db()


@pytest.fixture
def app():
    from sova.dashboard.app import create_app

    with (
        patch("sova.dashboard.app.recover_stale_runs", new_callable=AsyncMock),
        patch("sova.dashboard.app.list_projects", return_value={}),
    ):
        return create_app(project_dir=Path.cwd())


def _fake_snapshot(enabled: bool = True) -> svc.DependencyHealthSnapshot:
    packages = [
        svc.PackageHealth("requests", "pypi", "2.30.0", "2.31.0", "minor", False, False, None, "Apache-2.0", 15),
    ]
    return svc.DependencyHealthSnapshot(
        enabled=enabled,
        generated_at="2026-01-01T00:00:00+00:00",
        ecosystems=["pypi"],
        total=1,
        up_to_date=0,
        major_behind=0,
        deprecated=0,
        vulnerable=0,
        stale=0,
        health_percent=0.0,
        packages=packages,
    )


class TestDependencyHealthRouter:
    async def test_snapshot_endpoint(self, app) -> None:
        with patch.object(svc, "get_snapshot", new=AsyncMock(return_value=_fake_snapshot())):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/dependency-health/snapshot")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is True
        assert data["total"] == 1
        assert "packages" not in data

    async def test_snapshot_disabled(self, app) -> None:
        with patch.object(svc, "get_snapshot", new=AsyncMock(return_value=_fake_snapshot(enabled=False))):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/dependency-health/snapshot")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    async def test_packages_endpoint(self, app) -> None:
        with patch.object(svc, "get_snapshot", new=AsyncMock(return_value=_fake_snapshot())):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/dependency-health/packages")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["packages"]) == 1
        assert data["packages"][0]["name"] == "requests"

    async def test_package_detail_found(self, app) -> None:
        with patch.object(svc, "get_snapshot", new=AsyncMock(return_value=_fake_snapshot())):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/dependency-health/packages/requests")
        assert resp.status_code == 200
        assert resp.json()["name"] == "requests"

    async def test_package_detail_not_found(self, app) -> None:
        with patch.object(svc, "get_snapshot", new=AsyncMock(return_value=_fake_snapshot())):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/dependency-health/packages/nonexistent")
        assert resp.status_code == 404

    async def test_package_detail_scoped_npm_name(self, app) -> None:
        packages = [svc.PackageHealth("@scope/pkg", "npm", "1.0.0", "1.0.0", "up_to_date", False, False, None, None, 0)]
        snapshot = svc.DependencyHealthSnapshot(
            enabled=True,
            generated_at="2026-01-01T00:00:00+00:00",
            ecosystems=["npm"],
            total=1,
            up_to_date=1,
            major_behind=0,
            deprecated=0,
            vulnerable=0,
            stale=0,
            health_percent=100.0,
            packages=packages,
        )
        with patch.object(svc, "get_snapshot", new=AsyncMock(return_value=snapshot)):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/dependency-health/packages/@scope/pkg")
        assert resp.status_code == 200
        assert resp.json()["name"] == "@scope/pkg"

    async def test_refresh_endpoint_forces_rescan(self, app) -> None:
        mock_get_snapshot = AsyncMock(return_value=_fake_snapshot())
        with patch.object(svc, "get_snapshot", new=mock_get_snapshot):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/dependency-health/refresh")
        assert resp.status_code == 200
        assert mock_get_snapshot.call_args.kwargs["force_refresh"] is True

    async def test_snapshot_error_returns_500(self, app) -> None:
        with patch.object(svc, "get_snapshot", new=AsyncMock(side_effect=RuntimeError("boom"))):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/dependency-health/snapshot")
        assert resp.status_code == 500

    async def test_snapshot_no_project_selected_returns_400(self, app) -> None:
        with (
            patch("sova.dashboard.routers.dependency_health.get_project_dir", return_value=None),
            patch("sova.dashboard.routers.dependency_health.get_default_project_dir", return_value=None),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/dependency-health/snapshot")
        assert resp.status_code == 400
        assert resp.json()["detail"] == "No project selected"


class TestDependencyHealthPage:
    async def test_page_route_renders(self, app) -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/dependency-health")
        assert resp.status_code == 200
        assert b"Dependency Health" in resp.content

    async def test_multi_project_page_route_renders(self, tmp_path) -> None:
        from sova.config.registry import register_project, unregister_project
        from sova.dashboard.app import create_app

        multi_app = create_app(multi_project=True)
        project_dir = tmp_path / "test-project"
        project_dir.mkdir()
        slug = register_project(project_dir)
        try:
            async with AsyncClient(transport=ASGITransport(app=multi_app), base_url="http://test") as client:
                resp = await client.get(f"/p/{slug}/dependency-health")
            assert resp.status_code == 200
            assert b"Dependency Health" in resp.content
        finally:
            unregister_project(slug)
