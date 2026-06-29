"""Tests for sova.scheduler -- watch loop, parallel executor, server daemon."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
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
        config = _make_config(watch=WatchConfig(veto_seconds=1))

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
        config = _make_config(max_parallel_agents=2, watch=WatchConfig(veto_seconds=1))

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


# ---------------------------------------------------------------------------
# WatchLoop edge-case tests
# ---------------------------------------------------------------------------


class TestWatchLoopEdgeCases:
    """Additional edge-case tests for WatchLoop."""

    async def test_scan_includes_in_progress_tasks(self) -> None:
        from sova.scheduler.watch import WatchLoop

        tasks = [_make_task("1", "In Progress", TaskState.IN_PROGRESS)]
        adapter = _mock_adapter(tasks=tasks)
        loop = WatchLoop(config=_make_config(), adapter=adapter)

        actionable = await loop.scan()
        assert len(actionable) == 1
        assert actionable[0].id == "1"

    async def test_scan_excludes_done_and_in_review(self) -> None:
        from sova.scheduler.watch import WatchLoop

        tasks = [
            _make_task("1", "Done", TaskState.DONE),
            _make_task("2", "In review", TaskState.IN_REVIEW),
            _make_task("3", "Backlog", TaskState.BACKLOG),
        ]
        adapter = _mock_adapter(tasks=tasks)
        loop = WatchLoop(config=_make_config(), adapter=adapter)

        actionable = await loop.scan()
        assert len(actionable) == 1
        assert actionable[0].id == "3"

    async def test_scan_unknown_state_gets_low_priority(self) -> None:
        from sova.scheduler.watch import WatchLoop

        tasks = [
            _make_task("1", "Backlog", TaskState.BACKLOG),
            _make_task("2", "In Progress", TaskState.IN_PROGRESS),
        ]
        adapter = _mock_adapter(tasks=tasks)
        loop = WatchLoop(config=_make_config(), adapter=adapter)

        # Patch _STATE_PRIORITY to exclude BACKLOG, forcing the .get(state, 99) fallback
        patched_priority = {TaskState.RESEARCHED: 0, TaskState.IN_PROGRESS: 1, TaskState.TRIAGED: 2}
        with patch("sova.scheduler.watch._STATE_PRIORITY", patched_priority):
            actionable = await loop.scan()
        # IN_PROGRESS (priority 1) before BACKLOG (fallback 99)
        assert actionable[0].id == "2"
        assert actionable[1].id == "1"

    @pytest.mark.parametrize("veto_seconds", [0, -1])
    async def test_check_veto_non_positive_seconds_fast_paths(self, veto_seconds: int) -> None:
        from sova.scheduler.watch import WatchLoop

        config = _make_config()
        adapter = _mock_adapter()
        loop = WatchLoop(config=config, adapter=adapter)
        # Bypass validation to test the <= 0 guard in check_veto
        loop._config.watch.veto_seconds = veto_seconds  # type: ignore[assignment]

        task = _make_task("42")
        result = await loop.check_veto(task)
        assert result is True

    async def test_check_veto_cancelled_by_stop(self) -> None:
        from sova.scheduler.watch import WatchLoop

        config = _make_config(watch=WatchConfig(veto_seconds=60))
        adapter = _mock_adapter()
        loop = WatchLoop(config=config, adapter=adapter)
        task = _make_task("42")

        async def stop_soon() -> None:
            await asyncio.sleep(0.05)
            loop.stop()

        asyncio.create_task(stop_soon())
        result = await loop.check_veto(task)
        assert result is False

    async def test_run_idle_cycle_then_stop(self) -> None:
        from sova.scheduler.watch import WatchLoop

        adapter = _mock_adapter(tasks=[])
        config = _make_config()
        config.watch.interval_idle = 1  # type: ignore[assignment]
        loop = WatchLoop(config=config, adapter=adapter)

        call_count = 0
        original_scan = loop.scan

        async def counting_scan() -> list:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                loop.stop()
            return await original_scan()

        loop.scan = counting_scan  # type: ignore[assignment]
        await loop.run()

        assert call_count >= 2

    async def test_run_handles_scan_exception(self) -> None:
        from sova.scheduler.watch import WatchLoop

        adapter = _mock_adapter()
        adapter.list_tasks.side_effect = [RuntimeError("boom"), []]
        config = _make_config()
        config.watch.interval_active = 1  # type: ignore[assignment]
        loop = WatchLoop(config=config, adapter=adapter)

        call_count = 0
        original_scan = loop.scan

        async def stop_after_two() -> list:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                loop.stop()
            return await original_scan()

        loop.scan = stop_after_two  # type: ignore[assignment]
        await loop.run()

        assert call_count >= 2
        assert loop.is_running is False

    async def test_run_sets_running_flag(self) -> None:
        from sova.scheduler.watch import WatchLoop

        adapter = _mock_adapter(tasks=[])
        config = _make_config()
        config.watch.interval_idle = 1  # type: ignore[assignment]
        loop = WatchLoop(config=config, adapter=adapter)
        was_running = False

        original_scan = loop.scan

        async def check_running() -> list:
            nonlocal was_running
            was_running = loop.is_running
            loop.stop()
            return await original_scan()

        loop.scan = check_running  # type: ignore[assignment]
        await loop.run()

        assert was_running is True
        assert loop.is_running is False

    async def test_stop_sets_event_and_flag(self) -> None:
        from sova.scheduler.watch import WatchLoop

        loop = WatchLoop(config=_make_config(), adapter=_mock_adapter())
        assert loop._stop_event.is_set() is False

        loop.stop()
        assert loop.is_running is False
        assert loop._stop_event.is_set() is True

    @patch("sova.scheduler.watch.dispatch", new=AsyncMock())
    async def test_process_task_uses_project_dir(self) -> None:
        from sova.roles.base import RoleResult
        from sova.scheduler.watch import WatchLoop, dispatch

        dispatch.return_value = (MagicMock(name="dev"), RoleResult(success=True, summary="OK"))
        adapter = _mock_adapter()
        loop = WatchLoop(config=_make_config(), adapter=adapter, project_dir="/my/project")

        task = _make_task("42")
        await loop.process_task(task)

        ctx_arg = dispatch.call_args[0][0]
        assert str(ctx_arg.project_dir) == "/my/project"


# ---------------------------------------------------------------------------
# ParallelExecutor edge-case tests
# ---------------------------------------------------------------------------


class TestParallelExecutorEdgeCases:
    """Additional edge-case tests for ParallelExecutor."""

    async def test_execute_tasks_empty_list_returns_empty(self) -> None:
        from sova.scheduler.parallel import ParallelExecutor

        executor = ParallelExecutor(config=_make_config())
        results = await executor.execute_tasks([], adapter=_mock_adapter())
        assert results == []

    async def test_execute_tasks_passes_force_flag(self) -> None:
        from sova.scheduler.parallel import ParallelExecutor

        adapter = _mock_adapter(default_state=TaskState.RESEARCHED)
        executor = ParallelExecutor(config=_make_config())
        tasks = [_make_task("1", state=TaskState.RESEARCHED)]

        with patch("sova.scheduler.parallel.dispatch", new=AsyncMock()) as mock_dispatch:
            from sova.roles.base import RoleResult

            mock_dispatch.return_value = (MagicMock(), RoleResult(success=True, summary="OK"))
            await executor.execute_tasks(tasks, adapter=adapter, force=True)

            ctx_arg = mock_dispatch.call_args[0][0]
            assert ctx_arg.force is True

    async def test_dispatch_task_returns_role_name(self) -> None:
        from sova.scheduler.parallel import ParallelExecutor

        adapter = _mock_adapter(default_state=TaskState.RESEARCHED)
        executor = ParallelExecutor(config=_make_config())
        tasks = [_make_task("1", state=TaskState.RESEARCHED)]

        with patch("sova.scheduler.parallel.dispatch", new=AsyncMock()) as mock_dispatch:
            from sova.roles.base import RoleResult

            role_mock = MagicMock()
            role_mock.name = "developer"
            mock_dispatch.return_value = (role_mock, RoleResult(success=True, summary="Done"))
            results = await executor.execute_tasks(tasks, adapter=adapter)

        assert results[0].role == "developer"

    async def test_dispatch_task_failure_includes_error(self) -> None:
        from sova.scheduler.parallel import ParallelExecutor

        adapter = _mock_adapter(default_state=TaskState.RESEARCHED)
        executor = ParallelExecutor(config=_make_config())
        tasks = [_make_task("1", state=TaskState.RESEARCHED)]

        with patch("sova.scheduler.parallel.dispatch", new=AsyncMock()) as mock_dispatch:
            from sova.roles.base import RoleResult

            role_mock = MagicMock()
            role_mock.name = "developer"
            mock_dispatch.return_value = (role_mock, RoleResult(success=False, summary="", error="lint failed"))
            results = await executor.execute_tasks(tasks, adapter=adapter)

        assert results[0].success is False
        assert results[0].error == "lint failed"
        assert results[0].role == "developer"

    async def test_active_count_is_zero_when_idle(self) -> None:
        from sova.scheduler.parallel import ParallelExecutor

        executor = ParallelExecutor(config=_make_config())
        assert executor.active_count == 0

    async def test_project_dir_defaults_to_cwd(self) -> None:
        from sova.scheduler.parallel import ParallelExecutor

        executor = ParallelExecutor(config=_make_config())
        assert executor._project_dir == Path.cwd()

    async def test_project_dir_uses_provided_path(self) -> None:
        from sova.scheduler.parallel import ParallelExecutor

        executor = ParallelExecutor(config=_make_config(), project_dir=Path("/custom"))
        assert executor._project_dir == Path("/custom")


# ---------------------------------------------------------------------------
# read_pid_file / stop_server edge-case tests
# ---------------------------------------------------------------------------


class TestReadPidFile:
    """Tests for sova.scheduler.server.read_pid_file module-level function."""

    def test_read_pid_file_nonexistent(self, tmp_path: Path) -> None:
        from sova.scheduler.server import read_pid_file

        config = _make_config(server={"pid_file": str(tmp_path / "missing.pid")})
        assert read_pid_file(config) is None

    def test_read_pid_file_empty_file(self, tmp_path: Path) -> None:
        from sova.scheduler.server import read_pid_file

        pid_file = tmp_path / "empty.pid"
        pid_file.write_text("")
        config = _make_config(server={"pid_file": str(pid_file)})
        assert read_pid_file(config) is None

    def test_read_pid_file_invalid_content(self, tmp_path: Path) -> None:
        from sova.scheduler.server import read_pid_file

        pid_file = tmp_path / "bad.pid"
        pid_file.write_text("not-a-number")
        config = _make_config(server={"pid_file": str(pid_file)})
        assert read_pid_file(config) is None

    def test_read_pid_file_alive_process(self, tmp_path: Path) -> None:
        from sova.scheduler.server import read_pid_file

        pid_file = tmp_path / "sova.pid"
        pid_file.write_text(str(os.getpid()))
        config = _make_config(server={"pid_file": str(pid_file)})

        result = read_pid_file(config)
        assert result == os.getpid()

    def test_read_pid_file_stale_pid_cleans_up(self, tmp_path: Path) -> None:
        from sova.scheduler.server import read_pid_file

        pid_file = tmp_path / "stale.pid"
        pid_file.write_text("99999999")
        config = _make_config(server={"pid_file": str(pid_file)})

        with patch("sova.scheduler.server.os.kill", side_effect=OSError("No such process")):
            result = read_pid_file(config)

        assert result is None
        assert not pid_file.exists(), "stale PID file should be removed"

    def test_read_pid_file_no_config_uses_default(self, tmp_path: Path) -> None:
        from sova.scheduler.server import read_pid_file

        with patch("sova.scheduler.server._DEFAULT_PID_DIR", tmp_path):
            result = read_pid_file(None)
        # tmp_path has no PID file, so result is None
        assert result is None

    def test_read_pid_file_whitespace_around_pid(self, tmp_path: Path) -> None:
        from sova.scheduler.server import read_pid_file

        pid_file = tmp_path / "ws.pid"
        pid_file.write_text(f"  {os.getpid()}  \n")
        config = _make_config(server={"pid_file": str(pid_file)})

        result = read_pid_file(config)
        assert result == os.getpid()


class TestStopServer:
    """Tests for sova.scheduler.server.stop_server module-level function."""

    def test_stop_server_no_running_server(self, tmp_path: Path) -> None:
        from sova.scheduler.server import stop_server

        config = _make_config(server={"pid_file": str(tmp_path / "missing.pid")})
        assert stop_server(config) is False

    def test_stop_server_sends_sigterm(self, tmp_path: Path) -> None:
        from sova.scheduler.server import stop_server

        pid_file = tmp_path / "server.pid"
        pid_file.write_text(str(os.getpid()))
        config = _make_config(server={"pid_file": str(pid_file)})

        with patch("sova.scheduler.server.os.kill") as mock_kill:
            # First call (signal 0) checks alive, second call sends SIGTERM
            mock_kill.return_value = None
            result = stop_server(config)

        assert result is True
        # Verify SIGTERM was sent
        sigterm_calls = [c for c in mock_kill.call_args_list if c[0][1] == signal.SIGTERM]
        assert len(sigterm_calls) == 1

    def test_stop_server_kill_fails_returns_false(self, tmp_path: Path) -> None:
        from sova.scheduler.server import stop_server

        pid_file = tmp_path / "server.pid"
        pid_file.write_text(str(os.getpid()))
        config = _make_config(server={"pid_file": str(pid_file)})

        call_count = 0

        def kill_side_effect(pid: int, sig: int) -> None:
            nonlocal call_count
            call_count += 1
            if sig == signal.SIGTERM:
                raise OSError("Permission denied")

        with patch("sova.scheduler.server.os.kill", side_effect=kill_side_effect):
            result = stop_server(config)

        assert result is False


# ---------------------------------------------------------------------------
# SOVAServer additional tests
# ---------------------------------------------------------------------------


class TestSOVAServerEdgeCases:
    """Additional edge-case tests for SOVAServer."""

    async def test_health_check_endpoint(self) -> None:
        import httpx
        from httpx import ASGITransport

        from sova.scheduler.server import SOVAServer

        config = _make_config()
        server = SOVAServer(config=config)
        app = server.create_app()

        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert "uptime_s" in data
            assert "scheduler_running" in data
            assert "agents_active" in data

    async def test_scheduler_status_reports_config(self) -> None:
        import httpx
        from httpx import ASGITransport

        from sova.scheduler.server import SOVAServer

        config = _make_config(max_parallel_agents=5, watch=WatchConfig(interval_active=30, interval_idle=120))
        server = SOVAServer(config=config)
        app = server.create_app()

        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/scheduler/status")
            data = resp.json()
            assert data["max_parallel"] == 5
            assert data["watch_interval"] == 30
            assert data["idle_interval"] == 120

    async def test_pid_file_path_uses_config(self, tmp_path: Path) -> None:
        from sova.scheduler.server import SOVAServer

        custom_pid = str(tmp_path / "custom.pid")
        config = _make_config(server={"pid_file": custom_pid})
        server = SOVAServer(config=config)

        assert str(server._pid_file_path()) == custom_pid

    async def test_pid_file_path_default(self) -> None:
        from sova.scheduler.server import _DEFAULT_PID_DIR, SOVAServer

        config = _make_config()
        server = SOVAServer(config=config)

        assert server._pid_file_path() == _DEFAULT_PID_DIR / "sova-server.pid"

    async def test_write_and_remove_pid_file(self, tmp_path: Path) -> None:
        from sova.scheduler.server import SOVAServer

        pid_file = tmp_path / "test.pid"
        config = _make_config(server={"pid_file": str(pid_file)})
        server = SOVAServer(config=config)

        server._write_pid_file()
        assert pid_file.exists()
        assert int(pid_file.read_text()) == os.getpid()

        server._remove_pid_file()
        assert not pid_file.exists()

    async def test_remove_pid_file_missing_is_ok(self, tmp_path: Path) -> None:
        from sova.scheduler.server import SOVAServer

        pid_file = tmp_path / "nonexistent.pid"
        config = _make_config(server={"pid_file": str(pid_file)})
        server = SOVAServer(config=config)

        # Should not raise
        server._remove_pid_file()
