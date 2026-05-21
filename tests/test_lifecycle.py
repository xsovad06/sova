"""Tests for sova.dashboard.services.lifecycle_service and lifecycle router."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sova.core.state import PhaseStatus
from sova.dashboard.services import lifecycle_service
from sova.db.models import IssueLifecycle, LifecyclePhaseRecord, TaskRun
from sova.db.session import close_db, get_session, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    """Initialize an in-memory DB for lifecycle tests."""
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


@pytest.fixture
async def session():
    async with await get_session() as session:
        yield session


# -- Model tests --------------------------------------------------------------


class TestIssueLifecycleModel:
    async def test_create_lifecycle(self, session: AsyncSession):
        async with session.begin():
            lc = IssueLifecycle(
                issue_number="42",
                project_slug="myproject",
                current_phase="development",
                phase_status="pending",
            )
            session.add(lc)
            await session.flush()
            assert lc.id is not None
            assert lc.issue_number == "42"
            assert lc.current_phase == "development"

    async def test_normalize_issue_number(self, session: AsyncSession):
        async with session.begin():
            lc = IssueLifecycle(issue_number="#55", current_phase="development", phase_status="pending")
            session.add(lc)
            await session.flush()
            assert lc.issue_number == "55"

    async def test_lifecycle_with_phases(self, session: AsyncSession):
        async with session.begin():
            lc = IssueLifecycle(issue_number="10", current_phase="development", phase_status="active")
            session.add(lc)
            await session.flush()

            phase = LifecyclePhaseRecord(
                lifecycle_id=lc.id,
                phase="development",
                status="active",
                attempt=1,
                started_at=datetime.now(timezone.utc),
            )
            session.add(phase)
            await session.flush()
            assert phase.lifecycle_id == lc.id


# -- Service tests ------------------------------------------------------------


class TestLifecycleServiceCRUD:
    async def test_get_or_create_lifecycle_creates(self, session: AsyncSession):
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")
            assert lc.issue_number == "42"
            assert lc.current_phase == "development"
            assert lc.phase_status == "pending"

    async def test_get_or_create_lifecycle_idempotent(self, session: AsyncSession):
        async with session.begin():
            lc1 = await lifecycle_service.get_or_create_lifecycle(session, "42")
            lc2 = await lifecycle_service.get_or_create_lifecycle(session, "42")
            assert lc1.id == lc2.id

    async def test_get_or_create_strips_hash(self, session: AsyncSession):
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "#42")
            assert lc.issue_number == "42"

    async def test_list_active_lifecycles(self, session: AsyncSession):
        async with session.begin():
            await lifecycle_service.get_or_create_lifecycle(session, "42")
            await lifecycle_service.get_or_create_lifecycle(session, "43")
            lifecycles = await lifecycle_service.list_active_lifecycles(session)
            assert len(lifecycles) == 2

    async def test_list_active_excludes_terminal(self, session: AsyncSession):
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")
            lc.current_phase = "done"
            await session.flush()
            lifecycles = await lifecycle_service.list_active_lifecycles(session)
            assert len(lifecycles) == 0


class TestPhaseTransitions:
    async def test_start_phase(self, session: AsyncSession):
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")
            record = await lifecycle_service.start_phase(session, lc.id, "development")
            assert record is not None
            assert record.status == PhaseStatus.ACTIVE
            assert record.attempt == 1

    async def test_start_phase_idempotent(self, session: AsyncSession):
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")
            r1 = await lifecycle_service.start_phase(session, lc.id, "development")
            r2 = await lifecycle_service.start_phase(session, lc.id, "development")
            assert r1.id == r2.id

    async def test_complete_phase_advances(self, session: AsyncSession):
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")
            await lifecycle_service.start_phase(session, lc.id, "development")
            ok = await lifecycle_service.complete_phase(session, lc.id, "development", cost=0.50)
            assert ok
            lc = await lifecycle_service.get_lifecycle(session, lc.id)
            assert lc.current_phase == "post_pr"
            assert lc.phase_status == PhaseStatus.PENDING

    async def test_complete_terminal_phase_sets_done(self, session: AsyncSession):
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")
            await lifecycle_service.start_phase(session, lc.id, "post_merge")
            ok = await lifecycle_service.complete_phase(session, lc.id, "post_merge")
            assert ok
            lc = await lifecycle_service.get_lifecycle(session, lc.id)
            assert lc.current_phase == "done"
            assert lc.completed_at is not None

    async def test_fail_phase(self, session: AsyncSession):
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")
            await lifecycle_service.start_phase(session, lc.id, "development")
            ok = await lifecycle_service.fail_phase(session, lc.id, "development", "Tests failed")
            assert ok
            lc = await lifecycle_service.get_lifecycle(session, lc.id)
            assert lc.phase_status == PhaseStatus.FAILED

    async def test_skip_phase(self, session: AsyncSession):
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")
            ok = await lifecycle_service.skip_phase(session, lc.id, "development")
            assert ok
            lc = await lifecycle_service.get_lifecycle(session, lc.id)
            assert lc.current_phase == "post_pr"

    async def test_restart_phase(self, session: AsyncSession):
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")
            await lifecycle_service.start_phase(session, lc.id, "review")
            await lifecycle_service.fail_phase(session, lc.id, "review", "Gate failed")
            record = await lifecycle_service.restart_phase(session, lc.id, "review")
            assert record is not None
            lc = await lifecycle_service.get_lifecycle(session, lc.id)
            assert lc.current_phase == "review"
            assert lc.phase_status == PhaseStatus.PENDING

    async def test_force_advance(self, session: AsyncSession):
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")
            ok = await lifecycle_service.force_advance(session, lc.id, "integrate")
            assert ok
            lc = await lifecycle_service.get_lifecycle(session, lc.id)
            assert lc.current_phase == "integrate"

    async def test_force_advance_to_done(self, session: AsyncSession):
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")
            ok = await lifecycle_service.force_advance(session, lc.id, "done")
            assert ok
            lc = await lifecycle_service.get_lifecycle(session, lc.id)
            assert lc.current_phase == "done"
            assert lc.completed_at is not None

    async def test_abandon_lifecycle(self, session: AsyncSession):
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")
            await lifecycle_service.start_phase(session, lc.id, "development")
            ok = await lifecycle_service.abandon_lifecycle(session, lc.id)
            assert ok
            lc = await lifecycle_service.get_lifecycle(session, lc.id)
            assert lc.current_phase == "abandoned"


class TestLifecycleReconstruction:
    async def test_build_lifecycle_view_from_runs(self, session: AsyncSession):
        async with session.begin():
            run = TaskRun(
                issue_number="42",
                role="developer",
                status="done",
                branch_name="feat/test",
                pr_number=10,
                total_cost_usd=Decimal("0.50"),
                started_at=datetime.now(timezone.utc),
                ended_at=datetime.now(timezone.utc),
            )
            session.add(run)
            await session.flush()

        async with session.begin():
            view = await lifecycle_service.build_lifecycle_view(session, "42")
            assert view is not None
            assert view["reconstructed"] is True
            assert view["pr_number"] == 10
            assert view["branch_name"] == "feat/test"
            assert len(view["phases"]) >= 1

    async def test_build_lifecycle_view_no_runs(self, session: AsyncSession):
        async with session.begin():
            view = await lifecycle_service.build_lifecycle_view(session, "999")
            assert view is None

    async def test_build_lifecycle_view_prefers_real(self, session: AsyncSession):
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")
            await lifecycle_service.start_phase(session, lc.id, "development")

        async with session.begin():
            view = await lifecycle_service.build_lifecycle_view(session, "42")
            assert view is not None
            assert view["reconstructed"] is False
            assert view["id"] == lc.id


class TestLinkTaskRun:
    async def test_link_task_run_to_lifecycle(self, session: AsyncSession):
        async with session.begin():
            run = TaskRun(
                issue_number="42",
                role="developer",
                status="running",
                pr_number=10,
            )
            session.add(run)
            await session.flush()

            lc_id = await lifecycle_service.link_task_run_to_lifecycle(session, run)
            assert lc_id is not None
            assert run.lifecycle_id == lc_id

    async def test_link_unknown_role_returns_none(self, session: AsyncSession):
        async with session.begin():
            run = TaskRun(
                issue_number="42",
                role="unknown_role",
                status="running",
            )
            session.add(run)
            await session.flush()

            lc_id = await lifecycle_service.link_task_run_to_lifecycle(session, run)
            assert lc_id is None


# -- Router tests --------------------------------------------------------------


@pytest.fixture
def app(tmp_path):
    from sova.dashboard.app import create_app

    return create_app(project_dir=tmp_path)


class TestLifecycleRouter:
    async def test_list_active_empty(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/lifecycle/active")
            assert resp.status_code == 200
            data = resp.json()
            assert data["lifecycles"] == []

    async def test_get_by_issue_not_found(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/lifecycle/issue/999")
            assert resp.status_code == 200
            data = resp.json()
            assert "error" in data

    async def test_lifecycle_page_renders(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/lifecycle/42")
            assert resp.status_code == 200
            assert "Issue #42" in resp.text

    async def test_full_lifecycle_flow(self, app, session: AsyncSession):
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")
            lc_id = lc.id

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Get lifecycle
            resp = await client.get(f"/api/lifecycle/{lc_id}")
            assert resp.status_code == 200

            # Start phase
            resp = await client.post(f"/api/lifecycle/{lc_id}/phase/development/start")
            assert resp.status_code == 200
            assert resp.json()["status"] == "started"

            # Skip phase
            resp = await client.post(f"/api/lifecycle/{lc_id}/phase/development/skip")
            assert resp.status_code == 200
            assert resp.json()["status"] == "skipped"

            # Force advance
            resp = await client.post(
                f"/api/lifecycle/{lc_id}/advance",
                json={"to_phase": "integrate"},
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "advanced"

            # Abandon
            resp = await client.post(f"/api/lifecycle/{lc_id}/abandon")
            assert resp.status_code == 200
            assert resp.json()["status"] == "abandoned"
