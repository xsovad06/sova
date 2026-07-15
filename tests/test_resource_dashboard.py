"""Tests for resource monitoring dashboard service and API router."""

from __future__ import annotations

import os
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sova.db.models import ResourceSampleRecord, ResourceSummaryRecord, TaskRun
from sova.db.session import close_db, get_session, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


@pytest.fixture
async def session() -> AsyncSession:
    return await get_session()


@pytest.fixture
async def client():
    from sova.dashboard.app import create_app

    app = create_app(multi_project=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def seed_run_with_resources(session: AsyncSession):
    """Create a TaskRun with resource samples and summary."""
    now = datetime.now(timezone.utc)
    run = TaskRun(
        issue_number="100",
        role="developer",
        status="done",
        current_step="complete",
        branch_name="feat/test",
        total_cost_usd=Decimal("1.00"),
        project_slug="test",
        started_at=now - timedelta(hours=1),
        ended_at=now,
    )
    session.add(run)
    await session.flush()

    # Add resource samples
    samples = []
    for i in range(10):
        samples.append(
            ResourceSampleRecord(
                task_run_id=run.id,
                sampled_at=now - timedelta(minutes=60 - i * 6),
                cpu_percent=10.0 + i * 5,
                memory_rss_bytes=100_000_000 + i * 10_000_000,
                memory_vms_bytes=200_000_000 + i * 10_000_000,
                io_read_bytes=1000 * i if i % 2 == 0 else None,
                io_write_bytes=500 * i if i % 2 == 0 else None,
                num_children=2,
                num_threads=4 + i,
            )
        )
    session.add_all(samples)

    summary = ResourceSummaryRecord(
        task_run_id=run.id,
        sample_count=10,
        peak_cpu_percent=55.0,
        avg_cpu_percent=32.5,
        peak_memory_rss_bytes=190_000_000,
        peak_memory_vms_bytes=290_000_000,
        total_io_read_bytes=20000,
        total_io_write_bytes=10000,
        peak_num_threads=13,
    )
    session.add(summary)
    await session.commit()
    return run


@pytest.fixture
async def seed_run_without_resources(session: AsyncSession):
    """Create a TaskRun without resource data."""
    now = datetime.now(timezone.utc)
    run = TaskRun(
        issue_number="101",
        role="triage",
        status="done",
        current_step="complete",
        total_cost_usd=Decimal("0.10"),
        project_slug="test",
        started_at=now - timedelta(minutes=5),
        ended_at=now,
    )
    session.add(run)
    await session.commit()
    return run


class TestResourceService:
    @pytest.mark.asyncio
    async def test_get_resource_summary_with_data(self, session: AsyncSession, seed_run_with_resources) -> None:
        from sova.dashboard.services.resource_service import get_resource_summary

        result = await get_resource_summary(session, seed_run_with_resources.id)
        assert result is not None
        assert result["run_id"] == seed_run_with_resources.id
        s = result["summary"]
        assert s is not None
        assert s["peak_cpu_percent"] == 55.0
        assert s["avg_cpu_percent"] == 32.5
        assert s["peak_memory_rss_bytes"] == 190_000_000
        assert s["sample_count"] == 10
        assert s["total_io_read_bytes"] == 20000

    @pytest.mark.asyncio
    async def test_get_resource_summary_no_data(self, session: AsyncSession, seed_run_without_resources) -> None:
        from sova.dashboard.services.resource_service import get_resource_summary

        result = await get_resource_summary(session, seed_run_without_resources.id)
        assert result is not None
        assert result["summary"] is None

    @pytest.mark.asyncio
    async def test_get_resource_summary_nonexistent_run(self, session: AsyncSession) -> None:
        from sova.dashboard.services.resource_service import get_resource_summary

        result = await get_resource_summary(session, 99999)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_resource_samples(self, session: AsyncSession, seed_run_with_resources) -> None:
        from sova.dashboard.services.resource_service import get_resource_samples

        result = await get_resource_samples(session, seed_run_with_resources.id)
        assert result is not None
        assert result["total_count"] == 10
        assert result["returned_count"] == 10
        assert len(result["samples"]) == 10
        sample = result["samples"][0]
        assert "sampled_at" in sample
        assert "cpu_percent" in sample
        assert "memory_rss_bytes" in sample

    @pytest.mark.asyncio
    async def test_get_resource_samples_downsampling(self, session: AsyncSession, seed_run_with_resources) -> None:
        from sova.dashboard.services.resource_service import get_resource_samples

        result = await get_resource_samples(session, seed_run_with_resources.id, limit=5)
        assert result is not None
        assert result["total_count"] == 10
        assert result["returned_count"] == 5

    @pytest.mark.asyncio
    async def test_get_resource_samples_nonexistent_run(self, session: AsyncSession) -> None:
        from sova.dashboard.services.resource_service import get_resource_samples

        result = await get_resource_samples(session, 99999)
        assert result is None

    def test_get_live_metrics_no_agent(self) -> None:
        from sova.dashboard.services.resource_service import get_live_metrics

        result = get_live_metrics(99999)
        assert result is None

    def test_get_live_metrics_no_collector(self) -> None:
        from sova.dashboard.services.agent_pool import AgentState, _get_project_agents
        from sova.dashboard.services.resource_service import get_live_metrics

        pa = _get_project_agents()
        mock_process = MagicMock()
        mock_process.pid = 12345
        agent = AgentState(run_id=777, issue="50", role="developer", process=mock_process)
        agent.resource_collector = None
        pa.agents[777] = agent
        try:
            result = get_live_metrics(777)
            assert result is not None
            assert result["cpu_percent"] is None
            assert result["memory_rss_bytes"] is None
        finally:
            del pa.agents[777]

    def test_get_live_metrics_with_collector(self) -> None:
        from sova.dashboard.services.agent_pool import AgentState, _get_project_agents
        from sova.dashboard.services.resource_service import get_live_metrics
        from sova.monitoring.models import ResourceSample

        pa = _get_project_agents()
        mock_process = MagicMock()
        mock_process.pid = 12345
        agent = AgentState(run_id=778, issue="51", role="developer", process=mock_process)
        mock_collector = MagicMock()
        sample = ResourceSample(
            timestamp=1000.0,
            cpu_percent=42.5,
            memory_rss_bytes=150_000_000,
            memory_vms_bytes=300_000_000,
            io_read_bytes=None,
            io_write_bytes=None,
            num_children=1,
            num_threads=3,
        )
        mock_collector.samples = deque([sample])
        agent.resource_collector = mock_collector
        pa.agents[778] = agent
        try:
            result = get_live_metrics(778)
            assert result is not None
            assert result["cpu_percent"] == 42.5
            assert result["memory_rss_bytes"] == 150_000_000
        finally:
            del pa.agents[778]

    def test_get_live_metrics_empty_samples(self) -> None:
        from sova.dashboard.services.agent_pool import AgentState, _get_project_agents
        from sova.dashboard.services.resource_service import get_live_metrics

        pa = _get_project_agents()
        mock_process = MagicMock()
        mock_process.pid = 12345
        agent = AgentState(run_id=779, issue="52", role="developer", process=mock_process)
        mock_collector = MagicMock()
        mock_collector.samples = deque()  # empty deque is falsy
        agent.resource_collector = mock_collector
        pa.agents[779] = agent
        try:
            result = get_live_metrics(779)
            assert result is not None
            assert result["cpu_percent"] is None
        finally:
            del pa.agents[779]

    def test_get_system_info(self) -> None:
        from sova.dashboard.services.resource_service import get_system_info

        info = get_system_info()
        assert "cpu_count" in info
        assert "total_memory_bytes" in info
        assert info["cpu_count"] > 0
        assert info["total_memory_bytes"] > 0

    def test_downsample(self) -> None:
        from sova.dashboard.services.resource_service import _downsample

        items = list(range(100))
        result = _downsample(items, 10)
        assert len(result) == 10
        assert result[0] == 0
        assert result[-1] == 90

    def test_downsample_no_op(self) -> None:
        from sova.dashboard.services.resource_service import _downsample

        items = list(range(5))
        result = _downsample(items, 10)
        assert result == items

    def test_downsample_zero_limit_raises(self) -> None:
        from sova.dashboard.services.resource_service import _downsample

        with pytest.raises(ValueError, match="limit must be positive"):
            _downsample(list(range(10)), 0)

    def test_downsample_negative_limit_raises(self) -> None:
        from sova.dashboard.services.resource_service import _downsample

        with pytest.raises(ValueError, match="limit must be positive"):
            _downsample(list(range(10)), -5)


class TestResourceRouter:
    @pytest.mark.asyncio
    async def test_summary_endpoint(self, client, seed_run_with_resources) -> None:
        resp = await client.get(f"/api/resources/{seed_run_with_resources.id}/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["run_id"] == seed_run_with_resources.id
        assert data["summary"]["peak_cpu_percent"] == 55.0

    @pytest.mark.asyncio
    async def test_summary_endpoint_no_resources(self, client, seed_run_without_resources) -> None:
        resp = await client.get(f"/api/resources/{seed_run_without_resources.id}/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["summary"] is None

    @pytest.mark.asyncio
    async def test_summary_endpoint_404(self, client: AsyncClient) -> None:
        resp = await client.get("/api/resources/99999/summary")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_samples_endpoint(self, client, seed_run_with_resources) -> None:
        resp = await client.get(f"/api/resources/{seed_run_with_resources.id}/samples")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_count"] == 10
        assert len(data["samples"]) == 10

    @pytest.mark.asyncio
    async def test_samples_endpoint_with_limit(self, client, seed_run_with_resources) -> None:
        resp = await client.get(f"/api/resources/{seed_run_with_resources.id}/samples?limit=3")
        assert resp.status_code == 200
        data = resp.json()
        assert data["returned_count"] == 3

    @pytest.mark.asyncio
    async def test_samples_endpoint_404(self, client: AsyncClient) -> None:
        resp = await client.get("/api/resources/99999/samples")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_live_metrics_endpoint_no_agent(self, client: AsyncClient) -> None:
        resp = await client.get("/api/resources/live/99999")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cpu_percent"] is None
        assert data["memory_rss_bytes"] is None

    @pytest.mark.asyncio
    async def test_live_metrics_endpoint_with_agent(self, client: AsyncClient) -> None:
        from collections import deque
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_pool import AgentState, _get_project_agents
        from sova.monitoring.models import ResourceSample

        pa = _get_project_agents()
        mock_process = MagicMock()
        mock_process.pid = 12345
        agent = AgentState(run_id=850, issue="80", role="developer", process=mock_process)
        mock_collector = MagicMock()
        sample = ResourceSample(
            timestamp=1000.0,
            cpu_percent=60.0,
            memory_rss_bytes=120_000_000,
            memory_vms_bytes=240_000_000,
            io_read_bytes=None,
            io_write_bytes=None,
            num_children=1,
            num_threads=4,
        )
        mock_collector.samples = deque([sample])
        agent.resource_collector = mock_collector
        pa.agents[850] = agent
        try:
            resp = await client.get("/api/resources/live/850")
            assert resp.status_code == 200
            data = resp.json()
            assert data["cpu_percent"] == 60.0
            assert data["memory_rss_bytes"] == 120_000_000
        finally:
            del pa.agents[850]

    @pytest.mark.asyncio
    async def test_system_info_endpoint(self, client: AsyncClient) -> None:
        resp = await client.get("/api/resources/system")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cpu_count"] > 0
        assert data["total_memory_bytes"] > 0


class TestResourceRouterErrors:
    @pytest.mark.asyncio
    async def test_summary_endpoint_500_on_service_error(self, client: AsyncClient, seed_run_with_resources) -> None:
        from unittest.mock import patch

        target = "sova.dashboard.routers.resources.resource_service.get_resource_summary"
        with patch(target, side_effect=RuntimeError("db down")):
            resp = await client.get(f"/api/resources/{seed_run_with_resources.id}/summary")
        assert resp.status_code == 500
        assert "Failed to fetch resource summary" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_samples_endpoint_500_on_service_error(self, client: AsyncClient, seed_run_with_resources) -> None:
        from unittest.mock import patch

        target = "sova.dashboard.routers.resources.resource_service.get_resource_samples"
        with patch(target, side_effect=RuntimeError("db down")):
            resp = await client.get(f"/api/resources/{seed_run_with_resources.id}/samples")
        assert resp.status_code == 500
        assert "Failed to fetch resource samples" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_samples_endpoint_422_on_zero_limit(self, client: AsyncClient) -> None:
        resp = await client.get("/api/resources/1/samples?limit=0")
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_samples_endpoint_422_on_negative_limit(self, client: AsyncClient) -> None:
        resp = await client.get("/api/resources/1/samples?limit=-5")
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_live_metrics_endpoint_500_on_service_error(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        target = "sova.dashboard.routers.resources.resource_service.get_live_metrics"
        with patch(target, side_effect=RuntimeError("oops")):
            resp = await client.get("/api/resources/live/1")
        assert resp.status_code == 500
        assert "Failed to fetch live metrics" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_system_info_endpoint_500_on_service_error(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        target = "sova.dashboard.routers.resources.resource_service.get_system_info"
        with patch(target, side_effect=RuntimeError("oops")):
            resp = await client.get("/api/resources/system")
        assert resp.status_code == 500
        assert "Failed to fetch system info" in resp.json()["detail"]


class TestWorkHistoryHelpers:
    def test_compute_duration_ms_with_tz(self) -> None:
        from sova.dashboard.services.work_service import _compute_duration_ms

        start = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2024, 1, 1, 0, 1, 0, tzinfo=timezone.utc)
        assert _compute_duration_ms(start, end) == 60_000

    def test_compute_duration_ms_without_tz(self) -> None:
        from sova.dashboard.services.work_service import _compute_duration_ms

        start = datetime(2024, 1, 1, 0, 0, 0)
        end = datetime(2024, 1, 1, 0, 0, 30)
        assert _compute_duration_ms(start, end) == 30_000

    def test_compute_duration_ms_none(self) -> None:
        from sova.dashboard.services.work_service import _compute_duration_ms

        assert _compute_duration_ms(None, datetime.now(timezone.utc)) is None
        assert _compute_duration_ms(datetime.now(timezone.utc), None) is None

    def test_resolve_variant_researcher(self) -> None:
        from sova.dashboard.services.work_service import _resolve_variant

        result = _resolve_variant({"research", "spec"}, None, "researcher", None)
        assert result == "researcher"

    def test_resolve_variant_address_review(self) -> None:
        from sova.dashboard.services.work_service import _resolve_variant

        result = _resolve_variant({"address_review", "rebase"}, None, "developer", 42)
        assert result == "address_review"

    def test_resolve_variant_developer_fallback(self) -> None:
        from sova.dashboard.services.work_service import _resolve_variant

        result = _resolve_variant({"develop", "commit"}, "develop", "developer", None)
        assert result == "developer"


class TestRunToDict:
    @pytest.mark.asyncio
    async def test_run_to_dict_includes_resource_fields(self, session: AsyncSession, seed_run_with_resources) -> None:
        from sova.dashboard.services.work_service import list_runs

        runs = await list_runs(session, limit=10)
        run_dict = next(r for r in runs if r["id"] == seed_run_with_resources.id)
        assert "peak_cpu_percent" in run_dict
        assert run_dict["peak_cpu_percent"] == 55.0
        assert run_dict["peak_memory_rss_bytes"] == 190_000_000

    @pytest.mark.asyncio
    async def test_run_to_dict_no_resource_fields(self, session: AsyncSession, seed_run_without_resources) -> None:
        from sova.dashboard.services.work_service import list_runs

        runs = await list_runs(session, limit=10)
        run_dict = next(r for r in runs if r["id"] == seed_run_without_resources.id)
        assert "peak_cpu_percent" not in run_dict


class TestAgentLiveMetrics:
    @pytest.mark.asyncio
    async def test_get_all_agents_includes_resource_fields(self) -> None:
        from sova.dashboard.services.agent_lifecycle import get_all_agents
        from sova.dashboard.services.agent_pool import AgentState, _get_project_agents
        from sova.monitoring.models import ResourceSample

        pa = _get_project_agents()
        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.returncode = None
        agent = AgentState(run_id=800, issue="60", role="developer", process=mock_process)
        mock_collector = MagicMock()
        sample = ResourceSample(
            timestamp=1000.0,
            cpu_percent=75.0,
            memory_rss_bytes=200_000_000,
            memory_vms_bytes=400_000_000,
            io_read_bytes=None,
            io_write_bytes=None,
            num_children=1,
            num_threads=5,
        )
        mock_collector.samples = deque([sample])
        agent.resource_collector = mock_collector
        pa.agents[800] = agent
        try:
            result = await get_all_agents()
            agent_data = next(a for a in result["agents"] if a["run_id"] == 800)
            assert agent_data["cpu_percent"] == 75.0
            assert agent_data["memory_rss_bytes"] == 200_000_000
        finally:
            del pa.agents[800]

    @pytest.mark.asyncio
    async def test_get_all_agents_null_collector(self) -> None:
        from sova.dashboard.services.agent_lifecycle import get_all_agents
        from sova.dashboard.services.agent_pool import AgentState, _get_project_agents

        pa = _get_project_agents()
        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.returncode = None
        agent = AgentState(run_id=801, issue="61", role="developer", process=mock_process)
        agent.resource_collector = None
        pa.agents[801] = agent
        try:
            result = await get_all_agents()
            agent_data = next(a for a in result["agents"] if a["run_id"] == 801)
            assert agent_data["cpu_percent"] is None
            assert agent_data["memory_rss_bytes"] is None
        finally:
            del pa.agents[801]


class TestSystemMetricsService:
    def test_returns_available_and_system_data(self) -> None:
        from sova.dashboard.services.resource_service import get_system_metrics

        result = get_system_metrics()
        assert result["available"] is True
        assert "system" in result
        assert "agents" in result
        assert "agent_slots" in result

        sys = result["system"]
        assert sys["cpu_count"] is not None
        assert sys["cpu_count"] > 0
        assert sys["memory_total_bytes"] is not None
        assert sys["memory_used_bytes"] is not None
        assert sys["memory_percent"] is not None
        assert sys["cpu_percent"] is not None
        assert isinstance(sys["cpu_percent"], (int, float))

    def test_memory_used_consistent_with_percent(self) -> None:
        """memory_used_bytes must equal total - available, matching percent."""
        from unittest.mock import patch

        from sova.dashboard.services.resource_service import get_system_metrics

        mock_mem = MagicMock()
        mock_mem.total = 16_000_000_000
        mock_mem.available = 4_000_000_000
        mock_mem.used = 5_000_000_000  # macOS "active" only -- should NOT be used
        mock_mem.percent = 75.0

        with patch("sova.dashboard.services.resource_service.psutil.virtual_memory", return_value=mock_mem):
            result = get_system_metrics()

        sys = result["system"]
        assert sys["memory_used_bytes"] == 12_000_000_000  # total - available
        assert sys["memory_used_bytes"] != 5_000_000_000  # not the raw .used field

    def test_agent_slots_empty(self) -> None:
        from sova.dashboard.services.resource_service import get_system_metrics

        result = get_system_metrics()
        assert result["agent_slots"]["used"] >= 0
        assert result["agent_slots"]["max"] > 0
        assert isinstance(result["agents"], list)

    def test_with_running_agent_and_collector(self) -> None:
        from sova.dashboard.services.agent_pool import AgentState, _get_project_agents
        from sova.dashboard.services.resource_service import get_system_metrics
        from sova.monitoring.models import ResourceSample

        pa = _get_project_agents()
        mock_process = MagicMock()
        mock_process.pid = 12345
        agent = AgentState(run_id=900, issue="70", role="developer", process=mock_process)
        mock_collector = MagicMock()
        sample = ResourceSample(
            timestamp=1000.0,
            cpu_percent=50.0,
            memory_rss_bytes=100_000_000,
            memory_vms_bytes=200_000_000,
            io_read_bytes=None,
            io_write_bytes=None,
            num_children=1,
            num_threads=3,
        )
        mock_collector.samples = deque([sample])
        agent.resource_collector = mock_collector
        pa.agents[900] = agent
        try:
            result = get_system_metrics()
            assert result["agent_slots"]["used"] >= 1
            agent_data = next(a for a in result["agents"] if a["run_id"] == 900)
            assert agent_data["cpu_percent"] == 50.0
            assert agent_data["memory_rss_bytes"] == 100_000_000
            assert agent_data["issue"] == "70"
            assert agent_data["role"] == "developer"
        finally:
            del pa.agents[900]

    def test_with_agent_no_collector(self) -> None:
        from sova.dashboard.services.agent_pool import AgentState, _get_project_agents
        from sova.dashboard.services.resource_service import get_system_metrics

        pa = _get_project_agents()
        mock_process = MagicMock()
        mock_process.pid = 12345
        agent = AgentState(run_id=901, issue="71", role="triage", process=mock_process)
        agent.resource_collector = None
        pa.agents[901] = agent
        try:
            result = get_system_metrics()
            agent_data = next(a for a in result["agents"] if a["run_id"] == 901)
            assert agent_data["cpu_percent"] is None
            assert agent_data["memory_rss_bytes"] is None
        finally:
            del pa.agents[901]

    def test_load_avg_present_on_unix(self) -> None:
        from sova.dashboard.services.resource_service import get_system_metrics

        result = get_system_metrics()
        # On macOS/Linux, load_avg should be a list of 3 floats
        if hasattr(os, "getloadavg"):
            assert result["system"]["load_avg"] is not None
            assert len(result["system"]["load_avg"]) == 3

    def test_psutil_unavailable_returns_not_available(self) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.resource_service import get_system_metrics

        target = "sova.dashboard.services.resource_service.psutil.cpu_percent"
        with patch(target, side_effect=RuntimeError("no psutil")):
            result = get_system_metrics()
        assert result["available"] is False

    def test_psutil_access_denied_returns_unavailable(self) -> None:
        from unittest.mock import patch

        import psutil

        from sova.dashboard.services.resource_service import get_system_metrics

        target = "sova.dashboard.services.resource_service.psutil.cpu_percent"
        with patch(target, side_effect=psutil.AccessDenied(pid=1)):
            result = get_system_metrics()
        assert result["available"] is False


class TestSystemMetricsRouter:
    @pytest.mark.asyncio
    async def test_system_metrics_endpoint(self, client: AsyncClient) -> None:
        resp = await client.get("/api/resources/system/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is True
        assert "system" in data
        assert "agents" in data
        assert "agent_slots" in data

    @pytest.mark.asyncio
    async def test_system_metrics_endpoint_500(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        target = "sova.dashboard.routers.resources.resource_service.get_system_metrics"
        with patch(target, side_effect=RuntimeError("oops")):
            resp = await client.get("/api/resources/system/metrics")
        assert resp.status_code == 500
        assert "Failed to fetch system metrics" in resp.json()["detail"]
