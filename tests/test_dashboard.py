"""Tests for sova.dashboard -- FastAPI dashboard with DB-backed queries."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sova.db.models import CostRecord, Memory, StepExecution, TaskRun
from sova.db.session import close_db, get_session, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    """Initialize an in-memory DB for dashboard tests."""
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


@pytest.fixture
async def session() -> AsyncSession:
    return await get_session()


@pytest.fixture
async def seed_data(session: AsyncSession):
    """Populate the DB with test data."""
    now = datetime.now(timezone.utc)

    # Task runs
    run1 = TaskRun(
        issue_number="42",
        role="developer",
        status="done",
        current_step="complete",
        branch_name="feat/login",
        total_cost_usd=Decimal("1.50"),
        project_slug="myproject",
        started_at=now - timedelta(hours=2),
        ended_at=now - timedelta(hours=1),
    )
    run2 = TaskRun(
        issue_number="43",
        role="triage",
        status="failed",
        current_step="develop",
        branch_name="feat/signup",
        total_cost_usd=Decimal("0.75"),
        error_message="Tests failed",
        project_slug="myproject",
        started_at=now - timedelta(hours=1),
        ended_at=now - timedelta(minutes=30),
    )
    run3 = TaskRun(
        issue_number="44",
        role="developer",
        status="developing",
        current_step="develop",
        branch_name="feat/dashboard",
        total_cost_usd=Decimal("0.25"),
        project_slug="myproject",
        started_at=now - timedelta(minutes=10),
    )
    session.add_all([run1, run2, run3])
    await session.flush()

    # Step executions for run1
    steps = [
        StepExecution(
            task_run_id=run1.id,
            step_name="sync",
            status="done",
            cost_usd=Decimal("0.10"),
            duration_ms=5000,
            started_at=now - timedelta(hours=2),
            ended_at=now - timedelta(hours=2) + timedelta(seconds=5),
        ),
        StepExecution(
            task_run_id=run1.id,
            step_name="develop",
            status="done",
            cost_usd=Decimal("1.20"),
            duration_ms=300000,
            started_at=now - timedelta(hours=2) + timedelta(seconds=5),
            ended_at=now - timedelta(hours=2) + timedelta(minutes=5, seconds=5),
        ),
    ]
    session.add_all(steps)

    # Cost records
    costs = [
        CostRecord(
            task_run_id=run1.id,
            phase="develop",
            issue="42",
            model="opus",
            input_tokens=10000,
            output_tokens=5000,
            cost_usd=Decimal("0.80"),
            duration_ms=15000,
            recorded_at=now - timedelta(hours=1, minutes=30),
        ),
        CostRecord(
            task_run_id=run1.id,
            phase="simplify",
            issue="42",
            model="sonnet",
            input_tokens=5000,
            output_tokens=2000,
            cost_usd=Decimal("0.30"),
            duration_ms=8000,
            recorded_at=now - timedelta(hours=1, minutes=15),
        ),
        CostRecord(
            task_run_id=run2.id,
            phase="develop",
            issue="43",
            model="opus",
            input_tokens=8000,
            output_tokens=4000,
            cost_usd=Decimal("0.60"),
            duration_ms=12000,
            recorded_at=now - timedelta(minutes=45),
        ),
    ]
    session.add_all(costs)

    # Memory entries
    mem = Memory(
        category="learning",
        title="Test pattern",
        content="Always use fixtures for test data",
        tags="testing,pytest",
        tier="project",
    )
    session.add(mem)

    await session.commit()
    return {"runs": [run1, run2, run3], "costs": costs}


@pytest.fixture
async def client():
    from sova.dashboard.app import create_app

    app = create_app(multi_project=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Health / smoke tests
# ---------------------------------------------------------------------------


class TestDashboardHealth:
    async def test_root_redirects_to_dashboard(self, client: AsyncClient) -> None:
        resp = await client.get("/", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/dashboard"

    async def test_dashboard_page_loads(self, client: AsyncClient) -> None:
        resp = await client.get("/dashboard")
        assert resp.status_code == 200
        assert "Dashboard" in resp.text

    async def test_agents_page_loads(self, client: AsyncClient) -> None:
        resp = await client.get("/agents")
        assert resp.status_code == 200
        assert "Agents" in resp.text

    async def test_work_redirects_to_agents(self, client: AsyncClient) -> None:
        resp = await client.get("/work", follow_redirects=False)
        assert resp.status_code == 301
        assert resp.headers["location"] == "/agents"

    async def test_work_detail_renders_with_agents_sidebar(self, client: AsyncClient) -> None:
        resp = await client.get("/work/1")
        assert resp.status_code == 200
        assert "Run #1" in resp.text

    async def test_lifecycle_renders_with_agents_sidebar(self, client: AsyncClient) -> None:
        resp = await client.get("/lifecycle/42")
        assert resp.status_code == 200
        assert "Issue " in resp.text and "#42" in resp.text

    async def test_costs_page_loads(self, client: AsyncClient) -> None:
        resp = await client.get("/costs")
        assert resp.status_code == 200
        assert "Cost" in resp.text

    async def test_overview_redirects_to_dashboard(self, client: AsyncClient) -> None:
        resp = await client.get("/overview", follow_redirects=False)
        assert resp.status_code == 301
        assert resp.headers["location"] == "/dashboard"

    async def test_control_redirects_to_agents(self, client: AsyncClient) -> None:
        resp = await client.get("/control", follow_redirects=False)
        assert resp.status_code == 301
        assert resp.headers["location"] == "/agents"

    async def test_runs_redirects_to_agents(self, client: AsyncClient) -> None:
        resp = await client.get("/runs", follow_redirects=False)
        assert resp.status_code == 301
        assert resp.headers["location"] == "/agents"

    async def test_tasks_redirects_to_agents(self, client: AsyncClient) -> None:
        resp = await client.get("/tasks", follow_redirects=False)
        assert resp.status_code == 301
        assert resp.headers["location"] == "/agents"

    async def test_memory_page_loads(self, client: AsyncClient) -> None:
        resp = await client.get("/memory")
        assert resp.status_code == 200
        assert "Memory" in resp.text

    async def test_static_css_returns_200(self, client: AsyncClient) -> None:
        resp = await client.get("/static/style.css")
        assert resp.status_code == 200

    async def test_static_js_returns_200(self, client: AsyncClient) -> None:
        resp = await client.get("/static/app.js")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Overview API
# ---------------------------------------------------------------------------


class TestOverviewAPI:
    async def test_overview_api_empty_db(self, client: AsyncClient) -> None:
        resp = await client.get("/api/overview")
        assert resp.status_code == 200
        data = resp.json()
        assert "runs" in data
        assert "costs" in data
        assert "memory_count" in data
        assert data["runs"]["total"] == 0
        assert data["costs"]["total_cost_usd"] == 0

    async def test_overview_api_with_data(self, client: AsyncClient, seed_data) -> None:
        resp = await client.get("/api/overview")
        assert resp.status_code == 200
        data = resp.json()
        assert data["runs"]["total"] == 3
        assert data["runs"]["active"] == 1
        assert data["runs"]["done"] == 1
        assert data["runs"]["failed"] == 1
        assert data["costs"]["total_cost_usd"] > 0
        assert data["memory_count"] == 1


# ---------------------------------------------------------------------------
# Runs API
# ---------------------------------------------------------------------------


class TestRunsAPI:
    async def test_list_runs_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/runs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["runs"] == []

    async def test_list_runs_with_data(self, client: AsyncClient, seed_data) -> None:
        resp = await client.get("/api/runs")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["runs"]) == 3
        # Most recent first
        assert data["runs"][0]["issue_number"] == "44"

    async def test_list_runs_limit(self, client: AsyncClient, seed_data) -> None:
        resp = await client.get("/api/runs?limit=1")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["runs"]) == 1

    async def test_list_runs_filter_status(self, client: AsyncClient, seed_data) -> None:
        resp = await client.get("/api/runs?status=done")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["runs"]) == 1
        assert data["runs"][0]["status"] == "done"

    async def test_get_run_detail(self, client: AsyncClient, seed_data) -> None:
        runs_resp = await client.get("/api/runs")
        run_id = runs_resp.json()["runs"][-1]["id"]  # run1 (oldest)
        resp = await client.get(f"/api/runs/{run_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["run"]["issue_number"] == "42"
        assert len(data["steps"]) == 2
        assert data["steps"][0]["step_name"] == "sync"
        assert data["steps"][0]["retry_count"] == 0

    async def test_get_run_detail_with_retries(self, client: AsyncClient, session: AsyncSession) -> None:
        """Retried steps: API returns only the final attempt per step_name with retry_count."""
        now = datetime.now(timezone.utc)
        run = TaskRun(
            issue_number="99",
            role="developer",
            status="done",
            current_step="complete",
            branch_name="feat/retry-test",
            total_cost_usd=Decimal("0.50"),
            project_slug="myproject",
            started_at=now - timedelta(hours=1),
            ended_at=now,
        )
        session.add(run)
        await session.flush()

        # Step "develop" failed once (retry_count=0), then succeeded (retry_count=1)
        step_fail = StepExecution(
            task_run_id=run.id,
            step_name="develop",
            status="failed",
            retry_count=0,
            cost_usd=Decimal("0.10"),
            duration_ms=5000,
            error_message="Tests failed",
            started_at=now - timedelta(minutes=30),
            ended_at=now - timedelta(minutes=25),
        )
        step_ok = StepExecution(
            task_run_id=run.id,
            step_name="develop",
            status="done",
            retry_count=1,
            cost_usd=Decimal("0.30"),
            duration_ms=10000,
            started_at=now - timedelta(minutes=25),
            ended_at=now - timedelta(minutes=15),
        )
        session.add_all([step_fail, step_ok])
        await session.commit()

        resp = await client.get(f"/api/runs/{run.id}")
        assert resp.status_code == 200
        data = resp.json()
        # Only the final attempt should appear
        assert len(data["steps"]) == 1
        assert data["steps"][0]["step_name"] == "develop"
        assert data["steps"][0]["retry_count"] == 1
        assert data["steps"][0]["status"] == "done"
        assert data["steps"][0]["error_message"] is None

    async def test_get_run_detail_default_retry_count(self, client: AsyncClient, session: AsyncSession) -> None:
        """Steps created without explicit retry_count should serialize as 0."""
        now = datetime.now(timezone.utc)
        run = TaskRun(
            issue_number="100",
            role="developer",
            status="done",
            current_step="complete",
            branch_name="feat/retry-default-test",
            total_cost_usd=Decimal("0.10"),
            project_slug="myproject",
            started_at=now - timedelta(minutes=20),
            ended_at=now,
        )
        session.add(run)
        await session.flush()

        step = StepExecution(
            task_run_id=run.id,
            step_name="develop",
            status="done",
            cost_usd=Decimal("0.10"),
            duration_ms=1000,
            started_at=now - timedelta(minutes=10),
            ended_at=now - timedelta(minutes=5),
        )
        session.add(step)
        await session.commit()

        resp = await client.get(f"/api/runs/{run.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["steps"][0]["retry_count"] == 0

    async def test_get_run_not_found(self, client: AsyncClient) -> None:
        resp = await client.get("/api/runs/999")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Costs API
# ---------------------------------------------------------------------------


class TestCostsAPI:
    async def test_costs_summary_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/costs/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_cost_usd"] == 0
        assert data["total_invocations"] == 0

    async def test_costs_summary_with_data(self, client: AsyncClient, seed_data) -> None:
        resp = await client.get("/api/costs/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_cost_usd"] > 0
        assert data["total_invocations"] == 3

    async def test_costs_daily(self, client: AsyncClient, seed_data) -> None:
        resp = await client.get("/api/costs/daily")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        assert "date" in data[0]
        assert "cost_usd" in data[0]

    async def test_costs_by_issue(self, client: AsyncClient, seed_data) -> None:
        resp = await client.get("/api/costs/by-issue")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 2  # issues 42 and 43
        # Highest cost first
        assert data[0]["issue"] == "42"

    async def test_costs_by_phase(self, client: AsyncClient, seed_data) -> None:
        resp = await client.get("/api/costs/by-phase")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert any(p["phase"] == "develop" for p in data)

    async def test_costs_by_model(self, client: AsyncClient, seed_data) -> None:
        resp = await client.get("/api/costs/by-model")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert any(m["model"] == "opus" for m in data)


# ---------------------------------------------------------------------------
# Control API
# ---------------------------------------------------------------------------


class TestControlAPI:
    async def test_agent_status_idle(self, client: AsyncClient) -> None:
        resp = await client.get("/api/control/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "idle"
        assert data["running"] is False

    async def test_agent_output_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/control/output?since=0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["lines"] == []

    async def test_interrupted_runs_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/control/interrupted")
        assert resp.status_code == 200
        data = resp.json()
        assert data["interrupted"] == []

    async def test_interrupted_runs_returns_interrupted(self, client: AsyncClient, session: AsyncSession) -> None:
        """Interrupted TaskRuns should appear in the interrupted endpoint."""
        async with session.begin():
            run = TaskRun(
                issue_number="67",
                role="developer",
                status="interrupted",
                pid=99999,
                error_message="Dashboard restarted while agent was running",
            )
            session.add(run)
        resp = await client.get("/api/control/interrupted")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["interrupted"]) == 1
        assert data["interrupted"][0]["issue_number"] == "67"

    async def test_interrupted_runs_excludes_other_statuses(self, client: AsyncClient, session: AsyncSession) -> None:
        async with session.begin():
            session.add(TaskRun(issue_number="1", role="dev", status="done"))
            session.add(TaskRun(issue_number="2", role="dev", status="failed"))
            session.add(TaskRun(issue_number="3", role="dev", status="running"))
        resp = await client.get("/api/control/interrupted")
        assert resp.status_code == 200
        assert len(resp.json()["interrupted"]) == 0

    async def test_dismiss_interrupted_marks_as_failed(self, client: AsyncClient, session: AsyncSession) -> None:
        """Dismissing interrupted runs should change their status to failed."""
        async with session.begin():
            session.add(TaskRun(issue_number="73", role="developer", status="interrupted", pid=99999))
            session.add(TaskRun(issue_number="73", role="developer", status="interrupted", pid=99998))
            session.add(TaskRun(issue_number="42", role="auto", status="done"))

        resp = await client.post("/api/agents/interrupted/dismiss")
        assert resp.status_code == 200
        data = resp.json()
        assert data["dismissed"] == 2

        resp2 = await client.get("/api/agents/interrupted")
        assert resp2.json()["interrupted"] == []


# ---------------------------------------------------------------------------
# Control Service -- recovery and normalization
# ---------------------------------------------------------------------------


class TestControlServiceRecovery:
    async def test_recover_stale_runs_marks_dead_processes(self) -> None:
        """Runs with dead PIDs should be marked as interrupted."""
        from sova.dashboard.services.control_service import recover_stale_runs

        # Use get_session so data and recovery share the same connection
        session = await get_session()
        async with session.begin():
            run = TaskRun(
                issue_number="99",
                role="developer",
                status="running",
                pid=999999,  # PID that doesn't exist
            )
            session.add(run)
            await session.flush()
            run_id = run.id

        interrupted = await recover_stale_runs()

        assert len(interrupted) == 1
        assert interrupted[0]["issue"] == "99"
        assert interrupted[0]["run_id"] == run_id

        # Verify DB was updated (same session factory)
        session2 = await get_session()
        async with session2.begin():
            updated = await session2.get(TaskRun, run_id)
            assert updated.status == "interrupted"
            assert updated.ended_at is not None

    async def test_recover_stale_runs_no_stale(self) -> None:
        """No running TaskRuns means nothing to recover."""
        from sova.dashboard.services.control_service import recover_stale_runs

        session = await get_session()
        async with session.begin():
            session.add(TaskRun(issue_number="1", role="dev", status="done"))

        interrupted = await recover_stale_runs()
        assert interrupted == []

    async def test_recover_stale_runs_no_pid(self) -> None:
        """Runs without a PID (legacy) should also be marked interrupted."""
        from sova.dashboard.services.control_service import recover_stale_runs

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="50", role="auto", status="running", pid=None)
            session.add(run)
            await session.flush()
            run_id = run.id

        interrupted = await recover_stale_runs()

        assert len(interrupted) == 1

        session2 = await get_session()
        async with session2.begin():
            updated = await session2.get(TaskRun, run_id)
            assert updated.status == "interrupted"

    async def test_is_process_alive(self) -> None:
        """Process liveness check should work for known PIDs."""
        import os

        from sova.dashboard.services.control_service import _is_process_alive

        # Current process is alive
        assert _is_process_alive(os.getpid()) is True
        # Non-existent PID
        assert _is_process_alive(999999) is False


# ---------------------------------------------------------------------------
# Recovery -- merge-role runs check PR merged
# ---------------------------------------------------------------------------


class TestRecoveryMergeCheck:
    async def test_recover_merge_role_checks_pr_merged(self) -> None:
        """Merge-role runs should be marked 'done' if PR was actually merged."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.control_service import recover_stale_runs

        session = await get_session()
        async with session.begin():
            run = TaskRun(
                issue_number="113",
                role="command:integrate-pr",
                status="running",
                pid=999999,
                pr_number=130,
            )
            session.add(run)
            await session.flush()
            run_id = run.id

        with patch(
            "sova.dashboard.services.agent_lifecycle._check_pr_merged_on_failure",
            new_callable=AsyncMock,
            return_value=True,
        ):
            interrupted = await recover_stale_runs()

        assert len(interrupted) == 0

        session2 = await get_session()
        async with session2.begin():
            updated = await session2.get(TaskRun, run_id)
            assert updated.status == "done"
            assert "merged successfully" in updated.error_message

    async def test_recover_merge_role_not_merged_stays_interrupted(self) -> None:
        """Merge-role runs where PR was NOT merged stay interrupted."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.control_service import recover_stale_runs

        session = await get_session()
        async with session.begin():
            run = TaskRun(
                issue_number="113",
                role="command:integrate-pr",
                status="running",
                pid=999999,
                pr_number=130,
            )
            session.add(run)
            await session.flush()
            run_id = run.id

        with patch(
            "sova.dashboard.services.agent_lifecycle._check_pr_merged_on_failure",
            new_callable=AsyncMock,
            return_value=False,
        ):
            interrupted = await recover_stale_runs()

        assert len(interrupted) == 1

        session2 = await get_session()
        async with session2.begin():
            updated = await session2.get(TaskRun, run_id)
            assert updated.status == "interrupted"

    async def test_recover_non_merge_role_ignores_pr(self) -> None:
        """Non-merge roles should not check PR status even if pr_number is set."""
        from sova.dashboard.services.control_service import recover_stale_runs

        session = await get_session()
        async with session.begin():
            run = TaskRun(
                issue_number="42",
                role="developer",
                status="running",
                pid=999999,
                pr_number=130,
            )
            session.add(run)
            await session.flush()
            run_id = run.id

        interrupted = await recover_stale_runs()

        assert len(interrupted) == 1

        session2 = await get_session()
        async with session2.begin():
            updated = await session2.get(TaskRun, run_id)
            assert updated.status == "interrupted"


# ---------------------------------------------------------------------------
# Auto-handoff -- issue mismatch guard
# ---------------------------------------------------------------------------


class TestAutoHandoffIssueMismatch:
    async def test_skips_when_handoff_issue_mismatches(self) -> None:
        """Auto-handoff should not execute if handoff is for a different issue."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        agent = type(
            "AgentState",
            (),
            {"run_id": 1, "issue": "114", "project_dir": Path("/tmp/test")},
        )()

        handoff = DashboardHandoff(
            source="developer",
            status="awaiting_action",
            issue="113",
            summary="test",
            next_actions=[
                HandoffAction(id="review", label="Review", auto_execute=True, mode="agent"),
            ],
        )

        mock_lifecycle = AsyncMock()
        mock_clear = MagicMock()
        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_lifecycle.start_agent),
            patch("sova.dashboard.services.handoff_service.clear_handoff", mock_clear),
        ):
            await _process_auto_handoff(agent)

        mock_lifecycle.start_agent.assert_not_awaited()
        mock_clear.assert_not_called()

    async def test_executes_when_handoff_issue_matches(self) -> None:
        """Auto-handoff should execute normally when issues match."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        agent = type(
            "AgentState",
            (),
            {"run_id": 1, "issue": "113", "project_dir": Path("/tmp/test")},
        )()

        handoff = DashboardHandoff(
            source="developer",
            status="awaiting_action",
            issue="113",
            pr_number=130,
            summary="test",
            next_actions=[
                HandoffAction(
                    id="review",
                    label="Review",
                    auto_execute=True,
                    mode="agent",
                    args={"issue": "113", "role": "reviewer"},
                ),
            ],
        )

        mock_start = AsyncMock(return_value={"run_id": 2})
        mock_clear = MagicMock()
        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.dashboard.services.handoff_service.clear_handoff", mock_clear),
        ):
            await _process_auto_handoff(agent)

        mock_start.assert_awaited_once()
        mock_clear.assert_called_once()
        assert mock_clear.call_args[1].get("issue") == "113"


# ---------------------------------------------------------------------------
# Auto-handoff circuit breaker
# ---------------------------------------------------------------------------


class TestAutoHandoffCircuitBreaker:
    async def test_blocks_after_max_cycles(self) -> None:
        """Circuit breaker should block auto address-review after max cycles."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.db.session import get_session as real_get_session
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        # Seed 2 completed address-review runs for issue 115, PR 130
        # Each needs an "address_review" StepExecution to be counted
        async with await get_session() as session:
            async with session.begin():
                r1 = TaskRun(issue_number="115", role="developer", status="done", pr_number=130)
                r2 = TaskRun(issue_number="115", role="developer", status="done", pr_number=130)
                session.add_all([r1, r2])
            await session.flush()
            async with session.begin():
                session.add(StepExecution(task_run_id=r1.id, step_name="address_review", status="done"))
                session.add(StepExecution(task_run_id=r2.id, step_name="address_review", status="done"))

        agent = type(
            "AgentState",
            (),
            {"run_id": 10, "issue": "115", "project_dir": Path("/tmp/test")},
        )()

        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue="115",
            pr_number=130,
            summary="Findings to address",
            next_actions=[
                HandoffAction(
                    id="address",
                    label="Address Review",
                    auto_execute=True,
                    mode="agent",
                    args={"issue": "115", "role": "developer", "pr": 130},
                ),
            ],
        )

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        mock_start = AsyncMock()
        mock_clear = MagicMock()
        mock_write = MagicMock()
        mock_cfg = MagicMock()
        mock_cfg.pipeline.max_address_review_cycles = 2
        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.dashboard.services.handoff_service.clear_handoff", mock_clear),
            patch("sova.ipc.handoff.write_handoff_file", mock_write),
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch("sova.db.session.get_session", side_effect=_ignore_project_dir),
        ):
            await _process_auto_handoff(agent)

        # Agent should NOT be spawned
        mock_start.assert_not_awaited()
        # Blocked handoff should be written with manual-only actions
        mock_write.assert_called_once()
        blocked = mock_write.call_args[0][1]
        assert blocked.source == "circuit_breaker"
        assert all(not a.auto_execute for a in blocked.next_actions)
        assert "Circuit breaker" in blocked.summary

    async def test_allows_under_limit(self) -> None:
        """Circuit breaker should allow address-review when under the limit."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.db.session import get_session as real_get_session
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        # Seed only 1 completed address-review run (limit is 2)
        async with await get_session() as session:
            async with session.begin():
                r1 = TaskRun(issue_number="116", role="developer", status="done", pr_number=131)
                session.add(r1)
            await session.flush()
            async with session.begin():
                session.add(StepExecution(task_run_id=r1.id, step_name="address_review", status="done"))

        agent = type(
            "AgentState",
            (),
            {"run_id": 11, "issue": "116", "project_dir": Path("/tmp/test")},
        )()

        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue="116",
            pr_number=131,
            summary="Findings to address",
            next_actions=[
                HandoffAction(
                    id="address",
                    label="Address Review",
                    auto_execute=True,
                    mode="agent",
                    args={"issue": "116", "role": "developer", "pr": 131},
                ),
            ],
        )

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        mock_start = AsyncMock(return_value={"run_id": 12})
        mock_clear = MagicMock()
        mock_cfg = MagicMock()
        mock_cfg.pipeline.max_address_review_cycles = 2
        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.dashboard.services.handoff_service.clear_handoff", mock_clear),
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch("sova.db.session.get_session", side_effect=_ignore_project_dir),
        ):
            await _process_auto_handoff(agent)

        mock_start.assert_awaited_once()

    async def test_skips_when_pr_number_is_none(self) -> None:
        """Circuit breaker should not fire when pr_number is None."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        agent = type(
            "AgentState",
            (),
            {"run_id": 13, "issue": "117", "project_dir": Path("/tmp/test")},
        )()

        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue="117",
            summary="Findings",
            next_actions=[
                HandoffAction(
                    id="address",
                    label="Address Review",
                    auto_execute=True,
                    mode="agent",
                    args={"issue": "117", "role": "developer"},
                ),
            ],
        )

        mock_start = AsyncMock(return_value={"run_id": 14})
        mock_clear = MagicMock()
        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.dashboard.services.handoff_service.clear_handoff", mock_clear),
        ):
            await _process_auto_handoff(agent)

        # Should proceed without checking circuit breaker
        mock_start.assert_awaited_once()

    async def test_skips_for_non_developer_role(self) -> None:
        """Circuit breaker should not fire for non-developer roles (e.g., reviewer)."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        agent = type(
            "AgentState",
            (),
            {"run_id": 15, "issue": "118", "project_dir": Path("/tmp/test")},
        )()

        handoff = DashboardHandoff(
            source="developer",
            status="awaiting_action",
            issue="118",
            pr_number=132,
            summary="Ready for review",
            next_actions=[
                HandoffAction(
                    id="review",
                    label="Review",
                    auto_execute=True,
                    mode="agent",
                    args={"issue": "118", "role": "reviewer", "pr": 132},
                ),
            ],
        )

        mock_start = AsyncMock(return_value={"run_id": 16})
        mock_clear = MagicMock()
        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.dashboard.services.handoff_service.clear_handoff", mock_clear),
        ):
            await _process_auto_handoff(agent)

        mock_start.assert_awaited_once()

    async def test_zero_limit_disables_breaker(self) -> None:
        """Setting max_address_review_cycles=0 should disable the circuit breaker."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.db.session import get_session as real_get_session
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        # Seed many completed address-review runs
        async with await get_session() as session:
            runs = []
            async with session.begin():
                for _ in range(10):
                    r = TaskRun(issue_number="119", role="developer", status="done", pr_number=133)
                    session.add(r)
                    runs.append(r)
            await session.flush()
            async with session.begin():
                for r in runs:
                    session.add(StepExecution(task_run_id=r.id, step_name="address_review", status="done"))

        agent = type(
            "AgentState",
            (),
            {"run_id": 17, "issue": "119", "project_dir": Path("/tmp/test")},
        )()

        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue="119",
            pr_number=133,
            summary="Findings",
            next_actions=[
                HandoffAction(
                    id="address",
                    label="Address Review",
                    auto_execute=True,
                    mode="agent",
                    args={"issue": "119", "role": "developer", "pr": 133},
                ),
            ],
        )

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        mock_start = AsyncMock(return_value={"run_id": 18})
        mock_clear = MagicMock()
        mock_cfg = MagicMock()
        mock_cfg.pipeline.max_address_review_cycles = 0
        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.dashboard.services.handoff_service.clear_handoff", mock_clear),
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch("sova.db.session.get_session", side_effect=_ignore_project_dir),
        ):
            await _process_auto_handoff(agent)

        mock_start.assert_awaited_once()

    async def test_initial_dev_run_not_counted(self) -> None:
        """Initial developer run (with pr_number but no address_review step) should not count."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.db.session import get_session as real_get_session
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        # Seed: 1 initial dev run (no address_review step) + 1 address-review run
        async with await get_session() as session:
            async with session.begin():
                initial = TaskRun(issue_number="120", role="developer", status="done", pr_number=134)
                ar1 = TaskRun(issue_number="120", role="developer", status="done", pr_number=134)
                session.add_all([initial, ar1])
            await session.flush()
            async with session.begin():
                # Only the address-review run has the step record
                session.add(StepExecution(task_run_id=ar1.id, step_name="address_review", status="done"))

        agent = type(
            "AgentState",
            (),
            {"run_id": 19, "issue": "120", "project_dir": Path("/tmp/test")},
        )()

        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue="120",
            pr_number=134,
            summary="Findings to address",
            next_actions=[
                HandoffAction(
                    id="address",
                    label="Address Review",
                    auto_execute=True,
                    mode="agent",
                    args={"issue": "120", "role": "developer", "pr": 134},
                ),
            ],
        )

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        mock_start = AsyncMock(return_value={"run_id": 20})
        mock_clear = MagicMock()
        mock_cfg = MagicMock()
        mock_cfg.pipeline.max_address_review_cycles = 2
        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.dashboard.services.handoff_service.clear_handoff", mock_clear),
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch("sova.db.session.get_session", side_effect=_ignore_project_dir),
        ):
            await _process_auto_handoff(agent)

        # Should proceed: only 1 address-review run counted (not 2)
        mock_start.assert_awaited_once()

    async def test_circuit_breaker_isolates_by_issue(self) -> None:
        """Runs for issue 115 should not block address-review for issue 121."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sqlalchemy import select

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.db.session import get_session as real_get_session
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        # Seed 3 completed address-review runs for issue 115 (over limit)
        async with await get_session() as session:
            async with session.begin():
                for _ in range(3):
                    r = TaskRun(issue_number="115", role="developer", status="done", pr_number=140)
                    session.add(r)
            await session.flush()
            async with session.begin():
                for r in (await session.execute(select(TaskRun).where(TaskRun.pr_number == 140))).scalars():
                    session.add(StepExecution(task_run_id=r.id, step_name="address_review", status="done"))

        # Handoff is for issue 121 (no prior runs)
        agent = type(
            "AgentState",
            (),
            {"run_id": 21, "issue": "121", "project_dir": Path("/tmp/test")},
        )()

        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue="121",
            pr_number=141,
            summary="Findings to address",
            next_actions=[
                HandoffAction(
                    id="address",
                    label="Address Review",
                    auto_execute=True,
                    mode="agent",
                    args={"issue": "121", "role": "developer", "pr": 141},
                ),
            ],
        )

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        mock_start = AsyncMock(return_value={"run_id": 22})
        mock_clear = MagicMock()
        mock_cfg = MagicMock()
        mock_cfg.pipeline.max_address_review_cycles = 2
        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.dashboard.services.handoff_service.clear_handoff", mock_clear),
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch("sova.db.session.get_session", side_effect=_ignore_project_dir),
        ):
            await _process_auto_handoff(agent)

        # Issue 121 should NOT be blocked by issue 115's runs
        mock_start.assert_awaited_once()


# ---------------------------------------------------------------------------
# Queue service -- cross-run PR number lookup
# ---------------------------------------------------------------------------


class TestQueueServicePrLookup:
    async def test_pr_number_from_earlier_run(self) -> None:
        """PR number should be found from earlier developer run even when latest is reviewer."""
        from sova.dashboard.services.queue_service import _get_last_runs_by_issue

        session = await get_session()
        async with session.begin():
            session.add(TaskRun(issue_number="114", role="developer", status="done", pr_number=131))
            session.add(TaskRun(issue_number="114", role="reviewer", status="done", pr_number=None))

        result = await _get_last_runs_by_issue(None)
        assert result["114"]["pr_number"] == 131
        assert result["114"]["role"] == "reviewer"

    async def test_pr_number_from_latest_run(self) -> None:
        """When latest run has pr_number, it should be used directly."""
        from sova.dashboard.services.queue_service import _get_last_runs_by_issue

        session = await get_session()
        async with session.begin():
            session.add(TaskRun(issue_number="113", role="developer", status="done", pr_number=130))

        result = await _get_last_runs_by_issue(None)
        assert result["113"]["pr_number"] == 130

    async def test_no_pr_number_when_none_exists(self) -> None:
        """Issues with no PR across any run should return None."""
        from sova.dashboard.services.queue_service import _get_last_runs_by_issue

        session = await get_session()
        async with session.begin():
            session.add(TaskRun(issue_number="50", role="triage", status="done", pr_number=None))

        result = await _get_last_runs_by_issue(None)
        assert result["50"]["pr_number"] is None


# ---------------------------------------------------------------------------
# Per-issue handoff file I/O
# ---------------------------------------------------------------------------


class TestPerIssueHandoffFiles:
    def test_write_creates_per_issue_file(self, tmp_path: Path) -> None:
        from sova.ipc.handoff import DashboardHandoff, write_handoff_file

        h = DashboardHandoff(source="developer", status="awaiting_action", issue="113", summary="test")
        write_handoff_file(tmp_path, h)

        assert (tmp_path / ".claude" / "agent-control" / "handoff-113.json").exists()
        assert not (tmp_path / ".claude" / "agent-control" / "handoff.json").exists()

    def test_write_falls_back_to_legacy_when_no_issue(self, tmp_path: Path) -> None:
        from sova.ipc.handoff import DashboardHandoff, write_handoff_file

        h = DashboardHandoff(source="developer", status="completed", issue="", summary="test")
        write_handoff_file(tmp_path, h)

        assert (tmp_path / ".claude" / "agent-control" / "handoff.json").exists()

    def test_read_with_issue_reads_correct_file(self, tmp_path: Path) -> None:
        from sova.ipc.handoff import DashboardHandoff, read_handoff_file, write_handoff_file

        write_handoff_file(tmp_path, DashboardHandoff(source="dev", status="awaiting_action", issue="113", summary="a"))
        write_handoff_file(tmp_path, DashboardHandoff(source="dev", status="awaiting_action", issue="114", summary="b"))

        h = read_handoff_file(tmp_path, issue="113")
        assert h is not None
        assert h.issue == "113"
        assert h.summary == "a"

    def test_read_without_issue_returns_most_recent(self, tmp_path: Path) -> None:
        import os

        from sova.ipc.handoff import DashboardHandoff, read_handoff_file, write_handoff_file

        old_path = write_handoff_file(
            tmp_path, DashboardHandoff(source="dev", status="awaiting_action", issue="113", summary="old")
        )
        new_path = write_handoff_file(
            tmp_path, DashboardHandoff(source="dev", status="awaiting_action", issue="114", summary="new")
        )
        os.utime(old_path, (1_700_000_000, 1_700_000_000))
        os.utime(new_path, (1_700_000_100, 1_700_000_100))

        h = read_handoff_file(tmp_path)
        assert h is not None
        assert h.issue == "114"

    def test_read_falls_back_to_legacy(self, tmp_path: Path) -> None:
        import json

        cdir = tmp_path / ".claude" / "agent-control"
        cdir.mkdir(parents=True)
        (cdir / "handoff.json").write_text(
            json.dumps({"id": "x", "source": "dev", "status": "completed", "issue": "99", "summary": "legacy"})
        )

        from sova.ipc.handoff import read_handoff_file

        h = read_handoff_file(tmp_path, issue="99")
        assert h is not None
        assert h.issue == "99"

    def test_read_with_issue_ignores_legacy_for_different_issue(self, tmp_path: Path) -> None:
        import json

        cdir = tmp_path / ".claude" / "agent-control"
        cdir.mkdir(parents=True)
        (cdir / "handoff.json").write_text(
            json.dumps({"id": "x", "source": "dev", "status": "completed", "issue": "114", "summary": "other"})
        )

        from sova.ipc.handoff import read_handoff_file

        h = read_handoff_file(tmp_path, issue="113")
        assert h is None

    def test_read_all_returns_all_files(self, tmp_path: Path) -> None:
        from sova.ipc.handoff import DashboardHandoff, read_all_handoff_files, write_handoff_file

        for issue in ["113", "114", "115"]:
            write_handoff_file(
                tmp_path, DashboardHandoff(source="dev", status="awaiting_action", issue=issue, summary=f"#{issue}")
            )

        all_h = read_all_handoff_files(tmp_path)
        assert len(all_h) == 3
        issues = {h.issue for h in all_h}
        assert issues == {"113", "114", "115"}

    def test_parallel_writes_coexist(self, tmp_path: Path) -> None:
        from sova.ipc.handoff import DashboardHandoff, write_handoff_file

        for issue in ["113", "114", "115"]:
            write_handoff_file(
                tmp_path, DashboardHandoff(source="dev", status="awaiting_action", issue=issue, summary=f"#{issue}")
            )

        cdir = tmp_path / ".claude" / "agent-control"
        assert (cdir / "handoff-113.json").exists()
        assert (cdir / "handoff-114.json").exists()
        assert (cdir / "handoff-115.json").exists()


class TestPerIssueHandoffService:
    def test_clear_specific_issue(self, tmp_path: Path, monkeypatch) -> None:
        from sova.dashboard.services import handoff_service
        from sova.ipc.handoff import DashboardHandoff, write_handoff_file

        monkeypatch.setattr(handoff_service, "_resolve_project_dir", lambda: tmp_path)
        handoff_service._handoff_caches.clear()

        write_handoff_file(tmp_path, DashboardHandoff(source="dev", status="awaiting_action", issue="113", summary="a"))
        write_handoff_file(tmp_path, DashboardHandoff(source="dev", status="awaiting_action", issue="114", summary="b"))

        cleared = handoff_service.clear_handoff(issue="113")
        assert cleared is True

        cdir = tmp_path / ".claude" / "agent-control"
        assert not (cdir / "handoff-113.json").exists()
        assert (cdir / "handoff-114.json").exists()

    def test_clear_all(self, tmp_path: Path, monkeypatch) -> None:
        from sova.dashboard.services import handoff_service
        from sova.ipc.handoff import DashboardHandoff, write_handoff_file

        monkeypatch.setattr(handoff_service, "_resolve_project_dir", lambda: tmp_path)
        handoff_service._handoff_caches.clear()

        write_handoff_file(tmp_path, DashboardHandoff(source="dev", status="awaiting_action", issue="113", summary="a"))
        write_handoff_file(tmp_path, DashboardHandoff(source="dev", status="awaiting_action", issue="114", summary="b"))

        cleared = handoff_service.clear_handoff()
        assert cleared is True

        cdir = tmp_path / ".claude" / "agent-control"
        assert not (cdir / "handoff-113.json").exists()
        assert not (cdir / "handoff-114.json").exists()

    def test_get_all_handoffs(self, tmp_path: Path, monkeypatch) -> None:
        from sova.dashboard.services import handoff_service
        from sova.ipc.handoff import DashboardHandoff, write_handoff_file

        monkeypatch.setattr(handoff_service, "_resolve_project_dir", lambda: tmp_path)
        handoff_service._handoff_caches.clear()

        write_handoff_file(tmp_path, DashboardHandoff(source="dev", status="awaiting_action", issue="113", summary="a"))
        write_handoff_file(tmp_path, DashboardHandoff(source="dev", status="awaiting_action", issue="114", summary="b"))

        all_h = handoff_service.get_all_handoffs()
        assert len(all_h) == 2
        issues = {h["issue"] for h in all_h}
        assert issues == {"113", "114"}

    def test_get_handoff_with_issue(self, tmp_path: Path, monkeypatch) -> None:
        from sova.dashboard.services import handoff_service
        from sova.ipc.handoff import DashboardHandoff, write_handoff_file

        monkeypatch.setattr(handoff_service, "_resolve_project_dir", lambda: tmp_path)
        handoff_service._handoff_caches.clear()

        write_handoff_file(tmp_path, DashboardHandoff(source="dev", status="awaiting_action", issue="113", summary="a"))
        write_handoff_file(tmp_path, DashboardHandoff(source="dev", status="awaiting_action", issue="114", summary="b"))

        h = handoff_service.get_handoff(issue="113")
        assert h is not None
        assert h["issue"] == "113"


# ---------------------------------------------------------------------------
# Control Service -- duplicate agent prevention
# ---------------------------------------------------------------------------


class TestDuplicateAgentPrevention:
    async def test_start_agent_rejects_duplicate_issue(self) -> None:
        """Starting an agent for an issue that already has one running should fail."""
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import AgentState, ProjectAgents, start_agent

        pa = ProjectAgents()
        existing = AgentState(
            run_id=1,
            issue="73",
            role="developer",
            process=MagicMock(),
        )
        pa.agents[1] = existing

        with patch.object(agent_lifecycle, "_get_project_agents", return_value=pa):
            result = await start_agent("73")

        assert "error" in result
        assert "already has an active agent" in result["error"]
        assert result["existing_run_id"] == 1

    async def test_start_agent_allows_different_issue(self) -> None:
        """Starting an agent for a different issue should succeed (mocked spawn)."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import AgentState, ProjectAgents, start_agent

        pa = ProjectAgents()
        existing = AgentState(
            run_id=1,
            issue="73",
            role="developer",
            process=MagicMock(),
        )
        pa.agents[1] = existing

        mock_process = MagicMock()
        mock_process.pid = 12345

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(
                agent_lifecycle,
                "get_runtime",
                return_value=MagicMock(spawn=AsyncMock(return_value=mock_process)),
            ),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=2),
            patch.object(agent_lifecycle, "_set_output_file_path", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_transition_to_in_progress", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            result = await start_agent("74")

        assert result["status"] == "started"
        assert result["run_id"] == 2

    async def test_start_agent_passes_pr_number_to_task_run(self) -> None:
        """pr_number should be forwarded to _create_task_run for immediate persistence."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        pa = ProjectAgents()

        mock_process = MagicMock()
        mock_process.pid = 12345

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(
                agent_lifecycle,
                "get_runtime",
                return_value=MagicMock(spawn=AsyncMock(return_value=mock_process)),
            ),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=3) as mock_create,
            patch.object(agent_lifecycle, "_set_output_file_path", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_transition_to_in_progress", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            result = await start_agent("93", role="reviewer", pr_number=122)

        assert result["status"] == "started"
        mock_create.assert_awaited_once()
        call_kwargs = mock_create.call_args
        assert call_kwargs.kwargs["pr_number"] == 122

    async def test_start_agent_skips_in_progress_for_reviewer(self) -> None:
        """Reviewer agents must not trigger _transition_to_in_progress (would overwrite in_review)."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        pa = ProjectAgents()

        mock_process = MagicMock()
        mock_process.pid = 12345

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(
                agent_lifecycle,
                "get_runtime",
                return_value=MagicMock(spawn=AsyncMock(return_value=mock_process)),
            ),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=4),
            patch.object(agent_lifecycle, "_set_output_file_path", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_transition_to_in_progress", new_callable=AsyncMock) as mock_transition,
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            result = await start_agent("75", role="reviewer", pr_number=78)

        assert result["status"] == "started"
        mock_transition.assert_not_called()

    async def test_start_agent_skips_in_progress_for_address_review(self) -> None:
        """Developer spawned with pr_number (address-review) must not transition to in_progress."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        pa = ProjectAgents()

        mock_process = MagicMock()
        mock_process.pid = 12345

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(
                agent_lifecycle,
                "get_runtime",
                return_value=MagicMock(spawn=AsyncMock(return_value=mock_process)),
            ),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=5),
            patch.object(agent_lifecycle, "_set_output_file_path", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_transition_to_in_progress", new_callable=AsyncMock) as mock_transition,
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            result = await start_agent("80", role="developer", pr_number=88)

        assert result["status"] == "started"
        mock_transition.assert_not_called()

    async def test_start_agent_includes_run_id_in_prompt(self) -> None:
        """Prompt must include --run-id so the subprocess reuses the dashboard TaskRun."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        pa = ProjectAgents()

        mock_process = MagicMock()
        mock_process.pid = 12345

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        mock_spawn = AsyncMock(return_value=mock_process)
        mock_rt = MagicMock(spawn=mock_spawn)

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "get_runtime", return_value=mock_rt),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=7),
            patch.object(agent_lifecycle, "_set_output_file_path", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_update_task_run_pid", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_transition_to_in_progress", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            result = await start_agent("99")

        assert result["status"] == "started"
        assert mock_spawn.call_args is not None, "spawn() was never called"
        # spawn is called with positional args: (prompt, cwd, ...)
        prompt_arg = mock_spawn.call_args[0][0] if mock_spawn.call_args[0] else ""
        assert "--run-id 7" in prompt_arg

    async def test_start_agent_cleans_up_on_spawn_failure(self) -> None:
        """If process spawn fails, the pre-created TaskRun should be marked failed."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        pa = ProjectAgents()

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(
                agent_lifecycle,
                "get_runtime",
                return_value=MagicMock(spawn=AsyncMock(side_effect=OSError("spawn failed"))),
            ),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=8),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_finalize_orphaned_run", new_callable=AsyncMock) as mock_orphan,
        ):
            result = await start_agent("100")

        assert "error" in result
        mock_orphan.assert_awaited_once_with(8, pa.project_dir)

    async def test_start_command_rejects_duplicate_issue(self) -> None:
        """start_command() should reject if the same issue already has an active agent."""
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import AgentState, ProjectAgents, start_command

        pa = ProjectAgents()
        existing = AgentState(
            run_id=1,
            issue="16",
            role="developer",
            process=MagicMock(),
        )
        pa.agents[1] = existing

        with patch.object(agent_lifecycle, "_get_project_agents", return_value=pa):
            result = await start_command("integrate-pr", {"issue": "16"})

        assert "error" in result
        assert "already has an active agent" in result["error"]

    async def test_check_issue_conflict_catches_db_duplicate(self) -> None:
        """_check_issue_conflict should detect DB TaskRuns with alive PIDs."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_lifecycle import ProjectAgents, _check_issue_conflict
        from sova.db.models import TaskRun
        from sova.db.session import get_session as real_get_session

        pa = ProjectAgents()

        async with await real_get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="16",
                    role="developer",
                    status="running",
                    pid=12345,
                    current_step="develop",
                )
                session.add(run)

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        with (
            patch("sova.db.session.get_session", side_effect=_ignore_project_dir),
            patch(
                "sova.dashboard.services.agent_recovery._is_process_alive",
                return_value=True,
            ),
        ):
            result = await _check_issue_conflict("16", pa)

        assert result is not None
        assert "already has an active agent" in result["error"]
        assert "external" in result["error"]

    async def test_check_issue_conflict_degrades_on_db_failure(self) -> None:
        """_check_issue_conflict should return None (no conflict) if DB query fails."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_lifecycle import ProjectAgents, _check_issue_conflict

        pa = ProjectAgents()

        with patch("sova.db.session.get_session", side_effect=RuntimeError("DB down")):
            result = await _check_issue_conflict("16", pa)

        assert result is None


# ---------------------------------------------------------------------------
# build_action_command
# ---------------------------------------------------------------------------


class TestBuildActionCommand:
    def test_uses_issue_key_over_pr(self) -> None:
        from sova.dashboard.services.handoff_service import build_action_command

        result = build_action_command(
            {
                "mode": "agent",
                "args": {"issue": "16", "pr": 67, "role": "developer"},
            }
        )
        assert result["issue"] == "16"
        assert result["pr_number"] == 67
        assert result["role"] == "developer"

    def test_falls_back_to_ticket_key(self) -> None:
        from sova.dashboard.services.handoff_service import build_action_command

        result = build_action_command(
            {
                "mode": "agent",
                "args": {"ticket": "42", "role": "reviewer"},
            }
        )
        assert result["issue"] == "42"

    def test_falls_back_to_pr_as_issue(self) -> None:
        from sova.dashboard.services.handoff_service import build_action_command

        result = build_action_command(
            {
                "mode": "agent",
                "args": {"pr": 99},
            }
        )
        assert result["issue"] == "99"


# ---------------------------------------------------------------------------
# Auto-handoff orchestration
# ---------------------------------------------------------------------------


class TestAutoHandoff:
    async def test_auto_handoff_spawns_agent(self) -> None:
        """_process_auto_handoff should auto-spawn an agent for auto_execute actions."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import AgentState, _process_auto_handoff
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        agent = AgentState(
            run_id=1,
            issue="42",
            role="developer",
            process=MagicMock(),
        )

        handoff = DashboardHandoff(
            source="developer",
            status="awaiting_action",
            issue="42",
            pr_number=10,
            branch="feat/test",
            summary="Ready for review",
            next_actions=[
                HandoffAction(
                    id="review",
                    label="Review",
                    mode="agent",
                    args={"issue": "42", "pr": 10, "role": "reviewer"},
                    auto_execute=True,
                ),
            ],
        )

        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch.object(
                agent_lifecycle, "start_agent", new_callable=AsyncMock, return_value={"status": "started"}
            ) as mock_start,
            patch("sova.dashboard.services.handoff_service.clear_handoff") as mock_clear,
        ):
            await _process_auto_handoff(agent)

        mock_start.assert_awaited_once_with("42", role="reviewer", pr_number=10, slug=None)
        mock_clear.assert_called_once()

    async def test_auto_handoff_skips_non_auto_actions(self) -> None:
        """_process_auto_handoff should not trigger actions without auto_execute."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import AgentState, _process_auto_handoff
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        agent = AgentState(
            run_id=1,
            issue="42",
            role="reviewer",
            process=MagicMock(),
        )

        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue="42",
            pr_number=10,
            summary="Clean review",
            next_actions=[
                HandoffAction(
                    id="integrate",
                    label="Integrate PR",
                    mode="claude-command",
                    command="/integrate-pr 10",
                    auto_execute=False,
                ),
            ],
        )

        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch.object(agent_lifecycle, "start_agent", new_callable=AsyncMock) as mock_agent,
            patch.object(agent_lifecycle, "start_command", new_callable=AsyncMock) as mock_cmd,
        ):
            await _process_auto_handoff(agent)

        mock_agent.assert_not_awaited()
        mock_cmd.assert_not_awaited()

    async def test_auto_handoff_claude_command(self) -> None:
        """_process_auto_handoff should run claude-command actions with auto_execute."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import AgentState, _process_auto_handoff
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        agent = AgentState(
            run_id=1,
            issue="42",
            role="reviewer",
            process=MagicMock(),
        )

        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue="42",
            pr_number=10,
            summary="Clean review",
            next_actions=[
                HandoffAction(
                    id="integrate",
                    label="Integrate PR",
                    mode="claude-command",
                    command="/integrate-pr 10",
                    args={"issue": "42", "pr": 10},
                    auto_execute=True,
                ),
            ],
        )

        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch.object(
                agent_lifecycle, "start_command", new_callable=AsyncMock, return_value={"status": "started"}
            ) as mock_cmd,
            patch("sova.dashboard.services.handoff_service.clear_handoff") as mock_clear,
        ):
            await _process_auto_handoff(agent)

        mock_cmd.assert_awaited_once_with("integrate-pr", {"issue": "42", "pr": 10}, slug=None)
        mock_clear.assert_called_once()

    async def test_auto_handoff_no_handoff_file(self) -> None:
        """_process_auto_handoff should handle missing handoff gracefully."""
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services.control_service import AgentState, _process_auto_handoff

        agent = AgentState(
            run_id=1,
            issue="42",
            role="developer",
            process=MagicMock(),
        )

        with patch("sova.ipc.handoff.read_handoff_file", return_value=None):
            await _process_auto_handoff(agent)  # Should not raise


# ---------------------------------------------------------------------------
# Memory API
# ---------------------------------------------------------------------------


class TestMemoryAPI:
    async def test_list_memories_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/memory")
        assert resp.status_code == 200
        data = resp.json()
        assert data["memories"] == []
        assert data["total"] == 0

    async def test_list_memories_with_data(self, client: AsyncClient, seed_data) -> None:
        resp = await client.get("/api/memory")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["memories"][0]["title"] == "Test pattern"

    async def test_search_memories(self, client: AsyncClient, seed_data) -> None:
        resp = await client.get("/api/memory?q=fixture")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1

    async def test_search_no_results(self, client: AsyncClient, seed_data) -> None:
        resp = await client.get("/api/memory?q=nonexistent")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0


# ---------------------------------------------------------------------------
# Handoff API
# ---------------------------------------------------------------------------


class TestHandoffAPI:
    @pytest.fixture(autouse=True)
    def _isolate_handoff(self, tmp_path, monkeypatch):
        from sova.dashboard.services import handoff_service

        monkeypatch.setattr(handoff_service, "_resolve_project_dir", lambda: tmp_path)
        handoff_service._handoff_caches.clear()

    async def test_get_handoff_none(self, client: AsyncClient) -> None:
        resp = await client.get("/api/handoff")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_handoff"] is False

    async def test_get_handoff_with_file(self, client: AsyncClient, tmp_path) -> None:
        import json

        control_dir = tmp_path / ".claude" / "agent-control"
        control_dir.mkdir(parents=True)
        handoff_data = {
            "id": "test-123",
            "source": "ship-pr",
            "status": "awaiting_action",
            "summary": "Test handoff",
            "created_at": "2026-04-20T10:00:00Z",
            "next_actions": [
                {
                    "id": "merge",
                    "label": "Merge PR",
                    "style": "approve",
                    "mode": "claude-command",
                    "command": "approve-merge",
                },
            ],
        }
        (control_dir / "handoff.json").write_text(json.dumps(handoff_data))

        resp = await client.get("/api/handoff")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_handoff"] is True
        assert data["handoff"]["source"] == "ship-pr"
        assert len(data["handoff"]["next_actions"]) == 1

    async def test_clear_handoff_no_file(self, client: AsyncClient) -> None:
        resp = await client.post("/api/handoff/clear")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cleared"] is False

    async def test_clear_handoff_with_file(self, client: AsyncClient, tmp_path) -> None:
        import json

        control_dir = tmp_path / ".claude" / "agent-control"
        control_dir.mkdir(parents=True)
        (control_dir / "handoff.json").write_text(
            json.dumps(
                {
                    "id": "test-456",
                    "source": "test",
                    "status": "completed",
                    "summary": "Done",
                    "created_at": "2026-04-20T10:00:00Z",
                }
            )
        )

        resp = await client.post("/api/handoff/clear")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cleared"] is True

        # File should be gone
        assert not (control_dir / "handoff.json").exists()
        # Archive should exist
        assert (control_dir / "handoff-archive").exists()

    async def test_execute_no_handoff(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/handoff/execute",
            json={"action_id": "merge"},
        )
        assert resp.status_code == 404
        data = resp.json()
        assert "not found" in data["detail"]

    async def test_execute_action_not_found(self, client: AsyncClient, tmp_path) -> None:
        import json

        control_dir = tmp_path / ".claude" / "agent-control"
        control_dir.mkdir(parents=True)
        (control_dir / "handoff.json").write_text(
            json.dumps(
                {
                    "id": "test-789",
                    "source": "test",
                    "status": "awaiting_action",
                    "summary": "Test",
                    "created_at": "2026-04-20T10:00:00Z",
                    "next_actions": [{"id": "merge", "label": "Merge"}],
                }
            )
        )

        resp = await client.post(
            "/api/handoff/execute",
            json={"action_id": "nonexistent"},
        )
        assert resp.status_code == 404
        data = resp.json()
        assert "not found" in data["detail"]

    async def test_execute_agent_action_passes_pr_number(self, client: AsyncClient, tmp_path) -> None:
        """Executing an agent-mode handoff action should pass pr_number to start_agent."""
        import json
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services import control_service as cs

        control_dir = tmp_path / ".claude" / "agent-control"
        control_dir.mkdir(parents=True)
        (control_dir / "handoff.json").write_text(
            json.dumps(
                {
                    "id": "test-pr",
                    "source": "reviewer",
                    "status": "awaiting_action",
                    "issue": "16",
                    "pr_number": 67,
                    "summary": "5 findings",
                    "created_at": "2026-05-02T22:00:00Z",
                    "next_actions": [
                        {
                            "id": "address_review",
                            "label": "Address Review",
                            "mode": "agent",
                            "args": {"issue": "16", "pr": 67, "role": "developer"},
                            "auto_execute": True,
                        }
                    ],
                }
            )
        )

        with patch.object(
            cs, "start_agent", new_callable=AsyncMock, return_value={"status": "started", "run_id": 1, "pid": 123}
        ) as mock_start:
            resp = await client.post("/api/handoff/execute", json={"action_id": "address_review"})

        assert resp.status_code == 200
        mock_start.assert_awaited_once()
        call_kwargs = mock_start.call_args
        assert call_kwargs.kwargs["pr_number"] == 67
        assert call_kwargs.args[0] == "16"

    async def test_agents_page_has_handoff_support(self, client: AsyncClient) -> None:
        resp = await client.get("/agents")
        assert resp.status_code == 200
        assert "checkHandoffForAgents" in resp.text
        assert "executeHandoffAction" in resp.text

    async def test_agents_page_has_kanban_view(self, client: AsyncClient) -> None:
        resp = await client.get("/agents")
        assert resp.status_code == 200
        assert "kanban-grid" in resp.text
        assert "kanban-columns" in resp.text
        assert "switchAgentView" in resp.text
        assert "loadKanban" in resp.text
        assert "view-list" in resp.text
        assert "view-kanban" in resp.text


# ---------------------------------------------------------------------------
# Multi-project mode
# ---------------------------------------------------------------------------


class TestMultiProject:
    @pytest.fixture
    async def multi_client(self, tmp_path):
        """Create a multi-project dashboard with two registered projects."""
        from sova.config import registry

        # Use isolated registry
        reg_file = tmp_path / "registry" / "projects.json"
        import sova.config.registry as reg_mod

        orig_file = reg_mod._REGISTRY_FILE
        orig_dir = reg_mod._REGISTRY_DIR
        reg_mod._REGISTRY_FILE = reg_file
        reg_mod._REGISTRY_DIR = tmp_path / "registry"

        # Create two project dirs
        p1 = tmp_path / "project-alpha"
        p1.mkdir()
        (p1 / ".claude").mkdir()
        p2 = tmp_path / "project-beta"
        p2.mkdir()
        (p2 / ".claude").mkdir()

        registry.register_project(p1, slug="alpha")
        registry.register_project(p2, slug="beta")

        from sova.dashboard.app import create_app

        app = create_app(multi_project=True)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

        reg_mod._REGISTRY_FILE = orig_file
        reg_mod._REGISTRY_DIR = orig_dir

    async def test_home_shows_project_list(self, multi_client: AsyncClient) -> None:
        resp = await multi_client.get("/")
        assert resp.status_code == 200
        assert "alpha" in resp.text
        assert "beta" in resp.text

    async def test_project_redirect(self, multi_client: AsyncClient) -> None:
        resp = await multi_client.get("/p/alpha", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/p/alpha/dashboard"

    async def test_project_dashboard(self, multi_client: AsyncClient) -> None:
        resp = await multi_client.get("/p/alpha/dashboard")
        assert resp.status_code == 200
        assert "Dashboard" in resp.text

    async def test_project_api(self, multi_client: AsyncClient) -> None:
        resp = await multi_client.get("/p/alpha/api/overview")
        assert resp.status_code == 200
        data = resp.json()
        assert "runs" in data

    async def test_projects_api_list(self, multi_client: AsyncClient) -> None:
        resp = await multi_client.get("/api/projects")
        assert resp.status_code == 200
        data = resp.json()
        assert "alpha" in data["projects"]
        assert "beta" in data["projects"]

    async def test_fallback_api_still_works(self, multi_client: AsyncClient) -> None:
        resp = await multi_client.get("/api/overview")
        assert resp.status_code == 200

    async def test_setup_page_loads(self, multi_client: AsyncClient) -> None:
        resp = await multi_client.get("/setup")
        assert resp.status_code == 200
        assert "Project Setup" in resp.text

    async def test_project_work_redirects_to_agents(self, multi_client: AsyncClient) -> None:
        resp = await multi_client.get("/p/alpha/work", follow_redirects=False)
        assert resp.status_code == 301
        assert resp.headers["location"] == "/p/alpha/agents"

    async def test_project_work_detail_renders(self, multi_client: AsyncClient) -> None:
        resp = await multi_client.get("/p/alpha/work/1")
        assert resp.status_code == 200
        assert "Run #1" in resp.text

    async def test_project_tasks_redirects_to_agents(self, multi_client: AsyncClient) -> None:
        resp = await multi_client.get("/p/alpha/tasks", follow_redirects=False)
        assert resp.status_code == 301
        assert resp.headers["location"] == "/p/alpha/agents"

    async def test_project_runs_redirects_to_agents(self, multi_client: AsyncClient) -> None:
        resp = await multi_client.get("/p/alpha/runs", follow_redirects=False)
        assert resp.status_code == 301
        assert resp.headers["location"] == "/p/alpha/agents"

    async def test_project_lifecycle_renders(self, multi_client: AsyncClient) -> None:
        resp = await multi_client.get("/p/alpha/lifecycle/42")
        assert resp.status_code == 200
        assert "Issue " in resp.text and "#42" in resp.text

    async def test_uninstall_api_accepts_json_body(self, multi_client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        with patch("sova.cli.commands.project._uninstall", new_callable=AsyncMock) as mock_uninstall:
            mock_uninstall.return_value = []
            resp = await multi_client.post(
                "/api/projects/uninstall",
                json={"slug": "alpha", "remove_files": True, "remove_commands": True, "remove_memory": True},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["removed"] is True
        assert data["files_cleaned"] is True
        mock_uninstall.assert_awaited_once()
        kwargs = mock_uninstall.call_args.kwargs
        assert kwargs["remove_commands"] is True
        assert kwargs["remove_rules"] is False
        assert kwargs["remove_memory"] is True
        assert kwargs["remove_config"] is False

    async def test_uninstall_unknown_slug_unregisters(self, multi_client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        with patch("sova.cli.commands.project._uninstall", new_callable=AsyncMock) as mock_uninstall:
            resp = await multi_client.post(
                "/api/projects/uninstall",
                json={"slug": "nonexistent", "remove_files": True},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["removed"] is False
        assert data["files_cleaned"] is False
        mock_uninstall.assert_not_called()


# ---------------------------------------------------------------------------
# Setup API
# ---------------------------------------------------------------------------


class TestTasksAPI:
    """Tests for the tasks API endpoints."""

    async def test_active_tasks_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/tasks/active")
        assert resp.status_code == 200
        data = resp.json()
        assert "tasks" in data
        assert isinstance(data["tasks"], list)

    async def test_active_tasks_with_data(self, client: AsyncClient, seed_data) -> None:
        resp = await client.get("/api/tasks/active")
        assert resp.status_code == 200
        data = resp.json()
        # run3 has status "developing" -- non-terminal, should appear
        assert len(data["tasks"]) >= 1
        active = data["tasks"][0]
        assert active["issue_number"] == "44"
        assert active["status"] == "developing"
        assert "elapsed_seconds" in active

    async def test_task_history_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/tasks/history")
        assert resp.status_code == 200
        data = resp.json()
        assert data["tasks"] == []

    async def test_task_history_with_data(self, client: AsyncClient, seed_data) -> None:
        resp = await client.get("/api/tasks/history")
        assert resp.status_code == 200
        data = resp.json()
        # run1 (done) and run2 (failed) are terminal
        assert len(data["tasks"]) == 2
        statuses = {t["status"] for t in data["tasks"]}
        assert statuses == {"done", "failed"}

    async def test_task_history_limit(self, client: AsyncClient, seed_data) -> None:
        resp = await client.get("/api/tasks/history?limit=1")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["tasks"]) == 1

    async def test_tasks_redirects_to_agents(self, client: AsyncClient) -> None:
        resp = await client.get("/tasks", follow_redirects=False)
        assert resp.status_code == 301
        assert resp.headers["location"] == "/agents"

    async def test_queue_page_renders(self, client: AsyncClient) -> None:
        resp = await client.get("/queue")
        assert resp.status_code == 200
        assert b"Priority Queue" in resp.content

    async def test_queue_page_has_bulk_bar(self, client: AsyncClient) -> None:
        resp = await client.get("/queue")
        assert resp.status_code == 200
        assert b"bulk-bar" in resp.content
        assert b"select-all" in resp.content


class TestMilestoneBadge:
    """Tests for _milestone_badge helper in queue_service."""

    def test_empty_string_returns_dash(self) -> None:
        from sova.dashboard.services.queue_service import _milestone_badge

        assert _milestone_badge("") == "--"

    def test_standard_milestone(self) -> None:
        from sova.dashboard.services.queue_service import _milestone_badge

        assert _milestone_badge("P3: v0.1 Public Release") == "P3"

    def test_milestone_no_colon(self) -> None:
        from sova.dashboard.services.queue_service import _milestone_badge

        assert _milestone_badge("Beta") == "Beta"

    def test_long_prefix_truncated(self) -> None:
        from sova.dashboard.services.queue_service import _milestone_badge

        assert _milestone_badge("VeryLongPrefix: something") == "VeryLong"
        assert len(_milestone_badge("VeryLongPrefix: something")) <= 8

    def test_whitespace_around_prefix(self) -> None:
        from sova.dashboard.services.queue_service import _milestone_badge

        assert _milestone_badge("  P4  : future work") == "P4"


class TestPhaseOrder:
    """Tests for _extract_phase_order helper in queue_service."""

    def test_phase_with_title(self) -> None:
        from sova.dashboard.services.queue_service import _extract_phase_order

        assert _extract_phase_order("Phase 1: Ship It") == 1

    def test_phase_number_only(self) -> None:
        from sova.dashboard.services.queue_service import _extract_phase_order

        assert _extract_phase_order("Phase 7") == 7

    def test_short_prefix(self) -> None:
        from sova.dashboard.services.queue_service import _extract_phase_order

        assert _extract_phase_order("P3: v0.1") == 3

    def test_empty_returns_default(self) -> None:
        from sova.dashboard.services.queue_service import _extract_phase_order

        assert _extract_phase_order("") == 99

    def test_none_returns_default(self) -> None:
        from sova.dashboard.services.queue_service import _extract_phase_order

        assert _extract_phase_order(None) == 99

    def test_no_phase_pattern(self) -> None:
        from sova.dashboard.services.queue_service import _extract_phase_order

        assert _extract_phase_order("Beta Release") == 99

    def test_phase_ordering(self) -> None:
        from sova.dashboard.services.queue_service import _extract_phase_order

        assert _extract_phase_order("Phase 1: Ship It") < _extract_phase_order("Phase 4: Bank")
        assert _extract_phase_order("Phase 4: Bank") < _extract_phase_order("Phase 7: Scale")

    def test_leading_whitespace(self) -> None:
        from sova.dashboard.services.queue_service import _extract_phase_order

        assert _extract_phase_order("  Phase 3: v0.1") == 3


class TestLabelPriority:
    """Tests for _extract_label_priority with spaced and compact label formats."""

    def test_spaced_priority_high(self) -> None:
        from sova.dashboard.services.queue_service import _extract_label_priority

        assert _extract_label_priority(["priority: high"]) == 1

    def test_spaced_priority_critical(self) -> None:
        from sova.dashboard.services.queue_service import _extract_label_priority

        assert _extract_label_priority(["priority: critical"]) == 0

    def test_compact_priority_medium(self) -> None:
        from sova.dashboard.services.queue_service import _extract_label_priority

        assert _extract_label_priority(["priority:medium"]) == 2

    def test_no_priority_returns_default(self) -> None:
        from sova.dashboard.services.queue_service import _extract_label_priority

        assert _extract_label_priority(["type: feature", "area: accounts"]) == 99

    def test_priority_ordering(self) -> None:
        from sova.dashboard.services.queue_service import _extract_label_priority

        assert _extract_label_priority(["priority: critical"]) < _extract_label_priority(["priority: high"])
        assert _extract_label_priority(["priority: high"]) < _extract_label_priority(["priority: low"])


class TestQueueServiceEnrichment:
    """Tests for NEEDS_SPEC inclusion and JIRA priority sorting in queue_service."""

    def test_needs_spec_in_actionable_states(self) -> None:
        from sova.adapters.base import TaskState
        from sova.dashboard.services.queue_service import _ACTIONABLE_STATES

        assert TaskState.NEEDS_SPEC in _ACTIONABLE_STATES

    def test_needs_spec_has_state_priority(self) -> None:
        from sova.adapters.base import TaskState
        from sova.dashboard.services.queue_service import _STATE_PRIORITY

        assert TaskState.NEEDS_SPEC in _STATE_PRIORITY
        assert _STATE_PRIORITY[TaskState.NEEDS_SPEC] > _STATE_PRIORITY[TaskState.BACKLOG]

    def test_needs_spec_recommended_action(self) -> None:
        from sova.adapters.base import TaskState
        from sova.dashboard.services.queue_service import _RECOMMENDED_ACTION

        assert _RECOMMENDED_ACTION[TaskState.NEEDS_SPEC] == "spec"

    def test_jira_priority_order_critical_before_minor(self) -> None:
        from sova.dashboard.services.queue_service import _JIRA_PRIORITY_ORDER

        assert _JIRA_PRIORITY_ORDER["Critical"] < _JIRA_PRIORITY_ORDER["Minor"]

    def test_jira_priority_order_major_before_trivial(self) -> None:
        from sova.dashboard.services.queue_service import _JIRA_PRIORITY_ORDER

        assert _JIRA_PRIORITY_ORDER["Major"] < _JIRA_PRIORITY_ORDER["Trivial"]


class TestBatchAPI:
    """Tests for the batch queue API endpoints."""

    async def test_start_batch_rejects_empty_issues(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/queue/batch",
            json={"issues": [], "action": "triage"},
        )

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail[0]["loc"][-1] == "issues"

    async def test_start_batch_rejects_non_numeric_issue(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/queue/batch",
            json={"issues": ["abc"], "action": "triage"},
        )

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail[0]["loc"][-1] == "issues"

    async def test_batch_status_not_found(self, client: AsyncClient) -> None:
        resp = await client.get("/api/queue/batch/nonexistent/status")
        assert resp.status_code == 404
        data = resp.json()
        assert "detail" in data

    async def test_start_batch_invalid_action(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/queue/batch",
            json={"issues": ["1"], "action": "invalid_action"},
        )

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail[0]["loc"][-1] == "action"

    async def test_cancel_nonexistent_batch(self, client: AsyncClient) -> None:
        resp = await client.post("/api/queue/batch/nonexistent/cancel")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cancelled"] is False

    async def test_active_batch_none(self, client: AsyncClient) -> None:
        resp = await client.get("/api/queue/batch/active")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] is False

    async def test_active_batch_returns_running(self, client: AsyncClient) -> None:
        from sova.dashboard.services.batch_service import BatchJob, _active_batches

        _active_batches["test_active"] = BatchJob(batch_id="test_active", action="triage", status="running", results=[])
        try:
            resp = await client.get("/api/queue/batch/active")
            assert resp.status_code == 200
            data = resp.json()
            assert data["active"] is True
            assert data["batch"]["batch_id"] == "test_active"
        finally:
            _active_batches.pop("test_active", None)

    async def test_start_from_queue_rejects_non_numeric_issue(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/queue/start/abc",
            json={},
        )
        assert resp.status_code == 422

    async def test_start_from_queue_rejects_empty_role(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/queue/start/123",
            json={"role": "   "},
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail[0]["loc"][-1] == "role"


class TestAgentsAPI:
    """Tests for the new agents API endpoints."""

    async def test_start_agent_rejects_empty_issue_without_role(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/agents/start",
            json={"issue": ""},
        )

        assert resp.status_code == 200
        data = resp.json()
        assert "error" in data

    async def test_start_agent_rejects_non_numeric_issue(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/agents/start",
            json={"issue": "abc"},
        )

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail[0]["loc"][-1] == "issue"

    async def test_start_agent_allows_empty_issue_with_role(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        with patch(
            "sova.dashboard.services.control_service.start_agent",
            new_callable=AsyncMock,
            return_value={"run_id": 1, "status": "started"},
        ):
            resp = await client.post(
                "/api/agents/start",
                json={"issue": "", "role": "planner"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data.get("status") == "started"

    async def test_start_agent_rejects_empty_role(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/agents/start",
            json={"issue": "123", "role": "   "},
        )

        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail[0]["loc"][-1] == "role"

    async def test_start_agent_accepts_custom_role(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        with patch(
            "sova.dashboard.services.control_service.start_agent",
            new_callable=AsyncMock,
            return_value={"run_id": 1, "status": "started"},
        ):
            resp = await client.post(
                "/api/agents/start",
                json={"issue": "123", "role": "custom-workflow"},
            )
            assert resp.status_code == 200

    async def test_agents_active_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/agents/active")
        assert resp.status_code == 200
        data = resp.json()
        assert "agents" in data
        assert "completed" in data
        assert "max_concurrent" in data
        assert "slots_available" in data
        assert isinstance(data["agents"], list)
        assert data["agents"] == []

    async def test_agents_interrupted_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/agents/interrupted")
        assert resp.status_code == 200
        data = resp.json()
        assert "interrupted" in data

    async def test_agents_pipeline(self, client: AsyncClient) -> None:
        resp = await client.get("/api/agents/pipeline")
        assert resp.status_code == 200
        data = resp.json()
        assert "steps" in data
        assert "sync" in data["steps"]
        assert "handoff_to_reviewer" in data["steps"]

    async def test_agents_output_not_found(self, client: AsyncClient) -> None:
        resp = await client.get("/api/agents/999/output?since=0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["lines"] == []

    async def test_pr_status_no_project_dir(self, client: AsyncClient) -> None:
        resp = await client.get("/api/agents/issue/42/pr-status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_pr"] is False

    async def test_pr_status_with_pr(self, client: AsyncClient) -> None:
        from dataclasses import dataclass
        from unittest.mock import AsyncMock, patch

        @dataclass
        class FakePRInfo:
            number: int = 10
            url: str = "https://github.com/test/repo/pull/10"

        @dataclass
        class FakePRStatus:
            number: int = 10
            state: str = "OPEN"
            mergeable: str = "MERGEABLE"
            review_decision: str = "APPROVED"
            url: str = "https://github.com/test/repo/pull/10"
            title: str = "Test PR"
            is_approved: bool = True
            is_mergeable: bool = True

        @dataclass
        class FakeConfig:
            github_repo: str = "test/repo"
            github_user: str = "testuser"

        with (
            patch("sova.dashboard.project_context.get_project_dir", return_value="/tmp/test"),
            patch("sova.config.loader.load_config", return_value=FakeConfig()),
            patch("sova.git.operations.find_pr_for_issue", new_callable=AsyncMock, return_value=FakePRInfo()),
            patch("sova.git.operations.get_pr_status", new_callable=AsyncMock, return_value=FakePRStatus()),
            patch("sova.git.operations.get_ci_checks", new_callable=AsyncMock, return_value=[]),
        ):
            resp = await client.get("/api/agents/issue/42/pr-status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["has_pr"] is True
        assert data["pr_number"] == 10
        assert data["is_approved"] is True
        assert data["ci_status"] == "none"
        assert data["review_decision"] == "APPROVED"

    async def test_pr_status_no_pr_found(self, client: AsyncClient) -> None:
        from dataclasses import dataclass
        from unittest.mock import AsyncMock, patch

        @dataclass
        class FakeConfig:
            github_repo: str = "test/repo"
            github_user: str = "testuser"

        with (
            patch("sova.dashboard.project_context.get_project_dir", return_value="/tmp/test"),
            patch("sova.config.loader.load_config", return_value=FakeConfig()),
            patch("sova.git.operations.find_pr_for_issue", new_callable=AsyncMock, return_value=None),
        ):
            resp = await client.get("/api/agents/issue/99/pr-status")

        assert resp.status_code == 200
        assert resp.json()["has_pr"] is False

    async def test_run_command_endpoint(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services import control_service as cs

        with patch.object(
            cs, "start_command", new_callable=AsyncMock, return_value={"status": "started", "run_id": 1, "pid": 123}
        ):
            resp = await client.post("/api/agents/command", json={"command": "integrate-pr", "args": {"pr": 73}})

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "started"
        assert data["run_id"] == 1


class TestKanbanAPI:
    """Tests for the kanban API endpoint."""

    async def test_kanban_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/agents/kanban")
        assert resp.status_code == 200
        data = resp.json()
        assert "columns" in data
        assert data["columns"] == []

    async def test_kanban_groups_by_step(self, client: AsyncClient, session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)
        run1 = TaskRun(
            issue_number="100",
            role="developer",
            status="developing",
            current_step="develop",
            total_cost_usd=Decimal("0.10"),
            started_at=now - timedelta(minutes=5),
        )
        run2 = TaskRun(
            issue_number="101",
            role="developer",
            status="developing",
            current_step="develop",
            total_cost_usd=Decimal("0.20"),
            started_at=now - timedelta(minutes=3),
        )
        run3 = TaskRun(
            issue_number="102",
            role="developer",
            status="developing",
            current_step="self_review",
            total_cost_usd=Decimal("0.15"),
            started_at=now - timedelta(minutes=1),
        )
        session.add_all([run1, run2, run3])
        await session.commit()

        resp = await client.get("/api/agents/kanban")
        assert resp.status_code == 200
        data = resp.json()
        columns = data["columns"]

        assert len(columns) == 2
        col_names = [c["name"] for c in columns]
        assert "develop" in col_names
        assert "self_review" in col_names

        develop_col = next(c for c in columns if c["name"] == "develop")
        assert develop_col["count"] == 2
        assert len(develop_col["runs"]) == 2
        assert develop_col["pipeline"] == "developer"

        # Verify run objects have all fields the UI relies on
        run_obj = develop_col["runs"][0]
        for field in ("id", "issue_number", "role", "status", "elapsed_seconds", "total_cost_usd", "pipeline_variant"):
            assert field in run_obj, f"missing field '{field}' in kanban run object"

        review_col = next(c for c in columns if c["name"] == "self_review")
        assert review_col["count"] == 1
        assert len(review_col["runs"]) == 1

    async def test_kanban_pending_column(self, client: AsyncClient, session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)
        run_none = TaskRun(
            issue_number="110",
            role="developer",
            status="pending",
            current_step=None,
            total_cost_usd=Decimal("0"),
            started_at=now,
        )
        run_agent = TaskRun(
            issue_number="111",
            role="developer",
            status="pending",
            current_step="agent",
            total_cost_usd=Decimal("0"),
            started_at=now,
        )
        session.add_all([run_none, run_agent])
        await session.commit()

        resp = await client.get("/api/agents/kanban")
        assert resp.status_code == 200
        columns = resp.json()["columns"]

        pending_cols = [c for c in columns if c["name"] == "pending"]
        assert len(pending_cols) == 1
        assert pending_cols[0]["count"] == 2

    async def test_kanban_excludes_terminal(self, client: AsyncClient, seed_data) -> None:
        _ = seed_data
        resp = await client.get("/api/agents/kanban")
        assert resp.status_code == 200
        columns = resp.json()["columns"]

        # seed_data has run3 (developing/develop) as the only non-terminal run
        total_runs = sum(len(col["runs"]) for col in columns)
        assert total_runs > 0
        # Verify terminal runs (done/failed) are not included
        for col in columns:
            for run in col["runs"]:
                assert run["status"] not in ("done", "failed")

    async def test_kanban_per_column_limit(self, client: AsyncClient, session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)
        runs = [
            TaskRun(
                issue_number=str(200 + i),
                role="developer",
                status="developing",
                current_step="develop",
                total_cost_usd=Decimal("0.01"),
                started_at=now - timedelta(minutes=i),
            )
            for i in range(5)
        ]
        session.add_all(runs)
        await session.commit()

        resp = await client.get("/api/agents/kanban?per_column=2")
        assert resp.status_code == 200
        columns = resp.json()["columns"]
        develop_col = next(c for c in columns if c["name"] == "develop")
        assert develop_col["count"] == 5
        assert len(develop_col["runs"]) == 2

    async def test_kanban_columns_ordered_by_position(self, client: AsyncClient, session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)
        run1 = TaskRun(
            issue_number="300",
            role="developer",
            status="developing",
            current_step="push",
            total_cost_usd=Decimal("0"),
            started_at=now,
        )
        run2 = TaskRun(
            issue_number="301",
            role="developer",
            status="developing",
            current_step="sync",
            total_cost_usd=Decimal("0"),
            started_at=now,
        )
        session.add_all([run1, run2])
        await session.commit()

        resp = await client.get("/api/agents/kanban")
        assert resp.status_code == 200
        columns = resp.json()["columns"]
        assert len(columns) == 2
        # sync comes before push in the developer pipeline
        assert columns[0]["name"] == "sync"
        assert columns[1]["name"] == "push"
        assert columns[0]["position"] < columns[1]["position"]

    async def test_kanban_rejects_invalid_per_column(self, client: AsyncClient) -> None:
        resp = await client.get("/api/agents/kanban?per_column=0")
        assert resp.status_code == 422

        resp = await client.get("/api/agents/kanban?per_column=-1")
        assert resp.status_code == 422

    async def test_kanban_unknown_step(self, client: AsyncClient, session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)
        run = TaskRun(
            issue_number="400",
            role="developer",
            status="developing",
            current_step="nonexistent_step",
            total_cost_usd=Decimal("0"),
            started_at=now,
        )
        session.add(run)
        await session.commit()

        resp = await client.get("/api/agents/kanban")
        assert resp.status_code == 200
        columns = resp.json()["columns"]
        assert len(columns) == 1
        assert columns[0]["name"] == "nonexistent_step"
        assert columns[0]["pipeline"] == "unknown"
        assert columns[0]["position"] == 999

    async def test_kanban_mixed_variants(self, client: AsyncClient, session: AsyncSession) -> None:
        """Runs from different pipeline variants at a shared step get separate columns."""
        now = datetime.now(timezone.utc)
        # Developer run at 'commit'
        dev_run = TaskRun(
            issue_number="500",
            role="developer",
            status="developing",
            current_step="commit",
            total_cost_usd=Decimal("0.10"),
            started_at=now - timedelta(minutes=5),
        )
        # Address-review run at 'commit' with step history containing
        # address_review-only steps so variant detection identifies it correctly.
        ar_run = TaskRun(
            issue_number="501",
            role="developer",
            status="developing",
            current_step="commit",
            pr_number=42,
            total_cost_usd=Decimal("0.05"),
            started_at=now - timedelta(minutes=2),
        )
        session.add_all([dev_run, ar_run])
        await session.flush()

        # Add step execution history with an address_review-only step
        ar_step = StepExecution(
            task_run_id=ar_run.id,
            step_name="address_review",
            status="done",
            started_at=now - timedelta(minutes=3),
            ended_at=now - timedelta(minutes=2),
        )
        session.add(ar_step)
        await session.commit()

        resp = await client.get("/api/agents/kanban")
        assert resp.status_code == 200
        columns = resp.json()["columns"]

        # Both are at 'commit' but different variants -> separate columns
        commit_cols = [c for c in columns if c["name"] == "commit"]
        assert len(commit_cols) == 2

        pipelines = {c["pipeline"] for c in commit_cols}
        assert "developer" in pipelines
        assert "address_review" in pipelines

        # Each column has exactly one run
        for col in commit_cols:
            assert col["count"] == 1


class TestGetKanbanColumnsDirect:
    """Direct unit tests for get_kanban_columns (bypasses API router)."""

    async def test_empty_db_returns_empty(self, session: AsyncSession) -> None:
        from sova.dashboard.services.control_service import get_kanban_columns

        result = await get_kanban_columns(session)
        assert result == []

    async def test_single_run(self, session: AsyncSession) -> None:
        from sova.dashboard.services.control_service import get_kanban_columns

        now = datetime.now(timezone.utc)
        run = TaskRun(
            issue_number="10",
            role="developer",
            status="developing",
            current_step="develop",
            total_cost_usd=Decimal("0.05"),
            started_at=now - timedelta(minutes=2),
        )
        session.add(run)
        await session.commit()

        cols = await get_kanban_columns(session)
        assert len(cols) == 1
        assert cols[0]["name"] == "develop"
        assert cols[0]["pipeline"] == "developer"
        assert cols[0]["count"] == 1
        assert len(cols[0]["runs"]) == 1
        assert cols[0]["runs"][0]["issue_number"] == "10"

    async def test_pending_variants(self, session: AsyncSession) -> None:
        from sova.dashboard.services.control_service import get_kanban_columns

        now = datetime.now(timezone.utc)
        session.add_all(
            [
                TaskRun(
                    issue_number="20",
                    role="developer",
                    status="pending",
                    current_step=None,
                    total_cost_usd=Decimal("0"),
                    started_at=now,
                ),
                TaskRun(
                    issue_number="21",
                    role="developer",
                    status="pending",
                    current_step="agent",
                    total_cost_usd=Decimal("0"),
                    started_at=now,
                ),
            ]
        )
        await session.commit()

        cols = await get_kanban_columns(session)
        assert len(cols) == 1
        assert cols[0]["name"] == "pending"
        assert cols[0]["count"] == 2
        assert len(cols[0]["runs"]) == 2

    async def test_researcher_variant_detection(self, session: AsyncSession) -> None:
        from sova.dashboard.services.control_service import get_kanban_columns

        now = datetime.now(timezone.utc)
        run = TaskRun(
            issue_number="30",
            role="researcher",
            status="researching",
            current_step="research",
            total_cost_usd=Decimal("0"),
            started_at=now,
        )
        session.add(run)
        await session.flush()

        step = StepExecution(
            task_run_id=run.id,
            step_name="research",
            status="running",
            started_at=now,
        )
        session.add(step)
        await session.commit()

        cols = await get_kanban_columns(session)
        assert len(cols) == 1
        assert cols[0]["runs"][0]["pipeline_variant"] == "researcher"

    async def test_address_review_variant_detection(self, session: AsyncSession) -> None:
        from sova.dashboard.services.control_service import get_kanban_columns

        now = datetime.now(timezone.utc)
        run = TaskRun(
            issue_number="31",
            role="developer",
            status="developing",
            current_step="commit",
            pr_number=99,
            total_cost_usd=Decimal("0"),
            started_at=now,
        )
        session.add(run)
        await session.flush()

        session.add(
            StepExecution(
                task_run_id=run.id,
                step_name="address_review",
                status="done",
                started_at=now - timedelta(minutes=1),
                ended_at=now,
            )
        )
        await session.commit()

        cols = await get_kanban_columns(session)
        assert len(cols) == 1
        assert cols[0]["runs"][0]["pipeline_variant"] == "address_review"

    async def test_per_column_limit(self, session: AsyncSession) -> None:
        from sova.dashboard.services.control_service import get_kanban_columns

        now = datetime.now(timezone.utc)
        session.add_all(
            [
                TaskRun(
                    issue_number=str(40 + i),
                    role="developer",
                    status="developing",
                    current_step="develop",
                    total_cost_usd=Decimal("0"),
                    started_at=now - timedelta(minutes=i),
                )
                for i in range(5)
            ]
        )
        await session.commit()

        cols = await get_kanban_columns(session, per_column=2)
        assert len(cols) == 1
        assert cols[0]["count"] == 5
        assert len(cols[0]["runs"]) == 2

    async def test_columns_sorted_by_position(self, session: AsyncSession) -> None:
        from sova.dashboard.services.control_service import get_kanban_columns

        now = datetime.now(timezone.utc)
        session.add_all(
            [
                TaskRun(
                    issue_number="50",
                    role="developer",
                    status="developing",
                    current_step="push",
                    total_cost_usd=Decimal("0"),
                    started_at=now,
                ),
                TaskRun(
                    issue_number="51",
                    role="developer",
                    status="developing",
                    current_step="sync",
                    total_cost_usd=Decimal("0"),
                    started_at=now,
                ),
            ]
        )
        await session.commit()

        cols = await get_kanban_columns(session)
        assert len(cols) == 2
        assert cols[0]["name"] == "sync"
        assert cols[1]["name"] == "push"


class TestWorkAPI:
    """Tests for the new work API endpoints."""

    async def test_work_active_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/work/active")
        assert resp.status_code == 200
        data = resp.json()
        assert "tasks" in data
        assert isinstance(data["tasks"], list)

    async def test_work_history_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/work/history")
        assert resp.status_code == 200
        data = resp.json()
        assert "tasks" in data

    async def test_work_history_with_filters(self, client: AsyncClient) -> None:
        resp = await client.get("/api/work/history?status=done&role=developer&limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert "tasks" in data

    async def test_work_summary(self, client: AsyncClient) -> None:
        resp = await client.get("/api/work/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "done" in data
        assert "failed" in data
        assert "active" in data

    async def test_work_detail_not_found(self, client: AsyncClient) -> None:
        resp = await client.get("/api/work/999")
        assert resp.status_code == 404
        data = resp.json()
        assert "detail" in data

    async def test_work_mark_failed_not_found(self, client: AsyncClient) -> None:
        resp = await client.post("/api/work/999/mark-failed")
        assert resp.status_code == 404
        data = resp.json()
        assert "detail" in data

    async def test_work_mark_failed_stops_agent(self, client: AsyncClient, session: AsyncSession) -> None:
        """Mark-failed should call stop_agent before updating the DB."""
        from unittest.mock import AsyncMock, patch

        now = datetime.now(timezone.utc)
        run = TaskRun(issue_number="99", role="developer", status="running", started_at=now)
        session.add(run)
        await session.flush()
        run_id = run.id

        with patch("sova.dashboard.services.control_service.stop_agent", new_callable=AsyncMock) as mock_stop:
            mock_stop.return_value = {"status": "not_found"}
            resp = await client.post(f"/api/work/{run_id}/mark-failed")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "failed"
        mock_stop.assert_awaited_once_with(run_id=run_id)

    async def test_active_grouped_excludes_superseded_paused_runs(
        self, client: AsyncClient, session: AsyncSession
    ) -> None:
        """Paused runs should not appear in Active when a later run completed the issue."""
        now = datetime.now(timezone.utc)
        session.add(
            TaskRun(
                issue_number="73",
                role="developer",
                status="paused",
                current_step="develop",
                started_at=now - timedelta(hours=3),
            )
        )
        session.add(
            TaskRun(
                issue_number="73",
                role="developer",
                status="done",
                current_step="complete",
                started_at=now - timedelta(hours=1),
                ended_at=now,
            )
        )
        await session.commit()

        resp = await client.get("/api/work/active-grouped")
        data = resp.json()
        issue_numbers = [g["issue_number"] for g in data.get("issues", [])]
        assert "73" not in issue_numbers

    async def test_active_grouped_shows_paused_when_no_done_run(
        self, client: AsyncClient, session: AsyncSession
    ) -> None:
        """Paused runs should appear in Active when no later run completed the issue."""
        now = datetime.now(timezone.utc)
        session.add(
            TaskRun(
                issue_number="99",
                role="developer",
                status="paused",
                current_step="develop",
                started_at=now - timedelta(hours=1),
            )
        )
        await session.commit()

        resp = await client.get("/api/work/active-grouped")
        data = resp.json()
        issue_numbers = [g["issue_number"] for g in data.get("issues", [])]
        assert "99" in issue_numbers

    async def test_summary_active_count_excludes_superseded(self, client: AsyncClient, session: AsyncSession) -> None:
        """Summary active count should match the Active tab (exclude superseded)."""
        now = datetime.now(timezone.utc)
        session.add(TaskRun(issue_number="50", role="developer", status="paused", started_at=now - timedelta(hours=2)))
        session.add(TaskRun(issue_number="50", role="developer", status="done", started_at=now, ended_at=now))
        await session.commit()

        resp = await client.get("/api/work/summary")
        data = resp.json()
        assert data["active"] == 0

    async def test_work_detail_found(self, client: AsyncClient, session: AsyncSession) -> None:
        """get_work_detail returns run with steps and pipeline progress."""
        now = datetime.now(timezone.utc)
        run = TaskRun(
            issue_number="77",
            role="developer",
            status="running",
            current_step="develop",
            total_cost_usd=Decimal("0.50"),
            started_at=now - timedelta(minutes=10),
        )
        session.add(run)
        await session.flush()

        step = StepExecution(
            task_run_id=run.id,
            step_name="sync",
            status="done",
            cost_usd=Decimal("0.01"),
            started_at=now - timedelta(minutes=10),
            ended_at=now - timedelta(minutes=9),
        )
        session.add(step)
        await session.commit()

        resp = await client.get(f"/api/work/{run.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert "run" in data
        assert "steps" in data
        assert "pipeline" in data
        assert data["run"]["issue_number"] == "77"
        assert data["run"]["role"] == "developer"
        assert len(data["steps"]) == 1
        assert data["steps"][0]["step_name"] == "sync"

    async def test_work_detail_researcher_variant(self, client: AsyncClient, session: AsyncSession) -> None:
        """get_work_detail detects researcher variant from step history."""
        now = datetime.now(timezone.utc)
        run = TaskRun(
            issue_number="78",
            role="researcher",
            status="running",
            current_step="research",
            total_cost_usd=Decimal("0.10"),
            started_at=now,
        )
        session.add(run)
        await session.flush()

        step = StepExecution(
            task_run_id=run.id,
            step_name="research",
            status="running",
            cost_usd=Decimal("0.05"),
            started_at=now,
        )
        session.add(step)
        await session.commit()

        resp = await client.get(f"/api/work/{run.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["pipeline"]["pipeline_variant"] == "researcher"

    async def test_work_detail_address_review_variant(self, client: AsyncClient, session: AsyncSession) -> None:
        """get_work_detail detects address_review variant from step history."""
        now = datetime.now(timezone.utc)
        run = TaskRun(
            issue_number="79",
            role="developer",
            status="running",
            current_step="address_review",
            pr_number=42,
            total_cost_usd=Decimal("0.10"),
            started_at=now,
        )
        session.add(run)
        await session.flush()

        step = StepExecution(
            task_run_id=run.id,
            step_name="address_review",
            status="running",
            cost_usd=Decimal("0.05"),
            started_at=now,
        )
        session.add(step)
        await session.commit()

        resp = await client.get(f"/api/work/{run.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["pipeline"]["pipeline_variant"] == "address_review"

    async def test_work_history_with_researcher_run(self, client: AsyncClient, session: AsyncSession) -> None:
        """Work history correctly identifies researcher variant."""
        now = datetime.now(timezone.utc)
        run = TaskRun(
            issue_number="80",
            role="researcher",
            status="done",
            current_step="complete",
            total_cost_usd=Decimal("0.05"),
            started_at=now - timedelta(minutes=5),
            ended_at=now,
        )
        session.add(run)
        await session.flush()

        step = StepExecution(
            task_run_id=run.id,
            step_name="research",
            status="done",
            cost_usd=Decimal("0.05"),
            started_at=now - timedelta(minutes=5),
            ended_at=now,
        )
        session.add(step)
        await session.commit()

        resp = await client.get("/api/work/history?role=researcher")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["tasks"]) >= 1
        researcher_task = next(t for t in data["tasks"] if t["issue_number"] == "80")
        assert researcher_task["pipeline_variant"] == "researcher"

    async def test_work_history_with_address_review_run(self, client: AsyncClient, session: AsyncSession) -> None:
        """Work history correctly identifies address_review variant from steps."""
        now = datetime.now(timezone.utc)
        run = TaskRun(
            issue_number="81",
            role="developer",
            status="done",
            current_step="complete",
            pr_number=55,
            total_cost_usd=Decimal("0.08"),
            started_at=now - timedelta(minutes=3),
            ended_at=now,
        )
        session.add(run)
        await session.flush()

        step = StepExecution(
            task_run_id=run.id,
            step_name="address_review",
            status="done",
            cost_usd=Decimal("0.08"),
            started_at=now - timedelta(minutes=3),
            ended_at=now,
        )
        session.add(step)
        await session.commit()

        resp = await client.get("/api/work/history")
        assert resp.status_code == 200
        data = resp.json()
        ar_task = next(t for t in data["tasks"] if t["issue_number"] == "81")
        assert ar_task["pipeline_variant"] == "address_review"


class TestLogsAPI:
    """Tests for the logs API endpoints."""

    async def test_logs_empty(self, client: AsyncClient, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("sova.dashboard.routers.logs.get_project_dir", lambda: tmp_path)
        resp = await client.get("/api/logs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["entries"] == []
        assert data["total"] == 0

    async def test_logs_with_data(self, client: AsyncClient, tmp_path, monkeypatch) -> None:
        # Write a test log file
        log_dir = tmp_path / ".claude"
        log_dir.mkdir()
        log_file = log_dir / "sova.log"
        import json

        lines = [
            json.dumps(
                {
                    "level": "INFO",
                    "message": "Agent started",
                    "component": "core",
                    "timestamp": "2026-01-01T10:00:00",
                }
            ),
            json.dumps(
                {
                    "level": "ERROR",
                    "message": "Test failed",
                    "component": "runner",
                    "timestamp": "2026-01-01T10:01:00",
                }
            ),
            json.dumps(
                {
                    "level": "INFO",
                    "message": "Agent completed",
                    "component": "core",
                    "timestamp": "2026-01-01T10:02:00",
                }
            ),
        ]
        log_file.write_text("\n".join(lines) + "\n")

        # Patch get_project_dir to return tmp_path
        monkeypatch.setattr("sova.dashboard.routers.logs.get_project_dir", lambda: tmp_path)

        resp = await client.get("/api/logs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert len(data["entries"]) == 3
        # Most recent first
        assert data["entries"][0]["message"] == "Agent completed"

    async def test_logs_filter_level(self, client: AsyncClient, tmp_path, monkeypatch) -> None:
        import json

        log_dir = tmp_path / ".claude"
        log_dir.mkdir()
        log_file = log_dir / "sova.log"
        lines = [
            json.dumps({"level": "INFO", "message": "ok", "component": "core", "timestamp": "2026-01-01T10:00:00"}),
            json.dumps({"level": "ERROR", "message": "bad", "component": "core", "timestamp": "2026-01-01T10:01:00"}),
        ]
        log_file.write_text("\n".join(lines) + "\n")

        monkeypatch.setattr("sova.dashboard.routers.logs.get_project_dir", lambda: tmp_path)

        resp = await client.get("/api/logs?level=ERROR")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["entries"][0]["message"] == "bad"

    async def test_logs_components(self, client: AsyncClient, tmp_path, monkeypatch) -> None:
        import json

        log_dir = tmp_path / ".claude"
        log_dir.mkdir()
        log_file = log_dir / "sova.log"
        lines = [
            json.dumps({"level": "INFO", "message": "x", "component": "core"}),
            json.dumps({"level": "INFO", "message": "y", "component": "runner"}),
        ]
        log_file.write_text("\n".join(lines) + "\n")

        monkeypatch.setattr("sova.dashboard.routers.logs.get_project_dir", lambda: tmp_path)

        resp = await client.get("/api/logs/components")
        assert resp.status_code == 200
        data = resp.json()
        assert "core" in data["components"]
        assert "runner" in data["components"]

    async def test_logs_page_renders(self, client: AsyncClient) -> None:
        resp = await client.get("/logs")
        assert resp.status_code == 200
        assert b"Logs" in resp.content


class TestSettingsAPI:
    """Tests for the settings API endpoints."""

    async def test_get_config(self, client: AsyncClient) -> None:
        resp = await client.get("/api/settings/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "config" in data

    async def test_invariants_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/settings/invariants")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["invariants"], list)

    async def test_invariants_with_files(self, client: AsyncClient, tmp_path, monkeypatch) -> None:
        inv_dir = tmp_path / "invariants"
        inv_dir.mkdir()
        (inv_dir / "check-types.sh").write_text("#!/bin/bash\necho ok")
        (inv_dir / "check-types.sh").chmod(0o755)

        monkeypatch.setattr("sova.dashboard.routers.settings.get_project_dir", lambda: tmp_path)

        resp = await client.get("/api/settings/invariants")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["invariants"]) == 1
        assert data["invariants"][0]["name"] == "check-types.sh"
        assert data["invariants"][0]["executable"] is True

    async def test_personas(self, client: AsyncClient) -> None:
        resp = await client.get("/api/settings/personas")
        assert resp.status_code == 200
        data = resp.json()
        assert "personas" in data
        assert "detected" in data

    async def test_settings_page_renders(self, client: AsyncClient) -> None:
        resp = await client.get("/settings")
        assert resp.status_code == 200
        assert b"Settings" in resp.content

    async def test_get_config_grouped(self, client: AsyncClient) -> None:
        resp = await client.get("/api/settings/config/grouped")
        assert resp.status_code == 200
        data = resp.json()
        assert "groups" in data
        groups = data["groups"]
        assert isinstance(groups, list)
        if groups:
            g = groups[0]
            assert "id" in g
            assert "label" in g
            assert "settings" in g
            assert isinstance(g["settings"], list)
            if g["settings"]:
                s = g["settings"][0]
                assert "key" in s
                assert "label" in s
                assert "value" in s
                assert "value_type" in s

    async def test_grouped_config_has_descriptions(self, client: AsyncClient) -> None:
        resp = await client.get("/api/settings/config/grouped")
        data = resp.json()
        groups = data["groups"]
        has_description = any(s.get("description") for g in groups for s in g["settings"])
        assert has_description, "At least some settings should have descriptions"


class TestSetupAPI:
    async def test_browse_home(self, client: AsyncClient) -> None:
        resp = await client.post("/api/setup/browse", json={"path": ""})
        assert resp.status_code == 200
        data = resp.json()
        assert "current" in data
        assert "entries" in data
        assert isinstance(data["entries"], list)

    async def test_browse_specific_dir(self, client: AsyncClient, tmp_path) -> None:
        sub = tmp_path / "myproject"
        sub.mkdir()
        (sub / ".git").mkdir()
        resp = await client.post("/api/setup/browse", json={"path": str(tmp_path)})
        assert resp.status_code == 200
        data = resp.json()
        assert data["current"] == str(tmp_path)
        project_entry = next((e for e in data["entries"] if e["name"] == "myproject"), None)
        assert project_entry is not None
        assert project_entry["is_project"] is True

    async def test_scan_project(self, client: AsyncClient, tmp_path) -> None:
        # Create a minimal Python project
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n")
        (tmp_path / ".git").mkdir()
        resp = await client.post("/api/setup/scan", json={"project_path": str(tmp_path)})
        assert resp.status_code == 200
        data = resp.json()
        assert data["project_name"] == tmp_path.name
        assert "python" in data["tech_stack"]
        assert data["already_installed"] is False

    async def test_configure_project(self, client: AsyncClient, tmp_path) -> None:
        import sova.config.registry as reg_mod

        orig_file = reg_mod._REGISTRY_FILE
        orig_dir = reg_mod._REGISTRY_DIR
        reg_file = tmp_path / "reg" / "projects.json"
        reg_mod._REGISTRY_FILE = reg_file
        reg_mod._REGISTRY_DIR = tmp_path / "reg"

        project = tmp_path / "proj"
        project.mkdir()

        resp = await client.post(
            "/api/setup/configure",
            json={
                "project_path": str(project),
                "github_repo": "user/test",
                "base_branch": "main",
                "agent_model": "opus",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert (project / "sova.toml").exists()
        toml_content = (project / "sova.toml").read_text()
        assert 'github_repo = "user/test"' in toml_content

        reg_mod._REGISTRY_FILE = orig_file
        reg_mod._REGISTRY_DIR = orig_dir

    async def test_install_nonexistent_directory(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/setup/install",
            json={"project_path": "/tmp/nonexistent_sova_test_dir_xyz"},
        )
        assert resp.status_code == 404
        data = resp.json()
        assert "Directory not found" in data["detail"]

    async def test_configure_nonexistent_directory(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/setup/configure",
            json={"project_path": "/tmp/nonexistent_sova_test_dir_xyz", "github_repo": "u/r"},
        )
        assert resp.status_code == 404
        data = resp.json()
        assert "Directory not found" in data["detail"]

    async def test_sync_commands_success(self, client: AsyncClient, tmp_path, monkeypatch) -> None:
        from unittest.mock import patch

        from sova.commands.distribution import UpdateResult

        monkeypatch.setattr("sova.dashboard.routers.setup.get_project_dir", lambda: tmp_path)
        fake_result = UpdateResult(updated=3, skipped=1, conflicts=["foo.md"])
        with patch("sova.dashboard.routers.setup.asyncio.to_thread", return_value=fake_result):
            resp = await client.post("/api/setup/commands/sync")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["updated"] == 3
        assert data["skipped"] == 1
        assert data["conflicts"] == ["foo.md"]

    async def test_sync_commands_no_project(self, client: AsyncClient, monkeypatch) -> None:
        monkeypatch.setattr("sova.dashboard.routers.setup.get_project_dir", lambda: None)
        resp = await client.post("/api/setup/commands/sync")
        assert resp.status_code == 400
        assert "No active project" in resp.json()["detail"]

    async def test_sync_commands_bad_config(self, client: AsyncClient, tmp_path, monkeypatch) -> None:
        from unittest.mock import patch

        monkeypatch.setattr("sova.dashboard.routers.setup.get_project_dir", lambda: tmp_path)
        with patch(
            "sova.config.loader.load_config",
            side_effect=FileNotFoundError("sova.toml not found"),
        ):
            resp = await client.post("/api/setup/commands/sync")
        assert resp.status_code == 400
        assert "Failed to load project config" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Work Service -- direct service tests
# ---------------------------------------------------------------------------


class TestWorkServiceDirect:
    """Direct tests for work_service functions, bypassing the API layer."""

    async def test_get_work_history_all_statuses(self, session: AsyncSession) -> None:
        from sova.dashboard.services.work_service import get_work_history

        now = datetime.now(timezone.utc)
        async with session.begin():
            session.add(TaskRun(issue_number="1", role="dev", status="done", started_at=now, ended_at=now))
            session.add(TaskRun(issue_number="2", role="dev", status="failed", started_at=now, ended_at=now))
            session.add(TaskRun(issue_number="3", role="dev", status="interrupted", started_at=now, ended_at=now))
            session.add(TaskRun(issue_number="4", role="dev", status="developing"))

        result = await get_work_history(session)
        statuses = {r["status"] for r in result["tasks"]}
        assert statuses == {"done", "failed", "interrupted"}
        assert all(r["issue_number"] != "4" for r in result["tasks"])

    async def test_get_work_history_status_filter(self, session: AsyncSession) -> None:
        from sova.dashboard.services.work_service import get_work_history

        now = datetime.now(timezone.utc)
        async with session.begin():
            session.add(TaskRun(issue_number="1", role="dev", status="done", started_at=now, ended_at=now))
            session.add(TaskRun(issue_number="2", role="dev", status="failed", started_at=now, ended_at=now))

        result = await get_work_history(session, status="done")
        assert len(result["tasks"]) == 1
        assert result["tasks"][0]["status"] == "done"

    async def test_get_work_history_role_filter(self, session: AsyncSession) -> None:
        from sova.dashboard.services.work_service import get_work_history

        now = datetime.now(timezone.utc)
        async with session.begin():
            session.add(TaskRun(issue_number="1", role="developer", status="done", started_at=now, ended_at=now))
            session.add(TaskRun(issue_number="2", role="triage", status="done", started_at=now, ended_at=now))

        result = await get_work_history(session, role="triage")
        assert len(result["tasks"]) == 1
        assert result["tasks"][0]["role"] == "triage"

    async def test_get_work_history_limit_capped(self, session: AsyncSession) -> None:
        from sova.dashboard.services.work_service import get_work_history

        now = datetime.now(timezone.utc)
        async with session.begin():
            for i in range(5):
                session.add(TaskRun(issue_number=str(i), role="dev", status="done", started_at=now, ended_at=now))

        result = await get_work_history(session, limit=2)
        assert len(result["tasks"]) == 2

    async def test_get_work_history_accepts_large_limit(self, session: AsyncSession) -> None:
        from sova.dashboard.services.work_service import get_work_history

        now = datetime.now(timezone.utc)
        async with session.begin():
            session.add(TaskRun(issue_number="1", role="dev", status="done", started_at=now, ended_at=now))

        result = await get_work_history(session, limit=999)
        assert len(result["tasks"]) == 1

    async def test_get_work_history_offset(self, session: AsyncSession) -> None:
        from sova.dashboard.services.work_service import get_work_history

        now = datetime.now(timezone.utc)
        async with session.begin():
            for i in range(10):
                session.add(TaskRun(issue_number=str(i), role="dev", status="done", started_at=now, ended_at=now))

        page1 = await get_work_history(session, limit=3, offset=0)
        page2 = await get_work_history(session, limit=3, offset=3)
        assert len(page1["tasks"]) == 3
        assert len(page2["tasks"]) == 3
        assert page1["total"] == 10
        assert page2["total"] == 10
        ids_1 = {r["id"] for r in page1["tasks"]}
        ids_2 = {r["id"] for r in page2["tasks"]}
        assert ids_1.isdisjoint(ids_2)

    async def test_get_work_history_offset_past_end(self, session: AsyncSession) -> None:
        from sova.dashboard.services.work_service import get_work_history

        now = datetime.now(timezone.utc)
        async with session.begin():
            session.add(TaskRun(issue_number="1", role="dev", status="done", started_at=now, ended_at=now))

        result = await get_work_history(session, limit=10, offset=100)
        assert len(result["tasks"]) == 0
        assert result["total"] == 1

    async def test_get_work_history_non_pipeline_role_shows_none_steps(self, session: AsyncSession) -> None:
        from sova.dashboard.services.work_service import get_work_history

        now = datetime.now(timezone.utc)
        async with session.begin():
            session.add(TaskRun(issue_number="50", role="triage", status="done", started_at=now, ended_at=now))
            session.add(TaskRun(issue_number="51", role="developer", status="done", started_at=now, ended_at=now))

        result = await get_work_history(session)
        tasks_by_issue = {t["issue_number"]: t for t in result["tasks"]}
        assert tasks_by_issue["50"]["total_steps_possible"] is None
        assert tasks_by_issue["51"]["total_steps_possible"] == 15

    async def test_work_history_endpoint_pagination(self, client: AsyncClient) -> None:
        resp = await client.get("/api/work/history?limit=15&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert "tasks" in data
        assert "total" in data

    async def test_get_work_summary(self, session: AsyncSession) -> None:
        from sova.dashboard.services.work_service import get_work_summary

        now = datetime.now(timezone.utc)
        async with session.begin():
            session.add(TaskRun(issue_number="1", role="dev", status="done", started_at=now, ended_at=now))
            session.add(TaskRun(issue_number="2", role="dev", status="failed", started_at=now, ended_at=now))
            session.add(TaskRun(issue_number="3", role="dev", status="developing", started_at=now))

        summary = await get_work_summary(session)
        assert summary["total"] == 3
        assert summary["done"] == 1
        assert summary["failed"] == 1
        assert summary["active"] == 1

    async def test_mark_run_failed(self, session: AsyncSession) -> None:
        from sova.dashboard.services.work_service import mark_run_failed

        async with session.begin():
            run = TaskRun(issue_number="1", role="dev", status="developing")
            session.add(run)
            await session.flush()
            run_id = run.id

        result = await mark_run_failed(session, run_id, "Manually stopped")
        assert result is not None
        assert result["status"] == "failed"
        assert result["error_message"] == "Manually stopped"

    async def test_mark_run_failed_rejects_terminal(self, session: AsyncSession) -> None:
        from sova.dashboard.services.work_service import mark_run_failed

        async with session.begin():
            run = TaskRun(issue_number="1", role="dev", status="done")
            session.add(run)
            await session.flush()
            run_id = run.id

        result = await mark_run_failed(session, run_id)
        assert result is not None
        assert "error" in result
        assert "already done" in result["error"]

    async def test_mark_run_failed_returns_none_for_missing(self, session: AsyncSession) -> None:
        from sova.dashboard.services.work_service import mark_run_failed

        result = await mark_run_failed(session, 999)
        assert result is None

    async def test_list_runs_status_filter(self, session: AsyncSession) -> None:
        from sova.dashboard.services.work_service import list_runs

        now = datetime.now(timezone.utc)
        async with session.begin():
            session.add(TaskRun(issue_number="1", role="dev", status="done", started_at=now))
            session.add(TaskRun(issue_number="2", role="dev", status="running", started_at=now))

        result = await list_runs(session, status="running")
        assert len(result) == 1
        assert result[0]["status"] == "running"

    async def test_get_runs_for_issue(self, session: AsyncSession) -> None:
        from sova.dashboard.services.work_service import get_runs_for_issue

        now = datetime.now(timezone.utc)
        async with session.begin():
            session.add(TaskRun(issue_number="42", role="dev", status="done", started_at=now))
            session.add(TaskRun(issue_number="42", role="reviewer", status="done", started_at=now))
            session.add(TaskRun(issue_number="99", role="dev", status="done", started_at=now))

        result = await get_runs_for_issue(session, "#42")
        assert len(result) == 2
        assert all(r["issue_number"] == "42" for r in result)

    async def test_terminal_set_includes_interrupted(self) -> None:
        from sova.dashboard.services.work_service import _TERMINAL

        assert "interrupted" in _TERMINAL
        assert "done" in _TERMINAL
        assert "failed" in _TERMINAL
        assert "rejected" in _TERMINAL
        assert "developing" not in _TERMINAL

    def test_dedupe_steps_keeps_latest_per_name(self) -> None:
        from sova.dashboard.services.work_service import _dedupe_steps

        s1 = StepExecution(id=1, task_run_id=1, step_name="develop", status="failed", retry_count=0)
        s2 = StepExecution(id=2, task_run_id=1, step_name="develop", status="done", retry_count=1)
        s3 = StepExecution(id=3, task_run_id=1, step_name="push", status="done", retry_count=0)

        result = _dedupe_steps([s1, s2, s3])
        assert len(result) == 2
        assert result[0].id == 2
        assert result[0].step_name == "develop"
        assert result[0].retry_count == 1
        assert result[1].id == 3
        assert result[1].step_name == "push"

    def test_dedupe_steps_preserves_order(self) -> None:
        from sova.dashboard.services.work_service import _dedupe_steps

        s1 = StepExecution(id=1, task_run_id=1, step_name="sync", status="done", retry_count=0)
        s2 = StepExecution(id=2, task_run_id=1, step_name="develop", status="done", retry_count=0)

        result = _dedupe_steps([s1, s2])
        assert len(result) == 2
        assert result[0].step_name == "sync"
        assert result[1].step_name == "develop"

    async def test_get_work_detail_with_steps(self, session: AsyncSession) -> None:
        from sova.dashboard.services.work_service import get_work_detail

        now = datetime.now(timezone.utc)
        async with session.begin():
            run = TaskRun(issue_number="10", role="developer", status="developing", started_at=now)
            session.add(run)
            await session.flush()
            session.add(
                StepExecution(
                    id=1,
                    task_run_id=run.id,
                    step_name="sync",
                    status="done",
                    retry_count=0,
                    started_at=now,
                )
            )
            session.add(
                StepExecution(
                    id=2,
                    task_run_id=run.id,
                    step_name="develop",
                    status="failed",
                    retry_count=0,
                    started_at=now,
                )
            )
            session.add(
                StepExecution(
                    id=3,
                    task_run_id=run.id,
                    step_name="develop",
                    status="done",
                    retry_count=1,
                    started_at=now,
                )
            )

        result = await get_work_detail(session, run.id)
        assert result is not None
        assert result["run"]["id"] == run.id
        # Deduplication: two unique step names (sync + develop), latest develop kept
        assert len(result["steps"]) == 2
        step_names = [s["step_name"] for s in result["steps"]]
        assert step_names == ["sync", "develop"]
        assert result["steps"][1]["retry_count"] == 1
        assert "pipeline" in result

    async def test_get_run_steps_deduplicates(self, session: AsyncSession) -> None:
        from sova.dashboard.services.work_service import get_run_steps

        now = datetime.now(timezone.utc)
        async with session.begin():
            run = TaskRun(issue_number="11", role="developer", status="done", started_at=now, ended_at=now)
            session.add(run)
            await session.flush()
            session.add(
                StepExecution(
                    id=10,
                    task_run_id=run.id,
                    step_name="push",
                    status="failed",
                    retry_count=0,
                    started_at=now,
                )
            )
            session.add(
                StepExecution(
                    id=11,
                    task_run_id=run.id,
                    step_name="push",
                    status="done",
                    retry_count=1,
                    started_at=now,
                )
            )

        steps = await get_run_steps(session, run.id)
        assert len(steps) == 1
        assert steps[0]["step_name"] == "push"
        assert steps[0]["retry_count"] == 1

        # Non-deduplicated path returns all retry attempts
        all_steps = await get_run_steps(session, run.id, deduplicate=False)
        assert len(all_steps) == 2
        assert all_steps[0]["retry_count"] == 0
        assert all_steps[0]["status"] == "failed"
        assert all_steps[1]["retry_count"] == 1
        assert all_steps[1]["status"] == "done"


# ---------------------------------------------------------------------------
# Finalize guard -- terminal status not overwritten
# ---------------------------------------------------------------------------


class TestFinalizeTaskRunGuard:
    """_finalize_task_run must not overwrite an already-terminal run."""

    async def test_finalize_skips_already_failed_run(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _finalize_task_run
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="88",
                    role="developer",
                    status="failed",
                    error_message="Manually abandoned",
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        mock_agent = MagicMock(spec=AgentState)
        mock_agent.last_result_cost = 0.5
        mock_agent.project_dir = None

        await _finalize_task_run(run_id, exit_code=1, agent=mock_agent)

        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed.status == "failed"
                assert refreshed.error_message == "Manually abandoned"

    async def test_finalize_skips_done_run(self) -> None:
        """Already-done runs must not be overwritten (even with different exit code)."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _finalize_task_run
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="91",
                    role="developer",
                    status="done",
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        mock_agent = MagicMock(spec=AgentState)
        mock_agent.last_result_cost = 0
        mock_agent.project_dir = None

        await _finalize_task_run(run_id, exit_code=1, agent=mock_agent)

        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed.status == "done"
                assert refreshed.error_message is None

    async def test_finalize_skips_paused_run(self) -> None:
        """WorkflowEngine may set status to 'paused' (budget exceeded); dashboard must not overwrite."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _finalize_task_run
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="89",
                    role="developer",
                    status="paused",
                    error_message="Budget exceeded: $10.50",
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        mock_agent = MagicMock(spec=AgentState)
        mock_agent.last_result_cost = 10.5
        mock_agent.project_dir = None

        await _finalize_task_run(run_id, exit_code=0, agent=mock_agent)

        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed.status == "paused"
                assert refreshed.error_message == "Budget exceeded: $10.50"

    async def test_finalize_still_updates_cost_on_terminal(self) -> None:
        """Even when status is terminal, stream cost should be written (more accurate)."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _finalize_task_run
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="90",
                    role="developer",
                    status="done",
                    total_cost_usd=Decimal("5.00"),
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        mock_agent = MagicMock(spec=AgentState)
        mock_agent.last_result_cost = 7.50
        mock_agent.project_dir = None
        mock_agent.issue = "90"

        await _finalize_task_run(run_id, exit_code=0, agent=mock_agent)

        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed.status == "done"
                assert float(refreshed.total_cost_usd) == 7.50


# ---------------------------------------------------------------------------
# Merge-aware finalization
# ---------------------------------------------------------------------------


class TestMergeAwareFinalization:
    """_check_pr_merged_on_failure should detect merged PRs for integration commands."""

    async def test_detects_merged_pr(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_lifecycle import _check_pr_merged_on_failure

        mock_cfg = MagicMock(github_repo="owner/repo", github_user="user")
        mock_status = MagicMock(state="MERGED")
        with (
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch("sova.git.pr.get_pr_status", new_callable=AsyncMock, return_value=mock_status),
        ):
            assert await _check_pr_merged_on_failure(pr_number=88, project_dir=None) is True

    async def test_returns_false_for_open_pr(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_lifecycle import _check_pr_merged_on_failure

        mock_cfg = MagicMock(github_repo="owner/repo", github_user="user")
        mock_status = MagicMock(state="OPEN")
        with (
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch("sova.git.pr.get_pr_status", new_callable=AsyncMock, return_value=mock_status),
        ):
            assert await _check_pr_merged_on_failure(pr_number=88, project_dir=None) is False

    async def test_returns_false_on_gh_failure(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_lifecycle import _check_pr_merged_on_failure

        mock_cfg = MagicMock(github_repo="owner/repo", github_user="user")
        with (
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch("sova.git.pr.get_pr_status", new_callable=AsyncMock, side_effect=RuntimeError("not found")),
        ):
            assert await _check_pr_merged_on_failure(pr_number=88, project_dir=None) is False

    async def test_returns_false_when_no_pr(self) -> None:
        from sova.dashboard.services.agent_lifecycle import _check_pr_merged_on_failure

        assert await _check_pr_merged_on_failure(pr_number=None, project_dir=None) is False

    async def test_start_command_passes_pr_number_to_task_run(self) -> None:
        """start_command should extract pr from args and pass to _create_task_run."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle

        mock_process = MagicMock()
        mock_process.pid = 12345

        with (
            patch.object(agent_lifecycle, "_get_project_agents") as mock_gpa,
            patch.object(
                agent_lifecycle,
                "get_runtime",
                return_value=MagicMock(spawn=AsyncMock(return_value=mock_process)),
            ),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=99) as mock_create,
            patch.object(agent_lifecycle, "_set_output_file_path", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_link_run_to_lifecycle", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            from sova.dashboard.services.agent_pool import ProjectAgents

            pa = ProjectAgents()
            pa.project_dir = MagicMock()
            pa.project_dir.__truediv__ = MagicMock(return_value=MagicMock(is_file=MagicMock(return_value=False)))
            mock_gpa.return_value = pa

            result = await agent_lifecycle.start_command(
                "integrate-pr",
                args={"issue": "32", "pr": 88},
            )

        assert "error" not in result
        mock_create.assert_awaited_once()
        assert mock_create.call_args.kwargs.get("pr_number") == 88

    async def test_wait_and_finalize_overrides_failed_to_done_when_pr_merged(self) -> None:
        """_wait_and_finalize should mark status 'done' when integration cmd fails but PR is merged."""
        from pathlib import Path
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.agent_pool import AgentState, CompletedAgent, ProjectAgents

        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=1)

        agent = AgentState(
            run_id=42,
            issue="99",
            role="command:integrate-pr",
            process=mock_process,
            pr_number=88,
            project_dir=Path("/tmp/test-project"),
        )

        pa = ProjectAgents()
        pa.agents[42] = agent

        with (
            patch.object(agent_lifecycle, "_check_pr_merged_on_failure", new_callable=AsyncMock, return_value=True),
            patch.object(agent_lifecycle, "_finalize_task_run", new_callable=AsyncMock) as mock_finalize,
            patch.object(agent_lifecycle, "_finalize_lifecycle_phase", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_handoff._process_auto_handoff", new_callable=AsyncMock),
            patch("sova.config.loader.load_config", side_effect=Exception("skip notifications")),
        ):
            await agent_lifecycle._wait_and_finalize(pa, agent)

        assert len(pa.recently_completed) == 1
        completed: CompletedAgent = pa.recently_completed[0]
        assert completed.status == "done"
        assert completed.run_id == 42

        mock_finalize.assert_awaited_once()
        assert mock_finalize.call_args.kwargs["exit_code"] == 0


# ---------------------------------------------------------------------------
# Per-issue budget check
# ---------------------------------------------------------------------------


class TestIssueBudgetCheck:
    """_check_issue_budget must block spawns when cumulative cost exceeds limit."""

    async def test_blocks_over_budget_issue(self) -> None:
        from pathlib import Path
        from unittest.mock import patch

        from sova.dashboard.services.agent_lifecycle import _check_issue_budget
        from sova.db.models import IssueLifecycle
        from sova.db.session import get_session as real_get_session

        async with await get_session() as session:
            async with session.begin():
                lifecycle = IssueLifecycle(
                    issue_number="200",
                    current_phase="development",
                    total_cost_usd=Decimal("55.00"),
                )
                session.add(lifecycle)

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        with patch("sova.db.session.get_session", side_effect=_ignore_project_dir):
            result = await _check_issue_budget("200", Path.cwd())

        assert result is not None
        assert "error" in result
        assert "exceeded" in result["error"]
        assert result["total_cost_usd"] == 55.0

    async def test_allows_under_budget_issue(self) -> None:
        from pathlib import Path
        from unittest.mock import patch

        from sova.dashboard.services.agent_lifecycle import _check_issue_budget
        from sova.db.models import IssueLifecycle
        from sova.db.session import get_session as real_get_session

        async with await get_session() as session:
            async with session.begin():
                lifecycle = IssueLifecycle(
                    issue_number="201",
                    current_phase="development",
                    total_cost_usd=Decimal("5.00"),
                )
                session.add(lifecycle)

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        with patch("sova.db.session.get_session", side_effect=_ignore_project_dir):
            result = await _check_issue_budget("201", Path.cwd())

        assert result is None

    async def test_allows_no_lifecycle(self) -> None:
        """No prior lifecycle for the issue -- first run, always allowed."""
        from pathlib import Path
        from unittest.mock import patch

        from sova.dashboard.services.agent_lifecycle import _check_issue_budget
        from sova.db.session import get_session as real_get_session

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        with patch("sova.db.session.get_session", side_effect=_ignore_project_dir):
            result = await _check_issue_budget("999", Path.cwd())
        assert result is None


# ---------------------------------------------------------------------------
# Agent Recovery -- direct service tests
# ---------------------------------------------------------------------------


class TestAgentRecoveryDirect:
    """Direct tests for agent_recovery functions."""

    async def test_recover_stale_runs_dead_pid(self) -> None:
        from sova.dashboard.services.agent_recovery import recover_stale_runs

        session = await get_session()
        async with session.begin():
            run = TaskRun(
                issue_number="77",
                role="developer",
                status="running",
                pid=999999,
            )
            session.add(run)
            await session.flush()
            run_id = run.id

        interrupted = await recover_stale_runs()

        assert len(interrupted) == 1
        assert interrupted[0]["issue"] == "77"

        session2 = await get_session()
        async with session2.begin():
            updated = await session2.get(TaskRun, run_id)
            assert updated.status == "interrupted"
            assert updated.ended_at is not None
            assert "stale run recovered" in updated.error_message.lower()

    async def test_recover_stale_runs_nil_pid(self) -> None:
        from sova.dashboard.services.agent_recovery import recover_stale_runs

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="78", role="dev", status="running", pid=None)
            session.add(run)

        interrupted = await recover_stale_runs()
        assert len(interrupted) == 1

    async def test_recover_stale_runs_skips_alive(self) -> None:
        import os

        from sova.dashboard.services.agent_recovery import recover_stale_runs

        session = await get_session()
        async with session.begin():
            run = TaskRun(
                issue_number="79",
                role="dev",
                status="running",
                pid=os.getpid(),
            )
            session.add(run)

        interrupted = await recover_stale_runs()
        assert len(interrupted) == 0

    async def test_dismiss_interrupted_runs(self) -> None:
        from sova.dashboard.services.agent_recovery import dismiss_interrupted_runs

        session = await get_session()
        async with session.begin():
            session.add(TaskRun(issue_number="80", role="dev", status="interrupted", pid=99999))
            session.add(TaskRun(issue_number="81", role="dev", status="interrupted", pid=99998))
            session.add(TaskRun(issue_number="82", role="dev", status="done"))

        count = await dismiss_interrupted_runs()
        assert count == 2

        session2 = await get_session()
        async with session2.begin():
            from sqlalchemy import select

            stmt = select(TaskRun).where(TaskRun.status == "interrupted")
            result = await session2.execute(stmt)
            remaining = result.scalars().all()
            assert len(remaining) == 0

    async def test_recover_stale_runs_catches_pending_status(self) -> None:
        """recover_stale_runs should catch runs stuck in non-running non-terminal states."""
        from sova.dashboard.services.agent_recovery import recover_stale_runs

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="83", role="dev", status="pending", pid=999999)
            session.add(run)
            await session.flush()
            run_id = run.id

        interrupted = await recover_stale_runs()
        assert len(interrupted) == 1
        assert interrupted[0]["issue"] == "83"

        session2 = await get_session()
        async with session2.begin():
            updated = await session2.get(TaskRun, run_id)
            assert updated.status == "interrupted"
            assert "pending" in updated.error_message

    async def test_check_issue_conflict_auto_recovers_dead_pid(self) -> None:
        """_check_issue_conflict should mark dead-PID DB runs as interrupted."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_lifecycle import ProjectAgents, _check_issue_conflict
        from sova.db.session import get_session as real_get_session

        pa = ProjectAgents()

        async with await real_get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="84", role="developer", status="running", pid=999999)
                session.add(run)
                await session.flush()
                run_id = run.id

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        with patch("sova.db.session.get_session", side_effect=_ignore_project_dir):
            result = await _check_issue_conflict("84", pa)

        assert result is None

        async with await real_get_session() as session:
            async with session.begin():
                updated = await session.get(TaskRun, run_id)
                assert updated.status == "interrupted"
                assert updated.error_message is not None

    async def test_check_issue_conflict_force_skips_live_external(self) -> None:
        """_check_issue_conflict with force=True should skip live external agents."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_lifecycle import ProjectAgents, _check_issue_conflict
        from sova.db.session import get_session as real_get_session

        pa = ProjectAgents()

        async with await real_get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="85", role="developer", status="running", pid=12345)
                session.add(run)

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        with (
            patch("sova.db.session.get_session", side_effect=_ignore_project_dir),
            patch("sova.dashboard.services.agent_recovery._is_process_alive", return_value=True),
        ):
            result_no_force = await _check_issue_conflict("85", pa)
            assert result_no_force is not None
            assert "already has an active agent" in result_no_force["error"]

            result_force = await _check_issue_conflict("85", pa, force=True)
            assert result_force is None

    async def test_sova_review_verdict_no_run(self) -> None:
        from sova.dashboard.services.agent_recovery import get_sova_review_verdict

        result = await get_sova_review_verdict("999")
        assert result["has_sova_review"] is False
        assert result["verdict"] is None

    async def test_sova_review_verdict_approve(self) -> None:
        from sova.dashboard.services.agent_recovery import get_sova_review_verdict

        session = await get_session()
        async with session.begin():
            session.add(
                TaskRun(
                    issue_number="100",
                    role="reviewer",
                    status="done",
                    handoff_json={"next_action": "approve", "pending_findings": []},
                    ended_at=datetime.now(timezone.utc),
                )
            )

        result = await get_sova_review_verdict("100")
        assert result["has_sova_review"] is True
        assert result["verdict"] == "approve"
        assert result["finding_count"] == 0
        assert result["reviewed_at"] is not None

    async def test_sova_review_verdict_block(self) -> None:
        from sova.dashboard.services.agent_recovery import get_sova_review_verdict

        session = await get_session()
        async with session.begin():
            session.add(
                TaskRun(
                    issue_number="101",
                    role="reviewer",
                    status="done",
                    handoff_json={
                        "next_action": "address_review",
                        "pending_findings": [
                            {"file": "a.py", "severity": 8, "description": "bug"},
                            {"file": "b.py", "severity": 3, "description": "minor"},
                        ],
                    },
                    ended_at=datetime.now(timezone.utc),
                )
            )

        result = await get_sova_review_verdict("101")
        assert result["has_sova_review"] is True
        assert result["verdict"] == "block"
        assert result["finding_count"] == 2

    async def test_sova_review_verdict_revise(self) -> None:
        from sova.dashboard.services.agent_recovery import get_sova_review_verdict

        session = await get_session()
        async with session.begin():
            session.add(
                TaskRun(
                    issue_number="102",
                    role="reviewer",
                    status="done",
                    handoff_json={
                        "next_action": "address_review",
                        "pending_findings": [
                            {"file": "c.py", "severity": 5, "description": "style"},
                        ],
                    },
                    ended_at=datetime.now(timezone.utc),
                )
            )

        result = await get_sova_review_verdict("102")
        assert result["has_sova_review"] is True
        assert result["verdict"] == "revise"
        assert result["finding_count"] == 1

    async def test_sova_review_verdict_strips_hash(self) -> None:
        from sova.dashboard.services.agent_recovery import get_sova_review_verdict

        session = await get_session()
        async with session.begin():
            session.add(
                TaskRun(
                    issue_number="103",
                    role="reviewer",
                    status="done",
                    handoff_json={"next_action": "approve", "pending_findings": []},
                    ended_at=datetime.now(timezone.utc),
                )
            )

        result = await get_sova_review_verdict("#103")
        assert result["has_sova_review"] is True
        assert result["verdict"] == "approve"

    async def test_sova_review_verdict_picks_latest(self) -> None:
        from sova.dashboard.services.agent_recovery import get_sova_review_verdict

        now = datetime.now(timezone.utc)
        session = await get_session()
        async with session.begin():
            session.add(
                TaskRun(
                    issue_number="104",
                    role="reviewer",
                    status="done",
                    handoff_json={"next_action": "address_review", "pending_findings": [{"severity": 8}]},
                    ended_at=now - timedelta(hours=1),
                )
            )
            session.add(
                TaskRun(
                    issue_number="104",
                    role="reviewer",
                    status="done",
                    handoff_json={"next_action": "approve", "pending_findings": []},
                    ended_at=now,
                )
            )

        result = await get_sova_review_verdict("104")
        assert result["verdict"] == "approve"

    async def test_sova_review_verdict_null_handoff_fallback(self) -> None:
        from sova.dashboard.services.agent_recovery import get_sova_review_verdict

        session = await get_session()
        async with session.begin():
            session.add(
                TaskRun(
                    issue_number="105",
                    role="command:review-pr",
                    status="done",
                    handoff_json=None,
                    ended_at=datetime.now(timezone.utc),
                )
            )

        result = await get_sova_review_verdict("105")
        assert result["has_sova_review"] is True
        assert result["verdict"] == "revise"
        assert result["finding_count"] == 0
        assert result["reviewed_at"] is not None

    async def test_sova_review_verdict_interrupted_with_findings(self) -> None:
        """A reviewer killed during post-review cleanup still counts."""
        from sova.dashboard.services.agent_recovery import get_sova_review_verdict

        session = await get_session()
        async with session.begin():
            session.add(
                TaskRun(
                    issue_number="106",
                    role="reviewer",
                    status="interrupted",
                    handoff_json={
                        "next_action": "address_review",
                        "pending_findings": [
                            {"file": "x.py", "severity": 9, "description": "critical"},
                            {"file": "y.py", "severity": 4, "description": "minor"},
                        ],
                    },
                    started_at=datetime.now(timezone.utc),
                    ended_at=None,
                )
            )

        result = await get_sova_review_verdict("106")
        assert result["has_sova_review"] is True
        assert result["verdict"] == "block"
        assert result["finding_count"] == 2
        assert result["reviewed_at"] is not None

    async def test_recover_stale_runs_marks_done_with_handoff(self) -> None:
        """recover_stale_runs should mark a dead-PID run as 'done' when a valid handoff exists."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_recovery import recover_stale_runs

        now = datetime.now(timezone.utc)
        run_start = now - timedelta(minutes=10)

        session = await get_session()
        async with session.begin():
            run = TaskRun(
                issue_number="200",
                role="developer",
                status="running",
                pid=999999,
                started_at=run_start,
            )
            session.add(run)
            await session.flush()
            run_id = run.id

        handoff_data = {
            "status": "awaiting_action",
            "created_at": now.isoformat(),
            "details": {"cost_usd": 1.23},
        }
        with patch(
            "sova.dashboard.services.handoff_service.get_handoff",
            return_value=handoff_data,
        ):
            interrupted = await recover_stale_runs()

        assert len(interrupted) == 0

        session2 = await get_session()
        async with session2.begin():
            updated = await session2.get(TaskRun, run_id)
            assert updated.status == "done"
            assert updated.error_message is None
            from decimal import Decimal

            assert updated.total_cost_usd == Decimal("1.23")

    async def test_recover_stale_runs_stays_interrupted_with_old_handoff(self) -> None:
        """recover_stale_runs should NOT mark as done when handoff predates the run."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_recovery import recover_stale_runs

        now = datetime.now(timezone.utc)
        run_start = now - timedelta(minutes=5)

        session = await get_session()
        async with session.begin():
            run = TaskRun(
                issue_number="201",
                role="developer",
                status="running",
                pid=999999,
                started_at=run_start,
            )
            session.add(run)
            await session.flush()
            run_id = run.id

        old_handoff = {
            "status": "awaiting_action",
            "created_at": (now - timedelta(minutes=20)).isoformat(),
            "details": {"cost_usd": 0.50},
        }
        with patch(
            "sova.dashboard.services.handoff_service.get_handoff",
            return_value=old_handoff,
        ):
            interrupted = await recover_stale_runs()

        assert len(interrupted) == 1

        session2 = await get_session()
        async with session2.begin():
            updated = await session2.get(TaskRun, run_id)
            assert updated.status == "interrupted"

    async def test_recover_stale_runs_handoff_no_created_at(self) -> None:
        """recover_stale_runs stays interrupted when handoff has no created_at field."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_recovery import recover_stale_runs

        session = await get_session()
        async with session.begin():
            run = TaskRun(
                issue_number="202",
                role="developer",
                status="running",
                pid=999999,
                started_at=datetime.now(timezone.utc),
            )
            session.add(run)
            await session.flush()
            run_id = run.id

        handoff_data = {"status": "awaiting_action", "details": {}}
        with patch(
            "sova.dashboard.services.handoff_service.get_handoff",
            return_value=handoff_data,
        ):
            interrupted = await recover_stale_runs()

        assert len(interrupted) == 1

        session2 = await get_session()
        async with session2.begin():
            updated = await session2.get(TaskRun, run_id)
            assert updated.status == "interrupted"


class TestReadFileHandoff:
    def test_returns_none_when_no_file(self, tmp_path: Path) -> None:
        from sova.dashboard.services.agent_db import _read_file_handoff

        assert _read_file_handoff(tmp_path) is None

    def test_reads_valid_handoff(self, tmp_path: Path) -> None:
        import json

        from sova.dashboard.services.agent_db import _read_file_handoff

        control_dir = tmp_path / ".claude" / "agent-control"
        control_dir.mkdir(parents=True)
        handoff = {
            "id": "test",
            "source": "reviewer",
            "status": "awaiting_action",
            "issue": "42",
            "pr_number": 99,
            "summary": "test",
            "details": {"next_action": "address_review", "findings": [{"severity": 5}]},
            "next_actions": [],
        }
        (control_dir / "handoff.json").write_text(json.dumps(handoff))

        result = _read_file_handoff(tmp_path)
        assert result is not None
        assert result["issue"] == "42"
        assert result["pr_number"] == 99
        assert result["details"]["next_action"] == "address_review"
        assert result["source"] == "reviewer"

    def test_returns_none_on_corrupt_json(self, tmp_path: Path) -> None:
        from sova.dashboard.services.agent_db import _read_file_handoff

        control_dir = tmp_path / ".claude" / "agent-control"
        control_dir.mkdir(parents=True)
        (control_dir / "handoff.json").write_text("not json")

        assert _read_file_handoff(tmp_path) is None


# ---------------------------------------------------------------------------
# Pipeline variant detection -- get_step_progress
# ---------------------------------------------------------------------------


class TestStepProgress:
    """Tests for get_step_progress pipeline variant detection."""

    def test_developer_pipeline_default(self) -> None:
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        result = get_step_progress("develop")
        assert result["pipeline_variant"] == "developer"
        assert result["step_index"] == 3

    def test_address_review_from_step(self) -> None:
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        result = get_step_progress("rebase")
        assert result["pipeline_variant"] == "address_review"
        assert result["step_index"] == 0

    def test_none_step_defaults_to_developer(self) -> None:
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        result = get_step_progress(None)
        assert result["pipeline_variant"] == "developer"
        assert result["step_index"] == -1

    def test_none_step_with_pr_number_is_address_review(self) -> None:
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        result = get_step_progress(None, role="developer", pr_number=147)
        assert result["pipeline_variant"] == "address_review"
        assert result["step_index"] == -1
        assert result["total_steps"] == 9

    def test_agent_step_with_pr_number_is_address_review(self) -> None:
        """Dashboard outer TaskRun (current_step='agent') with pr_number -> address_review."""
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        result = get_step_progress("agent", role="developer", pr_number=147)
        assert result["pipeline_variant"] == "address_review"

    def test_shared_step_with_pr_number_is_developer(self) -> None:
        """WorkflowEngine TaskRun on shared step with pr_number acquired mid-pipeline."""
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        result = get_step_progress("commit", role="developer", pr_number=147)
        assert result["pipeline_variant"] == "developer"
        assert result["step_index"] == 6

    def test_shared_step_without_pr_number_is_developer(self) -> None:
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        result = get_step_progress("commit")
        assert result["pipeline_variant"] == "developer"
        assert result["step_index"] == 6

    def test_workflow_engine_post_create_pr_is_developer(self) -> None:
        """WorkflowEngine TaskRun after CreatePRStep must not be mislabeled."""
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        for step in ("monitor_ci", "extract_memory", "handoff_to_reviewer"):
            result = get_step_progress(step, role="developer", pr_number=147)
            assert result["pipeline_variant"] == "developer", f"step={step} should be developer"

    def test_reviewer_role_with_pr_number_is_command(self) -> None:
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        result = get_step_progress("commit", role="reviewer", pr_number=147)
        assert result["pipeline_variant"] == "command"

    def test_command_role_returns_command_variant(self) -> None:
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        result = get_step_progress(None, role="command:integrate-pr")
        assert result["pipeline_variant"] == "command"
        assert result["step_index"] == 0
        assert result["total_steps"] == 1

    def test_command_role_with_agent_step(self) -> None:
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        result = get_step_progress("agent", role="command:review-pr")
        assert result["pipeline_variant"] == "command"
        assert result["step_index"] == 0

    def test_command_role_ship_pr(self) -> None:
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        result = get_step_progress(None, role="command:ship-pr")
        assert result["pipeline_variant"] == "command"

    def test_researcher_role_unaffected_by_command_check(self) -> None:
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        result = get_step_progress(None, role="researcher")
        assert result["pipeline_variant"] == "researcher"

    def test_reviewer_role_returns_command_variant(self) -> None:
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        result = get_step_progress(None, role="reviewer")
        assert result["pipeline_variant"] == "command"
        assert result["step_index"] == 0
        assert result["total_steps"] == 1


# ---------------------------------------------------------------------------
# Output service -- OutputWriter and read_lines
# ---------------------------------------------------------------------------


class TestOutputService:
    """Tests for the output persistence layer."""

    def test_output_writer_creates_file(self, tmp_path) -> None:
        from sova.core.output import OutputWriter

        writer = OutputWriter(tmp_path, run_id=1)
        assert writer.path.exists()
        writer.close()

    def test_output_writer_write_and_read(self, tmp_path) -> None:
        from sova.core.output import OutputWriter, read_lines

        writer = OutputWriter(tmp_path, run_id=2)
        writer.write_line("Hello, world")
        writer.write_line("Second line")
        writer.close()

        lines, total = read_lines(tmp_path, run_id=2)
        assert total == 2
        assert lines == ["Hello, world", "Second line"]

    def test_output_writer_read_with_offset(self, tmp_path) -> None:
        from sova.core.output import OutputWriter, read_lines

        writer = OutputWriter(tmp_path, run_id=3)
        writer.write_line("Line 1")
        writer.write_line("Line 2")
        writer.write_line("Line 3")
        writer.close()

        lines, total = read_lines(tmp_path, run_id=3, since=1)
        assert total == 3
        assert lines == ["Line 2", "Line 3"]

    def test_read_lines_missing_file(self, tmp_path) -> None:
        from sova.core.output import read_lines

        lines, total = read_lines(tmp_path, run_id=999)
        assert lines == []
        assert total == 0

    def test_output_writer_strips_trailing_newlines(self, tmp_path) -> None:
        from sova.core.output import OutputWriter, read_lines

        writer = OutputWriter(tmp_path, run_id=4)
        writer.write_line("Line with newline\n")
        writer.close()

        lines, total = read_lines(tmp_path, run_id=4)
        assert total == 1
        assert lines == ["Line with newline"]

    def test_output_path(self, tmp_path) -> None:
        from sova.core.output import output_path

        path = output_path(tmp_path, run_id=42)
        assert path.name == "42.log"
        assert ".claude/agent-output" in str(path)


# ---------------------------------------------------------------------------
# Agent Output -- stream-json parsing
# ---------------------------------------------------------------------------


class TestParseStreamLine:
    """Tests for _parse_stream_line in agent_output.py."""

    def _agent(self):
        from unittest.mock import MagicMock

        a = MagicMock()
        a.last_result_cost = None
        return a

    def test_empty_line(self) -> None:
        from sova.dashboard.services.agent_output import _parse_stream_line

        assert _parse_stream_line("", self._agent()) == ""
        assert _parse_stream_line("   ", self._agent()) == ""

    def test_plain_text_passthrough(self) -> None:
        from sova.dashboard.services.agent_output import _parse_stream_line

        assert _parse_stream_line("not json", self._agent()) == "not json"

    def test_assistant_message(self) -> None:
        import json

        from sova.dashboard.services.agent_output import _parse_stream_line

        line = json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Hello"}]},
            }
        )
        assert _parse_stream_line(line, self._agent()) == "Hello"

    def test_content_block_delta(self) -> None:
        import json

        from sova.dashboard.services.agent_output import _parse_stream_line

        line = json.dumps(
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "world"},
            }
        )
        assert _parse_stream_line(line, self._agent()) == "world"

    def test_result_captures_cost(self) -> None:
        import json

        from sova.dashboard.services.agent_output import _parse_stream_line

        agent = self._agent()
        line = json.dumps({"type": "result", "total_cost_usd": 1.23})
        result = _parse_stream_line(line, agent)
        assert "$1.23" in result
        assert agent.last_result_cost == 1.23

    def test_unknown_type_returns_empty(self) -> None:
        import json

        from sova.dashboard.services.agent_output import _parse_stream_line

        line = json.dumps({"type": "system", "data": {}})
        assert _parse_stream_line(line, self._agent()) == ""


# ---------------------------------------------------------------------------
# Setup Service -- TomlConfig and generate_sova_toml
# ---------------------------------------------------------------------------


class TestTomlConfigGeneration:
    """Tests for TomlConfig dataclass and generate_sova_toml."""

    def test_default_toml_config(self) -> None:
        from sova.dashboard.services.setup_service import TomlConfig

        cfg = TomlConfig()
        assert cfg.base_branch == "main"
        assert cfg.task_source == "github"
        assert cfg.agent_model == "opus"
        assert cfg.max_budget == "10.00"
        assert cfg.ai_coauthor is True
        assert cfg.pr_auto_link is True

    def test_generate_sova_toml_includes_fields(self) -> None:
        from sova.dashboard.services.setup_service import TomlConfig, generate_sova_toml

        cfg = TomlConfig(github_repo="owner/repo", github_user="testuser", base_branch="develop")
        content = generate_sova_toml(cfg)
        assert 'github_repo = "owner/repo"' in content
        assert 'github_user = "testuser"' in content
        assert 'base_branch = "develop"' in content
        assert "[task_source]" in content
        assert "[agent]" in content

    def test_generate_sova_toml_ai_coauthor(self) -> None:
        from sova.dashboard.services.setup_service import TomlConfig, generate_sova_toml

        cfg = TomlConfig(ai_coauthor=False)
        content = generate_sova_toml(cfg)
        assert "ai_coauthor = false" in content

    def test_generate_sova_toml_custom_budget(self) -> None:
        from sova.dashboard.services.setup_service import TomlConfig, generate_sova_toml

        cfg = TomlConfig(max_budget="25.00")
        content = generate_sova_toml(cfg)
        assert 'max_budget = "25.00"' in content


# ---------------------------------------------------------------------------
# Roles API
# ---------------------------------------------------------------------------


class TestRolesAPI:
    async def test_list_roles(self, client):
        """GET /api/roles returns built-in roles."""
        resp = await client.get("/api/roles")
        assert resp.status_code == 200
        data = resp.json()
        assert "roles" in data
        names = {r["name"] for r in data["roles"]}
        assert "developer" in names
        assert "triage" in names

    async def test_list_commands(self, client):
        """GET /api/roles/commands returns discovered commands."""
        resp = await client.get("/api/roles/commands")
        assert resp.status_code == 200
        data = resp.json()
        assert "commands" in data
        assert len(data["commands"]) > 0
        # At least one command should have inputs/outputs
        names = {c["name"] for c in data["commands"]}
        assert "develop" in names

    async def test_get_builtin_role(self, client):
        """GET /api/roles/developer returns the built-in developer role."""
        resp = await client.get("/api/roles/developer")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "developer"
        assert data["is_builtin"] is True
        assert "graph_json" in data

    async def test_get_custom_role(self, client):
        """GET /api/roles/{name} returns a custom role from the DB."""
        graph = {
            "nodes": [{"id": "n1", "command": "develop", "label": "Dev", "position": {"x": 0, "y": 0}, "params": {}}],
            "edges": [],
        }
        await client.post(
            "/api/roles",
            json={
                "name": "fetch-me",
                "description": "Fetchable role",
                "graph_json": graph,
            },
        )
        resp = await client.get("/api/roles/fetch-me")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "fetch-me"
        assert data["is_builtin"] is False

    async def test_get_nonexistent_role(self, client):
        """GET /api/roles/{name} returns 404 for unknown role."""
        resp = await client.get("/api/roles/does-not-exist")
        assert resp.status_code == 404

    async def test_create_custom_role(self, client):
        """POST /api/roles creates a custom role."""
        graph = {
            "nodes": [{"id": "n1", "command": "develop", "label": "Dev", "position": {"x": 0, "y": 0}, "params": {}}],
            "edges": [],
        }
        resp = await client.post(
            "/api/roles",
            json={
                "name": "my-workflow",
                "description": "Test workflow",
                "graph_json": graph,
                "input_states": ["researched"],
                "output_state": "in_review",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "my-workflow"
        assert data["is_builtin"] is False

    async def test_create_rejects_builtin_name(self, client):
        """POST /api/roles rejects built-in role names."""
        resp = await client.post(
            "/api/roles",
            json={
                "name": "developer",
                "graph_json": {"nodes": [{"id": "n1", "command": "test"}], "edges": []},
            },
        )
        assert resp.status_code == 409

    async def test_create_accepts_draft_dag(self, client):
        """POST /api/roles accepts drafts (validation on save, not create)."""
        graph = {
            "nodes": [{"id": "n1", "command": "", "label": "Start"}],
            "edges": [],
        }
        resp = await client.post(
            "/api/roles",
            json={"name": "draft-role", "graph_json": graph},
        )
        assert resp.status_code == 200

    async def test_update_rejects_invalid_dag(self, client):
        """PUT /api/roles/{name} rejects DAGs with cycles."""
        graph_ok = {
            "nodes": [{"id": "a", "command": "x"}],
            "edges": [],
        }
        resp = await client.post(
            "/api/roles",
            json={"name": "bad-update-role", "graph_json": graph_ok},
        )
        assert resp.status_code == 200

        graph_bad = {
            "nodes": [{"id": "a", "command": "x"}, {"id": "b", "command": "y"}],
            "edges": [{"id": "e1", "source": "a", "target": "b"}, {"id": "e2", "source": "b", "target": "a"}],
        }
        resp = await client.put(
            "/api/roles/bad-update-role",
            json={"graph_json": graph_bad},
        )
        assert resp.status_code == 400
        data = resp.json()
        detail = data["detail"]
        assert "validation_errors" in detail

    async def test_update_custom_role(self, client):
        """PUT /api/roles/{name} updates a custom role."""
        # Create first
        graph = {
            "nodes": [{"id": "n1", "command": "develop", "label": "Dev", "position": {"x": 0, "y": 0}, "params": {}}],
            "edges": [],
        }
        await client.post(
            "/api/roles",
            json={
                "name": "editable",
                "graph_json": graph,
            },
        )

        # Update
        resp = await client.put(
            "/api/roles/editable",
            json={
                "description": "Updated description",
            },
        )
        data = resp.json()
        assert data.get("description") == "Updated description"

    async def test_update_rejects_builtin(self, client):
        """PUT /api/roles/developer rejects updates to built-in roles."""
        resp = await client.put(
            "/api/roles/developer",
            json={
                "description": "Hacked",
            },
        )
        assert resp.status_code == 404

    async def test_delete_custom_role(self, client):
        """DELETE /api/roles/{name} removes custom roles."""
        graph = {
            "nodes": [{"id": "n1", "command": "test", "label": "T", "position": {"x": 0, "y": 0}, "params": {}}],
            "edges": [],
        }
        await client.post(
            "/api/roles",
            json={
                "name": "deletable",
                "graph_json": graph,
            },
        )

        resp = await client.delete("/api/roles/deletable")
        data = resp.json()
        assert data["status"] == "deleted"

    async def test_delete_rejects_builtin(self, client):
        """DELETE /api/roles/developer rejects deleting built-in roles."""
        resp = await client.delete("/api/roles/developer")
        assert resp.status_code == 404

    async def test_validate_dag(self, client):
        """POST /api/roles/{name}/validate validates DAG structure."""
        graph = {
            "nodes": [{"id": "a", "command": "develop"}, {"id": "b", "command": "test"}],
            "edges": [{"id": "e1", "source": "a", "target": "b"}],
        }
        resp = await client.post("/api/roles/test/validate", json={"graph_json": graph})
        data = resp.json()
        assert data["valid"] is True
        assert data["errors"] == []

    async def test_validate_dag_with_errors(self, client):
        """POST /api/roles/{name}/validate returns errors for invalid DAGs."""
        resp = await client.post("/api/roles/test/validate", json={"graph_json": {"nodes": [], "edges": []}})
        data = resp.json()
        assert data["valid"] is False
        assert len(data["errors"]) > 0

    async def test_roles_page_renders(self, client):
        """GET /roles renders the roles list page."""
        resp = await client.get("/roles")
        assert resp.status_code == 200

    async def test_role_editor_page_renders(self, client):
        """GET /roles/{name} renders the role editor page."""
        resp = await client.get("/roles/developer")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Synthesize PR actions
# ---------------------------------------------------------------------------


class TestSynthesizePrActions:
    @pytest.fixture(autouse=True)
    def _synthesis_env(self, monkeypatch, tmp_path):
        """Set up common mocks for synthesize_pr_actions tests."""
        from unittest.mock import AsyncMock

        from sova.dashboard.services import agent_recovery
        from sova.git.pr import PRInfo

        agent_recovery._synthesis_cache.clear()
        agent_recovery._issue_pr_cache.clear()

        monkeypatch.setattr("sova.dashboard.project_context.get_project_dir", lambda: tmp_path)
        mock_cfg = type(
            "Cfg",
            (),
            {
                "github_repo": "user/repo",
                "github_user": "testuser",
                "task_source": type("TS", (), {"type": "github", "github_project_number": 0})(),
            },
        )()
        monkeypatch.setattr("sova.config.loader.load_config", lambda _: mock_cfg)
        monkeypatch.setattr(
            "sova.git.operations.find_pr_for_issue",
            AsyncMock(return_value=PRInfo(number=99, url="https://github.com/user/repo/pull/99")),
        )

        self.mock_adapter = AsyncMock()
        monkeypatch.setattr("sova.adapters.create_adapter", lambda _: self.mock_adapter)

    async def test_returns_address_review_on_changes_requested(self) -> None:
        from sova.adapters.base import PRReview
        from sova.dashboard.services import agent_recovery

        self.mock_adapter.get_pr_reviews.return_value = [
            PRReview(
                reviewer="alice",
                state="CHANGES_REQUESTED",
                body="Fix this",
                submitted_at="2026-01-01T10:00:00Z",
                is_bot=False,
            ),
        ]

        actions = await agent_recovery.synthesize_pr_actions("42")

        assert actions is not None
        assert len(actions) == 1
        assert actions[0]["id"] == "address_review"
        assert actions[0]["mode"] == "agent"
        assert actions[0]["args"]["issue"] == "42"
        assert actions[0]["args"]["pr"] == 99
        assert actions[0]["auto_execute"] is False

    async def test_returns_integrate_on_all_approved(self) -> None:
        from sova.adapters.base import PRReview
        from sova.dashboard.services import agent_recovery

        self.mock_adapter.get_pr_reviews.return_value = [
            PRReview(
                reviewer="alice", state="APPROVED", body="LGTM", submitted_at="2026-01-01T10:00:00Z", is_bot=False
            ),
        ]

        actions = await agent_recovery.synthesize_pr_actions("42")

        assert actions is not None
        assert len(actions) == 2
        assert actions[0]["id"] == "integrate"
        assert actions[1]["id"] == "approve"

    async def test_returns_none_for_only_bot_reviews(self) -> None:
        from sova.adapters.base import PRReview
        from sova.dashboard.services import agent_recovery

        self.mock_adapter.get_pr_reviews.return_value = [
            PRReview(
                reviewer="coderabbit[bot]",
                state="CHANGES_REQUESTED",
                body="Issues",
                submitted_at="2026-01-01T10:00:00Z",
                is_bot=True,
            ),
        ]

        actions = await agent_recovery.synthesize_pr_actions("42")
        assert actions is None

    async def test_changes_requested_takes_priority_over_approval(self) -> None:
        from sova.adapters.base import PRReview
        from sova.dashboard.services import agent_recovery

        self.mock_adapter.get_pr_reviews.return_value = [
            PRReview(
                reviewer="alice", state="APPROVED", body="LGTM", submitted_at="2026-01-01T10:00:00Z", is_bot=False
            ),
            PRReview(
                reviewer="bob",
                state="CHANGES_REQUESTED",
                body="Fix this",
                submitted_at="2026-01-01T11:00:00Z",
                is_bot=False,
            ),
        ]

        actions = await agent_recovery.synthesize_pr_actions("42")
        assert actions is not None
        assert len(actions) == 1
        assert actions[0]["id"] == "address_review"

    async def test_dismissed_reviews_excluded(self) -> None:
        from sova.adapters.base import PRReview
        from sova.dashboard.services import agent_recovery

        self.mock_adapter.get_pr_reviews.return_value = [
            PRReview(reviewer="alice", state="DISMISSED", body="", submitted_at="2026-01-01T10:00:00Z", is_bot=False),
        ]

        actions = await agent_recovery.synthesize_pr_actions("42")
        assert actions is None

    async def test_returns_none_when_no_pr(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services import agent_recovery

        monkeypatch.setattr(
            "sova.git.operations.find_pr_for_issue",
            AsyncMock(return_value=None),
        )

        actions = await agent_recovery.synthesize_pr_actions("42")
        assert actions is None

    async def test_deduplicates_reviewer_keeps_latest(self) -> None:
        from sova.adapters.base import PRReview
        from sova.dashboard.services import agent_recovery

        self.mock_adapter.get_pr_reviews.return_value = [
            PRReview(
                reviewer="alice",
                state="CHANGES_REQUESTED",
                body="Fix",
                submitted_at="2026-01-01T10:00:00Z",
                is_bot=False,
            ),
            PRReview(
                reviewer="alice", state="APPROVED", body="LGTM now", submitted_at="2026-01-01T12:00:00Z", is_bot=False
            ),
        ]

        actions = await agent_recovery.synthesize_pr_actions("42")
        assert actions is not None
        assert len(actions) == 2
        assert actions[0]["id"] == "integrate"


# ---------------------------------------------------------------------------
# Handoff fallback to synthesized PR actions
# ---------------------------------------------------------------------------


class TestHandoffFallback:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        from sova.dashboard.services import handoff_service

        monkeypatch.setattr(handoff_service, "_resolve_project_dir", lambda: tmp_path)
        handoff_service._handoff_caches.clear()

    async def test_get_handoff_falls_back_to_synthesized(self, client: AsyncClient, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        synthesized = {
            "source": "pr-review-state",
            "status": "awaiting_action",
            "issue": "42",
            "pr_number": 99,
            "summary": "Actions synthesized from PR review state",
            "next_actions": [
                {
                    "id": "address_review",
                    "label": "Address Review",
                    "mode": "agent",
                    "args": {"issue": "42", "pr": 99, "role": "developer"},
                },
            ],
        }
        monkeypatch.setattr(
            "sova.dashboard.routers.handoff.get_synthesized_handoff",
            AsyncMock(return_value=synthesized),
        )

        resp = await client.get("/api/handoff")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_handoff"] is True
        assert data["handoff"]["source"] == "pr-review-state"
        assert len(data["handoff"]["next_actions"]) == 1

    async def test_file_handoff_takes_precedence(self, client: AsyncClient, tmp_path, monkeypatch) -> None:
        import json
        from unittest.mock import AsyncMock

        control_dir = tmp_path / ".claude" / "agent-control"
        control_dir.mkdir(parents=True)
        (control_dir / "handoff.json").write_text(
            json.dumps(
                {
                    "id": "file-handoff",
                    "source": "reviewer",
                    "status": "awaiting_action",
                    "summary": "From file",
                    "created_at": "2026-04-20T10:00:00Z",
                    "next_actions": [{"id": "integrate", "label": "Integrate"}],
                }
            )
        )

        mock_synth = AsyncMock(return_value={"source": "pr-review-state", "next_actions": []})
        monkeypatch.setattr(
            "sova.dashboard.routers.handoff.get_synthesized_handoff",
            mock_synth,
        )

        resp = await client.get("/api/handoff")
        data = resp.json()
        assert data["has_handoff"] is True
        assert data["handoff"]["source"] == "reviewer"
        # Synthesized should not have been called
        mock_synth.assert_not_awaited()

    async def test_no_handoff_and_no_synthesis(self, client: AsyncClient, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        monkeypatch.setattr(
            "sova.dashboard.routers.handoff.get_synthesized_handoff",
            AsyncMock(return_value=None),
        )

        resp = await client.get("/api/handoff")
        data = resp.json()
        assert data["has_handoff"] is False


# ---------------------------------------------------------------------------
# _summarize_ci_checks
# ---------------------------------------------------------------------------


class TestSummarizeCiChecks:
    def test_returns_unknown_for_none(self) -> None:
        from sova.dashboard.services.agent_recovery import _summarize_ci_checks

        assert _summarize_ci_checks(None) == "unknown"

    def test_returns_none_for_empty(self) -> None:
        from sova.dashboard.services.agent_recovery import _summarize_ci_checks

        assert _summarize_ci_checks([]) == "none"

    def test_returns_passed_when_all_success(self) -> None:
        from sova.dashboard.services.agent_recovery import _summarize_ci_checks
        from sova.git.operations import CheckConclusion, CheckStatus, CICheck

        checks = [
            CICheck(name="test", status=CheckStatus.COMPLETED, conclusion=CheckConclusion.SUCCESS, details_url=""),
            CICheck(name="lint", status=CheckStatus.COMPLETED, conclusion=CheckConclusion.SUCCESS, details_url=""),
        ]
        assert _summarize_ci_checks(checks) == "passed"

    def test_returns_failed_when_any_failure(self) -> None:
        from sova.dashboard.services.agent_recovery import _summarize_ci_checks
        from sova.git.operations import CheckConclusion, CheckStatus, CICheck

        checks = [
            CICheck(name="test", status=CheckStatus.COMPLETED, conclusion=CheckConclusion.SUCCESS, details_url=""),
            CICheck(name="lint", status=CheckStatus.COMPLETED, conclusion=CheckConclusion.FAILURE, details_url=""),
        ]
        assert _summarize_ci_checks(checks) == "failed"

    def test_returns_pending_when_in_progress(self) -> None:
        from sova.dashboard.services.agent_recovery import _summarize_ci_checks
        from sova.git.operations import CheckStatus, CICheck

        checks = [
            CICheck(name="test", status=CheckStatus.IN_PROGRESS, conclusion=None, details_url=""),
        ]
        assert _summarize_ci_checks(checks) == "pending"


# ---------------------------------------------------------------------------
# _check_ttl_cache / _check_issue_cache
# ---------------------------------------------------------------------------


class TestTTLCache:
    def test_cache_miss(self) -> None:
        from sova.dashboard.services.agent_recovery import _check_ttl_cache

        cache: dict = {}
        hit, value = _check_ttl_cache(cache, "key")
        assert not hit
        assert value is None

    def test_cache_hit(self) -> None:
        import time

        from sova.dashboard.services.agent_recovery import _check_ttl_cache

        cache = {"key": (time.monotonic(), "result")}
        hit, value = _check_ttl_cache(cache, "key")
        assert hit
        assert value == "result"

    def test_cache_expired(self) -> None:
        import time

        from sova.dashboard.services.agent_recovery import _check_ttl_cache

        cache = {"key": (time.monotonic() - 120, "stale")}
        hit, value = _check_ttl_cache(cache, "key")
        assert not hit

    def test_issue_cache_miss(self) -> None:
        from sova.dashboard.services import agent_recovery

        agent_recovery._issue_pr_cache.clear()
        agent_recovery._synthesis_cache.clear()
        resolved, pr, result = agent_recovery._check_issue_cache("99")
        assert not resolved
        assert pr is None
        assert result is None

    def test_issue_cache_sentinel_no_pr(self) -> None:
        import time

        from sova.dashboard.services import agent_recovery

        agent_recovery._issue_pr_cache["99"] = (time.monotonic(), agent_recovery._SENTINEL_NO_PR)
        resolved, pr, result = agent_recovery._check_issue_cache("99")
        assert resolved
        assert pr is None
        assert result is None
        agent_recovery._issue_pr_cache.clear()

    def test_issue_cache_pr_known_synthesis_cached(self) -> None:
        import time

        from sova.dashboard.services import agent_recovery

        now = time.monotonic()
        agent_recovery._issue_pr_cache["99"] = (now, 42)
        agent_recovery._synthesis_cache[("99", 42)] = (now, [{"id": "test"}])
        resolved, pr, result = agent_recovery._check_issue_cache("99")
        assert resolved
        assert pr == 42
        assert result == [{"id": "test"}]
        agent_recovery._issue_pr_cache.clear()
        agent_recovery._synthesis_cache.clear()

    def test_issue_cache_pr_known_synthesis_not_cached(self) -> None:
        import time

        from sova.dashboard.services import agent_recovery

        agent_recovery._issue_pr_cache["99"] = (time.monotonic(), 42)
        agent_recovery._synthesis_cache.clear()
        resolved, pr, result = agent_recovery._check_issue_cache("99")
        assert not resolved
        assert pr == 42
        assert result is None
        agent_recovery._issue_pr_cache.clear()


# ---------------------------------------------------------------------------
# _deduplicate_reviews edge cases
# ---------------------------------------------------------------------------


class TestDeduplicateReviews:
    def test_fallback_string_comparison(self) -> None:
        """When timestamp parsing fails, fall back to string comparison."""
        from sova.dashboard.services.agent_recovery import _deduplicate_reviews

        r1 = type("R", (), {"reviewer": "alice", "state": "APPROVED", "submitted_at": "bad-ts-1", "is_bot": False})()
        attrs = {"reviewer": "alice", "state": "CHANGES_REQUESTED", "submitted_at": "bad-ts-2", "is_bot": False}
        r2 = type("R", (), attrs)()

        result = _deduplicate_reviews([r1, r2])
        assert len(result) == 1
        assert result["alice"].state == "CHANGES_REQUESTED"


# ---------------------------------------------------------------------------
# execute_handoff_action with synthesized handoff
# ---------------------------------------------------------------------------


class TestExecuteHandoffAction:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        from sova.dashboard.services import handoff_service

        monkeypatch.setattr(handoff_service, "_resolve_project_dir", lambda: tmp_path)
        handoff_service._handoff_caches.clear()

    async def test_execute_synthesized_handoff_action(self, client: AsyncClient, monkeypatch) -> None:
        from unittest.mock import AsyncMock, MagicMock

        synthesized = {
            "source": "pr-review-state",
            "status": "awaiting_action",
            "issue": "42",
            "pr_number": 99,
            "summary": "Synthesized",
            "next_actions": [
                {
                    "id": "address_review",
                    "label": "Address Review",
                    "description": "Address review findings",
                    "style": "approve",
                    "mode": "agent",
                    "command": "",
                    "args": {"issue": "42", "pr": 99, "role": "developer"},
                    "auto_execute": False,
                },
            ],
        }
        monkeypatch.setattr(
            "sova.dashboard.routers.handoff.get_synthesized_handoff",
            AsyncMock(return_value=synthesized),
        )
        monkeypatch.setattr(
            "sova.dashboard.services.control_service.start_agent",
            AsyncMock(return_value={"status": "started", "run_id": 1}),
        )
        mock_invalidate = MagicMock()
        monkeypatch.setattr(
            "sova.dashboard.routers.handoff.invalidate_synthesis_cache",
            mock_invalidate,
        )

        resp = await client.post("/api/handoff/execute", json={"action_id": "address_review"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "Address Review"
        mock_invalidate.assert_called_once_with("42", 99)

    async def test_execute_action_not_found(self, client: AsyncClient, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        monkeypatch.setattr(
            "sova.dashboard.routers.handoff.get_synthesized_handoff",
            AsyncMock(
                return_value={
                    "source": "pr-review-state",
                    "next_actions": [{"id": "integrate", "label": "Integrate"}],
                }
            ),
        )

        resp = await client.post("/api/handoff/execute", json={"action_id": "nonexistent"})
        assert resp.status_code == 404

    async def test_execute_no_handoff(self, client: AsyncClient, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        monkeypatch.setattr(
            "sova.dashboard.routers.handoff.get_synthesized_handoff",
            AsyncMock(return_value=None),
        )

        resp = await client.post("/api/handoff/execute", json={"action_id": "anything"})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# invalidate_synthesis_cache
# ---------------------------------------------------------------------------


class TestInvalidateSynthesisCache:
    def test_clears_both_caches(self) -> None:
        import time

        from sova.dashboard.services.agent_recovery import (
            _issue_pr_cache,
            _synthesis_cache,
            invalidate_synthesis_cache,
        )

        now = time.monotonic()
        _synthesis_cache[("42", 99)] = (now, [{"id": "test"}])
        _issue_pr_cache["42"] = (now, 99)

        invalidate_synthesis_cache("42", 99)

        assert ("42", 99) not in _synthesis_cache
        assert "42" not in _issue_pr_cache


# ---------------------------------------------------------------------------
# _fetch_and_interpret_reviews edge cases
# ---------------------------------------------------------------------------


class TestFetchAndInterpretReviews:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, tmp_path):
        from sova.dashboard.services import agent_recovery

        agent_recovery._synthesis_cache.clear()
        agent_recovery._issue_pr_cache.clear()

        monkeypatch.setattr("sova.dashboard.project_context.get_project_dir", lambda: tmp_path)
        mock_cfg = type(
            "Cfg",
            (),
            {
                "github_repo": "user/repo",
                "github_user": "testuser",
                "task_source": type("TS", (), {"type": "github", "github_project_number": 0})(),
            },
        )()
        monkeypatch.setattr("sova.config.loader.load_config", lambda _: mock_cfg)

    async def test_caches_none_on_adapter_exception(self, monkeypatch) -> None:
        from sova.dashboard.services import agent_recovery

        monkeypatch.setattr("sova.adapters.create_adapter", lambda _: (_ for _ in ()).throw(RuntimeError("boom")))

        result = await agent_recovery._fetch_and_interpret_reviews("42", 99, ("42", 99))
        assert result is None
        assert ("42", 99) in agent_recovery._synthesis_cache

    async def test_caches_none_on_empty_reviews(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services import agent_recovery

        mock_adapter = AsyncMock()
        mock_adapter.get_pr_reviews.return_value = []
        monkeypatch.setattr("sova.adapters.create_adapter", lambda _: mock_adapter)

        result = await agent_recovery._fetch_and_interpret_reviews("42", 99, ("42", 99))
        assert result is None
        assert ("42", 99) in agent_recovery._synthesis_cache


# ---------------------------------------------------------------------------
# synthesize_pr_actions cache and edge case paths
# ---------------------------------------------------------------------------


class TestSynthesizePrActionsCachePaths:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, tmp_path):
        from sova.dashboard.services import agent_recovery

        agent_recovery._synthesis_cache.clear()
        agent_recovery._issue_pr_cache.clear()

        monkeypatch.setattr("sova.dashboard.project_context.get_project_dir", lambda: tmp_path)
        mock_cfg = type(
            "Cfg",
            (),
            {
                "github_repo": "user/repo",
                "github_user": "testuser",
                "task_source": type("TS", (), {"type": "github", "github_project_number": 0})(),
            },
        )()
        monkeypatch.setattr("sova.config.loader.load_config", lambda _: mock_cfg)

    async def test_returns_none_when_no_repo_config(self, monkeypatch) -> None:
        from sova.dashboard.services import agent_recovery

        monkeypatch.setattr("sova.dashboard.project_context.get_project_dir", lambda: None)

        result = await agent_recovery.synthesize_pr_actions("42")
        assert result is None

    async def test_uses_cached_pr_number(self, monkeypatch) -> None:
        import time
        from unittest.mock import AsyncMock

        from sova.dashboard.services import agent_recovery

        agent_recovery._issue_pr_cache["42"] = (time.monotonic(), 99)

        mock_adapter = AsyncMock()
        mock_adapter.get_pr_reviews.return_value = []
        monkeypatch.setattr("sova.adapters.create_adapter", lambda _: mock_adapter)

        mock_find = AsyncMock()
        monkeypatch.setattr("sova.git.operations.find_pr_for_issue", mock_find)

        result = await agent_recovery.synthesize_pr_actions("42")
        assert result is None
        mock_find.assert_not_awaited()

    async def test_returns_cached_synthesis_result(self, monkeypatch) -> None:
        import time

        from sova.dashboard.services import agent_recovery

        now = time.monotonic()
        agent_recovery._issue_pr_cache["42"] = (now, 99)
        agent_recovery._synthesis_cache[("42", 99)] = (now, [{"id": "cached_action"}])

        result = await agent_recovery.synthesize_pr_actions("42")
        assert result == [{"id": "cached_action"}]

    async def test_skips_synthesis_when_active_run(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services import agent_recovery
        from sova.git.pr import PRInfo

        monkeypatch.setattr(
            "sova.git.operations.find_pr_for_issue",
            AsyncMock(return_value=PRInfo(number=99, url="https://github.com/user/repo/pull/99")),
        )
        monkeypatch.setattr(
            "sova.dashboard.services.agent_recovery._has_active_run",
            AsyncMock(return_value=True),
        )

        result = await agent_recovery.synthesize_pr_actions("42")
        assert result is None

    async def test_continues_when_active_run_check_fails(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.adapters.base import PRReview
        from sova.dashboard.services import agent_recovery
        from sova.git.pr import PRInfo

        monkeypatch.setattr(
            "sova.git.operations.find_pr_for_issue",
            AsyncMock(return_value=PRInfo(number=99, url="https://github.com/user/repo/pull/99")),
        )
        monkeypatch.setattr(
            "sova.dashboard.services.agent_recovery._has_active_run",
            AsyncMock(side_effect=RuntimeError("db error")),
        )

        mock_adapter = AsyncMock()
        mock_adapter.get_pr_reviews.return_value = [
            PRReview(reviewer="alice", state="APPROVED", body="ok", submitted_at="2026-01-01T10:00:00Z", is_bot=False),
        ]
        monkeypatch.setattr("sova.adapters.create_adapter", lambda _: mock_adapter)

        result = await agent_recovery.synthesize_pr_actions("42")
        assert result is not None
        assert result[0]["id"] == "integrate"

    async def test_synthesis_cache_hit_after_pr_lookup(self, monkeypatch) -> None:
        import time
        from unittest.mock import AsyncMock

        from sova.dashboard.services import agent_recovery
        from sova.git.pr import PRInfo

        monkeypatch.setattr(
            "sova.git.operations.find_pr_for_issue",
            AsyncMock(return_value=PRInfo(number=99, url="https://github.com/user/repo/pull/99")),
        )

        now = time.monotonic()
        agent_recovery._synthesis_cache[("42", 99)] = (now, [{"id": "cached"}])

        result = await agent_recovery.synthesize_pr_actions("42")
        assert result == [{"id": "cached"}]

    async def test_strips_hash_from_issue_number(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services import agent_recovery
        from sova.git.pr import PRInfo

        mock_find = AsyncMock(return_value=PRInfo(number=99, url="https://github.com/user/repo/pull/99"))
        monkeypatch.setattr("sova.git.operations.find_pr_for_issue", mock_find)

        mock_adapter = AsyncMock()
        mock_adapter.get_pr_reviews.return_value = []
        monkeypatch.setattr("sova.adapters.create_adapter", lambda _: mock_adapter)

        await agent_recovery.synthesize_pr_actions("#42")
        mock_find.assert_awaited_once_with("42", repo="user/repo", github_user="testuser")


# ---------------------------------------------------------------------------
# get_synthesized_handoff
# ---------------------------------------------------------------------------


class TestGetSynthesizedHandoff:
    @staticmethod
    def _mock_session_with_runs(runs, monkeypatch):
        """Build a mock get_session that returns the given runs from the query."""
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, MagicMock

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = runs

        @asynccontextmanager
        async def fake_begin():
            yield

        @asynccontextmanager
        async def fake_session():
            session = AsyncMock()
            session.execute.return_value = mock_result
            session.begin = fake_begin
            yield session

        async def get_session():
            return fake_session()

        monkeypatch.setattr("sova.db.session.get_session", get_session)

    async def test_returns_handoff_for_recent_done_run(self, monkeypatch) -> None:
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock

        from sova.dashboard.services import agent_recovery

        agent_recovery._synthesis_cache.clear()
        agent_recovery._issue_pr_cache.clear()

        mock_run = type(
            "Run",
            (),
            {
                "issue_number": "42",
                "pr_number": 99,
                "ended_at": datetime.now(timezone.utc),
                "started_at": datetime.now(timezone.utc),
            },
        )()

        self._mock_session_with_runs([mock_run], monkeypatch)

        mock_actions = [{"id": "integrate", "label": "Integrate PR"}]
        monkeypatch.setattr(
            "sova.dashboard.services.agent_recovery.synthesize_pr_actions",
            AsyncMock(return_value=mock_actions),
        )

        result = await agent_recovery.get_synthesized_handoff()
        assert result is not None
        assert result["source"] == "pr-review-state"
        assert result["issue"] == "42"
        assert result["pr_number"] == 99

    async def test_returns_none_when_no_runs(self, monkeypatch) -> None:
        from sova.dashboard.services import agent_recovery

        self._mock_session_with_runs([], monkeypatch)

        result = await agent_recovery.get_synthesized_handoff()
        assert result is None

    async def test_returns_none_on_exception(self, monkeypatch) -> None:
        from sova.dashboard.services import agent_recovery

        monkeypatch.setattr("sova.db.session.get_session", lambda: (_ for _ in ()).throw(RuntimeError("db down")))

        result = await agent_recovery.get_synthesized_handoff()
        assert result is None

    async def test_skips_runs_without_issue_number(self, monkeypatch) -> None:
        from datetime import datetime, timezone

        from sova.dashboard.services import agent_recovery

        mock_run = type(
            "Run",
            (),
            {
                "issue_number": None,
                "pr_number": 99,
                "ended_at": datetime.now(timezone.utc),
                "started_at": datetime.now(timezone.utc),
            },
        )()

        self._mock_session_with_runs([mock_run], monkeypatch)

        result = await agent_recovery.get_synthesized_handoff()
        assert result is None


# ---------------------------------------------------------------------------
# execute_handoff_action branch coverage
# ---------------------------------------------------------------------------


class TestExecuteHandoffActionBranches:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        from sova.dashboard.services import handoff_service

        monkeypatch.setattr(handoff_service, "_resolve_project_dir", lambda: tmp_path)
        handoff_service._handoff_caches.clear()

    async def test_execute_claude_command_action(self, client: AsyncClient, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        synthesized = {
            "source": "pr-review-state",
            "issue": "42",
            "pr_number": 99,
            "next_actions": [
                {
                    "id": "approve",
                    "label": "Merge Only",
                    "mode": "claude-command",
                    "command": "/approve-merge 99",
                    "args": {"issue": "42", "pr": 99},
                },
            ],
        }
        monkeypatch.setattr(
            "sova.dashboard.routers.handoff.get_synthesized_handoff",
            AsyncMock(return_value=synthesized),
        )
        monkeypatch.setattr(
            "sova.dashboard.services.control_service.start_command",
            AsyncMock(return_value={"status": "started", "run_id": 2}),
        )
        monkeypatch.setattr(
            "sova.dashboard.routers.handoff.invalidate_synthesis_cache",
            lambda *a: None,
        )

        resp = await client.post("/api/handoff/execute", json={"action_id": "approve"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "Merge Only"

    async def test_execute_shell_action_rejected(self, client: AsyncClient, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        synthesized = {
            "source": "test",
            "next_actions": [
                {"id": "shell_action", "label": "Run Shell", "mode": "shell", "command": "echo hi", "args": {}},
            ],
        }
        monkeypatch.setattr(
            "sova.dashboard.routers.handoff.get_synthesized_handoff",
            AsyncMock(return_value=synthesized),
        )

        resp = await client.post("/api/handoff/execute", json={"action_id": "shell_action"})
        assert resp.status_code == 400
        assert "Shell mode not yet supported" in resp.json()["detail"]

    async def test_execute_unknown_mode_rejected(self, client: AsyncClient, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        synthesized = {
            "source": "test",
            "next_actions": [
                {"id": "weird", "label": "Weird", "mode": "unknown_mode", "command": "x", "args": {}},
            ],
        }
        monkeypatch.setattr(
            "sova.dashboard.routers.handoff.get_synthesized_handoff",
            AsyncMock(return_value=synthesized),
        )

        resp = await client.post("/api/handoff/execute", json={"action_id": "weird"})
        assert resp.status_code == 400
        assert "Unknown execution type" in resp.json()["detail"]


class TestPrsAPI:
    async def test_open_prs_returns_list(self, client: AsyncClient, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        monkeypatch.setattr(
            "sova.dashboard.routers.prs.list_open_prs_with_state",
            AsyncMock(return_value=[{"number": 1, "title": "Test", "computed_state": "draft"}]),
        )
        resp = await client.get("/api/prs/open")
        assert resp.status_code == 200
        data = resp.json()
        assert "prs" in data
        assert len(data["prs"]) == 1
        assert data["prs"][0]["number"] == 1

    async def test_open_prs_empty(self, client: AsyncClient, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        monkeypatch.setattr(
            "sova.dashboard.routers.prs.list_open_prs_with_state",
            AsyncMock(return_value=[]),
        )
        resp = await client.get("/api/prs/open")
        assert resp.status_code == 200
        assert resp.json()["prs"] == []


# ---------------------------------------------------------------------------
# _resolve_issue_from_pr
# ---------------------------------------------------------------------------


class TestResolveIssueFromPr:
    """Tests for _resolve_issue_from_pr in agent_lifecycle."""

    @pytest.mark.asyncio
    async def test_extracts_closes_issue(self, tmp_path, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_lifecycle import _resolve_issue_from_pr

        mock_result = AsyncMock()
        mock_result.return_value.success = True
        mock_result.return_value.stdout = "## Summary\n\nCloses #42\n"
        monkeypatch.setattr("sova.dashboard.services.agent_lifecycle.run_shell", mock_result)

        result = await _resolve_issue_from_pr(99, tmp_path)
        assert result == "42"

    @pytest.mark.asyncio
    async def test_extracts_fixes_issue(self, tmp_path, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_lifecycle import _resolve_issue_from_pr

        mock_result = AsyncMock()
        mock_result.return_value.success = True
        mock_result.return_value.stdout = "Fixes #123"
        monkeypatch.setattr("sova.dashboard.services.agent_lifecycle.run_shell", mock_result)

        result = await _resolve_issue_from_pr(99, tmp_path)
        assert result == "123"

    @pytest.mark.asyncio
    async def test_extracts_resolves_case_insensitive(self, tmp_path, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_lifecycle import _resolve_issue_from_pr

        mock_result = AsyncMock()
        mock_result.return_value.success = True
        mock_result.return_value.stdout = "resolves #77"
        monkeypatch.setattr("sova.dashboard.services.agent_lifecycle.run_shell", mock_result)

        result = await _resolve_issue_from_pr(99, tmp_path)
        assert result == "77"

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_match(self, tmp_path, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_lifecycle import _resolve_issue_from_pr

        mock_result = AsyncMock()
        mock_result.return_value.success = True
        mock_result.return_value.stdout = "Just a regular PR body"
        monkeypatch.setattr("sova.dashboard.services.agent_lifecycle.run_shell", mock_result)

        result = await _resolve_issue_from_pr(99, tmp_path)
        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_empty_on_failure(self, tmp_path, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_lifecycle import _resolve_issue_from_pr

        mock_result = AsyncMock()
        mock_result.return_value.success = False
        mock_result.return_value.stdout = ""
        monkeypatch.setattr("sova.dashboard.services.agent_lifecycle.run_shell", mock_result)

        result = await _resolve_issue_from_pr(99, tmp_path)
        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_empty_on_exception(self, tmp_path, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_lifecycle import _resolve_issue_from_pr

        monkeypatch.setattr(
            "sova.dashboard.services.agent_lifecycle.run_shell",
            AsyncMock(side_effect=OSError("nope")),
        )

        result = await _resolve_issue_from_pr(99, tmp_path)
        assert result == ""


# ---------------------------------------------------------------------------
# _detect_variant / _detect_variant_from_steps -- command role coverage
# ---------------------------------------------------------------------------


class TestDetectVariantCommandRoles:
    """Tests for command/standalone role detection in work_service."""

    def test_detect_variant_command_integrate(self) -> None:
        from sova.dashboard.services.work_service import _detect_variant

        assert _detect_variant(None, role="command:integrate-pr") == "command"

    def test_detect_variant_command_ship(self) -> None:
        from sova.dashboard.services.work_service import _detect_variant

        assert _detect_variant("running", role="command:ship-pr") == "command"

    def test_detect_variant_command_review_pr(self) -> None:
        from sova.dashboard.services.work_service import _detect_variant

        assert _detect_variant(None, role="command:review-pr") == "command"

    def test_detect_variant_reviewer_standalone(self) -> None:
        from sova.dashboard.services.work_service import _detect_variant

        assert _detect_variant(None, role="reviewer") == "command"

    def test_detect_variant_reviewer_with_pr(self) -> None:
        from sova.dashboard.services.work_service import _detect_variant

        assert _detect_variant(None, role="reviewer", pr_number=42) == "command"

    def test_detect_variant_from_steps_command_role(self) -> None:
        from sova.dashboard.services.work_service import _detect_variant_from_steps

        assert _detect_variant_from_steps([], None, role="command:integrate-pr") == "command"

    def test_detect_variant_from_steps_reviewer(self) -> None:
        from sova.dashboard.services.work_service import _detect_variant_from_steps

        assert _detect_variant_from_steps([], None, role="reviewer") == "command"

    def test_detect_variant_from_steps_developer_not_command(self) -> None:
        from sova.dashboard.services.work_service import _detect_variant_from_steps

        assert _detect_variant_from_steps([], None, role="developer") != "command"


# ---------------------------------------------------------------------------
# WebSocket agent status
# ---------------------------------------------------------------------------


class TestWebSocketAgentStatus:
    def test_websocket_connect_and_receive_status_update(self) -> None:
        """WebSocket endpoint accepts connection and sends status_update with runs."""
        from starlette.testclient import TestClient

        from sova.dashboard.app import create_app

        app = create_app(multi_project=False)
        client = TestClient(app)
        with client.websocket_connect("/api/ws/agents/status") as ws:
            data = ws.receive_json()
            assert data["type"] == "status_update"
            assert isinstance(data["runs"], list)

    def test_websocket_client_disconnect(self) -> None:
        """Verify connection is removed from manager after client disconnects."""
        from starlette.testclient import TestClient

        from sova.dashboard.app import create_app
        from sova.dashboard.routers.agents import _ws_manager

        app = create_app(multi_project=False)
        client = TestClient(app)
        before = len(_ws_manager.active_connections)
        with client.websocket_connect("/api/ws/agents/status") as ws:
            ws.receive_json()
        # After disconnect, connection count should be back to baseline
        assert len(_ws_manager.active_connections) == before

    def test_websocket_multiple_clients(self) -> None:
        """Multiple clients can connect and each receives updates."""
        from starlette.testclient import TestClient

        from sova.dashboard.app import create_app

        app = create_app(multi_project=False)
        client = TestClient(app)
        with client.websocket_connect("/api/ws/agents/status") as ws1:
            with client.websocket_connect("/api/ws/agents/status") as ws2:
                d1 = ws1.receive_json()
                d2 = ws2.receive_json()
                assert d1["type"] == "status_update"
                assert d2["type"] == "status_update"

    def test_websocket_error_handling(self) -> None:
        """When get_all_agent_statuses raises, endpoint sends empty runs and does not crash."""
        from unittest.mock import AsyncMock, patch

        from starlette.testclient import TestClient

        from sova.dashboard.app import create_app

        app = create_app(multi_project=False)
        client = TestClient(app)
        with (
            patch(
                "sova.dashboard.services.agent_status.get_all_agent_statuses",
                new_callable=AsyncMock,
                side_effect=RuntimeError("DB failure"),
            ),
            client.websocket_connect("/api/ws/agents/status") as ws,
        ):
            data = ws.receive_json()
            assert data["type"] == "status_update"
            assert data["runs"] == []

    def test_websocket_multi_project_isolation(self) -> None:
        """ConnectionManager groups connections by project_dir for isolation."""
        import asyncio
        from pathlib import Path
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.routers.agents import _ConnectionManager

        mgr = _ConnectionManager()

        ws_a = MagicMock()
        ws_a.send_json = AsyncMock()
        ws_b = MagicMock()
        ws_b.send_json = AsyncMock()

        dir_a = Path("/project-a")
        dir_b = Path("/project-b")

        # Patch create_task to avoid needing a running event loop
        dummy_task = MagicMock()
        dummy_task.done.return_value = False
        with patch("asyncio.create_task", return_value=dummy_task):
            mgr.connect(ws_a, dir_a)
            mgr.connect(ws_b, dir_b)

        # Each project group has exactly one connection
        assert len(mgr._groups.get(dir_a, [])) == 1
        assert len(mgr._groups.get(dir_b, [])) == 1

        # Broadcast to project A should only reach ws_a
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(mgr._broadcast({"type": "test"}, dir_a))
        finally:
            loop.close()
        ws_a.send_json.assert_awaited_once_with({"type": "test"})
        ws_b.send_json.assert_not_awaited()

        # Disconnect from project A should not affect project B
        mgr.disconnect(ws_a, dir_a)
        assert len(mgr._groups.get(dir_a, [])) == 0
        assert len(mgr._groups.get(dir_b, [])) == 1

        mgr.disconnect(ws_b, dir_b)
        assert len(mgr._groups.get(dir_b, [])) == 0
