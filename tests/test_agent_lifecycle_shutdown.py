"""agent_lifecycle shutdown, background-task cancellation and crash recovery.

Split from tests/test_dashboard.py; see docs/testing-guidelines.md, "Test File Layout".
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

# Uses the shared per-worker in-memory DB, emptied before each test
# (see ``tests/conftest.py:reset_db_rows``). A test here that leaves a
# background task touching the DB when its event loop closes (the real
# ``start_agent()`` calls in ``TestDuplicateAgentPrevention``) can invalidate the
# shared connection; the next test's sweep recreates the schema.
pytestmark = pytest.mark.usefixtures("reset_db_rows")


# ---------------------------------------------------------------------------
# Control Service: recovery and normalization
# ---------------------------------------------------------------------------


class TestCancelBackgroundTasks:
    async def test_cancels_pending_tasks(self) -> None:
        """cancel_background_tasks should cancel all tasks in _background_tasks."""
        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.agent_lifecycle import cancel_background_tasks

        finished = False

        async def _long_running() -> None:
            nonlocal finished
            await asyncio.sleep(3600)
            finished = True

        task = asyncio.create_task(_long_running())
        agent_lifecycle._background_tasks.add(task)
        try:
            await cancel_background_tasks()
            assert task.cancelled()
            assert not finished
            assert len(agent_lifecycle._background_tasks) == 0
        finally:
            agent_lifecycle._background_tasks.discard(task)

    async def test_idempotent_when_empty(self) -> None:
        """cancel_background_tasks should be safe to call with no tasks."""
        from sova.dashboard.services.agent_lifecycle import (
            _background_tasks,
            cancel_background_tasks,
        )

        _background_tasks.clear()
        await cancel_background_tasks()
        assert len(_background_tasks) == 0

    async def test_cancels_per_agent_io_tasks(self) -> None:
        """cancel_background_tasks must also cancel per-agent reader/stderr/resource tasks."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_lifecycle import cancel_background_tasks
        from sova.dashboard.services.agent_pool import AgentState, _get_project_agents

        pa = _get_project_agents()
        mock_process = MagicMock()
        mock_process.pid = 99999

        agent = AgentState(run_id=9999, issue="999", role="developer", process=mock_process)

        reader_done = False
        stderr_done = False

        async def _fake_reader() -> None:
            nonlocal reader_done
            await asyncio.sleep(3600)
            reader_done = True

        async def _fake_stderr() -> None:
            nonlocal stderr_done
            await asyncio.sleep(3600)
            stderr_done = True

        agent.reader_task = asyncio.create_task(_fake_reader())
        agent.stderr_task = asyncio.create_task(_fake_stderr())
        pa.agents[9999] = agent

        try:
            await cancel_background_tasks()
            assert agent.reader_task.cancelled()
            assert agent.stderr_task.cancelled()
            assert not reader_done
            assert not stderr_done
        finally:
            del pa.agents[9999]


class TestShutdownTasks:
    """Cover the _shutdown_tasks helper in app.py."""

    async def test_cancels_all_task_types(self) -> None:
        """_shutdown_tasks cancels sweep, pr_throttle, pr_monitor tasks and stops metrics_writer."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.app import _shutdown_tasks

        async def _hang() -> None:
            await asyncio.sleep(3600)

        sweep = asyncio.create_task(_hang())
        throttle = asyncio.create_task(_hang())
        monitor = asyncio.create_task(_hang())
        writer = AsyncMock()

        cancel_bg = "sova.dashboard.services.agent_lifecycle.cancel_background_tasks"
        cancel_batch = "sova.dashboard.services.batch_service.cancel_all_batches"
        with (
            patch(cancel_bg, new_callable=AsyncMock) as mock_bg,
            patch("sova.dashboard.routers.agents._ws_manager") as mock_ws,
            patch(cancel_batch, new_callable=AsyncMock) as mock_batch,
        ):
            mock_ws.cancel_all = AsyncMock()
            await _shutdown_tasks(sweep, [throttle], [monitor], writer)
            mock_bg.assert_awaited_once()
            mock_ws.cancel_all.assert_awaited_once()
            mock_batch.assert_awaited_once()

        assert sweep.cancelled()
        assert throttle.cancelled()
        assert monitor.cancelled()
        writer.stop.assert_awaited_once()

    async def test_cancels_the_dependency_graph_builds(self) -> None:
        """A graph build stuck in a tracker call must not outlive the lifespan shutdown."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.app import _shutdown_tasks
        from sova.dashboard.services import work_item_service

        async def _hang() -> None:
            await asyncio.sleep(3600)

        sweep = asyncio.create_task(_hang())
        build = asyncio.create_task(_hang())
        work_item_service._graph_builds["shutdown-test"] = build

        cancel_bg = "sova.dashboard.services.agent_lifecycle.cancel_background_tasks"
        cancel_batch = "sova.dashboard.services.batch_service.cancel_all_batches"
        try:
            with (
                patch(cancel_bg, new_callable=AsyncMock),
                patch("sova.dashboard.routers.agents._ws_manager") as mock_ws,
                patch(cancel_batch, new_callable=AsyncMock),
            ):
                mock_ws.cancel_all = AsyncMock()
                await _shutdown_tasks(sweep, [], [], None)
        finally:
            work_item_service._graph_builds.pop("shutdown-test", None)

        assert build.cancelled()

    async def test_handles_none_metrics_writer(self) -> None:
        """_shutdown_tasks works when metrics_writer is None."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.app import _shutdown_tasks

        async def _hang() -> None:
            await asyncio.sleep(3600)

        sweep = asyncio.create_task(_hang())

        cancel_bg = "sova.dashboard.services.agent_lifecycle.cancel_background_tasks"
        cancel_batch = "sova.dashboard.services.batch_service.cancel_all_batches"
        with (
            patch(cancel_bg, new_callable=AsyncMock) as mock_bg,
            patch("sova.dashboard.routers.agents._ws_manager") as mock_ws,
            patch(cancel_batch, new_callable=AsyncMock) as mock_batch,
        ):
            mock_ws.cancel_all = AsyncMock()
            await _shutdown_tasks(sweep, [], [], None)
            mock_bg.assert_awaited_once()
            mock_ws.cancel_all.assert_awaited_once()
            mock_batch.assert_awaited_once()

        assert sweep.cancelled()

    async def test_handles_empty_bg_task_lists(self) -> None:
        """_shutdown_tasks works with empty pr_throttle and pr_monitor lists."""
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.app import _shutdown_tasks

        async def _hang() -> None:
            await asyncio.sleep(3600)

        sweep = asyncio.create_task(_hang())

        cancel_bg = "sova.dashboard.services.agent_lifecycle.cancel_background_tasks"
        cancel_batch = "sova.dashboard.services.batch_service.cancel_all_batches"
        with (
            patch(cancel_bg, new_callable=AsyncMock) as mock_bg,
            patch("sova.dashboard.routers.agents._ws_manager") as mock_ws,
            patch(cancel_batch, new_callable=AsyncMock) as mock_batch,
        ):
            mock_ws.cancel_all = AsyncMock()
            await _shutdown_tasks(sweep, [], [], None)
            mock_bg.assert_awaited_once()
            mock_ws.cancel_all.assert_awaited_once()
            mock_batch.assert_awaited_once()

        assert sweep.cancelled()


# ---------------------------------------------------------------------------
# Periodic recovery loop: per-project exception isolation
# ---------------------------------------------------------------------------


class TestPeriodicRecoveryLoopIsolation:
    """A failure recovering one project must not skip the rest until the next tick."""

    async def test_one_project_failing_does_not_block_the_others(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.app import _periodic_recovery_loop

        project_a = tmp_path / "project-a"
        project_b = tmp_path / "project-b"
        recovered: list[Path] = []

        async def fake_recover(path: Path) -> None:
            if path == project_a:
                raise RuntimeError("project-a recovery exploded")
            recovered.append(path)

        with (
            patch("sova.dashboard.app._collect_sweep_dirs", return_value=[project_a, project_b]),
            patch("sova.dashboard.app.recover_stale_runs", side_effect=fake_recover),
            patch(
                "sova.dashboard.services.agent_recovery.attempt_network_self_heal",
                new_callable=AsyncMock,
                return_value=[],
            ),
            # One real iteration, then stop the infinite loop.
            patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError()]),
        ):
            with pytest.raises(asyncio.CancelledError):
                await _periodic_recovery_loop(None, is_multi=True)

        # project-b's recovery still ran despite project-a's exception.
        assert recovered == [project_b]


class TestCrashRecoveryCleanup:
    """Test _crash_recovery_cleanup: branch deletion and state transition after merge succeeds despite agent crash."""

    @staticmethod
    def _make_agent(issue: str = "42", pr_number: int = 100):
        from pathlib import Path
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_pool import AgentState
        from sova.ipc.control import FileAgentProcess

        mock_process = AsyncMock(spec=FileAgentProcess)
        return AgentState(
            run_id=1,
            project_dir=Path("/fake/project"),
            issue=issue,
            role="command:integrate-pr",
            process=mock_process,
            pr_number=pr_number,
            last_result_cost=0.0,
            output_writer=None,
        )

    async def test_deletes_branch_and_transitions_state(self) -> None:
        """Branch is deleted and the Jira ticket transitioned on successful merge."""
        from unittest.mock import AsyncMock, MagicMock, patch

        agent = self._make_agent()

        with (
            patch("sova.dashboard.services.agent_finalize.load_config") as mock_load_config,
            patch("sova.adapters.create_adapter"),
            patch(
                "sova.dashboard.services.agent_finalize.get_pr_branch",
                new_callable=AsyncMock,
                return_value="feat/issue-42",
            ) as mock_get_branch,
            patch(
                "sova.dashboard.services.agent_finalize.delete_remote_branch",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_delete_branch,
            patch(
                "sova.dashboard.services.agent_finalize.handle_post_merge_state",
                new_callable=AsyncMock,
            ) as mock_post_merge,
            patch(
                "sova.dashboard.services.agent_finalize._resolve_issue_from_pr",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            mock_cfg = MagicMock()
            mock_cfg.github_repo = "owner/repo"
            mock_cfg.github_user = "testuser"
            mock_cfg.integration.post_merge_state = "done"
            mock_cfg.task_source.type = "jira"
            mock_load_config.return_value = mock_cfg

            from sova.dashboard.services.agent_lifecycle import _crash_recovery_cleanup

            await _crash_recovery_cleanup(agent)

        mock_get_branch.assert_awaited_once_with(100, repo="owner/repo", github_user="testuser")
        mock_delete_branch.assert_awaited_once_with("feat/issue-42", repo="owner/repo", github_user="testuser")
        mock_post_merge.assert_awaited_once()
        args, kwargs = mock_post_merge.call_args
        assert args[0] == "42"
        assert kwargs["post_merge_state"] == "done"
        assert kwargs["repo"] == "owner/repo"
        assert kwargs["github_user"] == "testuser"

    async def test_github_lets_the_pr_body_decide_not_agent_issue(self) -> None:
        """A merge agent started for issue 42 must not close it when the PR is only "Part of #42" (#1340)."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.git.merge import MergedIssues

        agent = self._make_agent()

        with (
            patch("sova.dashboard.services.agent_finalize.load_config") as mock_load_config,
            patch("sova.adapters.create_adapter"),
            patch(
                "sova.dashboard.services.agent_finalize.get_pr_branch",
                new_callable=AsyncMock,
                return_value="feat/issue-42",
            ),
            patch(
                "sova.dashboard.services.agent_finalize.delete_remote_branch",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "sova.dashboard.services.agent_finalize.handle_merged_pr_issues",
                new_callable=AsyncMock,
                return_value=MergedIssues(part_of=["42"]),
            ) as mock_merged,
            patch(
                "sova.dashboard.services.agent_finalize.handle_post_merge_state",
                new_callable=AsyncMock,
            ) as mock_post_merge,
            patch(
                "sova.dashboard.services.agent_finalize._resolve_issue_from_pr",
                new_callable=AsyncMock,
            ) as mock_resolve,
            patch("sova.dashboard.services.agent_finalize.log") as mock_log,
        ):
            mock_cfg = MagicMock()
            mock_cfg.github_repo = "owner/repo"
            mock_cfg.github_user = "testuser"
            mock_cfg.integration.post_merge_state = "done"
            mock_cfg.task_source.type = "github"
            mock_load_config.return_value = mock_cfg

            from sova.dashboard.services.agent_lifecycle import _crash_recovery_cleanup

            await _crash_recovery_cleanup(agent)

        mock_merged.assert_awaited_once()
        args, kwargs = mock_merged.call_args
        assert args == (100,)
        assert kwargs["post_merge_state"] == "done"
        assert kwargs["repo"] == "owner/repo"
        assert kwargs["github_user"] == "testuser"
        mock_post_merge.assert_not_awaited()
        mock_resolve.assert_not_awaited()
        handled = [c for c in mock_log.info.call_args_list if c.args == ("finalize.crash_recovery_issues_handled",)]
        assert [c.kwargs["actions"] for c in handled] == [["issue #42 left open (Part of): unchanged"]]

    async def test_continues_when_branch_deletion_fails(self) -> None:
        """State transition still runs even when branch deletion returns False."""
        from unittest.mock import AsyncMock, MagicMock, patch

        agent = self._make_agent()

        with (
            patch("sova.dashboard.services.agent_finalize.load_config") as mock_load_config,
            patch("sova.adapters.create_adapter"),
            patch(
                "sova.dashboard.services.agent_finalize.get_pr_branch",
                new_callable=AsyncMock,
                return_value="feat/issue-42",
            ),
            patch(
                "sova.dashboard.services.agent_finalize.delete_remote_branch",
                new_callable=AsyncMock,
                return_value=False,
            ) as mock_delete_branch,
            patch(
                "sova.dashboard.services.agent_finalize.handle_post_merge_state",
                new_callable=AsyncMock,
            ) as mock_post_merge,
            patch(
                "sova.dashboard.services.agent_finalize._resolve_issue_from_pr",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            mock_cfg = MagicMock()
            mock_cfg.github_repo = "owner/repo"
            mock_cfg.github_user = "testuser"
            mock_cfg.integration.post_merge_state = "done"
            mock_cfg.task_source.type = "jira"
            mock_load_config.return_value = mock_cfg

            from sova.dashboard.services.agent_lifecycle import _crash_recovery_cleanup

            await _crash_recovery_cleanup(agent)

        mock_delete_branch.assert_awaited_once()
        mock_post_merge.assert_awaited_once()

    async def test_skips_deletion_when_branch_lookup_fails(self) -> None:
        """When get_pr_branch returns empty string, deletion is skipped but state transition runs."""
        from unittest.mock import AsyncMock, MagicMock, patch

        agent = self._make_agent()

        with (
            patch("sova.dashboard.services.agent_finalize.load_config") as mock_load_config,
            patch("sova.adapters.create_adapter"),
            patch(
                "sova.dashboard.services.agent_finalize.get_pr_branch",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch(
                "sova.dashboard.services.agent_finalize.delete_remote_branch",
                new_callable=AsyncMock,
            ) as mock_delete_branch,
            patch(
                "sova.dashboard.services.agent_finalize.handle_post_merge_state",
                new_callable=AsyncMock,
            ) as mock_post_merge,
            patch(
                "sova.dashboard.services.agent_finalize._resolve_issue_from_pr",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            mock_cfg = MagicMock()
            mock_cfg.github_repo = "owner/repo"
            mock_cfg.github_user = "testuser"
            mock_cfg.integration.post_merge_state = "done"
            mock_cfg.task_source.type = "jira"
            mock_load_config.return_value = mock_cfg

            from sova.dashboard.services.agent_lifecycle import _crash_recovery_cleanup

            await _crash_recovery_cleanup(agent)

        mock_delete_branch.assert_not_awaited()
        mock_post_merge.assert_awaited_once()

    async def test_exception_in_cleanup_is_logged_and_suppressed_in_finalize(self, session) -> None:
        """Exception in _crash_recovery_cleanup is caught in _wait_and_finalize, logged, but finalization completes."""
        from pathlib import Path
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.dashboard.services.agent_db import OutcomeValidation
        from sova.dashboard.services.agent_pool import AgentState, ProjectAgents
        from sova.db.models import TaskRun
        from sova.ipc.control import FileAgentProcess

        # Create a TaskRun in the database
        run = TaskRun(
            issue_number="42",
            role="command:integrate-pr",
            status="running",
            project_slug="test-project",
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)

        # Create an agent with the run_id
        mock_process = AsyncMock(spec=FileAgentProcess)
        mock_process.wait.return_value = 1  # Non-zero exit
        mock_process.returncode = 1
        mock_process.stop = AsyncMock()

        project_dir = Path("/fake/project")
        agent = AgentState(
            run_id=run.id,
            project_dir=project_dir,
            issue="42",
            role="command:integrate-pr",
            process=mock_process,
            pr_number=100,
            last_result_cost=0.0,
            output_writer=None,
        )

        pa = ProjectAgents(project_dir=project_dir)
        pa.agents[run.id] = agent

        with (
            patch(
                "sova.dashboard.services.agent_finalize._check_pr_merged_on_failure",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "sova.dashboard.services.agent_finalize._crash_recovery_cleanup",
                new_callable=AsyncMock,
                side_effect=RuntimeError("GitHub API timeout"),
            ) as mock_cleanup,
            patch("sova.dashboard.services.agent_resource._finalize_resource_monitoring", new_callable=AsyncMock),
            patch(
                "sova.dashboard.services.agent_finalize._validate_command_outcome",
                new_callable=AsyncMock,
                return_value=OutcomeValidation(None),
            ),
            patch(
                "sova.dashboard.services.agent_finalize._validate_pipeline_outcome",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("sova.dashboard.services.agent_finalize._check_merge_queue_marker_file", new_callable=AsyncMock),
            patch("sova.dashboard.services.agent_handoff._process_auto_handoff", new_callable=AsyncMock),
            patch("sova.dashboard.routers.agents._ws_manager", MagicMock()),
        ):
            from sova.dashboard.services.agent_lifecycle import _wait_and_finalize

            # Should not raise even though _crash_recovery_cleanup raises
            await _wait_and_finalize(pa, agent)

            # Verify cleanup was called
            mock_cleanup.assert_awaited_once()

            # Verify the run was marked done (merge succeeded)
            await session.refresh(run)
            assert run.status == "done"

    async def test_returns_early_when_pr_number_is_none(self) -> None:
        """When agent.pr_number is None, cleanup returns immediately without git operations."""
        from unittest.mock import AsyncMock, patch

        agent = self._make_agent(pr_number=None)

        with (
            patch("sova.dashboard.services.agent_finalize.load_config") as mock_load_config,
            patch(
                "sova.dashboard.services.agent_finalize.get_pr_branch",
                new_callable=AsyncMock,
            ) as mock_get_branch,
            patch(
                "sova.dashboard.services.agent_finalize.delete_remote_branch",
                new_callable=AsyncMock,
            ) as mock_delete_branch,
            patch(
                "sova.dashboard.services.agent_finalize.handle_post_merge_state",
                new_callable=AsyncMock,
            ) as mock_post_merge,
        ):
            from sova.dashboard.services.agent_lifecycle import _crash_recovery_cleanup

            await _crash_recovery_cleanup(agent)

        mock_load_config.assert_not_called()
        mock_get_branch.assert_not_awaited()
        mock_delete_branch.assert_not_awaited()
        mock_post_merge.assert_not_awaited()

    async def test_skips_when_no_github_repo_configured(self) -> None:
        """When cfg.github_repo is None (JIRA-only project), cleanup returns early."""
        from unittest.mock import AsyncMock, MagicMock, patch

        agent = self._make_agent()

        with (
            patch("sova.dashboard.services.agent_finalize.load_config") as mock_load_config,
            patch(
                "sova.dashboard.services.agent_finalize.get_pr_branch",
                new_callable=AsyncMock,
            ) as mock_get_branch,
            patch(
                "sova.dashboard.services.agent_finalize.delete_remote_branch",
                new_callable=AsyncMock,
            ) as mock_delete_branch,
            patch(
                "sova.dashboard.services.agent_finalize.handle_post_merge_state",
                new_callable=AsyncMock,
            ) as mock_post_merge,
        ):
            mock_cfg = MagicMock()
            mock_cfg.github_repo = None
            mock_cfg.github_user = "testuser"
            mock_load_config.return_value = mock_cfg

            from sova.dashboard.services.agent_lifecycle import _crash_recovery_cleanup

            await _crash_recovery_cleanup(agent)

        mock_get_branch.assert_not_awaited()
        mock_delete_branch.assert_not_awaited()
        mock_post_merge.assert_not_awaited()
