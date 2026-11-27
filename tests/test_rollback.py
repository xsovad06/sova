"""Tests for issue state rollback on agent failure."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sova.adapters.base import TaskState
from sova.db.models import TaskRun
from sova.db.session import get_session

# Uses the shared per-worker in-memory DB, emptied before each test
# (see ``tests/conftest.py:reset_db_rows``).
pytestmark = pytest.mark.usefixtures("reset_db_rows")


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with await get_session() as s:
        yield s


@pytest.fixture
async def mock_adapter():
    """Mock adapter with transition_state, and get_task reporting the in-progress state a development run sets."""
    adapter = MagicMock()
    adapter.transition_state = AsyncMock()
    adapter.get_task = AsyncMock(return_value=MagicMock(state=TaskState.IN_PROGRESS))
    return adapter


@pytest.fixture
async def mock_config(tmp_path):
    """Mock config loader."""
    config = MagicMock()
    config.github_repo = "user/repo"
    config.github_user = "testuser"
    return config


class TestRollbackIssueState:
    """Tests for rollback_issue_state helper."""

    async def test_rollback_developer_to_researched(self, session, mock_adapter, mock_config, tmp_path):
        """Developer role without PR restores the state the run moved the issue out of (researched here)."""
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        task_run = TaskRun(
            issue_number="42",
            role="developer",
            status="failed",
            pr_number=None,
            assessment_json={"prior_state": "researched"},
            started_at=datetime.now(timezone.utc),
        )
        session.add(task_run)
        await session.commit()

        with patch("sova.dashboard.services.agent_recovery.load_config", return_value=mock_config):
            with patch("sova.adapters.create_adapter", return_value=mock_adapter):
                moved = await rollback_issue_state(task_run.id, tmp_path)

        mock_adapter.transition_state.assert_awaited_once_with("42", TaskState.RESEARCHED)
        assert moved is True

    @pytest.mark.parametrize("state", [TaskState.TRIAGED, TaskState.BACKLOG, TaskState.RESEARCHED])
    async def test_rollback_developer_leaves_a_state_it_is_no_longer_in(
        self, session, mock_adapter, mock_config, tmp_path, state
    ):
        """The tracker no longer shows in progress (something moved the issue since): nothing to undo."""
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        mock_adapter.get_task.return_value = MagicMock(state=state)
        task_run = TaskRun(
            issue_number="42",
            role="developer",
            status="failed",
            pr_number=None,
            assessment_json={"prior_state": "researched"},
            started_at=datetime.now(timezone.utc),
        )
        session.add(task_run)
        await session.commit()

        with patch("sova.dashboard.services.agent_recovery.load_config", return_value=mock_config):
            with patch("sova.adapters.create_adapter", return_value=mock_adapter):
                moved = await rollback_issue_state(task_run.id, tmp_path)

        mock_adapter.transition_state.assert_not_awaited()
        assert moved is False

    @pytest.mark.parametrize(
        ("assessment", "why"),
        [
            ({"prior_state": "in_progress"}, "a recorded in-progress prior state is not one to restore"),
            ({"prior_state": "no-such-state"}, "an unrecognized prior state is not guessed at"),
        ],
    )
    async def test_rollback_developer_leaves_an_issue_the_run_never_moved(
        self, session, mock_adapter, mock_config, tmp_path, assessment, why
    ):
        """A Resume on an issue already in progress is a failed run whose tracker state reads in progress too, but
        this run never set it: its failure must not move the issue back to researched."""
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        task_run = TaskRun(
            issue_number="42",
            role="developer",
            status="failed",
            pr_number=None,
            assessment_json=assessment,
            started_at=datetime.now(timezone.utc),
        )
        session.add(task_run)
        await session.commit()

        with patch("sova.dashboard.services.agent_recovery.load_config", return_value=mock_config):
            with patch("sova.adapters.create_adapter", return_value=mock_adapter):
                await rollback_issue_state(task_run.id, tmp_path)

        mock_adapter.transition_state.assert_not_awaited()

    @pytest.mark.parametrize(
        ("tracker_state", "surfaced"),
        [(TaskState.IN_PROGRESS, True), (TaskState.RESEARCHED, False)],
    )
    async def test_a_developer_run_with_no_recorded_state_leaves_the_issue_and_surfaces_a_stranded_one(
        self, session, mock_adapter, mock_config, tmp_path, tracker_state, surfaced
    ):
        """A run launched before the record existed (or whose state was unreadable at launch) is never guessed at:
        the issue is left alone, and when it reads in progress that is said in the feed, not only in a log."""
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        mock_adapter.get_task.return_value = MagicMock(state=tracker_state)
        task_run = TaskRun(
            issue_number="42",
            role="developer",
            status="failed",
            pr_number=None,
            assessment_json=None,
            started_at=datetime.now(timezone.utc),
        )
        session.add(task_run)
        await session.commit()

        with patch("sova.dashboard.services.agent_recovery.load_config", return_value=mock_config):
            with patch("sova.adapters.create_adapter", return_value=mock_adapter):
                with patch("sova.dashboard.services.feed_service.emit_safe") as emit:
                    await rollback_issue_state(task_run.id, tmp_path)

        mock_adapter.transition_state.assert_not_awaited()
        assert emit.called is surfaced

    async def test_rollback_restores_a_force_started_runs_triaged_prior_state(
        self, session, mock_adapter, mock_config, tmp_path
    ):
        """A force-started developer on a triaged issue moved it to in progress; its failure puts it back to
        triaged, not to researched (the #1418 promotion)."""
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        task_run = TaskRun(
            issue_number="42",
            role="developer",
            status="failed",
            pr_number=None,
            assessment_json={"prior_state": "triaged"},
            started_at=datetime.now(timezone.utc),
        )
        session.add(task_run)
        await session.commit()

        with patch("sova.dashboard.services.agent_recovery.load_config", return_value=mock_config):
            with patch("sova.adapters.create_adapter", return_value=mock_adapter):
                await rollback_issue_state(task_run.id, tmp_path)

        mock_adapter.transition_state.assert_awaited_once_with("42", TaskState.TRIAGED)

    async def test_an_unreadable_state_reads_as_unknown_not_as_not_in_progress(self, mock_adapter) -> None:
        """A failed read must not look like an answer: the helper reports None, never False."""
        from sova.dashboard.services.agent_recovery import _issue_in_progress

        mock_adapter.get_task.side_effect = RuntimeError("Failed to fetch issue #42")
        assert await _issue_in_progress(mock_adapter, "42") is None
        mock_adapter.get_task.side_effect = None
        mock_adapter.get_task.return_value = MagicMock(state=TaskState.TRIAGED)
        assert await _issue_in_progress(mock_adapter, "42") is False
        mock_adapter.get_task.return_value = MagicMock(state=TaskState.IN_PROGRESS)
        assert await _issue_in_progress(mock_adapter, "42") is True

    async def test_rollback_developer_unreadable_state_is_retried_once_then_surfaced(
        self, session, mock_adapter, mock_config, tmp_path, monkeypatch
    ):
        """A tracker read failure is not "in progress": the read is retried once, then the rollback changes nothing
        and the stranded issue is surfaced as a feed event rather than only a log line."""
        from sova.dashboard.services import agent_recovery
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        monkeypatch.setattr(agent_recovery, "_STATE_READ_RETRY_DELAY_S", 0)
        mock_adapter.get_task.side_effect = RuntimeError("Failed to fetch issue #42")
        task_run = TaskRun(
            issue_number="42",
            role="developer",
            status="failed",
            pr_number=None,
            assessment_json={"prior_state": "researched"},
            started_at=datetime.now(timezone.utc),
        )
        session.add(task_run)
        await session.commit()

        with patch("sova.dashboard.services.agent_recovery.load_config", return_value=mock_config):
            with patch("sova.adapters.create_adapter", return_value=mock_adapter):
                with patch("sova.dashboard.services.feed_service.emit_safe") as emit:
                    await rollback_issue_state(task_run.id, tmp_path)

        mock_adapter.transition_state.assert_not_awaited()
        assert mock_adapter.get_task.await_count == 2, "read once, then once more"
        emit.assert_called_once()
        assert "#42" in emit.call_args.args[0]

    async def test_rollback_developer_recovers_when_the_retry_reads(
        self, session, mock_adapter, mock_config, tmp_path, monkeypatch
    ):
        from sova.dashboard.services import agent_recovery
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        monkeypatch.setattr(agent_recovery, "_STATE_READ_RETRY_DELAY_S", 0)
        mock_adapter.get_task.side_effect = [RuntimeError("blip"), MagicMock(state=TaskState.IN_PROGRESS)]
        task_run = TaskRun(
            issue_number="42",
            role="developer",
            status="failed",
            pr_number=None,
            assessment_json={"prior_state": "researched"},
            started_at=datetime.now(timezone.utc),
        )
        session.add(task_run)
        await session.commit()

        with patch("sova.dashboard.services.agent_recovery.load_config", return_value=mock_config):
            with patch("sova.adapters.create_adapter", return_value=mock_adapter):
                await rollback_issue_state(task_run.id, tmp_path)

        mock_adapter.transition_state.assert_awaited_once_with("42", TaskState.RESEARCHED)

    async def test_rollback_developer_with_pr_to_in_review(self, session, mock_adapter, mock_config, tmp_path):
        """Developer role with PR rolls back to in_review."""
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        task_run = TaskRun(
            issue_number="42",
            role="developer",
            status="failed",
            pr_number=123,
            started_at=datetime.now(timezone.utc),
        )
        session.add(task_run)
        await session.commit()

        with patch("sova.dashboard.services.agent_recovery.load_config", return_value=mock_config):
            with patch("sova.adapters.create_adapter", return_value=mock_adapter):
                await rollback_issue_state(task_run.id, tmp_path)

        mock_adapter.transition_state.assert_awaited_once_with("42", TaskState.IN_REVIEW)

    async def test_rollback_researcher_to_triaged(self, session, mock_adapter, mock_config, tmp_path):
        """Researcher role rolls back to triaged."""
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        task_run = TaskRun(
            issue_number="42",
            role="researcher",
            status="failed",
            started_at=datetime.now(timezone.utc),
        )
        session.add(task_run)
        await session.commit()

        with patch("sova.dashboard.services.agent_recovery.load_config", return_value=mock_config):
            with patch("sova.adapters.create_adapter", return_value=mock_adapter):
                await rollback_issue_state(task_run.id, tmp_path)

        mock_adapter.transition_state.assert_awaited_once_with("42", TaskState.TRIAGED)

    async def test_rollback_reviewer_to_in_review(self, session, mock_adapter, mock_config, tmp_path):
        """Reviewer role rolls back to in_review."""
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        task_run = TaskRun(
            issue_number="42",
            role="reviewer",
            status="failed",
            pr_number=123,
            started_at=datetime.now(timezone.utc),
        )
        session.add(task_run)
        await session.commit()

        with patch("sova.dashboard.services.agent_recovery.load_config", return_value=mock_config):
            with patch("sova.adapters.create_adapter", return_value=mock_adapter):
                await rollback_issue_state(task_run.id, tmp_path)

        mock_adapter.transition_state.assert_awaited_once_with("42", TaskState.IN_REVIEW)

    async def test_rollback_triage_removes_all_agent_labels(self, session, mock_adapter, mock_config, tmp_path):
        """Triage role removes all agent: labels instead of transitioning."""
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        task_run = TaskRun(
            issue_number="42",
            role="triage",
            status="failed",
            started_at=datetime.now(timezone.utc),
        )
        session.add(task_run)
        await session.commit()

        mock_adapter.remove_label = AsyncMock()
        mock_adapter.get_task = AsyncMock(
            return_value=MagicMock(labels=["agent:triaged", "type:feature", "agent:in-progress"])
        )

        with patch("sova.dashboard.services.agent_recovery.load_config", return_value=mock_config):
            with patch("sova.adapters.create_adapter", return_value=mock_adapter):
                await rollback_issue_state(task_run.id, tmp_path)

        assert mock_adapter.transition_state.call_count == 0
        assert mock_adapter.remove_label.call_count == 2
        mock_adapter.remove_label.assert_any_call("42", "agent:triaged")
        mock_adapter.remove_label.assert_any_call("42", "agent:in-progress")

    @pytest.mark.parametrize(
        ("labels", "remove_effect", "moved"),
        [
            pytest.param(["type:feature"], None, False, id="nothing-to-remove"),
            pytest.param(["agent:triaged", "agent:ready"], None, True, id="all-removed"),
            pytest.param(["agent:triaged", "agent:ready"], [None, RuntimeError("HTTP 502")], False, id="partial"),
            pytest.param(["agent:triaged"], [RuntimeError("HTTP 502")], False, id="none-removed"),
        ],
    )
    async def test_rollback_triage_reports_whether_it_moved_the_issue(
        self,
        session: AsyncSession,
        mock_adapter: MagicMock,
        mock_config: MagicMock,
        tmp_path: Path,
        labels: list[str],
        remove_effect: list | None,
        moved: bool,
    ) -> None:
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        task_run = TaskRun(issue_number="42", role="triage", status="failed", started_at=datetime.now(timezone.utc))
        session.add(task_run)
        await session.commit()
        mock_adapter.remove_label = AsyncMock(side_effect=remove_effect)
        mock_adapter.get_task = AsyncMock(return_value=MagicMock(labels=labels))

        with patch("sova.dashboard.services.agent_recovery.load_config", return_value=mock_config):
            with patch("sova.adapters.create_adapter", return_value=mock_adapter):
                assert await rollback_issue_state(task_run.id, tmp_path) is moved

    async def test_rollback_triage_keeps_the_on_hold_label(
        self, session: AsyncSession, mock_adapter: MagicMock, mock_config: MagicMock, tmp_path: Path
    ) -> None:
        """A hold comes from a lead or /find-task, never triage, so a failed triage run must not lift it."""
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        task_run = TaskRun(
            issue_number="42",
            role="triage",
            status="failed",
            started_at=datetime.now(timezone.utc),
        )
        session.add(task_run)
        await session.commit()

        mock_adapter.remove_label = AsyncMock()
        mock_adapter.get_task = AsyncMock(return_value=MagicMock(labels=["agent:ready", "agent:on-hold"]))

        with patch("sova.dashboard.services.agent_recovery.load_config", return_value=mock_config):
            with patch("sova.adapters.create_adapter", return_value=mock_adapter):
                await rollback_issue_state(task_run.id, tmp_path)

        mock_adapter.remove_label.assert_awaited_once_with("42", "agent:ready")

    async def test_rollback_command_integrate_pr(self, session, mock_adapter, mock_config, tmp_path):
        """Command integrate-pr rolls back to in_review."""
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        task_run = TaskRun(
            issue_number="42",
            role="command:integrate-pr",
            status="failed",
            pr_number=123,
            started_at=datetime.now(timezone.utc),
        )
        session.add(task_run)
        await session.commit()

        with patch("sova.dashboard.services.agent_recovery.load_config", return_value=mock_config):
            with patch("sova.adapters.create_adapter", return_value=mock_adapter):
                await rollback_issue_state(task_run.id, tmp_path)

        mock_adapter.transition_state.assert_awaited_once_with("42", TaskState.IN_REVIEW)

    async def test_rollback_command_address_pr(self, session, mock_adapter, mock_config, tmp_path):
        """Command address-pr rolls back to in_review."""
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        task_run = TaskRun(
            issue_number="42",
            role="command:address-pr",
            status="failed",
            pr_number=123,
            started_at=datetime.now(timezone.utc),
        )
        session.add(task_run)
        await session.commit()

        with patch("sova.dashboard.services.agent_recovery.load_config", return_value=mock_config):
            with patch("sova.adapters.create_adapter", return_value=mock_adapter):
                await rollback_issue_state(task_run.id, tmp_path)

        mock_adapter.transition_state.assert_awaited_once_with("42", TaskState.IN_REVIEW)

    async def test_rollback_skips_when_concurrent_run_exists(self, session, mock_adapter, mock_config, tmp_path):
        """Rollback skips when another non-terminal run exists for the same issue."""
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        task_run1 = TaskRun(
            issue_number="42",
            role="developer",
            status="failed",
            started_at=datetime.now(timezone.utc),
        )
        task_run2 = TaskRun(
            issue_number="42",
            role="developer",
            status="running",
            started_at=datetime.now(timezone.utc),
        )
        session.add_all([task_run1, task_run2])
        await session.commit()

        with patch("sova.dashboard.services.agent_recovery.load_config", return_value=mock_config):
            with patch("sova.adapters.create_adapter", return_value=mock_adapter):
                await rollback_issue_state(task_run1.id, tmp_path)

        mock_adapter.transition_state.assert_not_called()

    async def test_rollback_skips_when_issue_number_missing(self, session, mock_adapter, mock_config, tmp_path):
        """Rollback logs and skips when issue_number is None."""
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        task_run = TaskRun(
            issue_number=None,
            role="developer",
            status="failed",
            started_at=datetime.now(timezone.utc),
        )
        session.add(task_run)
        await session.commit()

        with patch("sova.dashboard.services.agent_recovery.load_config", return_value=mock_config):
            with patch("sova.adapters.create_adapter", return_value=mock_adapter):
                await rollback_issue_state(task_run.id, tmp_path)

        mock_adapter.transition_state.assert_not_called()

    async def test_rollback_is_non_fatal_on_api_error(self, session, mock_adapter, mock_config, tmp_path):
        """Rollback logs and continues on GitHub API error."""
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        task_run = TaskRun(
            issue_number="42",
            role="developer",
            status="failed",
            assessment_json={"prior_state": "researched"},
            started_at=datetime.now(timezone.utc),
        )
        session.add(task_run)
        await session.commit()

        mock_adapter.transition_state.side_effect = Exception("API rate limit exceeded")

        with patch("sova.dashboard.services.agent_recovery.load_config", return_value=mock_config):
            with patch("sova.adapters.create_adapter", return_value=mock_adapter):
                with patch("sova.dashboard.services.agent_recovery.log") as mock_log:
                    await rollback_issue_state(task_run.id, tmp_path)

        mock_adapter.transition_state.assert_awaited_once()
        mock_log.warning.assert_called_once_with("rollback.failed", run_id=task_run.id, exc_info=True)


class TestTransitionToInProgress:
    """`_transition_to_in_progress` only moves the label: the state to restore is recorded with the run at launch."""

    async def _run(self, mock_adapter, mock_config, tmp_path) -> None:
        from sova.dashboard.services.agent_validation import _transition_to_in_progress

        with patch("sova.dashboard.services.agent_validation.load_config", return_value=mock_config):
            with patch("sova.adapters.create_adapter", return_value=mock_adapter):
                await _transition_to_in_progress("42", tmp_path)

    async def test_the_issue_is_moved_to_in_progress_without_reading_its_state(
        self, mock_adapter, mock_config, tmp_path
    ):
        mock_adapter.get_state = AsyncMock(side_effect=AssertionError("the state is recorded at launch"))

        await self._run(mock_adapter, mock_config, tmp_path)

        mock_adapter.transition_state.assert_awaited_once_with("42", TaskState.IN_PROGRESS)
        mock_adapter.get_state.assert_not_awaited()

    async def test_a_tracker_failure_is_swallowed(self, mock_adapter, mock_config, tmp_path):
        mock_adapter.transition_state = AsyncMock(side_effect=RuntimeError("tracker down"))

        await self._run(mock_adapter, mock_config, tmp_path)

        mock_adapter.transition_state.assert_awaited_once_with("42", TaskState.IN_PROGRESS)
