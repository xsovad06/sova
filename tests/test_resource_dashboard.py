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
    async def test_system_info_endpoint(self, client: AsyncClient) -> None:
        resp = await client.get("/api/resources/system")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cpu_count"] > 0
        assert data["total_memory_bytes"] > 0


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
