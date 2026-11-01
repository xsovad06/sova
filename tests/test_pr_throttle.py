"""Tests for PR creation throttle service: queue, process, dequeue, recovery."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import SQLAlchemyError

from sova.config.models import CodeRabbitQuotaConfig
from sova.core.state import TASK_RUN_TERMINAL
from sova.db.session import close_db, init_db

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from sova.config.models import ProjectConfig
    from sova.db.models import PRCreationQueue


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


async def _set_run_status(run_id: int, status: str) -> None:
    from sova.db.models import TaskRun
    from sova.db.session import get_session

    async with await get_session() as session:
        async with session.begin():
            run = await session.get(TaskRun, run_id)
            run.status = status


async def _enqueue_entry(run_id: int, *, title: str = "PR") -> int:
    from sova.db.session import get_session
    from sova.supervisor.pr_throttle import enqueue

    async with await get_session() as session:
        async with session.begin():
            return await enqueue(
                session,
                task_run_id=run_id,
                issue_number="42",
                title=title,
                body="body",
                base_branch="main",
                head_branch="feat/42",
                repo="owner/repo",
            )


async def _entry_state(entry_id: int) -> tuple[str, str | None]:
    from sova.db.models import PRCreationQueue
    from sova.db.session import get_session

    async with await get_session() as session:
        entry = await session.get(PRCreationQueue, entry_id)
        return entry.status, entry.error_message


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

    async def test_dequeue_cancels_every_pending_entry_for_a_run(self) -> None:
        """A retried step enqueues once per attempt: dequeue must not trip on the second pending entry."""
        from sova.db.models import PRCreationQueue
        from sova.db.session import get_session
        from sova.supervisor.pr_throttle import dequeue, enqueue

        run_id = await _create_task_run()
        async with await get_session() as session:
            async with session.begin():
                for _ in range(2):
                    await enqueue(
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
                result = await dequeue(session, task_run_id=run_id, reason="step ended")
        assert result is True

        async with await get_session() as session:
            entries = (await session.execute(__import__("sqlalchemy").select(PRCreationQueue))).scalars().all()
        assert [e.status for e in entries] == ["cancelled", "cancelled"]
        assert {e.error_message for e in entries} == {"step ended"}

    async def test_dequeue_leaves_a_creating_entry_alone(self) -> None:
        """A creating entry has a gh pr create in flight that cannot be recalled."""
        from sova.db.session import get_session
        from sova.supervisor.pr_throttle import dequeue

        run_id = await _create_task_run()
        await _insert_entry(run_id, "creating")
        async with await get_session() as session:
            async with session.begin():
                result = await dequeue(session, task_run_id=run_id)
        assert result is False
        assert [e.status for e in await _queue_entries(run_id)] == ["creating"]

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
            patch("sova.git.operations.create_pr", new_callable=AsyncMock, return_value=mock_pr) as mock_create_pr,
            patch("sova.supervisor.pr_throttle.run_post_create_side_effects", new_callable=AsyncMock),
        ):
            async with await get_session() as session:
                count = await process_queue(session, cfg)
        assert count == 1

        # Regression guard: the queued entry's github_user must reach create_pr,
        # otherwise it silently falls back to the daemon's ambient GH_TOKEN
        # instead of the configured account (issue seen in production logs as
        # "must be a collaborator" from a mismatched token).
        mock_create_pr.assert_awaited_once_with(
            title="feat(#42): test",
            body="body",
            base="main",
            head="feat/42",
            repo="owner/repo",
            github_user="user",
        )

        async with await get_session() as session:
            entry = await session.get(PRCreationQueue, entry_id)
            task_run = await session.get(TaskRun, run_id)
        assert entry.status == "created"
        assert entry.pr_number == 99
        assert task_run.pr_number == 99

    async def test_process_blocked_by_quota(self) -> None:
        from sova.db.session import get_session
        from sova.supervisor.coderabbit_quota import _upsert_event
        from sova.supervisor.pr_throttle import enqueue, process_queue

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        now = datetime.now(timezone.utc)

        # Fill quota
        async with await get_session() as session:
            for i in range(4):
                await _upsert_event(session, i + 1, "review", f"r{i}", now, "")
            await session.commit()

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

    async def test_entry_cancelled_after_selection_is_not_created(self) -> None:
        """The run's step may cancel its entry between the FIFO select and the CREATING flip."""
        from sqlalchemy import update

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
        with patch("sova.git.operations.create_pr", new_callable=AsyncMock) as mock_create_pr:
            async with await get_session() as session:
                real_execute = session.execute
                cancelled = False

                async def _cancel_before_first_update(stmt: object, *args: object, **kwargs: object) -> object:
                    nonlocal cancelled
                    if getattr(stmt, "is_update", False) and not cancelled:
                        cancelled = True
                        await real_execute(
                            update(PRCreationQueue).where(PRCreationQueue.id == entry_id).values(status="cancelled")
                        )
                    return await real_execute(stmt, *args, **kwargs)

                with patch.object(session, "execute", side_effect=_cancel_before_first_update):
                    count = await process_queue(session, cfg)
        assert cancelled is True
        assert count == 0
        mock_create_pr.assert_not_awaited()

        async with await get_session() as session:
            entry = await session.get(PRCreationQueue, entry_id)
        assert entry.status == "cancelled"

    async def test_run_ending_after_selection_is_not_created(self) -> None:
        """A run that ends between the FIFO select and the CREATING flip must not have its PR created.

        The run-finalization backstop skips an entry already past pending, so the flip's own predicate
        is the only thing left that can stop it.
        """
        from sqlalchemy import update

        from sova.db.models import TaskRun
        from sova.db.session import get_session
        from sova.supervisor.pr_throttle import process_queue

        run_id = await _create_task_run()
        entry_id = await _enqueue_entry(run_id)

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        with patch("sova.git.operations.create_pr", new_callable=AsyncMock) as mock_create_pr:
            async with await get_session() as session:
                real_execute = session.execute
                ended = False

                async def _end_run_before_first_update(stmt: object, *args: object, **kwargs: object) -> object:
                    nonlocal ended
                    if getattr(stmt, "is_update", False) and not ended:
                        ended = True
                        await real_execute(update(TaskRun).where(TaskRun.id == run_id).values(status="failed"))
                    return await real_execute(stmt, *args, **kwargs)

                with patch.object(session, "execute", side_effect=_end_run_before_first_update):
                    count = await process_queue(session, cfg)
        assert ended is True
        assert count == 0
        mock_create_pr.assert_not_awaited()
        # Left pending: the next pass's ended-run sweep cancels it with the reason, not a half-flipped state.
        assert (await _entry_state(entry_id))[0] == "pending"

        async with await get_session() as session:
            assert await process_queue(session, cfg) == 0
        status, error = await _entry_state(entry_id)
        assert status == "cancelled"
        assert error is not None
        assert "run ended" in error

    @pytest.mark.parametrize("run_status", sorted(TASK_RUN_TERMINAL))
    async def test_entry_of_an_ended_run_is_cancelled_not_created(self, run_status: str) -> None:
        """A held PR is never created once its run has ended, whichever terminal status the run reached."""
        from sova.db.session import get_session
        from sova.supervisor.pr_throttle import process_queue

        run_id = await _create_task_run()
        entry_id = await _enqueue_entry(run_id)
        await _set_run_status(run_id, run_status)

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        with patch("sova.git.operations.create_pr", new_callable=AsyncMock) as mock_create_pr:
            async with await get_session() as session:
                count = await process_queue(session, cfg)
        assert count == 0
        mock_create_pr.assert_not_awaited()

        status, error = await _entry_state(entry_id)
        assert status == "cancelled"
        assert error is not None
        assert "run ended" in error

    async def test_ended_run_entry_does_not_block_the_next_live_one(self) -> None:
        """The ended run's older entry is cancelled and the FIFO moves on to the live run's entry."""
        from sova.db.session import get_session
        from sova.git.pr import PRInfo
        from sova.supervisor.pr_throttle import process_queue

        ended_run = await _create_task_run()
        ended_entry = await _enqueue_entry(ended_run, title="ended")
        live_run = await _create_task_run()
        live_entry = await _enqueue_entry(live_run, title="live")
        await _set_run_status(ended_run, "interrupted")

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        mock_pr = PRInfo(number=7, url="https://github.com/owner/repo/pull/7")
        with (
            patch("sova.git.operations.create_pr", new_callable=AsyncMock, return_value=mock_pr) as mock_create_pr,
            patch("sova.supervisor.pr_throttle.run_post_create_side_effects", new_callable=AsyncMock),
        ):
            async with await get_session() as session:
                count = await process_queue(session, cfg)
        assert count == 1
        assert mock_create_pr.await_args.kwargs["title"] == "live"
        assert (await _entry_state(ended_entry))[0] == "cancelled"
        assert (await _entry_state(live_entry))[0] == "created"

    async def test_ended_run_entry_is_cancelled_even_when_the_quota_is_exhausted(self) -> None:
        """The cancel is not gated on a free quota slot: the entry can never become a PR, so it must not linger."""
        from sova.db.session import get_session
        from sova.supervisor.coderabbit_quota import _upsert_event
        from sova.supervisor.pr_throttle import process_queue

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        now = datetime.now(timezone.utc)
        async with await get_session() as session:
            for i in range(4):
                await _upsert_event(session, i + 1, "review", f"r{i}", now, "")
            await session.commit()

        run_id = await _create_task_run()
        entry_id = await _enqueue_entry(run_id)
        await _set_run_status(run_id, "failed")

        async with await get_session() as session:
            count = await process_queue(session, cfg)
        assert count == 0
        assert (await _entry_state(entry_id))[0] == "cancelled"

    async def test_entry_of_another_project_is_not_cancelled(self) -> None:
        """The sweep is scoped to the loop's own project, like the stale-entry sweep beside it."""
        from sova.db.session import get_session
        from sova.supervisor.pr_throttle import process_queue

        run_id = await _create_task_run()
        await _insert_entry(run_id, "pending")  # project_slug "x/never-matches"
        await _set_run_status(run_id, "failed")

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        async with await get_session() as session:
            await process_queue(session, cfg)
        assert [e.status for e in await _queue_entries(run_id)] == ["pending"]

    async def test_entry_of_a_live_run_is_still_created(self) -> None:
        """Only an observed terminal status cancels: a run still going keeps its place in the queue."""
        from sova.db.session import get_session
        from sova.git.pr import PRInfo
        from sova.supervisor.pr_throttle import process_queue

        run_id = await _create_task_run()
        entry_id = await _enqueue_entry(run_id)
        await _set_run_status(run_id, "pr_created")

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        mock_pr = PRInfo(number=9, url="https://github.com/owner/repo/pull/9")
        with (
            patch("sova.git.operations.create_pr", new_callable=AsyncMock, return_value=mock_pr),
            patch("sova.supervisor.pr_throttle.run_post_create_side_effects", new_callable=AsyncMock),
        ):
            async with await get_session() as session:
                count = await process_queue(session, cfg)
        assert count == 1
        assert (await _entry_state(entry_id))[0] == "created"


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

    @pytest.mark.parametrize("run_status", sorted(TASK_RUN_TERMINAL))
    async def test_creating_entry_of_an_ended_run_is_cancelled_not_reset(self, run_status: str) -> None:
        """A restart mid `gh pr create` must not turn a finished run's entry back into a PR to be created."""
        from sova.db.session import get_session
        from sova.supervisor.pr_throttle import recover_creating_entries

        run_id = await _create_task_run()
        entry_id = await _enqueue_entry(run_id)
        await _set_entry_status(run_id, "creating")
        await _set_run_status(run_id, run_status)

        async with await get_session() as session:
            async with session.begin():
                count = await recover_creating_entries(session)
        assert count == 0

        status, error = await _entry_state(entry_id)
        assert status == "cancelled"
        assert error is not None
        assert "run ended" in error

    async def test_only_the_live_runs_entry_is_reset(self) -> None:
        from sova.db.session import get_session
        from sova.supervisor.pr_throttle import recover_creating_entries

        ended_run = await _create_task_run()
        ended_entry = await _enqueue_entry(ended_run)
        live_run = await _create_task_run()
        live_entry = await _enqueue_entry(live_run)
        await _set_entry_status(ended_run, "creating")
        await _set_entry_status(live_run, "creating")
        await _set_run_status(ended_run, "stopped")

        async with await get_session() as session:
            async with session.begin():
                count = await recover_creating_entries(session)
        assert count == 1
        assert (await _entry_state(ended_entry))[0] == "cancelled"
        assert (await _entry_state(live_entry))[0] == "pending"

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

        async def _session_factory() -> AsyncSession:
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

        async def _session_factory() -> AsyncSession:
            return await get_session()

        with patch("sova.supervisor.pr_throttle._POLL_INTERVAL_SECONDS", 0.01):
            result = await poll_until_created(_session_factory, entry_id, timeout_seconds=0.05)
        assert result is None

    async def test_timeout_leaves_the_entry_for_the_caller_to_cancel(self) -> None:
        """Marking a creating entry failed under a gh pr create that may still succeed would be wrong."""
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

        async def _session_factory() -> AsyncSession:
            return await get_session()

        with patch("sova.supervisor.pr_throttle._POLL_INTERVAL_SECONDS", 0.01):
            assert await poll_until_created(_session_factory, entry_id, timeout_seconds=0.05) is None

        async with await get_session() as session:
            entry = await session.get(PRCreationQueue, entry_id)
        assert entry.status == "pending"
        assert entry.error_message is None

    async def test_does_not_sleep_past_the_deadline(self) -> None:
        """The wait is sized to leave the caller time to clean up, so a 30s poll interval cannot overshoot it."""
        from sova.supervisor.pr_throttle import poll_until_created

        async def _session_factory() -> AsyncSession:
            from sova.db.session import get_session

            return await get_session()

        run_id = await _create_task_run()
        entry_id = await _enqueue_entry(run_id)
        with patch("sova.supervisor.pr_throttle._POLL_INTERVAL_SECONDS", 30):
            result = await asyncio.wait_for(
                poll_until_created(_session_factory, entry_id, timeout_seconds=0.05), timeout=5
            )
        assert result is None

    async def test_a_vanished_entry_is_not_reported_as_a_timeout(self) -> None:
        """Returning None for a deleted row would read, to the caller, as a wait that ran out."""
        from sova.db.session import get_session
        from sova.supervisor.pr_throttle import QueueEntryMissingError, poll_until_created

        async def _session_factory() -> AsyncSession:
            return await get_session()

        with patch("sova.supervisor.pr_throttle._POLL_INTERVAL_SECONDS", 0.01):
            with pytest.raises(QueueEntryMissingError) as excinfo:
                await poll_until_created(_session_factory, 999, timeout_seconds=5)
        assert excinfo.value.entry_id == 999

    async def test_zero_timeout_returns_without_polling(self) -> None:
        from sova.supervisor.pr_throttle import poll_until_created

        factory = AsyncMock()
        assert await poll_until_created(factory, 1, timeout_seconds=0) is None
        factory.assert_not_called()


# ---------------------------------------------------------------------------
# CreatePRStep throttle integration tests
# ---------------------------------------------------------------------------


def _throttle_ctx(
    tmp_path: Path, *, task_run_id: int | None = 1, step_time_remaining: float | None = None
) -> MagicMock:
    """A context for CreatePRStep._create_pr_throttled with the throttle loop's heartbeat fresh."""
    from unittest.mock import MagicMock

    from sova.config.models import ProjectConfig
    from sova.supervisor.pr_throttle import touch_heartbeat

    touch_heartbeat(tmp_path)
    ctx = MagicMock()
    ctx.display_label = "#42"
    ctx.branch_name = "feat/42"
    ctx.has_issue = True
    ctx.issue_number = "42"
    ctx.task_run_id = task_run_id
    ctx.project_dir = str(tmp_path)
    ctx.config = ProjectConfig(coderabbit_quota=CodeRabbitQuotaConfig(enabled=True))
    ctx.base_branch = "main"
    ctx.repo = "owner/repo"
    ctx.working_dir = str(tmp_path)
    ctx.pr_number = None
    ctx.pr_url = ""
    ctx.step_time_remaining = step_time_remaining
    ctx.step_deadline_is_runaway = False
    return ctx


def _mock_session() -> AsyncMock:
    from unittest.mock import MagicMock

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()))
    return mock_session


class TestNextSlotHint:
    """_next_slot_hint names when the quota frees up, and never raises when it cannot tell."""

    @pytest.mark.parametrize(
        ("can_create", "minutes", "expected"),
        [
            (True, None, "throttle loop is not draining the queue"),
            (False, None, "no CodeRabbit quota slot is free"),
            (False, 11.2, "opens in about 12 minutes"),
        ],
    )
    async def test_describes_the_quota_state(
        self, tmp_path: Path, can_create: bool, minutes: float | None, expected: str
    ) -> None:
        from sova.core.steps.create_pr import CreatePRStep

        quota = MagicMock(can_create_pr=can_create, next_available_minutes=minutes)
        with (
            patch("sova.db.session.get_session", new_callable=AsyncMock, return_value=_mock_session()),
            patch("sova.supervisor.coderabbit_quota.get_quota_status", new_callable=AsyncMock, return_value=quota),
        ):
            hint = await CreatePRStep._next_slot_hint(_throttle_ctx(tmp_path))
        assert expected in hint

    @pytest.mark.parametrize(
        "error", [TimeoutError(), OSError("disk"), RuntimeError("loop"), ValueError("bad event row")]
    )
    async def test_quota_lookup_failure_returns_a_hint_instead_of_raising(
        self, tmp_path: Path, error: Exception
    ) -> None:
        from sova.core.steps.create_pr import CreatePRStep

        with (
            patch("sova.db.session.get_session", new_callable=AsyncMock, return_value=_mock_session()),
            patch("sova.supervisor.coderabbit_quota.get_quota_status", new_callable=AsyncMock, side_effect=error),
        ):
            hint = await CreatePRStep._next_slot_hint(_throttle_ctx(tmp_path))
        assert hint == "the next free CodeRabbit quota slot could not be determined"


class TestCreatePRStepThrottledInternals:
    """_create_pr_throttled with the queue, poll and cancel collaborators patched out."""

    async def test_throttled_enqueue_failure_falls_back(self, tmp_path: Path) -> None:
        """When enqueue raises, falls back to immediate creation."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.git.pr import PRInfo

        step = CreatePRStep()
        ctx = _throttle_ctx(tmp_path)

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

    async def test_throttled_poll_timeout_returns_failure(self, tmp_path: Path) -> None:
        """When poll_until_created returns None, step returns failure naming the entry and the next slot."""
        from sova.core.steps.create_pr import CreatePRStep

        step = CreatePRStep()
        ctx = _throttle_ctx(tmp_path)

        cancelled = {"id": 99, "status": "cancelled", "pr_number": None}
        with (
            patch("sova.db.session.get_session", new_callable=AsyncMock, return_value=_mock_session()),
            patch("sova.supervisor.pr_throttle.enqueue", new_callable=AsyncMock, return_value=99),
            patch("sova.supervisor.pr_throttle.poll_until_created", new_callable=AsyncMock, return_value=None),
            patch.object(step, "_cancel_queue_entry", new_callable=AsyncMock, return_value=cancelled),
            patch.object(step, "_next_slot_hint", new_callable=AsyncMock, return_value="the next slot is in 12 min"),
        ):
            result = await step._create_pr_throttled(ctx, "title", "body")
        assert result.success is False
        assert "timed out" in result.summary.lower()
        assert result.error is not None
        assert "queue entry 99" in result.error
        assert "the next slot is in 12 min" in result.error

    async def test_throttled_poll_success(self, tmp_path: Path) -> None:
        """When poll returns CREATED with pr_number, step succeeds."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.db.models import PRQueueStatus

        step = CreatePRStep()
        ctx = _throttle_ctx(tmp_path)

        poll_result = {"status": PRQueueStatus.CREATED, "pr_number": 77, "pr_url": "https://github.com/o/r/pull/77"}
        with (
            patch("sova.db.session.get_session", new_callable=AsyncMock, return_value=_mock_session()),
            patch("sova.supervisor.pr_throttle.enqueue", new_callable=AsyncMock, return_value=99),
            patch("sova.supervisor.pr_throttle.poll_until_created", new_callable=AsyncMock, return_value=poll_result),
            patch.object(step, "_cancel_queue_entry", new_callable=AsyncMock, return_value=poll_result) as cancel,
        ):
            result = await step._create_pr_throttled(ctx, "title", "body")
        assert result.success is True
        assert "77" in result.summary
        assert ctx.pr_number == 77
        # The cancel is unconditional (a no-op for an entry past pending), so no exit path can skip it.
        cancel.assert_awaited_once_with(ctx, 99)

    async def test_throttled_poll_failed_status(self, tmp_path: Path) -> None:
        """When poll returns FAILED status, step returns failure with error message."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.db.models import PRQueueStatus

        step = CreatePRStep()
        ctx = _throttle_ctx(tmp_path)

        poll_result = {"status": PRQueueStatus.FAILED, "pr_number": None, "error_message": "API rate limit"}
        with (
            patch("sova.db.session.get_session", new_callable=AsyncMock, return_value=_mock_session()),
            patch("sova.supervisor.pr_throttle.enqueue", new_callable=AsyncMock, return_value=99),
            patch("sova.supervisor.pr_throttle.poll_until_created", new_callable=AsyncMock, return_value=poll_result),
            patch.object(step, "_cancel_queue_entry", new_callable=AsyncMock, return_value=poll_result),
        ):
            result = await step._create_pr_throttled(ctx, "title", "body")
        assert result.success is False
        assert "API rate limit" in result.summary
        assert result.error == "API rate limit"

    async def test_throttled_poll_failed_status_null_error_message(self, tmp_path: Path) -> None:
        """When error_message is present but None, falls back to a message naming the queue entry."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.db.models import PRQueueStatus

        step = CreatePRStep()
        ctx = _throttle_ctx(tmp_path)

        poll_result = {"status": PRQueueStatus.FAILED, "pr_number": None, "error_message": None}
        with (
            patch("sova.db.session.get_session", new_callable=AsyncMock, return_value=_mock_session()),
            patch("sova.supervisor.pr_throttle.enqueue", new_callable=AsyncMock, return_value=99),
            patch("sova.supervisor.pr_throttle.poll_until_created", new_callable=AsyncMock, return_value=poll_result),
            patch.object(step, "_cancel_queue_entry", new_callable=AsyncMock, return_value=poll_result),
        ):
            result = await step._create_pr_throttled(ctx, "title", "body")
        assert result.success is False
        assert result.error is not None
        assert "99" in result.error


class TestCreatePRStepWithoutThrottleLoop:
    """With no live loop to drain the queue, the step creates the PR directly instead of enqueueing."""

    @staticmethod
    async def _throttled_with_heartbeat(tmp_path: Path, *, age_seconds: float | None) -> tuple:
        """Run the step; age_seconds None removes the heartbeat, otherwise back-dates it by that much."""
        from sova.core.steps.create_pr import CreatePRStep
        from sova.git.pr import PRInfo
        from sova.supervisor.pr_throttle import heartbeat_path

        step = CreatePRStep()
        ctx = _throttle_ctx(tmp_path)
        beat = heartbeat_path(tmp_path)
        if age_seconds is None:
            beat.unlink()
        else:
            old = datetime.now(timezone.utc).timestamp() - age_seconds
            os.utime(beat, (old, old))

        mock_pr = PRInfo(number=10, url="https://github.com/o/r/pull/10")
        with (
            patch("sova.supervisor.pr_throttle.enqueue", new_callable=AsyncMock) as enqueue,
            patch("sova.core.steps.create_pr.git_ops.create_pr", new_callable=AsyncMock, return_value=mock_pr),
            patch.object(step, "_post_create_side_effects", new_callable=AsyncMock),
        ):
            result = await step._create_pr_throttled(ctx, "title", "body")
        return result, enqueue, ctx

    async def test_missing_heartbeat_creates_directly(self, tmp_path: Path) -> None:
        result, enqueue, ctx = await self._throttled_with_heartbeat(tmp_path, age_seconds=None)
        assert result.success is True
        assert ctx.pr_number == 10
        enqueue.assert_not_called()

    async def test_stale_heartbeat_creates_directly(self, tmp_path: Path) -> None:
        result, enqueue, ctx = await self._throttled_with_heartbeat(tmp_path, age_seconds=3600)
        assert result.success is True
        assert ctx.pr_number == 10
        enqueue.assert_not_called()

    async def test_fresh_heartbeat_enqueues(self, tmp_path: Path) -> None:
        from sova.core.steps.create_pr import CreatePRStep

        step = CreatePRStep()
        ctx = _throttle_ctx(tmp_path)
        created = {"status": "created", "pr_number": 3, "pr_url": "u"}
        with (
            patch("sova.db.session.get_session", new_callable=AsyncMock, return_value=_mock_session()),
            patch("sova.supervisor.pr_throttle.enqueue", new_callable=AsyncMock, return_value=7) as enqueue,
            patch("sova.supervisor.pr_throttle.poll_until_created", new_callable=AsyncMock, return_value=created),
            patch.object(step, "_cancel_queue_entry", new_callable=AsyncMock, return_value=created),
        ):
            result = await step._create_pr_throttled(ctx, "title", "body")
        assert result.success is True
        enqueue.assert_awaited_once()


class TestCreatePRStepQueueWaitBound:
    """The queue wait is bounded by the step's remaining time, never past the 3600s cap."""

    @pytest.mark.parametrize(
        ("remaining", "expected"),
        [
            (None, 3600),  # no engine drives the context: only the cap applies
            (7200, 3600),  # a long step deadline does not lift the cap
            (500, 440),  # remaining less the 60s margin
        ],
    )
    async def test_wait_follows_step_time_remaining(
        self, tmp_path: Path, remaining: float | None, expected: float
    ) -> None:
        from sova.core.steps.create_pr import CreatePRStep

        step = CreatePRStep()
        ctx = _throttle_ctx(tmp_path, step_time_remaining=remaining)
        created = {"status": "created", "pr_number": 3, "pr_url": "u"}
        with (
            patch("sova.db.session.get_session", new_callable=AsyncMock, return_value=_mock_session()),
            patch("sova.supervisor.pr_throttle.enqueue", new_callable=AsyncMock, return_value=7),
            patch(
                "sova.supervisor.pr_throttle.poll_until_created", new_callable=AsyncMock, return_value=created
            ) as poll,
            patch.object(step, "_cancel_queue_entry", new_callable=AsyncMock, return_value=created),
        ):
            await step._create_pr_throttled(ctx, "title", "body")
        assert poll.call_args.kwargs["timeout_seconds"] == expected

    @pytest.mark.parametrize("remaining", [60, 30, 0, -5])
    async def test_no_time_left_fails_without_touching_the_queue(self, tmp_path: Path, remaining: float) -> None:
        """Inside the margin (or past the deadline) there is nothing to wait for: say so, and write no entry."""
        from sova.core.steps.create_pr import CreatePRStep

        step = CreatePRStep()
        ctx = _throttle_ctx(tmp_path, step_time_remaining=remaining)
        with (
            patch("sova.db.session.get_session", new_callable=AsyncMock) as get_session,
            patch("sova.supervisor.pr_throttle.enqueue", new_callable=AsyncMock) as enqueue,
            patch("sova.supervisor.pr_throttle.poll_until_created", new_callable=AsyncMock) as poll,
            patch.object(step, "_cancel_queue_entry", new_callable=AsyncMock) as cancel,
        ):
            result = await step._create_pr_throttled(ctx, "title", "body")
        assert result.success is False
        assert "no time" in (result.error or "")
        enqueue.assert_not_called()
        poll.assert_not_called()
        cancel.assert_not_called()
        get_session.assert_not_called()

    async def test_runaway_deadline_is_left_to_the_engine(self, tmp_path: Path) -> None:
        """A deadline set by the run-wide runaway guard must reach the engine's PAUSED path, so no early bail."""
        from sova.core.steps.create_pr import CreatePRStep

        step = CreatePRStep()
        ctx = _throttle_ctx(tmp_path, step_time_remaining=10)
        ctx.step_deadline_is_runaway = True
        created = {"status": "created", "pr_number": 3, "pr_url": "u"}
        with (
            patch("sova.db.session.get_session", new_callable=AsyncMock, return_value=_mock_session()),
            patch("sova.supervisor.pr_throttle.enqueue", new_callable=AsyncMock, return_value=7),
            patch(
                "sova.supervisor.pr_throttle.poll_until_created", new_callable=AsyncMock, return_value=created
            ) as poll,
            patch.object(step, "_cancel_queue_entry", new_callable=AsyncMock, return_value=created),
        ):
            await step._create_pr_throttled(ctx, "title", "body")
        assert poll.call_args.kwargs["timeout_seconds"] == 3600


async def _queue_entries_in(session: AsyncSession, task_run_id: int) -> list[PRCreationQueue]:
    from sqlalchemy import select

    from sova.db.models import PRCreationQueue

    result = await session.execute(
        select(PRCreationQueue).where(PRCreationQueue.task_run_id == task_run_id).order_by(PRCreationQueue.id)
    )
    return list(result.scalars().all())


async def _queue_entries(task_run_id: int) -> list:
    from sova.db.session import get_session

    async with await get_session() as session:
        return await _queue_entries_in(session, task_run_id)


async def _set_entry_status(task_run_id: int, status: str, *, pr_number: int | None = None) -> None:
    from sova.db.session import get_session

    async with await get_session() as session:
        async with session.begin():
            for entry in await _queue_entries_in(session, task_run_id):
                entry.status = status
                entry.pr_number = pr_number
                entry.pr_url = f"https://github.com/o/r/pull/{pr_number}" if pr_number else None


async def _exhaust_free_plan_quota() -> None:
    """Record four reviews ten minutes ago: the free plan allows 4 per hour, so no slot opens for about 50 minutes."""
    from sova.db.models import CodeRabbitEvent
    from sova.db.session import get_session

    now = datetime.now(timezone.utc)
    async with await get_session() as session:
        async with session.begin():
            for i in range(4):
                session.add(
                    CodeRabbitEvent(
                        pr_number=i,
                        event_type="review",
                        review_id=f"r{i}",
                        recorded_at=now - timedelta(minutes=10),
                        project_slug="",
                    )
                )


class TestCreatePRStepQueueCleanup:
    """Against a real database: every way out of the queue wait leaves no pending entry."""

    async def test_timeout_cancels_every_pending_entry_and_names_the_next_slot(self, tmp_path: Path) -> None:
        from sova.core.steps.create_pr import CreatePRStep

        run_id = await _create_task_run()
        await _insert_entry(run_id, "pending")  # a retried step's earlier, still-pending attempt
        await _exhaust_free_plan_quota()

        step = CreatePRStep()
        ctx = _throttle_ctx(tmp_path, task_run_id=run_id)
        with patch("sova.supervisor.pr_throttle.poll_until_created", new_callable=AsyncMock, return_value=None):
            result = await step._create_pr_throttled(ctx, "title", "body")

        entries = await _queue_entries(run_id)
        assert len(entries) == 2
        assert {e.status for e in entries} == {"cancelled"}
        assert all(e.processed_at is not None for e in entries)
        assert result.success is False
        assert result.error is not None
        assert f"queue entry {entries[-1].id}" in result.error
        assert "about 50 minutes" in result.error

    async def test_quota_window_longer_than_the_step_deadline_ends_the_real_wait_with_the_queue_error(
        self, tmp_path: Path
    ) -> None:
        """No mocked poll: the step's own deadline must bound the real poll loop and its sleeps.

        The quota has no free slot for about 50 minutes, so the entry can never be created inside a step
        that has a fraction of a second left after the margin. A sleep that overshot the bound would trip
        the outer ``wait_for`` instead of returning the explicit error.
        """
        from sova.core.steps.create_pr import _QUEUE_WAIT_MARGIN_SECONDS, CreatePRStep

        run_id = await _create_task_run()
        await _exhaust_free_plan_quota()

        step = CreatePRStep()
        ctx = _throttle_ctx(tmp_path, task_run_id=run_id, step_time_remaining=_QUEUE_WAIT_MARGIN_SECONDS + 0.2)
        with patch("sova.supervisor.pr_throttle._POLL_INTERVAL_SECONDS", 0.05):
            result = await asyncio.wait_for(step._create_pr_throttled(ctx, "title", "body"), timeout=5)

        entries = await _queue_entries(run_id)
        assert len(entries) == 1
        assert entries[0].status == "cancelled"
        assert result.success is False
        assert result.error is not None
        assert f"Timed out waiting for queue entry {entries[0].id}" in result.error
        assert "about 50 minutes" in result.error

    async def test_a_vanished_entry_is_reported_as_such_not_as_a_timeout(self, tmp_path: Path) -> None:
        from sova.core.steps.create_pr import CreatePRStep
        from sova.supervisor.pr_throttle import QueueEntryMissingError

        run_id = await _create_task_run()
        step = CreatePRStep()
        ctx = _throttle_ctx(tmp_path, task_run_id=run_id)
        with patch(
            "sova.supervisor.pr_throttle.poll_until_created",
            new_callable=AsyncMock,
            side_effect=QueueEntryMissingError(5),
        ):
            result = await step._create_pr_throttled(ctx, "title", "body")
        assert result.success is False
        assert result.error is not None
        assert "disappeared" in result.error
        assert "Timed out" not in result.error
        assert "timed out" not in result.summary.lower()
        # The cleanup still ran: the run's own pending entry is cancelled on this way out too.
        assert {e.status for e in await _queue_entries(run_id)} == {"cancelled"}

    async def test_timeout_with_a_free_slot_says_the_loop_is_not_draining(self, tmp_path: Path) -> None:
        from sova.core.steps.create_pr import CreatePRStep

        run_id = await _create_task_run()
        step = CreatePRStep()
        ctx = _throttle_ctx(tmp_path, task_run_id=run_id)
        with patch("sova.supervisor.pr_throttle.poll_until_created", new_callable=AsyncMock, return_value=None):
            result = await step._create_pr_throttled(ctx, "title", "body")
        assert result.success is False
        assert "not draining the queue" in (result.error or "")

    async def test_race_won_by_the_loop_is_a_success(self, tmp_path: Path) -> None:
        """The loop creates the PR just before the cancel lands: the final read shows CREATED."""
        from sova.core.steps.create_pr import CreatePRStep

        run_id = await _create_task_run()

        async def _loop_wins(*_args: object, **_kwargs: object) -> None:
            await _set_entry_status(run_id, "created", pr_number=88)

        step = CreatePRStep()
        ctx = _throttle_ctx(tmp_path, task_run_id=run_id)
        with patch("sova.supervisor.pr_throttle.poll_until_created", side_effect=_loop_wins):
            result = await step._create_pr_throttled(ctx, "title", "body")
        assert result.success is True
        assert "88" in result.summary
        assert ctx.pr_number == 88
        assert [e.status for e in await _queue_entries(run_id)] == ["created"]

    async def test_creating_entry_is_left_alone_and_reported_in_flight(self, tmp_path: Path) -> None:
        from sova.core.steps.create_pr import CreatePRStep

        run_id = await _create_task_run()

        async def _loop_starts_creating(*_args: object, **_kwargs: object) -> None:
            await _set_entry_status(run_id, "creating")

        step = CreatePRStep()
        ctx = _throttle_ctx(tmp_path, task_run_id=run_id)
        with patch("sova.supervisor.pr_throttle.poll_until_created", side_effect=_loop_starts_creating):
            result = await step._create_pr_throttled(ctx, "title", "body")
        assert result.success is False
        assert "in flight" in (result.error or "")
        assert [e.status for e in await _queue_entries(run_id)] == ["creating"]

    async def test_cancellation_cancels_the_entry_and_propagates(self, tmp_path: Path) -> None:
        """The engine's step timeout cancels the task mid-wait: the entry is cancelled, CancelledError survives."""
        from sova.core.steps.create_pr import CreatePRStep

        run_id = await _create_task_run()
        waiting = asyncio.Event()

        async def _wait_forever(*_args: object, **_kwargs: object) -> None:
            waiting.set()
            await asyncio.sleep(3600)

        step = CreatePRStep()
        ctx = _throttle_ctx(tmp_path, task_run_id=run_id)
        with patch("sova.supervisor.pr_throttle.poll_until_created", side_effect=_wait_forever):
            task = asyncio.create_task(step._create_pr_throttled(ctx, "title", "body"))
            await asyncio.wait_for(waiting.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        entries = await _queue_entries(run_id)
        assert [e.status for e in entries] == ["cancelled"]

    @pytest.mark.parametrize(
        "error",
        [SQLAlchemyError("db"), OSError("disk"), ValueError("driver"), AttributeError("mock")],
        ids=lambda error: type(error).__name__,
    )
    async def test_cancel_failure_does_not_replace_the_outcome(self, tmp_path: Path, error: Exception) -> None:
        """Whatever the session or the status read raises, the step reports its own outcome, not the cleanup's."""
        from sova.core.steps.create_pr import CreatePRStep

        run_id = await _create_task_run()
        step = CreatePRStep()
        ctx = _throttle_ctx(tmp_path, task_run_id=run_id)
        with (
            patch("sova.supervisor.pr_throttle.poll_until_created", new_callable=AsyncMock, return_value=None),
            patch("sova.supervisor.pr_throttle.dequeue", new_callable=AsyncMock, side_effect=error),
        ):
            result = await step._create_pr_throttled(ctx, "title", "body")
        assert result.success is False
        assert "Timed out waiting for queue entry" in (result.error or "")

    async def test_hung_cancel_is_bounded(self, tmp_path: Path) -> None:
        from sova.core.steps.create_pr import CreatePRStep

        run_id = await _create_task_run()
        release = asyncio.Event()

        async def _hang(*_args: object, **_kwargs: object) -> None:
            await release.wait()

        ctx = _throttle_ctx(tmp_path, task_run_id=run_id)
        with (
            patch("sova.core.steps.create_pr._QUEUE_CLEANUP_TIMEOUT_SECONDS", 0.05),
            patch("sova.supervisor.pr_throttle.dequeue", side_effect=_hang),
        ):
            final = await asyncio.wait_for(CreatePRStep._cancel_queue_entry(ctx, 1), timeout=2)
        assert final is None
        release.set()
        await asyncio.sleep(0)  # let the shielded work finish before the test's loop closes


class TestCreatePRStepThrottle:
    async def test_disabled_quota_uses_immediate(self, tmp_path: Path) -> None:
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
        ctx.working_dir = str(tmp_path)
        ctx.pr_number = None
        ctx.pr_url = ""

        mock_pr = PRInfo(number=10, url="https://github.com/o/r/pull/10")
        with (
            patch.object(step, "_try_adopt_existing_pr", new_callable=AsyncMock, return_value=None),
            patch.object(step, "_generate_pr_body", new_callable=AsyncMock, return_value=("body", "feat(core): test")),
            patch("sova.core.steps.create_pr.git_ops.create_pr", new_callable=AsyncMock, return_value=mock_pr),
            patch.object(step, "_post_create_side_effects", new_callable=AsyncMock),
        ):
            result = await step.execute(ctx)
        assert result.success is True
        assert "10" in result.summary

    async def test_enabled_quota_enqueues(self, tmp_path: Path) -> None:
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
        ctx.project_dir = str(tmp_path)
        ctx.working_dir = str(tmp_path)
        ctx.task_run_id = 1
        ctx.pr_number = None
        ctx.pr_url = ""

        with (
            patch.object(step, "_try_adopt_existing_pr", new_callable=AsyncMock, return_value=None),
            patch.object(step, "_generate_pr_body", new_callable=AsyncMock, return_value=("body", "feat(core): test")),
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

    async def test_no_task_run_id_falls_back_to_immediate(self, tmp_path: Path) -> None:
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
        ctx.working_dir = str(tmp_path)
        ctx.task_run_id = None
        ctx.pr_number = None
        ctx.pr_url = ""

        mock_pr = PRInfo(number=10, url="url")
        with (
            patch.object(step, "_try_adopt_existing_pr", new_callable=AsyncMock, return_value=None),
            patch.object(step, "_generate_pr_body", new_callable=AsyncMock, return_value=("body", "feat(core): test")),
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


class TestFinalizeCancelsPendingEntryOfEndedRun:
    """The run-finalization backstop: an ended run leaves no pending entry, whoever set its status."""

    @staticmethod
    def _agent() -> MagicMock:
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_pool import AgentState

        agent = MagicMock(spec=AgentState)
        agent.last_result_cost = None  # with no runtime model either, no cost record is written
        agent.runtime_model = None
        agent.role = "developer"
        agent.runtime_name = None
        agent.runtime_session_id = None
        agent.project_dir = None
        agent.process = None
        agent.output_lines = []
        agent.issue = None
        return agent

    @pytest.mark.parametrize("run_status", ["running", "failed"])
    async def test_finalize_cancels_every_pending_entry(self, run_status: str) -> None:
        """Both branches: the dashboard sets the status itself, or the subprocess already did."""
        from sova.dashboard.services.agent_db import _finalize_task_run
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        run_id = await _create_task_run()
        async with await get_session() as session:
            async with session.begin():
                run = await session.get(TaskRun, run_id)
                run.status = run_status
        await _insert_entry(run_id, "pending")
        await _insert_entry(run_id, "pending")

        await _finalize_task_run(run_id, exit_code=1, agent=self._agent())

        assert [e.status for e in await _queue_entries(run_id)] == ["cancelled", "cancelled"]

    async def test_a_failing_backstop_does_not_roll_back_the_handoff(self) -> None:
        """The dequeue shares the finalize transaction: its failure must not take the handoff write with it.

        A failed flush leaves the session needing a rollback, so without a savepoint around the dequeue
        the next statement raises and the whole finalize transaction (handoff included) is lost.
        """
        from sova.dashboard.services.agent_db import _finalize_task_run
        from sova.db.models import PRCreationQueue, TaskRun
        from sova.db.session import get_session

        run_id = await _create_task_run()
        await _set_run_status(run_id, "failed")
        handoff = {"issue": "42", "pr_number": None, "details": {"state": "failed"}, "source": "file"}

        async def _broken_dequeue(session: AsyncSession, *, task_run_id: int) -> bool:
            session.add(PRCreationQueue(task_run_id=task_run_id))  # NOT NULL columns missing: flush fails
            await session.flush()
            return True

        with (
            patch("sova.supervisor.pr_throttle.dequeue", side_effect=_broken_dequeue),
            patch("sova.dashboard.services.agent_db._read_file_handoff", return_value=handoff),
        ):
            await _finalize_task_run(run_id, exit_code=1, agent=self._agent())

        async with await get_session() as session:
            run = await session.get(TaskRun, run_id)
        assert run.handoff_json == {"state": "failed"}

    async def test_a_failed_backstop_is_logged_as_a_warning(self) -> None:
        """A backstop that fails silently leaves the loop's own sweep as the only guard: say so, visibly."""
        from sova.dashboard.services.agent_db import _dequeue_pr_entry

        with (
            patch("sova.supervisor.pr_throttle.dequeue", new_callable=AsyncMock, side_effect=SQLAlchemyError("locked")),
            patch("sova.dashboard.services.agent_db.log") as log,
        ):
            await _dequeue_pr_entry(MagicMock(), 5)
        log.warning.assert_called_once()
        assert log.warning.call_args.args[0] == "task_run.pr_dequeue_failed"
        log.debug.assert_not_called()


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

    async def test_ldap_reviewer_suggestion_runs_for_queued_prs(self) -> None:
        """Queued PRs (throttled behind CodeRabbit quota) get the same LDAP reviewer
        suggestion as directly-created ones, even when the PR has no linked issue."""
        from sova.config.models import LdapConfig, ProjectConfig
        from sova.supervisor.pr_throttle import run_post_create_side_effects

        cfg = ProjectConfig(ldap=LdapConfig(enabled=True))
        mock_adapter = AsyncMock()

        with (
            patch("sova.git.operations.assign_pr", new_callable=AsyncMock),
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("sova.adapters.create_adapter", return_value=mock_adapter),
            patch("sova.core.steps.create_pr.suggest_ldap_reviewers", new_callable=AsyncMock) as mock_suggest,
        ):
            await run_post_create_side_effects(
                pr_number=1,
                issue_number=None,
                repo="owner/repo",
                github_user="octocat",
            )

        mock_suggest.assert_awaited_once_with(
            cfg.ldap,
            mock_adapter,
            github_user="octocat",
            issue_number=None,
            pr_number=1,
        )

    async def test_ldap_disabled_skips_reviewer_suggestion(self) -> None:
        from sova.config.models import ProjectConfig
        from sova.supervisor.pr_throttle import run_post_create_side_effects

        cfg = ProjectConfig()
        assert cfg.ldap.enabled is False

        with (
            patch("sova.git.operations.assign_pr", new_callable=AsyncMock),
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("sova.core.steps.create_pr.suggest_ldap_reviewers", new_callable=AsyncMock) as mock_suggest,
        ):
            await run_post_create_side_effects(
                pr_number=1,
                issue_number=None,
                repo="owner/repo",
                github_user="octocat",
            )

        mock_suggest.assert_not_called()


class TestTriggerCodeRabbitReview:
    """Test the _trigger_coderabbit_review function in pr_throttle."""

    async def test_trigger_posts_comment_when_enabled(self) -> None:
        from sova.supervisor.pr_throttle import _trigger_coderabbit_review

        mock_run_result = AsyncMock()
        mock_run_result.success = True
        with (
            patch(
                "sova.config.loader.load_config",
                return_value=_make_config_with_trigger(True),
            ),
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None),
            patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=mock_run_result) as mock_run,
            patch("sova.supervisor.github_quota.track_rate_limit") as mock_track,
        ):
            await _trigger_coderabbit_review(pr_number=10, repo="o/r", github_user="u")
        mock_run.assert_called_once()
        args = mock_run.call_args.args
        assert "@coderabbitai review" in args
        mock_track.assert_called_once_with(mock_run_result, "u")

    async def test_trigger_skipped_when_disabled(self) -> None:
        from sova.supervisor.pr_throttle import _trigger_coderabbit_review

        with (
            patch(
                "sova.config.loader.load_config",
                return_value=_make_config_with_trigger(False),
            ),
            patch("sova.utils.shell.run", new_callable=AsyncMock) as mock_run,
        ):
            await _trigger_coderabbit_review(pr_number=10, repo="o/r", github_user="u")
        mock_run.assert_not_called()

    async def test_trigger_failure_is_non_fatal(self) -> None:
        from sova.supervisor.pr_throttle import _trigger_coderabbit_review

        mock_run_result = AsyncMock()
        mock_run_result.success = False
        mock_run_result.stderr = "error"
        with (
            patch(
                "sova.config.loader.load_config",
                return_value=_make_config_with_trigger(True),
            ),
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None),
            patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=mock_run_result),
            patch("sova.supervisor.github_quota.track_rate_limit") as mock_track,
        ):
            await _trigger_coderabbit_review(pr_number=10, repo="o/r", github_user="u")
        mock_track.assert_called_once_with(mock_run_result, "u")


def _make_config_with_trigger(enabled: bool):
    from sova.config.models import CodeRabbitConfig, ExternalReviewsConfig, ProjectConfig

    return ProjectConfig(
        external_reviews=ExternalReviewsConfig(
            coderabbit=CodeRabbitConfig(trigger_review=enabled),
        ),
    )


class TestPRThrottleLoopRun:
    async def test_loop_stops_on_cancel(self) -> None:
        from sova.supervisor.pr_throttle import PRThrottleLoop

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")

        async def _session_factory() -> AsyncSession:
            return await get_session()

        from sova.db.session import get_session

        loop = PRThrottleLoop(_session_factory, cfg)
        with patch("sova.supervisor.pr_throttle._PROCESS_INTERVAL_SECONDS", 0.01):
            task = asyncio.create_task(loop.run())
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async def test_loop_stops_on_event(self) -> None:
        from sova.supervisor.pr_throttle import PRThrottleLoop

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")

        async def _session_factory() -> AsyncSession:
            from sova.db.session import get_session

            return await get_session()

        stop = asyncio.Event()

        loop = PRThrottleLoop(_session_factory, cfg, stop_event=stop)
        with patch("sova.supervisor.pr_throttle._PROCESS_INTERVAL_SECONDS", 0.01):
            task = asyncio.create_task(loop.run())
            await asyncio.sleep(0.05)
            stop.set()
            # A timeout here means the loop ignored stop_event; let it fail the
            # test instead of cancelling the task and accepting that as proof.
            await asyncio.wait_for(task, timeout=1.0)
        assert task.done()


# ---------------------------------------------------------------------------
# Heartbeat: the cross-process signal that a loop is draining this project's queue
# ---------------------------------------------------------------------------


def _beat_as(project_dir: Path, pid: int) -> Path:
    """Write the heartbeat file another process's throttle loop would leave, and return its path."""
    from sova.supervisor.pr_throttle import heartbeat_path

    path = heartbeat_path(project_dir, pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}:loop", encoding="utf-8")
    return path


def _stale_epoch() -> float:
    """A timestamp well past the heartbeat's maximum age."""
    return time.time() - 3600


class TestHeartbeat:
    def test_touch_creates_the_file_under_agent_control(self, tmp_path: Path) -> None:
        from sova.supervisor.pr_throttle import heartbeat_path, touch_heartbeat

        touch_heartbeat(tmp_path)
        assert heartbeat_path(tmp_path) == (
            tmp_path / ".claude" / "agent-control" / f"pr-throttle.{os.getpid()}.heartbeat"
        )
        assert heartbeat_path(tmp_path).is_file()

    def test_fresh_heartbeat_is_alive(self, tmp_path: Path) -> None:
        from sova.supervisor.pr_throttle import throttle_loop_alive, touch_heartbeat

        touch_heartbeat(tmp_path)
        assert throttle_loop_alive(tmp_path) is True
        assert throttle_loop_alive(str(tmp_path)) is True

    def test_missing_heartbeat_is_not_alive(self, tmp_path: Path) -> None:
        from sova.supervisor.pr_throttle import throttle_loop_alive

        assert throttle_loop_alive(tmp_path) is False

    def test_stale_heartbeat_is_not_alive(self, tmp_path: Path) -> None:
        from sova.supervisor.pr_throttle import heartbeat_path, throttle_loop_alive, touch_heartbeat

        touch_heartbeat(tmp_path)
        old = datetime.now(timezone.utc).timestamp() - 3600
        os.utime(heartbeat_path(tmp_path), (old, old))
        assert throttle_loop_alive(tmp_path) is False

    def test_no_heartbeat_means_no_sibling(self, tmp_path: Path) -> None:
        from sova.supervisor.pr_throttle import sibling_loop_alive

        assert sibling_loop_alive(tmp_path) is False

    def test_fresh_heartbeat_of_another_live_process_is_a_sibling(self, tmp_path: Path) -> None:
        from sova.supervisor.pr_throttle import sibling_loop_alive

        _beat_as(tmp_path, os.getpid() + 1)
        with patch("sova.supervisor.pr_throttle.is_process_alive", return_value=True):
            assert sibling_loop_alive(tmp_path) is True

    def test_fresh_heartbeat_of_a_dead_process_is_a_crash_not_a_sibling(self, tmp_path: Path) -> None:
        """A daemon that crashed seconds ago leaves a fresh file behind; nothing is draining the queue."""
        from sova.supervisor.pr_throttle import sibling_loop_alive

        _beat_as(tmp_path, os.getpid() + 1)
        with patch("sova.supervisor.pr_throttle.is_process_alive", return_value=False):
            assert sibling_loop_alive(tmp_path) is False

    def test_fresh_heartbeat_of_this_process_is_not_a_sibling(self, tmp_path: Path) -> None:
        from sova.supervisor.pr_throttle import sibling_loop_alive, touch_heartbeat

        touch_heartbeat(tmp_path, f"{os.getpid()}:abc")
        with patch("sova.supervisor.pr_throttle.is_process_alive", return_value=True):
            assert sibling_loop_alive(tmp_path) is False

    def test_stale_heartbeat_of_a_live_process_is_not_a_sibling(self, tmp_path: Path) -> None:
        from sova.supervisor.pr_throttle import sibling_loop_alive

        os.utime(_beat_as(tmp_path, os.getpid() + 1), (_stale_epoch(), _stale_epoch()))
        with patch("sova.supervisor.pr_throttle.is_process_alive", return_value=True):
            assert sibling_loop_alive(tmp_path) is False

    @pytest.mark.parametrize("pid_text", ["", "no-pid-here", "12x"])
    def test_fresh_heartbeat_with_an_unparseable_pid_is_assumed_a_sibling(self, tmp_path: Path, pid_text: str) -> None:
        """Not provably dead, so recovery must not risk resetting an entry under an in-flight creation."""
        from sova.supervisor.pr_throttle import heartbeat_path, sibling_loop_alive

        path = heartbeat_path(tmp_path).with_name(f"pr-throttle.{pid_text}.heartbeat")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        assert sibling_loop_alive(tmp_path) is True

    @pytest.mark.parametrize("dead_beat_last", [True, False])
    def test_a_dead_processs_heartbeat_does_not_hide_a_live_sibling(self, tmp_path: Path, dead_beat_last: bool) -> None:
        """The daemon (live) and a `make dev` worker that has just exited (dead) both left a fresh file.

        With one shared last-writer file, whichever beat last decided the answer, so a worker that beat
        after the daemon made the daemon invisible and its in-flight `creating` entry look recoverable.
        """
        from sova.supervisor.pr_throttle import sibling_loop_alive

        live_pid, dead_pid = os.getpid() + 1, os.getpid() + 2
        first, second = (live_pid, dead_pid) if dead_beat_last else (dead_pid, live_pid)
        first_path = _beat_as(tmp_path, first)
        second_path = _beat_as(tmp_path, second)
        os.utime(first_path, (time.time() - 5, time.time() - 5))
        assert second_path.stat().st_mtime > first_path.stat().st_mtime
        with patch("sova.supervisor.pr_throttle.is_process_alive", side_effect=lambda pid: pid == live_pid):
            assert sibling_loop_alive(tmp_path) is True

    def test_another_processs_heartbeat_makes_the_loop_alive_for_create_pr(self, tmp_path: Path) -> None:
        from sova.supervisor.pr_throttle import throttle_loop_alive

        _beat_as(tmp_path, os.getpid() + 1)
        assert throttle_loop_alive(tmp_path) is True

    def test_touch_prunes_stale_files_of_crashed_processes_and_keeps_fresh_ones(self, tmp_path: Path) -> None:
        from sova.supervisor.pr_throttle import heartbeat_path, touch_heartbeat

        crashed = _beat_as(tmp_path, os.getpid() + 1)
        os.utime(crashed, (_stale_epoch(), _stale_epoch()))
        sibling = _beat_as(tmp_path, os.getpid() + 2)
        touch_heartbeat(tmp_path, "mine")
        assert not crashed.exists()
        assert sibling.exists()
        assert heartbeat_path(tmp_path).exists()

    def test_clear_removes_the_file(self, tmp_path: Path) -> None:
        from sova.supervisor.pr_throttle import clear_heartbeat, heartbeat_path, throttle_loop_alive, touch_heartbeat

        touch_heartbeat(tmp_path)
        clear_heartbeat(tmp_path)
        assert not heartbeat_path(tmp_path).exists()
        assert throttle_loop_alive(tmp_path) is False

    def test_clear_without_a_heartbeat_is_a_noop(self, tmp_path: Path) -> None:
        from sova.supervisor.pr_throttle import clear_heartbeat

        clear_heartbeat(tmp_path)

    def test_clear_failure_is_swallowed(self, tmp_path: Path) -> None:
        """An unremovable heartbeat is logged; shutdown must still complete."""
        from sova.supervisor.pr_throttle import clear_heartbeat, touch_heartbeat

        touch_heartbeat(tmp_path)
        with patch("pathlib.Path.unlink", side_effect=PermissionError("denied")):
            clear_heartbeat(tmp_path)

    def test_touch_stamps_the_owner_into_the_file(self, tmp_path: Path) -> None:
        from sova.supervisor.pr_throttle import heartbeat_path, throttle_loop_alive, touch_heartbeat

        touch_heartbeat(tmp_path, "1234:abc")
        assert heartbeat_path(tmp_path).read_text(encoding="utf-8") == "1234:abc"
        assert throttle_loop_alive(tmp_path) is True

    def test_clear_removes_a_heartbeat_the_caller_wrote_last(self, tmp_path: Path) -> None:
        from sova.supervisor.pr_throttle import clear_heartbeat, heartbeat_path, throttle_loop_alive, touch_heartbeat

        touch_heartbeat(tmp_path, "mine")
        clear_heartbeat(tmp_path, "mine")
        assert not heartbeat_path(tmp_path).exists()
        assert throttle_loop_alive(tmp_path) is False

    def test_clear_leaves_a_heartbeat_another_loop_in_this_process_wrote(self, tmp_path: Path) -> None:
        """A loop that stops after a newer one started beating must not unmark the newer one as alive."""
        from sova.supervisor.pr_throttle import clear_heartbeat, heartbeat_path, throttle_loop_alive, touch_heartbeat

        touch_heartbeat(tmp_path, "mine")
        touch_heartbeat(tmp_path, "newer-loop")
        clear_heartbeat(tmp_path, "mine")
        assert heartbeat_path(tmp_path).read_text(encoding="utf-8") == "newer-loop"
        assert throttle_loop_alive(tmp_path) is True

    def test_clear_leaves_another_processs_heartbeat(self, tmp_path: Path) -> None:
        """Two processes serving one project each own a file; stopping one must not unmark the other."""
        from sova.supervisor.pr_throttle import clear_heartbeat, throttle_loop_alive, touch_heartbeat

        sibling = _beat_as(tmp_path, os.getpid() + 1)
        touch_heartbeat(tmp_path, "mine")
        clear_heartbeat(tmp_path, "mine")
        assert sibling.exists()
        assert throttle_loop_alive(tmp_path) is True

    def test_clear_leaves_an_unreadable_heartbeat(self, tmp_path: Path) -> None:
        """A heartbeat whose owner cannot be read is not provably ours, so it stays."""
        from sova.supervisor.pr_throttle import clear_heartbeat, heartbeat_path, touch_heartbeat

        touch_heartbeat(tmp_path, "mine")
        heartbeat_path(tmp_path).write_bytes(b"\xff\xfe")
        clear_heartbeat(tmp_path, "mine")
        assert heartbeat_path(tmp_path).exists()

    def test_touch_failure_is_swallowed(self, tmp_path: Path) -> None:
        """A project path that is a file cannot hold .claude/: the write fails, the caller is not told."""
        from sova.supervisor.pr_throttle import throttle_loop_alive, touch_heartbeat

        not_a_dir = tmp_path / "file"
        not_a_dir.write_text("x", encoding="utf-8")
        touch_heartbeat(not_a_dir)
        assert throttle_loop_alive(not_a_dir) is False


async def _null_session_factory() -> AsyncMock:
    """A session factory for loop tests that patch ``process_queue`` and never use the session."""
    session = AsyncMock()
    session.__aenter__.return_value = session
    return session


class TestPRThrottleLoopHeartbeat:
    async def test_loop_touches_heartbeat_each_cycle(self, tmp_path: Path) -> None:
        from sova.db.session import get_session
        from sova.supervisor.pr_throttle import PRThrottleLoop, heartbeat_path, throttle_loop_alive

        async def _session_factory() -> AsyncSession:
            return await get_session()

        loop = PRThrottleLoop(_session_factory, CodeRabbitQuotaConfig(enabled=True, plan="free"), project_dir=tmp_path)
        assert throttle_loop_alive(tmp_path) is False
        with patch("sova.supervisor.pr_throttle._PROCESS_INTERVAL_SECONDS", 0.01):
            task = asyncio.create_task(loop.run())
            await asyncio.sleep(0.05)
            assert heartbeat_path(tmp_path).is_file()
            assert throttle_loop_alive(tmp_path) is True
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async def test_cancelled_loop_stops_reading_as_alive(self, tmp_path: Path) -> None:
        """Daemon shutdown cancels the loop: the heartbeat goes with it, so create_pr falls back at once."""
        from sova.supervisor.pr_throttle import PRThrottleLoop, heartbeat_path, throttle_loop_alive

        loop = PRThrottleLoop(_null_session_factory, CodeRabbitQuotaConfig(enabled=True), project_dir=tmp_path)
        with (
            patch("sova.supervisor.pr_throttle._PROCESS_INTERVAL_SECONDS", 0.01),
            patch("sova.supervisor.pr_throttle.process_queue", new_callable=AsyncMock),
        ):
            task = asyncio.create_task(loop.run())
            await asyncio.sleep(0.05)
            assert throttle_loop_alive(tmp_path) is True
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert throttle_loop_alive(tmp_path) is False
        assert not heartbeat_path(tmp_path).exists()

    async def test_loop_stopped_by_its_event_stops_reading_as_alive(self, tmp_path: Path) -> None:
        from sova.supervisor.pr_throttle import PRThrottleLoop, throttle_loop_alive

        stop = asyncio.Event()
        loop = PRThrottleLoop(
            _null_session_factory, CodeRabbitQuotaConfig(enabled=True), project_dir=tmp_path, stop_event=stop
        )
        with (
            patch("sova.supervisor.pr_throttle._PROCESS_INTERVAL_SECONDS", 0.01),
            patch("sova.supervisor.pr_throttle.process_queue", new_callable=AsyncMock),
        ):
            task = asyncio.create_task(loop.run())
            await asyncio.sleep(0.05)
            assert throttle_loop_alive(tmp_path) is True
            stop.set()
            await asyncio.wait_for(task, timeout=1.0)
        assert throttle_loop_alive(tmp_path) is False

    async def test_stopping_a_loop_keeps_a_sibling_processs_heartbeat(self, tmp_path: Path) -> None:
        """A second server process (e.g. `make dev` next to the daemon) keeps its project reading as alive."""
        from sova.supervisor.pr_throttle import PRThrottleLoop, throttle_loop_alive

        stop = asyncio.Event()
        loop = PRThrottleLoop(
            _null_session_factory, CodeRabbitQuotaConfig(enabled=True), project_dir=tmp_path, stop_event=stop
        )
        with (
            patch("sova.supervisor.pr_throttle._PROCESS_INTERVAL_SECONDS", 0.01),
            patch("sova.supervisor.pr_throttle.process_queue", new_callable=AsyncMock),
        ):
            task = asyncio.create_task(loop.run())
            await asyncio.sleep(0.05)
            stop.set()
            await asyncio.sleep(0)
            # The sibling beats after this loop's last beat but before this loop clears.
            _beat_as(tmp_path, os.getpid() + 1)
            await asyncio.wait_for(task, timeout=1.0)
        assert throttle_loop_alive(tmp_path) is True

    async def test_two_loops_use_distinct_owner_tokens(self, tmp_path: Path) -> None:
        from sova.supervisor.pr_throttle import PRThrottleLoop

        config = CodeRabbitQuotaConfig(enabled=True)
        first = PRThrottleLoop(_null_session_factory, config, project_dir=tmp_path)
        second = PRThrottleLoop(_null_session_factory, config, project_dir=tmp_path)
        assert first._heartbeat_owner != second._heartbeat_owner

    async def test_loop_with_no_project_dir_clears_nothing(self) -> None:
        from sova.supervisor.pr_throttle import PRThrottleLoop

        stop = asyncio.Event()
        stop.set()
        loop = PRThrottleLoop(_null_session_factory, CodeRabbitQuotaConfig(enabled=True), stop_event=stop)
        with patch("sova.supervisor.pr_throttle.clear_heartbeat") as clear:
            await asyncio.wait_for(loop.run(), timeout=1.0)
        clear.assert_not_called()

    async def test_loop_keeps_cycling_when_the_heartbeat_cannot_be_written(self, tmp_path: Path) -> None:
        from sova.supervisor.pr_throttle import PRThrottleLoop

        not_a_dir = tmp_path / "file"
        not_a_dir.write_text("x", encoding="utf-8")

        async def _session_factory() -> AsyncSession:
            from sova.db.session import get_session

            return await get_session()

        loop = PRThrottleLoop(_session_factory, CodeRabbitQuotaConfig(enabled=True), project_dir=not_a_dir)
        with (
            patch("sova.supervisor.pr_throttle._PROCESS_INTERVAL_SECONDS", 0.01),
            patch("sova.supervisor.pr_throttle.process_queue", new_callable=AsyncMock) as process,
        ):
            task = asyncio.create_task(loop.run())
            await asyncio.sleep(0.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert process.await_count >= 2

    async def test_heartbeat_stays_fresh_while_a_queue_pass_runs(self, tmp_path: Path) -> None:
        """A pass slower than the heartbeat's maximum age must not let CreatePRStep bypass the queue."""
        from sova.supervisor.pr_throttle import PRThrottleLoop

        release = asyncio.Event()
        started = asyncio.Event()

        async def _slow_pass(*_args: object, **_kwargs: object) -> None:
            started.set()
            await release.wait()

        session = AsyncMock()
        session.__aenter__.return_value = session

        async def _session_factory() -> AsyncMock:
            return session

        loop = PRThrottleLoop(_session_factory, CodeRabbitQuotaConfig(enabled=True), project_dir=tmp_path)
        with (
            patch("sova.supervisor.pr_throttle._PROCESS_INTERVAL_SECONDS", 0.01),
            patch("sova.supervisor.pr_throttle.process_queue", side_effect=_slow_pass),
            patch("sova.supervisor.pr_throttle.touch_heartbeat") as touch,
        ):
            task = asyncio.create_task(loop.run())
            await started.wait()
            beats_at_start = touch.call_count
            await asyncio.sleep(0.1)
            beats_during_pass = touch.call_count - beats_at_start
            release.set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert beats_during_pass >= 3


# ---------------------------------------------------------------------------
# Starting a project's loop from its own config
# ---------------------------------------------------------------------------


@pytest.fixture
async def clean_throttle_registry() -> AsyncIterator[ModuleType]:
    """Isolate the daemon registry and throttle tasks, and stop any loop a test started."""
    from sova.dashboard import app as app_module

    previous = app_module.get_daemon_components()
    app_module.set_daemon_components({})
    app_module._pr_throttle_tasks.clear()
    yield app_module
    tasks = list(app_module._pr_throttle_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    app_module._pr_throttle_tasks.clear()
    app_module.set_daemon_components(previous)


def _project_cfg(*, enabled: bool, repo: str = "o/r") -> ProjectConfig:
    from sova.config.models import ProjectConfig

    return ProjectConfig(coderabbit_quota=CodeRabbitQuotaConfig(enabled=enabled), github_repo=repo)


class TestEnsurePRThrottleLoop:
    async def test_starts_a_loop_and_registers_it(self, tmp_path: Path, clean_throttle_registry: ModuleType) -> None:
        app_module = clean_throttle_registry
        started = await app_module.ensure_pr_throttle_loop(tmp_path, _project_cfg(enabled=True))
        assert started is True
        registered = app_module.get_daemon_components()[str(tmp_path.resolve())]["pr_throttles"]
        assert len(registered) == 1
        assert len(app_module._pr_throttle_tasks) == 1

    async def test_second_call_for_the_same_project_is_a_noop(
        self, tmp_path: Path, clean_throttle_registry: ModuleType
    ) -> None:
        """Two loops on one project could both pick the same pending entry and open a duplicate PR."""
        app_module = clean_throttle_registry
        cfg = _project_cfg(enabled=True)
        assert await app_module.ensure_pr_throttle_loop(tmp_path, cfg) is True
        assert await app_module.ensure_pr_throttle_loop(tmp_path, cfg) is False
        assert await app_module.ensure_pr_throttle_loop(tmp_path / ".", cfg) is False
        assert len(app_module._pr_throttle_tasks) == 1

    async def test_concurrent_calls_start_one_loop(self, tmp_path: Path, clean_throttle_registry: ModuleType) -> None:
        app_module = clean_throttle_registry
        cfg = _project_cfg(enabled=True)
        results = await asyncio.gather(*(app_module.ensure_pr_throttle_loop(tmp_path, cfg) for _ in range(3)))
        assert results.count(True) == 1
        assert len(app_module._pr_throttle_tasks) == 1

    async def test_failed_recovery_unregisters_the_loop_so_a_retry_can_start_it(
        self, tmp_path: Path, clean_throttle_registry: ModuleType
    ) -> None:
        app_module = clean_throttle_registry
        cfg = _project_cfg(enabled=True)
        with (
            patch(
                "sova.supervisor.pr_throttle.recover_creating_entries",
                new_callable=AsyncMock,
                side_effect=SQLAlchemyError("locked"),
            ),
            pytest.raises(SQLAlchemyError),
        ):
            await app_module.ensure_pr_throttle_loop(tmp_path, cfg)
        assert not app_module.get_daemon_components()[str(tmp_path.resolve())]["pr_throttles"]
        assert app_module._pr_throttle_tasks == []

        assert await app_module.ensure_pr_throttle_loop(tmp_path, cfg) is True
        assert len(app_module._pr_throttle_tasks) == 1

    async def test_concurrent_start_during_recovery_does_not_start_a_second_loop(
        self, tmp_path: Path, clean_throttle_registry: ModuleType
    ) -> None:
        """The first caller is parked in recovery; the second must see the registered loop and return."""
        app_module = clean_throttle_registry
        cfg = _project_cfg(enabled=True)
        in_recovery = asyncio.Event()
        release = asyncio.Event()

        async def _slow_recovery(_session: object) -> int:
            in_recovery.set()
            await release.wait()
            return 0

        with patch("sova.supervisor.pr_throttle.recover_creating_entries", side_effect=_slow_recovery):
            first = asyncio.create_task(app_module.ensure_pr_throttle_loop(tmp_path, cfg))
            await in_recovery.wait()
            assert await app_module.ensure_pr_throttle_loop(tmp_path, cfg) is False
            release.set()
            assert await first is True
        assert len(app_module._pr_throttle_tasks) == 1

    async def test_disabled_quota_starts_nothing(self, tmp_path: Path, clean_throttle_registry: ModuleType) -> None:
        app_module = clean_throttle_registry
        assert await app_module.ensure_pr_throttle_loop(tmp_path, _project_cfg(enabled=False)) is False
        assert app_module.get_daemon_components() == {}
        assert app_module._pr_throttle_tasks == []

    async def test_loop_serves_the_project_it_was_started_for(
        self, tmp_path: Path, clean_throttle_registry: ModuleType
    ) -> None:
        from sova.supervisor.pr_throttle import throttle_loop_alive

        app_module = clean_throttle_registry
        await app_module.ensure_pr_throttle_loop(tmp_path, _project_cfg(enabled=True, repo="o/r"))
        await asyncio.sleep(0.05)
        throttle = app_module.get_daemon_components()[str(tmp_path.resolve())]["pr_throttles"][0]
        assert throttle._project_slug == "o/r"
        assert throttle._project_dir == tmp_path
        assert throttle_loop_alive(tmp_path) is True

    async def test_leaves_a_creating_entry_alone_while_a_live_sibling_serves_the_project(
        self, tmp_path: Path, clean_throttle_registry: ModuleType
    ) -> None:
        """A `make dev` dashboard reloading next to the daemon must not reset the daemon's in-flight creation.

        Reset to pending, the entry would be picked up by the new loop and `gh pr create` run a second time.
        """
        run_id = await _create_task_run()
        await _insert_entry(run_id, "creating")
        _beat_as(tmp_path, os.getpid() + 1)
        app_module = clean_throttle_registry
        with (
            patch("sova.supervisor.pr_throttle.is_process_alive", return_value=True),
            patch("sova.supervisor.pr_throttle.process_queue", new_callable=AsyncMock),
        ):
            started = await app_module.ensure_pr_throttle_loop(tmp_path, _project_cfg(enabled=True))
        assert started is True
        assert [e.status for e in await _queue_entries(run_id)] == ["creating"]

    async def test_leaves_a_creating_entry_alone_when_a_dead_workers_heartbeat_is_the_newest(
        self, tmp_path: Path, clean_throttle_registry: ModuleType
    ) -> None:
        """A routine `make dev` reload: the old worker (dead) beat after the daemon (live), then exited.

        The daemon's own file still reads fresh, so its in-flight `creating` entry must not be reset to
        `pending`, or the new worker's first cycle would run `gh pr create` for it a second time.
        """
        daemon_pid, dead_worker_pid = os.getpid() + 1, os.getpid() + 2
        run_id = await _create_task_run()
        await _insert_entry(run_id, "creating")
        daemon_beat = _beat_as(tmp_path, daemon_pid)
        os.utime(daemon_beat, (time.time() - 5, time.time() - 5))
        _beat_as(tmp_path, dead_worker_pid)
        app_module = clean_throttle_registry
        with (
            patch("sova.supervisor.pr_throttle.is_process_alive", side_effect=lambda pid: pid == daemon_pid),
            patch("sova.supervisor.pr_throttle.process_queue", new_callable=AsyncMock),
        ):
            await app_module.ensure_pr_throttle_loop(tmp_path, _project_cfg(enabled=True))
        assert [e.status for e in await _queue_entries(run_id)] == ["creating"]

    async def test_recovers_creating_entries_after_a_crash_left_a_fresh_heartbeat(
        self, tmp_path: Path, clean_throttle_registry: ModuleType
    ) -> None:
        """A restart seconds after a crash still sees the dead daemon's heartbeat; that is not a sibling."""
        run_id = await _create_task_run()
        await _insert_entry(run_id, "creating")
        _beat_as(tmp_path, os.getpid() + 1)
        app_module = clean_throttle_registry
        with (
            patch("sova.supervisor.pr_throttle.is_process_alive", return_value=False),
            patch("sova.supervisor.pr_throttle.process_queue", new_callable=AsyncMock),
        ):
            await app_module.ensure_pr_throttle_loop(tmp_path, _project_cfg(enabled=True))
        assert [e.status for e in await _queue_entries(run_id)] == ["pending"]

    async def test_recovers_creating_entries_before_the_loop_runs(
        self, tmp_path: Path, clean_throttle_registry: ModuleType
    ) -> None:
        run_id = await _create_task_run()
        await _insert_entry(run_id, "creating")
        app_module = clean_throttle_registry
        with patch("sova.supervisor.pr_throttle._PROCESS_INTERVAL_SECONDS", 3600):
            await app_module.ensure_pr_throttle_loop(tmp_path, _project_cfg(enabled=True, repo="x/never-matches"))
        entries = await _queue_entries(run_id)
        assert [e.status for e in entries] == ["pending"]


async def _insert_entry(run_id: int, status: str) -> None:
    from sova.db.models import PRCreationQueue
    from sova.db.session import get_session

    async with await get_session() as session:
        async with session.begin():
            session.add(
                PRCreationQueue(
                    task_run_id=run_id,
                    title="PR",
                    body="body",
                    base_branch="main",
                    head_branch="feat/x",
                    status=status,
                    project_slug="x/never-matches",
                )
            )


class TestStartPRThrottleLoops:
    """The lifespan scan judges each project by its own coderabbit_quota, not the root config's."""

    @staticmethod
    def _projects(tmp_path: Path, names: list[str]) -> dict[str, str]:
        projects = {}
        for name in names:
            d = tmp_path / name
            d.mkdir()
            projects[name] = str(d)
        return projects

    async def test_single_project_uses_its_own_config(
        self, tmp_path: Path, clean_throttle_registry: ModuleType
    ) -> None:
        app_module = clean_throttle_registry
        await app_module._start_pr_throttle_loops(tmp_path, _project_cfg(enabled=True), is_multi=False)
        assert str(tmp_path.resolve()) in app_module.get_daemon_components()

    async def test_single_project_with_quota_disabled_starts_nothing(
        self, tmp_path: Path, clean_throttle_registry: ModuleType
    ) -> None:
        app_module = clean_throttle_registry
        await app_module._start_pr_throttle_loops(tmp_path, _project_cfg(enabled=False), is_multi=False)
        assert app_module._pr_throttle_tasks == []

    async def test_multi_project_starts_loops_even_when_the_root_config_has_the_quota_off(
        self, tmp_path: Path, clean_throttle_registry: ModuleType
    ) -> None:
        """The root config used to gate the whole scan, so a project that enabled the quota never got a loop."""
        app_module = clean_throttle_registry
        projects = self._projects(tmp_path, ["on", "off"])
        configs = {
            projects["on"]: _project_cfg(enabled=True, repo="o/on"),
            projects["off"]: _project_cfg(enabled=False),
        }

        with (
            patch.object(app_module, "list_projects", return_value=projects),
            patch.object(app_module, "_try_load_config", side_effect=lambda p: configs[str(p)]),
        ):
            await app_module._start_pr_throttle_loops(tmp_path, _project_cfg(enabled=False), is_multi=True)

        registered = app_module.get_daemon_components()
        assert str((tmp_path / "on").resolve()) in registered
        assert str((tmp_path / "off").resolve()) not in registered
        assert str(tmp_path.resolve()) not in registered

    async def test_broken_config_and_missing_directory_do_not_stop_other_projects(
        self, tmp_path: Path, clean_throttle_registry: ModuleType
    ) -> None:
        app_module = clean_throttle_registry
        projects = self._projects(tmp_path, ["broken", "good"])
        projects["gone"] = str(tmp_path / "does-not-exist")
        configs = {projects["broken"]: None, projects["good"]: _project_cfg(enabled=True)}

        with (
            patch.object(app_module, "list_projects", return_value=projects),
            patch.object(app_module, "_try_load_config", side_effect=lambda p: configs[str(p)]),
        ):
            await app_module._start_pr_throttle_loops(tmp_path, _project_cfg(enabled=False), is_multi=True)

        assert list(app_module.get_daemon_components()) == [str((tmp_path / "good").resolve())]

    async def test_one_project_failing_to_start_does_not_abort_the_scan(
        self, tmp_path: Path, clean_throttle_registry: ModuleType
    ) -> None:
        app_module = clean_throttle_registry
        projects = self._projects(tmp_path, ["first", "second"])
        configs = {projects["first"]: _project_cfg(enabled=True), projects["second"]: _project_cfg(enabled=True)}
        real_ensure = app_module.ensure_pr_throttle_loop

        async def _ensure(project_dir: Path, cfg: ProjectConfig) -> bool:
            if project_dir.name == "first":
                raise OSError("database is locked")
            return await real_ensure(project_dir, cfg)

        with (
            patch.object(app_module, "list_projects", return_value=projects),
            patch.object(app_module, "_try_load_config", side_effect=lambda p: configs[str(p)]),
            patch.object(app_module, "ensure_pr_throttle_loop", side_effect=_ensure),
        ):
            await app_module._start_pr_throttle_loops(tmp_path, _project_cfg(enabled=False), is_multi=True)

        assert list(app_module.get_daemon_components()) == [str((tmp_path / "second").resolve())]
