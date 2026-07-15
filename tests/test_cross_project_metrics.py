"""Tests for cross-project metrics sharing (writer, reader, API)."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from sova.monitoring.cross_project import (
    MetricsSnapshotWriter,
    _aggregate_totals,
    _empty_totals,
    _read_snapshot,
    _slugify,
    read_cross_project_metrics,
)


class TestSlugify:
    def test_basic_path(self) -> None:
        assert _slugify(Path("/home/user/project")) == "home_user_project"

    def test_strips_leading_underscores(self) -> None:
        slug = _slugify(Path("/foo/bar"))
        assert not slug.startswith("_")

    def test_different_paths_different_slugs(self) -> None:
        assert _slugify(Path("/a/b")) != _slugify(Path("/a/c"))


class TestReadSnapshot:
    def test_dead_pid_returns_none(self, tmp_path: Path) -> None:
        snapshot = {
            "timestamp": time.time(),
            "pid": 2_000_000_000,  # PID that certainly does not exist
            "project_name": "dead",
            "project_dir": "/tmp/dead",
        }
        path = tmp_path / "dead.json"
        path.write_text(json.dumps(snapshot))

        result = _read_snapshot(path, time.time())
        assert result is None

    def test_valid_snapshot(self, tmp_path: Path) -> None:
        snapshot = {
            "timestamp": time.time(),
            "pid": os.getpid(),
            "project_name": "test-project",
            "project_dir": "/tmp/test",
            "dashboard_port": 8111,
            "system": {"cpu_percent": 45.0},
            "agents": [],
            "agent_slots": {"used": 0, "max": 3},
        }
        path = tmp_path / "test.json"
        path.write_text(json.dumps(snapshot))

        result = _read_snapshot(path, time.time())
        assert result is not None
        assert result["project_name"] == "test-project"
        assert result["age_seconds"] >= 0

    def test_stale_snapshot_returns_none(self, tmp_path: Path) -> None:
        snapshot = {
            "timestamp": time.time() - 60,  # 60s old, well past threshold
            "pid": os.getpid(),
            "project_name": "stale",
        }
        path = tmp_path / "stale.json"
        path.write_text(json.dumps(snapshot))

        result = _read_snapshot(path, time.time())
        assert result is None

    def test_non_dict_json_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "list.json"
        path.write_text(json.dumps([1, 2, 3]))

        result = _read_snapshot(path, time.time())
        assert result is None

    def test_corrupt_json(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{not valid json")

        result = _read_snapshot(path, time.time())
        assert result is None

    def test_missing_timestamp(self, tmp_path: Path) -> None:
        path = tmp_path / "no_ts.json"
        path.write_text(json.dumps({"pid": 1, "project_name": "x"}))

        result = _read_snapshot(path, time.time())
        assert result is None

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        result = _read_snapshot(tmp_path / "ghost.json", time.time())
        assert result is None


class TestReadCrossProjectMetrics:
    def test_no_metrics_dir(self, tmp_path: Path) -> None:
        result = read_cross_project_metrics(
            tmp_path / "proj",
            metrics_dir=tmp_path / "nonexistent",
        )
        assert result["this_project"] is None
        assert result["other_projects"] == []
        assert result["machine_totals"]["project_count"] == 0

    def test_single_project_self(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        project_dir = tmp_path / "myproject"

        slug = _slugify(project_dir)
        snapshot = {
            "timestamp": time.time(),
            "pid": os.getpid(),
            "project_name": "my-project",
            "project_dir": str(project_dir),
            "dashboard_port": 8111,
            "system": {"cpu_percent": 30.0},
            "agents": [{"cpu_percent": 10.0, "memory_rss_bytes": 50_000_000}],
            "agent_slots": {"used": 1, "max": 3},
        }
        (metrics_dir / f"{slug}.json").write_text(json.dumps(snapshot))

        result = read_cross_project_metrics(project_dir, metrics_dir=metrics_dir)
        assert result["this_project"] is not None
        assert result["this_project"]["project_name"] == "my-project"
        assert result["other_projects"] == []
        assert result["machine_totals"]["project_count"] == 1

    def test_multiple_projects(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        project_a = tmp_path / "project-a"
        project_b = tmp_path / "project-b"

        now = time.time()
        for proj, name, agents_used in [(project_a, "A", 2), (project_b, "B", 1)]:
            slug = _slugify(proj)
            snapshot = {
                "timestamp": now,
                "pid": os.getpid(),
                "project_name": name,
                "project_dir": str(proj),
                "dashboard_port": 8111,
                "system": {},
                "agents": [{"cpu_percent": 20.0, "memory_rss_bytes": 100_000_000}] * agents_used,
                "agent_slots": {"used": agents_used, "max": 3},
            }
            (metrics_dir / f"{slug}.json").write_text(json.dumps(snapshot))

        result = read_cross_project_metrics(project_a, metrics_dir=metrics_dir)
        assert result["this_project"] is not None
        assert len(result["other_projects"]) == 1
        assert result["other_projects"][0]["project_name"] == "B"
        assert result["machine_totals"]["project_count"] == 2
        assert result["machine_totals"]["total_agents_used"] == 3

    def test_stale_entries_filtered(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        project_dir = tmp_path / "current"

        # Write a stale snapshot for another project
        stale_slug = _slugify(tmp_path / "stale-project")
        stale_snapshot = {
            "timestamp": time.time() - 120,  # very stale
            "pid": 999999999,
            "project_name": "stale",
        }
        (metrics_dir / f"{stale_slug}.json").write_text(json.dumps(stale_snapshot))

        result = read_cross_project_metrics(project_dir, metrics_dir=metrics_dir)
        assert result["other_projects"] == []


class TestAggregateTotals:
    def test_empty(self) -> None:
        result = _aggregate_totals([])
        assert result == _empty_totals()

    def test_single_project(self) -> None:
        projects = [
            {
                "agent_slots": {"used": 2, "max": 5},
                "agents": [
                    {"cpu_percent": 30.0, "memory_rss_bytes": 100_000_000},
                    {"cpu_percent": 20.0, "memory_rss_bytes": 80_000_000},
                ],
            }
        ]
        result = _aggregate_totals(projects)
        assert result["project_count"] == 1
        assert result["total_agents_used"] == 2
        assert result["total_agents_max"] == 5
        assert result["total_agent_cpu_percent"] == 50.0
        assert result["total_agent_memory_bytes"] == 180_000_000

    def test_float_memory_values(self) -> None:
        projects = [
            {
                "agent_slots": {"used": 1, "max": 3},
                "agents": [{"cpu_percent": 10.0, "memory_rss_bytes": 50_000_000.5}],
            }
        ]
        result = _aggregate_totals(projects)
        assert result["total_agent_memory_bytes"] == 50_000_000
        assert isinstance(result["total_agent_memory_bytes"], int)

    def test_null_agent_metrics(self) -> None:
        projects = [
            {
                "agent_slots": {"used": 1, "max": 3},
                "agents": [{"cpu_percent": None, "memory_rss_bytes": None}],
            }
        ]
        result = _aggregate_totals(projects)
        assert result["total_agent_cpu_percent"] == 0.0
        assert result["total_agent_memory_bytes"] == 0


class TestMetricsSnapshotWriter:
    @pytest.mark.asyncio
    async def test_start_creates_dir(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / "new_metrics"
        writer = MetricsSnapshotWriter(
            project_dir=tmp_path / "proj",
            project_name="test",
            dashboard_port=8111,
            get_metrics_fn=lambda: {"available": True, "system": {}, "agents": [], "agent_slots": {}},
            metrics_dir=metrics_dir,
        )
        writer.start()
        assert metrics_dir.is_dir()
        await writer.stop()

    @pytest.mark.asyncio
    async def test_start_writes_initial_snapshot(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / "metrics"
        writer = MetricsSnapshotWriter(
            project_dir=tmp_path / "proj",
            project_name="test",
            dashboard_port=8111,
            get_metrics_fn=lambda: {"available": True, "system": {"cpu_percent": 5.0}, "agents": [], "agent_slots": {}},
            metrics_dir=metrics_dir,
        )
        writer.start()
        # Snapshot should exist immediately after start(), before the loop fires
        assert writer._snapshot_path.exists()
        data = json.loads(writer._snapshot_path.read_text())
        assert data["project_name"] == "test"
        await writer.stop()

    @pytest.mark.asyncio
    async def test_stop_cleans_up_file(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / "metrics"
        writer = MetricsSnapshotWriter(
            project_dir=tmp_path / "proj",
            project_name="test",
            dashboard_port=8111,
            get_metrics_fn=lambda: {"available": True, "system": {}, "agents": [], "agent_slots": {}},
            metrics_dir=metrics_dir,
        )
        writer.start()
        # Manually write a snapshot to verify cleanup
        writer._write_snapshot()
        assert writer._snapshot_path.exists()
        await writer.stop()
        assert not writer._snapshot_path.exists()

    def test_write_snapshot_atomic(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        writer = MetricsSnapshotWriter(
            project_dir=tmp_path / "proj",
            project_name="test",
            dashboard_port=8111,
            get_metrics_fn=lambda: {
                "available": True,
                "system": {"cpu_percent": 42.0},
                "agents": [],
                "agent_slots": {"used": 0, "max": 3},
            },
            metrics_dir=metrics_dir,
        )
        writer._write_snapshot()
        assert writer._snapshot_path.exists()
        data = json.loads(writer._snapshot_path.read_text())
        assert data["project_name"] == "test"
        assert data["dashboard_port"] == 8111
        assert "timestamp" in data
        assert data["system"]["cpu_percent"] == 42.0

    def test_write_snapshot_unavailable_metrics_skipped(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        writer = MetricsSnapshotWriter(
            project_dir=tmp_path / "proj",
            project_name="test",
            dashboard_port=8111,
            get_metrics_fn=lambda: {"available": False},
            metrics_dir=metrics_dir,
        )
        writer._write_snapshot()
        assert not writer._snapshot_path.exists()

    @pytest.mark.asyncio
    async def test_permission_error_disables_writer(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        original_mkdir = Path.mkdir

        def failing_mkdir(self_path: Path, *args: object, **kwargs: object) -> None:
            if "metrics" in str(self_path):
                raise PermissionError("mocked permission denied")
            original_mkdir(self_path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", failing_mkdir)
        writer = MetricsSnapshotWriter(
            project_dir=tmp_path / "proj",
            project_name="test",
            dashboard_port=8111,
            metrics_dir=tmp_path / "metrics",
        )
        writer.start()
        assert not writer._enabled
        await writer.stop()

    def test_write_snapshot_disabled_returns_early(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        writer = MetricsSnapshotWriter(
            project_dir=tmp_path / "proj",
            project_name="test",
            dashboard_port=8111,
            get_metrics_fn=lambda: {"available": True, "system": {}, "agents": [], "agent_slots": {}},
            metrics_dir=metrics_dir,
        )
        writer._enabled = False
        writer._write_snapshot()
        assert not writer._snapshot_path.exists()

    def test_write_snapshot_no_get_metrics_fn_uses_fallback(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        writer = MetricsSnapshotWriter(
            project_dir=tmp_path / "proj",
            project_name="test",
            dashboard_port=8111,
            get_metrics_fn=None,
            metrics_dir=metrics_dir,
        )
        mock_data = {"available": True, "system": {"cpu_percent": 10.0}, "agents": [], "agent_slots": {}}
        with patch("sova.monitoring.cross_project.get_system_metrics", return_value=mock_data, create=True):
            with patch("sova.dashboard.services.resource_service.get_system_metrics", return_value=mock_data):
                writer._write_snapshot()
        assert writer._snapshot_path.exists()
        data = json.loads(writer._snapshot_path.read_text())
        assert data["system"]["cpu_percent"] == 10.0

    def test_write_snapshot_atomic_write_oserror_disables(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        writer = MetricsSnapshotWriter(
            project_dir=tmp_path / "proj",
            project_name="test",
            dashboard_port=8111,
            get_metrics_fn=lambda: {"available": True, "system": {}, "agents": [], "agent_slots": {}},
            metrics_dir=metrics_dir,
        )
        with patch("sova.monitoring.cross_project.tempfile.mkstemp", side_effect=OSError("disk full")):
            writer._write_snapshot()
        assert not writer._enabled

    def test_write_snapshot_replace_failure_unlink_also_fails(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        writer = MetricsSnapshotWriter(
            project_dir=tmp_path / "proj",
            project_name="test",
            dashboard_port=8111,
            get_metrics_fn=lambda: {"available": True, "system": {}, "agents": [], "agent_slots": {}},
            metrics_dir=metrics_dir,
        )
        # os.replace raises OSError, cleanup os.unlink also raises OSError
        with (
            patch("sova.monitoring.cross_project.os.replace", side_effect=OSError("replace failed")),
            patch("sova.monitoring.cross_project.os.unlink", side_effect=OSError("unlink failed")),
        ):
            writer._write_snapshot()
        assert not writer._enabled

    def test_write_snapshot_replace_failure_cleans_temp(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        writer = MetricsSnapshotWriter(
            project_dir=tmp_path / "proj",
            project_name="test",
            dashboard_port=8111,
            get_metrics_fn=lambda: {"available": True, "system": {}, "agents": [], "agent_slots": {}},
            metrics_dir=metrics_dir,
        )
        with patch("sova.monitoring.cross_project.os.replace", side_effect=OSError("replace failed")):
            writer._write_snapshot()
        # Writer should be disabled after OSError
        assert not writer._enabled
        # Temp file should have been cleaned up
        tmp_files = list(metrics_dir.glob("*.tmp"))
        assert len(tmp_files) == 0

    @pytest.mark.asyncio
    async def test_stop_unlink_oserror_suppressed(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        writer = MetricsSnapshotWriter(
            project_dir=tmp_path / "proj",
            project_name="test",
            dashboard_port=8111,
            get_metrics_fn=lambda: {"available": True, "system": {}, "agents": [], "agent_slots": {}},
            metrics_dir=metrics_dir,
        )
        # Don't start the background task -- just test the unlink error path
        with patch("sova.monitoring.cross_project.Path.unlink", side_effect=OSError("perm denied")):
            await writer.stop()
        # Should not raise

    @pytest.mark.asyncio
    async def test_write_loop_catches_exception(self, tmp_path: Path) -> None:
        """Verify _write_loop catches exceptions from _write_snapshot and continues."""
        metrics_dir = tmp_path / "metrics"
        metrics_dir.mkdir()
        call_count = 0

        def counting_metrics() -> dict:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient error")
            return {"available": True, "system": {}, "agents": [], "agent_slots": {}}

        writer = MetricsSnapshotWriter(
            project_dir=tmp_path / "proj",
            project_name="test",
            dashboard_port=8111,
            get_metrics_fn=counting_metrics,
            metrics_dir=metrics_dir,
        )

        sleep_count = 0
        original_sleep = asyncio.sleep

        async def mock_sleep(delay: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count > 3:
                raise asyncio.CancelledError
            await original_sleep(0)  # yield control without waiting

        with patch("sova.monitoring.cross_project.asyncio.sleep", side_effect=mock_sleep):
            task = asyncio.create_task(writer._write_loop())
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Loop survived the RuntimeError on first call and continued
        assert call_count >= 2


class TestCrossProjectRouter:
    @pytest.fixture(autouse=True)
    async def setup_db(self):
        from sova.db.session import close_db, init_db

        os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
        await init_db(run_migrations=False)
        yield
        await close_db()
        os.environ.pop("SOVA_DATABASE_URL", None)

    @pytest.fixture
    async def client(self):
        from sova.dashboard.app import create_app

        app = create_app(multi_project=False)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    @pytest.mark.asyncio
    async def test_cross_project_endpoint(self, client: AsyncClient) -> None:
        resp = await client.get("/api/resources/cross-project")
        assert resp.status_code == 200
        data = resp.json()
        assert "other_projects" in data
        assert "machine_totals" in data
        assert isinstance(data["other_projects"], list)

    @pytest.mark.asyncio
    async def test_cross_project_endpoint_500_on_error(self, client: AsyncClient) -> None:
        target = "sova.dashboard.routers.resources.resource_service.get_cross_project_metrics"
        with patch(target, side_effect=RuntimeError("oops")):
            resp = await client.get("/api/resources/cross-project")
        assert resp.status_code == 500
        assert "cross-project" in resp.json()["detail"]
