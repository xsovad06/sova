"""Tests for PR creation throttle service -- queue, process, dequeue, recovery."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from sova.config.models import CodeRabbitQuotaConfig
from sova.db.session import close_db, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    """Initialize a fresh in-memory DB for each test."""
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


async def _create_task_run() -> int:
    """Helper to create a TaskRun and return its ID."""
    from sova.db.models import TaskRun
    from sova.db.session import get_session

    async with await get_session() as session:
        async with session.begin():
            tr = TaskRun(issue_number="42", role="developer", status="running")
            session.add(tr)
            await session.flush()
            return tr.id


# ---------------------------------------------------------------------------
# Enqueue tests
# ---------------------------------------------------------------------------


class TestEnqueue:
    async def test_enqueue_creates_entry(self) -> None:
        from sova.db.models import PRCreationQueue
        from sova.db.session import get_session
        from sova.supervisor.pr_throttle import enqueue

        run_id = await _create_task_run()
        async with await get_session() as session:
            async with session.begin():
                entry_id = await enqueue(
                    session,
                    task_run_id=run_id,
                    issue_number="42",
                    title="feat(#42): test PR",
                    body="PR body",
                    base_branch="main",
                    head_branch="feat/test",
                    repo="owner/repo",
                    github_user="testuser",
                )

        async with await get_session() as session:
            entry = await session.get(PRCreationQueue, entry_id)
        assert entry is not None
        assert entry.status == "pending"
        assert entry.task_run_id == run_id
        assert entry.title == "feat(#42): test PR"
        assert entry.head_branch == "feat/test"

    async def test_multiple_enqueue_creates_separate_entries(self) -> None:
        from sova.db.session import get_session
        from sova.supervisor.pr_throttle import enqueue

        run_id1 = await _create_task_run()
        run_id2 = await _create_task_run()

        async with await get_session() as session:
            async with session.begin():
                id1 = await enqueue(
                    session,
                    task_run_id=run_id1,
                    issue_number="1",
                    title="PR 1",
                    body="body",
                    base_branch="main",
                    head_branch="feat/1",
                )
                id2 = await enqueue(
                    session,
                    task_run_id=run_id2,
                    issue_number="2",
                    title="PR 2",
                    body="body",
                    base_branch="main",
                    head_branch="feat/2",
                )
        assert id1 != id2


# ---------------------------------------------------------------------------
# Dequeue tests
# ---------------------------------------------------------------------------


class TestDequeue:
    async def test_dequeue_pending_entry(self) -> None:
        from sova.db.models import PRCreationQueue
        from sova.db.session import get_session
        from sova.supervisor.pr_throttle import dequeue, enqueue

        run_id = await _create_task_run()
        async with await get_session() as session:
            async with session.begin():
                entry_id = await enqueue(
                    session,
                    task_run_id=run_id,
                    issue_number="42",
                    title="PR",
                    body="body",
                    base_branch="main",
                    head_branch="feat/x",
                )

        async with await get_session() as session:
            async with session.begin():
                result = await dequeue(session, task_run_id=run_id)
        assert result is True

        async with await get_session() as session:
            entry = await session.get(PRCreationQueue, entry_id)
        assert entry.status == "cancelled"

    async def test_dequeue_no_pending_entry(self) -> None:
        from sova.db.session import get_session
        from sova.supervisor.pr_throttle import dequeue

        run_id = await _create_task_run()
        async with await get_session() as session:
            async with session.begin():
                result = await dequeue(session, task_run_id=run_id)
        assert result is False

    async def test_dequeue_already_created_is_noop(self) -> None:
        from sova.db.models import PRCreationQueue
        from sova.db.session import get_session
        from sova.supervisor.pr_throttle import enqueue

        run_id = await _create_task_run()
        async with await get_session() as session:
            async with session.begin():
                entry_id = await enqueue(
                    session,
                    task_run_id=run_id,
                    issue_number="42",
                    title="PR",
                    body="body",
                    base_branch="main",
                    head_branch="feat/x",
                )
                # Simulate already created
                entry = await session.get(PRCreationQueue, entry_id)
                entry.status = "created"

        from sova.supervisor.pr_throttle import dequeue

        async with await get_session() as session:
            async with session.begin():
                result = await dequeue(session, task_run_id=run_id)
        assert result is False


# ---------------------------------------------------------------------------
# Process queue tests
# ---------------------------------------------------------------------------


class TestProcessQueue:
    async def test_process_empty_queue(self) -> None:
        from sova.db.session import get_session
        from sova.supervisor.pr_throttle import process_queue

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        async with await get_session() as session:
            count = await process_queue(session, cfg)
        assert count == 0

    async def test_process_creates_pr_when_quota_available(self) -> None:
        from sova.db.models import PRCreationQueue, TaskRun
        from sova.db.session import get_session
        from sova.git.pr import PRInfo
        from sova.supervisor.pr_throttle import enqueue, process_queue

        run_id = await _create_task_run()
        async with await get_session() as session:
            async with session.begin():
                entry_id = await enqueue(
                    session,
                    task_run_id=run_id,
                    issue_number="42",
                    title="feat(#42): test",
                    body="body",
                    base_branch="main",
                    head_branch="feat/42",
                    repo="owner/repo",
                    github_user="user",
                )

        mock_pr = PRInfo(number=99, url="https://github.com/owner/repo/pull/99")
        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        with (
            patch("sova.git.operations.create_pr", new_callable=AsyncMock, return_value=mock_pr),
            patch("sova.supervisor.pr_throttle.run_post_create_side_effects", new_callable=AsyncMock),
        ):
            async with await get_session() as session:
                count = await process_queue(session, cfg)
        assert count == 1

        async with await get_session() as session:
            entry = await session.get(PRCreationQueue, entry_id)
            task_run = await session.get(TaskRun, run_id)
        assert entry.status == "created"
        assert entry.pr_number == 99
        assert task_run.pr_number == 99

    async def test_process_blocked_by_quota(self) -> None:
        from sova.db.session import get_session
        from sova.supervisor.coderabbit_quota import record_event
        from sova.supervisor.pr_throttle import enqueue, process_queue

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        now = datetime.now(timezone.utc)

        # Fill quota
        async with await get_session() as session:
            for i in range(4):
                await record_event(
                    session,
                    pr_number=i + 1,
                    event_type="review",
                    review_id=f"r{i}",
                    recorded_at=now,
                )

        run_id = await _create_task_run()
        async with await get_session() as session:
            async with session.begin():
                await enqueue(
                    session,
                    task_run_id=run_id,
                    issue_number="42",
                    title="PR",
                    body="body",
                    base_branch="main",
                    head_branch="feat/42",
                )

        async with await get_session() as session:
            count = await process_queue(session, cfg)
        assert count == 0

    async def test_stale_entries_cancelled(self) -> None:
        from sova.db.models import PRCreationQueue
        from sova.db.session import get_session
        from sova.supervisor.pr_throttle import enqueue, process_queue

        run_id = await _create_task_run()
        async with await get_session() as session:
            async with session.begin():
                entry_id = await enqueue(
                    session,
                    task_run_id=run_id,
                    issue_number="42",
                    title="PR",
                    body="body",
                    base_branch="main",
                    head_branch="feat/42",
                )
                # Backdate the entry past the max age
                entry = await session.get(PRCreationQueue, entry_id)
                entry.enqueued_at = datetime.now(timezone.utc) - timedelta(hours=3)

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        async with await get_session() as session:
            await process_queue(session, cfg)

        async with await get_session() as session:
            entry = await session.get(PRCreationQueue, entry_id)
        assert entry.status == "cancelled"
        assert "max queue age" in entry.error_message

    async def test_create_failure_marks_failed(self) -> None:
        from sova.db.models import PRCreationQueue
        from sova.db.session import get_session
        from sova.supervisor.pr_throttle import enqueue, process_queue

        run_id = await _create_task_run()
        async with await get_session() as session:
            async with session.begin():
                entry_id = await enqueue(
                    session,
                    task_run_id=run_id,
                    issue_number="42",
                    title="PR",
                    body="body",
                    base_branch="main",
                    head_branch="feat/42",
                    repo="owner/repo",
                )

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        with patch(
            "sova.git.operations.create_pr",
            new_callable=AsyncMock,
            side_effect=RuntimeError("API error"),
        ):
            async with await get_session() as session:
                count = await process_queue(session, cfg)
        assert count == 0

        async with await get_session() as session:
            entry = await session.get(PRCreationQueue, entry_id)
        assert entry.status == "failed"
        assert "API error" in entry.error_message


# ---------------------------------------------------------------------------
# Recovery tests
# ---------------------------------------------------------------------------


class TestRecoverCreatingEntries:
    async def test_recover_creating_to_pending(self) -> None:
        from sova.db.models import PRCreationQueue
        from sova.db.session import get_session
        from sova.supervisor.pr_throttle import enqueue, recover_creating_entries

        run_id = await _create_task_run()
        async with await get_session() as session:
            async with session.begin():
                entry_id = await enqueue(
                    session,
                    task_run_id=run_id,
                    issue_number="42",
                    title="PR",
                    body="body",
                    base_branch="main",
                    head_branch="feat/42",
                )
                entry = await session.get(PRCreationQueue, entry_id)
                entry.status = "creating"

        async with await get_session() as session:
            async with session.begin():
                count = await recover_creating_entries(session)
        assert count == 1

        async with await get_session() as session:
            entry = await session.get(PRCreationQueue, entry_id)
        assert entry.status == "pending"

    async def test_no_creating_entries(self) -> None:
        from sova.db.session import get_session
        from sova.supervisor.pr_throttle import recover_creating_entries

        async with await get_session() as session:
            async with session.begin():
                count = await recover_creating_entries(session)
        assert count == 0


# ---------------------------------------------------------------------------
# Get queue entry status tests
# ---------------------------------------------------------------------------


class TestGetQueueEntryStatus:
    async def test_returns_status(self) -> None:
        from sova.db.session import get_session
        from sova.supervisor.pr_throttle import enqueue, get_queue_entry_status

        run_id = await _create_task_run()
        async with await get_session() as session:
            async with session.begin():
                entry_id = await enqueue(
                    session,
                    task_run_id=run_id,
                    issue_number="42",
                    title="PR",
                    body="body",
                    base_branch="main",
                    head_branch="feat/42",
                )

        async with await get_session() as session:
            status = await get_queue_entry_status(session, entry_id)
        assert status is not None
        assert status["status"] == "pending"
        assert status["pr_number"] is None

    async def test_not_found(self) -> None:
        from sova.db.session import get_session
        from sova.supervisor.pr_throttle import get_queue_entry_status

        async with await get_session() as session:
            status = await get_queue_entry_status(session, 99999)
        assert status is None


# ---------------------------------------------------------------------------
# Poll until created tests
# ---------------------------------------------------------------------------


class TestPollUntilCreated:
    async def test_returns_on_created(self) -> None:
        from sova.db.models import PRCreationQueue
        from sova.db.session import get_session
        from sova.supervisor.pr_throttle import enqueue, poll_until_created

        run_id = await _create_task_run()
        async with await get_session() as session:
            async with session.begin():
                entry_id = await enqueue(
                    session,
                    task_run_id=run_id,
                    issue_number="42",
                    title="PR",
                    body="body",
                    base_branch="main",
                    head_branch="feat/42",
                )

        # Mark created before polling starts
        async with await get_session() as session:
            async with session.begin():
                entry = await session.get(PRCreationQueue, entry_id)
                entry.status = "created"
                entry.pr_number = 77
                entry.pr_url = "https://github.com/o/r/pull/77"

        async def _session_factory():
            return await get_session()

        with patch("sova.supervisor.pr_throttle._POLL_INTERVAL_SECONDS", 0.01):
            result = await poll_until_created(_session_factory, entry_id, timeout_seconds=1)
        assert result is not None
        assert result["status"] == "created"
        assert result["pr_number"] == 77

    async def test_returns_none_on_timeout(self) -> None:
        from sova.db.session import get_session
        from sova.supervisor.pr_throttle import enqueue, poll_until_created

        run_id = await _create_task_run()
        async with await get_session() as session:
            async with session.begin():
                entry_id = await enqueue(
                    session,
                    task_run_id=run_id,
                    issue_number="42",
                    title="PR",
                    body="body",
                    base_branch="main",
                    head_branch="feat/42",
                )

        async def _session_factory():
            return await get_session()

        with patch("sova.supervisor.pr_throttle._POLL_INTERVAL_SECONDS", 0.01):
            result = await poll_until_created(_session_factory, entry_id, timeout_seconds=0.05)
        assert result is None


# ---------------------------------------------------------------------------
# CreatePRStep throttle integration tests
# ---------------------------------------------------------------------------


class TestCreatePRStepThrottledInternals:
    """Test the _create_pr_throttled method paths that exercise create_pr.py new code."""

    async def test_throttled_enqueue_failure_falls_back(self) -> None:
        """When enqueue raises, falls back to immediate creation."""
        from unittest.mock import MagicMock

        from sova.config.models import CodeRabbitQuotaConfig, ProjectConfig
        from sova.core.steps.create_pr import CreatePRStep
        from sova.git.pr import PRInfo

        step = CreatePRStep()
        ctx = MagicMock()
        ctx.display_label = "#42"
        ctx.branch_name = "feat/42"
        ctx.has_issue = True
        ctx.issue_number = "42"
        ctx.task_run_id = 1
        ctx.project_dir = "/tmp"
        ctx.config = ProjectConfig(coderabbit_quota=CodeRabbitQuotaConfig(enabled=True))
        ctx.base_branch = "main"
        ctx.repo = "owner/repo"
        ctx.working_dir = "/tmp"
        ctx.pr_number = None
        ctx.pr_url = ""

        mock_pr = PRInfo(number=10, url="https://github.com/o/r/pull/10")
        with (
            patch(
                "sova.db.session.get_session",
                new_callable=AsyncMock,
                side_effect=RuntimeError("DB down"),
            ),
            patch("sova.core.steps.create_pr.git_ops.create_pr", new_callable=AsyncMock, return_value=mock_pr),
            patch.object(step, "_post_create_side_effects", new_callable=AsyncMock),
        ):
            result = await step._create_pr_throttled(ctx, "title", "body")
        assert result.success is True
        assert "10" in result.summary

    async def test_throttled_poll_timeout_returns_failure(self) -> None:
        """When poll_until_created returns None, step returns failure."""
        from unittest.mock import MagicMock

        from sova.config.models import CodeRabbitQuotaConfig, ProjectConfig
        from sova.core.steps.create_pr import CreatePRStep

        step = CreatePRStep()
        ctx = MagicMock()
        ctx.display_label = "#42"
        ctx.branch_name = "feat/42"
        ctx.has_issue = True
        ctx.issue_number = "42"
        ctx.task_run_id = 1
        ctx.project_dir = "/tmp"
        ctx.config = ProjectConfig(coderabbit_quota=CodeRabbitQuotaConfig(enabled=True))
        ctx.base_branch = "main"
        ctx.repo = "owner/repo"
        ctx.working_dir = "/tmp"
        ctx.pr_number = None
        ctx.pr_url = ""

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()))

        with (
            patch("sova.db.session.get_session", new_callable=AsyncMock, return_value=mock_session),
            patch("sova.supervisor.pr_throttle.enqueue", new_callable=AsyncMock, return_value=99),
            patch("sova.supervisor.pr_throttle.poll_until_created", new_callable=AsyncMock, return_value=None),
        ):
            result = await step._create_pr_throttled(ctx, "title", "body")
        assert result.success is False
        assert "timed out" in result.summary.lower()

    async def test_throttled_poll_success(self) -> None:
        """When poll returns CREATED with pr_number, step succeeds."""
        from unittest.mock import MagicMock

        from sova.config.models import CodeRabbitQuotaConfig, ProjectConfig
        from sova.core.steps.create_pr import CreatePRStep
        from sova.db.models import PRQueueStatus

        step = CreatePRStep()
        ctx = MagicMock()
        ctx.display_label = "#42"
        ctx.branch_name = "feat/42"
        ctx.has_issue = True
        ctx.issue_number = "42"
        ctx.task_run_id = 1
        ctx.project_dir = "/tmp"
        ctx.config = ProjectConfig(coderabbit_quota=CodeRabbitQuotaConfig(enabled=True))
        ctx.base_branch = "main"
        ctx.repo = "owner/repo"
        ctx.working_dir = "/tmp"
        ctx.pr_number = None
        ctx.pr_url = ""

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()))

        poll_result = {"status": PRQueueStatus.CREATED, "pr_number": 77, "pr_url": "https://github.com/o/r/pull/77"}
        with (
            patch("sova.db.session.get_session", new_callable=AsyncMock, return_value=mock_session),
            patch("sova.supervisor.pr_throttle.enqueue", new_callable=AsyncMock, return_value=99),
            patch("sova.supervisor.pr_throttle.poll_until_created", new_callable=AsyncMock, return_value=poll_result),
        ):
            result = await step._create_pr_throttled(ctx, "title", "body")
        assert result.success is True
        assert "77" in result.summary
        assert ctx.pr_number == 77

    async def test_throttled_poll_failed_status(self) -> None:
        """When poll returns FAILED status, step returns failure with error message."""
        from unittest.mock import MagicMock

        from sova.config.models import CodeRabbitQuotaConfig, ProjectConfig
        from sova.core.steps.create_pr import CreatePRStep
        from sova.db.models import PRQueueStatus

        step = CreatePRStep()
        ctx = MagicMock()
        ctx.display_label = "#42"
        ctx.branch_name = "feat/42"
        ctx.has_issue = True
        ctx.issue_number = "42"
        ctx.task_run_id = 1
        ctx.project_dir = "/tmp"
        ctx.config = ProjectConfig(coderabbit_quota=CodeRabbitQuotaConfig(enabled=True))
        ctx.base_branch = "main"
        ctx.repo = "owner/repo"
        ctx.working_dir = "/tmp"
        ctx.pr_number = None
        ctx.pr_url = ""

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()))

        poll_result = {"status": PRQueueStatus.FAILED, "pr_number": None, "error_message": "API rate limit"}
        with (
            patch("sova.db.session.get_session", new_callable=AsyncMock, return_value=mock_session),
            patch("sova.supervisor.pr_throttle.enqueue", new_callable=AsyncMock, return_value=99),
            patch("sova.supervisor.pr_throttle.poll_until_created", new_callable=AsyncMock, return_value=poll_result),
        ):
            result = await step._create_pr_throttled(ctx, "title", "body")
        assert result.success is False
        assert "API rate limit" in result.summary


class TestCreatePRStepThrottle:
    async def test_disabled_quota_uses_immediate(self) -> None:
        """When coderabbit_quota.enabled is False, CreatePRStep creates PR directly."""
        from unittest.mock import MagicMock

        from sova.config.models import CodeRabbitQuotaConfig, ProjectConfig
        from sova.core.steps.create_pr import CreatePRStep
        from sova.git.pr import PRInfo

        step = CreatePRStep()
        ctx = MagicMock()
        ctx.display_label = "#42"
        ctx.branch_name = "feat/42"
        ctx.has_issue = True
        ctx.issue_number = "42"
        ctx.task = MagicMock()
        ctx.task.title = "test feature"
        ctx.config = ProjectConfig(coderabbit_quota=CodeRabbitQuotaConfig(enabled=False))
        ctx.base_branch = "main"
        ctx.repo = "owner/repo"
        ctx.working_dir = "/tmp"
        ctx.pr_number = None
        ctx.pr_url = ""

        mock_pr = PRInfo(number=10, url="https://github.com/o/r/pull/10")
        with (
            patch.object(step, "_try_adopt_existing_pr", new_callable=AsyncMock, return_value=None),
            patch.object(step, "_generate_pr_body", new_callable=AsyncMock, return_value="body"),
            patch("sova.core.steps.create_pr.git_ops.create_pr", new_callable=AsyncMock, return_value=mock_pr),
            patch.object(step, "_post_create_side_effects", new_callable=AsyncMock),
        ):
            result = await step.execute(ctx)
        assert result.success is True
        assert "10" in result.summary

    async def test_enabled_quota_enqueues(self) -> None:
        """When coderabbit_quota.enabled is True, CreatePRStep enqueues."""
        from unittest.mock import MagicMock

        from sova.config.models import CodeRabbitQuotaConfig, ProjectConfig
        from sova.core.steps.create_pr import CreatePRStep

        step = CreatePRStep()
        ctx = MagicMock()
        ctx.display_label = "#42"
        ctx.branch_name = "feat/42"
        ctx.has_issue = True
        ctx.issue_number = "42"
        ctx.task = MagicMock()
        ctx.task.title = "test feature"
        ctx.config = ProjectConfig(coderabbit_quota=CodeRabbitQuotaConfig(enabled=True))
        ctx.base_branch = "main"
        ctx.repo = "owner/repo"
        ctx.project_dir = "/tmp"
        ctx.working_dir = "/tmp"
        ctx.task_run_id = 1
        ctx.pr_number = None
        ctx.pr_url = ""

        with (
            patch.object(step, "_try_adopt_existing_pr", new_callable=AsyncMock, return_value=None),
            patch.object(step, "_generate_pr_body", new_callable=AsyncMock, return_value="body"),
            patch.object(
                step,
                "_create_pr_throttled",
                new_callable=AsyncMock,
                return_value=MagicMock(success=True, summary="Created PR #88 (throttled)"),
            ),
        ):
            result = await step.execute(ctx)
        assert result.success is True
        assert "88" in result.summary

    async def test_no_task_run_id_falls_back_to_immediate(self) -> None:
        """When task_run_id is None, falls back to immediate creation."""
        from unittest.mock import MagicMock

        from sova.config.models import CodeRabbitQuotaConfig, ProjectConfig
        from sova.core.steps.create_pr import CreatePRStep
        from sova.git.pr import PRInfo

        step = CreatePRStep()
        ctx = MagicMock()
        ctx.display_label = "#42"
        ctx.branch_name = "feat/42"
        ctx.has_issue = True
        ctx.issue_number = "42"
        ctx.task = MagicMock()
        ctx.task.title = "test"
        ctx.config = ProjectConfig(coderabbit_quota=CodeRabbitQuotaConfig(enabled=True))
        ctx.base_branch = "main"
        ctx.repo = "owner/repo"
        ctx.working_dir = "/tmp"
        ctx.task_run_id = None
        ctx.pr_number = None
        ctx.pr_url = ""

        mock_pr = PRInfo(number=10, url="url")
        with (
            patch.object(step, "_try_adopt_existing_pr", new_callable=AsyncMock, return_value=None),
            patch.object(step, "_generate_pr_body", new_callable=AsyncMock, return_value="body"),
            patch("sova.core.steps.create_pr.git_ops.create_pr", new_callable=AsyncMock, return_value=mock_pr),
            patch.object(step, "_post_create_side_effects", new_callable=AsyncMock),
        ):
            result = await step.execute(ctx)
        assert result.success is True


# ---------------------------------------------------------------------------
# Process queue loop tests
# ---------------------------------------------------------------------------


class TestAgentDBDequeueOnFailure:
    """Test that _finalize_task_run dequeues pending PR entries on non-done status."""

    async def test_finalize_dequeues_pending_entry(self) -> None:
        """When a run fails, its pending PR queue entry is cancelled."""
        from sova.db.models import PRCreationQueue, PRQueueStatus
        from sova.db.session import get_session
        from sova.supervisor.pr_throttle import enqueue

        tr_id = await _create_task_run()

        # Enqueue a PR
        async with await get_session() as session:
            async with session.begin():
                await enqueue(
                    session,
                    task_run_id=tr_id,
                    issue_number="42",
                    title="test",
                    body="body",
                    base_branch="main",
                    head_branch="feat/42",
                )

        # Simulate _finalize_task_run's dequeue logic
        from sova.supervisor.pr_throttle import dequeue

        async with await get_session() as session:
            async with session.begin():
                result = await dequeue(session, task_run_id=tr_id)
        assert result is True

        # Verify entry was cancelled
        async with await get_session() as session:
            entry = (
                await session.execute(
                    __import__("sqlalchemy").select(PRCreationQueue).where(PRCreationQueue.task_run_id == tr_id)
                )
            ).scalar_one()
            assert entry.status == PRQueueStatus.CANCELLED

    async def test_finalize_dequeue_noop_when_already_created(self) -> None:
        """Dequeue does nothing when the entry was already processed."""
        from sova.db.models import PRCreationQueue, PRQueueStatus
        from sova.db.session import get_session
        from sova.supervisor.pr_throttle import dequeue, enqueue

        tr_id = await _create_task_run()

        async with await get_session() as session:
            async with session.begin():
                await enqueue(
                    session,
                    task_run_id=tr_id,
                    issue_number="42",
                    title="test",
                    body="body",
                    base_branch="main",
                    head_branch="feat/42",
                )

        # Mark it as created first
        async with await get_session() as session:
            async with session.begin():
                entry = (
                    await session.execute(
                        __import__("sqlalchemy").select(PRCreationQueue).where(PRCreationQueue.task_run_id == tr_id)
                    )
                ).scalar_one()
                entry.status = PRQueueStatus.CREATED

        # Dequeue should be noop
        async with await get_session() as session:
            async with session.begin():
                result = await dequeue(session, task_run_id=tr_id)
        assert result is False


class TestRunPostCreateSideEffects:
    """Test the run_post_create_side_effects function."""

    async def test_assign_failure_logged_not_raised(self) -> None:
        from sova.supervisor.pr_throttle import run_post_create_side_effects

        with patch("sova.git.operations.assign_pr", new_callable=AsyncMock, side_effect=RuntimeError):
            await run_post_create_side_effects(
                pr_number=1,
                issue_number=None,
                repo="owner/repo",
                github_user="testuser",
            )

    async def test_tracker_update_failure_logged_not_raised(self) -> None:
        from sova.supervisor.pr_throttle import run_post_create_side_effects

        with (
            patch("sova.git.operations.assign_pr", new_callable=AsyncMock),
            patch("sova.adapters.create_adapter", side_effect=RuntimeError("no adapter")),
        ):
            await run_post_create_side_effects(
                pr_number=1,
                issue_number="42",
                repo="owner/repo",
                github_user="testuser",
            )

    async def test_no_github_user_skips_assign(self) -> None:
        from sova.supervisor.pr_throttle import run_post_create_side_effects

        with patch("sova.git.operations.assign_pr", new_callable=AsyncMock) as mock_assign:
            await run_post_create_side_effects(
                pr_number=1,
                issue_number=None,
                repo="owner/repo",
                github_user="",
            )
        mock_assign.assert_not_called()


class TestProcessQueueLoop:
    async def test_loop_stops_on_cancel(self) -> None:
        from sova.supervisor.pr_throttle import process_queue_loop

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")

        async def _session_factory():
            return await get_session()

        from sova.db.session import get_session

        with patch("sova.supervisor.pr_throttle._PROCESS_INTERVAL_SECONDS", 0.01):
            task = asyncio.create_task(process_queue_loop(_session_factory, cfg))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async def test_loop_stops_on_event(self) -> None:
        from sova.supervisor.pr_throttle import process_queue_loop

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")

        async def _session_factory():
            from sova.db.session import get_session

            return await get_session()

        stop = asyncio.Event()

        with patch("sova.supervisor.pr_throttle._PROCESS_INTERVAL_SECONDS", 0.01):
            task = asyncio.create_task(process_queue_loop(_session_factory, cfg, stop_event=stop))
            await asyncio.sleep(0.05)
            stop.set()
            # Give it time to check the event
            await asyncio.sleep(0.05)
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
