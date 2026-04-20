"""Tests for sova.dashboard -- FastAPI dashboard with DB-backed queries."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sova.db.models import CostRecord, Memory, StepExecution, TaskRun
from sova.db.session import close_db, get_session, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    """Initialize an in-memory DB for dashboard tests."""
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db()
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
    async def test_root_redirects_to_overview(self, client: AsyncClient) -> None:
        resp = await client.get("/", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/overview"

    async def test_overview_page_loads(self, client: AsyncClient) -> None:
        resp = await client.get("/overview")
        assert resp.status_code == 200
        assert "Overview" in resp.text

    async def test_runs_page_loads(self, client: AsyncClient) -> None:
        resp = await client.get("/runs")
        assert resp.status_code == 200
        assert "Runs" in resp.text

    async def test_costs_page_loads(self, client: AsyncClient) -> None:
        resp = await client.get("/costs")
        assert resp.status_code == 200
        assert "Cost" in resp.text

    async def test_control_page_loads(self, client: AsyncClient) -> None:
        resp = await client.get("/control")
        assert resp.status_code == 200
        assert "Control" in resp.text

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
    async def test_get_handoff_none(self, client: AsyncClient) -> None:
        resp = await client.get("/api/handoff")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_handoff"] is False

    async def test_get_handoff_with_file(self, client: AsyncClient, tmp_path) -> None:
        import json

        from sova.dashboard.services import handoff_service

        # Write a handoff file directly
        handoff_service.set_project_dir(tmp_path)
        control_dir = tmp_path / ".claude" / "agent-control"
        control_dir.mkdir(parents=True)
        handoff_data = {
            "id": "test-123",
            "source": "ship-pr",
            "status": "awaiting_action",
            "summary": "Test handoff",
            "created_at": "2026-04-20T10:00:00Z",
            "next_actions": [
                {"id": "merge", "label": "Merge PR", "style": "approve", "mode": "claude-command", "command": "approve-merge"},
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

        from sova.dashboard.services import handoff_service

        handoff_service.set_project_dir(tmp_path)
        control_dir = tmp_path / ".claude" / "agent-control"
        control_dir.mkdir(parents=True)
        (control_dir / "handoff.json").write_text(json.dumps({
            "id": "test-456",
            "source": "test",
            "status": "completed",
            "summary": "Done",
            "created_at": "2026-04-20T10:00:00Z",
        }))

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
        assert resp.status_code == 200
        data = resp.json()
        assert data["error"] == "No active handoff"

    async def test_execute_action_not_found(self, client: AsyncClient, tmp_path) -> None:
        import json

        from sova.dashboard.services import handoff_service

        handoff_service.set_project_dir(tmp_path)
        control_dir = tmp_path / ".claude" / "agent-control"
        control_dir.mkdir(parents=True)
        (control_dir / "handoff.json").write_text(json.dumps({
            "id": "test-789",
            "source": "test",
            "status": "awaiting_action",
            "summary": "Test",
            "created_at": "2026-04-20T10:00:00Z",
            "next_actions": [{"id": "merge", "label": "Merge"}],
        }))

        resp = await client.post(
            "/api/handoff/execute",
            json={"action_id": "nonexistent"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "not found" in data["error"]

    async def test_control_page_has_handoff_panel(self, client: AsyncClient) -> None:
        resp = await client.get("/control")
        assert resp.status_code == 200
        assert "handoff-panel" in resp.text
        assert "handoff-complete-panel" in resp.text
        assert "handoff-failed-panel" in resp.text
        assert "checkHandoff" in resp.text


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
        assert resp.headers["location"] == "/p/alpha/overview"

    async def test_project_overview(self, multi_client: AsyncClient) -> None:
        resp = await multi_client.get("/p/alpha/overview")
        assert resp.status_code == 200
        assert "Overview" in resp.text

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
        assert "time_in_state" in active

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

    async def test_tasks_page_renders(self, client: AsyncClient) -> None:
        resp = await client.get("/tasks")
        assert resp.status_code == 200
        assert b"Tasks" in resp.content

    async def test_queue_page_renders(self, client: AsyncClient) -> None:
        resp = await client.get("/queue")
        assert resp.status_code == 200
        assert b"Priority Queue" in resp.content


class TestLogsAPI:
    """Tests for the logs API endpoints."""

    async def test_logs_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/logs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["entries"] == []
        assert data["total"] == 0

    async def test_logs_with_data(self, client: AsyncClient, tmp_path, monkeypatch) -> None:
        import sova.dashboard.services.log_service as log_mod

        # Write a test log file
        log_dir = tmp_path / ".claude"
        log_dir.mkdir()
        log_file = log_dir / "sova.log"
        import json
        lines = [
            json.dumps({"level": "INFO", "message": "Agent started", "component": "core", "timestamp": "2026-01-01T10:00:00"}),
            json.dumps({"level": "ERROR", "message": "Test failed", "component": "runner", "timestamp": "2026-01-01T10:01:00"}),
            json.dumps({"level": "INFO", "message": "Agent completed", "component": "core", "timestamp": "2026-01-01T10:02:00"}),
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
        import sova.dashboard.services.log_service as log_mod

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

        resp = await client.post("/api/setup/configure", json={
            "project_path": str(project),
            "github_repo": "user/test",
            "base_branch": "main",
            "agent_model": "opus",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert (project / "sova.toml").exists()
        toml_content = (project / "sova.toml").read_text()
        assert 'github_repo = "user/test"' in toml_content

        reg_mod._REGISTRY_FILE = orig_file
        reg_mod._REGISTRY_DIR = orig_dir
