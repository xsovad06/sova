"""Tests for the /oversight dashboard page and API endpoints."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sova.db.models import OversightFinding, OversightRun, OversightRunStatus
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
async def seed_runs(session: AsyncSession):
    """Seed OversightRun + OversightFinding records."""
    now = datetime.now(timezone.utc)
    run1 = OversightRun(
        id="run-1",
        status=OversightRunStatus.DONE,
        cycle_number=1,
        duration_ms=1200,
        started_at=now - timedelta(hours=1),
        ended_at=now - timedelta(hours=1) + timedelta(seconds=1),
    )
    run2 = OversightRun(
        id="run-2",
        status=OversightRunStatus.ERROR,
        cycle_number=2,
        duration_ms=300,
        error="observation_failed",
        started_at=now - timedelta(minutes=10),
        ended_at=now - timedelta(minutes=10) + timedelta(seconds=0.3),
    )
    session.add_all([run1, run2])
    await session.flush()

    f1 = OversightFinding(
        run_id="run-1",
        title="Repeated CI failures on issue #28",
        scope="global",
        severity="warning",
        description="Agent #28 has failed CI 4 times in a row.",
        recommendation="Check test isolation.",
        confidence=0.85,
        dismissed=False,
    )
    f2 = OversightFinding(
        run_id="run-1",
        title="Stale research branch",
        scope="local",
        severity="info",
        description="Branch feat/issue-99 has been idle for 7 days.",
        confidence=0.72,
        project_slug="myproject",
        dismissed=False,
    )
    f3 = OversightFinding(
        run_id="run-1",
        title="Budget exceeded on issue #45",
        scope="global",
        severity="critical",
        confidence=0.91,
        dismissed=True,
    )
    f4 = OversightFinding(
        run_id="run-1",
        title="Already filed finding",
        scope="global",
        severity="info",
        confidence=0.80,
        github_issue_number=99,
        dismissed=False,
    )
    session.add_all([f1, f2, f3, f4])
    await session.commit()
    return {"runs": [run1, run2], "findings": [f1, f2, f3, f4]}


class TestOversightPage:
    async def test_page_loads(self, client: AsyncClient) -> None:
        resp = await client.get("/oversight")
        assert resp.status_code == 200
        assert "Oversight" in resp.text
        assert "Run Now" in resp.text

    async def test_page_has_stat_tiles(self, client: AsyncClient) -> None:
        resp = await client.get("/oversight")
        assert "stat-last-run" in resp.text
        assert "stat-next-wake" in resp.text
        assert "stat-pending" in resp.text
        assert "stat-issues-week" in resp.text

    async def test_page_has_findings_section(self, client: AsyncClient) -> None:
        resp = await client.get("/oversight")
        assert "findings-container" in resp.text
        assert "findings-filter" in resp.text

    async def test_page_has_persona_section(self, client: AsyncClient) -> None:
        resp = await client.get("/oversight")
        assert "Operations Persona" in resp.text
        assert "persona-preview" in resp.text


class TestOversightServiceDirect:
    """Direct service function tests to ensure coverage regardless of ASGI transport."""

    async def test_get_status_empty(self) -> None:
        from sova.dashboard.services.oversight_service import get_status

        async with await get_session() as session:
            result = await get_status(session, enabled=True, agent_running=False, wake_interval_minutes=30)
        assert result["last_run"] is None
        assert result["pending_findings_count"] == 0
        assert result["issues_proposed_this_week"] == 0
        assert result["next_wake_approx"] is None

    async def test_get_status_with_run_and_agent_running(self, session: AsyncSession, seed_runs: dict) -> None:
        from sova.dashboard.services.oversight_service import get_status

        async with await get_session() as session:
            result = await get_status(session, enabled=True, agent_running=True, wake_interval_minutes=60)
        assert result["last_run"] is not None
        assert result["last_run"]["id"] == "run-2"
        assert result["next_wake_approx"] is not None
        assert result["pending_findings_count"] == 2

    async def test_get_runs_direct(self, session: AsyncSession, seed_runs: dict) -> None:
        from sova.dashboard.services.oversight_service import get_runs

        async with await get_session() as session:
            runs = await get_runs(session, limit=10)
        assert len(runs) == 2
        assert runs[0]["findings_count"] >= 0
        assert runs[0]["issues_created"] >= 0

    async def test_get_findings_all_statuses(self, session: AsyncSession, seed_runs: dict) -> None:
        from sova.dashboard.services.oversight_service import get_findings

        async with await get_session() as session:
            pending = await get_findings(session, status="pending")
            created = await get_findings(session, status="created")
            dismissed = await get_findings(session, status="dismissed")
        assert len(pending) == 2
        assert len(created) == 1
        assert len(dismissed) == 1

    async def test_dismiss_finding_direct(self, session: AsyncSession, seed_runs: dict) -> None:
        from sova.dashboard.services.oversight_service import dismiss_finding

        finding_id = seed_runs["findings"][0].id
        async with await get_session() as session:
            async with session.begin():
                result = await dismiss_finding(session, finding_id)
        assert result is not None
        assert result["dismissed"] is True

    async def test_dismiss_finding_not_found(self) -> None:
        from sova.dashboard.services.oversight_service import dismiss_finding

        async with await get_session() as session:
            async with session.begin():
                result = await dismiss_finding(session, 99999)
        assert result is None

    async def test_get_finding_by_id_direct(self, session: AsyncSession, seed_runs: dict) -> None:
        from sova.dashboard.services.oversight_service import get_finding_by_id

        finding_id = seed_runs["findings"][0].id
        async with await get_session() as session:
            found = await get_finding_by_id(session, finding_id)
        assert found is not None
        assert found.id == finding_id

    async def test_update_finding_issue_number_direct(self, session: AsyncSession, seed_runs: dict) -> None:
        from sova.dashboard.services.oversight_service import get_finding_by_id, update_finding_issue_number

        finding_id = seed_runs["findings"][0].id
        async with await get_session() as session:
            async with session.begin():
                await update_finding_issue_number(session, finding_id, 123)
                updated = await get_finding_by_id(session, finding_id)
        assert updated.github_issue_number == 123

    async def test_update_finding_issue_number_not_found(self) -> None:
        from sova.dashboard.services.oversight_service import update_finding_issue_number

        async with await get_session() as session:
            async with session.begin():
                await update_finding_issue_number(session, 99999, 42)


class TestOversightStatus:
    async def test_status_empty_db(self, client: AsyncClient) -> None:
        resp = await client.get("/api/oversight/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["last_run"] is None
        assert data["pending_findings_count"] == 0
        assert data["issues_proposed_this_week"] == 0

    async def test_status_with_data(self, client: AsyncClient, seed_runs: dict) -> None:
        resp = await client.get("/api/oversight/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["last_run"] is not None
        assert data["last_run"]["id"] == "run-2"
        assert data["pending_findings_count"] == 2
        assert data["issues_proposed_this_week"] == 1


class TestOversightRuns:
    async def test_runs_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/oversight/runs")
        assert resp.status_code == 200
        assert resp.json()["runs"] == []

    async def test_runs_with_data(self, client: AsyncClient, seed_runs: dict) -> None:
        resp = await client.get("/api/oversight/runs")
        assert resp.status_code == 200
        runs = resp.json()["runs"]
        assert len(runs) == 2
        assert runs[0]["id"] == "run-2"
        assert runs[0]["status"] == "error"
        assert runs[0]["error"] == "observation_failed"
        assert runs[1]["id"] == "run-1"
        assert runs[1]["findings_count"] == 4
        assert runs[1]["issues_created"] == 1

    async def test_runs_limit(self, client: AsyncClient, seed_runs: dict) -> None:
        resp = await client.get("/api/oversight/runs?limit=1")
        assert resp.status_code == 200
        assert len(resp.json()["runs"]) == 1


class TestOversightFindings:
    async def test_findings_pending(self, client: AsyncClient, seed_runs: dict) -> None:
        resp = await client.get("/api/oversight/findings?status=pending")
        assert resp.status_code == 200
        findings = resp.json()["findings"]
        assert len(findings) == 2
        titles = {f["title"] for f in findings}
        assert "Repeated CI failures on issue #28" in titles
        assert "Stale research branch" in titles

    async def test_findings_created(self, client: AsyncClient, seed_runs: dict) -> None:
        resp = await client.get("/api/oversight/findings?status=created")
        assert resp.status_code == 200
        findings = resp.json()["findings"]
        assert len(findings) == 1
        assert findings[0]["github_issue_number"] == 99

    async def test_findings_dismissed(self, client: AsyncClient, seed_runs: dict) -> None:
        resp = await client.get("/api/oversight/findings?status=dismissed")
        assert resp.status_code == 200
        findings = resp.json()["findings"]
        assert len(findings) == 1
        assert findings[0]["dismissed"] is True

    async def test_findings_default_is_pending(self, client: AsyncClient, seed_runs: dict) -> None:
        resp = await client.get("/api/oversight/findings")
        assert resp.status_code == 200
        findings = resp.json()["findings"]
        assert len(findings) == 2
        assert all(not f["dismissed"] and f["github_issue_number"] is None for f in findings)

    async def test_findings_invalid_status(self, client: AsyncClient) -> None:
        resp = await client.get("/api/oversight/findings?status=invalid")
        assert resp.status_code == 422


class TestDismissFinding:
    async def test_dismiss_success(self, client: AsyncClient, seed_runs: dict) -> None:
        finding_id = seed_runs["findings"][0].id
        resp = await client.post(f"/api/oversight/findings/{finding_id}/dismiss")
        assert resp.status_code == 200
        assert resp.json()["dismissed"] is True

        pending = await client.get("/api/oversight/findings?status=pending")
        assert len(pending.json()["findings"]) == 1

    async def test_dismiss_not_found(self, client: AsyncClient) -> None:
        resp = await client.post("/api/oversight/findings/99999/dismiss")
        assert resp.status_code == 404


class TestCreateIssueFromFinding:
    async def test_create_issue_not_found(self, client: AsyncClient) -> None:
        resp = await client.post("/api/oversight/findings/99999/create-issue")
        assert resp.status_code == 404

    async def test_create_issue_already_has_issue(self, client: AsyncClient, seed_runs: dict) -> None:
        finding_with_issue = seed_runs["findings"][3]
        resp = await client.post(f"/api/oversight/findings/{finding_with_issue.id}/create-issue")
        assert resp.status_code == 409

    async def test_create_issue_dismissed_finding_rejected(self, client: AsyncClient, seed_runs: dict) -> None:
        dismissed_finding = seed_runs["findings"][2]
        resp = await client.post(f"/api/oversight/findings/{dismissed_finding.id}/create-issue")
        assert resp.status_code == 409
        assert "dismissed" in resp.json()["detail"].lower()

    async def test_create_issue_success(self, client: AsyncClient, seed_runs: dict) -> None:
        from sova.adapters.base import TaskState

        finding = seed_runs["findings"][0]

        mock_task = AsyncMock()
        mock_task.id = "42"
        mock_task.state = TaskState.BACKLOG

        mock_adapter = AsyncMock()
        mock_adapter.create_issue = AsyncMock(return_value=mock_task)

        with (
            patch("sova.adapters.create_adapter", return_value=mock_adapter),
            patch("sova.config.loader.load_config"),
        ):
            resp = await client.post(f"/api/oversight/findings/{finding.id}/create-issue")
            assert resp.status_code == 200
            data = resp.json()
            assert data["issue_number"] == 42
            assert data["finding_id"] == finding.id

        created = await client.get("/api/oversight/findings?status=created")
        titles = {f["title"] for f in created.json()["findings"]}
        assert finding.title in titles


class TestRunNow:
    async def test_run_now_no_agent(self, client: AsyncClient) -> None:
        resp = await client.post("/api/oversight/run-now")
        assert resp.status_code == 404

    async def test_run_now_with_agent(self, client: AsyncClient) -> None:
        from sova.dashboard.routers.oversight import _background_tasks, set_oversight_agent

        mock_agent = AsyncMock()
        mock_agent.running = True
        mock_agent.run_cycle_once = AsyncMock()
        set_oversight_agent(mock_agent)

        try:
            resp = await client.post("/api/oversight/run-now")
            assert resp.status_code == 202
            assert resp.json()["status"] == "accepted"
        finally:
            for t in list(_background_tasks):
                t.cancel()
            _background_tasks.clear()
            set_oversight_agent(None)

    async def test_run_now_rejects_while_pending(self, client: AsyncClient) -> None:
        from sova.dashboard.routers.oversight import _background_tasks, set_oversight_agent

        pending_future: asyncio.Future = asyncio.get_event_loop().create_future()
        mock_agent = AsyncMock()
        mock_agent.running = True
        mock_agent.run_cycle_once = AsyncMock(side_effect=lambda: pending_future)
        set_oversight_agent(mock_agent)

        try:
            resp1 = await client.post("/api/oversight/run-now")
            assert resp1.status_code == 202

            resp2 = await client.post("/api/oversight/run-now")
            assert resp2.status_code == 409
            assert "already pending" in resp2.json()["detail"].lower()
        finally:
            pending_future.set_result(None)
            for t in list(_background_tasks):
                t.cancel()
            _background_tasks.clear()
            set_oversight_agent(None)


class TestCancelRunNowTasks:
    async def test_cancel_run_now_tasks_empty(self) -> None:
        from sova.dashboard.routers.oversight import cancel_run_now_tasks

        await cancel_run_now_tasks()

    async def test_cancel_run_now_tasks_with_pending(self) -> None:
        from sova.dashboard.routers.oversight import _background_tasks, cancel_run_now_tasks

        async def _hang() -> None:
            await asyncio.sleep(3600)

        task = asyncio.create_task(_hang())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

        await cancel_run_now_tasks()
        assert len(_background_tasks) == 0
        assert task.cancelled()


class TestStatusNextWake:
    async def test_status_next_wake_when_running(self, client: AsyncClient, seed_runs: dict) -> None:
        from sova.dashboard.routers.oversight import set_oversight_agent

        mock_agent = AsyncMock()
        mock_agent.running = True
        set_oversight_agent(mock_agent)

        try:
            resp = await client.get("/api/oversight/status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["running"] is True
            assert data["next_wake_approx"] is not None
        finally:
            set_oversight_agent(None)


class TestRouterErrorPaths:
    async def test_status_db_error(self, client: AsyncClient) -> None:
        with patch("sova.dashboard.services.oversight_service.get_status", side_effect=RuntimeError("db fail")):
            resp = await client.get("/api/oversight/status")
            assert resp.status_code == 500

    async def test_status_config_load_error(self, client: AsyncClient) -> None:
        with patch("sova.config.loader.load_config", side_effect=RuntimeError("bad config")):
            resp = await client.get("/api/oversight/status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["enabled"] is False

    async def test_runs_db_error(self, client: AsyncClient) -> None:
        with patch("sova.dashboard.services.oversight_service.get_runs", side_effect=RuntimeError("db fail")):
            resp = await client.get("/api/oversight/runs")
            assert resp.status_code == 500

    async def test_findings_db_error(self, client: AsyncClient) -> None:
        with patch("sova.dashboard.services.oversight_service.get_findings", side_effect=RuntimeError("db fail")):
            resp = await client.get("/api/oversight/findings")
            assert resp.status_code == 500

    async def test_dismiss_db_error(self, client: AsyncClient, seed_runs: dict) -> None:
        finding_id = seed_runs["findings"][0].id
        with patch("sova.dashboard.services.oversight_service.dismiss_finding", side_effect=RuntimeError("db fail")):
            resp = await client.post(f"/api/oversight/findings/{finding_id}/dismiss")
            assert resp.status_code == 500

    async def test_create_issue_adapter_error(self, client: AsyncClient, seed_runs: dict) -> None:
        finding = seed_runs["findings"][0]
        with (
            patch("sova.config.loader.load_config"),
            patch("sova.adapters.create_adapter", side_effect=RuntimeError("adapter fail")),
        ):
            resp = await client.post(f"/api/oversight/findings/{finding.id}/create-issue")
            assert resp.status_code == 500


class TestRunCycleOnce:
    async def test_run_cycle_once_records_run(self) -> None:
        from sova.config.models import OversightConfig
        from sova.oversight.agent import OversightAgent

        agent = OversightAgent(config=OversightConfig(enabled=True, wake_interval_minutes=1))

        with (
            patch.object(agent, "_observe", new_callable=AsyncMock, return_value={"test": True}),
            patch.object(agent, "_analyze", new_callable=AsyncMock, return_value=([], None)),
        ):
            await agent.run_cycle_once()

        assert agent._cycle_number == 1

        async with await get_session() as session:
            from sqlalchemy import select

            result = await session.execute(select(OversightRun))
            runs = result.scalars().all()
            assert len(runs) == 1
            assert runs[0].status == OversightRunStatus.DONE
            assert runs[0].cycle_number == 1

    async def test_run_cycle_once_observation_failure(self) -> None:
        from sova.config.models import OversightConfig
        from sova.oversight.agent import OversightAgent

        agent = OversightAgent(config=OversightConfig(enabled=True, wake_interval_minutes=1))

        with patch.object(agent, "_observe", new_callable=AsyncMock, return_value=None):
            await agent.run_cycle_once()

        async with await get_session() as session:
            from sqlalchemy import select

            runs = (await session.execute(select(OversightRun))).scalars().all()
            assert len(runs) == 1
            assert runs[0].status == OversightRunStatus.ERROR
            assert runs[0].error == "observation_failed"

    async def test_run_cycle_once_analysis_error(self) -> None:
        from sova.config.models import OversightConfig
        from sova.oversight.agent import OversightAgent

        agent = OversightAgent(config=OversightConfig(enabled=True, wake_interval_minutes=1))

        with (
            patch.object(agent, "_observe", new_callable=AsyncMock, return_value={"data": True}),
            patch.object(agent, "_analyze", new_callable=AsyncMock, return_value=([], "llm_timeout")),
        ):
            await agent.run_cycle_once()

        async with await get_session() as session:
            from sqlalchemy import select

            runs = (await session.execute(select(OversightRun))).scalars().all()
            assert len(runs) == 1
            assert runs[0].status == OversightRunStatus.ERROR
            assert runs[0].error == "llm_timeout"

    async def test_run_cycle_once_exception_records_error(self) -> None:
        from sova.config.models import OversightConfig
        from sova.oversight.agent import OversightAgent

        agent = OversightAgent(config=OversightConfig(enabled=True, wake_interval_minutes=1))

        with patch.object(agent, "_observe", new_callable=AsyncMock, side_effect=ValueError("boom")):
            await agent.run_cycle_once()

        async with await get_session() as session:
            from sqlalchemy import select

            runs = (await session.execute(select(OversightRun))).scalars().all()
            assert len(runs) == 1
            assert runs[0].status == OversightRunStatus.ERROR
            assert "boom" in runs[0].error

    async def test_run_cycle_once_with_findings_calls_propose(self) -> None:
        from sova.config.models import OversightConfig
        from sova.oversight.agent import OversightAgent

        agent = OversightAgent(config=OversightConfig(enabled=True, wake_interval_minutes=1))
        mock_finding = MagicMock()

        with (
            patch.object(agent, "_observe", new_callable=AsyncMock, return_value={"data": True}),
            patch.object(agent, "_analyze", new_callable=AsyncMock, return_value=([mock_finding], None)),
            patch.object(agent, "_propose_issues", new_callable=AsyncMock) as mock_propose,
        ):
            await agent.run_cycle_once()

        mock_propose.assert_called_once_with([mock_finding])

    async def test_start_and_stop(self) -> None:
        from sova.config.models import OversightConfig
        from sova.oversight.agent import OversightAgent

        agent = OversightAgent(config=OversightConfig(enabled=True, wake_interval_minutes=1))

        with (
            patch.object(agent, "_observe", new_callable=AsyncMock, return_value=None),
        ):
            agent.start()
            assert agent.running is True
            await asyncio.sleep(0.05)
            await agent.stop()
            assert agent.running is False

    async def test_get_system_prompt_empty(self) -> None:
        from sova.config.models import OversightConfig
        from sova.oversight.agent import OversightAgent

        agent = OversightAgent(config=OversightConfig(enabled=True))
        assert agent.get_system_prompt() == ""

    async def test_get_system_prompt_with_persona(self) -> None:
        from sova.config.models import OversightConfig
        from sova.oversight.agent import OversightAgent

        agent = OversightAgent(config=OversightConfig(enabled=True))
        agent._persona = "Monitor CI failures"
        prompt = agent.get_system_prompt()
        assert "Monitor CI failures" in prompt
        assert "Operations Persona" in prompt

    async def test_determine_outcome_variants(self) -> None:
        from sova.oversight.agent import OversightAgent

        status, error = OversightAgent._determine_outcome(None, None)
        assert status == OversightRunStatus.ERROR
        assert error == "observation_failed"

        status, error = OversightAgent._determine_outcome({"data": True}, "llm_fail")
        assert status == OversightRunStatus.ERROR
        assert error == "llm_fail"

        status, error = OversightAgent._determine_outcome({"data": True}, None)
        assert status == OversightRunStatus.DONE
        assert error is None

    async def test_running_property(self) -> None:
        from sova.config.models import OversightConfig
        from sova.oversight.agent import OversightAgent

        agent = OversightAgent(config=OversightConfig(enabled=True))
        assert agent.running is False

        mock_task = MagicMock()
        mock_task.done.return_value = False
        agent._task = mock_task
        assert agent.running is True

        mock_task.done.return_value = True
        assert agent.running is False
