"""Tests for sova.dashboard.services.lifecycle_service and lifecycle router."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

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

    async def test_start_phase_already_active_links_task_run(self, session: AsyncSession):
        """When phase is already active but missing task_run_id, linking a new one updates it."""
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")
            r1 = await lifecycle_service.start_phase(session, lc.id, "development")
            assert r1.task_run_id is None
            r2 = await lifecycle_service.start_phase(session, lc.id, "development", task_run_id=99)
            assert r2.task_run_id == 99
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


class TestSyntheticMergePhases:
    """Tests for synthesizing integrate/post_merge phases from GitHub PR state."""

    @pytest.fixture(autouse=True)
    def clear_pr_cache(self):
        lifecycle_service._pr_state_cache.clear()
        yield
        lifecycle_service._pr_state_cache.clear()

    async def test_merged_pr_synthesizes_integrate_and_post_merge(self, session: AsyncSession):
        """When PR is merged and no integrate phase exists, synthesize both phases."""
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

        mock_status = AsyncMock()
        mock_status.state = "MERGED"

        with patch("sova.dashboard.services.lifecycle_service.get_pr_status", return_value=mock_status):
            async with session.begin():
                view = await lifecycle_service.build_lifecycle_view(
                    session, "42", github_repo="owner/repo", github_user="testuser"
                )

        assert view is not None
        phase_names = [p["phase"] for p in view["phases"]]
        assert "integrate" in phase_names
        assert "post_merge" in phase_names

        integrate = next(p for p in view["phases"] if p["phase"] == "integrate")
        assert integrate["status"] == "completed"
        assert integrate["source"] == "github"

        post_merge = next(p for p in view["phases"] if p["phase"] == "post_merge")
        assert post_merge["status"] == "completed"
        assert post_merge["source"] == "github"

        assert view["current_phase"] == "done"
        assert view["phase_status"] == "completed"

        # Verify chronological ordering: post_merge after integrate
        assert post_merge["started_at"] > integrate["started_at"]

    async def test_closed_pr_does_not_synthesize(self, session: AsyncSession):
        """Closed but not merged PR should not get synthetic phases."""
        async with session.begin():
            run = TaskRun(
                issue_number="42",
                role="developer",
                status="done",
                pr_number=10,
                started_at=datetime.now(timezone.utc),
                ended_at=datetime.now(timezone.utc),
            )
            session.add(run)
            await session.flush()

        mock_status = AsyncMock()
        mock_status.state = "CLOSED"

        with patch("sova.dashboard.services.lifecycle_service.get_pr_status", return_value=mock_status):
            async with session.begin():
                view = await lifecycle_service.build_lifecycle_view(
                    session, "42", github_repo="owner/repo", github_user="testuser"
                )

        phase_names = [p["phase"] for p in view["phases"]]
        assert "integrate" not in phase_names

    async def test_no_pr_number_skips_check(self, session: AsyncSession):
        """When no PR number is known, skip the merge check entirely."""
        async with session.begin():
            run = TaskRun(
                issue_number="42",
                role="developer",
                status="done",
                started_at=datetime.now(timezone.utc),
                ended_at=datetime.now(timezone.utc),
            )
            session.add(run)
            await session.flush()

        with patch("sova.dashboard.services.lifecycle_service.get_pr_status") as mock_get:
            async with session.begin():
                view = await lifecycle_service.build_lifecycle_view(
                    session, "42", github_repo="owner/repo", github_user="testuser"
                )
            mock_get.assert_not_called()

        phase_names = [p["phase"] for p in view["phases"]]
        assert "integrate" not in phase_names

    async def test_no_github_repo_skips_check(self, session: AsyncSession):
        """When github_repo is not provided, skip the merge check."""
        async with session.begin():
            run = TaskRun(
                issue_number="42",
                role="developer",
                status="done",
                pr_number=10,
                started_at=datetime.now(timezone.utc),
                ended_at=datetime.now(timezone.utc),
            )
            session.add(run)
            await session.flush()

        with patch("sova.dashboard.services.lifecycle_service.get_pr_status") as mock_get:
            async with session.begin():
                await lifecycle_service.build_lifecycle_view(session, "42")
            mock_get.assert_not_called()

    async def test_adapter_error_fails_open(self, session: AsyncSession):
        """When the PR check raises, return phases as-is without synthetic entries."""
        async with session.begin():
            run = TaskRun(
                issue_number="42",
                role="developer",
                status="done",
                pr_number=10,
                started_at=datetime.now(timezone.utc),
                ended_at=datetime.now(timezone.utc),
            )
            session.add(run)
            await session.flush()

        with patch(
            "sova.dashboard.services.lifecycle_service.get_pr_status",
            side_effect=RuntimeError("API error"),
        ):
            async with session.begin():
                view = await lifecycle_service.build_lifecycle_view(
                    session, "42", github_repo="owner/repo", github_user="testuser"
                )

        phase_names = [p["phase"] for p in view["phases"]]
        assert "integrate" not in phase_names

    async def test_db_integrate_phase_takes_precedence(self, session: AsyncSession):
        """If integrate phase already exists from DB, do not add synthetic one."""
        async with session.begin():
            dev_run = TaskRun(
                issue_number="42",
                role="developer",
                status="done",
                pr_number=10,
                started_at=datetime.now(timezone.utc),
                ended_at=datetime.now(timezone.utc),
            )
            integrate_run = TaskRun(
                issue_number="42",
                role="command:integrate-pr",
                status="done",
                pr_number=10,
                started_at=datetime.now(timezone.utc),
                ended_at=datetime.now(timezone.utc),
            )
            session.add(dev_run)
            session.add(integrate_run)
            await session.flush()

        mock_status = AsyncMock()
        mock_status.state = "MERGED"

        with patch("sova.dashboard.services.lifecycle_service.get_pr_status", return_value=mock_status):
            async with session.begin():
                view = await lifecycle_service.build_lifecycle_view(
                    session, "42", github_repo="owner/repo", github_user="testuser"
                )

        integrate_phases = [p for p in view["phases"] if p["phase"] == "integrate"]
        assert len(integrate_phases) == 1
        assert "source" not in integrate_phases[0]

    async def test_cache_prevents_repeated_api_calls(self, session: AsyncSession):
        """Multiple calls within TTL should not re-call the API."""
        async with session.begin():
            run = TaskRun(
                issue_number="42",
                role="developer",
                status="done",
                pr_number=10,
                started_at=datetime.now(timezone.utc),
                ended_at=datetime.now(timezone.utc),
            )
            session.add(run)
            await session.flush()

        mock_status = AsyncMock()
        mock_status.state = "MERGED"

        with patch("sova.dashboard.services.lifecycle_service.get_pr_status", return_value=mock_status) as mock_get:
            async with session.begin():
                await lifecycle_service.build_lifecycle_view(
                    session, "42", github_repo="owner/repo", github_user="testuser"
                )
            async with session.begin():
                await lifecycle_service.build_lifecycle_view(
                    session, "42", github_repo="owner/repo", github_user="testuser"
                )

            assert mock_get.call_count == 1

    async def test_cache_expires_after_ttl(self, session: AsyncSession):
        """After TTL expires, a new API call should be made."""
        async with session.begin():
            run = TaskRun(
                issue_number="42",
                role="developer",
                status="done",
                pr_number=10,
                started_at=datetime.now(timezone.utc),
                ended_at=datetime.now(timezone.utc),
            )
            session.add(run)
            await session.flush()

        mock_status = AsyncMock()
        mock_status.state = "MERGED"

        with patch("sova.dashboard.services.lifecycle_service.get_pr_status", return_value=mock_status) as mock_get:
            async with session.begin():
                await lifecycle_service.build_lifecycle_view(
                    session, "42", github_repo="owner/repo", github_user="testuser"
                )

            # Clear the cache to simulate TTL expiry
            lifecycle_service._pr_state_cache.clear()

            async with session.begin():
                await lifecycle_service.build_lifecycle_view(
                    session, "42", github_repo="owner/repo", github_user="testuser"
                )

            assert mock_get.call_count == 2

    async def test_real_lifecycle_also_gets_synthetic_phases(self, session: AsyncSession):
        """Real lifecycle (not reconstructed) with no integrate phase also benefits."""
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")
            await lifecycle_service.start_phase(session, lc.id, "development")
            await lifecycle_service.complete_phase(session, lc.id, "development")
            # Lifecycle is now at post_pr, no integrate phase
            lc.pr_number = 10
            await session.flush()

        mock_status = AsyncMock()
        mock_status.state = "MERGED"

        with patch("sova.dashboard.services.lifecycle_service.get_pr_status", return_value=mock_status):
            async with session.begin():
                view = await lifecycle_service.build_lifecycle_view(
                    session, "42", github_repo="owner/repo", github_user="testuser"
                )

        phase_names = [p["phase"] for p in view["phases"]]
        assert "integrate" in phase_names
        assert "post_merge" in phase_names

    async def test_cache_key_includes_repo(self, session: AsyncSession):
        """Same PR number in different repos should not collide in cache."""
        async with session.begin():
            run_a = TaskRun(
                issue_number="50",
                role="developer",
                status="done",
                pr_number=10,
                started_at=datetime.now(timezone.utc),
                ended_at=datetime.now(timezone.utc),
            )
            run_b = TaskRun(
                issue_number="51",
                role="developer",
                status="done",
                pr_number=10,
                started_at=datetime.now(timezone.utc),
                ended_at=datetime.now(timezone.utc),
            )
            session.add(run_a)
            session.add(run_b)
            await session.flush()

        merged_status = AsyncMock()
        merged_status.state = "MERGED"
        open_status = AsyncMock()
        open_status.state = "OPEN"

        async def side_effect(pr_number, repo, github_user):
            if repo == "owner/repo-a":
                return merged_status
            return open_status

        with patch(
            "sova.dashboard.services.lifecycle_service.get_pr_status",
            side_effect=side_effect,
        ):
            async with session.begin():
                view_a = await lifecycle_service.build_lifecycle_view(
                    session, "50", github_repo="owner/repo-a", github_user="testuser"
                )
            async with session.begin():
                view_b = await lifecycle_service.build_lifecycle_view(
                    session, "51", github_repo="owner/repo-b", github_user="testuser"
                )

        phases_a = [p["phase"] for p in view_a["phases"]]
        phases_b = [p["phase"] for p in view_b["phases"]]
        assert "integrate" in phases_a
        assert "integrate" not in phases_b

    async def test_abandoned_reconstruction_not_overwritten_by_merge(self, session: AsyncSession):
        """A reconstructed view with terminal-state phase should not have status overwritten."""
        # Create a failed developer run (simulates abandoned work)
        async with session.begin():
            run = TaskRun(
                issue_number="99",
                role="developer",
                status="failed",
                pr_number=10,
                started_at=datetime.now(timezone.utc),
                ended_at=datetime.now(timezone.utc),
            )
            session.add(run)
            await session.flush()

        mock_status = AsyncMock()
        mock_status.state = "MERGED"

        with patch("sova.dashboard.services.lifecycle_service.get_pr_status", return_value=mock_status):
            async with session.begin():
                view = await lifecycle_service.build_lifecycle_view(
                    session, "99", github_repo="owner/repo", github_user="testuser"
                )

        # Synthetic phases are added
        phase_names = [p["phase"] for p in view["phases"]]
        assert "integrate" in phase_names
        assert "post_merge" in phase_names
        # current_phase updates to done since "development" is not terminal
        assert view["current_phase"] == "done"

    async def test_missing_github_user_skips_check(self, session: AsyncSession):
        """When github_user is empty, skip the merge check."""
        async with session.begin():
            run = TaskRun(
                issue_number="42",
                role="developer",
                status="done",
                pr_number=10,
                started_at=datetime.now(timezone.utc),
                ended_at=datetime.now(timezone.utc),
            )
            session.add(run)
            await session.flush()

        with patch("sova.dashboard.services.lifecycle_service.get_pr_status") as mock_get:
            async with session.begin():
                await lifecycle_service.build_lifecycle_view(
                    session, "42", github_repo="owner/repo", github_user=""
                )
            mock_get.assert_not_called()


class TestReconstructionDuplicatePhases:
    """Tests for the reconstruction path when a role appears more than once."""

    async def test_duplicate_developer_runs_update_existing_phase(self, session: AsyncSession):
        """Two developer runs for the same issue should update the existing phase entry."""
        async with session.begin():
            run1 = TaskRun(
                issue_number="42",
                role="developer",
                status="failed",
                branch_name="feat/test",
                total_cost_usd=Decimal("0.30"),
                error_message="Lint failed",
                started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                ended_at=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
            )
            run2 = TaskRun(
                issue_number="42",
                role="developer",
                status="done",
                branch_name="feat/test",
                pr_number=20,
                total_cost_usd=Decimal("0.70"),
                started_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                ended_at=datetime(2026, 1, 2, 1, tzinfo=timezone.utc),
            )
            session.add(run1)
            session.add(run2)
            await session.flush()

        async with session.begin():
            view = await lifecycle_service.build_lifecycle_view(session, "42")

        assert view is not None
        dev_phases = [p for p in view["phases"] if p["phase"] == "development"]
        assert len(dev_phases) == 1
        assert dev_phases[0]["status"] == "completed"
        assert dev_phases[0]["attempt"] == 2
        assert dev_phases[0]["task_run_id"] == run2.id

    async def test_duplicate_run_with_error_message(self, session: AsyncSession):
        """Duplicate run with error_message updates the existing phase's error_message."""
        async with session.begin():
            run1 = TaskRun(
                issue_number="42",
                role="developer",
                status="done",
                started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                ended_at=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
            )
            run2 = TaskRun(
                issue_number="42",
                role="developer",
                status="failed",
                error_message="Tests failed",
                started_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                ended_at=datetime(2026, 1, 2, 1, tzinfo=timezone.utc),
            )
            session.add(run1)
            session.add(run2)
            await session.flush()

        async with session.begin():
            view = await lifecycle_service.build_lifecycle_view(session, "42")

        dev_phases = [p for p in view["phases"] if p["phase"] == "development"]
        assert len(dev_phases) == 1
        assert dev_phases[0]["error_message"] == "Tests failed"

    async def test_agent_resume_role_defaults_to_development(self, session: AsyncSession):
        """command:agent-resume maps to empty string, which falls back to development."""
        async with session.begin():
            run = TaskRun(
                issue_number="42",
                role="command:agent-resume",
                status="done",
                started_at=datetime.now(timezone.utc),
                ended_at=datetime.now(timezone.utc),
            )
            session.add(run)
            await session.flush()

        async with session.begin():
            view = await lifecycle_service.build_lifecycle_view(session, "42")

        assert view is not None
        assert any(p["phase"] == "development" for p in view["phases"])


class TestInferPhaseStatus:
    def test_done_maps_to_completed(self):
        assert lifecycle_service._infer_phase_status("done") == PhaseStatus.COMPLETED

    def test_failed_maps_to_failed(self):
        assert lifecycle_service._infer_phase_status("failed") == PhaseStatus.FAILED

    def test_rejected_maps_to_failed(self):
        assert lifecycle_service._infer_phase_status("rejected") == PhaseStatus.FAILED

    def test_interrupted_maps_to_failed(self):
        assert lifecycle_service._infer_phase_status("interrupted") == PhaseStatus.FAILED

    def test_running_maps_to_active(self):
        assert lifecycle_service._infer_phase_status("running") == PhaseStatus.ACTIVE

    def test_pending_maps_to_active(self):
        assert lifecycle_service._infer_phase_status("pending") == PhaseStatus.ACTIVE


class TestFinalizePhaseFromRun:
    async def test_finalize_success(self, session: AsyncSession):
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")
            run = TaskRun(
                issue_number="42",
                role="developer",
                status="done",
                lifecycle_id=lc.id,
                pr_number=10,
                branch_name="feat/test",
            )
            session.add(run)
            await session.flush()
            await lifecycle_service.start_phase(session, lc.id, "development", task_run_id=run.id)

        async with session.begin():
            await lifecycle_service.finalize_phase_from_run(session, run.id, exit_code=0, cost=0.50)

        async with session.begin():
            lc = await lifecycle_service.get_lifecycle(session, lc.id)
            assert lc.current_phase == "post_pr"
            assert lc.pr_number == 10
            assert lc.branch_name == "feat/test"

    async def test_finalize_failure(self, session: AsyncSession):
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")
            run = TaskRun(
                issue_number="42",
                role="developer",
                status="failed",
                lifecycle_id=lc.id,
                error_message="CI failed",
            )
            session.add(run)
            await session.flush()
            await lifecycle_service.start_phase(session, lc.id, "development", task_run_id=run.id)

        async with session.begin():
            await lifecycle_service.finalize_phase_from_run(session, run.id, exit_code=1)

        async with session.begin():
            lc = await lifecycle_service.get_lifecycle(session, lc.id)
            assert lc.phase_status == PhaseStatus.FAILED

    async def test_finalize_no_run(self, session: AsyncSession):
        """Non-existent run_id is a no-op."""
        async with session.begin():
            await lifecycle_service.finalize_phase_from_run(session, 99999, exit_code=0)

    async def test_finalize_no_lifecycle_id(self, session: AsyncSession):
        """Run without lifecycle_id is a no-op."""
        async with session.begin():
            run = TaskRun(issue_number="42", role="developer", status="done")
            session.add(run)
            await session.flush()

        async with session.begin():
            await lifecycle_service.finalize_phase_from_run(session, run.id, exit_code=0)

    async def test_finalize_unknown_role(self, session: AsyncSession):
        """Run with unmapped role is a no-op."""
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")
            run = TaskRun(
                issue_number="42",
                role="unknown_role",
                status="done",
                lifecycle_id=lc.id,
            )
            session.add(run)
            await session.flush()

        async with session.begin():
            await lifecycle_service.finalize_phase_from_run(session, run.id, exit_code=0)

    async def test_finalize_exception_is_non_fatal(self, session: AsyncSession):
        """Errors during finalization are logged but not raised."""
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")
            run = TaskRun(
                issue_number="42",
                role="developer",
                status="done",
                lifecycle_id=lc.id,
            )
            session.add(run)
            await session.flush()

        with patch.object(lifecycle_service, "complete_phase", side_effect=RuntimeError("DB error")):
            async with session.begin():
                await lifecycle_service.finalize_phase_from_run(session, run.id, exit_code=0)


class TestNoneLifecycleErrorBranches:
    """Cover error paths when lifecycle_id doesn't exist."""

    async def test_start_phase_missing_lifecycle(self, session: AsyncSession):
        async with session.begin():
            result = await lifecycle_service.start_phase(session, 99999, "development")
            assert result is None

    async def test_complete_phase_missing_lifecycle(self, session: AsyncSession):
        async with session.begin():
            result = await lifecycle_service.complete_phase(session, 99999, "development")
            assert result is False

    async def test_complete_phase_no_active_record(self, session: AsyncSession):
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")
            result = await lifecycle_service.complete_phase(session, lc.id, "development")
            assert result is False

    async def test_fail_phase_missing_lifecycle(self, session: AsyncSession):
        async with session.begin():
            result = await lifecycle_service.fail_phase(session, 99999, "development")
            assert result is False

    async def test_fail_phase_no_active_record(self, session: AsyncSession):
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")
            result = await lifecycle_service.fail_phase(session, lc.id, "development")
            assert result is False

    async def test_skip_phase_missing_lifecycle(self, session: AsyncSession):
        async with session.begin():
            result = await lifecycle_service.skip_phase(session, 99999, "development")
            assert result is False

    async def test_restart_phase_missing_lifecycle(self, session: AsyncSession):
        async with session.begin():
            result = await lifecycle_service.restart_phase(session, 99999, "development")
            assert result is None

    async def test_restart_phase_no_failed_record(self, session: AsyncSession):
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")
            result = await lifecycle_service.restart_phase(session, lc.id, "development")
            assert result is None

    async def test_force_advance_missing_lifecycle(self, session: AsyncSession):
        async with session.begin():
            result = await lifecycle_service.force_advance(session, 99999, "integrate")
            assert result is False

    async def test_force_advance_invalid_phase(self, session: AsyncSession):
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")
            result = await lifecycle_service.force_advance(session, lc.id, "nonexistent_phase")
            assert result is False

    async def test_abandon_missing_lifecycle(self, session: AsyncSession):
        async with session.begin():
            result = await lifecycle_service.abandon_lifecycle(session, 99999)
            assert result is False


class TestForceAdvanceIntermediateSkipping:
    async def test_force_advance_skips_intermediate_phases(self, session: AsyncSession):
        """Advancing from development to integrate should skip intermediate phase records."""
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")
            await lifecycle_service.start_phase(session, lc.id, "development")

        async with session.begin():
            lc = await lifecycle_service.get_lifecycle(session, lc.id)
            ok = await lifecycle_service.force_advance(session, lc.id, "integrate")
            assert ok

        async with session.begin():
            lc = await lifecycle_service.get_lifecycle(session, lc.id)
            assert lc.current_phase == "integrate"
            dev_records = await lifecycle_service._find_phase_records(session, lc.id, "development")
            assert all(r.status == PhaseStatus.SKIPPED for r in dev_records)


class TestLinkTaskRunExceptionPath:
    async def test_link_exception_returns_none(self, session: AsyncSession):
        """Exception during lifecycle linking returns None instead of raising."""
        async with session.begin():
            run = TaskRun(
                issue_number="42",
                role="developer",
                status="running",
            )
            session.add(run)
            await session.flush()

        with patch.object(
            lifecycle_service, "get_or_create_lifecycle", side_effect=RuntimeError("DB error")
        ):
            async with session.begin():
                result = await lifecycle_service.link_task_run_to_lifecycle(session, run)
                assert result is None

    async def test_link_no_issue_number(self, session: AsyncSession):
        """Run without issue_number returns None."""
        async with session.begin():
            run = TaskRun(role="developer", status="running")
            session.add(run)
            await session.flush()

            result = await lifecycle_service.link_task_run_to_lifecycle(session, run)
            assert result is None


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

    async def test_link_task_run_with_branch_name(self, session: AsyncSession):
        """Branch name from the run should be propagated to the lifecycle."""
        async with session.begin():
            run = TaskRun(
                issue_number="42",
                role="developer",
                status="running",
                branch_name="feat/my-branch",
            )
            session.add(run)
            await session.flush()

            lc_id = await lifecycle_service.link_task_run_to_lifecycle(session, run)
            assert lc_id is not None
            lc = await lifecycle_service.get_lifecycle(session, lc_id)
            assert lc.branch_name == "feat/my-branch"

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

    async def test_get_by_issue_config_load_failure(self, app, session: AsyncSession):
        """Config load failure should not break the endpoint (covers except branch)."""
        with patch(
            "sova.config.loader.load_config",
            side_effect=ValueError("Bad config"),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/api/lifecycle/issue/999")
                assert resp.status_code == 200
                # Falls through to build_lifecycle_view with empty github_repo/user
                assert "error" in resp.json()

    async def test_get_by_issue_returns_result(self, app, session: AsyncSession):
        """When lifecycle exists, return it."""
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")
            await lifecycle_service.start_phase(session, lc.id, "development")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/lifecycle/issue/42")
            assert resp.status_code == 200
            data = resp.json()
            assert data.get("issue_number") == "42"
            assert "error" not in data

    async def test_get_lifecycle_not_found(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/lifecycle/99999")
            assert resp.status_code == 200
            assert resp.json()["error"] == "Lifecycle not found"

    async def test_start_phase_invalid_phase(self, app, session: AsyncSession):
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(f"/api/lifecycle/{lc.id}/phase/bogus/start")
            assert resp.status_code == 200
            assert "error" in resp.json()

    async def test_start_phase_failed(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/lifecycle/99999/phase/development/start")
            assert resp.status_code == 200
            assert resp.json()["error"] == "Failed to start phase"

    async def test_skip_phase_invalid_phase(self, app, session: AsyncSession):
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(f"/api/lifecycle/{lc.id}/phase/bogus/skip")
            assert resp.status_code == 200
            assert "error" in resp.json()

    async def test_skip_phase_failed(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/lifecycle/99999/phase/development/skip")
            assert resp.status_code == 200
            assert resp.json()["error"] == "Failed to skip phase"

    async def test_restart_phase_invalid_phase(self, app, session: AsyncSession):
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(f"/api/lifecycle/{lc.id}/phase/bogus/restart")
            assert resp.status_code == 200
            assert "error" in resp.json()

    async def test_restart_phase_no_failed(self, app, session: AsyncSession):
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(f"/api/lifecycle/{lc.id}/phase/development/restart")
            assert resp.status_code == 200
            assert resp.json()["error"] == "No failed phase to restart"

    async def test_restart_phase_success(self, app, session: AsyncSession):
        """Successfully restart a previously failed phase."""
        async with session.begin():
            lc = await lifecycle_service.get_or_create_lifecycle(session, "42")
            await lifecycle_service.start_phase(session, lc.id, "development")
            await lifecycle_service.fail_phase(session, lc.id, "development", "CI failed")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(f"/api/lifecycle/{lc.id}/phase/development/restart")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "restart_ready"
            assert data["phase"] == "development"

    async def test_force_advance_failed(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/lifecycle/99999/advance",
                json={"to_phase": "integrate"},
            )
            assert resp.status_code == 200
            assert resp.json()["error"] == "Failed to advance"

    async def test_abandon_failed(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/lifecycle/99999/abandon")
            assert resp.status_code == 200
            assert resp.json()["error"] == "Failed to abandon lifecycle"

    async def test_lifecycle_page_renders(self, app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/lifecycle/42")
            assert resp.status_code == 200
            assert "Issue " in resp.text and "#42" in resp.text

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
