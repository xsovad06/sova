"""Overview, runs, costs and tasks API routes.

Split from tests/test_dashboard.py; see docs/testing-guidelines.md, "Test File Layout".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sova.db.models import CostRecord, StepExecution, TaskRun

# Uses the shared per-worker in-memory DB, emptied before each test
# (see ``tests/conftest.py:reset_db_rows``). A test here that leaves a
# background task touching the DB when its event loop closes (the real
# ``start_agent()`` calls in ``TestDuplicateAgentPrevention``) can invalidate the
# shared connection; the next test's sweep recreates the schema.
pytestmark = pytest.mark.usefixtures("reset_db_rows")


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
        assert "memory_count" not in data
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

    async def test_overview_api_today_cost_includes_todays_run(
        self, client: AsyncClient, session: AsyncSession
    ) -> None:
        # Anchored to datetime.now() rather than a fixed offset from it, so the
        # assertion holds regardless of what time of day (UTC) the suite runs.
        # A second run outside today's window exercises the today_start filter
        # itself: without it, today_cost_usd would pass this test identically
        # if the filter were dropped or the key swapped for total_cost_usd.
        session.add(
            TaskRun(
                issue_number="99",
                role="developer",
                status="done",
                total_cost_usd=Decimal("3.00"),
                started_at=datetime.now(timezone.utc),
            )
        )
        session.add(
            TaskRun(
                issue_number="100",
                role="developer",
                status="done",
                total_cost_usd=Decimal("7.00"),
                started_at=datetime.now(timezone.utc) - timedelta(days=3),
            )
        )
        await session.commit()

        resp = await client.get("/api/overview")
        assert resp.status_code == 200
        data = resp.json()
        assert float(data["costs"]["today_cost_usd"]) == pytest.approx(3.0)
        assert float(data["costs"]["total_cost_usd"]) == pytest.approx(10.0)


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
    """A service failure reaches the app-level handler: a 500 with the fixed body."""

    @pytest.mark.parametrize(
        ("service_method", "url"),
        [
            pytest.param("get_summary", "/api/costs/summary", id="summary"),
            pytest.param("get_daily", "/api/costs/daily", id="daily"),
            pytest.param("get_by_issue", "/api/costs/by-issue", id="by-issue"),
            pytest.param("get_by_phase", "/api/costs/by-phase", id="by-phase"),
            pytest.param("get_by_model", "/api/costs/by-model", id="by-model"),
            pytest.param("get_by_routing", "/api/costs/by-routing", id="by-routing"),
        ],
    )
    async def test_service_error_returns_500(self, client_no_raise: AsyncClient, service_method: str, url: str) -> None:
        with patch(f"sova.dashboard.routers.costs.cost_service.{service_method}", side_effect=RuntimeError("db down")):
            resp = await client_no_raise.get(url)
        assert resp.status_code == 500
        assert resp.json() == {"detail": "Internal error"}


# ---------------------------------------------------------------------------
# Setup API
# ---------------------------------------------------------------------------


class TestTasksAPI:
    """Tests for the tasks API endpoints."""

    @pytest.mark.parametrize(
        ("path", "marker"),
        [
            pytest.param("/queue", b"Priority Queue", id="queue"),
            pytest.param("/specs", b"Specs", id="specs"),
        ],
    )
    async def test_page_renders(self, client: AsyncClient, path: str, marker: bytes) -> None:
        resp = await client.get(path)
        assert resp.status_code == 200
        assert marker in resp.content

    async def test_queue_page_has_bulk_bar(self, client: AsyncClient) -> None:
        resp = await client.get("/queue")
        assert resp.status_code == 200
        assert b"bulk-bar" in resp.content
        assert b"select-all" in resp.content

    async def test_specs_page_has_search_and_filter(self, client: AsyncClient) -> None:
        resp = await client.get("/specs")
        assert resp.status_code == 200
        assert b"spec-search" in resp.content
        assert b"spec-status-filter" in resp.content


class TestMilestoneBadge:
    """Tests for _milestone_badge helper in queue_service."""

    @pytest.mark.parametrize(
        ("milestone", "expected"),
        [
            pytest.param("", "--", id="empty-string-returns-dash"),
            pytest.param("P3: v0.1 Public Release", "P3", id="standard-milestone"),
            pytest.param("Beta", "Beta", id="no-colon"),
            pytest.param("  P4  : future work", "P4", id="whitespace-around-prefix"),
        ],
    )
    def test_badge(self, milestone: str, expected: str) -> None:
        from sova.dashboard.services.queue_service import _milestone_badge

        assert _milestone_badge(milestone) == expected

    def test_long_prefix_truncated(self) -> None:
        from sova.dashboard.services.queue_service import _milestone_badge

        assert _milestone_badge("VeryLongPrefix: something") == "VeryLong"
        assert len(_milestone_badge("VeryLongPrefix: something")) <= 8


class TestComputeDurationMs:
    """Unit tests for _compute_duration_ms, the module's one duration-in-milliseconds helper."""

    def test_both_none(self) -> None:
        from sova.dashboard.services.work_service import _compute_duration_ms

        assert _compute_duration_ms(None, None) is None

    def test_started_none(self) -> None:
        from sova.dashboard.services.work_service import _compute_duration_ms

        assert _compute_duration_ms(None, datetime.now(timezone.utc)) is None

    def test_ended_none(self) -> None:
        from sova.dashboard.services.work_service import _compute_duration_ms

        assert _compute_duration_ms(datetime.now(timezone.utc), None) is None

    def test_naive_datetimes(self) -> None:
        from sova.dashboard.services.work_service import _compute_duration_ms

        start = datetime(2026, 1, 1, 12, 0, 0)
        end = datetime(2026, 1, 1, 12, 0, 10)
        assert _compute_duration_ms(start, end) == 10_000

    def test_aware_datetimes(self) -> None:
        from sova.dashboard.services.work_service import _compute_duration_ms

        start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 1, 1, 12, 5, 0, tzinfo=timezone.utc)
        assert _compute_duration_ms(start, end) == 300_000


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
