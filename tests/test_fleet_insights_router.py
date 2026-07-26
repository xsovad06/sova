"""Tests for sova.dashboard.routers.fleet_insights -- fleet analytics page and API."""

from __future__ import annotations

import os
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from sova.dashboard.routers.fleet_insights import _get_fleet_service
from sova.dashboard.services.fleet_service import (
    FailureCluster,
    FleetInsights,
    ProjectCostStat,
    StepFailureStat,
)
from sova.db.session import close_db, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    """Initialize an in-memory DB for dashboard tests."""
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


def _make_insights(
    *,
    total_runs: int = 100,
    success_rate: float = 0.85,
    retry_success_rate: float = 0.6,
    total_cost: Decimal = Decimal("12.50"),
    scanned: list[str] | None = None,
    skipped: list[str] | None = None,
    steps: list[StepFailureStat] | None = None,
    clusters: list[FailureCluster] | None = None,
    costs: list[ProjectCostStat] | None = None,
) -> FleetInsights:
    return FleetInsights(
        generated_at=1700000000.0,
        projects_scanned=["alpha", "beta"] if scanned is None else scanned,
        projects_skipped=skipped if skipped is not None else [],
        total_runs=total_runs,
        total_cost_usd=total_cost,
        success_rate=success_rate,
        retry_success_rate=retry_success_rate,
        step_failure_stats=steps or [],
        failure_clusters=clusters or [],
        cost_by_project=costs or [],
    )


def _mock_service(insights: FleetInsights) -> object:
    """Create a mock FleetService returning the given insights."""
    svc = AsyncMock()
    svc.get_insights = AsyncMock(return_value=insights)
    return svc


@pytest.fixture
async def client():
    from sova.dashboard.app import create_app

    app = create_app(multi_project=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def override_fleet(client: AsyncClient) -> object:
    """Return a helper that sets the FleetService dependency override on the app."""
    # Extract the app from the transport
    app = client._transport.app  # type: ignore[union-attr]

    def _set(insights: FleetInsights) -> AsyncMock:
        mock_svc = _mock_service(insights)
        app.dependency_overrides[_get_fleet_service] = lambda: mock_svc
        return mock_svc

    yield _set
    app.dependency_overrides.pop(_get_fleet_service, None)


# ---------------------------------------------------------------------------
# Page route tests
# ---------------------------------------------------------------------------


class TestFleetPage:
    async def test_fleet_page_loads(self, client: AsyncClient) -> None:
        resp = await client.get("/fleet")
        assert resp.status_code == 200
        assert "Fleet Insights" in resp.text

    async def test_fleet_page_has_sections(self, client: AsyncClient) -> None:
        resp = await client.get("/fleet")
        assert resp.status_code == 200
        assert "Step Failure Leaderboard" in resp.text
        assert "Error Clusters" in resp.text
        assert "Cost by Project" in resp.text
        assert "Retry Effectiveness" in resp.text
        assert "Project Scan Status" in resp.text

    async def test_fleet_sidebar_active(self, client: AsyncClient) -> None:
        resp = await client.get("/fleet")
        assert resp.status_code == 200
        assert "bg-sidebar-active" in resp.text


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestFleetInsightsAPI:
    async def test_get_insights_returns_json(self, client: AsyncClient, override_fleet) -> None:
        override_fleet(_make_insights())
        resp = await client.get("/api/fleet-insights/data")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_runs"] == 100
        assert data["success_rate"] == 0.85
        assert isinstance(data["total_cost_usd"], float)
        assert data["total_cost_usd"] == 12.5
        assert data["projects_scanned"] == ["alpha", "beta"]

    async def test_get_insights_with_force(self, client: AsyncClient, override_fleet) -> None:
        mock_svc = override_fleet(_make_insights())
        resp = await client.get("/api/fleet-insights/data?force=true")
        assert resp.status_code == 200
        mock_svc.get_insights.assert_called_once_with(force_refresh=True)

    async def test_empty_registry(self, client: AsyncClient, override_fleet) -> None:
        override_fleet(
            _make_insights(
                total_runs=0,
                success_rate=0.0,
                retry_success_rate=0.0,
                scanned=[],
                total_cost=Decimal(0),
            )
        )
        resp = await client.get("/api/fleet-insights/data")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_runs"] == 0
        assert data["projects_scanned"] == []

    async def test_skipped_projects_included(self, client: AsyncClient, override_fleet) -> None:
        override_fleet(_make_insights(skipped=["broken-proj"]))
        resp = await client.get("/api/fleet-insights/data")
        data = resp.json()
        assert data["projects_skipped"] == ["broken-proj"]

    async def test_step_failure_stats_serialized(self, client: AsyncClient, override_fleet) -> None:
        steps = [
            StepFailureStat(step_name="develop", total_count=50, failure_count=10, failure_rate=0.2),
            StepFailureStat(step_name="push", total_count=40, failure_count=0, failure_rate=0.0),
        ]
        override_fleet(_make_insights(steps=steps))
        resp = await client.get("/api/fleet-insights/data")
        data = resp.json()
        assert len(data["step_failure_stats"]) == 2
        assert data["step_failure_stats"][0]["step_name"] == "develop"
        assert data["step_failure_stats"][0]["failure_rate"] == 0.2

    async def test_failure_clusters_serialized(self, client: AsyncClient, override_fleet) -> None:
        clusters = [
            FailureCluster(pattern="Tests failed at <PATH>", count=5, projects=["alpha"]),
        ]
        override_fleet(_make_insights(clusters=clusters))
        resp = await client.get("/api/fleet-insights/data")
        data = resp.json()
        assert len(data["failure_clusters"]) == 1
        assert data["failure_clusters"][0]["count"] == 5
        assert data["failure_clusters"][0]["projects"] == ["alpha"]

    async def test_cost_breakdown_decimal_serialization(self, client: AsyncClient, override_fleet) -> None:
        costs = [
            ProjectCostStat(
                slug="alpha",
                run_count=10,
                total_cost_usd=Decimal("5.123456"),
                avg_cost_per_run=Decimal("0.512346"),
            ),
        ]
        override_fleet(_make_insights(costs=costs))
        resp = await client.get("/api/fleet-insights/data")
        data = resp.json()
        assert len(data["cost_by_project"]) == 1
        assert isinstance(data["cost_by_project"][0]["total_cost_usd"], float)
        assert data["cost_by_project"][0]["slug"] == "alpha"

    async def test_zero_retry_rate(self, client: AsyncClient, override_fleet) -> None:
        override_fleet(_make_insights(retry_success_rate=0.0))
        resp = await client.get("/api/fleet-insights/data")
        data = resp.json()
        assert data["retry_success_rate"] == 0.0

    async def test_service_error_returns_503(self, client: AsyncClient, override_fleet) -> None:
        mock_svc = _mock_service(_make_insights())
        mock_svc.get_insights = AsyncMock(side_effect=RuntimeError("db connection failed"))
        app = client._transport.app  # type: ignore[union-attr]
        app.dependency_overrides[_get_fleet_service] = lambda: mock_svc
        resp = await client.get("/api/fleet-insights/data")
        assert resp.status_code == 503
        app.dependency_overrides.pop(_get_fleet_service, None)
