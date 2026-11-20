"""Work API route and the work, queue and spec-status services behind it.

Split from tests/test_dashboard.py; see docs/testing-guidelines.md, "Test File Layout".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sova.db.models import StepExecution, TaskRun
from sova.db.session import get_session
from tests.conftest import DASHBOARD_DIR

# Uses the shared per-worker in-memory DB, emptied before each test
# (see ``tests/conftest.py:reset_db_rows``). A test here that leaves a
# background task touching the DB when its event loop closes (the real
# ``start_agent()`` calls in ``TestDuplicateAgentPrevention``) can invalidate the
# shared connection; the next test's sweep recreates the schema.
pytestmark = pytest.mark.usefixtures("reset_db_rows")


# ---------------------------------------------------------------------------
# Recovery: merge-role runs check PR merged
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


class TestQueueServiceEnrichment:
    """Tests for NEEDS_SPEC inclusion and recommended actions in queue_service."""

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
        from sova.dashboard.services.queue_service import _RECOMMENDED_ACTION, VALID_STATES_FOR_ACTION

        # The researcher only accepts TRIAGED, so a parked NEEDS_SPEC issue exits through harden.
        assert _RECOMMENDED_ACTION[TaskState.NEEDS_SPEC] == "harden"
        assert TaskState.NEEDS_SPEC in VALID_STATES_FOR_ACTION["harden"]

    def test_researched_default_action_is_review_spec(self) -> None:
        from sova.adapters.base import TaskState
        from sova.dashboard.services.queue_service import _RECOMMENDED_ACTION

        assert _RECOMMENDED_ACTION[TaskState.RESEARCHED] == "review_spec"


class TestEnrichSpecStatus:
    """Tests for _enrich_spec_status spec-aware action resolution."""

    async def test_non_researched_items_get_null_spec_status(self, tmp_path: Path) -> None:
        from sova.dashboard.services.queue_service import _enrich_spec_status

        queue = [
            {"state": "backlog", "issue": "1", "action": "triage"},
            {"state": "triaged", "issue": "2", "action": "research"},
        ]
        await _enrich_spec_status(queue, tmp_path)
        assert queue[0]["spec_status"] is None
        assert queue[1]["spec_status"] is None
        assert queue[0]["action"] == "triage"
        assert queue[1]["action"] == "research"

    @pytest.mark.parametrize(
        ("spec", "expected_status", "expected_action"),
        [
            pytest.param(None, "missing", "develop", id="missing-spec-gets-develop"),
            pytest.param({"status": "approved"}, "approved", "develop", id="approved-spec-gets-develop"),
            pytest.param({"status": "draft"}, "draft", "review_spec", id="draft-spec-gets-review-spec"),
            pytest.param({"status": "rejected"}, "rejected", "review_spec", id="rejected-spec-gets-review-spec"),
            pytest.param({"complexity": "medium"}, "draft", "review_spec", id="no-status-field-defaults-to-draft"),
        ],
    )
    async def test_researched_spec_action(
        self, tmp_path: Path, spec: dict[str, str] | None, expected_status: str, expected_action: str
    ) -> None:
        from sova.dashboard.services.queue_service import _enrich_spec_status

        queue = [{"state": "researched", "issue": "10", "action": "review_spec"}]
        with patch("sova.dashboard.services.spec_service.read_spec", return_value=spec):
            await _enrich_spec_status(queue, tmp_path)
        assert queue[0]["spec_status"] == expected_status
        assert queue[0]["action"] == expected_action

    async def test_read_spec_exception_falls_back_to_missing(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.queue_service import _enrich_spec_status

        queue = [{"state": "researched", "issue": "14", "action": "review_spec"}]
        with patch(
            "sova.dashboard.services.spec_service.read_spec",
            side_effect=OSError("permission denied"),
        ):
            await _enrich_spec_status(queue, tmp_path)
        assert queue[0]["spec_status"] == "missing"
        assert queue[0]["action"] == "develop"

    async def test_mixed_states_enriched_correctly(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.queue_service import _enrich_spec_status

        queue = [
            {"state": "backlog", "issue": "1", "action": "triage"},
            {"state": "researched", "issue": "2", "action": "review_spec"},
            {"state": "researched", "issue": "3", "action": "review_spec"},
            {"state": "in_progress", "issue": "4", "action": "develop"},
        ]

        def mock_read_spec(issue_number, project_dir=None):
            if issue_number == "2":
                return {"status": "approved"}
            if issue_number == "3":
                return {"status": "draft"}
            return None

        with patch("sova.dashboard.services.spec_service.read_spec", side_effect=mock_read_spec):
            await _enrich_spec_status(queue, tmp_path)

        assert queue[0]["spec_status"] is None
        assert queue[0]["action"] == "triage"
        assert queue[1]["spec_status"] == "approved"
        assert queue[1]["action"] == "develop"
        assert queue[2]["spec_status"] == "draft"
        assert queue[2]["action"] == "review_spec"
        assert queue[3]["spec_status"] is None
        assert queue[3]["action"] == "develop"


class TestWorkAPI:
    """Tests for the new work API endpoints."""

    async def test_work_active_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/work/active")
        assert resp.status_code == 200
        data = resp.json()
        assert "tasks" in data
        assert isinstance(data["tasks"], list)

    @pytest.mark.parametrize(
        "url",
        [
            pytest.param("/api/work/history", id="empty"),
            pytest.param("/api/work/history?status=done&role=developer&limit=10", id="with-filters"),
        ],
    )
    async def test_work_history(self, client: AsyncClient, url: str) -> None:
        resp = await client.get(url)
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

    async def test_work_issue_rebase_endpoint(self, client: AsyncClient) -> None:
        """The rebase-trigger endpoint answers at once and runs attempt_auto_rebase in the background.

        The outcome is recorded on the issue's action, so the card shows it when the rebase ends.
        """
        import asyncio
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.routers import work as work_router
        from sova.dashboard.services import action_registry

        action_registry.reset_actions()
        expected = {"status": "success", "pr_number": 123, "conflicts_resolved": 2}
        with patch(
            "sova.supervisor.rebase.attempt_auto_rebase", new_callable=AsyncMock, return_value=expected
        ) as mock_rebase:
            resp = await client.post("/api/work/issue/42/rebase")
            assert resp.status_code == 202
            body = resp.json()
            assert body["status"] == "started"
            await asyncio.gather(*list(work_router._rebase_tasks))

        mock_rebase.assert_awaited_once()
        assert mock_rebase.call_args.args[0] == 42
        actions = (await client.get("/api/work/actions")).json()["actions"]
        assert [(a["id"], a["issue"], a["status"], a["message"]) for a in actions] == [
            (body["action_id"], 42, "done", "Rebased")
        ]
        action_registry.reset_actions()

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


# ---------------------------------------------------------------------------
# Work Service: direct service tests
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


class TestAgentsPageRefreshWiring:
    """The Work page must refetch its board on agent exit, not only on a poll (#1149)."""

    def _board(self) -> str:
        return (DASHBOARD_DIR / "static" / "js" / "pages" / "work" / "board.js").read_text(encoding="utf-8")

    def _live_status(self) -> str:
        return (DASHBOARD_DIR / "static" / "js" / "pages" / "agents" / "live-status.js").read_text(encoding="utf-8")

    def test_handles_agent_finished_event(self) -> None:
        source = self._board()
        start = source.index("function _handleMessage(msg)")
        handler = source[start : source.index("return;", start)]
        assert "msg.type === 'agent_finished'" in handler
        assert "loadBoard()" in handler

    def test_agent_finished_debounces_agents_refresh_too(self) -> None:
        """A parallel batch finishing fires one agent_finished per agent; an undebounced
        loadAgents() per event costs one unthrottled request per exit (#1162 review).
        """
        source = self._live_status()
        start = source.index("msg.type === 'agent_finished'")
        handler = source[start : source.index("return;", start)]
        assert "_scheduleAgentsRefresh()" in handler
        assert "loadAgents()" not in handler

    def test_status_update_change_schedules_refresh(self) -> None:
        source = self._board()
        start = source.index("msg.type === 'status_update' && Array.isArray(msg.runs)")
        handler = source[start : source.index("_runSignature = signature;", start)]
        assert "signature !== _runSignature" in handler
        assert "loadBoard()" in handler


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
# Complexity endpoint
# ---------------------------------------------------------------------------


class TestComplexityEndpoint:
    """Tests for GET /api/agents/issue/{issue}/complexity."""

    async def test_complexity_from_db_assessment(self, client: AsyncClient, session: AsyncSession) -> None:
        from sova.db.models import TaskAssessmentRecord

        record = TaskAssessmentRecord(
            issue_number="99",
            suitability="suitable",
            confidence=Decimal("0.8"),
            reasoning="test",
            estimated_complexity="complex",
        )
        session.add(record)
        await session.commit()

        resp = await client.get("/api/agents/issue/99/complexity")
        assert resp.status_code == 200
        data = resp.json()
        assert data["complexity"] == "complex"
        assert data["suggested_model"] == "opus"
        # The launch modal's Model select is fed by runtime-layer discovery
        # (#1039); the per-tier option list this endpoint used to return has no
        # consumer left.
        assert "model_options" not in data

    async def test_complexity_suggestion_is_resolved_to_a_concrete_id(
        self, client: AsyncClient, session: AsyncSession
    ) -> None:
        """The runtime catalog the launch modal preselects from lists concrete
        IDs, never a bare tier alias, so the suggestion must be resolved the
        same way the pipeline resolves it or it can never be preselected."""
        from unittest.mock import patch

        from sova.db.models import TaskAssessmentRecord

        session.add(
            TaskAssessmentRecord(
                issue_number="98",
                suitability="suitable",
                confidence=Decimal("0.8"),
                reasoning="test",
                estimated_complexity="complex",
            )
        )
        await session.commit()

        with patch("sova.llm.client.resolve_alias", return_value="claude-opus-5") as resolve:
            resp = await client.get("/api/agents/issue/98/complexity")
        data = resp.json()
        assert data["suggested_model"] == "opus"
        assert data["suggested_model_id"] == "claude-opus-5"
        assert resolve.call_args.args[0] == "opus"

    async def test_complexity_suggestion_id_falls_back_to_alias_on_resolve_failure(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        mock_adapter = AsyncMock()
        mock_adapter.get_task = AsyncMock(side_effect=RuntimeError("API error"))

        with (
            patch("sova.adapters.create_adapter", return_value=mock_adapter),
            patch("sova.llm.client.resolve_alias", side_effect=ValueError("bad alias map")),
        ):
            resp = await client.get("/api/agents/issue/97/complexity")
        assert resp.status_code == 200
        assert resp.json()["suggested_model_id"] == "sonnet"

    async def test_complexity_fallback_to_adapter(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_task = MagicMock()
        mock_task.title = "Refactor the authentication module"
        mock_task.description = "Large refactor across multiple files"
        mock_task.labels = ["complex"]

        mock_adapter = AsyncMock()
        mock_adapter.get_task = AsyncMock(return_value=mock_task)

        with patch("sova.adapters.create_adapter", return_value=mock_adapter):
            resp = await client.get("/api/agents/issue/100/complexity")

        assert resp.status_code == 200
        data = resp.json()
        assert data["complexity"] == "complex"
        assert data["suggested_model"] == "opus"

    async def test_complexity_fallback_to_moderate_on_error(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        mock_adapter = AsyncMock()
        mock_adapter.get_task = AsyncMock(side_effect=RuntimeError("API error"))

        with patch("sova.adapters.create_adapter", return_value=mock_adapter):
            resp = await client.get("/api/agents/issue/101/complexity")

        assert resp.status_code == 200
        data = resp.json()
        assert data["complexity"] == "moderate"
        assert data["suggested_model"] == "sonnet"

    async def test_complexity_survives_a_failed_assessment_lookup(self, client: AsyncClient) -> None:
        """A database failure on the stored-assessment lookup costs the shortcut, not the launch modal."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sqlalchemy.exc import OperationalError

        mock_task = MagicMock()
        mock_task.title = "Refactor the authentication module"
        mock_task.description = "Large refactor across multiple files"
        mock_task.labels = ["complex"]
        mock_adapter = AsyncMock()
        mock_adapter.get_task = AsyncMock(return_value=mock_task)

        with (
            patch("sova.adapters.create_adapter", return_value=mock_adapter),
            patch(
                "sova.dashboard.services.llm_suggestion_service.get_session",
                new_callable=AsyncMock,
                side_effect=OperationalError("select", {}, Exception("db is locked")),
            ) as mock_get_session,
        ):
            resp = await client.get("/api/agents/issue/102/complexity")

        # The "complex" label gives the same answer with or without the lookup, so only the call proves it failed.
        mock_get_session.assert_awaited_once()
        assert resp.status_code == 200
        assert resp.json()["complexity"] == "complex"

    async def test_complexity_response_includes_confirm_model(self, client: AsyncClient, session: AsyncSession) -> None:
        from sova.db.models import TaskAssessmentRecord

        record = TaskAssessmentRecord(
            issue_number="200",
            suitability="suitable",
            confidence=Decimal("0.8"),
            reasoning="test",
            estimated_complexity="moderate",
        )
        session.add(record)
        await session.commit()

        resp = await client.get("/api/agents/issue/200/complexity")
        assert resp.status_code == 200
        data = resp.json()
        assert "confirm_model" in data
        assert data["confirm_model"] == "complex_only"

    async def test_complexity_confirm_model_reflects_config(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.config.models import DashboardConfig, ProjectConfig

        mock_task = MagicMock()
        mock_task.title = "Simple fix"
        mock_task.description = "A simple change"
        mock_task.labels = []

        mock_adapter = AsyncMock()
        mock_adapter.get_task = AsyncMock(return_value=mock_task)

        cfg = ProjectConfig()
        cfg.dashboard = DashboardConfig(confirm_model="always")

        with (
            patch("sova.adapters.create_adapter", return_value=mock_adapter),
            patch(
                "sova.dashboard.services.llm_suggestion_service.load_config",
                return_value=cfg,
            ),
            patch(
                "sova.dashboard.services.work_item_service.load_config",
                return_value=cfg,
            ),
        ):
            resp = await client.get("/api/agents/issue/202/complexity")

        assert resp.status_code == 200
        data = resp.json()
        assert data["confirm_model"] == "always"


class TestWorkItemReasonChainPayload:
    """Every work item payload carries the resolver's rendered reason chain (#992)."""

    @staticmethod
    def _task(state: str = "in_review") -> dict:
        return {"issue": 992, "state": state, "title": "Reason chain", "url": "", "labels": []}

    @staticmethod
    def _pr(**overrides: object) -> dict:
        pr = {
            "number": 1030,
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "ci_status": "passed",
            "head_sha": "sha-head",
            "thread_total": 0,
            "thread_resolved": 0,
        }
        pr.update(overrides)
        return pr

    def _build(self, **kwargs: object) -> dict:
        from sova.dashboard.services.work_item_service import _build_task_item

        return _build_task_item(self._task(), **kwargs)  # type: ignore[arg-type]

    def test_agent_running_item_explains_the_running_agent(self) -> None:
        item = self._build(pr_data=self._pr(), running={"role": "developer", "run_id": 7}, handoff=None)
        assert item["state"] == "agent_running"
        assert item["matched_reason"] == item["reason_chain"][-1]
        assert "agent" in item["matched_reason"]

    def test_standing_sova_revise_verdict_is_named(self) -> None:
        verdict = {"has_sova_review": True, "verdict": "revise", "review_head_sha": "sha-head"}
        item = self._build(pr_data=self._pr(), running=None, handoff=None, sova_verdict=verdict)
        assert item["state"] == "pr_sova_changes"
        assert "revise" in item["matched_reason"]
        assert item["reason_chain"][0] == "no agent is currently running"

    def test_external_changes_requested_is_named(self) -> None:
        verdict = {"has_sova_review": True, "verdict": "approve", "review_head_sha": "sha-head"}
        item = self._build(
            pr_data=self._pr(computed_state="changes_requested"),
            running=None,
            handoff=None,
            sova_verdict=verdict,
        )
        assert item["state"] == "pr_external_changes"
        assert "external reviewer" in item["matched_reason"]

    def test_unknown_thread_signal_says_unknown(self) -> None:
        verdict = {"has_sova_review": True, "verdict": "approve", "review_head_sha": "sha-head"}
        item = self._build(
            pr_data=self._pr(thread_total=None, thread_resolved=None),
            running=None,
            handoff=None,
            sova_verdict=verdict,
        )
        assert item["state"] == "pr_external_changes"
        assert "unknown" in item["matched_reason"]

    def test_integrate_ready_item_explains_readiness(self) -> None:
        verdict = {"has_sova_review": True, "verdict": "approve", "review_head_sha": "sha-head"}
        item = self._build(pr_data=self._pr(), running=None, handoff=None, sova_verdict=verdict)
        assert item["state"] == "pr_ready_to_merge"
        assert "ready to merge" in item["matched_reason"]
        assert "not ready" not in item["matched_reason"]

    def test_label_only_item_has_empty_chain_and_reason(self) -> None:
        item = self._build(pr_data=None, running=None, handoff=None)
        assert item["reason_chain"] == []
        assert item["matched_reason"] == ""
