"""Tests for settings-driven daemon lifecycle management.

Covers: supervisor daemon start/stop on enable toggle, oversight agent
start/stop on enable toggle, task_queue propagation, and restart_required
flag for non-hot-reloadable keys.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestDaemonEnabledGuard:
    """SupervisorDaemon._poll_once skips when supervisor.enabled is False."""

    @pytest.mark.asyncio
    async def test_poll_skips_when_disabled(self) -> None:
        from sova.supervisor.daemon import SupervisorDaemon

        cfg = MagicMock()
        cfg.supervisor.enabled = False
        cfg.supervisor.poll_interval_seconds = 60
        cfg.github_repo = "test/repo"

        daemon = SupervisorDaemon(
            config=cfg,
            project_dir=Path("/tmp/test"),
            session_factory=MagicMock(),
        )

        with patch("sova.config.loader.load_config", return_value=cfg):
            result = await daemon._poll_once()
        assert "skipped" in result

    @pytest.mark.asyncio
    async def test_poll_proceeds_when_enabled(self) -> None:
        from sova.supervisor.daemon import SupervisorDaemon

        cfg = MagicMock()
        cfg.supervisor.enabled = True
        cfg.supervisor.poll_interval_seconds = 60
        cfg.supervisor.auto_queue = False
        cfg.supervisor.require_approval = False
        cfg.supervisor.llm_planning = False
        cfg.github_repo = "test/repo"
        cfg.coderabbit_quota.enabled = False

        session_factory = AsyncMock()

        daemon = SupervisorDaemon(
            config=cfg,
            project_dir=Path("/tmp/test"),
            session_factory=session_factory,
        )

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("sova.supervisor.daemon.SupervisorDaemon._poll_progression", new_callable=AsyncMock) as mock_prog,
        ):
            mock_prog.return_value = ({"decisions": 0, "executed": 0, "pending": 0}, None)
            with patch.object(daemon, "_poll_health", new_callable=AsyncMock, return_value={"db": "ok"}):
                result = await daemon._poll_once()
                assert "skipped" not in result


class TestOversightLoopExit:
    """OversightAgent._run_loop exits when enabled becomes False."""

    @pytest.mark.asyncio
    async def test_loop_exits_when_disabled(self) -> None:
        from sova.oversight.agent import OversightAgent

        cfg = MagicMock()
        cfg.enabled = False
        cfg.wake_interval_minutes = 1

        agent = OversightAgent(config=cfg, project_dir="/tmp/test")
        agent._task = asyncio.current_task()

        with patch.object(agent, "_reload_config", return_value=cfg):
            await agent._run_loop()

        assert agent._task is None
        assert not agent.running

    @pytest.mark.asyncio
    async def test_loop_runs_cycle_when_enabled(self) -> None:
        from sova.oversight.agent import OversightAgent

        call_count = 0

        cfg_enabled = MagicMock()
        cfg_enabled.enabled = True
        cfg_enabled.wake_interval_minutes = 0.001

        cfg_disabled = MagicMock()
        cfg_disabled.enabled = False

        agent = OversightAgent(config=cfg_enabled, project_dir="/tmp/test")

        async def fake_cycle():
            nonlocal call_count
            call_count += 1

        configs = [cfg_enabled, cfg_disabled]

        def reload_side_effect():
            if configs:
                agent._config = configs.pop(0)
            return agent._config

        with (
            patch.object(agent, "_reload_config", side_effect=reload_side_effect),
            patch.object(agent, "run_cycle_once", side_effect=fake_cycle),
        ):
            agent._task = asyncio.current_task()
            await agent._run_loop()

        assert call_count == 1


class TestReloadDaemonConfigLifecycle:
    """_reload_daemon_config stops/starts daemon based on enabled flag."""

    @pytest.mark.asyncio
    async def test_stops_daemon_when_disabled(self) -> None:
        from sova.dashboard.routers import supervisor as sup_mod
        from sova.dashboard.routers.settings import _reload_daemon_config

        daemon = MagicMock()
        daemon.running = True
        daemon.stop = AsyncMock()

        cfg = MagicMock()
        cfg.supervisor.enabled = False

        project_dir = Path("/tmp/test")
        resolved_key = str(project_dir.resolve())

        orig_registry = sup_mod._daemon_registry
        sup_mod._daemon_registry = {resolved_key: daemon}
        try:
            with (
                patch("sova.config.loader.load_config", return_value=cfg),
                patch.object(sup_mod, "_get_daemon", return_value=daemon),
            ):
                await _reload_daemon_config(project_dir)

            daemon.stop.assert_awaited_once()
            assert resolved_key not in sup_mod._daemon_registry
        finally:
            sup_mod._daemon_registry = orig_registry

    @pytest.mark.asyncio
    async def test_starts_daemon_when_enabled_and_none(self) -> None:
        from sova.dashboard.routers import supervisor as sup_mod
        from sova.dashboard.routers.settings import _reload_daemon_config

        cfg = MagicMock()
        cfg.supervisor.enabled = True

        mock_session_factory = AsyncMock()
        mock_daemon_instance = MagicMock()
        mock_daemon_instance.start = MagicMock(return_value=MagicMock())

        orig_registry = sup_mod._daemon_registry
        sup_mod._daemon_registry = {}
        try:
            with (
                patch("sova.config.loader.load_config", return_value=cfg),
                patch.object(sup_mod, "_get_daemon", return_value=None),
                patch(
                    "sova.db.session.get_session_factory",
                    new_callable=AsyncMock,
                    return_value=mock_session_factory,
                ),
                patch(
                    "sova.supervisor.daemon.SupervisorDaemon",
                    return_value=mock_daemon_instance,
                ),
            ):
                await _reload_daemon_config(Path("/tmp/test"))

            mock_daemon_instance.start.assert_called_once()
        finally:
            sup_mod._daemon_registry = orig_registry

    @pytest.mark.asyncio
    async def test_hot_reloads_when_enabled_and_running(self) -> None:
        from sova.dashboard.routers import supervisor as sup_mod
        from sova.dashboard.routers.settings import _reload_daemon_config

        daemon = MagicMock()
        daemon.running = True

        cfg = MagicMock()
        cfg.supervisor.enabled = True

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch.object(sup_mod, "_get_daemon", return_value=daemon),
        ):
            await _reload_daemon_config(Path("/tmp/test"))

        daemon.reload_config.assert_called_once_with(cfg)


class TestReloadOversightConfigLifecycle:
    """_reload_oversight_config stops/starts oversight agent based on enabled flag."""

    @pytest.mark.asyncio
    async def test_stops_agent_when_disabled(self) -> None:
        from sova.dashboard.routers.settings import _reload_oversight_config

        agent = MagicMock()
        agent.running = True
        agent._config.enabled = False
        agent.stop = AsyncMock()

        with patch("sova.dashboard.routers.oversight.get_oversight_agent", return_value=agent):
            await _reload_oversight_config()

        agent.reload_config.assert_called_once()
        agent.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_starts_agent_when_enabled_and_not_running(self) -> None:
        from sova.dashboard.routers.settings import _reload_oversight_config

        agent = MagicMock()
        agent.running = False
        agent._config.enabled = True

        with patch("sova.dashboard.routers.oversight.get_oversight_agent", return_value=agent):
            await _reload_oversight_config()

        agent.reload_config.assert_called_once()
        agent.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_action_when_none(self) -> None:
        from sova.dashboard.routers.settings import _reload_oversight_config

        with patch("sova.dashboard.routers.oversight.get_oversight_agent", return_value=None):
            await _reload_oversight_config()


class TestRestartRequiredFlag:
    """Settings update returns restart_required for non-hot-reloadable keys."""

    def test_server_key_returns_restart_required(self) -> None:
        from sova.dashboard.routers.settings import _RESTART_REQUIRED_PREFIXES

        assert any("server." == p or "server.".startswith(p) for p in _RESTART_REQUIRED_PREFIXES)

    def test_supervisor_key_no_restart_required(self) -> None:
        from sova.dashboard.routers.settings import _RESTART_REQUIRED_PREFIXES

        key = "supervisor.enabled"
        assert not any(key == p or key.startswith(p) for p in _RESTART_REQUIRED_PREFIXES)

    def test_llm_provider_key_no_restart_required(self) -> None:
        """llm.provider is now live-reloadable, not restart-required."""
        from sova.dashboard.routers.settings import _RESTART_REQUIRED_PREFIXES

        key = "llm.provider"
        assert not any(key == p or key.startswith(p) for p in _RESTART_REQUIRED_PREFIXES)

    def test_watch_key_returns_restart_required(self) -> None:
        from sova.dashboard.routers.settings import _RESTART_REQUIRED_PREFIXES

        key = "watch.poll_interval"
        assert any(key == p or key.startswith(p) for p in _RESTART_REQUIRED_PREFIXES)

    def test_database_url_returns_restart_required(self) -> None:
        from sova.dashboard.routers.settings import _RESTART_REQUIRED_PREFIXES

        key = "database_url"
        assert any(key == p or key.startswith(p) for p in _RESTART_REQUIRED_PREFIXES)


class TestLifecycleErrorPropagation:
    """Lifecycle failures propagate to the caller instead of being swallowed."""

    @pytest.mark.asyncio
    async def test_daemon_stop_failure_raises(self) -> None:
        from sova.dashboard.routers import supervisor as sup_mod
        from sova.dashboard.routers.settings import _reload_daemon_config

        daemon = MagicMock()
        daemon.running = True
        daemon.stop = AsyncMock(side_effect=RuntimeError("stop failed"))

        cfg = MagicMock()
        cfg.supervisor.enabled = False

        project_dir = Path("/tmp/test")
        resolved_key = str(project_dir.resolve())

        orig_registry = sup_mod._daemon_registry
        sup_mod._daemon_registry = {resolved_key: daemon}
        try:
            with (
                patch("sova.config.loader.load_config", return_value=cfg),
                patch.object(sup_mod, "_get_daemon", return_value=daemon),
            ):
                with pytest.raises(RuntimeError, match="stop failed"):
                    await _reload_daemon_config(project_dir)
        finally:
            sup_mod._daemon_registry = orig_registry

    @pytest.mark.asyncio
    async def test_daemon_start_failure_raises(self) -> None:
        from sova.dashboard.routers import supervisor as sup_mod
        from sova.dashboard.routers.settings import _reload_daemon_config

        cfg = MagicMock()
        cfg.supervisor.enabled = True

        orig_registry = sup_mod._daemon_registry
        sup_mod._daemon_registry = {}
        try:
            with (
                patch("sova.config.loader.load_config", return_value=cfg),
                patch.object(sup_mod, "_get_daemon", return_value=None),
                patch(
                    "sova.db.session.get_session_factory",
                    new_callable=AsyncMock,
                    side_effect=RuntimeError("db unavailable"),
                ),
            ):
                with pytest.raises(RuntimeError, match="db unavailable"):
                    await _reload_daemon_config(Path("/tmp/test"))
        finally:
            sup_mod._daemon_registry = orig_registry


class TestLifecycleSerialization:
    """Concurrent lifecycle transitions are serialized per project."""

    @pytest.mark.asyncio
    async def test_concurrent_toggles_serialized(self) -> None:
        from sova.dashboard.routers import supervisor as sup_mod
        from sova.dashboard.routers.settings import _reload_daemon_config

        call_order: list[str] = []

        daemon = MagicMock()
        daemon.running = True

        async def slow_stop():
            call_order.append("stop_start")
            await asyncio.sleep(0.05)
            call_order.append("stop_end")

        daemon.stop = slow_stop

        cfg_disable = MagicMock()
        cfg_disable.supervisor.enabled = False

        cfg_enable = MagicMock()
        cfg_enable.supervisor.enabled = True

        mock_session_factory = AsyncMock()
        mock_daemon_instance = MagicMock()
        mock_daemon_instance.start = MagicMock(return_value=MagicMock())

        project_dir = Path("/tmp/test")
        resolved_key = str(project_dir.resolve())

        configs = iter([cfg_disable, cfg_enable])

        orig_registry = sup_mod._daemon_registry
        sup_mod._daemon_registry = {resolved_key: daemon}
        try:
            with (
                patch("sova.config.loader.load_config", side_effect=lambda _: next(configs)),
                patch.object(sup_mod, "_get_daemon", side_effect=lambda _: sup_mod._daemon_registry.get(resolved_key)),
                patch(
                    "sova.db.session.get_session_factory",
                    new_callable=AsyncMock,
                    return_value=mock_session_factory,
                ),
                patch(
                    "sova.supervisor.daemon.SupervisorDaemon",
                    return_value=mock_daemon_instance,
                ),
            ):
                await asyncio.gather(
                    _reload_daemon_config(project_dir),
                    _reload_daemon_config(project_dir),
                )

            assert call_order[0] == "stop_start"
            assert call_order[1] == "stop_end"
        finally:
            sup_mod._daemon_registry = orig_registry


class TestTaskQueuePropagation:
    """task_queue changes propagate to daemon via reload_config."""

    def test_queue_change_reaches_daemon(self) -> None:
        from sova.supervisor.daemon import SupervisorDaemon

        cfg1 = MagicMock()
        cfg1.supervisor.enabled = True
        cfg1.supervisor.task_queue = [1, 2, 3]
        cfg1.supervisor.poll_interval_seconds = 60

        cfg2 = MagicMock()
        cfg2.supervisor.enabled = True
        cfg2.supervisor.task_queue = [4, 5, 6]
        cfg2.supervisor.poll_interval_seconds = 60

        daemon = SupervisorDaemon(
            config=cfg1,
            project_dir=Path("/tmp/test"),
            session_factory=MagicMock(),
        )

        assert daemon._config.supervisor.task_queue == [1, 2, 3]
        daemon.reload_config(cfg2)
        assert daemon._config.supervisor.task_queue == [4, 5, 6]


class TestWatchdogReloadConfig:
    """AgentWatchdog.reload_config swaps config and wakes interruptible sleep."""

    def test_reload_swaps_config(self) -> None:
        from sova.supervisor.watchdog import AgentWatchdog

        cfg1 = MagicMock()
        cfg1.check_interval_seconds = 30
        cfg2 = MagicMock()
        cfg2.check_interval_seconds = 10

        wd = AgentWatchdog(config=cfg1, project_dir=Path("/tmp/test"))
        assert wd._config.check_interval_seconds == 30
        wd.reload_config(cfg2)
        assert wd._config.check_interval_seconds == 10
        assert wd._wake_event.is_set()

    @pytest.mark.asyncio
    async def test_interruptible_sleep_wakes_on_reload(self) -> None:
        from sova.supervisor.watchdog import AgentWatchdog

        cfg = MagicMock()
        cfg.check_interval_seconds = 30

        wd = AgentWatchdog(config=cfg, project_dir=Path("/tmp/test"))

        async def reload_soon():
            await asyncio.sleep(0.05)
            wd.reload_config(cfg)

        asyncio.create_task(reload_soon())
        import time

        t0 = time.monotonic()
        await wd._interruptible_sleep(10.0)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0


class TestPRMonitorReloadConfig:
    """PRMonitor.reload_config swaps config and wakes interruptible sleep."""

    def test_reload_swaps_config(self) -> None:
        from sova.supervisor.pr_monitor import PRMonitor

        cfg1 = MagicMock()
        cfg1.poll_interval = 60
        cfg2 = MagicMock()
        cfg2.poll_interval = 30
        ncfg = MagicMock()

        mon = PRMonitor(
            project_dir=Path("/tmp/test"),
            monitor_config=cfg1,
            notification_config=ncfg,
            repo="test/repo",
            github_user="testuser",
        )
        assert mon.monitor_config.poll_interval == 60
        mon.reload_config(cfg2, ncfg)
        assert mon.monitor_config.poll_interval == 30
        assert mon._wake_event.is_set()

    @pytest.mark.asyncio
    async def test_interruptible_sleep_wakes_on_reload(self) -> None:
        from sova.supervisor.pr_monitor import PRMonitor

        cfg = MagicMock()
        cfg.poll_interval = 60
        ncfg = MagicMock()

        mon = PRMonitor(
            project_dir=Path("/tmp/test"),
            monitor_config=cfg,
            notification_config=ncfg,
            repo="test/repo",
            github_user="testuser",
        )

        async def reload_soon():
            await asyncio.sleep(0.05)
            mon.reload_config(cfg, ncfg)

        asyncio.create_task(reload_soon())
        import time

        t0 = time.monotonic()
        await mon._interruptible_sleep(10.0)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0


class TestMergeQueueMonitorReloadConfig:
    """MergeQueueMonitor.reload_config swaps config and wakes sleep."""

    def test_reload_swaps_config(self) -> None:
        from sova.dashboard.services.merge_queue_monitor import MergeQueueMonitor

        cfg1 = MagicMock()
        cfg1.merge_queue_poll_interval = 120
        cfg2 = MagicMock()
        cfg2.merge_queue_poll_interval = 60

        mon = MergeQueueMonitor(
            project_dir=Path("/tmp/test"),
            repo="test/repo",
            github_user="testuser",
            integration_config=cfg1,
        )
        assert mon.integration_config.merge_queue_poll_interval == 120
        mon.reload_config(cfg2)
        assert mon.integration_config.merge_queue_poll_interval == 60
        assert mon._reload_event.is_set()

    @pytest.mark.asyncio
    async def test_interruptible_sleep_wakes_on_reload_not_stop(self) -> None:
        from sova.dashboard.services.merge_queue_monitor import MergeQueueMonitor

        cfg = MagicMock()
        cfg.merge_queue_poll_interval = 120

        mon = MergeQueueMonitor(
            project_dir=Path("/tmp/test"),
            repo="test/repo",
            github_user="testuser",
            integration_config=cfg,
        )

        async def reload_soon():
            await asyncio.sleep(0.05)
            mon.reload_config(cfg)

        asyncio.create_task(reload_soon())
        import time

        t0 = time.monotonic()
        should_stop = await mon._interruptible_sleep(10.0)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0
        assert not should_stop

    @pytest.mark.asyncio
    async def test_interruptible_sleep_exits_on_stop(self) -> None:
        from sova.dashboard.services.merge_queue_monitor import MergeQueueMonitor

        cfg = MagicMock()
        cfg.merge_queue_poll_interval = 120

        mon = MergeQueueMonitor(
            project_dir=Path("/tmp/test"),
            repo="test/repo",
            github_user="testuser",
            integration_config=cfg,
        )

        async def stop_soon():
            await asyncio.sleep(0.05)
            mon.stop()

        asyncio.create_task(stop_soon())
        import time

        t0 = time.monotonic()
        should_stop = await mon._interruptible_sleep(10.0)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0
        assert should_stop


class TestPRThrottleLoopReloadConfig:
    """PRThrottleLoop.reload_config swaps config and wakes interruptible sleep."""

    def test_reload_swaps_config(self) -> None:
        from sova.supervisor.pr_throttle import PRThrottleLoop

        cfg1 = MagicMock()
        cfg1.reviews_per_hour = 4
        cfg2 = MagicMock()
        cfg2.reviews_per_hour = 8

        loop_instance = PRThrottleLoop(
            session_factory=AsyncMock(),
            config=cfg1,
        )
        assert loop_instance._config.reviews_per_hour == 4
        loop_instance.reload_config(cfg2)
        assert loop_instance._config.reviews_per_hour == 8
        assert loop_instance._wake_event.is_set()

    @pytest.mark.asyncio
    async def test_interruptible_sleep_wakes_on_reload(self) -> None:
        from sova.supervisor.pr_throttle import PRThrottleLoop

        cfg = MagicMock()

        loop_instance = PRThrottleLoop(
            session_factory=AsyncMock(),
            config=cfg,
        )

        async def reload_soon():
            await asyncio.sleep(0.05)
            loop_instance.reload_config(cfg)

        asyncio.create_task(reload_soon())
        import time

        t0 = time.monotonic()
        await loop_instance._interruptible_sleep(10.0)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0


class TestOversightAgentReloadConfig:
    """OversightAgent.reload_config accepts ProjectConfig and wakes sleep."""

    def test_reload_with_project_config(self) -> None:
        from sova.oversight.agent import OversightAgent

        cfg = MagicMock()
        cfg.enabled = True
        cfg.wake_interval_minutes = 5

        project_cfg = MagicMock()
        project_cfg.oversight = cfg

        agent = OversightAgent(config=MagicMock(), project_dir="/tmp/test")
        agent.reload_config(project_cfg)
        assert agent._config is cfg
        assert agent._wake_event.is_set()

    def test_reload_without_config_falls_back_to_disk(self) -> None:
        from sova.oversight.agent import OversightAgent

        agent = OversightAgent(config=MagicMock(), project_dir="/tmp/test")
        with patch.object(agent, "_reload_config") as mock_disk:
            agent.reload_config()
            mock_disk.assert_called_once()

    @pytest.mark.asyncio
    async def test_interruptible_sleep_wakes_on_reload(self) -> None:
        from sova.oversight.agent import OversightAgent

        agent = OversightAgent(config=MagicMock(), project_dir="/tmp/test")

        async def reload_soon():
            await asyncio.sleep(0.05)
            agent._wake_event.set()

        asyncio.create_task(reload_soon())
        import time

        t0 = time.monotonic()
        await agent._interruptible_sleep(10.0)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0


class TestSingletonReload:
    """reload_provider and reload_runtime swap global singletons."""

    def test_reload_provider(self) -> None:
        from sova.llm.client import get_provider, reload_provider, reset_provider

        reset_provider()
        old = get_provider()

        cfg = MagicMock()
        cfg.llm.provider = "claude-code"
        cfg.llm.model = "test-model"
        cfg.llm.fallback_model = None
        cfg.llm.api_base = ""

        with patch("sova.llm.provider.create_provider") as mock_create:
            mock_provider = MagicMock()
            mock_create.return_value = mock_provider
            reload_provider(cfg)

        new = get_provider()
        assert new is mock_provider
        assert new is not old

        reset_provider()

    def test_reload_runtime(self) -> None:
        from sova.ipc.runtime import get_runtime, reload_runtime, set_runtime

        old = get_runtime()

        cfg = MagicMock()
        cfg.agent.runtime = "claude-code"

        with patch("sova.ipc.runtime.create_runtime") as mock_create:
            mock_rt = MagicMock()
            mock_create.return_value = mock_rt
            reload_runtime(cfg)

        new = get_runtime()
        assert new is mock_rt
        assert new is not old

        set_runtime(old)


class TestMatchReloadTarget:
    """_match_reload_target resolves config keys to reload targets."""

    def test_exact_section_match(self) -> None:
        from sova.dashboard.routers.settings import _match_reload_target

        assert _match_reload_target("supervisor") == "supervisor"
        assert _match_reload_target("oversight") == "oversight"
        assert _match_reload_target("watchdog") == "watchdog"
        assert _match_reload_target("llm") == "llm"

    def test_dotted_key_match(self) -> None:
        from sova.dashboard.routers.settings import _match_reload_target

        assert _match_reload_target("supervisor.enabled") == "supervisor"
        assert _match_reload_target("pr_monitor.poll_interval") == "pr_monitor"
        assert _match_reload_target("integration.merge_queue_timeout") == "integration"

    def test_runtime_prefix(self) -> None:
        from sova.dashboard.routers.settings import _match_reload_target

        assert _match_reload_target("agent.runtime") == "runtime"

    def test_no_match_returns_none(self) -> None:
        from sova.dashboard.routers.settings import _match_reload_target

        assert _match_reload_target("server.host") is None
        assert _match_reload_target("unknown_section") is None
        assert _match_reload_target("") is None


class TestReloadOversightConfigWithCfg:
    """_reload_oversight_config_with_cfg manages lifecycle using pre-loaded config."""

    @pytest.mark.asyncio
    async def test_stops_agent_when_disabled(self) -> None:
        from sova.dashboard.routers.settings import _reload_oversight_config_with_cfg

        agent = MagicMock()
        agent.running = True
        agent._config.enabled = False
        agent.stop = AsyncMock()

        cfg = MagicMock()

        with patch("sova.dashboard.routers.oversight.get_oversight_agent", return_value=agent):
            await _reload_oversight_config_with_cfg(cfg)

        agent.reload_config.assert_called_once_with(cfg)
        agent.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_starts_agent_when_enabled_and_not_running(self) -> None:
        from sova.dashboard.routers.settings import _reload_oversight_config_with_cfg

        agent = MagicMock()
        agent.running = False
        agent._config.enabled = True

        cfg = MagicMock()

        with patch("sova.dashboard.routers.oversight.get_oversight_agent", return_value=agent):
            await _reload_oversight_config_with_cfg(cfg)

        agent.reload_config.assert_called_once_with(cfg)
        agent.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_hot_reloads_when_enabled_and_running(self) -> None:
        from sova.dashboard.routers.settings import _reload_oversight_config_with_cfg

        agent = MagicMock()
        agent.running = True
        agent._config.enabled = True

        cfg = MagicMock()

        with patch("sova.dashboard.routers.oversight.get_oversight_agent", return_value=agent):
            await _reload_oversight_config_with_cfg(cfg)

        agent.reload_config.assert_called_once_with(cfg)
        agent.stop.assert_not_called()
        agent.start.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_action_when_agent_is_none(self) -> None:
        from sova.dashboard.routers.settings import _reload_oversight_config_with_cfg

        cfg = MagicMock()
        with patch("sova.dashboard.routers.oversight.get_oversight_agent", return_value=None):
            await _reload_oversight_config_with_cfg(cfg)


class TestDaemonComponentsAccessors:
    """get_daemon_components / set_daemon_components manage the global registry."""

    def test_get_returns_registry(self) -> None:
        from sova.dashboard.app import get_daemon_components, set_daemon_components

        original = get_daemon_components()
        try:
            new_reg: dict = {"key": {"watchdog": MagicMock()}}
            set_daemon_components(new_reg)
            assert get_daemon_components() is new_reg
        finally:
            set_daemon_components(original)

    def test_set_replaces_registry(self) -> None:
        from sova.dashboard.app import get_daemon_components, set_daemon_components

        original = get_daemon_components()
        try:
            reg1: dict = {}
            reg2: dict = {"a": {}}
            set_daemon_components(reg1)
            assert get_daemon_components() is reg1
            set_daemon_components(reg2)
            assert get_daemon_components() is reg2
        finally:
            set_daemon_components(original)


class TestPRThrottleLoopRunExceptionHandling:
    """PRThrottleLoop.run() handles exceptions from session factory."""

    @pytest.mark.asyncio
    async def test_run_catches_session_errors_and_continues(self) -> None:
        from sova.supervisor.pr_throttle import PRThrottleLoop

        call_count = 0
        stop = asyncio.Event()

        async def failing_factory():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                stop.set()
            raise RuntimeError("db error")

        loop_instance = PRThrottleLoop(
            session_factory=failing_factory,
            config=MagicMock(),
            stop_event=stop,
        )

        with patch.object(loop_instance, "_interruptible_sleep", new_callable=AsyncMock):
            await loop_instance.run()
        assert call_count >= 2


class TestProcessQueueLoopWrapper:
    """process_queue_loop backward-compat wrapper delegates to PRThrottleLoop."""

    @pytest.mark.asyncio
    async def test_wrapper_creates_loop_and_runs(self) -> None:
        from sova.supervisor.pr_throttle import process_queue_loop

        stop = asyncio.Event()
        stop.set()

        cfg = MagicMock()
        factory = AsyncMock()

        await process_queue_loop(factory, cfg, stop_event=stop)


class TestInterruptibleSleepTimeout:
    """Interruptible sleep reaches timeout when no event fires."""

    @pytest.mark.asyncio
    async def test_watchdog_sleep_timeout_path(self) -> None:
        from sova.supervisor.watchdog import AgentWatchdog

        wd = AgentWatchdog(config=MagicMock(), project_dir=Path("/tmp/test"))
        import time

        t0 = time.monotonic()
        await wd._interruptible_sleep(0.05)
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.04

    @pytest.mark.asyncio
    async def test_pr_monitor_sleep_timeout_path(self) -> None:
        from sova.supervisor.pr_monitor import PRMonitor

        mon = PRMonitor(
            project_dir=Path("/tmp/test"),
            monitor_config=MagicMock(poll_interval=60),
            notification_config=MagicMock(),
            repo="test/repo",
            github_user="testuser",
        )
        import time

        t0 = time.monotonic()
        await mon._interruptible_sleep(0.05)
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.04

    @pytest.mark.asyncio
    async def test_pr_throttle_sleep_timeout_path(self) -> None:
        from sova.supervisor.pr_throttle import PRThrottleLoop

        loop_instance = PRThrottleLoop(session_factory=AsyncMock(), config=MagicMock())
        import time

        t0 = time.monotonic()
        await loop_instance._interruptible_sleep(0.05)
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.04


class TestMergeQueueReloadWithNotification:
    """MergeQueueMonitor.reload_config passes notification_config when provided."""

    def test_reload_sets_notification_config(self) -> None:
        from sova.dashboard.services.merge_queue_monitor import MergeQueueMonitor

        ncfg = MagicMock()
        mon = MergeQueueMonitor(
            project_dir=Path("/tmp/test"),
            repo="test/repo",
            github_user="testuser",
            integration_config=MagicMock(),
        )
        assert mon.notification_config is None
        new_icfg = MagicMock()
        mon.reload_config(new_icfg, notification_config=ncfg)
        assert mon.notification_config is ncfg


class TestDispatchConfigReload:
    """_dispatch_config_reload routes to the correct daemon."""

    @pytest.mark.asyncio
    async def test_watchdog_none_is_noop(self) -> None:
        from sova.dashboard.routers.settings import _dispatch_config_reload

        cfg = MagicMock()
        await _dispatch_config_reload("watchdog", cfg, {}, Path("/tmp"))

    @pytest.mark.asyncio
    async def test_empty_monitor_list_is_noop(self) -> None:
        from sova.dashboard.routers.settings import _dispatch_config_reload

        cfg = MagicMock()
        await _dispatch_config_reload("pr_monitor", cfg, {"pr_monitors": []}, Path("/tmp"))
        await _dispatch_config_reload("coderabbit_quota", cfg, {"pr_throttles": []}, Path("/tmp"))
        await _dispatch_config_reload("integration", cfg, {"merge_queue_monitors": []}, Path("/tmp"))


class TestReloadAllConfigs:
    """_reload_all_configs dispatches to correct daemons based on key prefix."""

    @pytest.mark.asyncio
    async def test_watchdog_key_dispatches_to_watchdog(self) -> None:
        from sova.dashboard.routers.settings import _reload_all_configs

        cfg = MagicMock()
        wd = MagicMock()

        project_dir = Path("/tmp/test")
        resolved_key = str(project_dir.resolve())

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch(
                "sova.dashboard.app.get_daemon_components",
                return_value={resolved_key: {"watchdog": wd}},
            ),
        ):
            err = await _reload_all_configs(project_dir, "watchdog.check_interval_seconds")

        assert err is None
        wd.reload_config.assert_called_once_with(cfg.watchdog)

    @pytest.mark.asyncio
    async def test_pr_monitor_key_dispatches_to_monitors(self) -> None:
        from sova.dashboard.routers.settings import _reload_all_configs

        cfg = MagicMock()
        mon = MagicMock()

        project_dir = Path("/tmp/test")
        resolved_key = str(project_dir.resolve())

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch(
                "sova.dashboard.app.get_daemon_components",
                return_value={resolved_key: {"pr_monitors": [mon]}},
            ),
        ):
            err = await _reload_all_configs(project_dir, "pr_monitor.poll_interval")

        assert err is None
        mon.reload_config.assert_called_once_with(cfg.pr_monitor, cfg.notification)

    @pytest.mark.asyncio
    async def test_coderabbit_key_dispatches_to_throttle(self) -> None:
        from sova.dashboard.routers.settings import _reload_all_configs

        cfg = MagicMock()
        throttle = MagicMock()

        project_dir = Path("/tmp/test")
        resolved_key = str(project_dir.resolve())

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch(
                "sova.dashboard.app.get_daemon_components",
                return_value={resolved_key: {"pr_throttles": [throttle]}},
            ),
        ):
            err = await _reload_all_configs(project_dir, "coderabbit_quota.reviews_per_hour")

        assert err is None
        throttle.reload_config.assert_called_once_with(cfg.coderabbit_quota)

    @pytest.mark.asyncio
    async def test_integration_key_dispatches_to_merge_queue(self) -> None:
        from sova.dashboard.routers.settings import _reload_all_configs

        cfg = MagicMock()
        mqm = MagicMock()

        project_dir = Path("/tmp/test")
        resolved_key = str(project_dir.resolve())

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch(
                "sova.dashboard.app.get_daemon_components",
                return_value={resolved_key: {"merge_queue_monitors": [mqm]}},
            ),
        ):
            err = await _reload_all_configs(project_dir, "integration.merge_queue_timeout")

        assert err is None
        mqm.reload_config.assert_called_once_with(cfg.integration, cfg.notification)

    @pytest.mark.asyncio
    async def test_llm_key_reloads_provider(self) -> None:
        from sova.dashboard.routers.settings import _reload_all_configs

        cfg = MagicMock()

        project_dir = Path("/tmp/test")

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("sova.dashboard.app.get_daemon_components", return_value={}),
            patch("sova.llm.client.reload_provider") as mock_reload,
        ):
            err = await _reload_all_configs(project_dir, "llm.model")

        assert err is None
        mock_reload.assert_called_once_with(cfg)

    @pytest.mark.asyncio
    async def test_runtime_key_reloads_runtime(self) -> None:
        from sova.dashboard.routers.settings import _reload_all_configs

        cfg = MagicMock()

        project_dir = Path("/tmp/test")

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("sova.dashboard.app.get_daemon_components", return_value={}),
            patch("sova.ipc.runtime.reload_runtime") as mock_reload,
        ):
            err = await _reload_all_configs(project_dir, "agent.runtime")

        assert err is None
        mock_reload.assert_called_once_with(cfg)

    @pytest.mark.asyncio
    async def test_supervisor_key_dispatches_to_daemon_config(self) -> None:
        from sova.dashboard.routers.settings import _reload_all_configs

        cfg = MagicMock()
        project_dir = Path("/tmp/test")

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("sova.dashboard.app.get_daemon_components", return_value={}),
            patch("sova.dashboard.routers.settings._reload_daemon_config", new_callable=AsyncMock) as mock_reload,
        ):
            err = await _reload_all_configs(project_dir, "supervisor.enabled")

        assert err is None
        mock_reload.assert_called_once_with(project_dir)

    @pytest.mark.asyncio
    async def test_oversight_key_dispatches_to_oversight_reload(self) -> None:
        from sova.dashboard.routers.settings import _reload_all_configs

        cfg = MagicMock()
        project_dir = Path("/tmp/test")

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("sova.dashboard.app.get_daemon_components", return_value={}),
            patch(
                "sova.dashboard.routers.settings._reload_oversight_config_with_cfg",
                new_callable=AsyncMock,
            ) as mock_reload,
        ):
            err = await _reload_all_configs(project_dir, "oversight.wake_interval_minutes")

        assert err is None
        mock_reload.assert_called_once_with(cfg)

    @pytest.mark.asyncio
    async def test_unmatched_key_returns_none(self) -> None:
        from sova.dashboard.routers.settings import _reload_all_configs

        project_dir = Path("/tmp/test")

        with patch("sova.dashboard.app.get_daemon_components", return_value={}):
            err = await _reload_all_configs(project_dir, "some_unknown_key")

        assert err is None

    @pytest.mark.asyncio
    async def test_load_config_failure_returns_error_message(self) -> None:
        from sova.dashboard.routers.settings import _reload_all_configs

        project_dir = Path("/tmp/test")

        with patch("sova.config.loader.load_config", side_effect=RuntimeError("config parse error")):
            err = await _reload_all_configs(project_dir, "watchdog.enabled")

        assert err is not None
        assert "watchdog" in err

    @pytest.mark.asyncio
    async def test_reload_failure_returns_error_message(self) -> None:
        from sova.dashboard.routers.settings import _reload_all_configs

        cfg = MagicMock()
        wd = MagicMock()
        wd.reload_config.side_effect = RuntimeError("boom")

        project_dir = Path("/tmp/test")
        resolved_key = str(project_dir.resolve())

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch(
                "sova.dashboard.app.get_daemon_components",
                return_value={resolved_key: {"watchdog": wd}},
            ),
        ):
            err = await _reload_all_configs(project_dir, "watchdog.enabled")

        assert err is not None
        assert "watchdog" in err
