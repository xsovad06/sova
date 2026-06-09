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
        assert "Issue #42" in resp.text

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
            patch("sova.ipc.control.AgentProcess.spawn", new_callable=AsyncMock, return_value=mock_process),
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
            patch("sova.ipc.control.AgentProcess.spawn", new_callable=AsyncMock, return_value=mock_process),
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
            patch("sova.ipc.control.AgentProcess.spawn", new_callable=AsyncMock, return_value=mock_process),
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
            patch("sova.ipc.control.AgentProcess.spawn", new_callable=AsyncMock, return_value=mock_process),
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

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch(
                "sova.ipc.control.AgentProcess.spawn", new_callable=AsyncMock, return_value=mock_process
            ) as mock_spawn,
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
        prompt_arg = mock_spawn.call_args.kwargs.get("prompt") or mock_spawn.call_args[1].get("prompt", "")
        assert "--run-id 7" in prompt_arg

    async def test_start_agent_cleans_up_on_spawn_failure(self) -> None:
        """If process spawn fails, the pre-created TaskRun should be marked failed."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        pa = ProjectAgents()

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch("sova.ipc.control.AgentProcess.spawn", new_callable=AsyncMock, side_effect=OSError("spawn failed")),
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
        assert data["detail"] == "No active handoff"

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
        assert "Issue #42" in resp.text


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


class TestBatchAPI:
    """Tests for the batch queue API endpoints."""

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
        assert resp.status_code == 400
        data = resp.json()
        assert "detail" in data

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


class TestAgentsAPI:
    """Tests for the new agents API endpoints."""

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
        statuses = {r["status"] for r in result}
        assert statuses == {"done", "failed", "interrupted"}
        assert all(r["issue_number"] != "4" for r in result)

    async def test_get_work_history_status_filter(self, session: AsyncSession) -> None:
        from sova.dashboard.services.work_service import get_work_history

        now = datetime.now(timezone.utc)
        async with session.begin():
            session.add(TaskRun(issue_number="1", role="dev", status="done", started_at=now, ended_at=now))
            session.add(TaskRun(issue_number="2", role="dev", status="failed", started_at=now, ended_at=now))

        result = await get_work_history(session, status="done")
        assert len(result) == 1
        assert result[0]["status"] == "done"

    async def test_get_work_history_role_filter(self, session: AsyncSession) -> None:
        from sova.dashboard.services.work_service import get_work_history

        now = datetime.now(timezone.utc)
        async with session.begin():
            session.add(TaskRun(issue_number="1", role="developer", status="done", started_at=now, ended_at=now))
            session.add(TaskRun(issue_number="2", role="triage", status="done", started_at=now, ended_at=now))

        result = await get_work_history(session, role="triage")
        assert len(result) == 1
        assert result[0]["role"] == "triage"

    async def test_get_work_history_limit_capped(self, session: AsyncSession) -> None:
        from sova.dashboard.services.work_service import get_work_history

        now = datetime.now(timezone.utc)
        async with session.begin():
            for i in range(5):
                session.add(TaskRun(issue_number=str(i), role="dev", status="done", started_at=now, ended_at=now))

        result = await get_work_history(session, limit=2)
        assert len(result) == 2

    async def test_get_work_history_accepts_large_limit(self, session: AsyncSession) -> None:
        from sova.dashboard.services.work_service import get_work_history

        now = datetime.now(timezone.utc)
        async with session.begin():
            session.add(TaskRun(issue_number="1", role="dev", status="done", started_at=now, ended_at=now))

        result = await get_work_history(session, limit=999)
        assert len(result) == 1

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
            patch("sova.ipc.control.AgentProcess.spawn", new_callable=AsyncMock, return_value=mock_process),
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
        assert result["total_steps"] == 7

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

    def test_reviewer_role_with_pr_number_is_not_address_review(self) -> None:
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        result = get_step_progress("commit", role="reviewer", pr_number=147)
        assert result["pipeline_variant"] == "developer"


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
