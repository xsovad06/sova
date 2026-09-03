"""Tests for sova.dashboard.services.agent_recovery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sova.db.models import TaskRun
from sova.db.session import close_db, get_session, init_db

# agent_recovery.py resolves `get_session` via a function-local `from sova.db.session
# import get_session` on every call (not a module-level import), so patching
# "sova.db.session.get_session" at its definition module takes effect correctly here,
# unlike the general rule of patching at the importing module's namespace.


@pytest.fixture(autouse=True)
async def setup_db(monkeypatch):
    """Initialize an in-memory DB for agent_recovery tests."""
    monkeypatch.setenv("SOVA_DATABASE_URL", "sqlite+aiosqlite://")
    await init_db(run_migrations=False)
    yield
    await close_db()


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
        from unittest.mock import AsyncMock, MagicMock, patch

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

        mock_queue_status = MagicMock()
        mock_queue_status.in_queue = False
        mock_queue_status.is_merged = False
        mock_queue_status.state = "NOT_QUEUED"

        with (
            patch(
                "sova.dashboard.services.agent_lifecycle._check_pr_merged_on_failure",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "sova.git.merge.get_merge_queue_status",
                new_callable=AsyncMock,
                return_value=mock_queue_status,
            ) as mock_queue_check,
        ):
            interrupted = await recover_stale_runs()

        assert len(interrupted) == 1
        mock_queue_check.assert_awaited_once()

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

    async def test_recover_stale_runs_skips_alive_managed(self) -> None:
        """Alive processes that are managed by the current dashboard are skipped."""
        import os
        from unittest.mock import AsyncMock, MagicMock, patch

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
            await session.flush()
            run_id = run.id

        # Register the run as managed so it is not killed as orphan
        from sova.dashboard.services.agent_pool import _projects

        mock_pa = MagicMock()
        mock_pa.agents = {run_id: MagicMock()}
        _projects["__test__"] = mock_pa
        try:
            with patch("sova.dashboard.services.agent_recovery._kill_process", new_callable=AsyncMock) as mock_kill:
                interrupted = await recover_stale_runs()
            assert len(interrupted) == 0
            mock_kill.assert_not_called()
        finally:
            _projects.pop("__test__", None)

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
        # run_status must reflect the real (non-"done") status so callers like the
        # review-completed gate can refuse to treat this as a finished review.
        assert result["run_status"] == "interrupted"

    async def test_sova_review_verdict_failed_with_findings_exposes_run_status(self) -> None:
        """A reviewer that crashed after writing findings still surfaces a verdict,
        but run_status must be "failed" so callers can gate on run completion."""
        from sova.dashboard.services.agent_recovery import get_sova_review_verdict

        session = await get_session()
        async with session.begin():
            session.add(
                TaskRun(
                    issue_number="107",
                    role="reviewer",
                    status="failed",
                    handoff_json={
                        "next_action": "address_review",
                        "pending_findings": [{"file": "z.py", "severity": 3, "description": "nit"}],
                    },
                    started_at=datetime.now(timezone.utc),
                    ended_at=None,
                )
            )

        result = await get_sova_review_verdict("107")
        assert result["has_sova_review"] is True
        assert result["verdict"] == "revise"
        assert result["run_status"] == "failed"

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


class TestZombieProcessIdentity:
    """Tests for _is_zombie_process and _kill_process."""

    def test_is_zombie_process_returns_true_for_matching_pid(self) -> None:
        """Process created before the run started is treated as the expected agent."""
        from datetime import datetime, timedelta, timezone
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services.agent_recovery import _is_zombie_process

        run_start = datetime.now(timezone.utc)
        mock_proc = MagicMock()
        mock_proc.create_time.return_value = (run_start - timedelta(seconds=10)).timestamp()

        with patch("psutil.Process", return_value=mock_proc):
            assert _is_zombie_process(12345, run_start) is True

    def test_is_zombie_process_detects_recycled_pid(self) -> None:
        """Process created well after the run started indicates PID reuse."""
        from datetime import datetime, timedelta, timezone
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services.agent_recovery import _is_zombie_process

        run_start = datetime.now(timezone.utc) - timedelta(hours=2)
        mock_proc = MagicMock()
        mock_proc.create_time.return_value = (run_start + timedelta(minutes=30)).timestamp()

        with patch("psutil.Process", return_value=mock_proc):
            assert _is_zombie_process(12345, run_start) is False

    def test_is_zombie_process_falls_back_on_psutil_error(self) -> None:
        """When psutil fails, fall back to basic liveness check."""
        from datetime import datetime, timezone
        from unittest.mock import patch

        from sova.dashboard.services.agent_recovery import _is_zombie_process

        with patch("psutil.Process", side_effect=Exception("no psutil")):
            with patch("sova.dashboard.services.agent_recovery._is_process_alive", return_value=True):
                assert _is_zombie_process(12345, datetime.now(timezone.utc)) is True

    def test_is_zombie_process_with_none_started_at(self) -> None:
        """When started_at is None, skip time comparison and return True for alive PIDs."""
        from unittest.mock import MagicMock, patch

        from sova.dashboard.services.agent_recovery import _is_zombie_process

        mock_proc = MagicMock()
        mock_proc.create_time.return_value = 1000000.0

        with patch("psutil.Process", return_value=mock_proc):
            assert _is_zombie_process(12345, None) is True

    async def test_kill_process_sends_sigterm_then_sigkill(self) -> None:
        """Verify the SIGTERM -> sleep -> SIGKILL escalation."""
        from unittest.mock import call, patch

        from sova.dashboard.services.agent_recovery import _kill_process

        with (
            patch("os.kill") as mock_kill,
            patch("asyncio.sleep") as mock_sleep,
        ):
            import signal

            mock_kill.side_effect = [None, None, None]  # SIGTERM, alive check, SIGKILL
            await _kill_process(99999)
            assert mock_kill.call_args_list[0] == call(99999, signal.SIGTERM)
            assert mock_kill.call_args_list[1] == call(99999, 0)
            assert mock_kill.call_args_list[2] == call(99999, signal.SIGKILL)
            mock_sleep.assert_awaited_once_with(2)

    async def test_kill_process_returns_if_process_gone(self) -> None:
        """When SIGTERM raises OSError, the function returns without SIGKILL."""
        from unittest.mock import patch

        from sova.dashboard.services.agent_recovery import _kill_process

        with patch("os.kill", side_effect=OSError("no such process")):
            await _kill_process(99999)  # should not raise


class TestTerminalZombieRecovery:
    """Tests for _kill_terminal_zombies and orphan detection."""

    async def test_kill_terminal_zombies_kills_alive_process(self) -> None:
        """Terminal runs with alive PIDs should be killed."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_recovery import _kill_terminal_zombies

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="900", role="dev", status="done", pid=999999)
            session.add(run)

        with (
            patch("sova.dashboard.services.agent_recovery._is_zombie_process", return_value=True),
            patch("sova.dashboard.services.agent_recovery._kill_process", new_callable=AsyncMock) as mock_kill,
        ):
            killed = await _kill_terminal_zombies()

        assert killed >= 1
        mock_kill.assert_any_call(999999)

    async def test_kill_terminal_zombies_skips_dead_process(self) -> None:
        """Terminal runs with dead PIDs should not be killed."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_recovery import _kill_terminal_zombies

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="901", role="dev", status="done", pid=999999)
            session.add(run)

        with (
            patch("sova.dashboard.services.agent_recovery._is_zombie_process", return_value=False),
            patch("sova.dashboard.services.agent_recovery._kill_process", new_callable=AsyncMock) as mock_kill,
        ):
            await _kill_terminal_zombies()

        assert 999999 not in [c.args[0] for c in mock_kill.call_args_list]

    async def test_kill_terminal_zombies_skips_managed(self) -> None:
        """Terminal runs that are managed by the current dashboard should be skipped."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_pool import _projects
        from sova.dashboard.services.agent_recovery import _kill_terminal_zombies

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="902", role="dev", status="done", pid=999999)
            session.add(run)
            await session.flush()
            run_id = run.id

        mock_pa = MagicMock()
        mock_pa.agents = {run_id: MagicMock()}
        _projects["__test__"] = mock_pa
        try:
            with (
                patch("sova.dashboard.services.agent_recovery._is_zombie_process", return_value=True),
                patch("sova.dashboard.services.agent_recovery._kill_process", new_callable=AsyncMock) as mock_kill,
            ):
                await _kill_terminal_zombies()
            assert 999999 not in [c.args[0] for c in mock_kill.call_args_list]
        finally:
            _projects.pop("__test__", None)

    async def test_recover_stale_runs_kills_orphan(self) -> None:
        """Alive processes not managed by current dashboard should be killed."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.agent_recovery import recover_stale_runs

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="903", role="dev", status="running", pid=999999)
            session.add(run)
            await session.flush()
            run_id = run.id

        with (
            patch("sova.dashboard.services.agent_recovery._is_zombie_process", return_value=True),
            patch("sova.dashboard.services.agent_recovery._kill_process", new_callable=AsyncMock) as mock_kill,
        ):
            interrupted = await recover_stale_runs()

        assert any(r["run_id"] == run_id for r in interrupted)
        mock_kill.assert_any_call(999999)


class TestParseVerdictFromOutput:
    """_parse_verdict_from_output should extract verdicts from agent text output."""

    def test_approve_verdict(self) -> None:
        from sova.dashboard.services.agent_recovery import _parse_verdict_from_output

        lines = ["Some analysis...", "### Verdict", "**Approve**: no blockers"]
        assert _parse_verdict_from_output(lines) == "approve"

    def test_request_changes_verdict(self) -> None:
        from sova.dashboard.services.agent_recovery import _parse_verdict_from_output

        lines = ["Analysis...", "**Verdict: Request changes**: HIGH finding must be fixed"]
        assert _parse_verdict_from_output(lines) == "revise"

    def test_comment_only_verdict(self) -> None:
        from sova.dashboard.services.agent_recovery import _parse_verdict_from_output

        lines = ["Summary...", "Verdict: Comment only: observations"]
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
            session.add(OutputLine(task_run_id=run_id, line_number=2, text="**Verdict: Approve**: looks good"))

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
            session.add(OutputLine(task_run_id=run_id, line_number=2, text="Verdict: Request changes: fix the bug"))

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


class TestReviewerPostFailureVerdict:
    """Tests for the reviewer post-failed signal and verdict propagation."""

    async def test_sova_review_verdict_post_failed_returns_post_failed_not_revise(self) -> None:
        """When reviewer handoff has next_action=review_post_failed, verdict is post_failed not revise."""
        from sova.dashboard.services.agent_recovery import get_sova_review_verdict

        session = await get_session()
        async with session.begin():
            session.add(
                TaskRun(
                    issue_number="400",
                    role="reviewer",
                    status="done",
                    handoff_json={"next_action": "review_post_failed", "pending_findings": [], "cost_usd": "0.01"},
                    ended_at=datetime.now(timezone.utc),
                )
            )

        result = await get_sova_review_verdict("400")
        assert result["has_sova_review"] is True
        assert result["verdict"] == "post_failed"
        assert result["finding_count"] == 0

    async def test_sova_review_verdict_post_failed_with_findings_is_still_post_failed(self) -> None:
        """next_action=review_post_failed takes precedence even when findings are present."""
        from sova.dashboard.services.agent_recovery import get_sova_review_verdict

        session = await get_session()
        async with session.begin():
            session.add(
                TaskRun(
                    issue_number="401",
                    role="reviewer",
                    status="done",
                    handoff_json={
                        "next_action": "review_post_failed",
                        "pending_findings": [{"file": "a.py", "severity": 9, "description": "bug"}],
                    },
                    ended_at=datetime.now(timezone.utc),
                )
            )

        result = await get_sova_review_verdict("401")
        assert result["has_sova_review"] is True
        assert result["verdict"] == "post_failed"

    async def test_process_auto_handoff_no_spawn_for_post_failed(self) -> None:
        """_process_auto_handoff must not spawn any agent when all actions have auto_execute=False."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_handoff import _process_auto_handoff
        from sova.ipc.handoff import DashboardHandoff, HandoffAction

        agent = type(
            "AgentState",
            (),
            {"run_id": None, "issue": "402", "project_dir": Path("/tmp/test")},
        )()

        handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue="402",
            pr_number=77,
            summary="Review post failed: all API attempts failed",
            details={"next_action": "review_post_failed", "cost_usd": "0.02", "pending_findings": []},
            next_actions=[
                HandoffAction(
                    id="rerun_review",
                    label="Re-run Review",
                    description="Retry posting the review to GitHub",
                    style="neutral",
                    mode="agent",
                    args={"issue": "402", "role": "reviewer", "pr": 77},
                    auto_execute=False,
                ),
            ],
        )

        mock_start = AsyncMock()
        mock_clear = MagicMock()
        with (
            patch("sova.ipc.handoff.read_handoff_file", return_value=handoff),
            patch("sova.dashboard.services.agent_lifecycle.start_agent", mock_start),
            patch("sova.dashboard.services.handoff_service.clear_handoff", mock_clear),
        ):
            await _process_auto_handoff(agent)

        mock_start.assert_not_awaited()
        mock_clear.assert_not_called()

    async def test_post_failed_handoff_shows_rerun_review_not_address_review(self) -> None:
        """_write_handoff with post_failed=True must write rerun_review action, not address_review.

        Drives production code directly via ReviewerRole._write_handoff so the test cannot pass
        by asserting on its own construction.
        """
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.roles.reviewer import ReviewerRole, ReviewResult

        ctx = MagicMock()
        ctx.issue_number = "403"
        ctx.pr_number = 88
        ctx.branch_name = "fix/test"
        ctx.task_run_id = None
        ctx.config.pipeline.auto_address_review = True

        role = ReviewerRole()
        review = ReviewResult(post_failed=True)

        with (
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock),
            patch("sova.roles.reviewer.write_handoff_file") as mock_file_handoff,
        ):
            await role._write_handoff(ctx, review)

        file_handoff = mock_file_handoff.call_args[0][1]
        action_ids = [a.id for a in file_handoff.next_actions]
        auto_flags = [a.auto_execute for a in file_handoff.next_actions]

        assert "rerun_review" in action_ids
        assert "address_review" not in action_ids
        assert all(flag is False for flag in auto_flags), "no action must auto-execute on post failure"

    async def test_post_failed_verdict_does_not_trigger_pr_sova_changes_state(self) -> None:
        """post_failed verdict on an integrate-bound state must return PR_AWAITING_REVIEW.

        Without the post_failed guard, PR_APPROVED stays PR_APPROVED and shows "Integrate PR"
        on the dashboard even though the review never posted.
        """
        from sova.dashboard.services.work_item_service import WorkItemState, _apply_sova_verdict

        sova_verdict = {
            "has_sova_review": True,
            "verdict": "post_failed",
            "finding_count": 0,
            "reviewed_at": "2026-07-26T12:00:00Z",
        }
        # PR_APPROVED is the critical case: without the guard it stays PR_APPROVED
        # and the dashboard shows "Integrate PR" for a review that never posted.
        mapped = WorkItemState.PR_APPROVED
        result = _apply_sova_verdict(mapped, sova_verdict, external_reviews_enabled=True)

        assert result == WorkItemState.PR_AWAITING_REVIEW, (
            f"post_failed verdict must demote integrate-bound state to PR_AWAITING_REVIEW, got {result}"
        )


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


class TestGetRecoveryConfig:
    def test_returns_repo_and_user(self, monkeypatch) -> None:
        from sova.dashboard.services.agent_recovery import _get_recovery_config

        mock_cfg = type("Cfg", (), {"github_repo": "user/repo", "github_user": "alice"})()
        monkeypatch.setattr("sova.config.loader.load_config", lambda _: mock_cfg)

        repo, user = _get_recovery_config(None)
        assert repo == "user/repo"
        assert user == "alice"

    def test_missing_fields_fall_back_to_empty_string(self, monkeypatch) -> None:
        from sova.dashboard.services.agent_recovery import _get_recovery_config

        mock_cfg = type("Cfg", (), {"github_repo": None, "github_user": None})()
        monkeypatch.setattr("sova.config.loader.load_config", lambda _: mock_cfg)

        repo, user = _get_recovery_config(None)
        assert repo == ""
        assert user == ""

    def test_load_config_exception_returns_empty_tuple(self, monkeypatch) -> None:
        from sova.dashboard.services.agent_recovery import _get_recovery_config

        monkeypatch.setattr("sova.config.loader.load_config", lambda _: (_ for _ in ()).throw(RuntimeError("boom")))

        assert _get_recovery_config(None) == ("", "")


class TestGetPrBranchForRecovery:
    async def test_no_repo_returns_empty_string(self) -> None:
        from sova.dashboard.services.agent_recovery import _get_pr_branch_for_recovery

        result = await _get_pr_branch_for_recovery(1, "", None)
        assert result == ""

    async def test_returns_branch_on_success(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services import agent_recovery

        monkeypatch.setattr(agent_recovery, "_get_recovery_config", lambda _: ("user/repo", "alice"))
        monkeypatch.setattr("sova.utils.gh.resolve_gh_env", AsyncMock(return_value={}))
        monkeypatch.setattr(
            "sova.utils.shell.run",
            AsyncMock(return_value=type("Result", (), {"success": True, "stdout": "feat/issue-42\n"})()),
        )

        result = await agent_recovery._get_pr_branch_for_recovery(42, "user/repo", None)
        assert result == "feat/issue-42"

    async def test_returns_empty_string_on_failed_command(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services import agent_recovery

        monkeypatch.setattr(agent_recovery, "_get_recovery_config", lambda _: ("user/repo", "alice"))
        monkeypatch.setattr("sova.utils.gh.resolve_gh_env", AsyncMock(return_value={}))
        monkeypatch.setattr(
            "sova.utils.shell.run",
            AsyncMock(return_value=type("Result", (), {"success": False, "stdout": ""})()),
        )

        result = await agent_recovery._get_pr_branch_for_recovery(42, "user/repo", None)
        assert result == ""

    async def test_returns_empty_string_on_exception(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services import agent_recovery

        monkeypatch.setattr(agent_recovery, "_get_recovery_config", lambda _: ("user/repo", "alice"))
        monkeypatch.setattr("sova.utils.gh.resolve_gh_env", AsyncMock(side_effect=RuntimeError("network error")))

        result = await agent_recovery._get_pr_branch_for_recovery(42, "user/repo", None)
        assert result == ""


class TestGetManagedRunIds:
    def test_returns_run_ids_from_all_projects(self, monkeypatch) -> None:
        from sova.dashboard.services import agent_pool
        from sova.dashboard.services.agent_recovery import _get_managed_run_ids

        pa1 = agent_pool.ProjectAgents()
        pa1.agents[1] = object()
        pa1.agents[2] = object()
        pa2 = agent_pool.ProjectAgents()
        pa2.agents[3] = object()

        monkeypatch.setattr(agent_pool, "_projects", {"proj1": pa1, "proj2": pa2})

        ids, loaded_ok = _get_managed_run_ids()
        assert loaded_ok is True
        assert ids == {1, 2, 3}

    def test_returns_empty_and_not_loaded_on_exception(self, monkeypatch) -> None:
        from sova.dashboard.services import agent_pool
        from sova.dashboard.services.agent_recovery import _get_managed_run_ids

        class BadDict(dict):
            def values(self):
                raise RuntimeError("boom")

        monkeypatch.setattr(agent_pool, "_projects", BadDict())

        ids, loaded_ok = _get_managed_run_ids()
        assert ids == set()
        assert loaded_ok is False


class TestGetInterruptedRuns:
    async def test_returns_interrupted_runs(self) -> None:
        from sova.dashboard.services.agent_recovery import get_interrupted_runs

        session = await get_session()
        async with session.begin():
            session.add(TaskRun(issue_number="80", role="developer", status="interrupted"))
            session.add(TaskRun(issue_number="81", role="developer", status="done"))

        runs = await get_interrupted_runs()
        assert len(runs) == 1
        assert runs[0]["issue_number"] == "80"

    async def test_respects_limit(self) -> None:
        from sova.dashboard.services.agent_recovery import get_interrupted_runs

        session = await get_session()
        async with session.begin():
            for i in range(3):
                session.add(TaskRun(issue_number=str(90 + i), role="developer", status="interrupted"))

        runs = await get_interrupted_runs(limit=2)
        assert len(runs) == 2

    async def test_returns_empty_list_on_exception(self, monkeypatch) -> None:
        from sova.dashboard.services import run_service
        from sova.dashboard.services.agent_recovery import get_interrupted_runs

        async def _boom(*a, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(run_service, "list_runs", _boom)

        runs = await get_interrupted_runs()
        assert runs == []


class TestInterpretReviews:
    def test_ignores_dismissed_and_bot_reviews(self) -> None:
        from sova.adapters.base import PRReview
        from sova.dashboard.services.agent_recovery import _interpret_reviews

        reviews = {
            "alice": PRReview(reviewer="alice", state="DISMISSED", body="", submitted_at="t", is_bot=False),
            "coderabbit": PRReview(
                reviewer="coderabbit", state="CHANGES_REQUESTED", body="", submitted_at="t", is_bot=True
            ),
        }

        has_changes, approvals, count = _interpret_reviews(reviews)
        assert has_changes is False
        assert approvals == 0
        assert count == 0

    def test_counts_changes_requested_and_approvals(self) -> None:
        from sova.adapters.base import PRReview
        from sova.dashboard.services.agent_recovery import _interpret_reviews

        reviews = {
            "alice": PRReview(reviewer="alice", state="CHANGES_REQUESTED", body="", submitted_at="t", is_bot=False),
            "bob": PRReview(reviewer="bob", state="APPROVED", body="", submitted_at="t", is_bot=False),
            "carol": PRReview(reviewer="carol", state="COMMENTED", body="", submitted_at="t", is_bot=False),
        }

        has_changes, approvals, count = _interpret_reviews(reviews)
        assert has_changes is True
        assert approvals == 1
        assert count == 3


class TestBuildReviewActions:
    def test_build_address_review_action_shape(self) -> None:
        from sova.dashboard.services.agent_recovery import _build_address_review_action

        actions = _build_address_review_action("42", 99)
        assert len(actions) == 1
        action = actions[0]
        assert action["id"] == "address_review"
        assert action["mode"] == "agent"
        assert action["auto_execute"] is False
        assert action["args"] == {"issue": "42", "pr": 99, "role": "developer"}

    def test_build_integrate_actions_shape(self) -> None:
        from sova.dashboard.services.agent_recovery import _build_integrate_actions

        actions = _build_integrate_actions("42", 99)
        assert len(actions) == 1
        action = actions[0]
        assert action["id"] == "integrate"
        assert action["mode"] == "claude-command"
        assert action["command"] == "/integrate-pr 99"
        assert action["args"] == {"issue": "42", "pr": 99}


class TestRollbackIssueState:
    async def test_missing_run_returns_without_error(self) -> None:
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        await rollback_issue_state(999999)  # no such run id; should not raise

    async def test_missing_issue_number_skips_rollback(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_recovery import rollback_issue_state

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number=None, role="developer", status="failed")
            session.add(run)
            await session.flush()
            run_id = run.id

        mock_create_adapter = AsyncMock()
        monkeypatch.setattr("sova.adapters.create_adapter", mock_create_adapter)

        await rollback_issue_state(run_id)
        mock_create_adapter.assert_not_called()

    async def test_skips_when_concurrent_run_exists(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_recovery import rollback_issue_state

        session = await get_session()
        async with session.begin():
            failed_run = TaskRun(issue_number="70", role="developer", status="failed")
            other_run = TaskRun(issue_number="70", role="developer", status="developing")
            session.add_all([failed_run, other_run])
            await session.flush()
            run_id = failed_run.id

        mock_create_adapter = AsyncMock()
        monkeypatch.setattr("sova.adapters.create_adapter", mock_create_adapter)

        await rollback_issue_state(run_id)
        mock_create_adapter.assert_not_called()

    async def test_triage_role_removes_agent_labels(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.adapters.base import Task
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="71", role="triage", status="failed")
            session.add(run)
            await session.flush()
            run_id = run.id

        mock_adapter = AsyncMock()
        mock_adapter.get_task.return_value = Task(
            id="71", title="t", labels=["agent:triaged", "type:bug", "agent:in-progress"]
        )
        monkeypatch.setattr("sova.config.loader.load_config", lambda _: object())
        monkeypatch.setattr("sova.adapters.create_adapter", lambda _: mock_adapter)

        await rollback_issue_state(run_id)

        assert mock_adapter.remove_label.await_count == 2
        removed = {c.args[1] for c in mock_adapter.remove_label.await_args_list}
        assert removed == {"agent:triaged", "agent:in-progress"}
        mock_adapter.transition_state.assert_not_called()

    async def test_developer_with_pr_rolls_back_to_in_review(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.adapters.base import TaskState
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="72", role="developer", status="failed", pr_number=88)
            session.add(run)
            await session.flush()
            run_id = run.id

        mock_adapter = AsyncMock()
        monkeypatch.setattr("sova.config.loader.load_config", lambda _: object())
        monkeypatch.setattr("sova.adapters.create_adapter", lambda _: mock_adapter)

        await rollback_issue_state(run_id)

        mock_adapter.transition_state.assert_awaited_once_with("72", TaskState.IN_REVIEW)

    async def test_developer_without_pr_rolls_back_to_researched(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.adapters.base import TaskState
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="73", role="developer", status="failed")
            session.add(run)
            await session.flush()
            run_id = run.id

        mock_adapter = AsyncMock()
        monkeypatch.setattr("sova.config.loader.load_config", lambda _: object())
        monkeypatch.setattr("sova.adapters.create_adapter", lambda _: mock_adapter)

        await rollback_issue_state(run_id)

        mock_adapter.transition_state.assert_awaited_once_with("73", TaskState.RESEARCHED)

    async def test_command_prefix_role_is_parsed(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.adapters.base import TaskState
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="74", role="command:address-pr", status="failed")
            session.add(run)
            await session.flush()
            run_id = run.id

        mock_adapter = AsyncMock()
        monkeypatch.setattr("sova.config.loader.load_config", lambda _: object())
        monkeypatch.setattr("sova.adapters.create_adapter", lambda _: mock_adapter)

        await rollback_issue_state(run_id)

        mock_adapter.transition_state.assert_awaited_once_with("74", TaskState.IN_REVIEW)

    async def test_unknown_role_does_not_transition(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_recovery import rollback_issue_state

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="75", role="planner", status="failed")
            session.add(run)
            await session.flush()
            run_id = run.id

        mock_adapter = AsyncMock()
        monkeypatch.setattr("sova.config.loader.load_config", lambda _: object())
        monkeypatch.setattr("sova.adapters.create_adapter", lambda _: mock_adapter)

        await rollback_issue_state(run_id)

        mock_adapter.transition_state.assert_not_called()

    async def test_adapter_exception_is_swallowed(self, monkeypatch) -> None:
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        session = await get_session()
        async with session.begin():
            run = TaskRun(issue_number="76", role="developer", status="failed")
            session.add(run)
            await session.flush()
            run_id = run.id

        monkeypatch.setattr("sova.config.loader.load_config", lambda _: (_ for _ in ()).throw(RuntimeError("boom")))

        await rollback_issue_state(run_id)  # must not raise
