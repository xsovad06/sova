"""Shutdown resilience tests for the SOVA dashboard.

These tests verify that the dashboard shuts down cleanly under all agent
states, including running agents, active WebSocket connections, and batch
operations. They target the exact failure modes that caused repeated
dashboard freezes during WatchFiles-triggered reloads.

All tests are marked @pytest.mark.runtime and excluded from the standard
CI pipeline. Run with: make test-runtime
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.dashboard.services.agent_pool import AgentState


@pytest.mark.runtime
class TestShutdownCancelsWsProducerTasks:
    """WebSocket producer tasks must be cancelled during shutdown."""

    async def test_cancel_all_stops_producer_tasks(self, ws_manager) -> None:
        """cancel_all() cancels all running producer tasks."""
        produced = False

        async def _fake_produce() -> None:
            nonlocal produced
            await asyncio.sleep(3600)
            produced = True

        ws_manager._producer_tasks[None] = asyncio.create_task(_fake_produce())
        ws_manager._groups[None] = [MagicMock()]

        await ws_manager.cancel_all()

        assert not produced
        assert len(ws_manager._producer_tasks) == 0
        assert len(ws_manager._groups) == 0

    async def test_cancel_all_idempotent(self, ws_manager) -> None:
        """Calling cancel_all() on an empty manager is safe."""
        await ws_manager.cancel_all()
        await ws_manager.cancel_all()

    async def test_broadcast_timeout_on_slow_client(self, ws_manager) -> None:
        """_broadcast times out on a WebSocket that blocks on send_json."""
        slow_ws = AsyncMock()

        async def _slow_send(data: dict) -> None:
            await asyncio.sleep(30)

        slow_ws.send_json = _slow_send
        ws_manager._groups[None] = [slow_ws]

        await asyncio.wait_for(
            ws_manager._broadcast({"type": "test"}, None),
            timeout=5.0,
        )

    async def test_produce_loop_handles_cancellation(self, ws_manager) -> None:
        """_produce_loop re-raises CancelledError after cleanup."""
        mock_ws = AsyncMock()
        ws_manager._groups[None] = [mock_ws]

        with (
            patch(
                "sova.dashboard.services.agent_status.get_all_agent_statuses",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "sova.dashboard.services.agent_status.format_status_update",
                return_value={"type": "status_update", "runs": []},
            ),
        ):
            task = asyncio.create_task(ws_manager._produce_loop(None))
            await asyncio.sleep(0.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.done()
            assert task.cancelled()


@pytest.mark.runtime
class TestShutdownCancelsBatchTasks:
    """Batch job tasks must be cancelled during shutdown."""

    async def test_cancel_all_batches_stops_running_batches(self) -> None:
        """cancel_all_batches() cancels tasks in _active_batches."""
        from sova.dashboard.services.batch_service import (
            BatchJob,
            _active_batches,
            cancel_all_batches,
        )

        finished = False

        async def _fake_batch() -> None:
            nonlocal finished
            await asyncio.sleep(3600)
            finished = True

        job = BatchJob(
            batch_id="test-1",
            action="triage",
            project_dir=Path("/tmp"),
        )
        job._task = asyncio.create_task(_fake_batch())
        _active_batches["test-1"] = job

        try:
            await cancel_all_batches()
            assert job._task.cancelled()
            assert not finished
        finally:
            _active_batches.pop("test-1", None)

    async def test_cancel_all_batches_idempotent(self) -> None:
        """Calling cancel_all_batches() with no active batches is safe."""
        from sova.dashboard.services.batch_service import cancel_all_batches

        await cancel_all_batches()


@pytest.mark.runtime
class TestShutdownCancelsAllAgentTasks:
    """All per-agent tasks must be cancelled during shutdown."""

    async def test_cancels_reader_stderr_resource_tasks(self, clean_agent_state) -> None:
        """cancel_background_tasks() cancels all three per-agent task types."""
        from sova.dashboard.services.agent_lifecycle import cancel_background_tasks
        from sova.dashboard.services.agent_pool import _get_project_agents

        pa = _get_project_agents()
        mock_process = MagicMock()
        mock_process.pid = 99999

        agent = AgentState(run_id=9999, issue="999", role="developer", process=mock_process)

        async def _hang() -> None:
            await asyncio.sleep(3600)

        agent.reader_task = asyncio.create_task(_hang())
        agent.stderr_task = asyncio.create_task(_hang())
        agent.resource_flush_task = asyncio.create_task(_hang())
        pa.agents[9999] = agent

        try:
            await cancel_background_tasks()
            assert agent.reader_task.cancelled()
            assert agent.stderr_task.cancelled()
            assert agent.resource_flush_task.cancelled()
        finally:
            pa.agents.pop(9999, None)

    async def test_stops_resource_collectors(self, clean_agent_state) -> None:
        """cancel_background_tasks() stops ResourceCollectors on each agent."""
        from sova.dashboard.services.agent_lifecycle import cancel_background_tasks
        from sova.dashboard.services.agent_pool import _get_project_agents

        pa = _get_project_agents()
        mock_process = MagicMock()
        mock_process.pid = 99999
        mock_collector = AsyncMock()

        agent = AgentState(run_id=9999, issue="999", role="developer", process=mock_process)
        agent.resource_collector = mock_collector
        pa.agents[9999] = agent

        try:
            await cancel_background_tasks()
            mock_collector.stop.assert_awaited_once()
        finally:
            pa.agents.pop(9999, None)


@pytest.mark.runtime
class TestShutdownTimeout:
    """Shutdown must complete within the timeout even with stuck tasks."""

    async def test_cancel_background_tasks_timeout_on_stuck_task(self) -> None:
        """If a task's CancelledError handler blocks, shutdown still completes."""
        from sova.dashboard.services import agent_lifecycle
        from sova.dashboard.services.agent_lifecycle import cancel_background_tasks

        async def _sticky_task() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(3600)

        task = asyncio.create_task(_sticky_task())
        agent_lifecycle._background_tasks.add(task)

        try:
            await asyncio.wait_for(cancel_background_tasks(), timeout=5.0)
        finally:
            agent_lifecycle._background_tasks.discard(task)
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def test_full_shutdown_tasks_completes_within_timeout(self) -> None:
        """_shutdown_tasks completes even when all subsystems have stuck tasks."""
        from sova.dashboard.app import _shutdown_tasks

        async def _hang() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(3600)

        sweep = asyncio.create_task(_hang())

        with (
            patch("sova.dashboard.services.agent_lifecycle.cancel_background_tasks", new_callable=AsyncMock),
            patch("sova.dashboard.routers.agents._ws_manager") as mock_ws,
            patch("sova.dashboard.services.batch_service.cancel_all_batches", new_callable=AsyncMock),
        ):
            mock_ws.cancel_all = AsyncMock()
            await asyncio.wait_for(
                _shutdown_tasks(sweep, [], [], None),
                timeout=10.0,
            )


@pytest.mark.runtime
class TestWaitAndFinalizeCancellation:
    """_wait_and_finalize must handle CancelledError during process.wait()."""

    async def test_cancellation_terminates_process(self) -> None:
        """When _wait_and_finalize is cancelled, it terminates the agent process."""
        from sova.dashboard.services.agent_lifecycle import _wait_and_finalize
        from sova.dashboard.services.agent_pool import AgentState, _get_project_agents
        from sova.ipc.testing import MockAgentProcess

        pa = _get_project_agents()
        process = MockAgentProcess(should_hang=True)
        agent = AgentState(run_id=9999, issue="999", role="developer", process=process)
        pa.agents[9999] = agent

        task = asyncio.create_task(_wait_and_finalize(pa, agent))
        await asyncio.sleep(0.05)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert not process.is_running

        pa.agents.pop(9999, None)
