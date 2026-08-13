"""Tests for merge queue monitor and merge queue entry management."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.config.models import IntegrationConfig, NotificationConfig
from sova.dashboard.services.merge_queue_monitor import (
    MergeQueueMonitor,
    _load_queued_entries,
    _update_entry_status,
    create_merge_queue_entry,
    create_monitors_for_merge_queue,
)
from sova.db.models import MergeQueueEntry
from sova.db.session import close_db, get_session, init_db
from sova.git.merge import MergeQueueStatus


@pytest.fixture(autouse=True)
async def setup_db(monkeypatch: pytest.MonkeyPatch):
    """Initialize an in-memory DB for merge queue tests."""
    monkeypatch.setenv("SOVA_DATABASE_URL", "sqlite+aiosqlite://")
    await init_db(run_migrations=False)
    yield
    await close_db()


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    worktree_dir = tmp_path / ".claude" / "worktrees"
    worktree_dir.mkdir(parents=True)
    return tmp_path


def _make_monitor(project_dir: Path, **overrides: object) -> MergeQueueMonitor:
    defaults = {
        "project_dir": project_dir,
        "repo": "owner/repo",
        "github_user": "testuser",
        "integration_config": IntegrationConfig(merge_queue_poll_interval=1),
        "notification_config": NotificationConfig(desktop=True),
    }
    defaults.update(overrides)
    return MergeQueueMonitor(**defaults)


def _make_entry(**overrides: object) -> dict:
    defaults: dict = {
        "id": 1,
        "pr_number": 42,
        "repo": "owner/repo",
        "github_user": "user1",
        "issue_number": "10",
        "branch_name": "feat/test",
        "enqueued_at": datetime.now(timezone.utc),
    }
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# create_merge_queue_entry
# ---------------------------------------------------------------------------


class TestCreateMergeQueueEntry:
    @pytest.mark.asyncio
    async def test_creates_entry(self, project_dir: Path) -> None:
        entry_id = await create_merge_queue_entry(
            pr_number=42,
            repo="owner/repo",
            project_dir=project_dir,
            issue_number="10",
            task_run_id=None,
            github_user="user1",
            branch_name="feat/test",
        )
        assert entry_id is not None

        entries = await _load_queued_entries(project_dir)
        assert len(entries) == 1
        assert entries[0]["pr_number"] == 42
        assert entries[0]["repo"] == "owner/repo"
        assert entries[0]["issue_number"] == "10"
        assert entries[0]["github_user"] == "user1"
        assert entries[0]["branch_name"] == "feat/test"

    @pytest.mark.asyncio
    async def test_duplicate_returns_none(self, project_dir: Path) -> None:
        first = await create_merge_queue_entry(
            pr_number=42,
            repo="owner/repo",
            project_dir=project_dir,
        )
        assert first is not None

        second = await create_merge_queue_entry(
            pr_number=42,
            repo="owner/repo",
            project_dir=project_dir,
        )
        assert second is None

        entries = await _load_queued_entries(project_dir)
        assert len(entries) == 1

    @pytest.mark.asyncio
    async def test_different_pr_creates_separate_entries(self, project_dir: Path) -> None:
        await create_merge_queue_entry(pr_number=42, repo="owner/repo", project_dir=project_dir)
        await create_merge_queue_entry(pr_number=43, repo="owner/repo", project_dir=project_dir)

        entries = await _load_queued_entries(project_dir)
        assert len(entries) == 2


# ---------------------------------------------------------------------------
# _load_queued_entries / _update_entry_status
# ---------------------------------------------------------------------------


class TestLoadAndUpdateEntries:
    @pytest.mark.asyncio
    async def test_empty_db_returns_empty(self, project_dir: Path) -> None:
        entries = await _load_queued_entries(project_dir)
        assert entries == []

    @pytest.mark.asyncio
    async def test_update_status_changes_entry(self, project_dir: Path) -> None:
        entry_id = await create_merge_queue_entry(
            pr_number=42,
            repo="owner/repo",
            project_dir=project_dir,
        )
        assert entry_id is not None

        await _update_entry_status(entry_id, "merged", project_dir)

        entries = await _load_queued_entries(project_dir)
        assert len(entries) == 0

        async with await get_session(project_dir=project_dir) as session:
            from sqlalchemy import select

            result = await session.execute(select(MergeQueueEntry).where(MergeQueueEntry.id == entry_id))
            e = result.scalars().first()
            assert e is not None
            assert e.status == "merged"
            assert e.resolved_at is not None

    @pytest.mark.asyncio
    async def test_update_nonexistent_entry_is_noop(self, project_dir: Path) -> None:
        await _update_entry_status(9999, "merged", project_dir)


# ---------------------------------------------------------------------------
# MergeQueueMonitor._poll_cycle
# ---------------------------------------------------------------------------


class TestPollCycle:
    @pytest.mark.asyncio
    async def test_skips_when_rate_limited(self, project_dir: Path) -> None:
        monitor = _make_monitor(project_dir)

        tracker = MagicMock()
        tracker.should_skip.return_value = True
        with patch(
            "sova.supervisor.github_quota.get_github_quota_tracker",
            return_value=tracker,
        ):
            await monitor._poll_cycle()

        tracker.should_skip.assert_called_once()

    @pytest.mark.asyncio
    async def test_noop_when_no_entries(self, project_dir: Path) -> None:
        monitor = _make_monitor(project_dir)

        tracker = MagicMock()
        tracker.should_skip.return_value = False
        with patch(
            "sova.supervisor.github_quota.get_github_quota_tracker",
            return_value=tracker,
        ):
            await monitor._poll_cycle()

    @pytest.mark.asyncio
    async def test_calls_check_entry_for_each(self, project_dir: Path) -> None:
        await create_merge_queue_entry(pr_number=1, repo="owner/repo", project_dir=project_dir)
        await create_merge_queue_entry(pr_number=2, repo="owner/repo", project_dir=project_dir)

        monitor = _make_monitor(project_dir)

        tracker = MagicMock()
        tracker.should_skip.return_value = False

        with (
            patch(
                "sova.supervisor.github_quota.get_github_quota_tracker",
                return_value=tracker,
            ),
            patch.object(monitor, "_check_entry", new_callable=AsyncMock) as mock_check,
        ):
            await monitor._poll_cycle()

        assert mock_check.call_count == 2


# ---------------------------------------------------------------------------
# MergeQueueMonitor._check_entry
# ---------------------------------------------------------------------------


class TestCheckEntry:
    @pytest.mark.asyncio
    async def test_merged_triggers_handle_merged(self, project_dir: Path) -> None:
        monitor = _make_monitor(project_dir)
        entry = {
            "id": 1,
            "pr_number": 42,
            "repo": "owner/repo",
            "github_user": "user1",
            "issue_number": "10",
            "branch_name": "feat/test",
            "enqueued_at": datetime.now(timezone.utc),
        }

        merged_status = MergeQueueStatus(in_queue=False, state="MERGED", position=None, estimated_time="")
        with (
            patch("sova.git.merge.get_merge_queue_status", new_callable=AsyncMock, return_value=merged_status),
            patch.object(monitor, "_handle_merged", new_callable=AsyncMock) as mock_merged,
        ):
            await monitor._check_entry(entry)

        mock_merged.assert_called_once_with(entry)

    @pytest.mark.asyncio
    async def test_failed_triggers_handle_ejected(self, project_dir: Path) -> None:
        monitor = _make_monitor(project_dir)
        entry = {
            "id": 1,
            "pr_number": 42,
            "repo": "owner/repo",
            "github_user": "user1",
            "issue_number": "10",
            "branch_name": "feat/test",
            "enqueued_at": datetime.now(timezone.utc),
        }

        failed_status = MergeQueueStatus(in_queue=True, state="UNMERGEABLE", position=2, estimated_time="")
        with (
            patch("sova.git.merge.get_merge_queue_status", new_callable=AsyncMock, return_value=failed_status),
            patch.object(monitor, "_handle_ejected", new_callable=AsyncMock) as mock_ejected,
        ):
            await monitor._check_entry(entry)

        mock_ejected.assert_called_once_with(entry, "UNMERGEABLE")

    @pytest.mark.asyncio
    async def test_timeout_triggers_handle_timeout(self, project_dir: Path) -> None:
        monitor = _make_monitor(
            project_dir,
            integration_config=IntegrationConfig(merge_queue_timeout=10, merge_queue_poll_interval=1),
        )
        entry = {
            "id": 1,
            "pr_number": 42,
            "repo": "owner/repo",
            "github_user": "user1",
            "issue_number": "10",
            "branch_name": "feat/test",
            "enqueued_at": datetime.now(timezone.utc) - timedelta(seconds=20),
        }

        queued_status = MergeQueueStatus(in_queue=True, state="QUEUED", position=3, estimated_time="5m")
        with (
            patch("sova.git.merge.get_merge_queue_status", new_callable=AsyncMock, return_value=queued_status),
            patch.object(monitor, "_handle_timeout", new_callable=AsyncMock) as mock_timeout,
        ):
            await monitor._check_entry(entry)

        mock_timeout.assert_called_once_with(entry)

    @pytest.mark.asyncio
    async def test_still_queued_does_not_act(self, project_dir: Path) -> None:
        monitor = _make_monitor(
            project_dir,
            integration_config=IntegrationConfig(merge_queue_timeout=3600, merge_queue_poll_interval=1),
        )
        entry = {
            "id": 1,
            "pr_number": 42,
            "repo": "owner/repo",
            "github_user": "user1",
            "issue_number": "10",
            "branch_name": "feat/test",
            "enqueued_at": datetime.now(timezone.utc),
        }

        queued_status = MergeQueueStatus(in_queue=True, state="QUEUED", position=1, estimated_time="2m")
        with (
            patch("sova.git.merge.get_merge_queue_status", new_callable=AsyncMock, return_value=queued_status),
            patch.object(monitor, "_handle_merged", new_callable=AsyncMock) as mock_merged,
            patch.object(monitor, "_handle_ejected", new_callable=AsyncMock) as mock_ejected,
            patch.object(monitor, "_handle_timeout", new_callable=AsyncMock) as mock_timeout,
        ):
            await monitor._check_entry(entry)

        mock_merged.assert_not_called()
        mock_ejected.assert_not_called()
        mock_timeout.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_queued_merged_directly(self, project_dir: Path) -> None:
        monitor = _make_monitor(project_dir)
        entry = _make_entry()

        not_queued = MergeQueueStatus(in_queue=False, state="NOT_QUEUED", position=None, estimated_time="")
        with (
            patch("sova.git.merge.get_merge_queue_status", new_callable=AsyncMock, return_value=not_queued),
            patch.object(monitor, "_check_pr_merged_directly", new_callable=AsyncMock, return_value=True),
            patch.object(monitor, "_handle_merged", new_callable=AsyncMock) as mock_merged,
            patch.object(monitor, "_handle_ejected", new_callable=AsyncMock) as mock_ejected,
        ):
            await monitor._check_entry(entry)

        mock_merged.assert_called_once_with(entry)
        mock_ejected.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_queued_not_merged_triggers_ejected(self, project_dir: Path) -> None:
        monitor = _make_monitor(project_dir)
        entry = _make_entry()

        not_queued = MergeQueueStatus(in_queue=False, state="NOT_QUEUED", position=None, estimated_time="")
        with (
            patch("sova.git.merge.get_merge_queue_status", new_callable=AsyncMock, return_value=not_queued),
            patch.object(monitor, "_check_pr_merged_directly", new_callable=AsyncMock, return_value=False),
            patch.object(monitor, "_handle_merged", new_callable=AsyncMock) as mock_merged,
            patch.object(monitor, "_handle_ejected", new_callable=AsyncMock) as mock_ejected,
        ):
            await monitor._check_entry(entry)

        mock_ejected.assert_called_once_with(entry, "NOT_QUEUED")
        mock_merged.assert_not_called()


# ---------------------------------------------------------------------------
# MergeQueueMonitor._handle_merged
# ---------------------------------------------------------------------------


class TestHandleMerged:
    @pytest.mark.asyncio
    async def test_deletes_branch_and_transitions_issue(self, project_dir: Path) -> None:
        entry_id = await create_merge_queue_entry(
            pr_number=42,
            repo="owner/repo",
            project_dir=project_dir,
            issue_number="10",
            branch_name="feat/test",
            github_user="user1",
        )
        assert entry_id is not None

        entries = await _load_queued_entries(project_dir)
        entry = entries[0]

        monitor = _make_monitor(project_dir)

        with (
            patch("sova.git.merge.delete_remote_branch", new_callable=AsyncMock) as mock_delete,
            patch.object(monitor, "_transition_issue_state", new_callable=AsyncMock) as mock_transition,
            patch.object(monitor, "_cleanup_local_worktree", new_callable=AsyncMock) as mock_cleanup,
            patch("sova.dashboard.services.feed_service.emit_safe") as mock_emit,
            patch.object(monitor, "_notify"),
        ):
            await monitor._handle_merged(entry)

        mock_delete.assert_called_once_with("feat/test", repo="owner/repo", github_user="user1")
        mock_transition.assert_called_once_with("10", "owner/repo", "user1")
        mock_cleanup.assert_called_once()
        mock_emit.assert_called_once()

        resolved_entries = await _load_queued_entries(project_dir)
        assert len(resolved_entries) == 0

    @pytest.mark.asyncio
    async def test_no_branch_skips_delete(self, project_dir: Path) -> None:
        await create_merge_queue_entry(
            pr_number=42,
            repo="owner/repo",
            project_dir=project_dir,
            issue_number="10",
        )
        entries = await _load_queued_entries(project_dir)
        entry = entries[0]

        monitor = _make_monitor(project_dir)

        with (
            patch("sova.git.merge.delete_remote_branch", new_callable=AsyncMock) as mock_delete,
            patch.object(monitor, "_transition_issue_state", new_callable=AsyncMock),
            patch.object(monitor, "_cleanup_local_worktree", new_callable=AsyncMock),
            patch("sova.dashboard.services.feed_service.emit_safe"),
            patch.object(monitor, "_notify"),
        ):
            await monitor._handle_merged(entry)

        mock_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_issue_skips_transition(self, project_dir: Path) -> None:
        await create_merge_queue_entry(
            pr_number=42,
            repo="owner/repo",
            project_dir=project_dir,
            branch_name="feat/test",
        )
        entries = await _load_queued_entries(project_dir)
        entry = entries[0]

        monitor = _make_monitor(project_dir)

        with (
            patch("sova.git.merge.delete_remote_branch", new_callable=AsyncMock),
            patch.object(monitor, "_transition_issue_state", new_callable=AsyncMock) as mock_transition,
            patch.object(monitor, "_cleanup_local_worktree", new_callable=AsyncMock),
            patch("sova.dashboard.services.feed_service.emit_safe"),
            patch.object(monitor, "_notify"),
        ):
            await monitor._handle_merged(entry)

        mock_transition.assert_not_called()


# ---------------------------------------------------------------------------
# MergeQueueMonitor._handle_ejected
# ---------------------------------------------------------------------------


class TestHandleEjected:
    @pytest.mark.asyncio
    async def test_updates_status_and_emits(self, project_dir: Path) -> None:
        await create_merge_queue_entry(
            pr_number=42,
            repo="owner/repo",
            project_dir=project_dir,
        )
        entries = await _load_queued_entries(project_dir)
        entry = entries[0]

        monitor = _make_monitor(project_dir)

        with (
            patch("sova.dashboard.services.feed_service.emit_safe") as mock_emit,
            patch.object(monitor, "_notify"),
        ):
            await monitor._handle_ejected(entry, "UNMERGEABLE")

        mock_emit.assert_called_once()
        args = mock_emit.call_args
        assert "UNMERGEABLE" in args[0][0]

        remaining = await _load_queued_entries(project_dir)
        assert len(remaining) == 0


# ---------------------------------------------------------------------------
# MergeQueueMonitor._handle_timeout
# ---------------------------------------------------------------------------


class TestHandleTimeout:
    @pytest.mark.asyncio
    async def test_updates_status_and_emits(self, project_dir: Path) -> None:
        await create_merge_queue_entry(
            pr_number=42,
            repo="owner/repo",
            project_dir=project_dir,
        )
        entries = await _load_queued_entries(project_dir)
        entry = entries[0]

        monitor = _make_monitor(project_dir)

        with (
            patch("sova.dashboard.services.feed_service.emit_safe") as mock_emit,
            patch.object(monitor, "_notify"),
        ):
            await monitor._handle_timeout(entry)

        mock_emit.assert_called_once()

        remaining = await _load_queued_entries(project_dir)
        assert len(remaining) == 0


# ---------------------------------------------------------------------------
# MergeQueueMonitor._cleanup_local_worktree
# ---------------------------------------------------------------------------


class TestCleanupLocalWorktree:
    @pytest.mark.asyncio
    async def test_removes_worktree_if_exists(self, project_dir: Path) -> None:
        worktree_path = project_dir / ".claude" / "worktrees" / "10"
        worktree_path.mkdir(parents=True)

        monitor = _make_monitor(project_dir)
        entry = {"issue_number": "10"}

        with patch(
            "sova.git.worktree.cleanup_worktree",
            new_callable=AsyncMock,
        ) as mock_cleanup:
            await monitor._cleanup_local_worktree(entry)

        mock_cleanup.assert_called_once_with(worktree_path, cwd=project_dir)

    @pytest.mark.asyncio
    async def test_noop_if_no_issue(self, project_dir: Path) -> None:
        monitor = _make_monitor(project_dir)
        entry = {"issue_number": None}

        with patch(
            "sova.git.worktree.cleanup_worktree",
            new_callable=AsyncMock,
        ) as mock_cleanup:
            await monitor._cleanup_local_worktree(entry)

        mock_cleanup.assert_not_called()

    @pytest.mark.asyncio
    async def test_noop_if_worktree_missing(self, project_dir: Path) -> None:
        monitor = _make_monitor(project_dir)
        entry = {"issue_number": "99"}

        with patch(
            "sova.git.worktree.cleanup_worktree",
            new_callable=AsyncMock,
        ) as mock_cleanup:
            await monitor._cleanup_local_worktree(entry)

        mock_cleanup.assert_not_called()


# ---------------------------------------------------------------------------
# MergeQueueMonitor.run_loop
# ---------------------------------------------------------------------------


class TestRunLoop:
    @pytest.mark.asyncio
    async def test_stops_on_event(self, project_dir: Path) -> None:
        monitor = _make_monitor(project_dir)

        call_count = 0

        async def counting_poll():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                monitor.stop()

        with patch.object(monitor, "_poll_cycle", side_effect=counting_poll):
            await monitor.run_loop()

        assert call_count == 2

    @pytest.mark.asyncio
    async def test_continues_on_poll_error(self, project_dir: Path) -> None:
        monitor = _make_monitor(project_dir)

        call_count = 0

        async def failing_then_stopping():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("test error")
            monitor.stop()

        with patch.object(monitor, "_poll_cycle", side_effect=failing_then_stopping):
            await monitor.run_loop()

        assert call_count == 2


# ---------------------------------------------------------------------------
# MergeQueueMonitor._notify
# ---------------------------------------------------------------------------


class TestNotify:
    def test_sends_notification_when_configured(self, project_dir: Path) -> None:
        monitor = _make_monitor(project_dir)

        with patch("sova.ipc.notifications.notify") as mock_notify:
            monitor._notify(subtitle="Test", message="Hello")

        mock_notify.assert_called_once()

    def test_noop_when_no_config(self, project_dir: Path) -> None:
        monitor = _make_monitor(project_dir, notification_config=None)
        monitor._notify(subtitle="Test", message="Hello")


# ---------------------------------------------------------------------------
# _check_merge_queue_marker_file
# ---------------------------------------------------------------------------


class TestCheckMergeQueueMarkerFile:
    @pytest.mark.asyncio
    async def test_creates_entry_from_marker(self, project_dir: Path) -> None:
        from sova.dashboard.services.agent_lifecycle import _check_merge_queue_marker_file

        agent_control = project_dir / ".claude" / "agent-control"
        agent_control.mkdir(parents=True)
        marker = agent_control / "merge-queue.json"
        marker.write_text(
            json.dumps(
                {
                    "pr_number": 42,
                    "repo": "owner/repo",
                    "issue_number": "10",
                    "branch_name": "feat/test",
                    "github_user": "user1",
                }
            )
        )

        agent = MagicMock()
        agent.cwd = str(project_dir)
        agent.project_dir = project_dir

        with (
            patch(
                "sova.dashboard.services.merge_queue_monitor.create_merge_queue_entry",
                new_callable=AsyncMock,
                return_value=1,
            ) as mock_create,
            patch("sova.dashboard.services.feed_service.emit_safe"),
        ):
            await _check_merge_queue_marker_file(agent, run_id=100)

        mock_create.assert_called_once_with(
            pr_number=42,
            repo="owner/repo",
            project_dir=project_dir,
            issue_number="10",
            task_run_id=100,
            github_user="user1",
            branch_name="feat/test",
        )
        assert not marker.exists()

    @pytest.mark.asyncio
    async def test_noop_when_no_marker(self, project_dir: Path) -> None:
        from sova.dashboard.services.agent_lifecycle import _check_merge_queue_marker_file

        agent = MagicMock()
        agent.cwd = str(project_dir)
        agent.project_dir = project_dir

        with patch(
            "sova.dashboard.services.merge_queue_monitor.create_merge_queue_entry",
            new_callable=AsyncMock,
        ) as mock_create:
            await _check_merge_queue_marker_file(agent, run_id=100)

        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_noop_when_no_cwd(self) -> None:
        from sova.dashboard.services.agent_lifecycle import _check_merge_queue_marker_file

        agent = MagicMock()
        agent.cwd = None
        agent.project_dir = None

        await _check_merge_queue_marker_file(agent, run_id=100)

    @pytest.mark.asyncio
    async def test_noop_when_missing_pr_number(self, project_dir: Path) -> None:
        from sova.dashboard.services.agent_lifecycle import _check_merge_queue_marker_file

        agent_control = project_dir / ".claude" / "agent-control"
        agent_control.mkdir(parents=True)
        marker = agent_control / "merge-queue.json"
        marker.write_text(json.dumps({"repo": "owner/repo"}))

        agent = MagicMock()
        agent.cwd = str(project_dir)
        agent.project_dir = project_dir

        with patch(
            "sova.dashboard.services.merge_queue_monitor.create_merge_queue_entry",
            new_callable=AsyncMock,
        ) as mock_create:
            await _check_merge_queue_marker_file(agent, run_id=100)

        mock_create.assert_not_called()


# ---------------------------------------------------------------------------
# _check_merge_queue_on_failure
# ---------------------------------------------------------------------------


class TestCheckMergeQueueOnFailure:
    @pytest.mark.asyncio
    async def test_creates_entry_when_in_queue(self, project_dir: Path) -> None:
        from sova.dashboard.services.agent_lifecycle import _check_merge_queue_on_failure

        agent = MagicMock()
        agent.project_dir = project_dir
        agent.pr_number = 42
        agent.issue = "10"

        queued_status = MergeQueueStatus(in_queue=True, state="QUEUED", position=3, estimated_time="5m")

        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.git.merge.get_merge_queue_status", new_callable=AsyncMock, return_value=queued_status),
            patch(
                "sova.dashboard.services.merge_queue_monitor.create_merge_queue_entry",
                new_callable=AsyncMock,
                return_value=1,
            ),
            patch("sova.dashboard.services.feed_service.emit_safe"),
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value={}),
            patch(
                "sova.utils.shell.run",
                new_callable=AsyncMock,
                return_value=MagicMock(success=True, stdout="feat/test\n"),
            ),
        ):
            cfg = MagicMock()
            cfg.github_repo = "owner/repo"
            cfg.github_user = "testuser"
            mock_cfg.return_value = cfg

            status, exit_code = await _check_merge_queue_on_failure(
                agent,
                run_id=100,
                status="failed",
                exit_code=1,
            )

        assert status == "done"
        assert exit_code == 0

    @pytest.mark.asyncio
    async def test_returns_unchanged_when_not_in_queue(self, project_dir: Path) -> None:
        from sova.dashboard.services.agent_lifecycle import _check_merge_queue_on_failure

        agent = MagicMock()
        agent.project_dir = project_dir
        agent.pr_number = 42
        agent.issue = "10"

        not_queued = MergeQueueStatus(in_queue=False, state="NOT_QUEUED", position=None, estimated_time="")

        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.git.merge.get_merge_queue_status", new_callable=AsyncMock, return_value=not_queued),
        ):
            cfg = MagicMock()
            cfg.github_repo = "owner/repo"
            cfg.github_user = "testuser"
            mock_cfg.return_value = cfg

            status, exit_code = await _check_merge_queue_on_failure(
                agent,
                run_id=100,
                status="failed",
                exit_code=1,
            )

        assert status == "failed"
        assert exit_code == 1

    @pytest.mark.asyncio
    async def test_returns_unchanged_when_no_repo(self, project_dir: Path) -> None:
        from sova.dashboard.services.agent_lifecycle import _check_merge_queue_on_failure

        agent = MagicMock()
        agent.project_dir = project_dir
        agent.pr_number = 42

        with patch("sova.config.loader.load_config") as mock_cfg:
            cfg = MagicMock()
            cfg.github_repo = ""
            mock_cfg.return_value = cfg

            status, exit_code = await _check_merge_queue_on_failure(
                agent,
                run_id=100,
                status="failed",
                exit_code=1,
            )

        assert status == "failed"
        assert exit_code == 1


# ---------------------------------------------------------------------------
# MergeQueueEntry DB model
# ---------------------------------------------------------------------------


class TestMergeQueueEntryModel:
    @pytest.mark.asyncio
    async def test_model_fields(self) -> None:
        entry = MergeQueueEntry(
            pr_number=42,
            repo="owner/repo",
            issue_number="10",
            project_dir="/tmp/test",
            enqueued_at=datetime.now(timezone.utc),
            github_user="user1",
            branch_name="feat/test",
        )
        assert entry.pr_number == 42
        assert entry.status is None or entry.status == "queued"
        assert entry.resolved_at is None

    @pytest.mark.asyncio
    async def test_roundtrip(self, project_dir: Path) -> None:
        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                entry = MergeQueueEntry(
                    pr_number=42,
                    repo="owner/repo",
                    issue_number="10",
                    project_dir=str(project_dir),
                    enqueued_at=datetime.now(timezone.utc),
                    github_user="user1",
                    branch_name="feat/test",
                    status="queued",
                )
                session.add(entry)

        async with await get_session(project_dir=project_dir) as session:
            from sqlalchemy import select

            result = await session.execute(select(MergeQueueEntry))
            e = result.scalars().first()
            assert e is not None
            assert e.pr_number == 42
            assert e.repo == "owner/repo"
            assert e.status == "queued"
            assert e.branch_name == "feat/test"


# ---------------------------------------------------------------------------
# create_monitors_for_merge_queue
# ---------------------------------------------------------------------------


class TestCreateMonitorsForMergeQueue:
    def test_creates_monitors_for_eligible_projects(self, project_dir: Path) -> None:
        with (
            patch(
                "sova.config.registry.list_projects",
                return_value={"test": str(project_dir)},
            ),
            patch("sova.config.loader.load_config") as mock_cfg,
        ):
            cfg = MagicMock()
            cfg.integration = IntegrationConfig(merge_queue_enabled="auto")
            cfg.github_repo = "owner/repo"
            cfg.github_user = "testuser"
            cfg.notification = NotificationConfig(desktop=True)
            mock_cfg.return_value = cfg

            monitors = create_monitors_for_merge_queue()

        assert len(monitors) == 1
        assert monitors[0].repo == "owner/repo"

    def test_skips_disabled_projects(self, project_dir: Path) -> None:
        with (
            patch(
                "sova.config.registry.list_projects",
                return_value={"test": str(project_dir)},
            ),
            patch("sova.config.loader.load_config") as mock_cfg,
        ):
            cfg = MagicMock()
            cfg.integration = IntegrationConfig(merge_queue_enabled="false")
            cfg.github_repo = "owner/repo"
            cfg.github_user = "testuser"
            mock_cfg.return_value = cfg

            monitors = create_monitors_for_merge_queue()

        assert len(monitors) == 0

    def test_skips_projects_without_repo(self, project_dir: Path) -> None:
        with (
            patch(
                "sova.config.registry.list_projects",
                return_value={"test": str(project_dir)},
            ),
            patch("sova.config.loader.load_config") as mock_cfg,
        ):
            cfg = MagicMock()
            cfg.integration = IntegrationConfig(merge_queue_enabled="auto")
            cfg.github_repo = ""
            cfg.github_user = "testuser"
            mock_cfg.return_value = cfg

            monitors = create_monitors_for_merge_queue()

        assert len(monitors) == 0


# ---------------------------------------------------------------------------
# MergeQueueMonitor._transition_issue_state
# ---------------------------------------------------------------------------


class TestTransitionIssueState:
    @pytest.mark.asyncio
    async def test_calls_handle_post_merge_state(self, project_dir: Path) -> None:
        monitor = _make_monitor(project_dir)

        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.adapters.create_adapter") as mock_adapter_factory,
            patch("sova.git.merge.handle_post_merge_state", new_callable=AsyncMock) as mock_handle,
        ):
            cfg = MagicMock()
            cfg.task_source.type = "github"
            cfg.task_source.github_project_number = None
            mock_cfg.return_value = cfg
            mock_adapter = MagicMock()
            mock_adapter_factory.return_value = mock_adapter

            await monitor._transition_issue_state("10", "owner/repo", "user1")

        mock_handle.assert_called_once_with(
            "10",
            post_merge_state=monitor.integration_config.post_merge_state,
            repo="owner/repo",
            github_user="user1",
            adapter=mock_adapter,
        )


# ---------------------------------------------------------------------------
# MergeQueueMonitor._check_pr_merged_directly
# ---------------------------------------------------------------------------


class TestCheckPrMergedDirectly:
    @pytest.mark.asyncio
    async def test_returns_true_when_merged(self, project_dir: Path) -> None:
        monitor = _make_monitor(project_dir)

        mock_status = MagicMock()
        mock_status.state = "MERGED"

        with patch("sova.git.pr.get_pr_status", new_callable=AsyncMock, return_value=mock_status):
            result = await monitor._check_pr_merged_directly(42, "owner/repo", "user1")

        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_open(self, project_dir: Path) -> None:
        monitor = _make_monitor(project_dir)

        mock_status = MagicMock()
        mock_status.state = "OPEN"

        with patch("sova.git.pr.get_pr_status", new_callable=AsyncMock, return_value=mock_status):
            result = await monitor._check_pr_merged_directly(42, "owner/repo", "user1")

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_error(self, project_dir: Path) -> None:
        monitor = _make_monitor(project_dir)

        with patch("sova.git.pr.get_pr_status", new_callable=AsyncMock, side_effect=RuntimeError("API error")):
            result = await monitor._check_pr_merged_directly(42, "owner/repo", "user1")

        assert result is False


# ---------------------------------------------------------------------------
# Recovery helpers
# ---------------------------------------------------------------------------


class TestRecoveryHelpers:
    def test_get_recovery_config_returns_repo_and_user(self, project_dir: Path) -> None:
        from sova.dashboard.services.agent_recovery import _get_recovery_config

        with patch("sova.config.loader.load_config") as mock_cfg:
            cfg = MagicMock()
            cfg.github_repo = "owner/repo"
            cfg.github_user = "testuser"
            mock_cfg.return_value = cfg

            repo, user = _get_recovery_config(project_dir)

        assert repo == "owner/repo"
        assert user == "testuser"

    def test_get_recovery_config_returns_empty_on_error(self) -> None:
        from sova.dashboard.services.agent_recovery import _get_recovery_config

        with patch("sova.config.loader.load_config", side_effect=RuntimeError("no config")):
            repo, user = _get_recovery_config(None)

        assert repo == ""
        assert user == ""

    @pytest.mark.asyncio
    async def test_get_pr_branch_returns_branch_name(self, project_dir: Path) -> None:
        from sova.dashboard.services.agent_recovery import _get_pr_branch_for_recovery

        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value={}),
            patch(
                "sova.utils.shell.run",
                new_callable=AsyncMock,
                return_value=MagicMock(success=True, stdout="feat/my-branch\n"),
            ),
        ):
            cfg = MagicMock()
            cfg.github_repo = "owner/repo"
            cfg.github_user = "testuser"
            mock_cfg.return_value = cfg

            result = await _get_pr_branch_for_recovery(42, "owner/repo", project_dir)

        assert result == "feat/my-branch"

    @pytest.mark.asyncio
    async def test_get_pr_branch_returns_empty_when_no_repo(self) -> None:
        from sova.dashboard.services.agent_recovery import _get_pr_branch_for_recovery

        result = await _get_pr_branch_for_recovery(42, "", None)
        assert result == ""

    @pytest.mark.asyncio
    async def test_get_pr_branch_returns_empty_on_error(self, project_dir: Path) -> None:
        from sova.dashboard.services.agent_recovery import _get_pr_branch_for_recovery

        with (
            patch("sova.config.loader.load_config", side_effect=RuntimeError("no config")),
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, side_effect=RuntimeError("err")),
        ):
            result = await _get_pr_branch_for_recovery(42, "owner/repo", project_dir)

        assert result == ""

    @pytest.mark.asyncio
    async def test_get_pr_branch_returns_empty_on_failed_command(self, project_dir: Path) -> None:
        from sova.dashboard.services.agent_recovery import _get_pr_branch_for_recovery

        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value={}),
            patch(
                "sova.utils.shell.run",
                new_callable=AsyncMock,
                return_value=MagicMock(success=False, stdout=""),
            ),
        ):
            cfg = MagicMock()
            cfg.github_repo = "owner/repo"
            cfg.github_user = "testuser"
            mock_cfg.return_value = cfg

            result = await _get_pr_branch_for_recovery(42, "owner/repo", project_dir)

        assert result == ""


# ---------------------------------------------------------------------------
# MergeQueueMonitor._check_entry edge cases
# ---------------------------------------------------------------------------


class TestCheckEntryEdgeCases:
    @pytest.mark.asyncio
    async def test_uses_entry_github_user_over_monitor_default(self, project_dir: Path) -> None:
        monitor = _make_monitor(project_dir)
        entry = _make_entry(github_user="entry-user")

        queued_status = MergeQueueStatus(in_queue=True, state="QUEUED", position=1, estimated_time="2m")
        with (
            patch(
                "sova.git.merge.get_merge_queue_status",
                new_callable=AsyncMock,
                return_value=queued_status,
            ) as mock_status,
            patch.object(monitor, "_handle_merged", new_callable=AsyncMock),
            patch.object(monitor, "_handle_ejected", new_callable=AsyncMock),
            patch.object(monitor, "_handle_timeout", new_callable=AsyncMock),
        ):
            await monitor._check_entry(entry)

        call_kwargs = mock_status.call_args
        assert call_kwargs[1]["github_user"] == "entry-user"

    @pytest.mark.asyncio
    async def test_falls_back_to_monitor_github_user(self, project_dir: Path) -> None:
        monitor = _make_monitor(project_dir)
        entry = _make_entry(github_user="")

        queued_status = MergeQueueStatus(in_queue=True, state="QUEUED", position=1, estimated_time="2m")
        with (
            patch(
                "sova.git.merge.get_merge_queue_status",
                new_callable=AsyncMock,
                return_value=queued_status,
            ) as mock_status,
            patch.object(monitor, "_handle_merged", new_callable=AsyncMock),
            patch.object(monitor, "_handle_ejected", new_callable=AsyncMock),
            patch.object(monitor, "_handle_timeout", new_callable=AsyncMock),
        ):
            await monitor._check_entry(entry)

        call_kwargs = mock_status.call_args
        assert call_kwargs[1]["github_user"] == "testuser"

    @pytest.mark.asyncio
    async def test_naive_enqueued_at_gets_utc_timezone(self, project_dir: Path) -> None:
        monitor = _make_monitor(
            project_dir,
            integration_config=IntegrationConfig(merge_queue_timeout=3600, merge_queue_poll_interval=1),
        )
        naive_dt = datetime(2026, 1, 1, 12, 0, 0)
        entry = _make_entry(enqueued_at=naive_dt)

        queued_status = MergeQueueStatus(in_queue=True, state="QUEUED", position=1, estimated_time="2m")
        with (
            patch("sova.git.merge.get_merge_queue_status", new_callable=AsyncMock, return_value=queued_status),
            patch.object(monitor, "_handle_merged", new_callable=AsyncMock),
            patch.object(monitor, "_handle_ejected", new_callable=AsyncMock),
            patch.object(monitor, "_handle_timeout", new_callable=AsyncMock),
        ):
            await monitor._check_entry(entry)


# ---------------------------------------------------------------------------
# _load_queued_entries / _update_entry_status edge cases
# ---------------------------------------------------------------------------


class TestEntryEdgeCases:
    @pytest.mark.asyncio
    async def test_load_entries_filters_by_project_dir(self, project_dir: Path) -> None:
        other_dir = project_dir / "other"
        other_dir.mkdir()

        await create_merge_queue_entry(pr_number=42, repo="owner/repo", project_dir=project_dir)
        await create_merge_queue_entry(pr_number=43, repo="owner/repo", project_dir=other_dir)

        entries = await _load_queued_entries(project_dir)
        assert len(entries) == 1
        assert entries[0]["pr_number"] == 42

    @pytest.mark.asyncio
    async def test_update_sets_resolved_at(self, project_dir: Path) -> None:
        entry_id = await create_merge_queue_entry(
            pr_number=42, repo="owner/repo", project_dir=project_dir
        )
        assert entry_id is not None

        await _update_entry_status(entry_id, "ejected", project_dir)

        async with await get_session(project_dir=project_dir) as session:
            from sqlalchemy import select

            result = await session.execute(select(MergeQueueEntry).where(MergeQueueEntry.id == entry_id))
            e = result.scalars().first()
            assert e is not None
            assert e.status == "ejected"
            assert e.resolved_at is not None


# ---------------------------------------------------------------------------
# Cleanup error handling
# ---------------------------------------------------------------------------


class TestCleanupErrorHandling:
    @pytest.mark.asyncio
    async def test_worktree_cleanup_handles_exception(self, project_dir: Path) -> None:
        worktree_path = project_dir / ".claude" / "worktrees" / "10"
        worktree_path.mkdir(parents=True)

        monitor = _make_monitor(project_dir)
        entry = {"issue_number": "10"}

        with patch(
            "sova.git.worktree.cleanup_worktree",
            new_callable=AsyncMock,
            side_effect=RuntimeError("cleanup failed"),
        ):
            await monitor._cleanup_local_worktree(entry)

    @pytest.mark.asyncio
    async def test_branch_delete_failure_does_not_block_merge(self, project_dir: Path) -> None:
        entry_id = await create_merge_queue_entry(
            pr_number=42,
            repo="owner/repo",
            project_dir=project_dir,
            issue_number="10",
            branch_name="feat/test",
            github_user="user1",
        )
        assert entry_id is not None

        entries = await _load_queued_entries(project_dir)
        entry = entries[0]

        monitor = _make_monitor(project_dir)

        with (
            patch(
                "sova.git.merge.delete_remote_branch",
                new_callable=AsyncMock,
                side_effect=RuntimeError("branch delete failed"),
            ),
            patch.object(monitor, "_transition_issue_state", new_callable=AsyncMock) as mock_transition,
            patch.object(monitor, "_cleanup_local_worktree", new_callable=AsyncMock),
            patch("sova.dashboard.services.feed_service.emit_safe"),
            patch.object(monitor, "_notify"),
        ):
            await monitor._handle_merged(entry)

        mock_transition.assert_called_once()

        resolved_entries = await _load_queued_entries(project_dir)
        assert len(resolved_entries) == 0

    @pytest.mark.asyncio
    async def test_issue_transition_failure_does_not_block_merge(self, project_dir: Path) -> None:
        entry_id = await create_merge_queue_entry(
            pr_number=42,
            repo="owner/repo",
            project_dir=project_dir,
            issue_number="10",
            branch_name="feat/test",
            github_user="user1",
        )
        assert entry_id is not None

        entries = await _load_queued_entries(project_dir)
        entry = entries[0]

        monitor = _make_monitor(project_dir)

        with (
            patch("sova.git.merge.delete_remote_branch", new_callable=AsyncMock) as mock_delete,
            patch.object(
                monitor,
                "_transition_issue_state",
                new_callable=AsyncMock,
                side_effect=RuntimeError("transition failed"),
            ),
            patch.object(monitor, "_cleanup_local_worktree", new_callable=AsyncMock),
            patch("sova.dashboard.services.feed_service.emit_safe"),
            patch.object(monitor, "_notify"),
        ):
            await monitor._handle_merged(entry)

        mock_delete.assert_called_once()

        resolved_entries = await _load_queued_entries(project_dir)
        assert len(resolved_entries) == 0
