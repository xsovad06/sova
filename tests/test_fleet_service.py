"""Tests for the fleet insights service."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sova.config.models import FleetConfig
from sova.dashboard.services.fleet_service import (
    FleetInsights,
    FleetService,
)
from sova.db.models import (
    Base,
    FailureRecord,
    StepExecution,
    TaskRun,
)


@pytest.fixture(autouse=True)
def _clear_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent session.py from picking up a global DB URL."""
    monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)


# ---------------------------------------------------------------------------
# Helpers
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


async def _insert_rows(db_path: Path, rows: list) -> None:
    """Insert ORM model instances into the given database."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        for row in rows:
            session.add(row)
        await session.commit()
    await engine.dispose()


def _make_registry(tmp_path: Path, projects: dict[str, Path]) -> None:
    """Write a projects.json registry file."""
    import json

    registry_dir = tmp_path / ".config" / "sova"
    registry_dir.mkdir(parents=True, exist_ok=True)
    data = {slug: str(path) for slug, path in projects.items()}
    (registry_dir / "projects.json").write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# Config model tests
# ---------------------------------------------------------------------------


class TestFleetConfig:
    def test_defaults(self) -> None:
        cfg = FleetConfig()
        assert cfg.cache_ttl_seconds == 300
        assert cfg.query_timeout_seconds == 10

    def test_custom_values(self) -> None:
        cfg = FleetConfig(cache_ttl_seconds=60, query_timeout_seconds=5)
        assert cfg.cache_ttl_seconds == 60
        assert cfg.query_timeout_seconds == 5

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOVA_FLEET_CACHE_TTL_SECONDS", "120")
        cfg = FleetConfig()
        assert cfg.cache_ttl_seconds == 120

    def test_in_project_config(self) -> None:
        from sova.config.models import ProjectConfig

        pc = ProjectConfig()
        assert hasattr(pc, "fleet")
        assert isinstance(pc.fleet, FleetConfig)

    def test_toml_loading(self, tmp_path: Path) -> None:
        from sova.config.loader import load_config

        toml_content = "[fleet]\ncache_ttl_seconds = 42\nquery_timeout_seconds = 7\n"
        (tmp_path / "sova.toml").write_text(toml_content)
        cfg = load_config(tmp_path)
        assert cfg.fleet.cache_ttl_seconds == 42
        assert cfg.fleet.query_timeout_seconds == 7


# ---------------------------------------------------------------------------
# Settings meta tests
# ---------------------------------------------------------------------------


class TestSettingsMeta:
    def test_fleet_group_registered(self) -> None:
        from sova.dashboard.settings_meta import GROUP_ORDER, GROUPS

        assert "fleet" in GROUPS
        assert "fleet" in GROUP_ORDER

    def test_fleet_settings_in_registry(self) -> None:
        from sova.dashboard.settings_meta import get_meta

        meta_ttl = get_meta("fleet.cache_ttl_seconds")
        assert meta_ttl is not None
        assert meta_ttl.value_type == "number"

        meta_timeout = get_meta("fleet.query_timeout_seconds")
        assert meta_timeout is not None
        assert meta_timeout.value_type == "number"


# ---------------------------------------------------------------------------
# FleetService tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestFleetServiceEmptyRegistry:
    async def test_empty_registry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No registered projects returns empty insights."""
        monkeypatch.setattr("sova.dashboard.services.fleet_service.list_projects", dict)
        svc = FleetService(FleetConfig(cache_ttl_seconds=1))
        result = await svc.get_insights()

        assert isinstance(result, FleetInsights)
        assert result.projects_scanned == []
        assert result.projects_skipped == []
        assert result.total_runs == 0
        assert result.total_cost_usd == Decimal(0)
        assert result.success_rate == 0.0
        assert result.retry_success_rate == 0.0
        assert result.step_failure_stats == []
        assert result.failure_clusters == []
        assert result.cost_by_project == []


@pytest.mark.asyncio
class TestFleetServiceMissingDB:
    async def test_db_missing_skips_project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Project dir exists but no sova.db: project added to skipped list."""
        project_dir = tmp_path / "proj-a"
        project_dir.mkdir()

        monkeypatch.setattr(
            "sova.dashboard.services.fleet_service.list_projects",
            lambda: {"proj-a": str(project_dir)},
        )
        svc = FleetService(FleetConfig(cache_ttl_seconds=1))
        result = await svc.get_insights()

        assert result.projects_scanned == []
        assert result.projects_skipped == ["proj-a"]


@pytest.mark.asyncio
class TestFleetServiceSingleProject:
    async def test_single_project_basic(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Single project with runs, steps, failures, costs."""
        project_dir = tmp_path / "alpha"
        project_dir.mkdir()
        db_path = project_dir / ".claude" / "sova.db"
        await _create_project_db(db_path)

        # Insert test data
        runs = [
            TaskRun(issue_number="1", role="developer", status="done", total_cost_usd=Decimal("1.50")),
            TaskRun(issue_number="2", role="developer", status="done", total_cost_usd=Decimal("0.75")),
            TaskRun(issue_number="3", role="developer", status="failed", total_cost_usd=Decimal("0.25")),
        ]
        await _insert_rows(db_path, runs)

        steps = [
            StepExecution(task_run_id=1, step_name="develop", status="done"),
            StepExecution(task_run_id=1, step_name="push", status="done"),
            StepExecution(task_run_id=3, step_name="develop", status="failed"),
        ]
        await _insert_rows(db_path, steps)

        failures = [
            FailureRecord(task_run_id=3, step_name="develop", failure_type="error", message="test suite failed"),
        ]
        await _insert_rows(db_path, failures)

        monkeypatch.setattr(
            "sova.dashboard.services.fleet_service.list_projects",
            lambda: {"alpha": str(project_dir)},
        )
        svc = FleetService(FleetConfig(cache_ttl_seconds=1))
        result = await svc.get_insights()

        assert result.projects_scanned == ["alpha"]
        assert result.projects_skipped == []
        assert result.total_runs == 3
        assert result.total_cost_usd == Decimal("2.50")
        assert result.success_rate == pytest.approx(2 / 3, abs=0.01)

        # Step failure stats
        develop_stat = next((s for s in result.step_failure_stats if s.step_name == "develop"), None)
        assert develop_stat is not None
        assert develop_stat.total_count == 2
        assert develop_stat.failure_count == 1
        assert develop_stat.failure_rate == pytest.approx(0.5)

        push_stat = next((s for s in result.step_failure_stats if s.step_name == "push"), None)
        assert push_stat is not None
        assert push_stat.failure_count == 0

        # Failure clusters
        assert len(result.failure_clusters) == 1
        assert result.failure_clusters[0].pattern == "test suite failed"
        assert result.failure_clusters[0].count == 1

        # Cost by project
        assert len(result.cost_by_project) == 1
        assert result.cost_by_project[0].slug == "alpha"
        assert result.cost_by_project[0].run_count == 3
        assert result.cost_by_project[0].avg_cost_per_run == round(Decimal("2.50") / 3, 6)


@pytest.mark.asyncio
class TestFleetServiceMultiProject:
    async def test_merges_across_projects(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Results from multiple projects are merged correctly."""
        proj_a = tmp_path / "proj-a"
        proj_a.mkdir()
        db_a = proj_a / ".claude" / "sova.db"
        await _create_project_db(db_a)
        await _insert_rows(
            db_a,
            [
                TaskRun(issue_number="1", role="developer", status="done", total_cost_usd=Decimal("1.00")),
            ],
        )

        proj_b = tmp_path / "proj-b"
        proj_b.mkdir()
        db_b = proj_b / ".claude" / "sova.db"
        await _create_project_db(db_b)
        await _insert_rows(
            db_b,
            [
                TaskRun(issue_number="1", role="developer", status="done", total_cost_usd=Decimal("2.00")),
                TaskRun(issue_number="2", role="developer", status="failed", total_cost_usd=Decimal("0.50")),
            ],
        )

        monkeypatch.setattr(
            "sova.dashboard.services.fleet_service.list_projects",
            lambda: {"proj-a": str(proj_a), "proj-b": str(proj_b)},
        )
        svc = FleetService(FleetConfig(cache_ttl_seconds=1))
        result = await svc.get_insights()

        assert sorted(result.projects_scanned) == ["proj-a", "proj-b"]
        assert result.total_runs == 3
        assert result.total_cost_usd == Decimal("3.50")
        assert result.success_rate == pytest.approx(2 / 3, abs=0.01)
        assert len(result.cost_by_project) == 2


@pytest.mark.asyncio
class TestFleetServiceIncompatibleDB:
    async def test_missing_tables_skips(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """DB exists but has no SOVA tables: added to skipped."""
        project_dir = tmp_path / "broken"
        project_dir.mkdir()
        db_path = project_dir / ".claude" / "sova.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)

        # Create an empty database with no tables
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE dummy (id INTEGER PRIMARY KEY)"))
        await engine.dispose()

        monkeypatch.setattr(
            "sova.dashboard.services.fleet_service.list_projects",
            lambda: {"broken": str(project_dir)},
        )
        svc = FleetService(FleetConfig(cache_ttl_seconds=1))
        result = await svc.get_insights()

        # DB exists but lacks SOVA tables: classified as skipped, not scanned
        assert result.projects_scanned == []
        assert result.projects_skipped == ["broken"]
        assert result.total_runs == 0


@pytest.mark.asyncio
class TestFleetServiceCaching:
    async def test_cache_returns_same_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Cached result is returned within TTL."""
        monkeypatch.setattr("sova.dashboard.services.fleet_service.list_projects", dict)
        svc = FleetService(FleetConfig(cache_ttl_seconds=300))

        first = await svc.get_insights()
        second = await svc.get_insights()
        assert first is second

    async def test_force_refresh_bypasses_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """force_refresh=True always re-scans."""
        monkeypatch.setattr("sova.dashboard.services.fleet_service.list_projects", dict)
        svc = FleetService(FleetConfig(cache_ttl_seconds=300))

        first = await svc.get_insights()
        second = await svc.get_insights(force_refresh=True)
        assert first is not second


@pytest.mark.asyncio
class TestFleetServiceRetries:
    async def test_retry_success_rate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Runs with resumed_from_id are counted for retry success rate."""
        project_dir = tmp_path / "retries"
        project_dir.mkdir()
        db_path = project_dir / ".claude" / "sova.db"
        await _create_project_db(db_path)

        await _insert_rows(
            db_path,
            [
                TaskRun(issue_number="1", role="developer", status="failed", total_cost_usd=Decimal("0")),
                TaskRun(
                    issue_number="1", role="developer", status="done", total_cost_usd=Decimal("0"), resumed_from_id=1
                ),
                TaskRun(
                    issue_number="2",
                    role="developer",
                    status="failed",
                    total_cost_usd=Decimal("0"),
                    resumed_from_id=1,
                ),
            ],
        )

        monkeypatch.setattr(
            "sova.dashboard.services.fleet_service.list_projects",
            lambda: {"retries": str(project_dir)},
        )
        svc = FleetService(FleetConfig(cache_ttl_seconds=1))
        result = await svc.get_insights()

        # 2 retried runs (resumed_from_id is not None), 1 done
        assert result.retry_success_rate == pytest.approx(0.5)


@pytest.mark.asyncio
class TestFleetServiceZeroCosts:
    async def test_zero_run_count(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Project with DB but no runs yields zero avg cost."""
        project_dir = tmp_path / "empty-db"
        project_dir.mkdir()
        db_path = project_dir / ".claude" / "sova.db"
        await _create_project_db(db_path)

        monkeypatch.setattr(
            "sova.dashboard.services.fleet_service.list_projects",
            lambda: {"empty-db": str(project_dir)},
        )
        svc = FleetService(FleetConfig(cache_ttl_seconds=1))
        result = await svc.get_insights()

        assert result.total_runs == 0
        assert result.total_cost_usd == Decimal(0)
        assert result.success_rate == 0.0
        if result.cost_by_project:
            assert result.cost_by_project[0].avg_cost_per_run == Decimal(0)


@pytest.mark.asyncio
class TestFleetServiceFailureClusters:
    async def test_clusters_across_projects(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Same failure message from multiple projects is clustered together."""
        shared_msg = "timeout waiting for CI checks"

        for name in ("alpha", "beta"):
            pdir = tmp_path / name
            pdir.mkdir()
            db_path = pdir / ".claude" / "sova.db"
            await _create_project_db(db_path)
            await _insert_rows(
                db_path,
                [
                    TaskRun(issue_number="1", role="developer", status="failed", total_cost_usd=Decimal("0")),
                ],
            )
            await _insert_rows(
                db_path,
                [
                    FailureRecord(task_run_id=1, step_name="monitor_ci", failure_type="timeout", message=shared_msg),
                ],
            )

        monkeypatch.setattr(
            "sova.dashboard.services.fleet_service.list_projects",
            lambda: {"alpha": str(tmp_path / "alpha"), "beta": str(tmp_path / "beta")},
        )
        svc = FleetService(FleetConfig(cache_ttl_seconds=1))
        result = await svc.get_insights()

        cluster = next((c for c in result.failure_clusters if c.pattern == shared_msg), None)
        assert cluster is not None
        assert cluster.count == 2
        assert sorted(cluster.projects) == ["alpha", "beta"]


@pytest.mark.asyncio
class TestFleetServiceNormalization:
    async def test_normalize_error_clustering(self) -> None:
        """Failures differing only in IDs/paths/numbers cluster under one pattern."""
        from sova.dashboard.services.fleet_service import _normalize_error

        # UUID substitution
        msg_a = "DB error: connection 550e8400-e29b-41d4-a716-446655440000 timed out"
        msg_b = "DB error: connection aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee timed out"
        assert _normalize_error(msg_a) == _normalize_error(msg_b)

        # Hex ID substitution (UUID first, then hex)
        msg_c = "build failed for commit deadbeef1234567"
        msg_d = "build failed for commit cafebabe0987654"
        assert _normalize_error(msg_c) == _normalize_error(msg_d)

        # Path substitution
        msg_e = "Test failed in /home/alice/project/foo.py"
        msg_f = "Test failed in /home/bob/project/bar.py"
        assert _normalize_error(msg_e) == _normalize_error(msg_f)

        # Numeric ID substitution
        msg_g = "Issue 42 timed out after 300 seconds"
        msg_h = "Issue 99 timed out after 600 seconds"
        assert _normalize_error(msg_g) == _normalize_error(msg_h)

        # Stack trace: only first line is used
        msg_with_trace = "RuntimeError: disk full\n  File foo.py, line 42, in bar\n    do_thing()"
        assert _normalize_error(msg_with_trace) == "RuntimeError: disk full"

    async def test_normalize_error_clusters_messages_in_service(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Messages that differ only in path cluster to the same pattern in FleetService."""
        project_dir = tmp_path / "norm"
        project_dir.mkdir()
        db_path = project_dir / ".claude" / "sova.db"
        await _create_project_db(db_path)

        await _insert_rows(
            db_path,
            [
                TaskRun(issue_number="1", role="developer", status="failed", total_cost_usd=Decimal("0")),
            ],
        )
        # Two distinct messages that normalize to the same pattern
        await _insert_rows(
            db_path,
            [
                FailureRecord(
                    task_run_id=1,
                    step_name="develop",
                    failure_type="error",
                    message="Test failed in /home/alice/project/foo.py",
                ),
                FailureRecord(
                    task_run_id=1,
                    step_name="develop",
                    failure_type="error",
                    message="Test failed in /home/bob/project/bar.py",
                ),
            ],
        )

        monkeypatch.setattr(
            "sova.dashboard.services.fleet_service.list_projects",
            lambda: {"norm": str(project_dir)},
        )
        svc = FleetService(FleetConfig(cache_ttl_seconds=1))
        result = await svc.get_insights()

        # Both messages normalize to the same pattern, so count == 2
        assert len(result.failure_clusters) == 1
        assert result.failure_clusters[0].count == 2


@pytest.mark.asyncio
class TestFleetServiceAllSkipped:
    async def test_all_projects_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """All projects missing DB: valid result with empty stats."""
        proj = tmp_path / "ghost"
        proj.mkdir()

        monkeypatch.setattr(
            "sova.dashboard.services.fleet_service.list_projects",
            lambda: {"ghost": str(proj)},
        )
        svc = FleetService(FleetConfig(cache_ttl_seconds=1))
        result = await svc.get_insights()

        assert result.projects_scanned == []
        assert result.projects_skipped == ["ghost"]
        assert result.total_runs == 0
        assert result.cost_by_project == []


# ---------------------------------------------------------------------------
# Error branch coverage
# ---------------------------------------------------------------------------


async def _create_minimal_db(db_path: Path, extra_sql: str = "") -> None:
    """Create a SQLite DB with only a task_runs table (minimal schema)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        # Minimal task_runs for schema check to pass
        await conn.execute(text("CREATE TABLE task_runs (id INTEGER PRIMARY KEY)"))
        if extra_sql:
            await conn.execute(text(extra_sql))
    await engine.dispose()


@pytest.mark.asyncio
class TestFleetServiceQueryTimeout:
    async def test_timeout_skips_project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Project whose query times out is added to skipped."""
        project_dir = tmp_path / "slow"
        project_dir.mkdir()
        db_path = project_dir / ".claude" / "sova.db"
        await _create_project_db(db_path)

        async def _slow_query(_self: object, _slug: str, _db_path: Path) -> None:
            await asyncio.sleep(10)

        monkeypatch.setattr(
            "sova.dashboard.services.fleet_service.list_projects",
            lambda: {"slow": str(project_dir)},
        )

        svc = FleetService(FleetConfig(cache_ttl_seconds=1))
        svc._cfg = type(svc._cfg).model_construct(cache_ttl_seconds=1, query_timeout_seconds=0.01)
        with patch.object(FleetService, "_query_project", _slow_query):
            result = await svc.get_insights()

        assert result.projects_scanned == []
        assert "slow" in result.projects_skipped


@pytest.mark.asyncio
class TestFleetServiceQueryRunsFailed:
    async def test_query_runs_exception_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """_query_runs exception returns empty list when columns are missing."""
        project_dir = tmp_path / "bad-runs"
        project_dir.mkdir()
        db_path = project_dir / ".claude" / "sova.db"
        # task_runs exists (schema check passes) but lacks ORM columns
        await _create_minimal_db(db_path)

        monkeypatch.setattr(
            "sova.dashboard.services.fleet_service.list_projects",
            lambda: {"bad-runs": str(project_dir)},
        )

        svc = FleetService(FleetConfig(cache_ttl_seconds=1))
        result = await svc.get_insights()

        # All queries fail gracefully, project is still scanned
        assert "bad-runs" in result.projects_scanned
        assert result.total_runs == 0


@pytest.mark.asyncio
class TestFleetServiceQueryStepsFailed:
    async def test_query_steps_exception_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """_query_steps exception returns empty list when table is missing."""
        project_dir = tmp_path / "bad-steps"
        project_dir.mkdir()
        db_path = project_dir / ".claude" / "sova.db"
        # task_runs with correct columns but no step_executions table
        await _create_minimal_db(db_path)

        monkeypatch.setattr(
            "sova.dashboard.services.fleet_service.list_projects",
            lambda: {"bad-steps": str(project_dir)},
        )

        svc = FleetService(FleetConfig(cache_ttl_seconds=1))
        result = await svc.get_insights()

        assert "bad-steps" in result.projects_scanned
        assert result.step_failure_stats == []


@pytest.mark.asyncio
class TestFleetServiceQueryFailuresFailed:
    async def test_query_failures_exception_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_query_failures exception returns empty list when table is missing."""
        project_dir = tmp_path / "bad-fail"
        project_dir.mkdir()
        db_path = project_dir / ".claude" / "sova.db"
        await _create_minimal_db(db_path)

        monkeypatch.setattr(
            "sova.dashboard.services.fleet_service.list_projects",
            lambda: {"bad-fail": str(project_dir)},
        )

        svc = FleetService(FleetConfig(cache_ttl_seconds=1))
        result = await svc.get_insights()

        assert "bad-fail" in result.projects_scanned
        assert result.failure_clusters == []


@pytest.mark.asyncio
class TestFleetServiceQueryResumedFailed:
    async def test_query_resumed_exception_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """_query_resumed_runs exception returns empty list (e.g. old schema)."""
        project_dir = tmp_path / "old-schema"
        project_dir.mkdir()
        db_path = project_dir / ".claude" / "sova.db"
        # task_runs without resumed_from_id column
        await _create_minimal_db(db_path)

        monkeypatch.setattr(
            "sova.dashboard.services.fleet_service.list_projects",
            lambda: {"old-schema": str(project_dir)},
        )

        svc = FleetService(FleetConfig(cache_ttl_seconds=1))
        result = await svc.get_insights()

        assert "old-schema" in result.projects_scanned
        assert result.retry_success_rate == 0.0
