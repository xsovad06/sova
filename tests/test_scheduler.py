"""Tests for sova.scheduler -- watch loop, parallel executor, server daemon."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.adapters.base import Task, TaskState
from sova.config.models import ProjectConfig, WatchConfig
from sova.db.session import close_db, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    """Initialize an in-memory DB for scheduler tests."""
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


def _make_config(**overrides: object) -> ProjectConfig:
    defaults = {
        "github_repo": "owner/repo",
        "max_parallel_agents": 2,
    }
    defaults.update(overrides)
    return ProjectConfig(**defaults)


def _make_task(
    task_id: str = "42",
    title: str = "Test issue",
    state: TaskState = TaskState.BACKLOG,
) -> Task:
    return Task(id=task_id, title=title, body="Body", state=state)


def _mock_adapter(
    tasks: list[Task] | None = None,
    default_state: TaskState = TaskState.BACKLOG,
) -> AsyncMock:
    adapter = AsyncMock()
    adapter.get_state.return_value = default_state
    adapter.list_tasks.return_value = tasks or []
    adapter.get_task.side_effect = lambda tid: Task(
        id=tid,
        title=f"Issue #{tid}",
        body="Body",
        state=default_state,
    )
    return adapter


# ---------------------------------------------------------------------------
# WatchLoop tests
# ---------------------------------------------------------------------------


class TestWatchLoop:
    """Tests for sova.scheduler.watch.WatchLoop."""

    async def test_scan_finds_actionable_tasks(self) -> None:
        from sova.scheduler.watch import WatchLoop

        tasks = [
            _make_task("1", "Backlog task", TaskState.BACKLOG),
            _make_task("2", "Done task", TaskState.DONE),
            _make_task("3", "Triaged task", TaskState.TRIAGED),
        ]
        adapter = _mock_adapter(tasks=tasks)
        config = _make_config()

        loop = WatchLoop(config=config, adapter=adapter)
        actionable = await loop.scan()

        assert len(actionable) == 2
        # Sorted by pipeline priority: TRIAGED (2) before BACKLOG (3)
        assert actionable[0].id == "3"
        assert actionable[1].id == "1"

    async def test_scan_returns_empty_when_no_tasks(self) -> None:
        from sova.scheduler.watch import WatchLoop

        adapter = _mock_adapter(tasks=[])
        config = _make_config()

        loop = WatchLoop(config=config, adapter=adapter)
        actionable = await loop.scan()

        assert actionable == []

    async def test_scan_orders_by_pipeline_stage(self) -> None:
        from sova.scheduler.watch import WatchLoop

        tasks = [
            _make_task("1", "Backlog", TaskState.BACKLOG),
            _make_task("2", "Researched", TaskState.RESEARCHED),
            _make_task("3", "Triaged", TaskState.TRIAGED),
        ]
        adapter = _mock_adapter(tasks=tasks)
        config = _make_config()

        loop = WatchLoop(config=config, adapter=adapter)
        actionable = await loop.scan()

        # Researched tasks are most ready, then triaged, then backlog
        assert actionable[0].id == "2"
        assert actionable[1].id == "3"
        assert actionable[2].id == "1"

    @patch("sova.scheduler.watch.dispatch", new=AsyncMock())
    async def test_process_task_dispatches_role(self) -> None:
        from sova.roles.base import RoleResult
        from sova.scheduler.watch import WatchLoop, dispatch

        dispatch.return_value = (MagicMock(name="triage"), RoleResult(success=True, summary="OK"))
        adapter = _mock_adapter()
        config = _make_config()

        loop = WatchLoop(config=config, adapter=adapter)
        task = _make_task("42", state=TaskState.BACKLOG)
        result = await loop.process_task(task)

        assert result is True
        dispatch.assert_called_once()

    @patch("sova.scheduler.watch.dispatch", new=AsyncMock())
    async def test_process_task_returns_false_on_failure(self) -> None:
        from sova.roles.base import RoleResult
        from sova.scheduler.watch import WatchLoop, dispatch

        dispatch.return_value = (MagicMock(name="dev"), RoleResult(success=False, summary="", error="Failed"))
        adapter = _mock_adapter()
        config = _make_config()

        loop = WatchLoop(config=config, adapter=adapter)
        task = _make_task("42")
        result = await loop.process_task(task)

        assert result is False

    @patch("sova.scheduler.watch.dispatch", new=AsyncMock())
    async def test_process_task_handles_exception(self) -> None:
        from sova.scheduler.watch import WatchLoop, dispatch

        dispatch.side_effect = RuntimeError("boom")
        adapter = _mock_adapter()
        config = _make_config()

        loop = WatchLoop(config=config, adapter=adapter)
        task = _make_task("42")
        result = await loop.process_task(task)

        assert result is False

    async def test_run_processes_one_cycle_then_stops(self) -> None:
        from sova.scheduler.watch import WatchLoop

        tasks = [_make_task("1", state=TaskState.RESEARCHED)]
        adapter = _mock_adapter(tasks=tasks)
        config = _make_config()

        loop = WatchLoop(config=config, adapter=adapter)

        call_count = 0

        async def mock_process(task: Task) -> bool:
            nonlocal call_count
            call_count += 1
            loop.stop()
            return True

        loop.process_task = mock_process  # type: ignore[assignment]
        await loop.run()

        assert call_count == 1

    async def test_veto_window_skips_task_when_vetoed(self) -> None:
        from sova.scheduler.watch import WatchLoop

        config = _make_config(watch=WatchConfig(veto_seconds=1))
        adapter = _mock_adapter()
        loop = WatchLoop(config=config, adapter=adapter)

        # Short veto window expires without interruption, task proceeds
        task = _make_task("42", state=TaskState.BACKLOG)
        can_proceed = await loop.check_veto(task)
        assert can_proceed is True

    async def test_is_running_reflects_state(self) -> None:
        from sova.scheduler.watch import WatchLoop

        config = _make_config()
        adapter = _mock_adapter()
        loop = WatchLoop(config=config, adapter=adapter)

        assert loop.is_running is False
        # After calling stop it should still be False
        loop.stop()
        assert loop.is_running is False


# ---------------------------------------------------------------------------
# ParallelExecutor tests
# ---------------------------------------------------------------------------


class TestParallelExecutor:
    """Tests for sova.scheduler.parallel.ParallelExecutor."""

    async def test_submit_respects_max_concurrent(self) -> None:
        from sova.scheduler.parallel import ParallelExecutor

        config = _make_config(max_parallel_agents=2)
        executor = ParallelExecutor(config=config)

        assert executor.max_concurrent == 2
        assert executor.active_count == 0

    async def test_execute_tasks_runs_all(self) -> None:
        from sova.scheduler.parallel import ParallelExecutor

        config = _make_config(max_parallel_agents=3)
        adapter = _mock_adapter(default_state=TaskState.RESEARCHED)
        executor = ParallelExecutor(config=config)

        tasks = [
            _make_task("1", state=TaskState.RESEARCHED),
            _make_task("2", state=TaskState.RESEARCHED),
        ]

        with patch("sova.scheduler.parallel.dispatch", new=AsyncMock()) as mock_dispatch:
            from sova.roles.base import RoleResult

            mock_dispatch.return_value = (MagicMock(), RoleResult(success=True, summary="OK"))

            results = await executor.execute_tasks(tasks, adapter=adapter)

        assert len(results) == 2
        assert all(r.success for r in results)

    async def test_execute_tasks_limits_concurrency(self) -> None:
        from sova.scheduler.parallel import ParallelExecutor

        config = _make_config(max_parallel_agents=1)
        adapter = _mock_adapter(default_state=TaskState.RESEARCHED)
        executor = ParallelExecutor(config=config)

        concurrent_count = 0
        max_concurrent_seen = 0

        async def track_concurrency(*args, **kwargs):
            nonlocal concurrent_count, max_concurrent_seen
            concurrent_count += 1
            max_concurrent_seen = max(max_concurrent_seen, concurrent_count)
            await asyncio.sleep(0.01)
            concurrent_count -= 1
            from sova.roles.base import RoleResult

            return MagicMock(), RoleResult(success=True, summary="OK")

        tasks = [
            _make_task("1", state=TaskState.RESEARCHED),
            _make_task("2", state=TaskState.RESEARCHED),
            _make_task("3", state=TaskState.RESEARCHED),
        ]

        with patch("sova.scheduler.parallel.dispatch", new=AsyncMock(side_effect=track_concurrency)):
            await executor.execute_tasks(tasks, adapter=adapter)

        assert max_concurrent_seen <= 1

    async def test_execute_tasks_collects_failures(self) -> None:
        from sova.scheduler.parallel import ParallelExecutor

        config = _make_config(max_parallel_agents=2)
        adapter = _mock_adapter(default_state=TaskState.RESEARCHED)
        executor = ParallelExecutor(config=config)

        call_count = 0

        async def alternating_dispatch(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            from sova.roles.base import RoleResult

            if call_count % 2 == 0:
                return MagicMock(), RoleResult(success=False, summary="", error="Failed")
            return MagicMock(), RoleResult(success=True, summary="OK")

        tasks = [
            _make_task("1", state=TaskState.RESEARCHED),
            _make_task("2", state=TaskState.RESEARCHED),
        ]

        with patch("sova.scheduler.parallel.dispatch", new=AsyncMock(side_effect=alternating_dispatch)):
            results = await executor.execute_tasks(tasks, adapter=adapter)

        successes = [r for r in results if r.success]
        failures = [r for r in results if not r.success]
        assert len(successes) == 1
        assert len(failures) == 1

    async def test_execute_tasks_handles_exceptions(self) -> None:
        from sova.scheduler.parallel import ParallelExecutor

        config = _make_config(max_parallel_agents=2)
        adapter = _mock_adapter(default_state=TaskState.RESEARCHED)
        executor = ParallelExecutor(config=config)

        tasks = [_make_task("1", state=TaskState.RESEARCHED)]

        with patch("sova.scheduler.parallel.dispatch", new=AsyncMock(side_effect=RuntimeError("boom"))):
            results = await executor.execute_tasks(tasks, adapter=adapter)

        assert len(results) == 1
        assert results[0].success is False
        assert "boom" in results[0].error

    async def test_stop_cancels_active_tasks(self) -> None:
        from sova.scheduler.parallel import ParallelExecutor

        config = _make_config(max_parallel_agents=2)
        executor = ParallelExecutor(config=config)

        # Stopping when nothing is running should not raise
        await executor.stop()


# ---------------------------------------------------------------------------
# SOVAServer tests
# ---------------------------------------------------------------------------


class TestSOVAServer:
    """Tests for sova.scheduler.server.SOVAServer."""

    async def test_create_server(self) -> None:
        from sova.scheduler.server import SOVAServer

        config = _make_config()
        server = SOVAServer(config=config, host="127.0.0.1", port=8111)

        assert server.host == "127.0.0.1"
        assert server.port == 8111

    async def test_server_creates_dashboard_app(self) -> None:
        from sova.scheduler.server import SOVAServer

        config = _make_config()
        server = SOVAServer(config=config)
        app = server.create_app()

        assert app is not None
        assert app.title == "SOVA Dashboard"

    async def test_server_has_scheduler_status_endpoint(self) -> None:
        import httpx
        from httpx import ASGITransport

        from sova.scheduler.server import SOVAServer

        config = _make_config()
        server = SOVAServer(config=config)
        app = server.create_app()

        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/scheduler/status")
            assert resp.status_code == 200
            data = resp.json()
            assert "running" in data
            assert "active_tasks" in data

    async def test_server_start_stop_lifecycle(self) -> None:
        from sova.scheduler.server import SOVAServer

        config = _make_config()
        server = SOVAServer(config=config)

        assert server.is_running is False
        # stop before start should not raise
        await server.stop()
        assert server.is_running is False


# ---------------------------------------------------------------------------
# CLI commands: sova server start/stop/status
# ---------------------------------------------------------------------------


class TestServerCLI:
    """Tests for sova server CLI subcommands."""

    def test_server_help(self) -> None:
        from typer.testing import CliRunner

        from sova.cli.app import app

        runner = CliRunner()
        result = runner.invoke(app, ["server", "--help"])
        assert result.exit_code == 0
        assert "start" in result.output
        assert "stop" in result.output
        assert "status" in result.output

    def test_server_start_help(self) -> None:
        from typer.testing import CliRunner

        from sova.cli.app import app

        runner = CliRunner()
        result = runner.invoke(app, ["server", "start", "--help"])
        assert result.exit_code == 0
        assert "host" in result.output.lower() or "port" in result.output.lower()

    def test_server_status_shows_not_running(self) -> None:
        from typer.testing import CliRunner

        from sova.cli.app import app

        runner = CliRunner()
        result = runner.invoke(app, ["server", "status"])
        assert result.exit_code == 0
        assert "not running" in result.output.lower() or "stopped" in result.output.lower()


# ---------------------------------------------------------------------------
# ServerConfig tests
# ---------------------------------------------------------------------------


class TestServerConfig:
    """Tests for sova.config.models.ServerConfig."""

    def test_defaults(self) -> None:
        from sova.config.models import ServerConfig

        cfg = ServerConfig()
        assert cfg.host == "127.0.0.1"
        assert cfg.port == 8111
        assert cfg.pid_file == ""
        assert cfg.scheduler_enabled is True

    def test_env_override(self) -> None:
        from sova.config.models import ServerConfig

        os.environ["SOVA_SERVER_HOST"] = "0.0.0.0"
        os.environ["SOVA_SERVER_PORT"] = "9000"
        try:
            cfg = ServerConfig()
            assert cfg.host == "0.0.0.0"
            assert cfg.port == 9000
        finally:
            os.environ.pop("SOVA_SERVER_HOST", None)
            os.environ.pop("SOVA_SERVER_PORT", None)

    def test_project_config_includes_server(self) -> None:
        config = _make_config()
        assert hasattr(config, "server")


# ---------------------------------------------------------------------------
# Integration: WatchLoop + ParallelExecutor
# ---------------------------------------------------------------------------


class TestWatchParallelIntegration:
    """Tests for WatchLoop using ParallelExecutor for concurrent dispatch."""

    async def test_watch_loop_with_parallel_executor(self) -> None:
        from sova.scheduler.parallel import ParallelExecutor
        from sova.scheduler.watch import WatchLoop

        tasks = [
            _make_task("1", state=TaskState.RESEARCHED),
            _make_task("2", state=TaskState.TRIAGED),
        ]
        adapter = _mock_adapter(tasks=tasks, default_state=TaskState.RESEARCHED)
        config = _make_config(max_parallel_agents=2)

        executor = ParallelExecutor(config=config)
        loop = WatchLoop(config=config, adapter=adapter, executor=executor)

        processed = []

        async def mock_process(task: Task) -> bool:
            processed.append(task.id)
            loop.stop()
            return True

        loop.process_task = mock_process  # type: ignore[assignment]
        await loop.run()

        assert len(processed) == 1
        # Should pick the highest-priority task first (RESEARCHED)
        assert processed[0] == "1"
