"""Tests for the telemetry ingestion endpoint and FleetService telemetry merge."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from sova.config.models import FleetConfig
from sova.dashboard.services.fleet_service import FleetService
from sova.db.models import TelemetryEvent
from sova.db.session import close_db, get_session, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


@pytest.fixture(autouse=True)
def _clear_rate_windows():
    """Clear rate limit state before and after every test to prevent leakage."""
    from sova.dashboard.routers import telemetry as telemetry_mod

    telemetry_mod._rate_windows.clear()
    yield
    telemetry_mod._rate_windows.clear()


@pytest.fixture
def telemetry_token(monkeypatch: pytest.MonkeyPatch) -> str:
    token = "test-secret-token-abc123"
    monkeypatch.setenv("SOVA_TELEMETRY_TOKEN", token)
    return token


def _make_app():
    from sova.dashboard.app import create_app

    return create_app(project_dir=Path.cwd())


def _make_payload(**overrides) -> dict:
    base = {
        "machine_id": "machine-001",
        "run_id": "run-001",
        "project_slug": "my-project",
        "role": "developer",
        "status": "done",
        "cost_usd": 1.5,
        "duration_ms": 30000,
        "run_at": "2026-07-26T10:00:00Z",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestTelemetryAuth:
    async def test_no_token_configured_returns_503(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SOVA_TELEMETRY_TOKEN", raising=False)
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/telemetry/ingest", json=_make_payload())
        assert resp.status_code == 503

    async def test_missing_auth_header_returns_401(self, telemetry_token: str) -> None:
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/telemetry/ingest", json=_make_payload())
        assert resp.status_code == 401

    async def test_wrong_token_returns_403(self, telemetry_token: str) -> None:
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/telemetry/ingest",
                json=_make_payload(),
                headers={"Authorization": "Bearer wrong-token"},
            )
        assert resp.status_code == 403

    async def test_valid_token_accepted(self, telemetry_token: str) -> None:
        app = _make_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/telemetry/ingest",
                json=_make_payload(),
                headers={"Authorization": f"Bearer {telemetry_token}"},
            )
        assert resp.status_code == 201
        assert resp.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Ingestion tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestTelemetryIngest:
    async def test_basic_ingest(self, telemetry_token: str) -> None:
        app = _make_app()
        payload = _make_payload(
            step_outcomes={"develop": "done", "push": {"status": "done"}},
            issue_number="42",
            pr_number=10,
            error_message=None,
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/telemetry/ingest",
                json=payload,
                headers={"Authorization": f"Bearer {telemetry_token}"},
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "ok"
        assert data["machine_id"] == "machine-001"

        # Verify DB row
        async with await get_session() as session:
            from sqlalchemy import select

            result = await session.execute(select(TelemetryEvent))
            event = result.scalar_one()
            assert event.machine_id == "machine-001"
            assert event.run_id == "run-001"
            assert event.project_slug == "my-project"
            assert event.role == "developer"
            assert event.status == "done"
            assert event.issue_number == "42"
            assert event.pr_number == 10
            assert event.cost_usd == Decimal("1.5")
            assert event.step_outcomes == {"develop": "done", "push": {"status": "done"}}
            assert event.received_at is not None

    async def test_duplicate_returns_200(self, telemetry_token: str) -> None:
        app = _make_app()
        headers = {"Authorization": f"Bearer {telemetry_token}"}
        payload = _make_payload()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp1 = await client.post("/api/telemetry/ingest", json=payload, headers=headers)
            assert resp1.status_code == 201
            assert resp1.json()["status"] == "ok"

            resp2 = await client.post("/api/telemetry/ingest", json=payload, headers=headers)
            assert resp2.status_code == 200
            assert resp2.json()["status"] == "duplicate"

        # Only one row in DB
        async with await get_session() as session:
            from sqlalchemy import func, select

            result = await session.execute(select(func.count(TelemetryEvent.id)))
            assert result.scalar_one() == 1

    async def test_different_run_ids_both_stored(self, telemetry_token: str) -> None:
        app = _make_app()
        headers = {"Authorization": f"Bearer {telemetry_token}"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp1 = await client.post(
                "/api/telemetry/ingest",
                json=_make_payload(run_id="run-001"),
                headers=headers,
            )
            resp2 = await client.post(
                "/api/telemetry/ingest",
                json=_make_payload(run_id="run-002"),
                headers=headers,
            )
            assert resp1.status_code == 201
            assert resp2.status_code == 201

        async with await get_session() as session:
            from sqlalchemy import func, select

            result = await session.execute(select(func.count(TelemetryEvent.id)))
            assert result.scalar_one() == 2


# ---------------------------------------------------------------------------
# Rate limiting tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestTelemetryRateLimit:
    async def test_rate_limit_enforced(self, telemetry_token: str, monkeypatch: pytest.MonkeyPatch) -> None:
        from sova.dashboard.routers import telemetry as telemetry_mod

        # Lower limit for testing
        monkeypatch.setattr(telemetry_mod, "_RATE_LIMIT_MAX", 3)

        app = _make_app()
        headers = {"Authorization": f"Bearer {telemetry_token}"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for i in range(3):
                resp = await client.post(
                    "/api/telemetry/ingest",
                    json=_make_payload(run_id=f"rate-{i}"),
                    headers=headers,
                )
                assert resp.status_code == 201

            # 4th request should be rate limited
            resp = await client.post(
                "/api/telemetry/ingest",
                json=_make_payload(run_id="rate-3"),
                headers=headers,
            )
            assert resp.status_code == 429
            assert resp.headers.get("retry-after") == "60"

    async def test_rate_limit_retry_after_header(self, telemetry_token: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """429 response includes Retry-After header."""
        from sova.dashboard.routers import telemetry as telemetry_mod

        monkeypatch.setattr(telemetry_mod, "_RATE_LIMIT_MAX", 1)

        app = _make_app()
        headers = {"Authorization": f"Bearer {telemetry_token}"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/api/telemetry/ingest", json=_make_payload(run_id="fill"), headers=headers)
            resp = await client.post("/api/telemetry/ingest", json=_make_payload(run_id="over"), headers=headers)

        assert resp.status_code == 429
        assert resp.headers["retry-after"] == "60"


# ---------------------------------------------------------------------------
# FleetService telemetry merge tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestFleetServiceTelemetryMerge:
    async def _insert_telemetry_events(self, events: list[TelemetryEvent]) -> None:
        async with await get_session() as session:
            async with session.begin():
                for ev in events:
                    session.add(ev)

    async def test_remote_events_merged_into_insights(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Telemetry events appear in fleet insights as remote:{machine}:{slug} entries."""
        now = datetime.now(timezone.utc)
        await self._insert_telemetry_events(
            [
                TelemetryEvent(
                    machine_id="m1",
                    run_id="r1",
                    project_slug="proj-x",
                    role="developer",
                    status="done",
                    cost_usd=Decimal("2.0"),
                    duration_ms=10000,
                    run_at=now,
                    step_outcomes={"develop": "done", "push": "done"},
                ),
                TelemetryEvent(
                    machine_id="m1",
                    run_id="r2",
                    project_slug="proj-x",
                    role="developer",
                    status="failed",
                    cost_usd=Decimal("0.5"),
                    duration_ms=5000,
                    run_at=now,
                    step_outcomes={"develop": {"status": "failed"}},
                ),
            ]
        )

        monkeypatch.setattr("sova.dashboard.services.fleet_service.list_projects", dict)
        svc = FleetService(FleetConfig(cache_ttl_seconds=1))
        result = await svc.get_insights()

        assert result.total_runs == 2
        assert result.total_cost_usd == Decimal("2.5")
        assert result.success_rate == pytest.approx(0.5)

        # Cost by project includes remote entry with machine_id
        assert len(result.cost_by_project) == 1
        assert result.cost_by_project[0].slug == "remote:m1:proj-x"

        # Step stats from step_outcomes
        develop_stat = next((s for s in result.step_failure_stats if s.step_name == "develop"), None)
        assert develop_stat is not None
        assert develop_stat.total_count == 2
        assert develop_stat.failure_count == 1

        push_stat = next((s for s in result.step_failure_stats if s.step_name == "push"), None)
        assert push_stat is not None
        assert push_stat.total_count == 1
        assert push_stat.failure_count == 0

    async def test_null_step_outcomes_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Events with None step_outcomes don't break iteration."""
        now = datetime.now(timezone.utc)
        await self._insert_telemetry_events(
            [
                TelemetryEvent(
                    machine_id="m1",
                    run_id="r1",
                    project_slug="proj-y",
                    role="developer",
                    status="done",
                    cost_usd=Decimal("1.0"),
                    duration_ms=5000,
                    run_at=now,
                    step_outcomes=None,
                ),
            ]
        )

        monkeypatch.setattr("sova.dashboard.services.fleet_service.list_projects", dict)
        svc = FleetService(FleetConfig(cache_ttl_seconds=1))
        result = await svc.get_insights()

        assert result.total_runs == 1
        assert result.step_failure_stats == []

    async def test_empty_step_outcomes_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Events with empty dict step_outcomes don't produce step stats."""
        now = datetime.now(timezone.utc)
        await self._insert_telemetry_events(
            [
                TelemetryEvent(
                    machine_id="m1",
                    run_id="r1",
                    project_slug="proj-z",
                    role="developer",
                    status="done",
                    cost_usd=Decimal("0.5"),
                    duration_ms=3000,
                    run_at=now,
                    step_outcomes={},
                ),
            ]
        )

        monkeypatch.setattr("sova.dashboard.services.fleet_service.list_projects", dict)
        svc = FleetService(FleetConfig(cache_ttl_seconds=1))
        result = await svc.get_insights()

        assert result.total_runs == 1
        assert result.step_failure_stats == []

    async def test_telemetry_query_exception_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When _query_telemetry_events raises, falls back to empty results."""
        monkeypatch.setattr("sova.dashboard.services.fleet_service.list_projects", dict)

        async def _broken() -> tuple[list, list]:
            raise Exception("no such table: telemetry_events")

        svc = FleetService(FleetConfig(cache_ttl_seconds=1))
        with patch.object(FleetService, "_query_telemetry_events", side_effect=_broken):
            result = await svc.get_insights()

        assert result.total_runs == 0

    async def test_cost_decimal_consistency(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Float cost_usd from remote events is cast to Decimal for summing."""
        now = datetime.now(timezone.utc)
        await self._insert_telemetry_events(
            [
                TelemetryEvent(
                    machine_id="m1",
                    run_id="r1",
                    project_slug="proj-a",
                    role="developer",
                    status="done",
                    cost_usd=Decimal("0.1"),
                    duration_ms=1000,
                    run_at=now,
                ),
                TelemetryEvent(
                    machine_id="m1",
                    run_id="r2",
                    project_slug="proj-a",
                    role="developer",
                    status="done",
                    cost_usd=Decimal("0.2"),
                    duration_ms=2000,
                    run_at=now,
                ),
            ]
        )

        monkeypatch.setattr("sova.dashboard.services.fleet_service.list_projects", dict)
        svc = FleetService(FleetConfig(cache_ttl_seconds=1))
        result = await svc.get_insights()

        assert isinstance(result.total_cost_usd, Decimal)
        assert result.total_cost_usd == Decimal("0.3")

    async def test_malformed_step_outcomes_value_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-string, non-dict step_outcomes values are skipped without error."""
        now = datetime.now(timezone.utc)
        await self._insert_telemetry_events(
            [
                TelemetryEvent(
                    machine_id="m1",
                    run_id="r1",
                    project_slug="proj-m",
                    role="developer",
                    status="done",
                    cost_usd=Decimal("1.0"),
                    duration_ms=1000,
                    run_at=now,
                    step_outcomes={"develop": ["list", "value"], "push": 42, "commit": None},
                ),
            ]
        )

        monkeypatch.setattr("sova.dashboard.services.fleet_service.list_projects", dict)
        svc = FleetService(FleetConfig(cache_ttl_seconds=1))
        result = await svc.get_insights()

        assert result.total_runs == 1
        # Malformed values are skipped; no step stats produced
        assert result.step_failure_stats == []

    async def test_different_machines_separate_slugs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two machines running the same project produce distinct slug entries."""
        now = datetime.now(timezone.utc)
        await self._insert_telemetry_events(
            [
                TelemetryEvent(
                    machine_id="m1",
                    run_id="r1",
                    project_slug="proj",
                    role="developer",
                    status="done",
                    cost_usd=Decimal("1.0"),
                    duration_ms=1000,
                    run_at=now,
                ),
                TelemetryEvent(
                    machine_id="m2",
                    run_id="r1",
                    project_slug="proj",
                    role="developer",
                    status="done",
                    cost_usd=Decimal("2.0"),
                    duration_ms=2000,
                    run_at=now,
                ),
            ]
        )

        monkeypatch.setattr("sova.dashboard.services.fleet_service.list_projects", dict)
        svc = FleetService(FleetConfig(cache_ttl_seconds=1))
        result = await svc.get_insights()

        assert result.total_runs == 2
        slugs = {p.slug for p in result.cost_by_project}
        assert slugs == {"remote:m1:proj", "remote:m2:proj"}

    async def test_stale_eviction_removes_idle_machines(self) -> None:
        """Stale machine entries with expired windows are evicted."""
        import time

        from sova.dashboard.routers import telemetry as telemetry_mod

        # Simulate >200 machine entries with empty deques
        for i in range(210):
            telemetry_mod._rate_windows[f"stale-{i}"] = deque()

        # Add one active entry
        telemetry_mod._rate_windows["active"] = deque([time.monotonic()])

        # Trigger eviction by calling _check_rate_limit on any machine
        try:
            telemetry_mod._check_rate_limit("trigger")
        except Exception:
            pass

        # Stale entries should be cleaned, active should remain
        assert "active" in telemetry_mod._rate_windows
        assert len(telemetry_mod._rate_windows) < 210


# Need deque for the stale eviction test
from collections import deque  # noqa: E402
