"""Tests for issue state rollback on agent failure."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sova.adapters.base import TaskState
from sova.db.models import TaskRun
from sova.db.session import close_db, get_session, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    """Initialize an in-memory DB for rollback tests."""
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


@pytest.fixture
async def session() -> AsyncSession:
    return await get_session()


@pytest.fixture
async def mock_adapter():
    """Mock adapter with transition_state method."""
    adapter = MagicMock()
    adapter.transition_state = AsyncMock()
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
        """Developer role without PR rolls back to researched."""
        from sova.dashboard.services.agent_recovery import rollback_issue_state

        task_run = TaskRun(
            issue_number="42",
            role="developer",
            status="failed",
            pr_number=None,
            started_at=datetime.now(timezone.utc),
        )
        session.add(task_run)
        await session.commit()

        with patch("sova.config.loader.load_config", return_value=mock_config):
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

        with patch("sova.config.loader.load_config", return_value=mock_config):
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

        with patch("sova.config.loader.load_config", return_value=mock_config):
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

        with patch("sova.config.loader.load_config", return_value=mock_config):
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

        with patch("sova.config.loader.load_config", return_value=mock_config):
            with patch("sova.adapters.create_adapter", return_value=mock_adapter):
                await rollback_issue_state(task_run.id, tmp_path)

        assert mock_adapter.transition_state.call_count == 0
        assert mock_adapter.remove_label.call_count == 2
        mock_adapter.remove_label.assert_any_call("42", "agent:triaged")
        mock_adapter.remove_label.assert_any_call("42", "agent:in-progress")

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

        with patch("sova.config.loader.load_config", return_value=mock_config):
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

        with patch("sova.config.loader.load_config", return_value=mock_config):
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

        with patch("sova.config.loader.load_config", return_value=mock_config):
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

        with patch("sova.config.loader.load_config", return_value=mock_config):
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
            started_at=datetime.now(timezone.utc),
        )
        session.add(task_run)
        await session.commit()

        mock_adapter.transition_state.side_effect = Exception("API rate limit exceeded")

        with patch("sova.config.loader.load_config", return_value=mock_config):
            with patch("sova.adapters.create_adapter", return_value=mock_adapter):
                with patch("sova.dashboard.services.agent_recovery.log") as mock_log:
                    await rollback_issue_state(task_run.id, tmp_path)

        mock_adapter.transition_state.assert_awaited_once()
        mock_log.warning.assert_called_once_with("rollback.failed", run_id=task_run.id, exc_info=True)
