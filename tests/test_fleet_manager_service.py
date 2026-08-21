"""Tests for the fleet manager service and registry extensions."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from sova.config.registry import (
    ProjectEntry,
    get_project_entries,
    list_projects,
    register_project,
    unregister_project,
    update_fleet_priority,
)
from sova.dashboard.services.agent_pool import (
    ProjectAgents,
    list_all_pools,
)
from sova.dashboard.services.fleet_manager_service import (
    FleetManagerService,
    FleetStatus,
)
from sova.db.models import Base


@pytest.fixture(autouse=True)
def _clear_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


class TestRegistrySchemaTransparency:
    """Verify old-format and new-format coexistence."""

    def test_load_old_format(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Old format {slug: "/path"} loads correctly via list_projects()."""
        registry = tmp_path / "projects.json"
        registry.write_text(json.dumps({"myapp": "/home/user/myapp"}))
        monkeypatch.setattr("sova.config.registry._REGISTRY_FILE", registry)
        monkeypatch.setattr("sova.config.registry._REGISTRY_DIR", tmp_path)

        projects = list_projects()
        assert projects == {"myapp": "/home/user/myapp"}

    def test_load_new_format(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """New format {slug: {path, fleet_priority}} loads correctly."""
        registry = tmp_path / "projects.json"
        data = {"myapp": {"path": "/home/user/myapp", "fleet_priority": 5}}
        registry.write_text(json.dumps(data))
        monkeypatch.setattr("sova.config.registry._REGISTRY_FILE", registry)
        monkeypatch.setattr("sova.config.registry._REGISTRY_DIR", tmp_path)

        projects = list_projects()
        assert projects == {"myapp": "/home/user/myapp"}

    def test_get_entries_new_format(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_project_entries returns full ProjectEntry objects."""
        registry = tmp_path / "projects.json"
        data = {"myapp": {"path": "/home/user/myapp", "fleet_priority": 3}}
        registry.write_text(json.dumps(data))
        monkeypatch.setattr("sova.config.registry._REGISTRY_FILE", registry)
        monkeypatch.setattr("sova.config.registry._REGISTRY_DIR", tmp_path)

        entries = get_project_entries()
        assert "myapp" in entries
        assert entries["myapp"].path == "/home/user/myapp"
        assert entries["myapp"].fleet_priority == 3

    def test_get_entries_old_format_default_priority(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Old-format entries get fleet_priority=0 by default."""
        registry = tmp_path / "projects.json"
        registry.write_text(json.dumps({"myapp": "/home/user/myapp"}))
        monkeypatch.setattr("sova.config.registry._REGISTRY_FILE", registry)
        monkeypatch.setattr("sova.config.registry._REGISTRY_DIR", tmp_path)

        entries = get_project_entries()
        assert entries["myapp"].fleet_priority == 0

    def test_register_preserves_fleet_priority_on_reregister(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Re-registering the same project preserves fleet_priority."""
        registry = tmp_path / "projects.json"
        monkeypatch.setattr("sova.config.registry._REGISTRY_FILE", registry)
        monkeypatch.setattr("sova.config.registry._REGISTRY_DIR", tmp_path)

        project_dir = tmp_path / "myproject"
        project_dir.mkdir()
        slug = register_project(project_dir, "myproject")
        assert slug == "myproject"

        # Set fleet_priority to 5
        assert update_fleet_priority("myproject", 5) is True
        assert get_project_entries()["myproject"].fleet_priority == 5

        # Re-register with same path: priority must survive
        slug2 = register_project(project_dir, "myproject")
        assert slug2 == "myproject"
        assert get_project_entries()["myproject"].fleet_priority == 5

    def test_register_writes_new_format(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """register_project always writes new format."""
        registry = tmp_path / "projects.json"
        monkeypatch.setattr("sova.config.registry._REGISTRY_FILE", registry)
        monkeypatch.setattr("sova.config.registry._REGISTRY_DIR", tmp_path)

        project_dir = tmp_path / "myproject"
        project_dir.mkdir()
        slug = register_project(project_dir, "myproject")
        assert slug == "myproject"

        raw = json.loads(registry.read_text())
        assert isinstance(raw["myproject"], dict)
        assert raw["myproject"]["path"] == str(project_dir.resolve())
        assert raw["myproject"]["fleet_priority"] == 0

    def test_unregister_removes_entry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """unregister_project removes the entry."""
        registry = tmp_path / "projects.json"
        data = {"myapp": {"path": str(tmp_path), "fleet_priority": 0}}
        registry.write_text(json.dumps(data))
        monkeypatch.setattr("sova.config.registry._REGISTRY_FILE", registry)
        monkeypatch.setattr("sova.config.registry._REGISTRY_DIR", tmp_path)

        assert unregister_project("myapp") is True
        assert "myapp" not in list_projects()


class TestUpdateFleetPriority:
    def test_update_existing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        registry = tmp_path / "projects.json"
        data = {"myapp": {"path": "/home/user/myapp", "fleet_priority": 0}}
        registry.write_text(json.dumps(data))
        monkeypatch.setattr("sova.config.registry._REGISTRY_FILE", registry)
        monkeypatch.setattr("sova.config.registry._REGISTRY_DIR", tmp_path)

        assert update_fleet_priority("myapp", 5) is True
        entries = get_project_entries()
        assert entries["myapp"].fleet_priority == 5

    def test_update_nonexistent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        registry = tmp_path / "projects.json"
        registry.write_text("{}")
        monkeypatch.setattr("sova.config.registry._REGISTRY_FILE", registry)
        monkeypatch.setattr("sova.config.registry._REGISTRY_DIR", tmp_path)

        assert update_fleet_priority("nope", 5) is False


# ---------------------------------------------------------------------------
# Agent pool tests
# ---------------------------------------------------------------------------


class TestAgentPoolPublicAPI:
    def test_list_all_pools_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """list_all_pools returns empty dict when no projects initialized."""
        import sova.dashboard.services.agent_pool as pool_mod

        monkeypatch.setattr(pool_mod, "_projects", {})
        assert list_all_pools() == {}

    def test_list_all_pools_returns_copy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """list_all_pools returns a copy, not the internal dict."""
        import sova.dashboard.services.agent_pool as pool_mod

        pa = ProjectAgents()
        monkeypatch.setattr(pool_mod, "_projects", {"test": pa})
        result = list_all_pools()
        assert result == {"test": pa}
        assert result is not pool_mod._projects


# ---------------------------------------------------------------------------
# Fleet Manager Service tests
# ---------------------------------------------------------------------------


async def _create_project_db(db_path: Path) -> None:
    """Create a SOVA SQLite database with the full schema at db_path."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()


class TestFleetManagerService:
    @pytest.mark.asyncio
    async def test_empty_registry(self) -> None:
        """Empty registry returns empty fleet status."""
        service = FleetManagerService()
        with patch("sova.dashboard.services.fleet_manager_service.get_project_entries", return_value={}):
            status = await service.get_fleet_status()
        assert isinstance(status, FleetStatus)
        assert status.projects == []
        assert status.total_active_agents == 0

    @pytest.mark.asyncio
    async def test_project_without_pool(self, tmp_path: Path) -> None:
        """Project in registry but not in pool uses TOML max_concurrent."""
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        db_path = project_dir / ".claude" / "sova.db"
        await _create_project_db(db_path)

        entries = {"proj": ProjectEntry(path=str(project_dir))}
        service = FleetManagerService()

        with (
            patch("sova.dashboard.services.fleet_manager_service.get_project_entries", return_value=entries),
            patch("sova.dashboard.services.fleet_manager_service.list_all_pools", return_value={}),
            patch("sova.dashboard.services.fleet_manager_service.read_max_parallel", return_value=5),
        ):
            status = await service.get_fleet_status()

        assert len(status.projects) == 1
        assert status.projects[0].slug == "proj"
        assert status.projects[0].max_concurrent == 5
        assert status.projects[0].active_agents == 0

    @pytest.mark.asyncio
    async def test_project_with_pool(self, tmp_path: Path) -> None:
        """Project in pool uses pool's agent count and max_concurrent."""
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        db_path = project_dir / ".claude" / "sova.db"
        await _create_project_db(db_path)

        entries = {"proj": ProjectEntry(path=str(project_dir))}
        pa = ProjectAgents(max_concurrent=3)

        service = FleetManagerService()

        with (
            patch("sova.dashboard.services.fleet_manager_service.get_project_entries", return_value=entries),
            patch("sova.dashboard.services.fleet_manager_service.list_all_pools", return_value={"proj": pa}),
        ):
            status = await service.get_fleet_status()

        assert status.projects[0].max_concurrent == 3
        assert status.projects[0].active_agents == 0
        assert status.total_max_slots == 3

    @pytest.mark.asyncio
    async def test_db_missing(self) -> None:
        """Project with no DB returns zero counts and error flag."""
        entries = {"proj": ProjectEntry(path="/nonexistent/path")}
        service = FleetManagerService()

        with (
            patch("sova.dashboard.services.fleet_manager_service.get_project_entries", return_value=entries),
            patch("sova.dashboard.services.fleet_manager_service.list_all_pools", return_value={}),
            patch("sova.dashboard.services.fleet_manager_service.read_max_parallel", return_value=2),
        ):
            status = await service.get_fleet_status()

        assert len(status.projects) == 1
        assert status.projects[0].queued_tasks == 0
        assert status.projects[0].error == "DB not found"

    @pytest.mark.asyncio
    async def test_db_timeout(self, tmp_path: Path) -> None:
        """DB timeout sets error flag on the project."""
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        db_path = project_dir / ".claude" / "sova.db"
        await _create_project_db(db_path)

        entries = {"proj": ProjectEntry(path=str(project_dir))}
        service = FleetManagerService()

        async def _slow_query(*args, **kwargs):
            await asyncio.sleep(100)

        with (
            patch("sova.dashboard.services.fleet_manager_service.get_project_entries", return_value=entries),
            patch("sova.dashboard.services.fleet_manager_service.list_all_pools", return_value={}),
            patch("sova.dashboard.services.fleet_manager_service.read_max_parallel", return_value=2),
            patch.object(service, "_query_project_db", side_effect=_slow_query),
            patch("sova.dashboard.services.fleet_manager_service._QUERY_TIMEOUT", 0.01),
        ):
            status = await service.get_fleet_status()

        assert status.projects[0].error == "DB query timed out"

    @pytest.mark.asyncio
    async def test_set_max_concurrent_unknown_project(self) -> None:
        """set_max_concurrent returns 'not_found' for unknown project."""
        service = FleetManagerService()
        with patch("sova.dashboard.services.fleet_manager_service.list_projects", return_value={}):
            result = await service.set_max_concurrent("nope", 5)
        assert result == "not_found"

    @pytest.mark.asyncio
    async def test_set_max_concurrent_updates_pool(self, tmp_path: Path) -> None:
        """set_max_concurrent updates in-memory pool after successful DB write."""
        pa = ProjectAgents(max_concurrent=2)
        service = FleetManagerService()

        with (
            patch("sova.dashboard.services.fleet_manager_service.list_projects", return_value={"proj": str(tmp_path)}),
            patch("sova.dashboard.services.fleet_manager_service.list_all_pools", return_value={"proj": pa}),
            patch.object(service, "_write_max_concurrent_to_db", new_callable=AsyncMock, return_value=True),
        ):
            result = await service.set_max_concurrent("proj", 5)

        assert result is None
        assert pa.max_concurrent == 5

    @pytest.mark.asyncio
    async def test_set_max_concurrent_db_failure_no_memory_update(self, tmp_path: Path) -> None:
        """set_max_concurrent does NOT update in-memory pool when DB write fails."""
        pa = ProjectAgents(max_concurrent=2)
        service = FleetManagerService()

        with (
            patch("sova.dashboard.services.fleet_manager_service.list_projects", return_value={"proj": str(tmp_path)}),
            patch("sova.dashboard.services.fleet_manager_service.list_all_pools", return_value={"proj": pa}),
            patch.object(service, "_write_max_concurrent_to_db", new_callable=AsyncMock, return_value=False),
        ):
            result = await service.set_max_concurrent("proj", 5)

        assert result == "db_error"
        assert pa.max_concurrent == 2

    @pytest.mark.asyncio
    async def test_coderabbit_dedup_across_projects(self, tmp_path: Path) -> None:
        """Global CodeRabbit count deduplicates by review_id across projects."""
        # Create two project DBs with overlapping CodeRabbit events
        for name in ("proj-a", "proj-b"):
            project_dir = tmp_path / name
            project_dir.mkdir()
            db_path = project_dir / ".claude" / "sova.db"
            await _create_project_db(db_path)

            import aiosqlite

            async with aiosqlite.connect(str(db_path)) as db:
                # Both projects share review_id "shared-1"; each has one unique
                for i, rid in enumerate((f"unique-{name}", "shared-1")):
                    await db.execute(
                        "INSERT INTO coderabbit_events (pr_number, review_id, event_type, recorded_at, project_slug)"
                        " VALUES (?, ?, 'review', datetime('now'), ?)",
                        (100 + i, rid, name),
                    )
                await db.commit()

        entries = {
            "proj-a": ProjectEntry(path=str(tmp_path / "proj-a")),
            "proj-b": ProjectEntry(path=str(tmp_path / "proj-b")),
        }
        service = FleetManagerService()

        with (
            patch("sova.dashboard.services.fleet_manager_service.get_project_entries", return_value=entries),
            patch("sova.dashboard.services.fleet_manager_service.list_all_pools", return_value={}),
            patch("sova.dashboard.services.fleet_manager_service.read_max_parallel", return_value=2),
        ):
            status = await service.get_fleet_status()

        # 3 unique review_ids: unique-proj-a, unique-proj-b, shared-1
        assert status.global_coderabbit_used == 3
        # Per-project counts are local (not deduplicated)
        per_project = {p.slug: p.coderabbit_reviews_in_window for p in status.projects}
        assert per_project["proj-a"] == 2
        assert per_project["proj-b"] == 2

    @pytest.mark.asyncio
    async def test_sorted_by_priority(self) -> None:
        """Projects are sorted by fleet_priority then slug."""
        entries = {
            "beta": ProjectEntry(path="/b", fleet_priority=2),
            "alpha": ProjectEntry(path="/a", fleet_priority=1),
            "gamma": ProjectEntry(path="/g", fleet_priority=1),
        }
        service = FleetManagerService()

        with (
            patch("sova.dashboard.services.fleet_manager_service.get_project_entries", return_value=entries),
            patch("sova.dashboard.services.fleet_manager_service.list_all_pools", return_value={}),
            patch("sova.dashboard.services.fleet_manager_service.read_max_parallel", return_value=2),
        ):
            status = await service.get_fleet_status()

        slugs = [p.slug for p in status.projects]
        assert slugs == ["alpha", "gamma", "beta"]


# ---------------------------------------------------------------------------
# Router tests (via ASGI)
# ---------------------------------------------------------------------------


@pytest.fixture
def _fleet_app():
    """Create a minimal FastAPI app with the fleet_manager router."""
    from fastapi import FastAPI

    import sova.dashboard.routers.fleet_manager as fm_mod

    app = FastAPI()
    app.include_router(fm_mod.router, prefix="/api")
    return app, fm_mod


@pytest.mark.asyncio
async def test_fleet_manager_status_endpoint(_fleet_app) -> None:
    """GET /api/fleet-manager/status returns expected shape."""
    from httpx import ASGITransport, AsyncClient

    app, fm_mod = _fleet_app

    mock_status = FleetStatus(
        projects=[],
        total_active_agents=0,
        total_max_slots=0,
        total_queued=0,
        global_coderabbit_used=0,
        global_coderabbit_limit=4,
        global_coderabbit_can_create=True,
    )

    original = fm_mod._service
    try:
        mock_svc = AsyncMock()
        mock_svc.get_fleet_status.return_value = mock_status
        fm_mod._service = mock_svc
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/fleet-manager/status")
    finally:
        fm_mod._service = original

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_active_agents"] == 0
    assert data["global_coderabbit_limit"] == 4
    assert isinstance(data["projects"], list)


@pytest.mark.asyncio
async def test_fleet_manager_slots_endpoint(_fleet_app) -> None:
    """PATCH /api/fleet-manager/projects/{slug}/slots updates slots."""
    from httpx import ASGITransport, AsyncClient

    app, fm_mod = _fleet_app

    original = fm_mod._service
    try:
        mock_svc = AsyncMock()
        mock_svc.set_max_concurrent = AsyncMock(return_value=None)
        fm_mod._service = mock_svc
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.patch(
                "/api/fleet-manager/projects/myapp/slots",
                json={"value": 5},
            )
    finally:
        fm_mod._service = original

    assert resp.status_code == 200
    assert resp.json() == {"slug": "myapp", "max_concurrent": 5}


@pytest.mark.asyncio
async def test_fleet_manager_slots_not_found(_fleet_app) -> None:
    """PATCH to unknown project returns 404."""
    from httpx import ASGITransport, AsyncClient

    app, fm_mod = _fleet_app

    original = fm_mod._service
    try:
        mock_svc = AsyncMock()
        mock_svc.set_max_concurrent = AsyncMock(return_value="not_found")
        fm_mod._service = mock_svc
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.patch(
                "/api/fleet-manager/projects/nonexistent/slots",
                json={"value": 3},
            )
    finally:
        fm_mod._service = original

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_fleet_manager_slots_db_error(_fleet_app) -> None:
    """PATCH returns 503 when DB write fails (not 404)."""
    from httpx import ASGITransport, AsyncClient

    app, fm_mod = _fleet_app

    original = fm_mod._service
    try:
        mock_svc = AsyncMock()
        mock_svc.set_max_concurrent = AsyncMock(return_value="db_error")
        fm_mod._service = mock_svc
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.patch(
                "/api/fleet-manager/projects/myapp/slots",
                json={"value": 3},
            )
    finally:
        fm_mod._service = original

    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Coverage: uncovered paths in FleetManagerService
# ---------------------------------------------------------------------------


class TestFleetManagerServiceCoverage:
    """Additional tests for uncovered error paths and edge cases."""

    @pytest.mark.asyncio
    async def test_get_project_status_exception_logged(self) -> None:
        """When _get_project_status raises, it is logged and skipped."""
        entries = {"bad": ProjectEntry(path="/nonexistent")}
        service = FleetManagerService()

        with (
            patch("sova.dashboard.services.fleet_manager_service.get_project_entries", return_value=entries),
            patch("sova.dashboard.services.fleet_manager_service.list_all_pools", return_value={}),
            patch.object(service, "_get_project_status", side_effect=RuntimeError("boom")),
        ):
            status = await service.get_fleet_status()

        assert status.projects == []
        assert status.total_active_agents == 0

    @pytest.mark.asyncio
    async def test_db_generic_error(self, tmp_path: Path) -> None:
        """Generic DB error sets error message on the project."""
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        db_path = project_dir / ".claude" / "sova.db"
        await _create_project_db(db_path)

        entries = {"proj": ProjectEntry(path=str(project_dir))}
        service = FleetManagerService()

        with (
            patch("sova.dashboard.services.fleet_manager_service.get_project_entries", return_value=entries),
            patch("sova.dashboard.services.fleet_manager_service.list_all_pools", return_value={}),
            patch("sova.dashboard.services.fleet_manager_service.read_max_parallel", return_value=2),
            patch.object(service, "_query_project_db", side_effect=OSError("disk error")),
        ):
            status = await service.get_fleet_status()

        assert len(status.projects) == 1
        assert status.projects[0].error == "DB error: disk error"

    @pytest.mark.asyncio
    async def test_query_project_db_batch_fallback(self, tmp_path: Path) -> None:
        """When batch query fails (no coderabbit_events table), falls back to queue-only."""
        db_path = tmp_path / "test.db"
        # Create DB with only task_runs table (no coderabbit_events)
        import aiosqlite

        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, status TEXT)")
            await db.execute("INSERT INTO task_runs (status) VALUES ('running')")
            await db.execute("INSERT INTO task_runs (status) VALUES ('done')")
            await db.commit()

        service = FleetManagerService()
        result = await service._query_project_db(db_path)
        assert result["queued"] == 1  # only 'running' is non-terminal
        assert result["cr_reviews"] == 0

    @pytest.mark.asyncio
    async def test_query_project_db_total_failure(self, tmp_path: Path) -> None:
        """When both batch and fallback queries fail, returns zeros."""
        db_path = tmp_path / "empty.db"
        # Create empty DB with no tables
        import aiosqlite

        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute("SELECT 1")  # just create the file

        service = FleetManagerService()
        result = await service._query_project_db(db_path)
        assert result["queued"] == 0
        assert result["cr_reviews"] == 0

    def test_read_coderabbit_limit_exception(self) -> None:
        """_read_coderabbit_limit returns 0 on config load failure."""
        with patch("sova.config.loader.load_config", side_effect=RuntimeError("config broken")):
            result = FleetManagerService._read_coderabbit_limit(Path("/nonexistent"))
        assert result == 0

    @pytest.mark.asyncio
    async def test_write_max_concurrent_to_db_writes(self, tmp_path: Path) -> None:
        """_write_max_concurrent_to_db calls save_setting with correct key/value and returns True."""
        mock_save = AsyncMock()
        mock_commit = AsyncMock()
        mock_session = AsyncMock()
        mock_session.commit = mock_commit
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_get_session = AsyncMock(return_value=mock_session)

        service = FleetManagerService()
        with (
            patch("sova.config.db_loader.save_setting", mock_save),
            patch("sova.db.session.get_session", mock_get_session),
        ):
            result = await service._write_max_concurrent_to_db(tmp_path, 8)

        assert result is True
        mock_save.assert_awaited_once_with(mock_session, "max_parallel_agents", 8)
        mock_commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_write_max_concurrent_to_db_error_returns_false(self) -> None:
        """_write_max_concurrent_to_db returns False on DB errors."""
        mock_save = AsyncMock(side_effect=Exception("DB gone"))
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_get_session = AsyncMock(return_value=mock_session)

        service = FleetManagerService()
        with (
            patch("sova.config.db_loader.save_setting", mock_save),
            patch("sova.db.session.get_session", mock_get_session),
        ):
            result = await service._write_max_concurrent_to_db(Path("/dummy"), 5)
        assert result is False
