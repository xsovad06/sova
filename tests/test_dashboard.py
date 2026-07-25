"""Tests for sova.dashboard -- FastAPI dashboard with DB-backed queries."""

from __future__ import annotations

import asyncio
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
    async def test_healthz_returns_ok(self, client: AsyncClient) -> None:
        resp = await client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

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

    async def test_costs_page_has_per_section_error_handling(self, client: AsyncClient) -> None:
        resp = await client.get("/costs")
        assert resp.status_code == 200
        body = resp.text
        assert "function loadSummary()" in body
        assert "function loadEnergy()" in body
        assert "function loadDailyChart()" in body
        assert "function loadCostTable(" in body
        assert "function renderSectionError(" in body
        assert "function setElementError(" in body
        assert "} catch (err)" in body
        # Verify energy error handler clears detail elements
        assert "cost-energy-detail" in body
        assert "cost-co2-detail" in body

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

    async def test_status_badge_has_step_label_mapping(self, client: AsyncClient) -> None:
        resp = await client.get("/static/app.js")
        js = resp.text
        assert "_STEP_LABELS" in js
        assert "_WAITING_STEPS" in js
        assert "_computeBadgeLabel" in js
        assert "_getBadgeColors" in js
        for step in ["monitor_ci", "wait_for_external_reviews", "handoff_to_reviewer", "handoff_to_user"]:
            assert step in js, f"waiting step {step} missing from _WAITING_STEPS"

    async def test_status_badge_five_state_colors(self, client: AsyncClient) -> None:
        resp = await client.get("/static/app.js")
        js = resp.text
        assert "done:" in js
        # Done uses blue (accent) not green
        done_idx = js.index("done:")
        done_section = js[done_idx : done_idx + 120]
        assert "bg-accent'" in done_section or "bg-accent," in done_section or "'bg-accent'" in done_section
        # Running states use green
        running_idx = js.index("running:")
        running_section = js[running_idx : running_idx + 120]
        assert "bg-accent-green" in running_section
        # CI monitoring uses peach (waiting) via _WAITING_COLOR constant
        assert "_WAITING_COLOR" in js
        ci_idx = js.index("ci_monitoring:")
        ci_section = js[ci_idx : ci_idx + 120]
        assert "_WAITING_COLOR" in ci_section

    async def test_awaiting_approval_is_terminal_with_color(self, client: AsyncClient) -> None:
        resp = await client.get("/static/app.js")
        js = resp.text
        # awaiting_approval must be in _STATUS_TERMINAL
        terminal_idx = js.index("_STATUS_TERMINAL")
        terminal_section = js[terminal_idx : terminal_idx + 200]
        assert "awaiting_approval" in terminal_section
        # awaiting_approval must have a color mapping in STATUS_COLORS
        colors_idx = js.index("STATUS_COLORS")
        colors_section = js[colors_idx : js.index("};", colors_idx)]
        assert "awaiting_approval" in colors_section

    async def test_badge_label_terminal_states_capitalized(self, client: AsyncClient) -> None:
        """Verify _computeBadgeLabel logic: terminal states produce capitalized labels."""
        resp = await client.get("/static/app.js")
        js = resp.text
        # Extract terminal statuses from _STATUS_TERMINAL
        import re

        terminal_match = re.search(r"_STATUS_TERMINAL\s*=\s*\{([^}]+)\}", js)
        assert terminal_match, "_STATUS_TERMINAL not found"
        terminal_keys = re.findall(r"(\w+)\s*:", terminal_match.group(1))
        assert len(terminal_keys) >= 5, f"Expected at least 5 terminal states, got {terminal_keys}"
        for status in ["done", "failed", "interrupted", "rejected", "paused"]:
            assert status in terminal_keys, f"{status} missing from _STATUS_TERMINAL"

    async def test_badge_label_waiting_steps_use_waiting_prefix(self, client: AsyncClient) -> None:
        """Verify waiting steps are in _WAITING_STEPS and use peach color."""
        resp = await client.get("/static/app.js")
        js = resp.text
        import re

        waiting_match = re.search(r"_WAITING_STEPS\s*=\s*\{([^}]+)\}", js)
        assert waiting_match, "_WAITING_STEPS not found"
        waiting_keys = re.findall(r"(\w+)\s*:", waiting_match.group(1))
        expected_waiting = ["monitor_ci", "wait_for_external_reviews", "handoff_to_reviewer", "handoff_to_user"]
        for step in expected_waiting:
            assert step in waiting_keys, f"{step} missing from _WAITING_STEPS"
        # Verify _computeBadgeLabel uses 'Waiting: ' prefix for waiting steps
        assert "'Waiting: '" in js or '"Waiting: "' in js

    async def test_badge_label_running_steps_use_running_prefix(self, client: AsyncClient) -> None:
        """Verify non-waiting, non-terminal steps produce 'Running: {label}' badges."""
        resp = await client.get("/static/app.js")
        js = resp.text
        assert "'Running: '" in js or '"Running: "' in js
        # Verify non-waiting steps like 'develop' have explicit labels
        import re

        labels_match = re.search(r"_STEP_LABELS\s*=\s*\{([^}]+)\}", js)
        assert labels_match
        # develop should map to 'Developing', not be in _WAITING_STEPS
        assert "develop:" in labels_match.group(1)
        assert "'Developing'" in labels_match.group(1) or '"Developing"' in labels_match.group(1)

    async def test_badge_fallback_label_capitalizes_first_letter(self, client: AsyncClient) -> None:
        """Verify unmapped steps get capitalized fallback labels."""
        resp = await client.get("/static/app.js")
        js = resp.text
        # The fallback path should capitalize: rawLabel.charAt(0).toUpperCase()
        assert "charAt(0).toUpperCase()" in js

    async def test_badge_non_terminal_states_use_green_not_blue(self, client: AsyncClient) -> None:
        """Verify transitional states (researched, pr_created) use green, not blue."""
        resp = await client.get("/static/app.js")
        js = resp.text
        import re

        colors_match = re.search(r"STATUS_COLORS\s*=\s*\{(.+?)\n\};", js, re.DOTALL)
        assert colors_match
        colors_block = colors_match.group(1)
        # researched and pr_created should use accent-green (running), not accent (blue/completed)
        for status in ["researched", "pr_created"]:
            idx = colors_block.index(f"{status}:")
            section = colors_block[idx : idx + 120]
            assert "bg-accent-green" in section, f"{status} should use green (running) color, not blue"

    async def test_ws_notifications_cover_all_terminal_states(self, client: AsyncClient) -> None:
        """Verify WebSocket notification handler fires for all terminal states except paused."""
        resp = await client.get("/agents")
        html = resp.text
        # Should use _STATUS_TERMINAL lookup instead of hardcoded done/failed check
        assert "_STATUS_TERMINAL[curr]" in html, "Notification check should use _STATUS_TERMINAL lookup"
        # Should exclude paused (user-initiated, not worth notifying)
        assert "!== 'paused'" in html or '!== "paused"' in html

    async def test_agents_page_has_ws_notification_tracking(self, client: AsyncClient) -> None:
        resp = await client.get("/agents")
        html = resp.text
        assert "_wsPreviousStatuses" in html
        assert "sendBrowserNotification" in html

    async def test_supervisor_page_loads(self, client: AsyncClient) -> None:
        from dataclasses import dataclass
        from unittest.mock import patch

        @dataclass
        class FakeCfg:
            github_repo: str = "owner/repo"

        with (
            patch("sova.dashboard.project_context.get_project_dir", return_value="/tmp/proj"),
            patch("sova.config.loader.load_config", return_value=FakeCfg()),
        ):
            resp = await client.get("/supervisor")
        assert resp.status_code == 200

    async def test_supervisor_page_falls_back_on_config_error(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        with patch("sova.config.loader.load_config", side_effect=RuntimeError("no config")):
            resp = await client.get("/supervisor")
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

    async def test_costs_by_routing_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/costs/by-routing")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    async def test_costs_by_routing_with_data(self, client: AsyncClient, seed_data) -> None:
        resp = await client.get("/api/costs/by-routing")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        # All seed records have no model_selection_reason, so grouped as "untracked"
        assert any(r["reason"] == "untracked" for r in data)

    async def test_costs_by_routing_with_reasons(self, client: AsyncClient, session: AsyncSession) -> None:
        """Routing breakdown distinguishes tracked reasons from untracked."""
        session.add_all(
            [
                CostRecord(
                    phase="triage",
                    issue="80",
                    model="haiku",
                    cost_usd=Decimal("0.01"),
                    model_selection_reason="role:triage->haiku",
                ),
                CostRecord(
                    phase="develop",
                    issue="81",
                    model="opus",
                    cost_usd=Decimal("0.50"),
                    model_selection_reason="complexity:complex->opus",
                ),
                CostRecord(
                    phase="review",
                    issue="82",
                    model="sonnet",
                    cost_usd=Decimal("0.10"),
                    model_selection_reason="",
                ),
            ]
        )
        await session.commit()

        resp = await client.get("/api/costs/by-routing")
        assert resp.status_code == 200
        data = resp.json()
        reasons = {r["reason"] for r in data}
        assert "role:triage->haiku" in reasons
        assert "complexity:complex->opus" in reasons
        # Empty string reason is grouped as "untracked"
        assert "untracked" in reasons
        # Cost values must be JSON-serializable floats, not Decimal
        for entry in data:
            assert isinstance(entry["cost_usd"], (int, float))


class TestCostsAPIErrorHandling:
    """Cover the except branches in sova/dashboard/routers/costs.py."""

    async def test_cost_summary_error(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        with patch("sova.dashboard.routers.costs.cost_service.get_summary", side_effect=RuntimeError("db down")):
            resp = await client.get("/api/costs/summary")
        assert resp.status_code == 500
        assert "Failed to fetch cost summary" in resp.json()["detail"]

    async def test_daily_costs_error(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        with patch("sova.dashboard.routers.costs.cost_service.get_daily", side_effect=RuntimeError("db down")):
            resp = await client.get("/api/costs/daily")
        assert resp.status_code == 500
        assert "Failed to fetch daily costs" in resp.json()["detail"]

    async def test_costs_by_issue_error(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        with patch("sova.dashboard.routers.costs.cost_service.get_by_issue", side_effect=RuntimeError("db down")):
            resp = await client.get("/api/costs/by-issue")
        assert resp.status_code == 500
        assert "Failed to fetch costs by issue" in resp.json()["detail"]

    async def test_costs_by_phase_error(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        with patch("sova.dashboard.routers.costs.cost_service.get_by_phase", side_effect=RuntimeError("db down")):
            resp = await client.get("/api/costs/by-phase")
        assert resp.status_code == 500
        assert "Failed to fetch costs by phase" in resp.json()["detail"]

    async def test_costs_by_model_error(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        with patch("sova.dashboard.routers.costs.cost_service.get_by_model", side_effect=RuntimeError("db down")):
            resp = await client.get("/api/costs/by-model")
        assert resp.status_code == 500
        assert "Failed to fetch costs by model" in resp.json()["detail"]

    async def test_costs_by_routing_error(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        with patch("sova.dashboard.routers.costs.cost_service.get_by_routing", side_effect=RuntimeError("db down")):
            resp = await client.get("/api/costs/by-routing")
        assert resp.status_code == 500
        assert "Failed to fetch costs by routing" in resp.json()["detail"]


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

    async def test_recover_stale_runs_skips_paused(self) -> None:
        """Paused runs (gate failures) must not be clobbered to interrupted."""
        from sova.dashboard.services.control_service import recover_stale_runs

        session = await get_session()
        async with session.begin():
            run = TaskRun(
                issue_number="77",
                role="researcher",
                status="paused",
                pid=999999,
            )
            session.add(run)
            await session.flush()
            run_id = run.id

        interrupted = await recover_stale_runs()
        assert all(r["run_id"] != run_id for r in interrupted)

        session2 = await get_session()
        async with session2.begin():
            updated = await session2.get(TaskRun, run_id)
            assert updated.status == "paused"

    async def test_is_process_alive(self) -> None:
        """Process liveness check should work for known PIDs."""
        import os

        from sova.dashboard.services.control_service import _is_process_alive

        # Current process is alive
        assert _is_process_alive(os.getpid()) is True
        # Non-existent PID
        assert _is_process_alive(999999) is False


# ---------------------------------------------------------------------------
# Background task shutdown
# ---------------------------------------------------------------------------


class TestCancelBackgroundTasks:
    async def test_cancels_pending_tasks(self) -> None:
        """cancel_background_tasks should cancel all tasks in _background_tasks."""
        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.agent_lifecycle import cancel_background_tasks

        finished = False

        async def _long_running() -> None:
            nonlocal finished
            await asyncio.sleep(3600)
            finished = True

        task = asyncio.create_task(_long_running())
        agent_lifecycle._background_tasks.add(task)
        try:
            await cancel_background_tasks()
            assert task.cancelled()
            assert not finished
            assert len(agent_lifecycle._background_tasks) == 0
        finally:
            agent_lifecycle._background_tasks.discard(task)

    async def test_idempotent_when_empty(self) -> None:
        """cancel_background_tasks should be safe to call with no tasks."""
        from sova.dashboard.services.agent_lifecycle import (
            _background_tasks,
            cancel_background_tasks,
        )

        _background_tasks.clear()
        await cancel_background_tasks()
        assert len(_background_tasks) == 0

    async def test_cancels_per_agent_io_tasks(self) -> None:
        """cancel_background_tasks must also cancel per-agent reader/stderr/resource tasks."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_lifecycle import cancel_background_tasks
        from sova.dashboard.services.agent_pool import AgentState, _get_project_agents

        pa = _get_project_agents()
        mock_process = MagicMock()
        mock_process.pid = 99999

        agent = AgentState(run_id=9999, issue="999", role="developer", process=mock_process)

        reader_done = False
        stderr_done = False

        async def _fake_reader() -> None:
            nonlocal reader_done
            await asyncio.sleep(3600)
            reader_done = True

        async def _fake_stderr() -> None:
            nonlocal stderr_done
            await asyncio.sleep(3600)
            stderr_done = True

        agent.reader_task = asyncio.create_task(_fake_reader())
        agent.stderr_task = asyncio.create_task(_fake_stderr())
        pa.agents[9999] = agent

        try:
            await cancel_background_tasks()
            assert agent.reader_task.cancelled()
            assert agent.stderr_task.cancelled()
            assert not reader_done
            assert not stderr_done
        finally:
            del pa.agents[9999]


class TestShutdownTasks:
    """Cover the _shutdown_tasks helper in app.py."""

    async def test_cancels_all_task_types(self) -> None:
        """_shutdown_tasks cancels sweep, pr_throttle, pr_monitor tasks and stops metrics_writer."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.app import _shutdown_tasks

        async def _hang() -> None:
            await asyncio.sleep(3600)

        sweep = asyncio.create_task(_hang())
        throttle = asyncio.create_task(_hang())
        monitor = asyncio.create_task(_hang())
        writer = AsyncMock()

        cancel_bg = "sova.dashboard.services.agent_lifecycle.cancel_background_tasks"
        cancel_batch = "sova.dashboard.services.batch_service.cancel_all_batches"
        with (
            patch(cancel_bg, new_callable=AsyncMock) as mock_bg,
            patch("sova.dashboard.routers.agents._ws_manager") as mock_ws,
            patch(cancel_batch, new_callable=AsyncMock) as mock_batch,
        ):
            mock_ws.cancel_all = AsyncMock()
            await _shutdown_tasks(sweep, [throttle], [monitor], writer)
            mock_bg.assert_awaited_once()
            mock_ws.cancel_all.assert_awaited_once()
            mock_batch.assert_awaited_once()

        assert sweep.cancelled()
        assert throttle.cancelled()
        assert monitor.cancelled()
        writer.stop.assert_awaited_once()

    async def test_handles_none_metrics_writer(self) -> None:
        """_shutdown_tasks works when metrics_writer is None."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.app import _shutdown_tasks

        async def _hang() -> None:
            await asyncio.sleep(3600)

        sweep = asyncio.create_task(_hang())

        cancel_bg = "sova.dashboard.services.agent_lifecycle.cancel_background_tasks"
        cancel_batch = "sova.dashboard.services.batch_service.cancel_all_batches"
        with (
            patch(cancel_bg, new_callable=AsyncMock) as mock_bg,
            patch("sova.dashboard.routers.agents._ws_manager") as mock_ws,
            patch(cancel_batch, new_callable=AsyncMock) as mock_batch,
        ):
            mock_ws.cancel_all = AsyncMock()
            await _shutdown_tasks(sweep, [], [], None)
            mock_bg.assert_awaited_once()
            mock_ws.cancel_all.assert_awaited_once()
            mock_batch.assert_awaited_once()

        assert sweep.cancelled()

    async def test_handles_empty_bg_task_lists(self) -> None:
        """_shutdown_tasks works with empty pr_throttle and pr_monitor lists."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.app import _shutdown_tasks

        async def _hang() -> None:
            await asyncio.sleep(3600)

        sweep = asyncio.create_task(_hang())

        cancel_bg = "sova.dashboard.services.agent_lifecycle.cancel_background_tasks"
        cancel_batch = "sova.dashboard.services.batch_service.cancel_all_batches"
        with (
            patch(cancel_bg, new_callable=AsyncMock) as mock_bg,
            patch("sova.dashboard.routers.agents._ws_manager") as mock_ws,
            patch(cancel_batch, new_callable=AsyncMock) as mock_batch,
        ):
            mock_ws.cancel_all = AsyncMock()
            await _shutdown_tasks(sweep, [], [], None)
            mock_bg.assert_awaited_once()
            mock_ws.cancel_all.assert_awaited_once()
            mock_batch.assert_awaited_once()

        assert sweep.cancelled()


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

    async def test_does_not_persist_when_issue_mismatches_with_nonempty_details(self) -> None:
        """When the handoff issue mismatches the agent issue, no persist must happen
        even if details is non-empty -- writing another issue's verdict would corrupt
        the completing run's handoff_json."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.db.session import get_session as real_get_session
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        findings = [{"file": "b.py", "line": 2, "severity": 8, "category": "bug", "description": "Y", "suggestion": ""}]

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="200", role="reviewer", status="done", pr_number=60)
                session.add(run)
                await session.flush()
                run_id = run.id

        # Handoff is for issue 201 but agent is running issue 200 -- mismatch
        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue="201",
            pr_number=60,
            summary="1 finding",
            details={"next_action": "address_review", "pending_findings": findings},
            next_actions=[
                HandoffAction(
                    id="address_review",
                    label="Address",
                    auto_execute=True,
                    mode="agent",
                    args={"issue": "201", "role": "developer", "pr": 60},
                ),
            ],
        )

        agent = type("AgentState", (), {"run_id": run_id, "issue": "200", "project_dir": Path("/tmp/test")})()

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        mock_start = AsyncMock()
        mock_clear = MagicMock()
        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.dashboard.services.handoff_service.clear_handoff", mock_clear),
            patch("sova.db.session.get_session", side_effect=_ignore_project_dir),
        ):
            await _process_auto_handoff(agent)

        mock_start.assert_not_awaited()
        mock_clear.assert_not_called()

        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed is not None
                assert not refreshed.handoff_json, "mismatched handoff details must not be persisted"

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

    async def test_persists_handoff_details_to_completing_run(self) -> None:
        """_process_auto_handoff must persist reviewer handoff details to TaskRun.handoff_json
        before clearing the file, so get_sova_review_verdict() finds the real verdict later."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.db.session import get_session as real_get_session
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        findings = [{"file": "a.py", "line": 1, "severity": 9, "category": "bug", "description": "X", "suggestion": ""}]

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="120", role="reviewer", status="done", pr_number=50)
                session.add(run)
                await session.flush()
                run_id = run.id

        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue="120",
            pr_number=50,
            summary="1 finding",
            details={"next_action": "address_review", "pending_findings": findings, "cost_usd": "0.01"},
            next_actions=[
                HandoffAction(
                    id="address_review",
                    label="Address",
                    auto_execute=True,
                    mode="agent",
                    args={"issue": "120", "role": "developer", "pr": 50},
                ),
            ],
        )

        agent = type("AgentState", (), {"run_id": run_id, "issue": "120", "project_dir": Path("/tmp/test")})()

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        mock_start = AsyncMock(return_value={"run_id": 99})
        mock_clear = MagicMock()
        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.dashboard.services.handoff_service.clear_handoff", mock_clear),
            patch("sova.db.session.get_session", side_effect=_ignore_project_dir),
            patch(
                "sova.config.loader.load_config",
                return_value=MagicMock(pipeline=MagicMock(max_address_review_cycles=0)),
            ),
        ):
            await _process_auto_handoff(agent)

        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed is not None
                assert refreshed.handoff_json is not None, "handoff_json must be persisted before file is cleared"
                assert refreshed.handoff_json.get("next_action") == "address_review"
                persisted_findings = refreshed.handoff_json.get("pending_findings", [])
                assert len(persisted_findings) == 1
                assert persisted_findings[0]["severity"] == 9

    async def test_skips_persist_when_handoff_json_already_set(self) -> None:
        """_process_auto_handoff must not overwrite handoff_json if already populated
        (subprocess wrote it correctly to the right DB)."""
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.db.session import get_session as real_get_session
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        existing_handoff = {"next_action": "approve", "pending_findings": [], "role": "reviewer"}

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="121",
                    role="reviewer",
                    status="done",
                    pr_number=51,
                    handoff_json=existing_handoff,
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue="121",
            pr_number=51,
            summary="No findings",
            details={"next_action": "approve", "pending_findings": [], "cost_usd": "0.01"},
            next_actions=[
                HandoffAction(
                    id="integrate",
                    label="Integrate PR",
                    auto_execute=False,
                    mode="claude-command",
                    command="/integrate-pr 51",
                ),
            ],
        )

        agent = type("AgentState", (), {"run_id": run_id, "issue": "121", "project_dir": Path("/tmp/test")})()

        original = real_get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        mock_clear = MagicMock()
        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch("sova.dashboard.services.handoff_service.clear_handoff", mock_clear),
            patch("sova.db.session.get_session", side_effect=_ignore_project_dir),
        ):
            await _process_auto_handoff(agent)

        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed.handoff_json == existing_handoff, "pre-existing handoff_json must not be overwritten"


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
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_resolve_branch_name", new_callable=AsyncMock, return_value="feat/test"),
            patch.object(
                agent_lifecycle,
                "_resolve_issue_worktree",
                new_callable=AsyncMock,
                return_value=Path("/tmp/wt"),
            ),
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
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_resolve_branch_name", new_callable=AsyncMock, return_value="feat/test"),
            patch.object(
                agent_lifecycle,
                "_resolve_issue_worktree",
                new_callable=AsyncMock,
                return_value=Path("/tmp/wt"),
            ),
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
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_resolve_branch_name", new_callable=AsyncMock, return_value="feat/test"),
            patch.object(
                agent_lifecycle,
                "_resolve_issue_worktree",
                new_callable=AsyncMock,
                return_value=Path("/tmp/wt"),
            ),
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

    async def test_start_agent_resolves_issue_from_pr(self) -> None:
        """When issue is empty but pr_number is set, derive issue from PR body."""
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
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=10) as mock_create,
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_link_run_to_lifecycle", new_callable=AsyncMock),
            patch(
                "sova.dashboard.services.agent_lifecycle._resolve_issue_from_pr",
                new_callable=AsyncMock,
                return_value="55",
            ) as mock_resolve,
            patch(
                "sova.dashboard.services.agent_lifecycle._resolve_branch_name",
                new_callable=AsyncMock,
                return_value="fix/issue-55",
            ),
            patch(
                "sova.dashboard.services.agent_lifecycle._resolve_issue_worktree",
                new_callable=AsyncMock,
                return_value=pa.project_dir / ".claude" / "worktrees" / "55",
            ),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            result = await start_agent("", role="developer", pr_number=332)

        assert result["status"] == "started"
        mock_resolve.assert_awaited_once_with(332, pa.project_dir)
        call_args = mock_create.call_args
        assert call_args[0][0] == "55"

    async def test_start_agent_no_issue_resolve_when_issue_provided(self) -> None:
        """When issue is already provided, do not call _resolve_issue_from_pr."""
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

        worktree_path = pa.project_dir / ".claude" / "worktrees" / "42"

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(
                agent_lifecycle,
                "get_runtime",
                return_value=MagicMock(spawn=AsyncMock(return_value=mock_process)),
            ),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=11),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_transition_to_in_progress", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch(
                "sova.dashboard.services.agent_lifecycle._resolve_issue_from_pr",
                new_callable=AsyncMock,
                return_value="99",
            ) as mock_resolve,
            patch(
                "sova.dashboard.services.agent_lifecycle._resolve_branch_name",
                new_callable=AsyncMock,
                return_value="fix/issue-42",
            ),
            patch(
                "sova.dashboard.services.agent_lifecycle._resolve_issue_worktree",
                new_callable=AsyncMock,
                return_value=worktree_path,
            ),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            result = await start_agent("42", role="developer", pr_number=332)

        assert result["status"] == "started"
        mock_resolve.assert_not_awaited()

    async def test_start_agent_resolves_worktree_from_pr(self) -> None:
        """start_agent with pr_number should resolve worktree via branch lookup."""
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

        worktree_path = pa.project_dir / ".claude" / "worktrees" / "55"

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(
                agent_lifecycle,
                "get_runtime",
                return_value=MagicMock(spawn=AsyncMock(return_value=mock_process)),
            ) as mock_runtime_factory,
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=10),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_link_run_to_lifecycle", new_callable=AsyncMock),
            patch(
                "sova.dashboard.services.agent_lifecycle._resolve_issue_from_pr",
                new_callable=AsyncMock,
                return_value="55",
            ),
            patch(
                "sova.dashboard.services.agent_lifecycle._resolve_branch_name",
                new_callable=AsyncMock,
                return_value="fix/issue-55",
            ) as mock_branch,
            patch(
                "sova.dashboard.services.agent_lifecycle._resolve_issue_worktree",
                new_callable=AsyncMock,
                return_value=worktree_path,
            ) as mock_wt,
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            result = await start_agent("", role="developer", pr_number=332)

        assert result["status"] == "started"
        mock_branch.assert_awaited_once_with(332, pa.project_dir)
        mock_wt.assert_awaited_once_with("55", pa.project_dir, branch_name="fix/issue-55", pr_number=332)
        spawn_call = mock_runtime_factory.return_value.spawn
        actual_cwd = spawn_call.call_args[0][1]
        assert actual_cwd == worktree_path

    async def test_start_agent_recovers_pr_number_from_db_history(self) -> None:
        """When start_agent is called for a developer run without pr_number but a prior
        terminal run for the same issue has one, start_agent must recover that pr_number
        so the agent gets --pr and routes to the address-review pipeline instead of
        failing at AssessStep with 'Open PR already exists'."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        # Seed a terminal developer run with a known pr_number in the test DB
        async with await get_session() as session:
            async with session.begin():
                prior_run = TaskRun(
                    issue_number="344",
                    role="developer",
                    status="interrupted",
                    pr_number=372,
                )
                session.add(prior_run)

        pa = ProjectAgents()

        mock_process = MagicMock()
        mock_process.pid = 98765

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        spawned_prompt: list[str] = []

        async def _capture_spawn(prompt, cwd, env=None):
            spawned_prompt.append(prompt)
            return mock_process

        original = get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(
                agent_lifecycle,
                "get_runtime",
                return_value=MagicMock(spawn=_capture_spawn),
            ),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=99),
            patch.object(agent_lifecycle, "_update_task_run_pid", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(
                agent_lifecycle, "_resolve_branch_name", new_callable=AsyncMock, return_value="feat/issue-344"
            ),
            patch.object(
                agent_lifecycle, "_resolve_issue_worktree", new_callable=AsyncMock, return_value=Path("/tmp/wt")
            ),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_link_run_to_lifecycle", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_check_issue_budget", new_callable=AsyncMock, return_value=None),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
            patch("sova.db.session.get_session", side_effect=_ignore_project_dir),
        ):
            result = await start_agent("344", role="developer")  # no pr_number passed

        assert result.get("status") == "started", f"Expected started, got: {result}"
        assert spawned_prompt, "spawn was never called"
        prompt_text = spawned_prompt[0]
        assert "--pr 372" in prompt_text, f"Expected '--pr 372' in prompt, got: {prompt_text}"

    async def test_start_agent_does_not_recover_pr_number_for_reviewer(self) -> None:
        """PR number recovery from DB history should only apply to 'developer' role.
        Other roles are not affected."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import ProjectAgents, start_agent

        # Seed a terminal developer run with a pr_number
        async with await get_session() as session:
            async with session.begin():
                session.add(TaskRun(issue_number="344", role="developer", status="done", pr_number=372))

        pa = ProjectAgents()

        mock_process = MagicMock()
        mock_process.pid = 11111

        async def _empty_async_iter():
            return
            yield

        mock_process.stdout_lines = _empty_async_iter
        mock_process.stderr_lines = _empty_async_iter
        mock_process.wait = AsyncMock(return_value=0)

        spawned_prompt: list[str] = []

        async def _capture_spawn(prompt, cwd, env=None):
            spawned_prompt.append(prompt)
            return mock_process

        original = get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(
                agent_lifecycle,
                "get_runtime",
                return_value=MagicMock(spawn=_capture_spawn),
            ),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=88),
            patch.object(agent_lifecycle, "_update_task_run_pid", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_resolve_branch_name", new_callable=AsyncMock, return_value=""),
            patch.object(
                agent_lifecycle, "_resolve_issue_worktree", new_callable=AsyncMock, return_value=Path("/tmp/wt")
            ),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_link_run_to_lifecycle", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_check_issue_budget", new_callable=AsyncMock, return_value=None),
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
            patch("sova.db.session.get_session", side_effect=_ignore_project_dir),
        ):
            result = await start_agent("344", role="reviewer")  # reviewer role

        assert result.get("status") == "started"
        assert spawned_prompt
        # reviewer should NOT have --pr recovered from history
        assert "--pr" not in spawned_prompt[0], f"Reviewer should not get --pr: {spawned_prompt[0]}"

    async def test_recover_pr_number_returns_none_on_db_error(self) -> None:
        """_recover_last_pr_number must return None (not raise) when the DB query fails."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_lifecycle import _recover_last_pr_number

        with patch("sova.db.session.get_session", side_effect=RuntimeError("db boom")):
            result = await _recover_last_pr_number("42", Path("/tmp/proj"))

        assert result is None

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
# start_command worktree resolution
# ---------------------------------------------------------------------------


class TestStartCommandWorktreeResolution:
    """start_command() should use an existing worktree as cwd when available."""

    async def test_uses_worktree_when_exists(self, tmp_path: Path) -> None:
        """When a worktree exists for the issue, cwd should be the worktree path."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.agent_lifecycle import ProjectAgents, start_command

        pa = ProjectAgents()
        pa.project_dir = tmp_path

        worktree = tmp_path / ".claude" / "worktrees" / "42"
        worktree.mkdir(parents=True)

        mock_process = MagicMock()
        mock_process.pid = 9999
        mock_runtime = MagicMock()
        mock_runtime.spawn = AsyncMock(return_value=mock_process)

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "_check_issue_conflict", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "get_runtime", return_value=mock_runtime),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=100),
            patch.object(agent_lifecycle, "_link_run_to_lifecycle", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch("sova.dashboard.services.agent_output._read_output", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_output._read_stderr", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_resolve_command_prompt", return_value="prompt"),
        ):
            result = await start_command("address-pr", {"issue": "42", "pr": 10})

        assert result.get("status") == "started"
        spawn_cwd = mock_runtime.spawn.call_args[0][1]
        assert spawn_cwd == worktree, f"Expected worktree {worktree}, got {spawn_cwd}"

    async def test_rejects_pr_command_when_worktree_isolation_fails(self, tmp_path: Path) -> None:
        """PR-based commands should be rejected when worktree resolution falls back to project_dir."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.agent_lifecycle import ProjectAgents, start_command

        pa = ProjectAgents()
        pa.project_dir = tmp_path

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "_check_issue_conflict", new_callable=AsyncMock, return_value=None),
        ):
            result = await start_command("address-pr", {"issue": "42", "pr": 10})

        assert "error" in result
        assert "worktree isolation failed" in result["error"]

    async def test_falls_back_to_project_dir_when_no_pr(self, tmp_path: Path) -> None:
        """Non-PR commands should fall back to project_dir when no worktree exists."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.agent_lifecycle import ProjectAgents, start_command

        pa = ProjectAgents()
        pa.project_dir = tmp_path

        mock_process = MagicMock()
        mock_process.pid = 9999
        mock_runtime = MagicMock()
        mock_runtime.spawn = AsyncMock(return_value=mock_process)

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "_check_issue_conflict", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "get_runtime", return_value=mock_runtime),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=100),
            patch.object(agent_lifecycle, "_link_run_to_lifecycle", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch("sova.dashboard.services.agent_output._read_output", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_output._read_stderr", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_resolve_command_prompt", return_value="prompt"),
        ):
            result = await start_command("address-pr", {"issue": "42"})

        assert result.get("status") == "started"
        spawn_cwd = mock_runtime.spawn.call_args[0][1]
        assert spawn_cwd == tmp_path, f"Expected project dir {tmp_path}, got {spawn_cwd}"

    async def test_resolve_issue_worktree_returns_worktree_path(self, tmp_path: Path) -> None:
        """_resolve_issue_worktree returns worktree path when directory exists."""
        from sova.dashboard.services.agent_lifecycle import _resolve_issue_worktree

        worktree = tmp_path / ".claude" / "worktrees" / "42"
        worktree.mkdir(parents=True)
        assert await _resolve_issue_worktree("42", tmp_path) == worktree

    async def test_resolve_issue_worktree_returns_project_dir_when_missing(self, tmp_path: Path) -> None:
        """_resolve_issue_worktree returns project_dir when no worktree exists."""
        from sova.dashboard.services.agent_lifecycle import _resolve_issue_worktree

        assert await _resolve_issue_worktree("42", tmp_path) == tmp_path

    async def test_resolve_issue_worktree_handles_non_numeric_issue(self, tmp_path: Path) -> None:
        """_resolve_issue_worktree returns project_dir for non-numeric issue IDs."""
        from sova.dashboard.services.agent_lifecycle import _resolve_issue_worktree

        assert await _resolve_issue_worktree("standup", tmp_path) == tmp_path

    async def test_resolve_issue_worktree_strips_hash(self, tmp_path: Path) -> None:
        """_resolve_issue_worktree strips leading # from issue number."""
        from sova.dashboard.services.agent_lifecycle import _resolve_issue_worktree

        worktree = tmp_path / ".claude" / "worktrees" / "42"
        worktree.mkdir(parents=True)
        assert await _resolve_issue_worktree("#42", tmp_path) == worktree

    async def test_resolve_issue_worktree_branch_fallback(self, tmp_path: Path) -> None:
        """_resolve_issue_worktree falls back to branch-based lookup."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_context import _resolve_issue_worktree

        wt_path = tmp_path / "worktrees" / "feat-branch"
        wt_path.mkdir(parents=True)
        with patch(
            "sova.dashboard.services.agent_context.find_worktree_by_branch",
            new_callable=AsyncMock,
            return_value=wt_path,
        ):
            result = await _resolve_issue_worktree("standup", tmp_path, branch_name="feat/issue-99")
        assert result == wt_path

    async def test_resolve_issue_worktree_branch_fallback_filters_main(self, tmp_path: Path) -> None:
        """_resolve_issue_worktree skips branch worktree if it equals project_dir."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_context import _resolve_issue_worktree

        with patch(
            "sova.dashboard.services.agent_context.find_worktree_by_branch",
            new_callable=AsyncMock,
            return_value=tmp_path,
        ):
            result = await _resolve_issue_worktree("standup", tmp_path, branch_name="main")
        assert result == tmp_path

    async def test_resolve_issue_worktree_branch_fallback_error(self, tmp_path: Path) -> None:
        """_resolve_issue_worktree handles branch lookup errors gracefully."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_context import _resolve_issue_worktree

        with patch(
            "sova.dashboard.services.agent_context.find_worktree_by_branch",
            new_callable=AsyncMock,
            side_effect=RuntimeError("git error"),
        ):
            result = await _resolve_issue_worktree("standup", tmp_path, branch_name="broken")
        assert result == tmp_path

    async def test_resolve_issue_worktree_frees_branch_from_main(self, tmp_path: Path) -> None:
        """When branch is checked out in main repo, switch main to default branch and create worktree."""
        from dataclasses import dataclass
        from unittest.mock import AsyncMock, call, patch

        from sova.dashboard.services.agent_context import _resolve_issue_worktree

        @dataclass
        class FakeWorktreeInfo:
            path: Path
            branch: str
            issue_id: str

        wt_path = tmp_path / ".claude" / "worktrees" / "pr-99"
        fake_info = FakeWorktreeInfo(path=wt_path, branch="fix/stuff", issue_id="pr-99")
        mock_shell = AsyncMock()

        # First call (git checkout main) succeeds
        success_result = AsyncMock()
        success_result.success = True
        success_result.stdout = "origin/main\n"
        mock_shell.return_value = success_result

        with (
            patch(
                "sova.dashboard.services.agent_context.find_worktree_by_branch",
                new_callable=AsyncMock,
                return_value=tmp_path,  # branch found in main repo
            ),
            patch(
                "sova.dashboard.services.agent_context.run_shell",
                mock_shell,
            ),
            patch(
                "sova.git.worktree.create_worktree",
                new_callable=AsyncMock,
                return_value=fake_info,
            ),
        ):
            result = await _resolve_issue_worktree("", tmp_path, branch_name="fix/stuff", pr_number=99)

        assert result == wt_path
        assert mock_shell.await_args_list == [
            call(
                "git",
                "symbolic-ref",
                "refs/remotes/origin/HEAD",
                "--short",
                cwd=tmp_path,
                timeout=5,
            ),
            call("git", "checkout", "main", cwd=tmp_path, timeout=10),
        ]

    async def test_resolve_issue_worktree_returns_project_dir_on_switch_failure(self, tmp_path: Path) -> None:
        """When git checkout fails (e.g. uncommitted changes), return project_dir without attempting create_worktree."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_context import _resolve_issue_worktree

        mock_shell = AsyncMock()
        symbolic_ref_result = AsyncMock()
        symbolic_ref_result.success = True
        symbolic_ref_result.stdout = "origin/main\n"

        checkout_result = AsyncMock()
        checkout_result.success = False
        checkout_result.stderr = "error: Your local changes would be overwritten"

        mock_shell.side_effect = [symbolic_ref_result, checkout_result]

        mock_create = AsyncMock()
        with (
            patch(
                "sova.dashboard.services.agent_context.find_worktree_by_branch",
                new_callable=AsyncMock,
                return_value=tmp_path,
            ),
            patch("sova.dashboard.services.agent_context.run_shell", mock_shell),
            patch("sova.git.worktree.create_worktree", mock_create),
        ):
            result = await _resolve_issue_worktree("", tmp_path, branch_name="fix/stuff", pr_number=99)

        assert result == tmp_path
        mock_create.assert_not_awaited()

    async def test_non_issue_command_uses_project_dir(self, tmp_path: Path) -> None:
        """Commands without an issue number should always use project dir."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.agent_lifecycle import ProjectAgents, start_command

        pa = ProjectAgents()
        pa.project_dir = tmp_path

        mock_process = MagicMock()
        mock_process.pid = 9999
        mock_runtime = MagicMock()
        mock_runtime.spawn = AsyncMock(return_value=mock_process)

        with (
            patch.object(agent_lifecycle, "_get_project_agents", return_value=pa),
            patch.object(agent_lifecycle, "_check_issue_conflict", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "get_runtime", return_value=mock_runtime),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=100),
            patch.object(agent_lifecycle, "_link_run_to_lifecycle", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch("sova.dashboard.services.agent_output._read_output", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_output._read_stderr", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_resolve_command_prompt", return_value="prompt"),
        ):
            result = await start_command("standup", {})

        assert result.get("status") == "started"
        spawn_cwd = mock_runtime.spawn.call_args[0][1]
        assert spawn_cwd == tmp_path


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

    async def test_auto_handoff_invalid_pr_number_falls_back_to_none(self) -> None:
        """When args['pr'] is non-numeric, int() raises ValueError.
        _process_auto_handoff must catch it, log a warning, and continue with pr_num=None."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.control_service import AgentState, _process_auto_handoff
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        agent = AgentState(run_id=5, issue="55", role="developer", process=MagicMock())

        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue="55",
            pr_number=None,
            summary="findings",
            next_actions=[
                HandoffAction(
                    id="address_review",
                    label="Address",
                    mode="agent",
                    args={"issue": "55", "role": "developer", "pr": "pr-not-a-number"},
                    auto_execute=True,
                ),
            ],
        )

        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch.object(
                agent_lifecycle, "start_agent", new_callable=AsyncMock, return_value={"status": "started"}
            ) as mock_start,
            patch("sova.dashboard.services.handoff_service.clear_handoff"),
            patch(
                "sova.config.loader.load_config",
                return_value=MagicMock(pipeline=MagicMock(max_address_review_cycles=0)),
            ),
        ):
            await _process_auto_handoff(agent)

        mock_start.assert_awaited_once_with("55", role="developer", pr_number=None, slug=None)

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
            "source": "integrate-pr",
            "status": "awaiting_action",
            "summary": "Test handoff",
            "created_at": "2026-04-20T10:00:00Z",
            "next_actions": [
                {
                    "id": "integrate",
                    "label": "Integrate PR",
                    "style": "approve",
                    "mode": "claude-command",
                    "command": "integrate-pr",
                },
            ],
        }
        (control_dir / "handoff.json").write_text(json.dumps(handoff_data))

        resp = await client.get("/api/handoff")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_handoff"] is True
        assert data["handoff"]["source"] == "integrate-pr"
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

    async def test_agents_page_has_pr_summary_strip(self, client: AsyncClient) -> None:
        resp = await client.get("/agents")
        assert resp.status_code == 200
        assert 'id="pr-summary-strip"' in resp.text
        assert "renderPrSummary" in resp.text

    async def test_pr_summary_strip_js_constants_and_structure(self, client: AsyncClient) -> None:
        """Verify JS constants, metric labels, and stale threshold for renderPrSummary."""
        resp = await client.get("/agents")
        html = resp.text
        # Stale threshold: 7 days in seconds
        assert "_STALE_THRESHOLD_SECONDS = 7 * 86400" in html
        # Caching key mechanism
        assert "_lastPrSummaryKey" in html
        # All six metric labels present in the metrics array
        for label in ("Open", "Review", "Changes", "CI Fail", "Ready", "Stale"):
            assert f"label: '{label}'" in html
        # Avg Age fallback label when stale count is zero
        assert "'Avg Age'" in html
        # Error handling: try-catch wraps the function body
        assert "console.error('PR summary render failed:'" in html

    async def test_pr_summary_strip_state_counting_logic(self, client: AsyncClient) -> None:
        """Verify JS logic counts the correct computed_state values for each metric."""
        resp = await client.get("/agents")
        html = resp.text
        # awaiting_review state counted
        assert "st === 'awaiting_review'" in html
        # changes_requested state counted
        assert "st === 'changes_requested'" in html
        # Only approved_ci_green counted for Ready metric
        assert "st === 'approved_ci_green'" in html
        # CI fail detection via ci_status field
        assert "pr.ci_status === 'failed'" in html

    async def test_pr_summary_strip_visible_when_collapsed(self, client: AsyncClient) -> None:
        """Verify the summary strip stays visible when the tracker list is collapsed."""
        resp = await client.get("/agents")
        html = resp.text
        # Strip starts hidden (empty data, not collapsed state)
        assert 'id="pr-summary-strip" class="hidden"' in html
        # togglePrTracker only hides the list, not the strip
        assert "list.classList.toggle('hidden', _prTrackerCollapsed)" in html
        # Strip becomes visible via classList.remove('hidden') when data arrives
        assert "strip.classList.remove('hidden')" in html

    async def test_pr_summary_strip_dynamic_grid_cols(self, client: AsyncClient) -> None:
        """Verify grid-cols is computed from metrics.length, not hardcoded."""
        resp = await client.get("/agents")
        html = resp.text
        assert "grid-cols-' + metrics.length + '" in html

    async def test_pr_api_provides_fields_for_metrics(self, client: AsyncClient, monkeypatch) -> None:
        """Verify /api/prs/open returns all fields needed by renderPrSummary."""
        from unittest.mock import AsyncMock

        mock_prs = [
            {
                "number": 1,
                "title": "Test PR",
                "computed_state": "awaiting_review",
                "age_seconds": 700000,
                "ci_status": "failed",
            },
            {
                "number": 2,
                "title": "Old PR",
                "computed_state": "approved",
                "age_seconds": 100,
                "ci_status": "passed",
            },
        ]
        monkeypatch.setattr(
            "sova.dashboard.routers.prs.list_open_prs_with_state",
            AsyncMock(return_value=mock_prs),
        )
        resp = await client.get("/api/prs/open")
        assert resp.status_code == 200
        prs = resp.json()["prs"]
        assert len(prs) == 2
        # All fields required by renderPrSummary are present
        for pr in prs:
            assert "computed_state" in pr
            assert "age_seconds" in pr
            assert "ci_status" in pr

    async def test_pr_summary_ready_counts_only_approved_ci_green(self, client: AsyncClient) -> None:
        """Ready metric must count only approved_ci_green, not plain approved."""
        resp = await client.get("/agents")
        html = resp.text
        # The approved counter increments only for approved_ci_green
        assert "st === 'approved_ci_green') approved++" in html
        # Plain 'approved' without ci_green must NOT increment the counter
        assert "st === 'approved') approved++" not in html

    async def test_pr_summary_empty_array_hides_strip(self, client: AsyncClient) -> None:
        """When PR data is empty, the strip is hidden and cache key is cleared."""
        resp = await client.get("/agents")
        html = resp.text
        # Empty-array guard: hide strip and clear key
        assert "if (!prs.length)" in html
        assert "_lastPrSummaryKey = null" in html

    async def test_pr_summary_avg_age_fallback(self, client: AsyncClient) -> None:
        """When stale count is zero, the Stale cell shows avg age instead."""
        resp = await client.get("/agents")
        html = resp.text
        # Stale cell toggles between stale count and avg age
        assert "m.label === 'Stale' && m.value === 0 ? avgAge : String(m.value)" in html
        assert "m.label === 'Stale' && m.value === 0 ? 'Avg Age' : m.label" in html

    async def test_pr_summary_cache_key_includes_all_metrics(self, client: AsyncClient) -> None:
        """Cache key must include all metric values to detect any change."""
        resp = await client.get("/agents")
        html = resp.text
        # Key joins all six computed values plus ageHours
        assert "[total, awaitingReview, changesRequested, ciFailing, approved, stale, ageHours].join(',')" in html

    async def test_pr_summary_strip_not_hidden_on_collapse_in_toggle(self, client: AsyncClient) -> None:
        """togglePrTracker must not hide the summary strip."""
        resp = await client.get("/agents")
        html = resp.text
        # The toggle function should NOT contain strip visibility logic
        # (strip stays visible regardless of collapse state)
        assert "strip.classList.toggle('hidden'" not in html


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

    async def test_try_load_config_returns_none_on_broken_toml(self, tmp_path):
        """_try_load_config returns None when the project has invalid config."""
        from sova.dashboard.app import _try_load_config

        p = tmp_path / "broken-project"
        p.mkdir()
        (p / "sova.toml").write_text("INVALID TOML {{{{")

        result = _try_load_config(p)
        assert result is None

    async def test_try_load_config_returns_config_on_valid_project(self, tmp_path):
        """_try_load_config returns a ProjectConfig for valid projects."""
        from sova.dashboard.app import _try_load_config

        p = tmp_path / "valid-project"
        p.mkdir()
        (p / "sova.toml").write_text('[project]\ngithub_repo = "user/repo"\n')

        result = _try_load_config(p)
        assert result is not None
        assert result.github_repo == "user/repo"

    async def test_collect_supervisor_configs_skips_broken(self, tmp_path):
        """_collect_supervisor_configs skips projects with broken config."""
        from sova.dashboard.app import _collect_supervisor_configs

        broken = tmp_path / "broken"
        broken.mkdir()
        (broken / "sova.toml").write_text("INVALID {{{{")

        result = _collect_supervisor_configs({"broken": str(broken)})
        assert result == []

    async def test_collect_supervisor_configs_skips_nonexistent(self, tmp_path):
        """_collect_supervisor_configs skips directories that don't exist."""
        from sova.dashboard.app import _collect_supervisor_configs

        result = _collect_supervisor_configs({"gone": str(tmp_path / "nonexistent")})
        assert result == []

    async def test_collect_supervisor_configs_skips_disabled(self, tmp_path):
        """_collect_supervisor_configs skips projects with supervisor disabled."""
        from sova.dashboard.app import _collect_supervisor_configs

        p = tmp_path / "disabled-sv"
        p.mkdir()
        (p / "sova.toml").write_text('[project]\ngithub_repo = "u/r"\n\n[supervisor]\nenabled = false\n')

        result = _collect_supervisor_configs({"proj": str(p)})
        assert result == []

    async def test_collect_supervisor_configs_returns_enabled(self, tmp_path):
        """_collect_supervisor_configs returns projects with supervisor enabled."""
        from sova.dashboard.app import _collect_supervisor_configs

        p = tmp_path / "enabled-sv"
        p.mkdir()
        (p / "sova.toml").write_text('[project]\ngithub_repo = "u/r"\n\n[supervisor]\nenabled = true\n')

        result = _collect_supervisor_configs({"proj": str(p)})
        assert len(result) == 1
        assert result[0][0] == p
        assert result[0][1].supervisor.enabled is True

    async def test_project_supervisor_page_loads(self, multi_client: AsyncClient) -> None:
        """Supervisor page must not 500 in multi-project mode (github_repo context var)."""
        resp = await multi_client.get("/p/alpha/supervisor")
        assert resp.status_code == 200
        assert "Supervisor" in resp.text


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

    async def test_specs_page_renders(self, client: AsyncClient) -> None:
        resp = await client.get("/specs")
        assert resp.status_code == 200
        assert b"Specs" in resp.content

    async def test_specs_page_has_search_and_filter(self, client: AsyncClient) -> None:
        resp = await client.get("/specs")
        assert resp.status_code == 200
        assert b"spec-search" in resp.content
        assert b"spec-status-filter" in resp.content


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

        assert resp.status_code == 400

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

    async def test_work_items_returns_unified_payload(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        mock_result = {
            "items": [{"issue_number": "42", "state": "triaged"}],
            "running_count": 0,
            "slots_available": 3,
            "max_concurrent": 3,
        }
        with patch(
            "sova.dashboard.services.work_item_service.get_work_items",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            resp = await client.get("/api/agents/work-items")
            assert resp.status_code == 200
            data = resp.json()
            assert "items" in data
            assert data["items"][0]["issue_number"] == "42"
            assert data["running_count"] == 0
            assert data["max_concurrent"] == 3

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

    async def test_kanban_endpoint_returns_mode(self, client: AsyncClient) -> None:
        """The /api/agents/kanban response includes a 'mode' field."""
        resp = await client.get("/api/agents/kanban")
        assert resp.status_code == 200
        data = resp.json()
        assert "mode" in data
        assert data["mode"] in ("step_based", "role_based")
        assert "columns" in data

    async def test_kanban_includes_run_label(self, client: AsyncClient, session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)
        session.add(
            TaskRun(
                issue_number="200",
                role="developer",
                status="developing",
                current_step="develop",
                run_label="feat: add kanban labels",
                started_at=now - timedelta(minutes=2),
            )
        )
        await session.commit()

        resp = await client.get("/api/agents/kanban")
        assert resp.status_code == 200
        data = resp.json()
        all_runs = [r for col in data["columns"] for r in col.get("runs", [])]
        run_obj = next((r for r in all_runs if r["issue_number"] == "200"), None)
        assert run_obj is not None
        assert run_obj["run_label"] == "feat: add kanban labels"

    async def test_kanban_run_label_empty_for_issue_runs(self, client: AsyncClient, session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)
        session.add(
            TaskRun(
                issue_number="201",
                role="developer",
                status="developing",
                current_step="develop",
                started_at=now - timedelta(minutes=1),
            )
        )
        await session.commit()

        resp = await client.get("/api/agents/kanban")
        assert resp.status_code == 200
        all_runs = [r for col in resp.json()["columns"] for r in col.get("runs", [])]
        run_obj = next((r for r in all_runs if r["issue_number"] == "201"), None)
        assert run_obj is not None
        assert run_obj["run_label"] == ""

    async def test_kanban_includes_failed_runs(self, client: AsyncClient, session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)
        session.add(
            TaskRun(
                issue_number="300",
                role="developer",
                status="failed",
                error_message="CI failed",
                started_at=now - timedelta(hours=1),
                ended_at=now - timedelta(minutes=30),
            )
        )
        await session.commit()

        resp = await client.get("/api/agents/kanban")
        assert resp.status_code == 200
        data = resp.json()
        assert "failed_runs" in data
        assert len(data["failed_runs"]) == 1
        assert data["failed_runs"][0]["issue_number"] == "300"
        assert data["failed_runs"][0]["error_message"] == "CI failed"

    async def test_kanban_failed_runs_empty_when_none(self, client: AsyncClient) -> None:
        resp = await client.get("/api/agents/kanban")
        assert resp.status_code == 200
        data = resp.json()
        assert "failed_runs" in data
        assert data["failed_runs"] == []

    async def test_kanban_failed_runs_excludes_old(self, client: AsyncClient, session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)
        session.add(
            TaskRun(
                issue_number="301",
                role="developer",
                status="failed",
                started_at=now - timedelta(hours=48),
                ended_at=now - timedelta(hours=47),
            )
        )
        await session.commit()

        resp = await client.get("/api/agents/kanban")
        assert resp.status_code == 200
        assert resp.json()["failed_runs"] == []


class TestGetRecentFailedRunsDirect:
    """Direct unit tests for get_recent_failed_runs."""

    async def test_empty_db(self, session: AsyncSession) -> None:
        from sova.dashboard.services.work_service import get_recent_failed_runs

        result = await get_recent_failed_runs(session)
        assert result == []

    async def test_returns_recent_failures(self, session: AsyncSession) -> None:
        from sova.dashboard.services.work_service import get_recent_failed_runs

        now = datetime.now(timezone.utc)
        session.add(
            TaskRun(
                issue_number="50",
                role="developer",
                status="failed",
                run_label="fix: broken test",
                error_message="test timeout",
                pr_number=42,
                total_cost_usd=Decimal("1.23"),
                started_at=now - timedelta(hours=2),
                ended_at=now - timedelta(hours=1),
            )
        )
        await session.commit()

        result = await get_recent_failed_runs(session)
        assert len(result) == 1
        r = result[0]
        assert r["issue_number"] == "50"
        assert r["role"] == "developer"
        assert r["status"] == "failed"
        assert r["run_label"] == "fix: broken test"
        assert r["error_message"] == "test timeout"
        assert r["pr_number"] == 42
        assert r["duration_ms"] is not None

    async def test_excludes_non_failed(self, session: AsyncSession) -> None:
        from sova.dashboard.services.work_service import get_recent_failed_runs

        now = datetime.now(timezone.utc)
        session.add_all(
            [
                TaskRun(issue_number="60", role="developer", status="done", started_at=now, ended_at=now),
                TaskRun(issue_number="61", role="developer", status="running", started_at=now),
                TaskRun(issue_number="62", role="developer", status="interrupted", started_at=now, ended_at=now),
            ]
        )
        await session.commit()

        result = await get_recent_failed_runs(session)
        assert result == []

    async def test_excludes_old_failures(self, session: AsyncSession) -> None:
        from sova.dashboard.services.work_service import get_recent_failed_runs

        now = datetime.now(timezone.utc)
        session.add(
            TaskRun(
                issue_number="70",
                role="developer",
                status="failed",
                started_at=now - timedelta(hours=48),
                ended_at=now - timedelta(hours=47),
            )
        )
        await session.commit()

        result = await get_recent_failed_runs(session)
        assert result == []

    async def test_run_label_empty_default(self, session: AsyncSession) -> None:
        from sova.dashboard.services.work_service import get_recent_failed_runs

        now = datetime.now(timezone.utc)
        session.add(
            TaskRun(
                issue_number="80",
                role="developer",
                status="failed",
                started_at=now - timedelta(hours=1),
                ended_at=now,
            )
        )
        await session.commit()

        result = await get_recent_failed_runs(session)
        assert result[0]["run_label"] == ""

    async def test_respects_limit(self, session: AsyncSession) -> None:
        from sova.dashboard.services.work_service import get_recent_failed_runs

        now = datetime.now(timezone.utc)
        for i in range(5):
            session.add(
                TaskRun(
                    issue_number=str(90 + i),
                    role="developer",
                    status="failed",
                    started_at=now - timedelta(hours=1),
                    ended_at=now - timedelta(minutes=i),
                )
            )
        await session.commit()

        result = await get_recent_failed_runs(session, limit=3)
        assert len(result) == 3

    async def test_command_run_no_issue_number(self, session: AsyncSession) -> None:
        from sova.dashboard.services.work_service import get_recent_failed_runs

        now = datetime.now(timezone.utc)
        session.add(
            TaskRun(
                issue_number=None,
                role="command:review-pr",
                status="failed",
                run_label="review-pr #55",
                started_at=now - timedelta(hours=1),
                ended_at=now,
            )
        )
        await session.commit()

        result = await get_recent_failed_runs(session)
        assert len(result) == 1
        assert result[0]["issue_number"] is None
        assert result[0]["run_label"] == "review-pr #55"

    async def test_failed_run_with_null_ended_at(self, session: AsyncSession) -> None:
        """Crashed runs with NULL ended_at should still appear (falls back to started_at)."""
        from sova.dashboard.services.work_service import get_recent_failed_runs

        now = datetime.now(timezone.utc)
        session.add(
            TaskRun(
                issue_number="100",
                role="developer",
                status="failed",
                started_at=now - timedelta(hours=1),
                ended_at=None,
            )
        )
        await session.commit()

        result = await get_recent_failed_runs(session)
        assert len(result) == 1
        assert result[0]["issue_number"] == "100"
        assert result[0]["duration_ms"] is None
        assert result[0]["duration_formatted"] is None

    async def test_duration_formatted_populated(self, session: AsyncSession) -> None:
        """Verify duration_formatted is a human-readable string when both timestamps exist."""
        from sova.dashboard.services.work_service import get_recent_failed_runs

        now = datetime.now(timezone.utc)
        session.add(
            TaskRun(
                issue_number="101",
                role="developer",
                status="failed",
                started_at=now - timedelta(hours=2),
                ended_at=now - timedelta(hours=1),
            )
        )
        await session.commit()

        result = await get_recent_failed_runs(session)
        assert len(result) == 1
        assert result[0]["duration_ms"] is not None
        assert result[0]["duration_formatted"] is not None
        assert "h" in result[0]["duration_formatted"] or "m" in result[0]["duration_formatted"]

    async def test_failed_runs_boundary_24h(self, session: AsyncSession) -> None:
        """Runs exactly outside the 24h window are excluded; those inside are included."""
        from sova.dashboard.services.work_service import get_recent_failed_runs

        now = datetime.now(timezone.utc)
        session.add_all(
            [
                TaskRun(
                    issue_number="110",
                    role="developer",
                    status="failed",
                    started_at=now - timedelta(hours=25),
                    ended_at=now - timedelta(hours=24, seconds=1),
                ),
                TaskRun(
                    issue_number="111",
                    role="developer",
                    status="failed",
                    started_at=now - timedelta(hours=24),
                    ended_at=now - timedelta(hours=23, minutes=59),
                ),
            ]
        )
        await session.commit()

        result = await get_recent_failed_runs(session)
        issues = [r["issue_number"] for r in result]
        assert "111" in issues
        assert "110" not in issues


class TestCalculateDurationMs:
    """Unit tests for _calculate_duration_ms helper."""

    def test_both_none(self) -> None:
        from sova.dashboard.services.work_service import _calculate_duration_ms

        assert _calculate_duration_ms(None, None) is None

    def test_started_none(self) -> None:
        from sova.dashboard.services.work_service import _calculate_duration_ms

        assert _calculate_duration_ms(None, datetime.now(timezone.utc)) is None

    def test_ended_none(self) -> None:
        from sova.dashboard.services.work_service import _calculate_duration_ms

        assert _calculate_duration_ms(datetime.now(timezone.utc), None) is None

    def test_naive_datetimes(self) -> None:
        from sova.dashboard.services.work_service import _calculate_duration_ms

        start = datetime(2026, 1, 1, 12, 0, 0)
        end = datetime(2026, 1, 1, 12, 0, 10)
        assert _calculate_duration_ms(start, end) == 10_000

    def test_aware_datetimes(self) -> None:
        from sova.dashboard.services.work_service import _calculate_duration_ms

        start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 1, 1, 12, 5, 0, tzinfo=timezone.utc)
        assert _calculate_duration_ms(start, end) == 300_000


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

    async def test_role_based_empty(self, session: AsyncSession) -> None:
        from sova.dashboard.services.control_service import get_kanban_columns

        result = await get_kanban_columns(session, mode="role_based")
        assert result == []

    async def test_role_based_developer_runs(self, session: AsyncSession) -> None:
        from sova.dashboard.services.control_service import get_kanban_columns

        now = datetime.now(timezone.utc)
        session.add_all(
            [
                TaskRun(
                    issue_number="50",
                    role="developer",
                    status="running",
                    current_step="develop",
                    started_at=now,
                ),
                TaskRun(
                    issue_number="51",
                    role="developer",
                    status="running",
                    current_step="push",
                    started_at=now,
                ),
            ]
        )
        await session.commit()

        cols = await get_kanban_columns(session, mode="role_based")
        assert len(cols) == 1
        assert cols[0]["name"] == "developing"
        assert cols[0]["pipeline"] == "role_based"
        assert cols[0]["count"] == 2

    async def test_role_based_mixed_roles(self, session: AsyncSession) -> None:
        from sova.dashboard.services.control_service import get_kanban_columns

        now = datetime.now(timezone.utc)
        session.add_all(
            [
                TaskRun(
                    issue_number="60",
                    role="developer",
                    status="running",
                    current_step="develop",
                    started_at=now,
                ),
                TaskRun(
                    issue_number="61",
                    role="researcher",
                    status="running",
                    current_step="research",
                    started_at=now,
                ),
                TaskRun(
                    issue_number="62",
                    role="reviewer",
                    status="running",
                    current_step="review",
                    started_at=now,
                ),
            ]
        )
        await session.commit()

        cols = await get_kanban_columns(session, mode="role_based")
        col_names = {c["name"] for c in cols}
        assert col_names == {"developing", "researched", "in_review"}
        assert all(c["pipeline"] == "role_based" for c in cols)

    async def test_role_based_columns_sorted(self, session: AsyncSession) -> None:
        from sova.dashboard.services.control_service import get_kanban_columns

        now = datetime.now(timezone.utc)
        session.add_all(
            [
                TaskRun(
                    issue_number="70",
                    role="reviewer",
                    status="running",
                    current_step="review",
                    started_at=now,
                ),
                TaskRun(
                    issue_number="71",
                    role="developer",
                    status="running",
                    current_step="develop",
                    started_at=now,
                ),
            ]
        )
        await session.commit()

        cols = await get_kanban_columns(session, mode="role_based")
        positions = [c["position"] for c in cols]
        assert positions == sorted(positions)
        assert cols[0]["name"] == "developing"
        assert cols[1]["name"] == "in_review"

    async def test_role_based_per_column_limit(self, session: AsyncSession) -> None:
        from sova.dashboard.services.control_service import get_kanban_columns

        now = datetime.now(timezone.utc)
        session.add_all(
            [
                TaskRun(
                    issue_number=str(i),
                    role="developer",
                    status="running",
                    current_step="develop",
                    started_at=now,
                )
                for i in range(5)
            ]
        )
        await session.commit()

        cols = await get_kanban_columns(session, mode="role_based", per_column=2)
        assert len(cols) == 1
        assert cols[0]["count"] == 5
        assert len(cols[0]["runs"]) == 2

    async def test_role_based_command_run(self, session: AsyncSession) -> None:
        from sova.dashboard.services.control_service import get_kanban_columns

        now = datetime.now(timezone.utc)
        session.add(
            TaskRun(
                issue_number="80",
                role="command:review-pr",
                status="running",
                current_step="agent",
                started_at=now,
            )
        )
        await session.commit()

        cols = await get_kanban_columns(session, mode="role_based")
        assert len(cols) == 1
        assert cols[0]["name"] == "in_review"

    async def test_role_based_triage_run(self, session: AsyncSession) -> None:
        from sova.dashboard.services.control_service import get_kanban_columns

        now = datetime.now(timezone.utc)
        session.add(
            TaskRun(
                issue_number="85",
                role="triage",
                status="running",
                current_step="agent",
                started_at=now,
            )
        )
        await session.commit()

        cols = await get_kanban_columns(session, mode="role_based")
        assert len(cols) == 1
        assert cols[0]["name"] == "triaged"

    async def test_role_based_integration_commands(self, session: AsyncSession) -> None:
        from sova.dashboard.services.control_service import get_kanban_columns

        now = datetime.now(timezone.utc)
        session.add_all(
            [
                TaskRun(
                    issue_number="86",
                    role="command:integrate-pr",
                    status="running",
                    current_step="agent",
                    started_at=now,
                ),
                TaskRun(
                    issue_number="87",
                    role="command:approve-merge",
                    status="running",
                    current_step="agent",
                    started_at=now,
                ),
            ]
        )
        await session.commit()

        cols = await get_kanban_columns(session, mode="role_based")
        assert len(cols) == 1
        assert cols[0]["name"] == "in_review"
        assert cols[0]["count"] == 2

    async def test_step_based_mode_is_default(self, session: AsyncSession) -> None:
        from sova.dashboard.services.control_service import get_kanban_columns

        now = datetime.now(timezone.utc)
        session.add(
            TaskRun(
                issue_number="90",
                role="developer",
                status="running",
                current_step="develop",
                started_at=now,
            )
        )
        await session.commit()

        cols = await get_kanban_columns(session)
        assert len(cols) == 1
        assert cols[0]["name"] == "develop"
        assert cols[0]["pipeline"] == "developer"


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

    async def test_active_grouped_excludes_paused_runs(self, client: AsyncClient, session: AsyncSession) -> None:
        """Paused runs are terminal and should not appear in Active."""
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
        assert "99" not in issue_numbers

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

    async def test_work_history_failed_run_has_rerun_fields(self, client: AsyncClient, session: AsyncSession) -> None:
        """Failed runs in history include issue_number, role, and pr_number for re-run."""
        now = datetime.now(timezone.utc)
        run = TaskRun(
            issue_number="90",
            role="researcher",
            status="failed",
            current_step="spec",
            pr_number=None,
            total_cost_usd=Decimal("0.50"),
            error_message="Expected .claude/specs/{issue}-*.md",
            started_at=now - timedelta(minutes=10),
            ended_at=now,
        )
        session.add(run)
        await session.commit()

        resp = await client.get("/api/work/history?status=failed")
        assert resp.status_code == 200
        data = resp.json()
        failed_task = next(t for t in data["tasks"] if t["issue_number"] == "90")
        assert failed_task["status"] == "failed"
        assert failed_task["role"] == "researcher"
        assert failed_task["error_message"] == "Expected .claude/specs/{issue}-*.md"
        assert "issue_number" in failed_task
        assert "pr_number" in failed_task

    async def test_work_history_interrupted_run_has_rerun_fields(
        self, client: AsyncClient, session: AsyncSession
    ) -> None:
        """Interrupted runs in history include fields needed for re-run."""
        now = datetime.now(timezone.utc)
        run = TaskRun(
            issue_number="91",
            role="developer",
            status="interrupted",
            current_step="develop",
            pr_number=42,
            total_cost_usd=Decimal("1.20"),
            error_message="Agent process died unexpectedly",
            started_at=now - timedelta(minutes=15),
            ended_at=now,
        )
        session.add(run)
        await session.commit()

        resp = await client.get("/api/work/history")
        assert resp.status_code == 200
        data = resp.json()
        interrupted_task = next(t for t in data["tasks"] if t["issue_number"] == "91")
        assert interrupted_task["status"] == "interrupted"
        assert interrupted_task["role"] == "developer"
        assert interrupted_task["pr_number"] == 42
        assert interrupted_task["error_message"] == "Agent process died unexpectedly"


class TestStepPassedStatus:
    """Steps with legacy 'passed' status are counted as completed."""

    async def test_work_detail_includes_passed_steps(self, client: AsyncClient, session: AsyncSession) -> None:
        """Run detail API returns steps with 'passed' status alongside 'done'."""
        now = datetime.now(timezone.utc)
        run = TaskRun(
            issue_number="200",
            role="developer",
            status="done",
            current_step="handoff_to_reviewer",
            total_cost_usd=Decimal("1.00"),
            project_slug="myproject",
            started_at=now - timedelta(hours=1),
            ended_at=now,
        )
        session.add(run)
        await session.flush()

        session.add_all(
            [
                StepExecution(
                    task_run_id=run.id,
                    step_name="sync",
                    status="passed",
                    cost_usd=Decimal("0.00"),
                    duration_ms=3000,
                    started_at=now - timedelta(hours=1),
                    ended_at=now - timedelta(minutes=59, seconds=57),
                ),
                StepExecution(
                    task_run_id=run.id,
                    step_name="develop",
                    status="done",
                    cost_usd=Decimal("0.80"),
                    duration_ms=60000,
                    started_at=now - timedelta(minutes=59, seconds=57),
                    ended_at=now - timedelta(minutes=58, seconds=57),
                ),
            ]
        )
        await session.commit()

        resp = await client.get(f"/api/work/{run.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["steps"]) == 2
        statuses = {s["step_name"]: s["status"] for s in data["steps"]}
        assert statuses["sync"] == "passed"
        assert statuses["develop"] == "done"

    async def test_work_history_counts_passed_as_completed(self, client: AsyncClient, session: AsyncSession) -> None:
        """History endpoint counts both 'passed' and 'done' steps as completed."""
        now = datetime.now(timezone.utc)
        run = TaskRun(
            issue_number="201",
            role="developer",
            status="done",
            current_step="handoff_to_reviewer",
            total_cost_usd=Decimal("0.50"),
            project_slug="myproject",
            started_at=now - timedelta(hours=1),
            ended_at=now,
        )
        session.add(run)
        await session.flush()

        session.add_all(
            [
                StepExecution(
                    task_run_id=run.id,
                    step_name="sync",
                    status="passed",
                    duration_ms=3000,
                    started_at=now - timedelta(hours=1),
                    ended_at=now - timedelta(minutes=59, seconds=57),
                ),
                StepExecution(
                    task_run_id=run.id,
                    step_name="assess",
                    status="passed",
                    duration_ms=2000,
                    started_at=now - timedelta(minutes=59, seconds=57),
                    ended_at=now - timedelta(minutes=59, seconds=55),
                ),
                StepExecution(
                    task_run_id=run.id,
                    step_name="develop",
                    status="done",
                    duration_ms=60000,
                    started_at=now - timedelta(minutes=59, seconds=55),
                    ended_at=now - timedelta(minutes=58, seconds=55),
                ),
            ]
        )
        await session.commit()

        resp = await client.get("/api/work/history")
        assert resp.status_code == 200
        data = resp.json()
        task = next(t for t in data["tasks"] if t["issue_number"] == "201")
        assert task["steps_completed"] == 3


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

    async def test_create_milestones_returns_404_for_missing_dir(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/setup/milestones/create",
            json={"project_path": "/nonexistent/path/that/does/not/exist"},
        )
        assert resp.status_code == 404
        assert "Directory not found" in resp.json()["detail"]


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
        assert tasks_by_issue["51"]["total_steps_possible"] == 16

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

    async def test_finalize_applies_file_handoff_when_already_terminal(self) -> None:
        """_finalize_task_run must persist file handoff even when TaskRun is already terminal.

        The WorkflowEngine subprocess may finalize status before the dashboard's
        _finalize_task_run runs, causing an early-return. handoff_json must still
        be set so get_sova_review_verdict() finds the real verdict.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_db import _finalize_task_run
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="93",
                    role="reviewer",
                    status="done",
                    pr_number=77,
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        findings = [{"file": "a.py", "line": 1, "severity": 9, "category": "bug", "description": "X", "suggestion": ""}]
        file_handoff_data = {
            "issue": "93",
            "pr_number": 77,
            "details": {"next_action": "address_review", "pending_findings": findings, "cost_usd": "0.05"},
            "source": "reviewer",
        }

        mock_agent = MagicMock(spec=AgentState)
        mock_agent.last_result_cost = 0.0
        mock_agent.project_dir = None
        mock_agent.issue = "93"
        mock_agent.role = "reviewer"

        original = get_session

        async def _ignore_project_dir(**_kw):
            return await original()

        with (
            patch("sova.dashboard.services.agent_db._read_file_handoff", return_value=file_handoff_data),
            patch("sova.db.session.get_session", side_effect=_ignore_project_dir),
            patch("sova.supervisor.pr_throttle.dequeue", AsyncMock()),
        ):
            await _finalize_task_run(run_id, exit_code=0, agent=mock_agent)

        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed.status == "done"
                assert refreshed.handoff_json is not None, "handoff_json must be set even on terminal early-return"
                assert refreshed.handoff_json.get("next_action") == "address_review"
                findings_out = refreshed.handoff_json.get("pending_findings", [])
                assert len(findings_out) == 1
                assert findings_out[0]["severity"] == 9


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
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_link_run_to_lifecycle", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "check_memory_pressure", return_value=(None, None)),
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
        assert result["total_cost_usd"] == "55.000000"

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
# Memory pressure gate
# ---------------------------------------------------------------------------


class TestMemoryPressureGate:
    """check_memory_pressure blocks, warns, or clears based on available memory."""

    def test_blocks_when_below_block_threshold(self) -> None:
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch

        from sova.dashboard.services.agent_validation import check_memory_pressure

        guard = SimpleNamespace(enabled=True, warn_threshold_gb=4.0, block_threshold_gb=1.0)
        cfg = SimpleNamespace(memory_guard=guard)
        mem = SimpleNamespace(available=int(0.5 * 1024**3))

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("psutil.virtual_memory", return_value=mem),
        ):
            block, warn = check_memory_pressure(Path("/tmp"))

        assert block is not None
        assert "error" in block
        assert "Insufficient" in block["error"]
        assert warn is None

    def test_warns_when_below_warn_threshold(self) -> None:
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch

        from sova.dashboard.services.agent_validation import check_memory_pressure

        guard = SimpleNamespace(enabled=True, warn_threshold_gb=4.0, block_threshold_gb=1.0)
        cfg = SimpleNamespace(memory_guard=guard)
        mem = SimpleNamespace(available=int(2.5 * 1024**3))

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("psutil.virtual_memory", return_value=mem),
        ):
            block, warn = check_memory_pressure(Path("/tmp"))

        assert block is None
        assert warn is not None
        assert "Low memory" in warn

    def test_clears_when_sufficient_memory(self) -> None:
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch

        from sova.dashboard.services.agent_validation import check_memory_pressure

        guard = SimpleNamespace(enabled=True, warn_threshold_gb=4.0, block_threshold_gb=1.0)
        cfg = SimpleNamespace(memory_guard=guard)
        mem = SimpleNamespace(available=int(8.0 * 1024**3))

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("psutil.virtual_memory", return_value=mem),
        ):
            block, warn = check_memory_pressure(Path("/tmp"))

        assert block is None
        assert warn is None

    def test_disabled_skips_check(self) -> None:
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch

        from sova.dashboard.services.agent_validation import check_memory_pressure

        guard = SimpleNamespace(enabled=False, warn_threshold_gb=4.0, block_threshold_gb=1.0)
        cfg = SimpleNamespace(memory_guard=guard)

        with patch("sova.config.loader.load_config", return_value=cfg):
            block, warn = check_memory_pressure(Path("/tmp"))

        assert block is None
        assert warn is None

    def test_psutil_failure_is_fail_open(self) -> None:
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch

        from sova.dashboard.services.agent_validation import check_memory_pressure

        guard = SimpleNamespace(enabled=True, warn_threshold_gb=4.0, block_threshold_gb=1.0)
        cfg = SimpleNamespace(memory_guard=guard)

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("psutil.virtual_memory", side_effect=OSError("no psutil")),
        ):
            block, warn = check_memory_pressure(Path("/tmp"))

        assert block is None
        assert warn is None

    def test_config_load_failure_is_fail_open(self) -> None:
        from pathlib import Path
        from unittest.mock import patch

        from sova.dashboard.services.agent_validation import check_memory_pressure

        with patch(
            "sova.config.loader.load_config",
            side_effect=RuntimeError("bad config"),
        ):
            block, warn = check_memory_pressure(Path("/tmp"))

        assert block is None
        assert warn is None

    def test_block_includes_available_gb(self) -> None:
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch

        from sova.dashboard.services.agent_validation import check_memory_pressure

        guard = SimpleNamespace(enabled=True, warn_threshold_gb=4.0, block_threshold_gb=1.0)
        cfg = SimpleNamespace(memory_guard=guard)
        mem = SimpleNamespace(available=int(0.5 * 1024**3))

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("psutil.virtual_memory", return_value=mem),
        ):
            block, _ = check_memory_pressure(Path("/tmp"))

        assert block is not None
        assert block["available_gb"] == 0.5
        assert block["block_threshold_gb"] == 1.0

    async def test_start_agent_blocked_by_memory_pressure(self) -> None:
        """start_agent returns error when memory is below block threshold."""
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services import agent_lifecycle

        mem_error = {"error": "Insufficient memory: 0.5 GB available"}

        with (
            patch.object(agent_lifecycle, "_get_project_agents") as mock_gpa,
            patch.object(agent_lifecycle, "check_memory_pressure", return_value=(mem_error, None)),
        ):
            from sova.dashboard.services.agent_pool import ProjectAgents

            pa = ProjectAgents()
            pa.project_dir = MagicMock()
            mock_gpa.return_value = pa

            result = await agent_lifecycle.start_agent("42")

        assert "error" in result
        assert "Insufficient memory" in result["error"]

    async def test_start_agent_force_bypasses_memory_gate(self) -> None:
        """start_agent with force=True skips the memory check."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle

        mock_process = MagicMock()
        mock_process.pid = 99

        with (
            patch.object(agent_lifecycle, "_get_project_agents") as mock_gpa,
            patch.object(
                agent_lifecycle,
                "get_runtime",
                return_value=MagicMock(spawn=AsyncMock(return_value=mock_process)),
            ),
            patch.object(agent_lifecycle, "_create_task_run", new_callable=AsyncMock, return_value=1),
            patch.object(agent_lifecycle, "_resolve_project_gh_env", new_callable=AsyncMock, return_value=None),
            patch.object(agent_lifecycle, "_wait_and_finalize", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_link_run_to_lifecycle", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "check_memory_pressure") as mock_mem,
            patch("sova.dashboard.services.agent_lifecycle.OutputWriter"),
        ):
            from sova.dashboard.services.agent_pool import ProjectAgents

            pa = ProjectAgents()
            pa.project_dir = MagicMock()
            mock_gpa.return_value = pa

            result = await agent_lifecycle.start_agent("42", force=True)

        assert "error" not in result
        mock_mem.assert_not_called()

    async def test_start_command_blocked_by_memory_pressure(self) -> None:
        """start_command returns error when memory is below block threshold."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle

        mem_error = {"error": "Insufficient memory: 0.5 GB available"}

        with (
            patch.object(agent_lifecycle, "_get_project_agents") as mock_gpa,
            patch.object(agent_lifecycle, "check_memory_pressure", return_value=(mem_error, None)),
            patch.object(
                agent_lifecycle, "_resolve_command_context", new_callable=AsyncMock, return_value=(None, "42")
            ),
        ):
            from sova.dashboard.services.agent_pool import ProjectAgents

            pa = ProjectAgents()
            pa.project_dir = MagicMock()
            mock_gpa.return_value = pa

            result = await agent_lifecycle.start_command("integrate-pr", args={"issue": "42"})

        assert "error" in result
        assert "Insufficient memory" in result["error"]

    def test_system_metrics_pressure_level_critical(self) -> None:
        """get_system_metrics includes memory_pressure='critical' when below block threshold."""
        from types import SimpleNamespace
        from unittest.mock import patch

        from sova.dashboard.services import resource_service

        guard = SimpleNamespace(enabled=True, warn_threshold_gb=4.0, block_threshold_gb=1.0)
        mem = SimpleNamespace(
            total=16 * 1024**3,
            available=int(0.5 * 1024**3),
            percent=97.0,
        )

        with (
            patch("psutil.cpu_percent", return_value=50.0),
            patch("psutil.virtual_memory", return_value=mem),
            patch("psutil.cpu_count", return_value=8),
            patch.object(resource_service, "_get_memory_guard_config", return_value=guard),
        ):
            result = resource_service.get_system_metrics()

        assert result["system"]["memory_pressure"] == "critical"
        assert result["system"]["memory_available_gb"] == 0.5

    def test_system_metrics_pressure_level_warning(self) -> None:
        """get_system_metrics includes memory_pressure='warning' when below warn threshold."""
        from types import SimpleNamespace
        from unittest.mock import patch

        from sova.dashboard.services import resource_service

        guard = SimpleNamespace(enabled=True, warn_threshold_gb=4.0, block_threshold_gb=1.0)
        mem = SimpleNamespace(
            total=16 * 1024**3,
            available=int(2.5 * 1024**3),
            percent=85.0,
        )

        with (
            patch("psutil.cpu_percent", return_value=50.0),
            patch("psutil.virtual_memory", return_value=mem),
            patch("psutil.cpu_count", return_value=8),
            patch.object(resource_service, "_get_memory_guard_config", return_value=guard),
        ):
            result = resource_service.get_system_metrics()

        assert result["system"]["memory_pressure"] == "warning"

    def test_system_metrics_pressure_level_ok(self) -> None:
        """get_system_metrics includes memory_pressure='ok' when above all thresholds."""
        from types import SimpleNamespace
        from unittest.mock import patch

        from sova.dashboard.services import resource_service

        guard = SimpleNamespace(enabled=True, warn_threshold_gb=4.0, block_threshold_gb=1.0)
        mem = SimpleNamespace(
            total=16 * 1024**3,
            available=int(8.0 * 1024**3),
            percent=50.0,
        )

        with (
            patch("psutil.cpu_percent", return_value=50.0),
            patch("psutil.virtual_memory", return_value=mem),
            patch("psutil.cpu_count", return_value=8),
            patch.object(resource_service, "_get_memory_guard_config", return_value=guard),
        ):
            result = resource_service.get_system_metrics()

        assert result["system"]["memory_pressure"] == "ok"


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

    async def test_sova_review_verdict_null_handoff_reviewer_role(self) -> None:
        """reviewer role with null handoff_json (pipeline bypass) returns has_sova_review=True."""
        from sova.dashboard.services.agent_recovery import get_sova_review_verdict

        session = await get_session()
        async with session.begin():
            session.add(
                TaskRun(
                    issue_number="107",
                    role="reviewer",
                    status="done",
                    handoff_json=None,
                    ended_at=datetime.now(timezone.utc),
                )
            )

        result = await get_sova_review_verdict("107")
        assert result["has_sova_review"] is True
        assert result["verdict"] == "revise"
        assert result["finding_count"] == 0
        assert result["reviewed_at"] is not None

    async def test_sova_review_verdict_address_pr_after_review_resets_to_approve(self) -> None:
        """When command:address-pr completed after the reviewer, verdict resets to approve."""
        from sova.dashboard.services.agent_recovery import get_sova_review_verdict

        session = await get_session()
        review_ts = datetime.now(timezone.utc)
        addr_ts = review_ts + timedelta(seconds=5)
        async with session.begin():
            session.add(
                TaskRun(
                    issue_number="108",
                    role="reviewer",
                    status="done",
                    handoff_json=None,
                    pr_number=900,
                    ended_at=review_ts,
                )
            )
            session.add(
                TaskRun(
                    issue_number="108",
                    role="command:address-pr",
                    status="done",
                    pr_number=900,
                    ended_at=addr_ts,
                )
            )

        result = await get_sova_review_verdict("108", pr_number=900)
        assert result["has_sova_review"] is True
        assert result["verdict"] == "approve"
        assert result["finding_count"] == 0

    async def test_sova_review_verdict_older_address_pr_does_not_reset(self) -> None:
        """An address-pr run older than the reviewer run does not reset the verdict."""
        from sova.dashboard.services.agent_recovery import get_sova_review_verdict

        session = await get_session()
        addr_ts = datetime.now(timezone.utc)
        review_ts = addr_ts + timedelta(seconds=5)
        async with session.begin():
            session.add(
                TaskRun(
                    issue_number="109",
                    role="reviewer",
                    status="done",
                    handoff_json=None,
                    pr_number=901,
                    ended_at=review_ts,
                )
            )
            session.add(
                TaskRun(
                    issue_number="109",
                    role="command:address-pr",
                    status="done",
                    pr_number=901,
                    ended_at=addr_ts,
                )
            )

        result = await get_sova_review_verdict("109", pr_number=901)
        assert result["has_sova_review"] is True
        assert result["verdict"] == "revise"

    async def test_sova_review_verdict_authoritative_handoff_superseded_by_newer_address_pr(self) -> None:
        """Newer address-pr resets verdict even when reviewer has authoritative handoff_json."""
        from sova.dashboard.services.agent_recovery import get_sova_review_verdict

        session = await get_session()
        review_ts = datetime.now(timezone.utc)
        addr_ts = review_ts + timedelta(seconds=5)
        async with session.begin():
            session.add(
                TaskRun(
                    issue_number="110",
                    role="reviewer",
                    status="done",
                    handoff_json={
                        "next_action": "address_review",
                        "pending_findings": [{"file": "z.py", "severity": 9, "description": "critical"}],
                    },
                    pr_number=902,
                    ended_at=review_ts,
                )
            )
            session.add(
                TaskRun(
                    issue_number="110",
                    role="command:address-pr",
                    status="done",
                    pr_number=902,
                    ended_at=addr_ts,
                )
            )

        result = await get_sova_review_verdict("110", pr_number=902)
        assert result["has_sova_review"] is True
        assert result["verdict"] == "approve"
        assert result["finding_count"] == 0

    async def test_sova_review_verdict_failed_address_pr_does_not_supersede(self) -> None:
        """A failed address-pr run must not supersede the reviewer verdict."""
        from sova.dashboard.services.agent_recovery import get_sova_review_verdict

        session = await get_session()
        review_ts = datetime.now(timezone.utc)
        addr_ts = review_ts + timedelta(seconds=5)
        async with session.begin():
            session.add(
                TaskRun(
                    issue_number="111",
                    role="reviewer",
                    status="done",
                    handoff_json={
                        "next_action": "address_review",
                        "pending_findings": [{"file": "a.py", "severity": 7, "description": "bug"}],
                    },
                    pr_number=903,
                    ended_at=review_ts,
                )
            )
            # Failed address-pr with newer timestamp must NOT reset verdict to approve.
            session.add(
                TaskRun(
                    issue_number="111",
                    role="command:address-pr",
                    status="failed",
                    pr_number=903,
                    ended_at=addr_ts,
                )
            )

        result = await get_sova_review_verdict("111", pr_number=903)
        assert result["has_sova_review"] is True
        # severity=7 maps to "block"; the failed address-pr must not clear this.
        assert result["verdict"] == "block"

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

    async def test_recover_stale_runs_handoff_check_exception(self) -> None:
        """recover_stale_runs catches exceptions from handoff check gracefully."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_recovery import recover_stale_runs

        session = await get_session()
        async with session.begin():
            run = TaskRun(
                issue_number="203",
                role="developer",
                status="running",
                pid=999999,
                started_at=datetime.now(timezone.utc),
            )
            session.add(run)
            await session.flush()
            run_id = run.id

        with patch(
            "sova.dashboard.services.handoff_service.get_handoff",
            side_effect=RuntimeError("disk error"),
        ):
            interrupted = await recover_stale_runs()

        assert len(interrupted) == 1

        session2 = await get_session()
        async with session2.begin():
            updated = await session2.get(TaskRun, run_id)
            assert updated.status == "interrupted"

    async def test_recover_stale_runs_merge_check_exception(self) -> None:
        """recover_stale_runs catches exceptions from merge check gracefully."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_recovery import recover_stale_runs

        session = await get_session()
        async with session.begin():
            run = TaskRun(
                issue_number="204",
                role="integrate-pr",
                status="running",
                pid=999999,
                pr_number=50,
                started_at=datetime.now(timezone.utc),
            )
            session.add(run)
            await session.flush()
            run_id = run.id

        with (
            patch(
                "sova.dashboard.services.handoff_service.get_handoff",
                return_value=None,
            ),
            patch(
                "sova.dashboard.services.agent_lifecycle._check_pr_merged_on_failure",
                side_effect=RuntimeError("gh failed"),
            ),
        ):
            interrupted = await recover_stale_runs()

        assert len(interrupted) == 1
        session2 = await get_session()
        async with session2.begin():
            updated = await session2.get(TaskRun, run_id)
            assert updated.status == "interrupted"

    async def test_recover_stale_runs_outer_exception(self) -> None:
        """recover_stale_runs returns empty list on outer exception."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_recovery import recover_stale_runs

        with patch(
            "sova.db.session.get_session",
            side_effect=RuntimeError("db init failed"),
        ):
            result = await recover_stale_runs()
        assert result == []

    async def test_get_interrupted_runs_exception(self) -> None:
        """get_interrupted_runs returns empty list on DB error."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_recovery import get_interrupted_runs

        with patch(
            "sova.db.session.get_session",
            side_effect=RuntimeError("db error"),
        ):
            result = await get_interrupted_runs()
        assert result == []

    async def test_dismiss_interrupted_runs_exception(self) -> None:
        """dismiss_interrupted_runs returns 0 on DB error."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_recovery import dismiss_interrupted_runs

        with patch(
            "sova.db.session.get_session",
            side_effect=RuntimeError("db error"),
        ):
            result = await dismiss_interrupted_runs()
        assert result == 0

    async def test_sova_review_verdict_approve_no_findings_no_approve_action(self) -> None:
        """Verdict defaults to approve when no findings and next_action is not 'approve'."""
        from sova.dashboard.services.agent_recovery import get_sova_review_verdict

        session = await get_session()
        async with session.begin():
            session.add(
                TaskRun(
                    issue_number="207",
                    role="reviewer",
                    status="done",
                    handoff_json={
                        "next_action": "some_other_action",
                        "pending_findings": [],
                    },
                    ended_at=datetime.now(timezone.utc),
                )
            )

        result = await get_sova_review_verdict("207")
        assert result["has_sova_review"] is True
        assert result["verdict"] == "approve"
        assert result["finding_count"] == 0

    async def test_sova_review_verdict_exception(self) -> None:
        """get_sova_review_verdict returns no-review on DB error."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_recovery import get_sova_review_verdict

        with patch(
            "sova.db.session.get_session",
            side_effect=RuntimeError("db error"),
        ):
            result = await get_sova_review_verdict("999")
        assert result["has_sova_review"] is False
        assert result["verdict"] is None


# ---------------------------------------------------------------------------
# Verdict parsing from output lines
# ---------------------------------------------------------------------------


class TestParseVerdictFromOutput:
    """_parse_verdict_from_output should extract verdicts from agent text output."""

    def test_approve_verdict(self) -> None:
        from sova.dashboard.services.agent_recovery import _parse_verdict_from_output

        lines = ["Some analysis...", "### Verdict", "**Approve** -- no blockers"]
        assert _parse_verdict_from_output(lines) == "approve"

    def test_request_changes_verdict(self) -> None:
        from sova.dashboard.services.agent_recovery import _parse_verdict_from_output

        lines = ["Analysis...", "**Verdict: Request changes** -- HIGH finding must be fixed"]
        assert _parse_verdict_from_output(lines) == "revise"

    def test_comment_only_verdict(self) -> None:
        from sova.dashboard.services.agent_recovery import _parse_verdict_from_output

        lines = ["Summary...", "Verdict: Comment only -- observations"]
        assert _parse_verdict_from_output(lines) == "revise"

    def test_no_verdict_found(self) -> None:
        from sova.dashboard.services.agent_recovery import _parse_verdict_from_output

        lines = ["Just some analysis", "No verdict keyword here"]
        assert _parse_verdict_from_output(lines) is None

    def test_markdown_bold_verdict(self) -> None:
        from sova.dashboard.services.agent_recovery import _parse_verdict_from_output

        lines = ["### **Verdict**", "**Approve**"]
        assert _parse_verdict_from_output(lines) == "approve"

    def test_verdict_with_parenthetical(self) -> None:
        from sova.dashboard.services.agent_recovery import _parse_verdict_from_output

        lines = [
            "**Verdict: Request changes** (posted as COMMENT since GitHub "
            "does not allow self-reviews with formal approval state.)"
        ]
        assert _parse_verdict_from_output(lines) == "revise"

    def test_empty_lines(self) -> None:
        from sova.dashboard.services.agent_recovery import _parse_verdict_from_output

        assert _parse_verdict_from_output([]) is None


class TestSovaReviewVerdictOutputFallback:
    """get_sova_review_verdict falls back to parsing output_lines for command:review-pr."""

    async def test_fallback_parses_approve_from_output(self) -> None:
        from sova.dashboard.services.agent_recovery import get_sova_review_verdict
        from sova.db.models import OutputLine

        session = await get_session()
        async with session.begin():
            run = TaskRun(
                issue_number="300",
                role="command:review-pr",
                status="done",
                handoff_json=None,
                ended_at=datetime.now(timezone.utc),
            )
            session.add(run)
            await session.flush()
            run_id = run.id

            session.add(OutputLine(task_run_id=run_id, line_number=1, text="Review analysis..."))
            session.add(OutputLine(task_run_id=run_id, line_number=2, text="**Verdict: Approve** -- looks good"))

        result = await get_sova_review_verdict("300")
        assert result["has_sova_review"] is True
        assert result["verdict"] == "approve"

    async def test_fallback_parses_request_changes_from_output(self) -> None:
        from sova.dashboard.services.agent_recovery import get_sova_review_verdict
        from sova.db.models import OutputLine

        session = await get_session()
        async with session.begin():
            run = TaskRun(
                issue_number="301",
                role="command:review-pr",
                status="done",
                handoff_json=None,
                ended_at=datetime.now(timezone.utc),
            )
            session.add(run)
            await session.flush()
            run_id = run.id

            session.add(OutputLine(task_run_id=run_id, line_number=1, text="Finding: [HIGH] bug"))
            session.add(OutputLine(task_run_id=run_id, line_number=2, text="Verdict: Request changes -- fix the bug"))

        result = await get_sova_review_verdict("301")
        assert result["has_sova_review"] is True
        assert result["verdict"] == "revise"

    async def test_fallback_defaults_to_revise_when_no_verdict(self) -> None:
        from sova.dashboard.services.agent_recovery import get_sova_review_verdict

        session = await get_session()
        async with session.begin():
            run = TaskRun(
                issue_number="302",
                role="command:review-pr",
                status="done",
                handoff_json=None,
                ended_at=datetime.now(timezone.utc),
            )
            session.add(run)
            await session.flush()

        result = await get_sova_review_verdict("302")
        assert result["has_sova_review"] is True
        assert result["verdict"] == "revise"

    async def test_handoff_json_takes_precedence_over_output(self) -> None:
        """When handoff_json exists, it is authoritative even if output says otherwise."""
        from sova.dashboard.services.agent_recovery import get_sova_review_verdict
        from sova.db.models import OutputLine

        session = await get_session()
        async with session.begin():
            run = TaskRun(
                issue_number="303",
                role="command:review-pr",
                status="done",
                handoff_json={"next_action": "approve", "pending_findings": []},
                ended_at=datetime.now(timezone.utc),
            )
            session.add(run)
            await session.flush()
            run_id = run.id

            session.add(OutputLine(task_run_id=run_id, line_number=1, text="Verdict: Request changes"))

        result = await get_sova_review_verdict("303")
        assert result["verdict"] == "approve"


# ---------------------------------------------------------------------------
# Command outcome validation
# ---------------------------------------------------------------------------


class TestCommandOutcomeValidation:
    """_validate_command_outcome checks whether command runs produced expected outcomes."""

    async def test_non_command_role_skipped(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_command_outcome
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.role = "developer"
        result = await _validate_command_outcome(1, agent)
        assert result is None

    async def test_unknown_command_skipped(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_command_outcome
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.role = "command:some-other-command"
        result = await _validate_command_outcome(1, agent)
        assert result is None

    async def test_address_pr_fails_without_push_evidence(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_command_outcome
        from sova.dashboard.services.agent_pool import AgentState
        from sova.db.models import OutputLine

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="310", role="command:address-pr", status="done", pr_number=100)
            session.add(run)
            await session.flush()
            run_id = run.id

            session.add(OutputLine(task_run_id=run_id, line_number=1, text="All findings already addressed."))
            session.add(OutputLine(task_run_id=run_id, line_number=2, text="Tests pass. Summary complete."))

        agent = MagicMock(spec=AgentState)
        agent.role = "command:address-pr"
        agent.pr_number = 100
        agent.pre_run_sha = None
        agent.project_dir = None
        result = await _validate_command_outcome(run_id, agent)
        assert result is not None
        assert "without pushing" in result

    async def test_address_pr_passes_with_push_evidence(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_command_outcome
        from sova.dashboard.services.agent_pool import AgentState
        from sova.db.models import OutputLine

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="311", role="command:address-pr", status="done", pr_number=101)
            session.add(run)
            await session.flush()
            run_id = run.id

            session.add(OutputLine(task_run_id=run_id, line_number=1, text="Fixed all findings."))
            session.add(OutputLine(task_run_id=run_id, line_number=2, text="Committed: fix(core): address review"))
            session.add(OutputLine(task_run_id=run_id, line_number=3, text="git push --force-with-lease"))

        agent = MagicMock(spec=AgentState)
        agent.role = "command:address-pr"
        agent.pr_number = 101
        agent.pre_run_sha = None
        agent.project_dir = None
        result = await _validate_command_outcome(run_id, agent)
        assert result is None

    async def test_address_pr_passes_when_git_confirms_pushed(self) -> None:
        """Git ref comparison confirms push, skips text scanning entirely."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_db import _validate_command_outcome
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="312", role="command:address-pr", status="done", pr_number=102)
                session.add(run)
                await session.flush()
                run_id = run.id

        agent = MagicMock(spec=AgentState)
        agent.role = "command:address-pr"
        agent.pr_number = 102
        agent.pre_run_sha = None
        agent.project_dir = None
        with patch(
            "sova.dashboard.services.agent_db._check_pr_branch_pushed",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await _validate_command_outcome(run_id, agent)
        assert result is None

    async def test_address_pr_fails_when_git_confirms_unpushed(self) -> None:
        """Git ref comparison detects unpushed commits."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_db import _validate_command_outcome
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="313", role="command:address-pr", status="done", pr_number=103)
                session.add(run)
                await session.flush()
                run_id = run.id

        agent = MagicMock(spec=AgentState)
        agent.role = "command:address-pr"
        agent.pr_number = 103
        agent.pre_run_sha = None
        agent.project_dir = None
        with patch(
            "sova.dashboard.services.agent_db._check_pr_branch_pushed",
            new_callable=AsyncMock,
            return_value=False,
        ):
            result = await _validate_command_outcome(run_id, agent)
        assert result is not None
        assert "without pushing" in result

    async def test_review_pr_fails_without_post_evidence(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_command_outcome
        from sova.dashboard.services.agent_pool import AgentState
        from sova.db.models import OutputLine

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="312", role="command:review-pr", status="done", pr_number=102)
            session.add(run)
            await session.flush()
            run_id = run.id

            session.add(OutputLine(task_run_id=run_id, line_number=1, text="Reviewed the code."))
            session.add(OutputLine(task_run_id=run_id, line_number=2, text="Verdict: Approve -- looks good"))

        agent = MagicMock(spec=AgentState)
        agent.role = "command:review-pr"
        agent.pr_number = 102
        agent.project_dir = None
        result = await _validate_command_outcome(run_id, agent)
        assert result is not None
        assert "without posting a review" in result

    async def test_review_pr_passes_with_post_evidence(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_command_outcome
        from sova.dashboard.services.agent_pool import AgentState
        from sova.db.models import OutputLine

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="313", role="command:review-pr", status="done", pr_number=103)
            session.add(run)
            await session.flush()
            run_id = run.id

            session.add(OutputLine(task_run_id=run_id, line_number=1, text="Analysis complete."))
            session.add(
                OutputLine(
                    task_run_id=run_id,
                    line_number=2,
                    text="Review posted at: https://github.com/org/repo/pull/103#pullrequestreview-123",
                )
            )

        agent = MagicMock(spec=AgentState)
        agent.role = "command:review-pr"
        agent.pr_number = 103
        agent.project_dir = None
        result = await _validate_command_outcome(run_id, agent)
        assert result is None

    async def test_no_output_lines_passes_validation(self) -> None:
        """When no output lines exist (e.g., stream parsing failed), skip validation."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_command_outcome
        from sova.dashboard.services.agent_pool import AgentState

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="314", role="command:address-pr", status="done", pr_number=104)
            session.add(run)
            await session.flush()
            run_id = run.id

        agent = MagicMock(spec=AgentState)
        agent.role = "command:address-pr"
        agent.pr_number = 104
        agent.pre_run_sha = None
        agent.project_dir = None
        result = await _validate_command_outcome(run_id, agent)
        assert result is None


class TestReviewPrVerdictPersistence:
    """_validate_review_pr extracts verdict marker and persists handoff_json."""

    async def test_extract_marker_approve(self) -> None:
        from sova.dashboard.services.agent_db import _extract_review_verdict_marker

        lines = ["some output", "<!-- sova-review: approve -->", "more output"]
        assert _extract_review_verdict_marker(lines) == "approve"

    async def test_extract_marker_revise(self) -> None:
        from sova.dashboard.services.agent_db import _extract_review_verdict_marker

        lines = ["<!-- sova-review: revise -->"]
        assert _extract_review_verdict_marker(lines) == "revise"

    async def test_extract_marker_block(self) -> None:
        from sova.dashboard.services.agent_db import _extract_review_verdict_marker

        lines = ["<!-- sova-review: block -->"]
        assert _extract_review_verdict_marker(lines) == "block"

    async def test_extract_marker_case_insensitive(self) -> None:
        from sova.dashboard.services.agent_db import _extract_review_verdict_marker

        lines = ["<!-- sova-review: APPROVE -->"]
        assert _extract_review_verdict_marker(lines) == "approve"

    async def test_extract_marker_last_wins(self) -> None:
        from sova.dashboard.services.agent_db import _extract_review_verdict_marker

        lines = ["<!-- sova-review: approve -->", "<!-- sova-review: revise -->"]
        assert _extract_review_verdict_marker(lines) == "revise"

    async def test_extract_marker_same_line_last_wins(self) -> None:
        from sova.dashboard.services.agent_db import _extract_review_verdict_marker

        lines = ["<!-- sova-review: approve --> <!-- sova-review: revise -->"]
        assert _extract_review_verdict_marker(lines) == "revise"

    async def test_extract_marker_none_when_absent(self) -> None:
        from sova.dashboard.services.agent_db import _extract_review_verdict_marker

        lines = ["no marker here", "just regular output"]
        assert _extract_review_verdict_marker(lines) is None

    async def test_extract_marker_with_extra_whitespace(self) -> None:
        from sova.dashboard.services.agent_db import _extract_review_verdict_marker

        lines = ["<!--  sova-review:  approve  -->"]
        assert _extract_review_verdict_marker(lines) == "approve"

    async def test_persist_writes_handoff_json_approve(self) -> None:
        from sova.dashboard.services.agent_db import _persist_review_verdict

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="359", role="command:review-pr", status="done", pr_number=200)
            session.add(run)
            await session.flush()
            run_id = run.id

        await _persist_review_verdict(run_id, "approve", None)

        async with await get_session() as session:
            run = await session.get(TaskRun, run_id)
            assert run.handoff_json is not None
            assert run.handoff_json["next_action"] == "approve"
            assert run.handoff_json["pending_findings"] == []

    async def test_persist_writes_handoff_json_revise(self) -> None:
        from sova.dashboard.services.agent_db import _persist_review_verdict

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="359", role="command:review-pr", status="done", pr_number=201)
            session.add(run)
            await session.flush()
            run_id = run.id

        await _persist_review_verdict(run_id, "revise", None)

        async with await get_session() as session:
            run = await session.get(TaskRun, run_id)
            assert run.handoff_json is not None
            assert run.handoff_json["next_action"] == "address_review"

    async def test_persist_writes_handoff_json_block(self) -> None:
        from sova.dashboard.services.agent_db import _persist_review_verdict

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="359", role="command:review-pr", status="done", pr_number=202)
            session.add(run)
            await session.flush()
            run_id = run.id

        await _persist_review_verdict(run_id, "block", None)

        async with await get_session() as session:
            run = await session.get(TaskRun, run_id)
            assert run.handoff_json["next_action"] == "address_review"

    async def test_persist_skips_when_handoff_already_set(self) -> None:
        from sova.dashboard.services.agent_db import _persist_review_verdict

        existing = {"next_action": "approve", "pending_findings": []}
        session = await get_session()
        async with session.begin():
            run = TaskRun(
                issue_number="359",
                role="command:review-pr",
                status="done",
                pr_number=203,
                handoff_json=existing,
            )
            session.add(run)
            await session.flush()
            run_id = run.id

        await _persist_review_verdict(run_id, "revise", None)

        async with await get_session() as session:
            run = await session.get(TaskRun, run_id)
            assert run.handoff_json["next_action"] == "approve"

    async def test_validate_review_pr_persists_verdict_from_marker(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_review_pr
        from sova.dashboard.services.agent_pool import AgentState
        from sova.db.models import OutputLine

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="359", role="command:review-pr", status="done", pr_number=204)
            session.add(run)
            await session.flush()
            run_id = run.id

            session.add(OutputLine(task_run_id=run_id, line_number=1, text="<!-- sova-review: revise -->"))
            session.add(
                OutputLine(
                    task_run_id=run_id,
                    line_number=2,
                    text="Review posted at: https://github.com/org/repo/pull/204#pullrequestreview-456",
                )
            )

        agent = MagicMock(spec=AgentState)
        agent.role = "command:review-pr"
        agent.pr_number = 204
        agent.project_dir = None

        result = await _validate_review_pr(run_id, agent)
        assert result is None

        async with await get_session() as session:
            run = await session.get(TaskRun, run_id)
            assert run.handoff_json is not None
            assert run.handoff_json["next_action"] == "address_review"

    async def test_validate_review_pr_falls_back_to_prose_verdict(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_review_pr
        from sova.dashboard.services.agent_pool import AgentState
        from sova.db.models import OutputLine

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="359", role="command:review-pr", status="done", pr_number=205)
            session.add(run)
            await session.flush()
            run_id = run.id

            session.add(OutputLine(task_run_id=run_id, line_number=1, text="Verdict: Approve"))
            session.add(
                OutputLine(
                    task_run_id=run_id,
                    line_number=2,
                    text="Review posted successfully via pullRequestReview API",
                )
            )

        agent = MagicMock(spec=AgentState)
        agent.role = "command:review-pr"
        agent.pr_number = 205
        agent.project_dir = None

        result = await _validate_review_pr(run_id, agent)
        assert result is None

        async with await get_session() as session:
            run = await session.get(TaskRun, run_id)
            assert run.handoff_json is not None
            assert run.handoff_json["next_action"] == "approve"

    async def test_validate_review_pr_no_verdict_defaults_to_revise(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_review_pr
        from sova.dashboard.services.agent_pool import AgentState
        from sova.db.models import OutputLine

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="359", role="command:review-pr", status="done", pr_number=206)
            session.add(run)
            await session.flush()
            run_id = run.id

            session.add(
                OutputLine(
                    task_run_id=run_id,
                    line_number=1,
                    text="Review posted at: https://github.com/org/repo/pull/206#pullrequestreview-789",
                )
            )

        agent = MagicMock(spec=AgentState)
        agent.role = "command:review-pr"
        agent.pr_number = 206
        agent.project_dir = None

        result = await _validate_review_pr(run_id, agent)
        assert result is None

        async with await get_session() as session:
            run = await session.get(TaskRun, run_id)
            assert run.handoff_json is not None
            assert run.handoff_json["next_action"] == "address_review"


class TestDowngradeToFailed:
    """_downgrade_to_failed changes done runs to failed with a reason."""

    async def test_downgrades_done_to_failed(self) -> None:
        from sova.dashboard.services.agent_db import _downgrade_to_failed

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="320", role="command:address-pr", status="done")
            session.add(run)
            await session.flush()
            run_id = run.id

        await _downgrade_to_failed(run_id, "Did not push changes", None)

        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed.status == "failed"
                assert refreshed.error_message == "Did not push changes"

    async def test_does_not_downgrade_non_done(self) -> None:
        from sova.dashboard.services.agent_db import _downgrade_to_failed

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="321", role="developer", status="failed", error_message="Original error")
            session.add(run)
            await session.flush()
            run_id = run.id

        await _downgrade_to_failed(run_id, "New reason", None)

        async with await get_session() as session:
            async with session.begin():
                refreshed = await session.get(TaskRun, run_id)
                assert refreshed.status == "failed"
                assert refreshed.error_message == "Original error"


class TestPipelineOutcomeValidation:
    """_validate_pipeline_outcome checks whether pipeline roles executed their workflows."""

    async def test_command_role_skipped(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.role = "command:address-pr"
        result = await _validate_pipeline_outcome(1, agent)
        assert result is None

    async def test_non_pipeline_role_skipped(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.role = "triage"
        result = await _validate_pipeline_outcome(1, agent)
        assert result is None

    async def test_none_role_skipped(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.role = None
        result = await _validate_pipeline_outcome(1, agent)
        assert result is None

    async def test_developer_pipeline_bypassed(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="400",
                    role="developer",
                    status="done",
                    current_step="agent",
                    pr_number=None,
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        agent = MagicMock(spec=AgentState)
        agent.role = "developer"
        agent.project_dir = None
        result = await _validate_pipeline_outcome(run_id, agent)
        assert result is not None
        assert "Pipeline bypassed" in result
        assert "no PR was created" in result

    async def test_researcher_pipeline_bypassed(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="401",
                    role="researcher",
                    status="done",
                    current_step="agent",
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        agent = MagicMock(spec=AgentState)
        agent.role = "researcher"
        agent.project_dir = None
        result = await _validate_pipeline_outcome(run_id, agent)
        assert result is not None
        assert "Pipeline bypassed" in result
        assert "no PR was created" not in result

    async def test_planner_pipeline_bypassed(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="402",
                    role="planner",
                    status="done",
                    current_step="agent",
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        agent = MagicMock(spec=AgentState)
        agent.role = "planner"
        agent.project_dir = None
        result = await _validate_pipeline_outcome(run_id, agent)
        assert result is not None
        assert "Pipeline bypassed" in result

    async def test_developer_pipeline_ran_successfully(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState
        from sova.db.models import StepExecution

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="403",
                    role="developer",
                    status="done",
                    current_step="create_pr",
                    pr_number=42,
                )
                session.add(run)
                await session.flush()
                run_id = run.id
                session.add(StepExecution(task_run_id=run_id, step_name="develop", status="done"))
                session.add(StepExecution(task_run_id=run_id, step_name="create_pr", status="done"))

        agent = MagicMock(spec=AgentState)
        agent.role = "developer"
        agent.project_dir = None
        result = await _validate_pipeline_outcome(run_id, agent)
        assert result is None

    async def test_developer_pipeline_ran_no_pr_after_create_pr(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState
        from sova.db.models import StepExecution

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="404",
                    role="developer",
                    status="done",
                    current_step="push",
                    pr_number=None,
                )
                session.add(run)
                await session.flush()
                run_id = run.id
                session.add(StepExecution(task_run_id=run_id, step_name="develop", status="done"))
                session.add(StepExecution(task_run_id=run_id, step_name="create_pr", status="done"))

        agent = MagicMock(spec=AgentState)
        agent.role = "developer"
        agent.project_dir = None
        result = await _validate_pipeline_outcome(run_id, agent)
        assert result is not None
        assert "Pipeline incomplete" in result

    async def test_developer_early_step_no_pr_ok(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState
        from sova.db.models import StepExecution

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="405",
                    role="developer",
                    status="done",
                    current_step="develop",
                    pr_number=None,
                )
                session.add(run)
                await session.flush()
                run_id = run.id
                session.add(StepExecution(task_run_id=run_id, step_name="develop", status="done"))

        agent = MagicMock(spec=AgentState)
        agent.role = "developer"
        agent.project_dir = None
        result = await _validate_pipeline_outcome(run_id, agent)
        assert result is None

    async def test_sentinel_cleared_zero_steps(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="406",
                    role="developer",
                    status="done",
                    current_step=None,
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        agent = MagicMock(spec=AgentState)
        agent.role = "developer"
        agent.project_dir = None
        result = await _validate_pipeline_outcome(run_id, agent)
        assert result is None

    async def test_sentinel_active_with_steps(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState
        from sova.db.models import StepExecution

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="407",
                    role="developer",
                    status="done",
                    current_step="agent",
                )
                session.add(run)
                await session.flush()
                run_id = run.id
                session.add(StepExecution(task_run_id=run_id, step_name="develop", status="done"))

        agent = MagicMock(spec=AgentState)
        agent.role = "developer"
        agent.project_dir = None
        result = await _validate_pipeline_outcome(run_id, agent)
        assert result is None

    async def test_logs_prompt_on_bypass(self) -> None:
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="412",
                    role="developer",
                    status="done",
                    current_step="agent",
                    pr_number=None,
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        agent = MagicMock(spec=AgentState)
        agent.role = "developer"
        agent.project_dir = None
        agent.prompt = "Run sova run 99 --run-id 500"

        with patch("sova.dashboard.services.agent_db.log") as mock_log:
            result = await _validate_pipeline_outcome(run_id, agent)

        assert result is not None
        assert "Pipeline bypassed" in result
        mock_log.warning.assert_called_once()
        call_args = mock_log.warning.call_args
        assert call_args[0][0] == "validate_pipeline.bypass_diagnostic"
        assert "Run sova run 99" in call_args[1]["prompt_sent"]


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


class TestAgentContextHelpers:
    """Tests for agent_context helper functions."""

    def test_strip_frontmatter_removes_yaml(self) -> None:
        """_strip_frontmatter removes YAML frontmatter block."""
        from sova.dashboard.services.agent_context import _strip_frontmatter

        content = "---\nname: test\n---\nBody content"
        assert _strip_frontmatter(content) == "Body content"

    def test_strip_frontmatter_no_frontmatter(self) -> None:
        """_strip_frontmatter returns content unchanged when no frontmatter."""
        from sova.dashboard.services.agent_context import _strip_frontmatter

        content = "Just regular content"
        assert _strip_frontmatter(content) == "Just regular content"

    def test_resolve_command_prompt_local_command(self, tmp_path: Path) -> None:
        """_resolve_command_prompt returns slash command for local project commands."""
        from sova.dashboard.services.agent_context import _resolve_command_prompt

        cmd_dir = tmp_path / ".claude" / "commands"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "test-cmd.md").write_text("# Test")

        result = _resolve_command_prompt("test-cmd", {"issue": "42"}, tmp_path)
        assert result == "/test-cmd issue=42"

    def test_resolve_command_prompt_local_command_no_args(self, tmp_path: Path) -> None:
        """_resolve_command_prompt returns slash command without args."""
        from sova.dashboard.services.agent_context import _resolve_command_prompt

        cmd_dir = tmp_path / ".claude" / "commands"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "test-cmd.md").write_text("# Test")

        result = _resolve_command_prompt("test-cmd", None, tmp_path)
        assert result == "/test-cmd"

    def test_resolve_command_prompt_fallback(self, tmp_path: Path) -> None:
        """_resolve_command_prompt returns slash command when no file found."""
        from sova.dashboard.services.agent_context import _resolve_command_prompt

        result = _resolve_command_prompt("nonexistent", {"x": "1"}, tmp_path)
        assert result == "/nonexistent x=1"

    async def test_resolve_command_context_with_issue(self, tmp_path: Path) -> None:
        """_resolve_command_context extracts issue from args."""
        from sova.dashboard.services.agent_context import _resolve_command_context

        pr_number, issue = await _resolve_command_context({"issue": "42"}, "develop", tmp_path)
        assert pr_number is None
        assert issue == "42"

    async def test_resolve_command_context_with_pr(self, tmp_path: Path) -> None:
        """_resolve_command_context extracts pr_number from args."""
        from sova.dashboard.services.agent_context import _resolve_command_context

        pr_number, issue = await _resolve_command_context({"issue": "42", "pr": "100"}, "develop", tmp_path)
        assert pr_number == 100
        assert issue == "42"

    async def test_resolve_command_context_invalid_pr(self, tmp_path: Path) -> None:
        """_resolve_command_context handles non-numeric PR gracefully."""
        from sova.dashboard.services.agent_context import _resolve_command_context

        pr_number, issue = await _resolve_command_context({"pr": "abc"}, "develop", tmp_path)
        assert pr_number is None
        assert issue == ""

    async def test_resolve_command_context_no_issue_no_pr(self, tmp_path: Path) -> None:
        """Without issue or PR, issue is empty -- never the command name."""
        from sova.dashboard.services.agent_context import _resolve_command_context

        pr_number, issue = await _resolve_command_context({}, "address-pr", tmp_path)
        assert pr_number is None
        assert issue == ""

    async def test_resolve_command_context_pr_resolves_issue(self, tmp_path: Path) -> None:
        """_resolve_command_context resolves issue from PR when issue is missing."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_context import _resolve_command_context

        with patch(
            "sova.dashboard.services.agent_context._resolve_issue_from_pr",
            new_callable=AsyncMock,
            return_value="55",
        ):
            pr_number, issue = await _resolve_command_context({"pr": "100"}, "develop", tmp_path)
        assert pr_number == 100
        assert issue == "55"

    async def test_resolve_project_gh_env_success(self, tmp_path: Path) -> None:
        """_resolve_project_gh_env returns env dict on success."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_context import _resolve_project_gh_env

        mock_cfg = MagicMock()
        mock_cfg.github_user = "testuser"
        with (
            patch("sova.config.loader.load_config", return_value=mock_cfg),
            patch(
                "sova.utils.gh.resolve_gh_env",
                new_callable=AsyncMock,
                return_value={"GH_TOKEN": "tok"},
            ),
        ):
            result = await _resolve_project_gh_env(tmp_path)
        assert result == {"GH_TOKEN": "tok"}

    async def test_resolve_project_gh_env_failure(self, tmp_path: Path) -> None:
        """_resolve_project_gh_env returns None on failure."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_context import _resolve_project_gh_env

        with patch(
            "sova.config.loader.load_config",
            side_effect=Exception("no config"),
        ):
            result = await _resolve_project_gh_env(tmp_path)
        assert result is None

    async def test_resolve_issue_from_pr_non_numeric(self, tmp_path: Path) -> None:
        """_resolve_issue_from_pr handles non-numeric pr_number gracefully."""
        from sova.dashboard.services.agent_context import _resolve_issue_from_pr

        result = await _resolve_issue_from_pr("abc", tmp_path)
        assert result == ""

    async def test_resolve_issue_from_pr_success(self, tmp_path: Path) -> None:
        """_resolve_issue_from_pr extracts issue from PR body."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_context import _resolve_issue_from_pr

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.stdout = "Some text\nCloses #42\nMore text"
        with (
            patch(
                "sova.dashboard.services.agent_context.run_shell",
                new_callable=AsyncMock,
                return_value=mock_result,
            ),
            patch(
                "sova.dashboard.services.agent_context._is_issue",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await _resolve_issue_from_pr(100, tmp_path)
        assert result == "42"

    async def test_resolve_issue_from_pr_no_match(self, tmp_path: Path) -> None:
        """_resolve_issue_from_pr returns empty when no issue link found."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_context import _resolve_issue_from_pr

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.stdout = "No issue link here"
        with patch(
            "sova.dashboard.services.agent_context.run_shell",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            result = await _resolve_issue_from_pr(100, tmp_path)
        assert result == ""


class TestIsIssue:
    """Unit tests for the _is_issue helper."""

    async def test_returns_true_for_real_issue(self, tmp_path: Path, monkeypatch) -> None:
        """Returns True when the Issues API returns no pull_request field (empty output)."""
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_context import _is_issue

        mock_result = AsyncMock()
        mock_result.return_value.success = True
        mock_result.return_value.stdout = ""
        monkeypatch.setattr("sova.dashboard.services.agent_context.run_shell", mock_result)

        assert await _is_issue("42", tmp_path) is True

    async def test_returns_false_for_pr(self, tmp_path: Path, monkeypatch) -> None:
        """Returns False when the Issues API returns a pull_request field (it is a PR)."""
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_context import _is_issue

        mock_result = AsyncMock()
        mock_result.return_value.success = True
        mock_result.return_value.stdout = '{"url": "https://..."}'
        monkeypatch.setattr("sova.dashboard.services.agent_context.run_shell", mock_result)

        assert await _is_issue("339", tmp_path) is False

    async def test_returns_true_on_exception(self, tmp_path: Path, monkeypatch) -> None:
        """Returns True (safe default) when run_shell raises."""
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_context import _is_issue

        monkeypatch.setattr(
            "sova.dashboard.services.agent_context.run_shell",
            AsyncMock(side_effect=OSError("gh not found")),
        )

        assert await _is_issue("99", tmp_path) is True

    async def test_returns_true_on_failed_command(self, tmp_path: Path, monkeypatch) -> None:
        """Returns False when the gh command fails (success=False)."""
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_context import _is_issue

        mock_result = AsyncMock()
        mock_result.return_value.success = False
        mock_result.return_value.stdout = ""
        monkeypatch.setattr("sova.dashboard.services.agent_context.run_shell", mock_result)

        assert await _is_issue("99", tmp_path) is False


class TestCheckPrBranchPushed:
    """Unit tests for the _check_pr_branch_pushed helper."""

    async def test_returns_none_when_no_pr_number(self, tmp_path: Path) -> None:
        """Returns None immediately when agent has no pr_number."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _check_pr_branch_pushed
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 0
        assert await _check_pr_branch_pushed(agent) is None

    async def test_returns_none_when_branch_lookup_fails(self, tmp_path: Path, monkeypatch) -> None:
        """Returns None when the gh CLI fails to return a branch name."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _check_pr_branch_pushed
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 100
        agent.project_dir = tmp_path

        async def mock_run(*args, **kwargs):
            result = MagicMock()
            result.success = False
            result.stdout = ""
            return result

        monkeypatch.setattr("sova.utils.shell.run", mock_run)
        assert await _check_pr_branch_pushed(agent) is None

    async def test_returns_none_when_fetch_fails(self, tmp_path: Path, monkeypatch) -> None:
        """Returns None when git fetch fails, to prevent stale-ref false result."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _check_pr_branch_pushed
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 100
        agent.project_dir = tmp_path
        call_count = 0

        async def mock_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.success = True
                result.stdout = "feature/my-branch\n"
            else:
                result.success = False
                result.stdout = ""
            return result

        monkeypatch.setattr("sova.utils.shell.run", mock_run)
        assert await _check_pr_branch_pushed(agent) is None

    async def test_returns_true_when_no_unpushed_commits(self, tmp_path: Path, monkeypatch) -> None:
        """Returns True when rev-list count is 0 (branch is pushed)."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _check_pr_branch_pushed
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 100
        agent.project_dir = tmp_path
        call_count = 0

        async def mock_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            result.success = True
            if call_count == 1:
                result.stdout = "feature/my-branch\n"
            elif call_count == 2:
                result.stdout = ""
            else:
                result.stdout = "0\n"
            return result

        monkeypatch.setattr("sova.utils.shell.run", mock_run)
        assert await _check_pr_branch_pushed(agent) is True

    async def test_returns_false_when_unpushed_commits(self, tmp_path: Path, monkeypatch) -> None:
        """Returns False when rev-list count > 0 (commits not pushed)."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _check_pr_branch_pushed
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 100
        agent.project_dir = tmp_path
        call_count = 0

        async def mock_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            result.success = True
            if call_count == 1:
                result.stdout = "feature/my-branch\n"
            elif call_count == 2:
                result.stdout = ""
            else:
                result.stdout = "3\n"
            return result

        monkeypatch.setattr("sova.utils.shell.run", mock_run)
        assert await _check_pr_branch_pushed(agent) is False


class TestCapturePrHeadSha:
    """Unit tests for _capture_pr_head_sha."""

    async def test_returns_sha_on_success(self, tmp_path: Path, monkeypatch) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _capture_pr_head_sha

        async def mock_run(*args, **kwargs):
            result = MagicMock()
            result.success = True
            result.stdout = "abc123def456\n"
            return result

        monkeypatch.setattr("sova.utils.shell.run", mock_run)
        sha = await _capture_pr_head_sha(42, tmp_path)
        assert sha == "abc123def456"

    async def test_returns_none_on_failure(self, tmp_path: Path, monkeypatch) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _capture_pr_head_sha

        async def mock_run(*args, **kwargs):
            result = MagicMock()
            result.success = False
            result.stdout = ""
            return result

        monkeypatch.setattr("sova.utils.shell.run", mock_run)
        sha = await _capture_pr_head_sha(42, tmp_path)
        assert sha is None

    async def test_returns_none_on_exception(self, tmp_path: Path, monkeypatch) -> None:
        from sova.dashboard.services.agent_db import _capture_pr_head_sha

        async def mock_run(*args, **kwargs):
            raise TimeoutError("timed out")

        monkeypatch.setattr("sova.utils.shell.run", mock_run)
        sha = await _capture_pr_head_sha(42, tmp_path)
        assert sha is None

    async def test_returns_none_on_empty_stdout(self, tmp_path: Path, monkeypatch) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _capture_pr_head_sha

        async def mock_run(*args, **kwargs):
            result = MagicMock()
            result.success = True
            result.stdout = "   \n"
            return result

        monkeypatch.setattr("sova.utils.shell.run", mock_run)
        sha = await _capture_pr_head_sha(42, tmp_path)
        assert sha is None


class TestCheckPrPushedViaSha:
    """Unit tests for _check_pr_pushed_via_sha."""

    async def test_returns_none_when_no_pre_run_sha(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _check_pr_pushed_via_sha
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pre_run_sha = None
        agent.pr_number = 42
        assert await _check_pr_pushed_via_sha(agent) is None

    async def test_returns_none_when_no_pr_number(self) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _check_pr_pushed_via_sha
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pre_run_sha = "abc123"
        agent.pr_number = None
        assert await _check_pr_pushed_via_sha(agent) is None

    async def test_returns_true_when_sha_changed(self, tmp_path: Path, monkeypatch) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _check_pr_pushed_via_sha
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pre_run_sha = "aaa111"
        agent.pr_number = 42
        agent.project_dir = tmp_path

        async def mock_run(*args, **kwargs):
            result = MagicMock()
            result.success = True
            result.stdout = "bbb222\tOPEN\n"
            return result

        monkeypatch.setattr("sova.utils.shell.run", mock_run)
        assert await _check_pr_pushed_via_sha(agent) is True

    async def test_returns_true_when_pr_merged(self, tmp_path: Path, monkeypatch) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _check_pr_pushed_via_sha
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pre_run_sha = "aaa111"
        agent.pr_number = 42
        agent.project_dir = tmp_path

        async def mock_run(*args, **kwargs):
            result = MagicMock()
            result.success = True
            result.stdout = "aaa111\tMERGED\n"
            return result

        monkeypatch.setattr("sova.utils.shell.run", mock_run)
        assert await _check_pr_pushed_via_sha(agent) is True

    async def test_returns_none_when_sha_unchanged(self, tmp_path: Path, monkeypatch) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _check_pr_pushed_via_sha
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pre_run_sha = "aaa111"
        agent.pr_number = 42
        agent.project_dir = tmp_path

        async def mock_run(*args, **kwargs):
            result = MagicMock()
            result.success = True
            result.stdout = "aaa111\tOPEN\n"
            return result

        monkeypatch.setattr("sova.utils.shell.run", mock_run)
        assert await _check_pr_pushed_via_sha(agent) is None

    async def test_returns_none_on_api_failure(self, tmp_path: Path, monkeypatch) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _check_pr_pushed_via_sha
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pre_run_sha = "aaa111"
        agent.pr_number = 42
        agent.project_dir = tmp_path

        async def mock_run(*args, **kwargs):
            result = MagicMock()
            result.success = False
            result.stdout = ""
            return result

        monkeypatch.setattr("sova.utils.shell.run", mock_run)
        assert await _check_pr_pushed_via_sha(agent) is None

    async def test_returns_none_on_exception(self, tmp_path: Path, monkeypatch) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _check_pr_pushed_via_sha
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pre_run_sha = "aaa111"
        agent.pr_number = 42
        agent.project_dir = tmp_path

        async def mock_run(*args, **kwargs):
            raise TimeoutError("timed out")

        monkeypatch.setattr("sova.utils.shell.run", mock_run)
        assert await _check_pr_pushed_via_sha(agent) is None

    async def test_returns_none_when_sha_empty(self, tmp_path: Path, monkeypatch) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _check_pr_pushed_via_sha
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pre_run_sha = "abc123"
        agent.pr_number = 42
        agent.project_dir = tmp_path

        async def mock_run(*args, **kwargs):
            result = MagicMock()
            result.success = True
            result.stdout = "\tOPEN\n"
            return result

        monkeypatch.setattr("sova.utils.shell.run", mock_run)
        assert await _check_pr_pushed_via_sha(agent) is None

    async def test_returns_none_when_state_empty(self, tmp_path: Path, monkeypatch) -> None:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_db import _check_pr_pushed_via_sha
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pre_run_sha = "abc123"
        agent.pr_number = 42
        agent.project_dir = tmp_path

        async def mock_run(*args, **kwargs):
            result = MagicMock()
            result.success = True
            result.stdout = "deadbeef\t\n"
            return result

        monkeypatch.setattr("sova.utils.shell.run", mock_run)
        assert await _check_pr_pushed_via_sha(agent) is None


class TestValidateAddressPrWithSha:
    """Tests that _validate_address_pr uses SHA comparison as primary check."""

    async def test_sha_check_short_circuits_on_push_detected(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_db import _validate_address_pr
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 42
        agent.pre_run_sha = "aaa111"
        agent.project_dir = tmp_path

        with (
            patch(
                "sova.dashboard.services.agent_db._check_pr_pushed_via_sha",
                new_callable=AsyncMock,
                return_value=True,
            ) as sha_mock,
            patch(
                "sova.dashboard.services.agent_db._check_pr_branch_pushed",
                new_callable=AsyncMock,
            ) as branch_mock,
        ):
            result = await _validate_address_pr(1, agent)
        assert result is None
        sha_mock.assert_awaited_once_with(agent)
        branch_mock.assert_not_awaited()

    async def test_falls_through_to_branch_check_when_sha_inconclusive(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_db import _validate_address_pr
        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.pr_number = 42
        agent.pre_run_sha = "aaa111"
        agent.project_dir = tmp_path

        with (
            patch(
                "sova.dashboard.services.agent_db._check_pr_pushed_via_sha",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "sova.dashboard.services.agent_db._check_pr_branch_pushed",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await _validate_address_pr(1, agent)
        assert result is None


class TestPipelineBypassDiagnosticLogging:
    """Tests that _validate_pipeline_outcome logs prompt on bypass."""

    async def test_logs_prompt_on_bypass(self) -> None:
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services.agent_db import _validate_pipeline_outcome
        from sova.dashboard.services.agent_pool import AgentState

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="99",
                    role="developer",
                    status="done",
                    current_step="agent",
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        agent = MagicMock(spec=AgentState)
        agent.role = "developer"
        agent.project_dir = None
        agent.prompt = "Run sova run 99"
        with patch("sova.dashboard.services.agent_db.log") as mock_log:
            result = await _validate_pipeline_outcome(run_id, agent)
        assert result is not None
        assert "Pipeline bypassed" in result
        mock_log.warning.assert_any_call(
            "validate_pipeline.bypass_diagnostic",
            run_id=run_id,
            role="developer",
            prompt_sent="Run sova run 99",
        )


class TestBuildBypassMessage:
    """Tests for _build_bypass_message helper."""

    def test_developer_no_pr(self) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.agent_db import _build_bypass_message

        with patch("sova.dashboard.services.agent_db.log"):
            msg = _build_bypass_message("developer", None, "some prompt", 1)
        assert "Pipeline bypassed" in msg
        assert "no PR was created" in msg

    def test_developer_with_pr(self) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.agent_db import _build_bypass_message

        with patch("sova.dashboard.services.agent_db.log"):
            msg = _build_bypass_message("developer", 42, "some prompt", 1)
        assert "Pipeline bypassed" in msg
        assert "no PR was created" not in msg

    def test_researcher_role(self) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.agent_db import _build_bypass_message

        with patch("sova.dashboard.services.agent_db.log"):
            msg = _build_bypass_message("researcher", None, None, 1)
        assert "researcher" in msg
        assert "no PR was created" not in msg

    def test_no_prompt_skips_log(self) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.agent_db import _build_bypass_message

        with patch("sova.dashboard.services.agent_db.log") as mock_log:
            _build_bypass_message("developer", None, None, 1)
        mock_log.warning.assert_not_called()

    def test_prompt_truncated_to_500(self) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.agent_db import _build_bypass_message

        long_prompt = "x" * 1000
        with patch("sova.dashboard.services.agent_db.log") as mock_log:
            _build_bypass_message("developer", None, long_prompt, 5)
        mock_log.warning.assert_called_once()
        call_kwargs = mock_log.warning.call_args
        assert len(call_kwargs[1]["prompt_sent"]) == 500


@pytest.mark.asyncio
class TestCheckIncompletePr:
    """Tests for _check_incomplete_pr helper."""

    async def test_returns_none_when_no_pr_steps(self) -> None:
        from sova.dashboard.services.agent_db import _check_incomplete_pr

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="1", role="developer", status="done")
                session.add(run)
                await session.flush()
                run_id = run.id

        async with await get_session() as session:
            async with session.begin():
                result = await _check_incomplete_pr(run_id, session)
        assert result is None

    async def test_returns_message_when_create_pr_done(self) -> None:
        from sova.core.state import STEP_DONE_STATUSES
        from sova.dashboard.services.agent_db import _check_incomplete_pr
        from sova.db.models import StepExecution

        done_status = next(iter(STEP_DONE_STATUSES))

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(issue_number="2", role="developer", status="done")
                session.add(run)
                await session.flush()
                run_id = run.id
                step = StepExecution(task_run_id=run_id, step_name="create_pr", status=done_status)
                session.add(step)

        async with await get_session() as session:
            async with session.begin():
                result = await _check_incomplete_pr(run_id, session)
        assert result is not None
        assert "pr_number is still None" in result


class TestStepProgress:
    """Tests for get_step_progress pipeline variant detection."""

    def test_developer_pipeline_default(self) -> None:
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        result = get_step_progress("develop")
        assert result["pipeline_variant"] == "developer"
        assert result["step_index"] == 4

    def test_address_review_from_step(self) -> None:
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        result = get_step_progress("rebase")
        assert result["pipeline_variant"] == "address_review"
        assert result["step_index"] == 1

    def test_none_step_defaults_to_developer(self) -> None:
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        result = get_step_progress(None)
        assert result["pipeline_variant"] == "developer"
        assert result["step_index"] == 0

    def test_none_step_with_pr_number_is_address_review(self) -> None:
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        result = get_step_progress(None, role="developer", pr_number=147)
        assert result["pipeline_variant"] == "address_review"
        assert result["step_index"] == 0
        assert result["total_steps"] == 10

    def test_agent_step_with_pr_number_is_address_review(self) -> None:
        """Dashboard outer TaskRun (current_step='agent') with pr_number -> address_review."""
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        result = get_step_progress("agent", role="developer", pr_number=147)
        assert result["pipeline_variant"] == "address_review"
        assert result["step_index"] == 0

    def test_shared_step_with_pr_number_is_developer(self) -> None:
        """WorkflowEngine TaskRun on shared step with pr_number acquired mid-pipeline."""
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        result = get_step_progress("commit", role="developer", pr_number=147)
        assert result["pipeline_variant"] == "developer"
        assert result["step_index"] == 7

    def test_shared_step_without_pr_number_is_developer(self) -> None:
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        result = get_step_progress("commit")
        assert result["pipeline_variant"] == "developer"
        assert result["step_index"] == 7

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

    def test_command_role_approve_merge(self) -> None:
        from sova.dashboard.services.agent_lifecycle import get_step_progress

        result = get_step_progress(None, role="command:approve-merge")
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
    """Tests for the DB-backed output persistence layer."""

    async def _create_run(self, session: AsyncSession, run_id: int) -> None:
        """Create a TaskRun record for FK constraint."""
        async with session.begin():
            session.add(TaskRun(id=run_id, role="developer", status="running"))

    @pytest.mark.asyncio
    async def test_output_writer_write_and_read(self, session) -> None:
        from sova.core.output import OutputWriter, read_lines

        await self._create_run(session, 1)
        writer = OutputWriter(Path("/tmp/fake"), run_id=1)
        writer.write_line("Hello, world")
        writer.write_line("Second line")
        await writer.close()

        lines, total = await read_lines(Path("/tmp/fake"), run_id=1)
        assert total == 2
        assert lines == ["Hello, world", "Second line"]

    @pytest.mark.asyncio
    async def test_output_writer_read_with_offset(self, session) -> None:
        from sova.core.output import OutputWriter, read_lines

        await self._create_run(session, 2)
        writer = OutputWriter(Path("/tmp/fake"), run_id=2)
        writer.write_line("Line 1")
        writer.write_line("Line 2")
        writer.write_line("Line 3")
        await writer.close()

        lines, total = await read_lines(Path("/tmp/fake"), run_id=2, since=1)
        assert total == 3
        assert lines == ["Line 2", "Line 3"]

    @pytest.mark.asyncio
    async def test_read_lines_empty_run(self) -> None:
        from sova.core.output import read_lines

        lines, total = await read_lines(Path("/tmp/fake"), run_id=999)
        assert lines == []
        assert total == 0

    @pytest.mark.asyncio
    async def test_output_writer_strips_trailing_newlines(self, session) -> None:
        from sova.core.output import OutputWriter, read_lines

        await self._create_run(session, 3)
        writer = OutputWriter(Path("/tmp/fake"), run_id=3)
        writer.write_line("Line with newline\n")
        await writer.close()

        lines, total = await read_lines(Path("/tmp/fake"), run_id=3)
        assert total == 1
        assert lines == ["Line with newline"]

    @pytest.mark.asyncio
    async def test_flush_threshold(self, session) -> None:
        from sova.core.output import OutputWriter

        await self._create_run(session, 4)
        writer = OutputWriter(Path("/tmp/fake"), run_id=4, flush_threshold=3)
        writer.write_line("A")
        writer.write_line("B")
        assert not writer.should_flush()
        writer.write_line("C")
        assert writer.should_flush()
        await writer.close()

    @pytest.mark.asyncio
    async def test_closed_writer_ignores_writes(self, session) -> None:
        from sova.core.output import OutputWriter, read_lines

        await self._create_run(session, 5)
        writer = OutputWriter(Path("/tmp/fake"), run_id=5)
        writer.write_line("Before close")
        await writer.close()
        writer.write_line("After close")

        lines, total = await read_lines(Path("/tmp/fake"), run_id=5)
        assert total == 1
        assert lines == ["Before close"]

    @pytest.mark.asyncio
    async def test_cleanup_old_output(self, session) -> None:
        from sova.core.output import OutputWriter, cleanup_old_output, read_lines

        async with session.begin():
            session.add(
                TaskRun(
                    id=100,
                    role="developer",
                    status="done",
                    ended_at=datetime.now(timezone.utc) - timedelta(days=60),
                )
            )

        writer = OutputWriter(Path("/tmp/fake"), run_id=100)
        writer.write_line("old output")
        await writer.close()

        deleted = await cleanup_old_output(Path("/tmp/fake"), retention_days=30)
        assert deleted == 1

        lines, total = await read_lines(Path("/tmp/fake"), run_id=100)
        assert total == 0

    @pytest.mark.asyncio
    async def test_cleanup_preserves_recent_output(self, session) -> None:
        from sova.core.output import OutputWriter, cleanup_old_output, read_lines

        async with session.begin():
            session.add(
                TaskRun(
                    id=101,
                    role="developer",
                    status="done",
                    ended_at=datetime.now(timezone.utc) - timedelta(days=5),
                )
            )

        writer = OutputWriter(Path("/tmp/fake"), run_id=101)
        writer.write_line("recent output")
        await writer.close()

        deleted = await cleanup_old_output(Path("/tmp/fake"), retention_days=30)
        assert deleted == 0

        lines, total = await read_lines(Path("/tmp/fake"), run_id=101)
        assert total == 1

    @pytest.mark.asyncio
    async def test_legacy_file_read(self, tmp_path) -> None:
        from sova.core.output import read_lines_from_file

        log_file = tmp_path / "42.log"
        log_file.write_text("line one\nline two\nline three\n")

        lines, total = read_lines_from_file(log_file)
        assert total == 3
        assert lines == ["line one", "line two", "line three"]

        lines, total = read_lines_from_file(log_file, since=1)
        assert total == 3
        assert lines == ["line two", "line three"]

    @pytest.mark.asyncio
    async def test_legacy_file_read_missing(self, tmp_path) -> None:
        from sova.core.output import read_lines_from_file

        lines, total = read_lines_from_file(tmp_path / "nonexistent.log")
        assert lines == []
        assert total == 0

    @pytest.mark.asyncio
    async def test_readopted_run_seeds_line_number(self, session) -> None:
        """A new OutputWriter on a run with existing output continues numbering."""
        from sova.core.output import OutputWriter, read_lines

        await self._create_run(session, 200)
        # First writer writes 3 lines
        w1 = OutputWriter(Path("/tmp/fake"), run_id=200)
        w1.write_line("Line 0")
        w1.write_line("Line 1")
        w1.write_line("Line 2")
        await w1.close()

        # Second writer (re-adoption) should continue from line 3
        w2 = OutputWriter(Path("/tmp/fake"), run_id=200)
        w2.write_line("Line 3")
        w2.write_line("Line 4")
        await w2.close()

        lines, total = await read_lines(Path("/tmp/fake"), run_id=200)
        assert total == 5
        assert lines == ["Line 0", "Line 1", "Line 2", "Line 3", "Line 4"]

    @pytest.mark.asyncio
    async def test_double_close_is_noop(self, session) -> None:
        from sova.core.output import OutputWriter, read_lines

        await self._create_run(session, 6)
        writer = OutputWriter(Path("/tmp/fake"), run_id=6)
        writer.write_line("data")
        await writer.close()
        await writer.close()  # second close is a no-op

        lines, total = await read_lines(Path("/tmp/fake"), run_id=6)
        assert total == 1
        assert lines == ["data"]

    @pytest.mark.asyncio
    async def test_flush_failure_recovery(self, session) -> None:
        from unittest.mock import patch

        from sova.core.output import OutputWriter, read_lines

        await self._create_run(session, 7)
        writer = OutputWriter(Path("/tmp/fake"), run_id=7, flush_threshold=2)
        writer.write_line("line1")
        writer.write_line("line2")

        # First flush fails -- lines should be preserved in buffer
        with patch("sova.db.session.get_session", side_effect=Exception("db down")):
            await writer.flush()

        assert len(writer._buffer) == 2, "failed flush should restore buffer"

        # Second flush succeeds -- lines reach DB
        await writer.close()
        lines, total = await read_lines(Path("/tmp/fake"), run_id=7)
        assert total == 2
        assert lines == ["line1", "line2"]

    @pytest.mark.asyncio
    async def test_cleanup_old_output_exception(self) -> None:
        from unittest.mock import patch

        from sova.core.output import cleanup_old_output

        with patch("sova.db.session.get_session", side_effect=Exception("db error")):
            deleted = await cleanup_old_output(Path("/tmp/fake"), retention_days=30)
        assert deleted == 0

    @pytest.mark.asyncio
    async def test_read_lines_exception(self) -> None:
        from unittest.mock import patch

        from sova.core.output import read_lines

        with patch("sova.db.session.get_session", side_effect=Exception("db error")):
            lines, total = await read_lines(Path("/tmp/fake"), run_id=999)
        assert lines == []
        assert total == 0

    def test_legacy_file_read_oserror(self, tmp_path) -> None:
        from unittest.mock import patch

        from sova.core.output import read_lines_from_file

        log_file = tmp_path / "broken.log"
        log_file.write_text("data")
        with patch("builtins.open", side_effect=OSError("permission denied")):
            lines, total = read_lines_from_file(log_file)
        assert lines == []
        assert total == 0


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

    def test_generate_sova_toml_jira_config(self) -> None:
        from sova.dashboard.services.setup_service import TomlConfig, generate_sova_toml

        cfg = TomlConfig(
            task_source="jira",
            jira_base_url="https://test.atlassian.net",
            jira_email="user@example.com",
            jira_project_key="PROJ",
            jira_component="Backend",
            jira_track_agent_work=True,
            jira_status_mapping={"In Progress": "in_progress", "Done": "done"},
        )
        content = generate_sova_toml(cfg)
        assert 'type = "jira"' in content
        assert 'jira_base_url = "https://test.atlassian.net"' in content
        assert 'jira_email = "user@example.com"' in content
        assert 'jira_project_key = "PROJ"' in content
        assert 'jira_component = "Backend"' in content
        assert "jira_track_agent_work = true" in content
        assert "jira_status_mapping" in content

    def test_generate_sova_toml_jira_no_optional_fields(self) -> None:
        from sova.dashboard.services.setup_service import TomlConfig, generate_sova_toml

        cfg = TomlConfig(
            task_source="jira",
            jira_base_url="https://test.atlassian.net",
            jira_email="user@example.com",
            jira_project_key="PROJ",
        )
        content = generate_sova_toml(cfg)
        assert 'type = "jira"' in content
        assert "jira_component" not in content
        assert "jira_track_agent_work" not in content
        assert "jira_status_mapping" not in content

    def test_toml_config_jira_defaults(self) -> None:
        from sova.dashboard.services.setup_service import TomlConfig

        cfg = TomlConfig()
        assert cfg.jira_base_url == ""
        assert cfg.jira_email == ""
        assert cfg.jira_project_key == ""
        assert cfg.jira_component == ""
        assert cfg.jira_status_mapping is None
        assert cfg.jira_track_agent_work is False


class TestJiraSetupAPI:
    async def test_jira_test_connection_success(self, client) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        with patch("sova.dashboard.services.setup_service._jira_api_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200, json=lambda: {"displayName": "Test User", "emailAddress": "e@x.com"}
            )
            resp = await client.post(
                "/api/setup/jira/test",
                json={"base_url": "https://test.atlassian.net", "email": "e@x.com", "api_token": "t"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["display_name"] == "Test User"

    async def test_jira_projects_success(self, client) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        with patch("sova.dashboard.services.setup_service._jira_api_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200, json=lambda: [{"key": "PROJ", "name": "My Project", "lead": {"displayName": "Lead"}}]
            )
            resp = await client.post(
                "/api/setup/jira/projects",
                json={"base_url": "https://test.atlassian.net", "email": "e@x.com", "api_token": "t"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert len(data["projects"]) == 1
        assert data["projects"][0]["key"] == "PROJ"

    async def test_jira_statuses_success(self, client) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        with patch("sova.dashboard.services.setup_service._jira_api_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: [
                    {
                        "statuses": [
                            {"name": "To Do", "statusCategory": {"name": "To Do"}},
                            {"name": "In Progress", "statusCategory": {"name": "In Progress"}},
                        ],
                    }
                ],
            )
            resp = await client.post(
                "/api/setup/jira/statuses",
                json={
                    "base_url": "https://test.atlassian.net",
                    "email": "e@x.com",
                    "api_token": "t",
                    "project_key": "TEST",
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert len(data["statuses"]) == 2
        assert "To Do" in data["suggested_mapping"]


class TestJiraSSRFPrevention:
    """Verify _validate_jira_base_url rejects internal/private addresses."""

    def test_rejects_localhost(self) -> None:
        from sova.dashboard.services.setup_service import _validate_jira_base_url

        with pytest.raises(ValueError, match="local address"):
            _validate_jira_base_url("https://localhost/jira")

    def test_rejects_loopback_ip(self) -> None:
        from sova.dashboard.services.setup_service import _validate_jira_base_url

        with pytest.raises(ValueError, match="local address"):
            _validate_jira_base_url("https://127.0.0.1/jira")

    def test_rejects_private_10_range(self) -> None:
        from sova.dashboard.services.setup_service import _validate_jira_base_url

        with pytest.raises(ValueError, match="private/reserved"):
            _validate_jira_base_url("https://10.0.0.1/jira")

    def test_rejects_private_172_range(self) -> None:
        from sova.dashboard.services.setup_service import _validate_jira_base_url

        with pytest.raises(ValueError, match="private/reserved"):
            _validate_jira_base_url("https://172.16.0.1/jira")

    def test_rejects_private_192_range(self) -> None:
        from sova.dashboard.services.setup_service import _validate_jira_base_url

        with pytest.raises(ValueError, match="private/reserved"):
            _validate_jira_base_url("https://192.168.1.1/jira")

    def test_rejects_link_local(self) -> None:
        from sova.dashboard.services.setup_service import _validate_jira_base_url

        with pytest.raises(ValueError, match="private/reserved"):
            _validate_jira_base_url("https://169.254.1.1/jira")

    def test_rejects_http_scheme(self) -> None:
        from sova.dashboard.services.setup_service import _validate_jira_base_url

        with pytest.raises(ValueError, match="only https"):
            _validate_jira_base_url("http://example.atlassian.net")

    def test_rejects_ipv6_loopback(self) -> None:
        from sova.dashboard.services.setup_service import _validate_jira_base_url

        with pytest.raises(ValueError, match="local address"):
            _validate_jira_base_url("https://[::1]/jira")

    def test_accepts_valid_atlassian_url(self) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.setup_service import _validate_jira_base_url

        # Mock DNS resolution to return a public IP
        with patch(
            "socket.getaddrinfo",
            return_value=[
                (2, 1, 6, "", ("104.192.141.1", 0)),
            ],
        ):
            result = _validate_jira_base_url("https://mycompany.atlassian.net")
        assert result == "https://mycompany.atlassian.net"


# ---------------------------------------------------------------------------
# create_starter_milestones
# ---------------------------------------------------------------------------


class TestCreateStarterMilestones:
    async def test_creates_milestones_skips_existing(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, patch

        from sova.adapters.base import Milestone
        from sova.dashboard.services.setup_service import create_starter_milestones

        mock_adapter = AsyncMock()
        mock_adapter.list_milestones.return_value = [
            Milestone(title="Phase 1: Now", state="open"),
        ]
        mock_adapter.create_milestone.return_value = Milestone(title="Phase 2: Next", state="open")

        with (
            patch("sova.config.loader.load_config"),
            patch("sova.adapters.create_adapter", return_value=mock_adapter),
        ):
            result = await create_starter_milestones(tmp_path)

        assert result["status"] == "ok"
        assert "Phase 1: Now" in result["skipped"]
        assert len(result["created"]) == 3  # Phase 2, 3, 4
        # Verify descriptions are passed to create_milestone
        for call in mock_adapter.create_milestone.call_args_list:
            assert "description" in call.kwargs
            assert call.kwargs["description"]  # non-empty

    async def test_handles_partial_failure(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.setup_service import create_starter_milestones

        mock_adapter = AsyncMock()
        mock_adapter.list_milestones.return_value = []
        call_count = 0

        async def side_effect(title: str, **_kwargs: object) -> object:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("API error")
            from sova.adapters.base import Milestone

            return Milestone(title=title, state="open")

        mock_adapter.create_milestone.side_effect = side_effect

        with (
            patch("sova.config.loader.load_config"),
            patch("sova.adapters.create_adapter", return_value=mock_adapter),
        ):
            result = await create_starter_milestones(tmp_path)

        assert result["status"] == "ok"
        assert len(result["created"]) == 3
        assert len(result["failed"]) == 1

    async def test_returns_error_when_no_config(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.setup_service import create_starter_milestones

        with patch(
            "sova.config.loader.load_config",
            side_effect=FileNotFoundError("sova.toml not found"),
        ):
            result = await create_starter_milestones(tmp_path)

        assert result["status"] == "error"
        assert "config" in result["detail"].lower()

    async def test_permission_error_captured(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.setup_service import create_starter_milestones

        mock_adapter = AsyncMock()
        mock_adapter.list_milestones.return_value = []
        mock_adapter.create_milestone.side_effect = PermissionError("Insufficient permissions")

        with (
            patch("sova.config.loader.load_config"),
            patch("sova.adapters.create_adapter", return_value=mock_adapter),
        ):
            result = await create_starter_milestones(tmp_path)

        assert result["status"] == "ok"
        assert len(result["failed"]) == 4
        assert "permissions" in result["failed"][0]["error"].lower()


# ---------------------------------------------------------------------------
# _read_existing_toml
# ---------------------------------------------------------------------------


class TestReadExistingToml:
    def test_returns_empty_when_no_file(self, tmp_path: Path) -> None:
        from sova.dashboard.services.setup_service import _read_existing_toml

        assert _read_existing_toml(tmp_path) == {}

    def test_reads_flat_and_nested_keys(self, tmp_path: Path) -> None:
        from sova.dashboard.services.setup_service import _read_existing_toml

        (tmp_path / "sova.toml").write_text('github_repo = "owner/repo"\n\n[task_source]\ntype = "github"\n')
        result = _read_existing_toml(tmp_path)
        assert result["github_repo"] == "owner/repo"
        assert result["task_source.type"] == "github"

    def test_returns_empty_on_invalid_toml(self, tmp_path: Path) -> None:
        from sova.dashboard.services.setup_service import _read_existing_toml

        (tmp_path / "sova.toml").write_text("invalid toml {{{{")
        assert _read_existing_toml(tmp_path) == {}


# ---------------------------------------------------------------------------
# create_starter_milestones -- adapter error paths
# ---------------------------------------------------------------------------


class TestCreateStarterMilestonesAdapterError:
    async def test_returns_error_when_adapter_fails(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.setup_service import create_starter_milestones

        with (
            patch("sova.config.loader.load_config"),
            patch("sova.adapters.create_adapter", side_effect=ValueError("missing repo")),
        ):
            result = await create_starter_milestones(tmp_path)

        assert result["status"] == "error"

    async def test_returns_error_when_list_milestones_fails(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.setup_service import create_starter_milestones

        mock_adapter = AsyncMock()
        mock_adapter.list_milestones.side_effect = RuntimeError("network error")

        with (
            patch("sova.config.loader.load_config"),
            patch("sova.adapters.create_adapter", return_value=mock_adapter),
        ):
            result = await create_starter_milestones(tmp_path)

        assert result["status"] == "error"
        assert "milestones" in result["detail"].lower()


# ---------------------------------------------------------------------------
# Milestones endpoint
# ---------------------------------------------------------------------------


class TestMilestonesEndpoint:
    async def test_create_milestones_success(self, client) -> None:
        from unittest.mock import AsyncMock, patch

        with patch(
            "sova.dashboard.services.setup_service.create_starter_milestones",
            new_callable=AsyncMock,
            return_value={"status": "ok", "created": ["Phase 1: Now"], "skipped": [], "failed": []},
        ):
            resp = await client.post(
                "/api/setup/milestones/create",
                json={"project_path": "/tmp"},
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


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
        assert len(actions) == 1
        assert actions[0]["id"] == "integrate"

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
        assert len(actions) == 1
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

    def test_returns_passed_for_completed_non_failure_non_success(self) -> None:
        """Completed checks with non-failure, non-success conclusion fall through to 'passed'."""
        from sova.dashboard.services.agent_recovery import _summarize_ci_checks
        from sova.git.operations import CheckConclusion, CheckStatus, CICheck

        checks = [
            CICheck(name="lint", status=CheckStatus.COMPLETED, conclusion=CheckConclusion.NEUTRAL, details_url=""),
        ]
        assert _summarize_ci_checks(checks) == "passed"


# ---------------------------------------------------------------------------
# _load_repo_config
# ---------------------------------------------------------------------------


class TestLoadRepoConfig:
    def test_load_repo_config_no_project_dir(self) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.agent_recovery import _load_repo_config

        with patch("sova.dashboard.project_context.get_project_dir", return_value=None):
            assert _load_repo_config() is None

    def test_load_repo_config_exception(self) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.agent_recovery import _load_repo_config

        with (
            patch("sova.dashboard.project_context.get_project_dir", return_value=Path("/tmp")),
            patch(
                "sova.config.loader.load_config",
                side_effect=RuntimeError("bad config"),
            ),
        ):
            assert _load_repo_config() is None

    def test_load_repo_config_no_github_repo(self) -> None:
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services.agent_recovery import _load_repo_config

        mock_cfg = MagicMock()
        mock_cfg.github_repo = ""
        with (
            patch("sova.dashboard.project_context.get_project_dir", return_value=Path("/tmp")),
            patch(
                "sova.config.loader.load_config",
                return_value=mock_cfg,
            ),
        ):
            assert _load_repo_config() is None


# ---------------------------------------------------------------------------
# get_pr_status_for_issue exception paths
# ---------------------------------------------------------------------------


class TestGetPrStatusForIssue:
    async def test_pr_status_fetch_exception(self) -> None:
        """get_pr_status_for_issue returns error dict when get_pr_status raises."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_recovery import get_pr_status_for_issue

        mock_pr = MagicMock(number=10)
        with (
            patch(
                "sova.dashboard.services.agent_recovery._load_repo_config",
                return_value=("owner/repo", "user"),
            ),
            patch(
                "sova.git.operations.find_pr_for_issue",
                new_callable=AsyncMock,
                return_value=mock_pr,
            ),
            patch(
                "sova.git.operations.get_pr_status",
                new_callable=AsyncMock,
                side_effect=RuntimeError("api error"),
            ),
        ):
            result = await get_pr_status_for_issue("42")
        assert result["has_pr"] is True
        assert result["pr_number"] == 10
        assert "error" in result

    async def test_ci_checks_fetch_exception(self) -> None:
        """get_pr_status_for_issue handles CI check failures gracefully."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_recovery import get_pr_status_for_issue

        mock_pr = MagicMock(number=10)
        mock_status = MagicMock(
            number=10,
            state="OPEN",
            review_decision=None,
            mergeable=True,
            title="test",
            url="http://x",
            is_approved=False,
            is_mergeable=True,
        )
        with (
            patch(
                "sova.dashboard.services.agent_recovery._load_repo_config",
                return_value=("owner/repo", "user"),
            ),
            patch(
                "sova.git.operations.find_pr_for_issue",
                new_callable=AsyncMock,
                return_value=mock_pr,
            ),
            patch(
                "sova.git.operations.get_pr_status",
                new_callable=AsyncMock,
                return_value=mock_status,
            ),
            patch(
                "sova.git.operations.get_ci_checks",
                new_callable=AsyncMock,
                side_effect=RuntimeError("ci api error"),
            ),
            patch(
                "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
                new_callable=AsyncMock,
                return_value={"has_sova_review": False, "verdict": None, "finding_count": 0, "reviewed_at": None},
            ),
        ):
            result = await get_pr_status_for_issue("42")
        assert result["has_pr"] is True
        assert result["ci_status"] == "unknown"


# ---------------------------------------------------------------------------
# _check_ttl_cache / _check_issue_cache
# ---------------------------------------------------------------------------


class TestTTLCache:
    @pytest.fixture(autouse=True)
    def _clear_caches(self):
        from sova.dashboard.services import agent_recovery

        agent_recovery._issue_pr_cache.clear()
        agent_recovery._synthesis_cache.clear()
        yield
        agent_recovery._issue_pr_cache.clear()
        agent_recovery._synthesis_cache.clear()

    def test_issue_cache_miss(self) -> None:
        from sova.dashboard.services import agent_recovery

        resolved, pr, result = agent_recovery._check_issue_cache("99")
        assert not resolved
        assert pr is None
        assert result is None

    def test_issue_cache_sentinel_no_pr(self) -> None:
        from sova.dashboard.services import agent_recovery

        agent_recovery._issue_pr_cache["99"] = agent_recovery._SENTINEL_NO_PR
        resolved, pr, result = agent_recovery._check_issue_cache("99")
        assert resolved
        assert pr is None
        assert result is None

    def test_issue_cache_pr_known_synthesis_cached(self) -> None:
        from sova.dashboard.services import agent_recovery

        agent_recovery._issue_pr_cache["99"] = 42
        agent_recovery._synthesis_cache[("99", 42)] = [{"id": "test"}]
        resolved, pr, result = agent_recovery._check_issue_cache("99")
        assert resolved
        assert pr == 42
        assert result == [{"id": "test"}]

    def test_issue_cache_pr_known_synthesis_not_cached(self) -> None:
        from sova.dashboard.services import agent_recovery

        agent_recovery._issue_pr_cache["99"] = 42
        resolved, pr, result = agent_recovery._check_issue_cache("99")
        assert not resolved
        assert pr == 42
        assert result is None

    def test_issue_cache_pr_none_value(self) -> None:
        """When cached_pr is None (not sentinel), return miss."""
        from sova.dashboard.services import agent_recovery

        agent_recovery._issue_pr_cache["99"] = None
        resolved, pr, result = agent_recovery._check_issue_cache("99")
        assert not resolved
        assert pr is None
        assert result is None

    def test_ttl_expiry(self) -> None:
        """TTLCache entries expire after the configured TTL."""
        from cachetools import TTLCache

        short_ttl_cache: TTLCache[str, int] = TTLCache(maxsize=256, ttl=0.1)
        short_ttl_cache["key"] = 42
        assert short_ttl_cache["key"] == 42

        import time

        time.sleep(0.15)
        with pytest.raises(KeyError):
            _ = short_ttl_cache["key"]

    def test_lru_eviction_on_maxsize(self) -> None:
        """Oldest entry is evicted when maxsize is exceeded."""
        from cachetools import TTLCache

        cache: TTLCache[int, str] = TTLCache(maxsize=3, ttl=60)
        cache[1] = "a"
        cache[2] = "b"
        cache[3] = "c"
        assert len(cache) == 3

        cache[4] = "d"
        assert len(cache) == 3
        assert 1 not in cache  # LRU entry evicted
        assert cache[4] == "d"


# ---------------------------------------------------------------------------
# _prune_completed
# ---------------------------------------------------------------------------


class TestPruneCompleted:
    def test_prune_completed_default_now(self) -> None:
        """_prune_completed uses time.monotonic() when now=None."""
        import time

        from sova.dashboard.services.agent_pool import (
            CompletedAgent,
            ProjectAgents,
            _prune_completed,
        )

        pa = ProjectAgents()
        pa.recently_completed.append(
            CompletedAgent(
                run_id=1, issue="1", role="dev", status="done", cost=0.5, completed_at=time.monotonic() - 9999
            ),
        )
        _prune_completed(pa)
        assert len(pa.recently_completed) == 0

    def test_prune_completed_removes_expired(self) -> None:
        """_prune_completed poplefts expired entries."""
        from sova.dashboard.services.agent_pool import (
            RECENTLY_COMPLETED_TTL,
            CompletedAgent,
            ProjectAgents,
            _prune_completed,
        )

        now = 10000.0
        pa = ProjectAgents()
        pa.recently_completed.append(
            CompletedAgent(
                run_id=1, issue="1", role="dev", status="done", cost=0.5, completed_at=now - RECENTLY_COMPLETED_TTL - 1
            ),
        )
        pa.recently_completed.append(
            CompletedAgent(run_id=2, issue="2", role="dev", status="done", cost=0.5, completed_at=now - 1),
        )
        _prune_completed(pa, now=now)
        assert len(pa.recently_completed) == 1
        assert pa.recently_completed[0].run_id == 2


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
# Spec action execution via handoff endpoint
# ---------------------------------------------------------------------------


class TestExecuteSpecActions:
    """Test that synthesized spec actions (approve/reject) are executable."""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        from sova.dashboard.services import handoff_service

        monkeypatch.setattr(handoff_service, "_resolve_project_dir", lambda: tmp_path)
        handoff_service._handoff_caches.clear()

    async def test_approve_spec_calls_resume_from_approval(self, client: AsyncClient, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        monkeypatch.setattr(
            "sova.dashboard.routers.handoff.get_synthesized_handoff",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            "sova.dashboard.routers.handoff._find_awaiting_approval_run",
            AsyncMock(return_value=42),
        )
        monkeypatch.setattr(
            "sova.dashboard.services.control_service.resume_from_approval",
            AsyncMock(return_value={"run_id": 100, "resumed_from": 42, "issue": "258", "role": "researcher"}),
        )

        resp = await client.post("/api/handoff/execute", json={"action_id": "approve-spec", "issue": "258"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "Approve Spec"
        assert data["run_id"] == 100

    async def test_reject_spec_calls_reject_spec(self, client: AsyncClient, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        monkeypatch.setattr(
            "sova.dashboard.routers.handoff.get_synthesized_handoff",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            "sova.dashboard.routers.handoff._find_awaiting_approval_run",
            AsyncMock(return_value=42),
        )
        monkeypatch.setattr(
            "sova.dashboard.services.control_service.reject_spec",
            AsyncMock(return_value={"run_id": 42, "issue": "258", "status": "rejected"}),
        )

        resp = await client.post("/api/handoff/execute", json={"action_id": "reject-spec", "issue": "258"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "Reject"
        assert data["status"] == "rejected"

    async def test_spec_action_no_awaiting_run_returns_404(self, client: AsyncClient, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        monkeypatch.setattr(
            "sova.dashboard.routers.handoff.get_synthesized_handoff",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            "sova.dashboard.routers.handoff._find_awaiting_approval_run",
            AsyncMock(return_value=None),
        )

        resp = await client.post("/api/handoff/execute", json={"action_id": "approve-spec", "issue": "258"})
        assert resp.status_code == 404

    async def test_spec_action_conflict_returns_409(self, client: AsyncClient, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        monkeypatch.setattr(
            "sova.dashboard.routers.handoff.get_synthesized_handoff",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            "sova.dashboard.routers.handoff._find_awaiting_approval_run",
            AsyncMock(return_value=42),
        )
        monkeypatch.setattr(
            "sova.dashboard.services.control_service.resume_from_approval",
            AsyncMock(return_value={"error": "conflict", "detail": "Already claimed"}),
        )

        resp = await client.post("/api/handoff/execute", json={"action_id": "approve-spec", "issue": "258"})
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# reject_spec (DB-level)
# ---------------------------------------------------------------------------


class TestRejectSpec:
    """DB-level tests for the reject_spec service function."""

    async def test_reject_success(self) -> None:
        from sova.dashboard.services.agent_lifecycle import reject_spec

        async with await get_session() as session, session.begin():
            run = TaskRun(issue_number="80", role="researcher", status="awaiting_approval")
            session.add(run)
        await session.commit()
        run_id = run.id

        result = await reject_spec(run_id)
        assert result["status"] == "rejected"
        assert result["run_id"] == run_id
        assert result["issue"] == "80"

        async with await get_session() as session, session.begin():
            updated = await session.get(TaskRun, run_id)
        assert updated.status == "rejected"

    async def test_reject_not_found(self) -> None:
        from sova.dashboard.services.agent_lifecycle import reject_spec

        result = await reject_spec(999999)
        assert result["error"] == "not_found"

    async def test_reject_wrong_status(self) -> None:
        from sova.dashboard.services.agent_lifecycle import reject_spec

        async with await get_session() as session, session.begin():
            run = TaskRun(issue_number="81", role="researcher", status="done")
            session.add(run)
        await session.commit()
        run_id = run.id

        result = await reject_spec(run_id)
        assert result["error"] == "conflict"
        assert "done" in result["detail"]


# ---------------------------------------------------------------------------
# _find_awaiting_approval_run (DB-level)
# ---------------------------------------------------------------------------


class TestFindAwaitingApprovalRun:
    """DB-level tests for the handoff router helper."""

    async def test_finds_most_recent(self) -> None:
        from sova.dashboard.routers.handoff import _find_awaiting_approval_run

        async with await get_session() as session, session.begin():
            older = TaskRun(issue_number="90", role="researcher", status="awaiting_approval")
            newer = TaskRun(issue_number="90", role="researcher", status="awaiting_approval")
            session.add_all([older, newer])
        await session.commit()

        result = await _find_awaiting_approval_run("90")
        assert result == newer.id

    async def test_returns_none_when_no_match(self) -> None:
        from sova.dashboard.routers.handoff import _find_awaiting_approval_run

        result = await _find_awaiting_approval_run("nonexistent")
        assert result is None

    async def test_ignores_non_awaiting_runs(self) -> None:
        from sova.dashboard.routers.handoff import _find_awaiting_approval_run

        async with await get_session() as session, session.begin():
            run = TaskRun(issue_number="91", role="researcher", status="done")
            session.add(run)
        await session.commit()

        result = await _find_awaiting_approval_run("91")
        assert result is None


# ---------------------------------------------------------------------------
# invalidate_synthesis_cache
# ---------------------------------------------------------------------------


class TestInvalidateSynthesisCache:
    def test_clears_both_caches(self) -> None:
        from sova.dashboard.services.agent_recovery import (
            _issue_pr_cache,
            _synthesis_cache,
            invalidate_synthesis_cache,
        )

        _synthesis_cache[("42", 99)] = [{"id": "test"}]
        _issue_pr_cache["42"] = 99

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
        from unittest.mock import AsyncMock

        from sova.dashboard.services import agent_recovery

        agent_recovery._issue_pr_cache["42"] = 99

        mock_adapter = AsyncMock()
        mock_adapter.get_pr_reviews.return_value = []
        monkeypatch.setattr("sova.adapters.create_adapter", lambda _: mock_adapter)

        mock_find = AsyncMock()
        monkeypatch.setattr("sova.git.operations.find_pr_for_issue", mock_find)

        result = await agent_recovery.synthesize_pr_actions("42")
        assert result is None
        mock_find.assert_not_awaited()

    async def test_returns_cached_synthesis_result(self, monkeypatch) -> None:
        from sova.dashboard.services import agent_recovery

        agent_recovery._issue_pr_cache["42"] = 99
        agent_recovery._synthesis_cache[("42", 99)] = [{"id": "cached_action"}]

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
        from unittest.mock import AsyncMock

        from sova.dashboard.services import agent_recovery
        from sova.git.pr import PRInfo

        monkeypatch.setattr(
            "sova.git.operations.find_pr_for_issue",
            AsyncMock(return_value=PRInfo(number=99, url="https://github.com/user/repo/pull/99")),
        )

        agent_recovery._synthesis_cache[("42", 99)] = [{"id": "cached"}]

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
                    "id": "integrate",
                    "label": "Integrate PR",
                    "mode": "claude-command",
                    "command": "/integrate-pr 99",
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

        resp = await client.post("/api/handoff/execute", json={"action_id": "integrate"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "Integrate PR"

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


class TestSummarizeCi:
    """Tests for _summarize_ci: CI rollup aggregation."""

    def test_none_rollup_returns_none(self) -> None:
        from sova.dashboard.services.pr_service import _summarize_ci

        assert _summarize_ci(None) == "none"

    def test_empty_rollup_returns_none(self) -> None:
        from sova.dashboard.services.pr_service import _summarize_ci

        assert _summarize_ci([]) == "none"

    def test_all_success_returns_passed(self) -> None:
        from sova.dashboard.services.pr_service import _summarize_ci

        rollup = [
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ]
        assert _summarize_ci(rollup) == "passed"

    def test_cancelled_plus_success_returns_passed(self) -> None:
        """CANCELLED runs from a superseded push must not poison a later successful run."""
        from sova.dashboard.services.pr_service import _summarize_ci

        rollup = [
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "CANCELLED"},
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "CANCELLED"},
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ]
        assert _summarize_ci(rollup) == "passed"

    def test_cancelled_alone_returns_passed(self) -> None:
        """CANCELLED without SUCCESS returns passed (via skipped-only path), not failed."""
        from sova.dashboard.services.pr_service import _summarize_ci

        rollup = [
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "CANCELLED"},
        ]
        assert _summarize_ci(rollup) == "passed"

    def test_failure_plus_success_returns_failed(self) -> None:
        from sova.dashboard.services.pr_service import _summarize_ci

        rollup = [
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"},
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ]
        assert _summarize_ci(rollup) == "failed"

    def test_cancelled_plus_failure_returns_failed(self) -> None:
        """A real failure dominates over cancelled runs."""
        from sova.dashboard.services.pr_service import _summarize_ci

        rollup = [
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "CANCELLED"},
            {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"},
        ]
        assert _summarize_ci(rollup) == "failed"

    def test_pending_check_returns_pending(self) -> None:
        from sova.dashboard.services.pr_service import _summarize_ci

        rollup = [
            {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": None},
        ]
        assert _summarize_ci(rollup) == "pending"

    def test_status_context_success(self) -> None:
        from sova.dashboard.services.pr_service import _summarize_ci

        rollup = [{"__typename": "StatusContext", "state": "SUCCESS"}]
        assert _summarize_ci(rollup) == "passed"

    def test_status_context_failure(self) -> None:
        from sova.dashboard.services.pr_service import _summarize_ci

        rollup = [{"__typename": "StatusContext", "state": "FAILURE"}]
        assert _summarize_ci(rollup) == "failed"


class TestExtractLinkedIssue:
    """Tests for _extract_linked_issue PR-vs-issue disambiguation."""

    def test_prefers_closing_issues_references(self) -> None:
        from sova.dashboard.services.pr_service import _extract_linked_issue

        raw = {"closingIssuesReferences": [{"number": 42}], "body": "Closes #99"}
        assert _extract_linked_issue(raw) == 42

    def test_falls_back_to_body_parsing(self) -> None:
        from sova.dashboard.services.pr_service import _extract_linked_issue

        raw = {"closingIssuesReferences": [], "body": "Fixes #55"}
        assert _extract_linked_issue(raw) == 55

    def test_returns_none_when_no_link(self) -> None:
        from sova.dashboard.services.pr_service import _extract_linked_issue

        raw = {"closingIssuesReferences": [], "body": "No issue link"}
        assert _extract_linked_issue(raw) is None

    def test_handles_missing_fields(self) -> None:
        from sova.dashboard.services.pr_service import _extract_linked_issue

        assert _extract_linked_issue({}) is None


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
        monkeypatch.setattr("sova.dashboard.services.agent_context.run_shell", mock_result)
        monkeypatch.setattr("sova.dashboard.services.agent_context._is_issue", AsyncMock(return_value=True))

        result = await _resolve_issue_from_pr(99, tmp_path)
        assert result == "42"

    @pytest.mark.asyncio
    async def test_extracts_fixes_issue(self, tmp_path, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_lifecycle import _resolve_issue_from_pr

        mock_result = AsyncMock()
        mock_result.return_value.success = True
        mock_result.return_value.stdout = "Fixes #123"
        monkeypatch.setattr("sova.dashboard.services.agent_context.run_shell", mock_result)
        monkeypatch.setattr("sova.dashboard.services.agent_context._is_issue", AsyncMock(return_value=True))

        result = await _resolve_issue_from_pr(99, tmp_path)
        assert result == "123"

    @pytest.mark.asyncio
    async def test_extracts_resolves_case_insensitive(self, tmp_path, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_lifecycle import _resolve_issue_from_pr

        mock_result = AsyncMock()
        mock_result.return_value.success = True
        mock_result.return_value.stdout = "resolves #77"
        monkeypatch.setattr("sova.dashboard.services.agent_context.run_shell", mock_result)
        monkeypatch.setattr("sova.dashboard.services.agent_context._is_issue", AsyncMock(return_value=True))

        result = await _resolve_issue_from_pr(99, tmp_path)
        assert result == "77"

    @pytest.mark.asyncio
    async def test_skips_pr_reference(self, tmp_path, monkeypatch) -> None:
        """When 'Closes #N' references a PR (not an issue), return empty."""
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_lifecycle import _resolve_issue_from_pr

        mock_result = AsyncMock()
        mock_result.return_value.success = True
        mock_result.return_value.stdout = "Closes #339"
        monkeypatch.setattr("sova.dashboard.services.agent_context.run_shell", mock_result)
        monkeypatch.setattr("sova.dashboard.services.agent_context._is_issue", AsyncMock(return_value=False))

        result = await _resolve_issue_from_pr(341, tmp_path)
        assert result == ""

    @pytest.mark.asyncio
    async def test_skips_pr_ref_returns_second_valid_issue(self, tmp_path, monkeypatch) -> None:
        """When the first reference is a PR, evaluate subsequent matches and return the first valid issue."""
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_lifecycle import _resolve_issue_from_pr

        mock_result = AsyncMock()
        mock_result.return_value.success = True
        mock_result.return_value.stdout = "Closes #339 and Fixes #42"
        monkeypatch.setattr("sova.dashboard.services.agent_context.run_shell", mock_result)
        is_issue = AsyncMock(side_effect=lambda n, _d: n == "42")
        monkeypatch.setattr("sova.dashboard.services.agent_context._is_issue", is_issue)

        result = await _resolve_issue_from_pr(341, tmp_path)
        assert result == "42"

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_match(self, tmp_path, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_lifecycle import _resolve_issue_from_pr

        mock_result = AsyncMock()
        mock_result.return_value.success = True
        mock_result.return_value.stdout = "Just a regular PR body"
        monkeypatch.setattr("sova.dashboard.services.agent_context.run_shell", mock_result)

        result = await _resolve_issue_from_pr(99, tmp_path)
        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_empty_on_failure(self, tmp_path, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_lifecycle import _resolve_issue_from_pr

        mock_result = AsyncMock()
        mock_result.return_value.success = False
        mock_result.return_value.stdout = ""
        monkeypatch.setattr("sova.dashboard.services.agent_context.run_shell", mock_result)

        result = await _resolve_issue_from_pr(99, tmp_path)
        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_empty_on_exception(self, tmp_path, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_lifecycle import _resolve_issue_from_pr

        monkeypatch.setattr(
            "sova.dashboard.services.agent_context.run_shell",
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

    def test_detect_variant_command_approve_merge(self) -> None:
        from sova.dashboard.services.work_service import _detect_variant

        assert _detect_variant("running", role="command:approve-merge") == "command"

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

    def test_websocket_sequential_clients(self) -> None:
        """Sequential clients can each connect and receive updates."""
        from starlette.testclient import TestClient

        from sova.dashboard.app import create_app

        app = create_app(multi_project=False)
        # Use separate TestClient instances to avoid threading deadlock
        # when nesting websocket_connect context managers on the same client.
        client1 = TestClient(app)
        client2 = TestClient(app)
        with client1.websocket_connect("/api/ws/agents/status") as ws1:
            d1 = ws1.receive_json()
            assert d1["type"] == "status_update"
        with client2.websocket_connect("/api/ws/agents/status") as ws2:
            d2 = ws2.receive_json()
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


# ---------------------------------------------------------------------------
# Installation status / sync API
# ---------------------------------------------------------------------------


class TestInstallationAPI:
    @pytest.fixture
    async def install_client(self, tmp_path, monkeypatch):
        """Dashboard client with a project that has commands and guidelines installed."""
        from sova.commands.manifest import create_manifest

        project_dir = tmp_path / "myproject"
        project_dir.mkdir()
        (project_dir / ".claude").mkdir()

        # Create sova.toml
        (project_dir / "sova.toml").write_text(
            'github_repo = "owner/myapp"\ngithub_user = "owner"\ntest_cmd = "pytest"\nlint_cmd = "ruff"\n'
        )

        commands_dir = project_dir / ".claude" / "commands"
        commands_dir.mkdir(parents=True, exist_ok=True)
        rules_dir = project_dir / ".claude" / "rules"
        rules_dir.mkdir(parents=True, exist_ok=True)

        # Install a few fake commands with manifest
        (commands_dir / "develop.md").write_text("# Develop\n")
        create_manifest(commands_dir, {"develop.md": "abc123"})

        # Install a guideline with manifest
        (rules_dir / "security.md").write_text("# Security\n")
        create_manifest(rules_dir, {"security.md": "def456"})

        # Patch get_project_dir so the settings router resolves to our tmp project
        monkeypatch.setattr(
            "sova.dashboard.routers.settings.get_project_dir",
            lambda: project_dir,
        )
        monkeypatch.setattr(
            "sova.dashboard.routers.setup.get_project_dir",
            lambda: project_dir,
        )

        from sova.dashboard.app import create_app

        app = create_app(project_dir=project_dir)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    async def test_installation_status_returns_200(self, install_client: AsyncClient) -> None:
        resp = await install_client.get("/api/settings/installation/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "commands" in data
        assert "guidelines" in data
        assert "total_updates" in data
        assert "has_updates" in data
        assert isinstance(data["commands"]["changed"], list)
        assert isinstance(data["commands"]["new"], list)
        assert isinstance(data["commands"]["removed"], list)
        assert isinstance(data["guidelines"]["changed"], list)

    async def test_sync_returns_structured_and_flat(self, install_client: AsyncClient) -> None:
        resp = await install_client.post("/api/setup/commands/sync")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        # Backward-compatible flat fields
        assert "updated" in data
        assert "skipped" in data
        assert "conflicts" in data
        # Structured per-category fields
        assert "commands" in data
        assert "guidelines" in data
        assert "updated" in data["commands"]
        assert "updated" in data["guidelines"]


# ---------------------------------------------------------------------------
# Output reader resilience
# ---------------------------------------------------------------------------


class TestReadOutputResilience:
    """_read_output and _read_stderr must log and surface errors instead of dying silently."""

    async def test_read_output_logs_exception_and_writes_error_line(self) -> None:
        """When stdout_lines raises, the error should be logged and written to the output file."""
        from collections import deque
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_output import _read_output
        from sova.dashboard.services.agent_pool import AgentState

        mock_process = AsyncMock()

        async def exploding_lines():
            yield '{"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]}}'
            raise RuntimeError("pipe broken")

        mock_process.stdout_lines = exploding_lines

        mock_writer = MagicMock()
        mock_writer.should_flush.return_value = False
        mock_writer.flush = AsyncMock()
        agent = AgentState(
            run_id=1,
            issue="10",
            role="developer",
            process=mock_process,
            output_writer=mock_writer,
            output_lines=deque(maxlen=5000),
        )

        with patch("sova.dashboard.services.agent_output.log") as mock_log:
            await _read_output(agent)

        assert len(agent.output_lines) >= 1
        assert "hello" in agent.output_lines[0]
        assert any("output reader crashed" in line.lower() for line in agent.output_lines)
        mock_log.exception.assert_called_once()
        error_writes = [call for call in mock_writer.write_line.call_args_list if "output reader" in str(call).lower()]
        assert len(error_writes) == 1

    async def test_read_stderr_logs_exception(self) -> None:
        """When stderr_lines raises, the error should be logged."""
        from collections import deque
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_output import _read_stderr
        from sova.dashboard.services.agent_pool import AgentState

        mock_process = AsyncMock()

        async def exploding_stderr():
            yield "some warning"
            raise RuntimeError("stderr pipe broken")

        mock_process.stderr_lines = exploding_stderr

        mock_writer = MagicMock()
        mock_writer.should_flush.return_value = False
        mock_writer.flush = AsyncMock()
        agent = AgentState(
            run_id=2,
            issue="11",
            role="developer",
            process=mock_process,
            output_writer=mock_writer,
            output_lines=deque(maxlen=5000),
        )

        with patch("sova.dashboard.services.agent_output.log") as mock_log:
            await _read_stderr(agent)

        mock_log.exception.assert_called_once()

    async def test_read_output_reraises_cancelled_error(self) -> None:
        """CancelledError must still propagate (not be caught by the general handler)."""
        import asyncio
        from collections import deque
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_output import _read_output
        from sova.dashboard.services.agent_pool import AgentState

        mock_process = AsyncMock()

        async def cancelled_lines():
            raise asyncio.CancelledError()
            yield  # noqa: RET503

        mock_process.stdout_lines = cancelled_lines

        agent = AgentState(
            run_id=3,
            issue="12",
            role="developer",
            process=mock_process,
            output_lines=deque(maxlen=5000),
        )

        with pytest.raises(asyncio.CancelledError):
            await _read_output(agent)


# ---------------------------------------------------------------------------
# Finalize-before-pop ordering (race fix)
# ---------------------------------------------------------------------------


class TestWaitAndFinalizeOrdering:
    """_wait_and_finalize must finalize the DB record before removing from pa.agents."""

    async def test_agent_in_pa_agents_during_finalize(self) -> None:
        """The run_id must still be in pa.agents when _finalize_task_run is called."""
        from pathlib import Path
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.agent_pool import AgentState, ProjectAgents

        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=0)

        agent = AgentState(
            run_id=50,
            issue="100",
            role="developer",
            process=mock_process,
            project_dir=Path("/tmp/test-project"),
        )

        pa = ProjectAgents()
        pa.agents[50] = agent

        was_in_agents_during_finalize = []

        async def tracking_finalize(run_id, *, exit_code, agent):
            was_in_agents_during_finalize.append(run_id in pa.agents)

        with (
            patch.object(agent_lifecycle, "_finalize_task_run", new_callable=AsyncMock, side_effect=tracking_finalize),
            patch.object(agent_lifecycle, "_finalize_lifecycle_phase", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_handoff._process_auto_handoff", new_callable=AsyncMock),
            patch("sova.config.loader.load_config", side_effect=Exception("skip notifications")),
        ):
            await agent_lifecycle._wait_and_finalize(pa, agent)

        assert was_in_agents_during_finalize == [True], (
            "_finalize_task_run must be called while run_id is still in pa.agents"
        )
        assert 50 not in pa.agents, "run_id should be removed from pa.agents after finalization"

    async def test_finalize_before_pop_prevents_sweep_race(self) -> None:
        """Sweep should skip runs still in pa.agents, preventing the interrupted race."""
        from pathlib import Path
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.agent_pool import AgentState, ProjectAgents

        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=1)

        agent = AgentState(
            run_id=51,
            issue="101",
            role="command:integrate-pr",
            process=mock_process,
            pr_number=200,
            project_dir=Path("/tmp/test-project"),
        )

        pa = ProjectAgents()
        pa.agents[51] = agent

        finalize_call_order = []

        async def recording_finalize(run_id, *, exit_code, agent):
            finalize_call_order.append(("finalize", run_id in pa.agents))

        with (
            patch.object(
                agent_lifecycle,
                "_check_pr_merged_on_failure",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                agent_lifecycle,
                "_finalize_task_run",
                new_callable=AsyncMock,
                side_effect=recording_finalize,
            ),
            patch.object(agent_lifecycle, "_finalize_lifecycle_phase", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_handoff._process_auto_handoff", new_callable=AsyncMock),
            patch("sova.config.loader.load_config", side_effect=Exception("skip notifications")),
        ):
            await agent_lifecycle._wait_and_finalize(pa, agent)

        assert finalize_call_order[0] == ("finalize", True)


# ---------------------------------------------------------------------------
# Liveness sweep merge-role awareness
# ---------------------------------------------------------------------------


class TestLivenessSweepMergeCheck:
    """_liveness_sweep_loop should check PR merge status for merge-role runs."""

    @staticmethod
    def _patch_sweep_deps(**extra_patches):
        """Context manager patching sweep dependencies to use the test in-memory DB."""
        from contextlib import ExitStack
        from unittest.mock import patch

        async def _test_get_session(project_dir=None):  # noqa: ARG001
            return await get_session()

        stack = ExitStack()
        stack.enter_context(patch("sova.db.session.get_session", _test_get_session))
        stack.enter_context(patch("sova.dashboard.services.control_service._is_process_alive", return_value=False))
        for target, mock_val in extra_patches.items():
            stack.enter_context(patch(target, **mock_val))
        return stack

    async def test_sweep_marks_merged_pr_as_done(self) -> None:
        """Dead-PID merge-role run with merged PR should get status 'done', not 'interrupted'."""
        from unittest.mock import AsyncMock

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="42",
                    role="command:integrate-pr",
                    status="running",
                    pid=999999,
                    pr_number=100,
                    project_slug="test",
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        with self._patch_sweep_deps(
            **{
                "sova.dashboard.services.agent_lifecycle._check_pr_merged_on_failure": {
                    "new_callable": AsyncMock,
                    "return_value": True,
                },
            }
        ):
            from sova.dashboard.app import _liveness_sweep_once

            await _liveness_sweep_once(None, is_multi=False)

        async with await get_session() as session:
            refreshed = await session.get(TaskRun, run_id)
            assert refreshed.status == "done"
            assert "merged" in (refreshed.error_message or "").lower()

    async def test_sweep_marks_non_merge_role_as_interrupted(self) -> None:
        """Dead-PID non-merge-role run should still get 'interrupted'."""
        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="43",
                    role="developer",
                    status="running",
                    pid=999998,
                    project_slug="test",
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        with self._patch_sweep_deps():
            from sova.dashboard.app import _liveness_sweep_once

            await _liveness_sweep_once(None, is_multi=False)

        async with await get_session() as session:
            refreshed = await session.get(TaskRun, run_id)
            assert refreshed.status == "interrupted"

    async def test_sweep_skips_managed_runs(self) -> None:
        """Runs still in pa.agents should be skipped by the sweep."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.control_service import _projects

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="44",
                    role="developer",
                    status="running",
                    pid=999997,
                    project_slug="test",
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        from sova.dashboard.services.agent_pool import ProjectAgents

        mock_pa = ProjectAgents()
        mock_pa.agents[run_id] = MagicMock()

        original = dict(_projects)
        _projects["__default__"] = mock_pa
        try:
            with self._patch_sweep_deps():
                from sova.dashboard.app import _liveness_sweep_once

                await _liveness_sweep_once(None, is_multi=False)
        finally:
            _projects.clear()
            _projects.update(original)

        async with await get_session() as session:
            refreshed = await session.get(TaskRun, run_id)
            assert refreshed.status == "running", "managed run should not be touched by sweep"

    async def test_sweep_skips_alive_processes(self) -> None:
        """Runs whose PID is still alive should be left untouched."""
        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="45",
                    role="developer",
                    status="running",
                    pid=999996,
                    project_slug="test",
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        with self._patch_sweep_deps(
            **{
                "sova.dashboard.services.control_service._is_process_alive": {
                    "return_value": True,
                },
            }
        ):
            from sova.dashboard.app import _liveness_sweep_once

            await _liveness_sweep_once(None, is_multi=False)

        async with await get_session() as session:
            refreshed = await session.get(TaskRun, run_id)
            assert refreshed.status == "running", "alive process should not be marked interrupted"

    async def test_sweep_skips_paused_runs(self) -> None:
        """Paused runs (gate failures) must not be reclassified by the sweep."""
        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="88",
                    role="researcher",
                    status="paused",
                    pid=999993,
                    project_slug="test",
                )
                session.add(run)
                await session.flush()
                run_id = run.id

        with self._patch_sweep_deps(
            **{
                "sova.dashboard.services.control_service._is_process_alive": {
                    "return_value": False,
                },
            }
        ):
            from sova.dashboard.app import _liveness_sweep_once

            await _liveness_sweep_once(None, is_multi=False)

        async with await get_session() as session:
            refreshed = await session.get(TaskRun, run_id)
            assert refreshed.status == "paused", "paused run should not be reclassified by sweep"


class TestWaitAndFinalizeOutputWriter:
    """Cover the output_writer.close() try/except in _wait_and_finalize."""

    async def test_output_writer_close_called(self) -> None:
        """output_writer.close() should be called before finalization."""
        from pathlib import Path
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.agent_pool import AgentState, ProjectAgents

        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=0)
        mock_writer = MagicMock()
        mock_writer.close = AsyncMock()

        agent = AgentState(
            run_id=60,
            issue="200",
            role="developer",
            process=mock_process,
            output_writer=mock_writer,
            project_dir=Path("/tmp/test-project"),
        )

        pa = ProjectAgents()
        pa.agents[60] = agent

        with (
            patch.object(agent_lifecycle, "_finalize_task_run", new_callable=AsyncMock),
            patch.object(agent_lifecycle, "_finalize_lifecycle_phase", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_handoff._process_auto_handoff", new_callable=AsyncMock),
            patch("sova.config.loader.load_config", side_effect=Exception("skip")),
        ):
            await agent_lifecycle._wait_and_finalize(pa, agent)

        mock_writer.close.assert_awaited_once()

    async def test_output_writer_close_error_does_not_block_finalize(self) -> None:
        """If output_writer.close() raises, finalization must still proceed."""
        from pathlib import Path
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.agent_pool import AgentState, ProjectAgents

        mock_process = AsyncMock()
        mock_process.wait = AsyncMock(return_value=0)
        mock_writer = MagicMock()
        mock_writer.close = AsyncMock(side_effect=OSError("disk full"))

        agent = AgentState(
            run_id=61,
            issue="201",
            role="developer",
            process=mock_process,
            output_writer=mock_writer,
            project_dir=Path("/tmp/test-project"),
        )

        pa = ProjectAgents()
        pa.agents[61] = agent

        finalize_called = []

        async def track_finalize(run_id, *, exit_code, agent):
            finalize_called.append(run_id)

        with (
            patch.object(agent_lifecycle, "_finalize_task_run", new_callable=AsyncMock, side_effect=track_finalize),
            patch.object(agent_lifecycle, "_finalize_lifecycle_phase", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_handoff._process_auto_handoff", new_callable=AsyncMock),
            patch("sova.config.loader.load_config", side_effect=Exception("skip")),
        ):
            await agent_lifecycle._wait_and_finalize(pa, agent)

        assert finalize_called == [61], "finalization must run even if close() fails"
        assert 61 not in pa.agents


class TestOutputReaderWriteLineFallback:
    """Cover the write_line error path in the output reader crash handler."""

    async def test_write_line_oserror_is_caught(self) -> None:
        """When write_line raises OSError during error handling, it should be caught."""
        from collections import deque
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_output import _read_output
        from sova.dashboard.services.agent_pool import AgentState

        mock_process = AsyncMock()

        async def exploding_stdout():
            yield "line1"
            raise RuntimeError("pipe broken")

        mock_process.stdout_lines = exploding_stdout

        mock_writer = MagicMock()
        mock_writer.should_flush.return_value = False
        mock_writer.flush = AsyncMock()
        # First write_line call succeeds (for "line1"), second fails (for error message)
        mock_writer.write_line.side_effect = [None, OSError("disk full")]

        agent = AgentState(
            run_id=70,
            issue="300",
            role="developer",
            process=mock_process,
            output_writer=mock_writer,
            output_lines=deque(maxlen=5000),
        )

        with patch("sova.dashboard.services.agent_output.log") as mock_log:
            await _read_output(agent)

        # Error message should still be in the in-memory deque
        assert any("output reader crashed" in line.lower() for line in agent.output_lines)
        mock_log.exception.assert_called_once()
        mock_log.debug.assert_called_once()


class TestGetOutputBranches:
    """Cover get_output fallback branches in agent_output.py."""

    async def test_get_output_in_memory_agent(self) -> None:
        """When run_id matches an in-memory agent, return from deque."""
        from collections import deque
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services.agent_output import get_output

        mock_agent = MagicMock()
        mock_agent.output_lines = deque(["line0", "line1", "line2"])

        mock_pa = MagicMock()
        mock_pa.agents = {42: mock_agent}

        with patch("sova.dashboard.services.agent_pool._get_project_agents", return_value=mock_pa):
            lines = await get_output(since=1, run_id=42)
        assert lines == ["line1", "line2"]

    async def test_get_output_db_fallback(self, session) -> None:
        """When run_id is not in memory, fall back to DB read."""
        from pathlib import Path
        from unittest.mock import MagicMock, patch

        from sova.core.output import OutputWriter
        from sova.dashboard.services.agent_output import get_output
        from sova.db.models import TaskRun

        async with session.begin():
            session.add(TaskRun(id=200, role="developer", status="done"))

        writer = OutputWriter(Path("/tmp/fake"), run_id=200)
        writer.write_line("db line")
        await writer.close()

        mock_pa = MagicMock()
        mock_pa.agents = {}
        mock_pa.project_dir = Path("/tmp/fake")

        with patch("sova.dashboard.services.agent_pool._get_project_agents", return_value=mock_pa):
            lines = await get_output(since=0, run_id=200)
        assert lines == ["db line"]

    async def test_get_output_no_run_id_returns_first_agent(self) -> None:
        """Without run_id, return first agent's output (legacy compat)."""
        from collections import deque
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services.agent_output import get_output

        mock_agent = MagicMock()
        mock_agent.output_lines = deque(["first", "second"])

        mock_pa = MagicMock()
        mock_pa.agents = {1: mock_agent}

        with patch("sova.dashboard.services.agent_pool._get_project_agents", return_value=mock_pa):
            lines = await get_output(since=0)
        assert lines == ["first", "second"]

    async def test_get_output_no_run_id_no_agents(self) -> None:
        """Without run_id and no agents, return empty list."""
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services.agent_output import get_output

        mock_pa = MagicMock()
        mock_pa.agents = {}

        with patch("sova.dashboard.services.agent_pool._get_project_agents", return_value=mock_pa):
            lines = await get_output(since=0)
        assert lines == []


class TestReadStderrFlushTrigger:
    """Cover _read_stderr flush threshold path."""

    async def test_stderr_triggers_flush(self) -> None:
        from collections import deque
        from unittest.mock import AsyncMock, MagicMock

        from sova.dashboard.services.agent_output import _read_stderr
        from sova.dashboard.services.agent_pool import AgentState

        mock_process = AsyncMock()

        async def stderr_lines():
            yield "warning line"

        mock_process.stderr_lines = stderr_lines

        mock_writer = MagicMock()
        mock_writer.should_flush.return_value = True
        mock_writer.flush = AsyncMock()
        agent = AgentState(
            run_id=80,
            issue="400",
            role="developer",
            process=mock_process,
            output_writer=mock_writer,
            output_lines=deque(maxlen=5000),
        )

        await _read_stderr(agent)

        mock_writer.write_line.assert_called_once()
        mock_writer.flush.assert_awaited_once()
        assert any("stderr" in line for line in agent.output_lines)


# ---------------------------------------------------------------------------
# Resume from approval -- POST /api/agents/{run_id}/resume-from-approval
# ---------------------------------------------------------------------------


class TestResumeFromApproval:
    """Tests for the approval resume endpoint and service function."""

    async def test_resume_success(self) -> None:
        """Successful resume spawns a new agent with resume_run_id."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_lifecycle import resume_from_approval

        # Seed a TaskRun with awaiting_approval status
        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="50",
                    role="developer",
                    status="awaiting_approval",
                    pr_number=10,
                )
                session.add(run)
            await session.commit()
            run_id = run.id

        with patch(
            "sova.dashboard.services.agent_lifecycle.start_agent",
            new_callable=AsyncMock,
            return_value={"status": "started", "pid": 999, "run_id": run_id + 1},
        ) as mock_start:
            result = await resume_from_approval(run_id)

        assert result["run_id"] == run_id + 1
        assert result["resumed_from"] == run_id
        assert result["issue"] == "50"
        assert result["role"] == "developer"
        mock_start.assert_awaited_once()
        call_kwargs = mock_start.call_args
        assert call_kwargs.kwargs["resume_run_id"] == run_id
        assert call_kwargs.kwargs["pr_number"] == 10
        assert call_kwargs.kwargs["force"] is True

    async def test_resume_not_found(self) -> None:
        """Resuming a non-existent run returns not_found error."""
        from sova.dashboard.services.agent_lifecycle import resume_from_approval

        result = await resume_from_approval(999999)
        assert result["error"] == "not_found"

    async def test_resume_wrong_status(self) -> None:
        """Resuming a run with wrong status returns conflict error."""
        from sova.dashboard.services.agent_lifecycle import resume_from_approval

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="51",
                    role="developer",
                    status="done",
                )
                session.add(run)
            await session.commit()
            run_id = run.id

        result = await resume_from_approval(run_id)
        assert result["error"] == "conflict"
        assert "done" in result["detail"]

    async def test_resume_clears_handoff(self) -> None:
        """Successful resume clears the handoff file for the issue."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_lifecycle import resume_from_approval

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="52",
                    role="developer",
                    status="awaiting_approval",
                )
                session.add(run)
            await session.commit()
            run_id = run.id

        with (
            patch(
                "sova.dashboard.services.agent_lifecycle.start_agent",
                new_callable=AsyncMock,
                return_value={"status": "started", "pid": 111, "run_id": run_id + 1},
            ),
            patch(
                "sova.dashboard.services.handoff_service.clear_handoff",
            ) as mock_clear,
        ):
            result = await resume_from_approval(run_id)

        assert "error" not in result
        mock_clear.assert_called_once_with(issue="52")

    async def test_resume_endpoint_404(self) -> None:
        """The router endpoint returns 404 for missing run."""
        from sova.dashboard.app import create_app

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/agents/999999/resume-from-approval")
        assert resp.status_code == 404

    async def test_resume_endpoint_409(self) -> None:
        """The router endpoint returns 409 for wrong status."""
        from sova.dashboard.app import create_app

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="53",
                    role="developer",
                    status="done",
                )
                session.add(run)
            await session.commit()
            run_id = run.id

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/api/agents/{run_id}/resume-from-approval")
        assert resp.status_code == 409

    async def test_resume_spawn_failure_reverts_status(self) -> None:
        """When start_agent fails, the TaskRun status reverts to awaiting_approval."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_lifecycle import resume_from_approval

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="54",
                    role="developer",
                    status="awaiting_approval",
                    pr_number=11,
                )
                session.add(run)
            await session.commit()
            run_id = run.id

        with patch(
            "sova.dashboard.services.agent_lifecycle.start_agent",
            new_callable=AsyncMock,
            return_value={"error": "Maximum concurrent agents reached (1)", "running": 1},
        ):
            result = await resume_from_approval(run_id)

        assert "error" in result
        # Verify status was reverted so the approval button reappears
        async with await get_session() as session:
            async with session.begin():
                reverted = await session.get(TaskRun, run_id)
        assert reverted.status == "awaiting_approval"

    async def test_resume_spawn_failure_preserves_handoff(self) -> None:
        """When start_agent fails, the handoff file is NOT cleared."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_lifecycle import resume_from_approval

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="55",
                    role="developer",
                    status="awaiting_approval",
                )
                session.add(run)
            await session.commit()
            run_id = run.id

        with (
            patch(
                "sova.dashboard.services.agent_lifecycle.start_agent",
                new_callable=AsyncMock,
                return_value={"error": "spawn failed"},
            ),
            patch(
                "sova.dashboard.services.handoff_service.clear_handoff",
            ) as mock_clear,
        ):
            result = await resume_from_approval(run_id)

        assert "error" in result
        mock_clear.assert_not_called()

    async def test_resume_endpoint_500_on_spawn_error(self) -> None:
        """The router returns 500 for generic spawn errors (not 404/409)."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.app import create_app

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="56",
                    role="developer",
                    status="awaiting_approval",
                )
                session.add(run)
            await session.commit()
            run_id = run.id

        app = create_app()
        transport = ASGITransport(app=app)

        with patch(
            "sova.dashboard.services.agent_lifecycle.start_agent",
            new_callable=AsyncMock,
            return_value={"error": "spawn_failed", "detail": "Agent process died"},
        ):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(f"/api/agents/{run_id}/resume-from-approval")

        assert resp.status_code == 500
        assert "Agent process died" in resp.json()["detail"]

    async def test_resume_idempotent_double_call(self) -> None:
        """Two concurrent resume calls -- second one gets conflict due to CAS."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_lifecycle import resume_from_approval

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="57",
                    role="developer",
                    status="awaiting_approval",
                )
                session.add(run)
            await session.commit()
            run_id = run.id

        call_count = 0

        async def mock_start(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return {"status": "started", "pid": 999, "run_id": run_id + call_count}

        with patch(
            "sova.dashboard.services.agent_lifecycle.start_agent",
            new_callable=AsyncMock,
            side_effect=mock_start,
        ):
            result1 = await resume_from_approval(run_id)
            result2 = await resume_from_approval(run_id)

        # First call succeeds
        assert "error" not in result1
        # Second call gets conflict (status already changed by CAS)
        assert result2["error"] == "conflict"

    async def test_resume_start_agent_error_budget_exceeded(self) -> None:
        """When start_agent returns a budget/concurrent error, resume propagates it."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_lifecycle import resume_from_approval

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="60",
                    role="developer",
                    status="awaiting_approval",
                )
                session.add(run)
            await session.commit()
            run_id = run.id

        with patch(
            "sova.dashboard.services.agent_lifecycle.start_agent",
            new_callable=AsyncMock,
            return_value={"error": "budget_exceeded", "detail": "Run budget exceeded"},
        ):
            result = await resume_from_approval(run_id)

        assert result["error"] == "budget_exceeded"
        # Status should be reverted
        async with await get_session() as session:
            async with session.begin():
                reverted = await session.get(TaskRun, run_id)
        assert reverted.status == "awaiting_approval"

    async def test_resume_issueless_run(self) -> None:
        """Resuming a run with no issue_number still works (skips handoff clear)."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_lifecycle import resume_from_approval

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="",
                    role="developer",
                    status="awaiting_approval",
                )
                session.add(run)
            await session.commit()
            run_id = run.id

        with (
            patch(
                "sova.dashboard.services.agent_lifecycle.start_agent",
                new_callable=AsyncMock,
                return_value={"status": "started", "pid": 222, "run_id": run_id + 1},
            ),
            patch(
                "sova.dashboard.services.handoff_service.clear_handoff",
            ) as mock_clear,
        ):
            result = await resume_from_approval(run_id)

        assert "error" not in result
        assert result["run_id"] == run_id + 1
        # clear_handoff should NOT be called when issue is empty
        mock_clear.assert_not_called()

    async def test_resume_clear_handoff_exception(self) -> None:
        """When clear_handoff raises, resume still succeeds (non-fatal)."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_lifecycle import resume_from_approval

        async with await get_session() as session:
            async with session.begin():
                run = TaskRun(
                    issue_number="61",
                    role="developer",
                    status="awaiting_approval",
                )
                session.add(run)
            await session.commit()
            run_id = run.id

        with (
            patch(
                "sova.dashboard.services.agent_lifecycle.start_agent",
                new_callable=AsyncMock,
                return_value={"status": "started", "pid": 333, "run_id": run_id + 1},
            ),
            patch(
                "sova.dashboard.services.handoff_service.clear_handoff",
                side_effect=OSError("disk full"),
            ),
        ):
            result = await resume_from_approval(run_id)

        # Should succeed despite clear_handoff failure
        assert "error" not in result
        assert result["run_id"] == run_id + 1


class TestWorkItemAwaitingApprovalFallback:
    """Tests for awaiting_approval fallback in _build_task_item."""

    def test_build_task_item_awaiting_approval_no_handoff(self) -> None:
        """When last run is awaiting_approval with no handoff, state is SPEC_REVIEW with resume action."""
        from sova.dashboard.services.work_item_service import WorkItemState, _build_task_item

        task = {
            "issue": 99,
            "state": "in_progress",
            "title": "Test task",
            "url": "",
            "last_run": {"id": 1, "status": "awaiting_approval"},
        }
        item = _build_task_item(task, pr_data=None, running=None, handoff=None)
        assert item["state"] == WorkItemState.SPEC_REVIEW
        assert item["primary_action"]["handler"] == "resume_from_approval"

    def test_awaiting_approval_no_handoff_synthesizes_spec_actions(self) -> None:
        """When handoff file is missing, synthesize approve/reject actions for the UI."""
        from sova.dashboard.services.work_item_service import WorkItemState, _build_task_item

        task = {
            "issue": 258,
            "state": "in_progress",
            "title": "Spec review task",
            "url": "",
            "last_run": {"id": 572, "status": "awaiting_approval"},
        }
        item = _build_task_item(task, pr_data=None, running=None, handoff=None)
        assert item["state"] == WorkItemState.SPEC_REVIEW
        action_ids = [a["id"] for a in item["handoff_actions"]]
        assert "approve-spec" in action_ids
        assert "reject-spec" in action_ids
        approve = next(a for a in item["handoff_actions"] if a["id"] == "approve-spec")
        assert approve["args"]["issue"] == "258"

    def test_awaiting_approval_with_handoff_uses_handoff_actions(self) -> None:
        """When handoff file exists, use its actions instead of synthesized ones."""
        from sova.dashboard.services.work_item_service import WorkItemState, _build_task_item

        task = {
            "issue": 99,
            "state": "in_progress",
            "title": "Test task",
            "url": "",
            "last_run": {"id": 1, "status": "awaiting_approval"},
        }
        handoff = {"status": "awaiting_action", "next_actions": [{"id": "approve-spec", "label": "Custom Approve"}]}
        item = _build_task_item(task, pr_data=None, running=None, handoff=handoff)
        assert item["state"] == WorkItemState.SPEC_REVIEW
        assert item["handoff_actions"][0]["label"] == "Custom Approve"

    def test_build_task_item_awaiting_approval_with_handoff(self) -> None:
        """When handoff is present, handoff takes priority over last_run_status."""
        from sova.dashboard.services.work_item_service import WorkItemState, _build_task_item

        task = {
            "issue": 99,
            "state": "in_progress",
            "title": "Test task",
            "url": "",
            "last_run": {"id": 1, "status": "awaiting_approval"},
        }
        handoff = {"status": "awaiting_action", "next_actions": [{"id": "approve-spec"}]}
        item = _build_task_item(task, pr_data=None, running=None, handoff=handoff)
        assert item["state"] == WorkItemState.SPEC_REVIEW

    def test_build_task_item_no_awaiting_approval(self) -> None:
        """Without awaiting_approval, normal state computation proceeds."""
        from sova.dashboard.services.work_item_service import WorkItemState, _build_task_item

        task = {
            "issue": 99,
            "state": "in_progress",
            "title": "Test task",
            "url": "",
            "last_run": {"id": 1, "status": "done"},
        }
        item = _build_task_item(task, pr_data=None, running=None, handoff=None)
        assert item["state"] == WorkItemState.IN_PROGRESS


# ---------------------------------------------------------------------------
# Planner create-issues endpoint
# ---------------------------------------------------------------------------


class TestPlannerCreateIssues:
    async def test_create_issues_success(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        from sova.adapters.base import Task, TaskState

        mock_adapter = AsyncMock()
        mock_adapter.create_issue.return_value = Task(id="200", title="feat(cli): new cmd", state=TaskState.BACKLOG)

        with patch("sova.adapters.create_adapter", return_value=mock_adapter):
            resp = await client.post(
                "/api/agents/planner/create-issues",
                json={
                    "tasks": [
                        {"title": "feat(cli): new cmd", "description": "A new command"},
                    ]
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["created"]) == 1
        assert data["created"][0]["number"] == "200"
        assert data["errors"] == []

    async def test_create_issues_empty_list(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/agents/planner/create-issues",
            json={"tasks": []},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["created"] == []
        assert data["errors"] == []

    async def test_create_issues_partial_failure(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        from sova.adapters.base import Task, TaskState

        mock_adapter = AsyncMock()
        call_count = 0

        async def _create_issue(title: str, body: str = "", labels=None, **_kw):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("Rate limited")
            return Task(id=str(100 + call_count), title=title, state=TaskState.BACKLOG)

        mock_adapter.create_issue.side_effect = _create_issue

        with patch("sova.adapters.create_adapter", return_value=mock_adapter):
            resp = await client.post(
                "/api/agents/planner/create-issues",
                json={
                    "tasks": [
                        {"title": "Task 1", "description": "Desc 1"},
                        {"title": "Task 2", "description": "Desc 2"},
                        {"title": "Task 3", "description": "Desc 3"},
                    ]
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["created"]) == 2
        assert len(data["errors"]) == 1
        assert data["errors"][0]["title"] == "Task 2"


class TestHandoffIssueFilter:
    async def test_handoff_filter_by_issue(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        handoffs = [
            {
                "source": "planner",
                "status": "awaiting_action",
                "issue": "planner",
                "summary": "Proposed 3 tasks",
                "details": {"planned_tasks": []},
                "next_actions": [],
            },
            {
                "source": "developer",
                "status": "awaiting_action",
                "issue": "42",
                "summary": "PR ready",
                "next_actions": [],
            },
        ]

        with patch("sova.dashboard.services.handoff_service.get_all_handoffs", return_value=handoffs):
            resp = await client.get("/api/handoff?issue=planner")

        assert resp.status_code == 200
        data = resp.json()
        assert data["has_handoff"] is True
        assert len(data["handoffs"]) == 1
        assert data["handoff"]["issue"] == "planner"

    async def test_handoff_filter_no_match(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        handoffs = [
            {
                "source": "developer",
                "status": "awaiting_action",
                "issue": "42",
                "summary": "PR ready",
                "next_actions": [],
            },
        ]

        with patch("sova.dashboard.services.handoff_service.get_all_handoffs", return_value=handoffs):
            resp = await client.get("/api/handoff?issue=planner")

        assert resp.status_code == 200
        data = resp.json()
        assert data["has_handoff"] is False
        assert data["handoffs"] == []


# ---------------------------------------------------------------------------
# check_memory_pressure
# ---------------------------------------------------------------------------


class TestCheckMemoryPressure:
    def test_returns_block_when_below_block_threshold(self) -> None:
        from unittest.mock import MagicMock, patch

        from sova.config.models import MemoryGuardConfig, ProjectConfig
        from sova.dashboard.services.agent_validation import check_memory_pressure

        cfg = ProjectConfig(memory_guard=MemoryGuardConfig(enabled=True, warn_threshold_gb=4.0, block_threshold_gb=2.0))
        mock_mem = MagicMock()
        mock_mem.available = int(1.0 * 1024**3)  # 1.0 GB, below block_threshold_gb=2.0

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("psutil.virtual_memory", return_value=mock_mem),
        ):
            block, warn = check_memory_pressure(Path("/tmp/test"))

        assert block is not None
        assert "Insufficient memory" in block["error"]
        assert block["available_gb"] == 1.0
        assert warn is None

    def test_returns_warning_when_below_warn_threshold(self) -> None:
        from unittest.mock import MagicMock, patch

        from sova.config.models import MemoryGuardConfig, ProjectConfig
        from sova.dashboard.services.agent_validation import check_memory_pressure

        cfg = ProjectConfig(memory_guard=MemoryGuardConfig(enabled=True, warn_threshold_gb=4.0, block_threshold_gb=2.0))
        mock_mem = MagicMock()
        mock_mem.available = int(3.0 * 1024**3)  # 3.0 GB, below warn=4.0 but above block=2.0

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("psutil.virtual_memory", return_value=mock_mem),
        ):
            block, warn = check_memory_pressure(Path("/tmp/test"))

        assert block is None
        assert warn is not None
        assert "Low memory" in warn

    def test_returns_none_when_sufficient(self) -> None:
        from unittest.mock import MagicMock, patch

        from sova.config.models import MemoryGuardConfig, ProjectConfig
        from sova.dashboard.services.agent_validation import check_memory_pressure

        cfg = ProjectConfig(memory_guard=MemoryGuardConfig(enabled=True, warn_threshold_gb=4.0, block_threshold_gb=2.0))
        mock_mem = MagicMock()
        mock_mem.available = int(8.0 * 1024**3)

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("psutil.virtual_memory", return_value=mock_mem),
        ):
            block, warn = check_memory_pressure(Path("/tmp/test"))

        assert block is None
        assert warn is None

    def test_returns_none_when_disabled(self) -> None:
        from unittest.mock import patch

        from sova.config.models import MemoryGuardConfig, ProjectConfig
        from sova.dashboard.services.agent_validation import check_memory_pressure

        cfg = ProjectConfig(
            memory_guard=MemoryGuardConfig(enabled=False, warn_threshold_gb=4.0, block_threshold_gb=2.0)
        )

        with patch("sova.config.loader.load_config", return_value=cfg):
            block, warn = check_memory_pressure(Path("/tmp/test"))

        assert block is None
        assert warn is None

    def test_fail_open_on_exception(self) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.agent_validation import check_memory_pressure

        with patch("sova.config.loader.load_config", side_effect=RuntimeError("boom")):
            block, warn = check_memory_pressure(Path("/tmp/test"))

        assert block is None
        assert warn is None


# ---------------------------------------------------------------------------
# _get_memory_guard_config and get_system_metrics memory pressure fields
# ---------------------------------------------------------------------------


class TestResourceServiceMemoryPressure:
    def test_get_memory_guard_config_returns_config(self) -> None:
        from unittest.mock import patch

        import sova.dashboard.services.resource_service as rs
        from sova.config.models import MemoryGuardConfig, ProjectConfig

        cfg = ProjectConfig(memory_guard=MemoryGuardConfig(enabled=True, warn_threshold_gb=5.0, block_threshold_gb=2.0))

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("sova.dashboard.project_context.get_project_dir", return_value=Path("/tmp")),
        ):
            result = rs._get_memory_guard_config()

        assert result is not None
        assert result.warn_threshold_gb == 5.0
        assert result.block_threshold_gb == 2.0

    def test_get_memory_guard_config_returns_none_on_error(self) -> None:
        from unittest.mock import patch

        import sova.dashboard.services.resource_service as rs

        with patch("sova.config.loader.load_config", side_effect=RuntimeError("no config")):
            result = rs._get_memory_guard_config()

        assert result is None

    def test_system_metrics_includes_memory_pressure_critical(self) -> None:
        from unittest.mock import MagicMock, patch

        import sova.dashboard.services.resource_service as rs
        from sova.config.models import MemoryGuardConfig

        guard = MemoryGuardConfig(enabled=True, warn_threshold_gb=4.0, block_threshold_gb=2.0)

        mock_mem = MagicMock()
        mock_mem.available = int(1.0 * 1024**3)  # 1.0 GB
        mock_mem.total = int(16.0 * 1024**3)
        mock_mem.percent = 93.75

        with (
            patch("psutil.virtual_memory", return_value=mock_mem),
            patch("psutil.cpu_percent", return_value=50.0),
            patch("psutil.cpu_count", return_value=8),
            patch.object(rs, "_get_memory_guard_config", return_value=guard),
            patch.object(rs, "_get_project_agents", return_value=MagicMock(agents={}, max_concurrent=3)),
        ):
            result = rs.get_system_metrics()

        assert result["available"] is True
        assert result["system"]["memory_pressure"] == "critical"
        assert result["system"]["memory_available_gb"] == 1.0

    def test_system_metrics_includes_memory_pressure_warning(self) -> None:
        from unittest.mock import MagicMock, patch

        import sova.dashboard.services.resource_service as rs
        from sova.config.models import MemoryGuardConfig

        guard = MemoryGuardConfig(enabled=True, warn_threshold_gb=4.0, block_threshold_gb=2.0)

        mock_mem = MagicMock()
        mock_mem.available = int(3.0 * 1024**3)  # 3.0 GB (between block=2 and warn=4)
        mock_mem.total = int(16.0 * 1024**3)
        mock_mem.percent = 81.25

        with (
            patch("psutil.virtual_memory", return_value=mock_mem),
            patch("psutil.cpu_percent", return_value=50.0),
            patch("psutil.cpu_count", return_value=8),
            patch.object(rs, "_get_memory_guard_config", return_value=guard),
            patch.object(rs, "_get_project_agents", return_value=MagicMock(agents={}, max_concurrent=3)),
        ):
            result = rs.get_system_metrics()

        assert result["system"]["memory_pressure"] == "warning"

    def test_system_metrics_pressure_ok_when_guard_disabled(self) -> None:
        from unittest.mock import MagicMock, patch

        import sova.dashboard.services.resource_service as rs
        from sova.config.models import MemoryGuardConfig

        guard = MemoryGuardConfig(enabled=False, warn_threshold_gb=4.0, block_threshold_gb=2.0)

        mock_mem = MagicMock()
        mock_mem.available = int(1.0 * 1024**3)  # low but guard disabled
        mock_mem.total = int(16.0 * 1024**3)
        mock_mem.percent = 93.75

        with (
            patch("psutil.virtual_memory", return_value=mock_mem),
            patch("psutil.cpu_percent", return_value=50.0),
            patch("psutil.cpu_count", return_value=8),
            patch.object(rs, "_get_memory_guard_config", return_value=guard),
            patch.object(rs, "_get_project_agents", return_value=MagicMock(agents={}, max_concurrent=3)),
        ):
            result = rs.get_system_metrics()

        assert result["system"]["memory_pressure"] == "ok"

    def test_system_metrics_pressure_ok_when_guard_none(self) -> None:
        from unittest.mock import MagicMock, patch

        import sova.dashboard.services.resource_service as rs

        mock_mem = MagicMock()
        mock_mem.available = int(1.0 * 1024**3)
        mock_mem.total = int(16.0 * 1024**3)
        mock_mem.percent = 93.75

        with (
            patch("psutil.virtual_memory", return_value=mock_mem),
            patch("psutil.cpu_percent", return_value=50.0),
            patch("psutil.cpu_count", return_value=8),
            patch.object(rs, "_get_memory_guard_config", return_value=None),
            patch.object(rs, "_get_project_agents", return_value=MagicMock(agents={}, max_concurrent=3)),
        ):
            result = rs.get_system_metrics()

        assert result["system"]["memory_pressure"] == "ok"


# ---------------------------------------------------------------------------
# Auto-handoff memory pressure gate
# ---------------------------------------------------------------------------


class TestAutoHandoffMemoryGate:
    async def test_memory_block_stops_auto_handoff(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        agent = type(
            "AgentState",
            (),
            {"run_id": 1, "issue": "42", "project_dir": Path("/tmp/test")},
        )()

        handoff = DashboardHandoff(
            source="developer",
            status="awaiting_action",
            issue="42",
            pr_number=10,
            summary="test",
            next_actions=[
                HandoffAction(
                    id="review",
                    label="Review",
                    auto_execute=True,
                    mode="agent",
                    args={"issue": "42", "role": "reviewer"},
                ),
            ],
        )

        block_error = {"error": "Insufficient memory: 0.5 GB available"}
        mock_start = AsyncMock()
        mock_clear = MagicMock()
        mock_write = MagicMock()

        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch(
                "sova.dashboard.services.agent_handoff.check_memory_pressure",
                return_value=(block_error, None),
            ),
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.dashboard.services.handoff_service.clear_handoff", mock_clear),
            patch("sova.ipc.handoff.write_handoff_file", mock_write),
        ):
            await _process_auto_handoff(agent)

        # Should NOT have spawned the next agent
        mock_start.assert_not_awaited()
        # Should have cleared old handoff and written a blocked one
        mock_clear.assert_called_once()
        mock_write.assert_called_once()
        written_handoff = mock_write.call_args[0][1]
        assert written_handoff.source == "memory_guard"
        assert written_handoff.next_actions[0].auto_execute is False
        assert "(manual)" in written_handoff.next_actions[0].label


# ---------------------------------------------------------------------------
# start_agent / start_command memory pressure gate
# ---------------------------------------------------------------------------


class TestStartAgentMemoryGate:
    async def test_start_agent_blocked_by_memory(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_lifecycle import start_agent

        block_error = {"error": "Insufficient memory: 0.5 GB available"}

        with (
            patch(
                "sova.dashboard.services.agent_lifecycle.check_memory_pressure",
                return_value=(block_error, None),
            ),
            patch(
                "sova.dashboard.services.agent_lifecycle._check_issue_conflict",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("sova.dashboard.services.agent_lifecycle._evict_completed_for_issue", new_callable=MagicMock),
        ):
            result = await start_agent("42", role="developer")

        assert result == block_error

    async def test_start_agent_bypasses_memory_with_force(self) -> None:
        """When force=True, memory pressure check is skipped."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_lifecycle import start_agent

        mock_check = MagicMock(return_value=({"error": "blocked"}, None))

        with (
            patch(
                "sova.dashboard.services.agent_validation.check_memory_pressure",
                mock_check,
            ),
            patch(
                "sova.dashboard.services.agent_lifecycle._check_issue_conflict",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("sova.dashboard.services.agent_lifecycle._evict_completed_for_issue", new_callable=MagicMock),
            patch(
                "sova.dashboard.services.agent_lifecycle._resolve_branch_name",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "sova.dashboard.services.agent_lifecycle._resolve_issue_worktree",
                new_callable=AsyncMock,
                return_value=Path("/tmp"),
            ),
            patch("sova.dashboard.services.agent_lifecycle._create_task_run", new_callable=AsyncMock, return_value=99),
            patch("sova.dashboard.services.agent_lifecycle._transition_to_in_progress", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_lifecycle.get_runtime") as mock_runtime,
        ):
            mock_process = MagicMock()
            mock_process.pid = 12345
            mock_runtime.return_value.spawn = AsyncMock(return_value=mock_process)
            mock_runtime.return_value.get_model_name.return_value = "test"

            await start_agent("42", role="developer", force=True)

        # memory_pressure should NOT have been called when force=True
        mock_check.assert_not_called()

    async def test_start_command_blocked_by_memory(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_lifecycle import start_command

        block_error = {"error": "Insufficient memory: 0.5 GB available"}

        with (
            patch(
                "sova.dashboard.services.agent_lifecycle.check_memory_pressure",
                return_value=(block_error, None),
            ),
            patch(
                "sova.dashboard.services.agent_lifecycle._check_issue_conflict",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("sova.dashboard.services.agent_lifecycle._evict_completed_for_issue", new_callable=MagicMock),
        ):
            result = await start_command("review-pr", args={"issue": "42"})

        assert result == block_error


# ---------------------------------------------------------------------------
# get_sova_review_verdict -- address-pr supersede logic
# ---------------------------------------------------------------------------


class TestReviewVerdictAddressPrSupersede:
    async def test_address_pr_supersedes_review_verdict(self) -> None:
        """When address-pr completed after the review run, verdict should be 'approve'."""
        from sova.dashboard.services.agent_recovery import get_sova_review_verdict

        now = datetime.now(timezone.utc)

        session = await get_session()
        async with session.begin():
            review_run = TaskRun(
                issue_number="50",
                role="reviewer",
                status="done",
                pr_number=200,
                handoff_json={"next_action": "revise", "pending_findings": [{"severity": 5}]},
                started_at=now - timedelta(hours=2),
                ended_at=now - timedelta(hours=1),
            )
            session.add(review_run)
            await session.flush()

            # address-pr completed AFTER the review
            addr_run = TaskRun(
                issue_number="50",
                role="command:address-pr",
                status="done",
                pr_number=200,
                started_at=now - timedelta(minutes=30),
                ended_at=now - timedelta(minutes=10),
            )
            session.add(addr_run)

        result = await get_sova_review_verdict(issue_number="50", pr_number=200, project_dir=Path("/tmp"))

        assert result["has_sova_review"] is True
        assert result["verdict"] == "approve"
        assert result["finding_count"] == 0

    async def test_no_address_pr_preserves_review_verdict(self) -> None:
        """Without address-pr, normal review verdict should be returned."""
        from sova.dashboard.services.agent_recovery import get_sova_review_verdict

        now = datetime.now(timezone.utc)

        session = await get_session()
        async with session.begin():
            review_run = TaskRun(
                issue_number="51",
                role="reviewer",
                status="done",
                pr_number=201,
                handoff_json={"next_action": "revise", "pending_findings": [{"severity": 5}]},
                started_at=now - timedelta(hours=2),
                ended_at=now - timedelta(hours=1),
            )
            session.add(review_run)

        result = await get_sova_review_verdict(issue_number="51", pr_number=201, project_dir=Path("/tmp"))

        assert result["has_sova_review"] is True
        assert result["verdict"] == "revise"

    async def test_older_address_pr_does_not_supersede(self) -> None:
        """address-pr that completed BEFORE the review should not supersede."""
        from sova.dashboard.services.agent_recovery import get_sova_review_verdict

        now = datetime.now(timezone.utc)

        session = await get_session()
        async with session.begin():
            # address-pr ran BEFORE the review
            addr_run = TaskRun(
                issue_number="52",
                role="command:address-pr",
                status="done",
                pr_number=202,
                started_at=now - timedelta(hours=3),
                ended_at=now - timedelta(hours=2, minutes=30),
            )
            session.add(addr_run)
            await session.flush()

            review_run = TaskRun(
                issue_number="52",
                role="reviewer",
                status="done",
                pr_number=202,
                handoff_json={"next_action": "revise", "pending_findings": [{"severity": 8}]},
                started_at=now - timedelta(hours=2),
                ended_at=now - timedelta(hours=1),
            )
            session.add(review_run)

        result = await get_sova_review_verdict(issue_number="52", pr_number=202, project_dir=Path("/tmp"))

        assert result["has_sova_review"] is True
        assert result["verdict"] == "block"  # severity 8 >= 7


# ---------------------------------------------------------------------------
# sync_branch -- "already used by worktree" handling
# ---------------------------------------------------------------------------


class TestSyncBranchWorktreeConflict:
    async def test_already_used_by_worktree_updates_ref(self) -> None:
        """When branch is in active use by another worktree, fetch refspec should be used."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.git.branch import sync_branch

        checkout_fail = MagicMock()
        checkout_fail.success = False
        checkout_fail.stderr = "fatal: 'main' is already used by worktree at '/some/path'"

        fetch_ref_ok = MagicMock()
        fetch_ref_ok.success = True

        stash_ok = MagicMock()
        stash_ok.success = True
        stash_ok.stdout = "No local changes to save"

        with (
            patch("sova.git.branch.run_checked", new_callable=AsyncMock) as mock_fetch,
            patch("sova.git.branch.run", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.side_effect = [stash_ok, checkout_fail, fetch_ref_ok]

            await sync_branch("main", cwd=Path("/tmp/worktree"))

        mock_fetch.assert_awaited_once_with("git", "fetch", "origin", "main", cwd=Path("/tmp/worktree"))
        assert mock_run.await_count == 3
        mock_run.assert_any_await("git", "fetch", "origin", "main:main", cwd=Path("/tmp/worktree"))
        stash_pop_calls = [c for c in mock_run.call_args_list if c[0] == ("git", "stash", "pop")]
        assert len(stash_pop_calls) == 0, "No stash pop when nothing was stashed"

    async def test_already_used_by_worktree_pops_stash_when_dirty(self) -> None:
        """When branch is in active use by another worktree and we stashed, stash must be popped."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.git.branch import sync_branch

        checkout_fail = MagicMock()
        checkout_fail.success = False
        checkout_fail.stderr = "fatal: 'main' is already used by worktree at '/some/path'"

        fetch_ref_ok = MagicMock()
        fetch_ref_ok.success = True

        stash_dirty = MagicMock()
        stash_dirty.success = True
        stash_dirty.stdout = "Saved working directory and index state WIP on feat: abc1234"

        pop_ok = MagicMock()
        pop_ok.success = True

        with (
            patch("sova.git.branch.run_checked", new_callable=AsyncMock),
            patch("sova.git.branch.run", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.side_effect = [stash_dirty, checkout_fail, fetch_ref_ok, pop_ok]

            await sync_branch("main", cwd=Path("/tmp/worktree"))

        assert mock_run.await_count == 4
        mock_run.assert_any_await("git", "stash", "pop", cwd=Path("/tmp/worktree"))

    async def test_already_used_by_worktree_ref_update_fails(self) -> None:
        """When fetch refspec fails, should log warning but not raise."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.git.branch import sync_branch

        checkout_fail = MagicMock()
        checkout_fail.success = False
        checkout_fail.stderr = "fatal: 'main' is already used by worktree at '/some/path'"

        fetch_ref_fail = MagicMock()
        fetch_ref_fail.success = False
        fetch_ref_fail.stderr = "non-fast-forward update"

        stash_ok = MagicMock()
        stash_ok.success = True
        stash_ok.stdout = "No local changes to save"

        with (
            patch("sova.git.branch.run_checked", new_callable=AsyncMock),
            patch("sova.git.branch.run", new_callable=AsyncMock) as mock_run,
        ):
            mock_run.side_effect = [stash_ok, checkout_fail, fetch_ref_fail]
            # Should not raise
            await sync_branch("main", cwd=Path("/tmp/worktree"))


# ---------------------------------------------------------------------------
# get_primary_worktree_root
# ---------------------------------------------------------------------------


class TestGetPrimaryWorktreeRoot:
    async def test_returns_parent_of_absolute_common_dir(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.git.worktree import get_primary_worktree_root

        result_mock = MagicMock()
        result_mock.success = True
        result_mock.stdout = "/home/user/project/.git\n"

        with patch("sova.git.worktree.run", new_callable=AsyncMock, return_value=result_mock):
            root = await get_primary_worktree_root(cwd=Path("/home/user/project/.claude/worktrees/42"))

        assert root == Path("/home/user/project")

    async def test_returns_cwd_for_relative_common_dir(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.git.worktree import get_primary_worktree_root

        result_mock = MagicMock()
        result_mock.success = True
        result_mock.stdout = ".git\n"

        with patch("sova.git.worktree.run", new_callable=AsyncMock, return_value=result_mock):
            root = await get_primary_worktree_root(cwd=Path("/home/user/project"))

        assert root == Path("/home/user/project")

    async def test_returns_cwd_on_git_failure(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.git.worktree import get_primary_worktree_root

        result_mock = MagicMock()
        result_mock.success = False

        with patch("sova.git.worktree.run", new_callable=AsyncMock, return_value=result_mock):
            root = await get_primary_worktree_root(cwd=Path("/tmp/not-a-repo"))

        assert root == Path("/tmp/not-a-repo")

    async def test_defaults_to_cwd(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.git.worktree import get_primary_worktree_root

        result_mock = MagicMock()
        result_mock.success = True
        result_mock.stdout = ".git\n"

        with (
            patch("sova.git.worktree.run", new_callable=AsyncMock, return_value=result_mock),
            patch("pathlib.Path.cwd", return_value=Path("/current/dir")),
        ):
            root = await get_primary_worktree_root()

        assert root == Path("/current/dir")


class TestPrSuggestionEndpoint:
    """Tests for POST /api/prs/{pr_number}/suggestion."""

    async def test_returns_204_when_no_api_key(self, client: AsyncClient, monkeypatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        from sova.dashboard.services.llm_suggestion_service import clear_cache

        clear_cache()
        body = {
            "deterministic_state": "pr_sova_pending",
            "deterministic_action_id": "review_pr",
            "pr_computed_state": "approved_ci_green",
            "has_sova_review": False,
            "sova_verdict": None,
            "mergeable": "MERGEABLE",
            "review_decision": "APPROVED",
            "ci_passed": True,
        }
        resp = await client.post("/api/prs/378/suggestion", json=body)
        assert resp.status_code == 204

    async def test_returns_suggestion_when_llm_disagrees(self, client: AsyncClient, monkeypatch) -> None:
        import json
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.llm_suggestion_service import clear_cache

        clear_cache()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        content = json.dumps({"action_id": "integrate", "reasoning": "CI green and approved"})
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"content": [{"text": content}]}

        body = {
            "deterministic_state": "pr_sova_pending",
            "deterministic_action_id": "review_pr",
            "pr_computed_state": "approved_ci_green",
        }
        with patch("httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_cls.return_value.__aenter__.return_value = mock_http
            mock_http.post.return_value = mock_resp
            resp = await client.post("/api/prs/378/suggestion", json=body)

        assert resp.status_code == 200
        data = resp.json()
        assert data["action_id"] == "integrate"
        assert data["disagrees"] is True


class TestPrFeedbackEndpoint:
    """Tests for POST /api/prs/feedback."""

    async def test_stores_feedback_record(self, client: AsyncClient) -> None:
        body = {
            "pr_number": 378,
            "issue_number": "377",
            "deterministic_state": "pr_sova_pending",
            "deterministic_action_id": "review_pr",
            "llm_action_id": "integrate",
            "llm_reasoning": "CI green and approved",
            "user_choice": "deterministic",
        }
        resp = await client.post("/api/prs/feedback", json=body)
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert isinstance(data["id"], int)

    async def test_stores_llm_choice(self, client: AsyncClient) -> None:
        body = {
            "pr_number": 100,
            "deterministic_state": "pr_awaiting_review",
            "deterministic_action_id": "review_pr",
            "llm_action_id": "address_pr",
            "user_choice": "llm",
        }
        resp = await client.post("/api/prs/feedback", json=body)
        assert resp.status_code == 201

    async def test_rejects_invalid_user_choice(self, client: AsyncClient) -> None:
        body = {
            "pr_number": 1,
            "deterministic_state": "pr_sova_pending",
            "deterministic_action_id": "review_pr",
            "llm_action_id": "integrate",
            "user_choice": "not_valid_at_all",
        }
        resp = await client.post("/api/prs/feedback", json=body)
        assert resp.status_code == 400

    async def test_accepts_action_id_as_user_choice(self, client: AsyncClient) -> None:
        """user_choice can be any valid action_id, not just 'deterministic'/'llm'."""
        body = {
            "pr_number": 200,
            "deterministic_state": "pr_sova_pending",
            "deterministic_action_id": "review_pr",
            "llm_action_id": "integrate",
            "user_choice": "address_pr",
        }
        resp = await client.post("/api/prs/feedback", json=body)
        assert resp.status_code == 201


class TestAgentPoolConfig:
    def test_read_max_parallel_returns_config_value(self, tmp_path):
        toml_file = tmp_path / "sova.toml"
        toml_file.write_text("[project]\nmax_parallel_agents = 5\n")
        from sova.dashboard.services.agent_pool import read_max_parallel

        result = read_max_parallel(tmp_path)
        assert result == 5

    def test_read_max_parallel_fallback_on_missing_config(self, tmp_path):
        from sova.dashboard.services.agent_pool import ProjectAgents, read_max_parallel

        result = read_max_parallel(tmp_path / "nonexistent")
        assert result == ProjectAgents.max_concurrent

    def test_sync_max_concurrent_updates_pool(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        from sova.dashboard.services import agent_pool

        mock_cfg = MagicMock()
        mock_cfg.max_parallel_agents = 7
        monkeypatch.setattr("sova.config.loader.load_config", lambda *a, **kw: mock_cfg, raising=False)
        old_projects = agent_pool._projects.copy()
        agent_pool._projects.clear()
        try:
            pa = agent_pool._get_project_agents("test-sync")
            pa.max_concurrent = 2
            agent_pool.sync_max_concurrent(project_dir=tmp_path, slug="test-sync")
            assert pa.max_concurrent == 7
        finally:
            agent_pool._projects.clear()
            agent_pool._projects.update(old_projects)

    def test_sync_max_concurrent_no_change_when_equal(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        from sova.dashboard.services import agent_pool

        mock_cfg = MagicMock()
        mock_cfg.max_parallel_agents = 2
        monkeypatch.setattr("sova.config.loader.load_config", lambda *a, **kw: mock_cfg, raising=False)
        old_projects = agent_pool._projects.copy()
        agent_pool._projects.clear()
        try:
            pa = agent_pool._get_project_agents("test-noop")
            pa.max_concurrent = 2
            agent_pool.sync_max_concurrent(project_dir=tmp_path, slug="test-noop")
            assert pa.max_concurrent == 2
        finally:
            agent_pool._projects.clear()
            agent_pool._projects.update(old_projects)

    def test_sync_max_concurrent_swallows_config_errors(self, monkeypatch):
        from sova.dashboard.services import agent_pool

        monkeypatch.setattr(
            "sova.config.loader.load_config",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no config")),
            raising=False,
        )
        old_projects = agent_pool._projects.copy()
        agent_pool._projects.clear()
        try:
            pa = agent_pool._get_project_agents("test-err")
            pa.max_concurrent = 3
            agent_pool.sync_max_concurrent(project_dir=Path("/nonexistent"), slug="test-err")
            assert pa.max_concurrent == 3
        finally:
            agent_pool._projects.clear()
            agent_pool._projects.update(old_projects)

    def test_get_project_agents_reads_config_on_create(self, tmp_path, monkeypatch):
        from sova.dashboard.services import agent_pool

        toml_file = tmp_path / "sova.toml"
        toml_file.write_text("[project]\nmax_parallel_agents = 4\n")
        monkeypatch.setattr("sova.dashboard.services.agent_pool.get_project_dir", lambda: tmp_path)
        old_projects = agent_pool._projects.copy()
        agent_pool._projects.clear()
        try:
            pa = agent_pool._get_project_agents("test-init")
            assert pa.max_concurrent == 4
        finally:
            agent_pool._projects.clear()
            agent_pool._projects.update(old_projects)


class TestSettingsMaxParallelSync:
    async def test_update_config_triggers_sync(self, client: AsyncClient, monkeypatch):
        from unittest.mock import MagicMock

        sync_called = []
        monkeypatch.setattr(
            "sova.dashboard.routers.settings.settings_service",
            MagicMock(update_config=MagicMock(return_value={"status": "ok", "key": "max_parallel_agents"})),
        )
        monkeypatch.setattr(
            "sova.dashboard.services.agent_pool.sync_max_concurrent",
            lambda *a, **kw: sync_called.append(True),
        )
        resp = await client.post(
            "/api/settings/config",
            json={"key": "max_parallel_agents", "value": "5"},
        )
        assert resp.status_code == 200
        assert len(sync_called) == 1

    async def test_update_config_no_sync_for_other_keys(self, client: AsyncClient, monkeypatch):
        from unittest.mock import MagicMock

        sync_called = []
        monkeypatch.setattr(
            "sova.dashboard.routers.settings.settings_service",
            MagicMock(update_config=MagicMock(return_value={"status": "ok", "key": "github_repo"})),
        )
        monkeypatch.setattr(
            "sova.dashboard.services.agent_pool.sync_max_concurrent",
            lambda *a, **kw: sync_called.append(True),
        )
        resp = await client.post(
            "/api/settings/config",
            json={"key": "github_repo", "value": "org/repo"},
        )
        assert resp.status_code == 200
        assert len(sync_called) == 0

    async def test_update_config_accepts_json_boolean(self, client: AsyncClient, monkeypatch):
        from unittest.mock import MagicMock

        mock_service = MagicMock(update_config=MagicMock(return_value={"status": "ok", "key": "supervisor.enabled"}))
        monkeypatch.setattr("sova.dashboard.routers.settings.settings_service", mock_service)
        resp = await client.post(
            "/api/settings/config",
            json={"key": "supervisor.enabled", "value": True},
        )
        assert resp.status_code == 200
        mock_service.update_config.assert_called_once()
        _, kwargs = mock_service.update_config.call_args
        assert kwargs["value"] == "true"
