"""Tests for settings-driven daemon lifecycle management.

Covers: supervisor daemon start/stop on enable toggle, oversight agent
start/stop on enable toggle, task_queue propagation, and restart_required
flag for non-hot-reloadable keys.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, TypeVar
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from sova.llm.models import LLMResult

_T = TypeVar("_T")

# Far longer than the wake-up bound below, so a sleep that is never interrupted
# fails with TimeoutError instead of passing slowly.
_LONG_SLEEP = 3600.0
_WAKE_TIMEOUT = 5.0


async def _assert_wakes_early(sleep: Coroutine[Any, Any, _T], trigger: Callable[[], object]) -> _T:
    """Start *sleep*, let it suspend on its wait, fire *trigger*, and require it to return promptly.

    The single ``asyncio.sleep(0)`` yield runs the sleeper task up to its first
    suspension point, so the trigger fires while the sleeper is genuinely waiting
    rather than before it starts. The bounded ``wait_for`` is the assertion that
    the trigger woke it: an unwoken sleep of ``_LONG_SLEEP`` raises TimeoutError.
    """
    sleeper = asyncio.create_task(sleep)
    try:
        await asyncio.sleep(0)
        assert not sleeper.done(), "sleeper returned before the trigger fired"
        trigger()
        return await asyncio.wait_for(sleeper, timeout=_WAKE_TIMEOUT)
    finally:
        if not sleeper.done():
            sleeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sleeper


class TestDaemonEnabledGuard:
    """SupervisorDaemon._poll_once skips when supervisor.enabled is False."""

    @pytest.mark.asyncio
    async def test_poll_skips_when_disabled(self, tmp_path: Path) -> None:
        from sova.supervisor.daemon import SupervisorDaemon

        cfg = MagicMock()
        cfg.supervisor.enabled = False
        cfg.supervisor.poll_interval_seconds = 60
        cfg.github_repo = "test/repo"

        daemon = SupervisorDaemon(
            config=cfg,
            project_dir=tmp_path,
            session_factory=MagicMock(),
        )

        with patch("sova.config.loader.load_config", return_value=cfg):
            result = await daemon._poll_once()
        assert "skipped" in result

    @pytest.mark.asyncio
    async def test_poll_proceeds_when_enabled(self, tmp_path: Path) -> None:
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
            project_dir=tmp_path,
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
    async def test_loop_exits_when_disabled(self, tmp_path: Path) -> None:
        from sova.oversight.agent import OversightAgent

        cfg = MagicMock()
        cfg.enabled = False
        cfg.wake_interval_minutes = 1

        agent = OversightAgent(config=cfg, project_dir=str(tmp_path))
        agent._task = asyncio.current_task()

        with patch.object(agent, "_reload_config", return_value=cfg):
            await agent._run_loop()

        assert agent._task is None
        assert not agent.running

    @pytest.mark.asyncio
    async def test_loop_runs_cycle_when_enabled(self, tmp_path: Path) -> None:
        from sova.oversight.agent import OversightAgent

        call_count = 0

        cfg_enabled = MagicMock()
        cfg_enabled.enabled = True
        cfg_enabled.wake_interval_minutes = 0.001

        cfg_disabled = MagicMock()
        cfg_disabled.enabled = False

        agent = OversightAgent(config=cfg_enabled, project_dir=str(tmp_path))

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
    async def test_stops_daemon_when_disabled(self, tmp_path: Path) -> None:
        from sova.dashboard.routers import supervisor as sup_mod
        from sova.dashboard.routers.settings import _reload_daemon_config

        daemon = MagicMock()
        daemon.running = True
        daemon.stop = AsyncMock()

        cfg = MagicMock()
        cfg.supervisor.enabled = False

        project_dir = tmp_path
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
    async def test_starts_daemon_when_enabled_and_none(self, tmp_path: Path) -> None:
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
                await _reload_daemon_config(tmp_path)

            mock_daemon_instance.start.assert_called_once()
        finally:
            sup_mod._daemon_registry = orig_registry

    @pytest.mark.asyncio
    async def test_hot_reloads_when_enabled_and_running(self, tmp_path: Path) -> None:
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
            await _reload_daemon_config(tmp_path)

        daemon.reload_config.assert_called_once_with(cfg)


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
    async def test_daemon_stop_failure_raises(self, tmp_path: Path) -> None:
        from sova.dashboard.routers import supervisor as sup_mod
        from sova.dashboard.routers.settings import _reload_daemon_config

        daemon = MagicMock()
        daemon.running = True
        daemon.stop = AsyncMock(side_effect=RuntimeError("stop failed"))

        cfg = MagicMock()
        cfg.supervisor.enabled = False

        project_dir = tmp_path
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
    async def test_daemon_start_failure_raises(self, tmp_path: Path) -> None:
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
                    await _reload_daemon_config(tmp_path)
        finally:
            sup_mod._daemon_registry = orig_registry


class TestLifecycleSerialization:
    """Concurrent lifecycle transitions are serialized per project."""

    @pytest.mark.asyncio
    async def test_concurrent_toggles_serialized(self, tmp_path: Path) -> None:
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

        project_dir = tmp_path
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

    def test_queue_change_reaches_daemon(self, tmp_path: Path) -> None:
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
            project_dir=tmp_path,
            session_factory=MagicMock(),
        )

        assert daemon._config.supervisor.task_queue == [1, 2, 3]
        daemon.reload_config(cfg2)
        assert daemon._config.supervisor.task_queue == [4, 5, 6]


class TestWatchdogReloadConfig:
    """AgentWatchdog.reload_config swaps config and wakes interruptible sleep."""

    def test_reload_swaps_config(self, tmp_path: Path) -> None:
        from sova.supervisor.watchdog import AgentWatchdog

        cfg1 = MagicMock()
        cfg1.check_interval_seconds = 30
        cfg2 = MagicMock()
        cfg2.check_interval_seconds = 10

        wd = AgentWatchdog(config=cfg1, project_dir=tmp_path)
        assert wd._config.check_interval_seconds == 30
        wd.reload_config(cfg2)
        assert wd._config.check_interval_seconds == 10
        assert wd._wake_event.is_set()

    @pytest.mark.asyncio
    async def test_interruptible_sleep_wakes_on_reload(self, tmp_path: Path) -> None:
        from sova.supervisor.watchdog import AgentWatchdog

        cfg = MagicMock()
        cfg.check_interval_seconds = 30

        wd = AgentWatchdog(config=cfg, project_dir=tmp_path)

        await _assert_wakes_early(wd._interruptible_sleep(_LONG_SLEEP), lambda: wd.reload_config(cfg))
        assert not wd._wake_event.is_set()


class TestPRMonitorReloadConfig:
    """PRMonitor.reload_config swaps config and wakes interruptible sleep."""

    def test_reload_swaps_config(self, tmp_path: Path) -> None:
        from sova.supervisor.pr_monitor import PRMonitor

        cfg1 = MagicMock()
        cfg1.poll_interval = 60
        cfg2 = MagicMock()
        cfg2.poll_interval = 30
        ncfg = MagicMock()

        mon = PRMonitor(
            project_dir=tmp_path,
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
    async def test_interruptible_sleep_wakes_on_reload(self, tmp_path: Path) -> None:
        from sova.supervisor.pr_monitor import PRMonitor

        cfg = MagicMock()
        cfg.poll_interval = 60
        ncfg = MagicMock()

        mon = PRMonitor(
            project_dir=tmp_path,
            monitor_config=cfg,
            notification_config=ncfg,
            repo="test/repo",
            github_user="testuser",
        )

        await _assert_wakes_early(mon._interruptible_sleep(_LONG_SLEEP), lambda: mon.reload_config(cfg, ncfg))
        assert not mon._wake_event.is_set()


class TestMergeQueueMonitorReloadConfig:
    """MergeQueueMonitor.reload_config swaps config and wakes sleep."""

    def test_reload_swaps_config(self, tmp_path: Path) -> None:
        from sova.dashboard.services.merge_queue_monitor import MergeQueueMonitor

        cfg1 = MagicMock()
        cfg1.merge_queue_poll_interval = 120
        cfg2 = MagicMock()
        cfg2.merge_queue_poll_interval = 60

        mon = MergeQueueMonitor(
            project_dir=tmp_path,
            repo="test/repo",
            github_user="testuser",
            integration_config=cfg1,
        )
        assert mon.integration_config.merge_queue_poll_interval == 120
        mon.reload_config(cfg2)
        assert mon.integration_config.merge_queue_poll_interval == 60
        assert mon._reload_event.is_set()

    @pytest.mark.asyncio
    async def test_interruptible_sleep_wakes_on_reload_not_stop(self, tmp_path: Path) -> None:
        from sova.dashboard.services.merge_queue_monitor import MergeQueueMonitor

        cfg = MagicMock()
        cfg.merge_queue_poll_interval = 120

        mon = MergeQueueMonitor(
            project_dir=tmp_path,
            repo="test/repo",
            github_user="testuser",
            integration_config=cfg,
        )

        should_stop = await _assert_wakes_early(mon._interruptible_sleep(_LONG_SLEEP), lambda: mon.reload_config(cfg))
        assert not should_stop

    @pytest.mark.asyncio
    async def test_interruptible_sleep_exits_on_stop(self, tmp_path: Path) -> None:
        from sova.dashboard.services.merge_queue_monitor import MergeQueueMonitor

        cfg = MagicMock()
        cfg.merge_queue_poll_interval = 120

        mon = MergeQueueMonitor(
            project_dir=tmp_path,
            repo="test/repo",
            github_user="testuser",
            integration_config=cfg,
        )

        should_stop = await _assert_wakes_early(mon._interruptible_sleep(_LONG_SLEEP), mon.stop)
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

        await _assert_wakes_early(
            loop_instance._interruptible_sleep(_LONG_SLEEP), lambda: loop_instance.reload_config(cfg)
        )
        assert not loop_instance._wake_event.is_set()


class TestOversightAgentReloadConfig:
    """OversightAgent.reload_config accepts ProjectConfig and wakes sleep."""

    def test_reload_with_project_config(self, tmp_path: Path) -> None:
        from sova.oversight.agent import OversightAgent

        cfg = MagicMock()
        cfg.enabled = True
        cfg.wake_interval_minutes = 5

        project_cfg = MagicMock()
        project_cfg.oversight = cfg

        agent = OversightAgent(config=MagicMock(), project_dir=str(tmp_path))
        agent.reload_config(project_cfg)
        assert agent._config is cfg
        assert agent._wake_event.is_set()

    def test_reload_without_config_falls_back_to_disk(self, tmp_path: Path) -> None:
        from sova.oversight.agent import OversightAgent

        agent = OversightAgent(config=MagicMock(), project_dir=str(tmp_path))
        with patch.object(agent, "_reload_config") as mock_disk:
            agent.reload_config()
            mock_disk.assert_called_once()

    @pytest.mark.asyncio
    async def test_interruptible_sleep_wakes_on_reload(self, tmp_path: Path) -> None:
        from sova.oversight.agent import OversightAgent

        agent = OversightAgent(config=MagicMock(), project_dir=str(tmp_path))

        project_cfg = MagicMock()
        await _assert_wakes_early(agent._interruptible_sleep(_LONG_SLEEP), lambda: agent.reload_config(project_cfg))
        assert agent._config is project_cfg.oversight
        assert not agent._wake_event.is_set()


class TestSingletonReload:
    """reload_provider and reload_runtime swap global singletons."""

    def test_reload_provider(self) -> None:
        from sova.config.models import LLMConfig, ProjectConfig
        from sova.llm.client import get_provider, reload_provider, reset_provider

        reset_provider()
        old = get_provider()

        cfg = ProjectConfig(llm=LLMConfig(model="test-model"))

        with patch("sova.llm.client.create_provider") as mock_create:
            mock_provider = MagicMock()
            mock_create.return_value = mock_provider
            reload_provider(cfg)

        new = get_provider()
        assert new is mock_provider
        assert new is not old
        # The whole section, not a kwarg subset: a hand-picked forward is how a
        # newly added field stops applying after a hot-reload (R12).
        assert mock_create.call_args.args[0] is cfg.llm

        reset_provider()

    @pytest.mark.asyncio
    async def test_llm_hot_reload_honors_a_changed_alias_map(self, tmp_path: Path) -> None:
        """A settings save that only changes model_aliases must reach the new provider."""
        from sova.config.models import AgentConfig, LLMConfig, ProjectConfig
        from sova.dashboard.routers.settings import _dispatch_config_reload
        from sova.llm import client

        client.reset_provider()
        cfg = ProjectConfig(
            llm=LLMConfig(provider="litellm", model_aliases={"smart": "ollama/llama3.1:70b"}),
            agent=AgentConfig(model="smart"),
        )

        provider = MagicMock()
        provider.normalize_model_name = lambda m: m
        provider.invoke = AsyncMock(return_value=LLMResult(text="ok", model="ollama/llama3.1:70b"))
        with patch("sova.llm.client.create_provider", return_value=provider) as mock_create:
            await _dispatch_config_reload("llm", cfg, {}, tmp_path)

        assert mock_create.call_args.args[0].model_aliases == {"smart": "ollama/llama3.1:70b"}
        assert client.get_provider() is provider

        with patch.object(client, "_try_load_config", return_value=cfg):
            await client.invoke("prompt", model="smart", timeout=900.0)
        assert provider.invoke.call_args.kwargs["model"] == "ollama/llama3.1:70b"

        client.reset_provider()

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
        mock_create.assert_called_once_with("claude-code", codex=cfg.codex)

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
        assert _match_reload_target("memory_guard.runtime_action") == "watchdog"

    def test_runtime_prefix(self) -> None:
        from sova.dashboard.routers.settings import _match_reload_target

        assert _match_reload_target("agent.runtime") == "runtime"
        assert _match_reload_target("codex.model") == "runtime"
        assert _match_reload_target("codex.sandbox") == "runtime"

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


class TestInterruptibleSleepTimeout:
    """Interruptible sleep reaches timeout when no event fires."""

    @pytest.mark.asyncio
    async def test_watchdog_sleep_timeout_path(self, tmp_path: Path) -> None:
        from sova.supervisor.watchdog import AgentWatchdog

        wd = AgentWatchdog(config=MagicMock(), project_dir=tmp_path)
        import time

        t0 = time.monotonic()
        await wd._interruptible_sleep(0.05)
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.04

    @pytest.mark.asyncio
    async def test_pr_monitor_sleep_timeout_path(self, tmp_path: Path) -> None:
        from sova.supervisor.pr_monitor import PRMonitor

        mon = PRMonitor(
            project_dir=tmp_path,
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

    def test_reload_sets_notification_config(self, tmp_path: Path) -> None:
        from sova.dashboard.services.merge_queue_monitor import MergeQueueMonitor

        ncfg = MagicMock()
        mon = MergeQueueMonitor(
            project_dir=tmp_path,
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
    async def test_watchdog_none_is_noop(self, tmp_path: Path) -> None:
        from sova.dashboard.routers.settings import _dispatch_config_reload

        cfg = MagicMock()
        await _dispatch_config_reload("watchdog", cfg, {}, tmp_path)

    @pytest.mark.asyncio
    async def test_empty_monitor_list_is_noop(self, tmp_path: Path) -> None:
        from sova.dashboard.routers.settings import _dispatch_config_reload

        cfg = MagicMock()
        await _dispatch_config_reload("pr_monitor", cfg, {"pr_monitors": []}, tmp_path)
        await _dispatch_config_reload("integration", cfg, {"merge_queue_monitors": []}, tmp_path)

    @pytest.mark.asyncio
    async def test_quota_change_starts_a_loop_when_none_is_registered(self, tmp_path: Path) -> None:
        """Enabling the quota after server start must start a loop; there is none to reload."""
        from sova.dashboard.routers.settings import _dispatch_config_reload

        cfg = MagicMock()
        with patch("sova.dashboard.app.ensure_pr_throttle_loop", new_callable=AsyncMock) as ensure:
            await _dispatch_config_reload("coderabbit_quota", cfg, {}, tmp_path)
        ensure.assert_awaited_once_with(tmp_path, cfg)

    @pytest.mark.asyncio
    async def test_quota_change_reloads_a_registered_loop_before_ensuring(self, tmp_path: Path) -> None:
        """The registered loop must take the new limits first: `ensure` returns early once a loop is registered."""
        from sova.dashboard.routers.settings import _dispatch_config_reload

        cfg = MagicMock()
        throttle = MagicMock()
        parent = MagicMock()
        parent.attach_mock(throttle.reload_config, "reload")
        with patch("sova.dashboard.app.ensure_pr_throttle_loop", new_callable=AsyncMock) as ensure:
            parent.attach_mock(ensure, "ensure")
            await _dispatch_config_reload("coderabbit_quota", cfg, {"pr_throttles": [throttle]}, tmp_path)
        assert parent.mock_calls == [call.reload(cfg.coderabbit_quota), call.ensure(tmp_path, cfg)]

    @pytest.mark.asyncio
    async def test_quota_change_in_multi_project_root_starts_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A root-level request in multi-project mode names no project, so it cannot start a loop.

        The working directory is a real directory here on purpose: starting a loop for it is the bug.
        Reloading is separate: it acts on the loops the caller found, see the `_reload_all_configs` tests.
        """
        from sova.dashboard.routers.settings import _dispatch_config_reload

        monkeypatch.chdir(tmp_path)
        cfg = MagicMock()
        with patch("sova.dashboard.app.ensure_pr_throttle_loop", new_callable=AsyncMock) as ensure:
            await _dispatch_config_reload("coderabbit_quota", cfg, {}, None)
        ensure.assert_not_awaited()


class TestReloadAllConfigs:
    """_reload_all_configs dispatches to correct daemons based on key prefix."""

    @pytest.mark.asyncio
    async def test_watchdog_key_dispatches_to_watchdog(self, tmp_path: Path) -> None:
        from sova.dashboard.routers.settings import _reload_all_configs

        cfg = MagicMock()
        wd = MagicMock()

        project_dir = tmp_path
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
        wd.reload_config.assert_called_once_with(cfg.watchdog, cfg.memory_guard)

    @pytest.mark.asyncio
    async def test_pr_monitor_key_dispatches_to_monitors(self, tmp_path: Path) -> None:
        from sova.dashboard.routers.settings import _reload_all_configs

        cfg = MagicMock()
        mon = MagicMock()

        project_dir = tmp_path
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
    async def test_coderabbit_key_dispatches_to_throttle(self, tmp_path: Path) -> None:
        from sova.dashboard.routers.settings import _reload_all_configs

        cfg = MagicMock()
        throttle = MagicMock()

        project_dir = tmp_path
        resolved_key = str(project_dir.resolve())

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch(
                "sova.dashboard.app.get_daemon_components",
                return_value={resolved_key: {"pr_throttles": [throttle]}},
            ),
            patch("sova.dashboard.app.ensure_pr_throttle_loop", new_callable=AsyncMock) as ensure,
        ):
            err = await _reload_all_configs(project_dir, "coderabbit_quota.reviews_per_hour")

        assert err is None
        throttle.reload_config.assert_called_once_with(cfg.coderabbit_quota)
        ensure.assert_awaited_once_with(project_dir, cfg)

    @pytest.mark.asyncio
    async def test_coderabbit_key_without_a_scope_follows_the_single_project_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`create_app(project_dir)` can differ from the working directory; the lifespan keyed on the former.

        The config, the daemon components lookup, the reload and the start must all use that directory.
        A lookup under the working directory finds no throttle, so the running loop would keep stale limits.
        """
        from sova.dashboard.routers.settings import _reload_all_configs

        app_project = tmp_path / "app-project"
        app_project.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        cfg = MagicMock()
        throttle = MagicMock()

        with (
            patch("sova.dashboard.routers.settings.get_default_project_dir", return_value=app_project),
            patch("sova.config.loader.load_config", return_value=cfg) as load,
            patch(
                "sova.dashboard.app.get_daemon_components",
                return_value={str(app_project.resolve()): {"pr_throttles": [throttle]}},
            ),
            patch("sova.dashboard.app.ensure_pr_throttle_loop", new_callable=AsyncMock) as ensure,
        ):
            err = await _reload_all_configs(None, "coderabbit_quota.reviews_per_hour")

        assert err is None
        load.assert_called_once_with(app_project)
        throttle.reload_config.assert_called_once_with(cfg.coderabbit_quota)
        ensure.assert_awaited_once_with(app_project, cfg)

    @pytest.mark.asyncio
    async def test_coderabbit_key_in_multi_project_root_reloads_the_cwd_loop_and_starts_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No scope and no single-project default: the request falls back to the working directory's config.

        Like every other reload target, it reloads a loop registered under the working directory with the
        config it just loaded, since that is the config the request wrote. It starts no loop: the root
        config decides nothing in multi-project mode, and the working directory was never a named project.
        """
        from sova.dashboard.routers.settings import _reload_all_configs

        monkeypatch.chdir(tmp_path)
        cfg = MagicMock()
        throttle = MagicMock()

        with (
            patch("sova.dashboard.routers.settings.get_default_project_dir", return_value=None),
            patch("sova.config.loader.load_config", return_value=cfg) as load,
            patch(
                "sova.dashboard.app.get_daemon_components",
                return_value={str(tmp_path.resolve()): {"pr_throttles": [throttle]}},
            ),
            patch("sova.dashboard.app.ensure_pr_throttle_loop", new_callable=AsyncMock) as ensure,
        ):
            err = await _reload_all_configs(None, "coderabbit_quota.reviews_per_hour")

        assert err is None
        load.assert_called_once_with(None)
        throttle.reload_config.assert_called_once_with(cfg.coderabbit_quota)
        ensure.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_coderabbit_key_in_a_scoped_request_follows_the_request_scope_not_the_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A `/p/{slug}/` request resolves its project from the contextvar, ahead of the startup default.

        The default names some other project in multi-project mode; resolving it first would start or
        reload the wrong project's loop.
        """
        from sova.config.context import clear_project_context, set_project_context
        from sova.dashboard.routers.settings import _reload_all_configs

        scoped = tmp_path / "scoped"
        scoped.mkdir()
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.chdir(other)
        cfg = MagicMock()
        scoped_throttle = MagicMock()
        other_throttle = MagicMock()
        components = {
            str(scoped.resolve()): {"pr_throttles": [scoped_throttle]},
            str(other.resolve()): {"pr_throttles": [other_throttle]},
        }

        set_project_context(scoped, "scoped")
        try:
            with (
                patch("sova.dashboard.routers.settings.get_default_project_dir", return_value=other),
                patch("sova.config.loader.load_config", return_value=cfg) as load,
                patch("sova.dashboard.app.get_daemon_components", return_value=components),
                patch("sova.dashboard.app.ensure_pr_throttle_loop", new_callable=AsyncMock) as ensure,
            ):
                err = await _reload_all_configs(None, "coderabbit_quota.enabled")
        finally:
            clear_project_context()

        assert err is None
        load.assert_called_once_with(scoped)
        scoped_throttle.reload_config.assert_called_once_with(cfg.coderabbit_quota)
        other_throttle.reload_config.assert_not_called()
        ensure.assert_awaited_once_with(scoped, cfg)

    @pytest.mark.asyncio
    async def test_a_saved_quota_setting_is_visible_to_the_reload_that_follows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Real config load and a real project database: the write and the reload must read the same store.

        The write has no project argument and lands in the database `init_db(resolved)` opened at startup;
        the reload reads the project the default names. The app project and the working directory differ
        on purpose: a reload that fell back to `load_config(project_dir)` would read the working
        directory's store, find `enabled=False`, and start no loop.
        """
        from sova.dashboard.routers.settings import _reload_all_configs
        from sova.dashboard.services import settings_service
        from sova.db.session import close_db, init_db

        app_project = tmp_path / "app-project"
        app_project.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.delenv("SOVA_DATABASE_URL", raising=False)
        monkeypatch.chdir(elsewhere)
        await init_db(app_project)
        try:
            with (
                patch("sova.dashboard.routers.settings.get_default_project_dir", return_value=app_project),
                patch("sova.dashboard.app.ensure_pr_throttle_loop", new_callable=AsyncMock) as ensure,
            ):
                saved = await settings_service.update_config(None, key="coderabbit_quota.enabled", value="true")
                err = await _reload_all_configs(None, "coderabbit_quota.enabled")
        finally:
            await close_db()

        assert saved.get("status") == "ok"
        assert err is None
        ensure.assert_awaited_once()
        started_dir, started_cfg = ensure.await_args.args
        assert started_dir == app_project
        assert started_cfg.coderabbit_quota.enabled is True

    @pytest.mark.asyncio
    async def test_coderabbit_reload_still_runs_when_starting_the_loop_fails(self, tmp_path: Path) -> None:
        """A start that raises (a locked database during recovery) is reported, but only after the reload.

        Awaiting the start first would let a failed start keep a registered loop on its old limits.
        """
        from sova.dashboard.routers.settings import _reload_all_configs

        cfg = MagicMock()
        throttle = MagicMock()

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch(
                "sova.dashboard.app.get_daemon_components",
                return_value={str(tmp_path.resolve()): {"pr_throttles": [throttle]}},
            ),
            patch(
                "sova.dashboard.app.ensure_pr_throttle_loop",
                new_callable=AsyncMock,
                side_effect=RuntimeError("database is locked"),
            ),
        ):
            err = await _reload_all_configs(tmp_path, "coderabbit_quota.reviews_per_hour")

        assert err is not None
        assert "coderabbit_quota" in err
        throttle.reload_config.assert_called_once_with(cfg.coderabbit_quota)

    @pytest.mark.asyncio
    async def test_supervisor_key_without_a_scope_follows_the_single_project_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every unscoped target resolves the project the way the lifespan keyed it, not by working directory.

        For the supervisor that is the directory `_reload_daemon_config` is handed: it keys the lifecycle
        lock, the daemon lookup and where a new daemon is registered.
        """
        from sova.dashboard.routers.settings import _reload_all_configs

        app_project = tmp_path / "app-project"
        app_project.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        cfg = MagicMock()

        with (
            patch("sova.dashboard.routers.settings.get_default_project_dir", return_value=app_project),
            patch("sova.config.loader.load_config", return_value=cfg) as load,
            patch("sova.dashboard.app.get_daemon_components", return_value={}),
            patch("sova.dashboard.routers.settings._reload_daemon_config", new_callable=AsyncMock) as reload_daemon,
        ):
            err = await _reload_all_configs(None, "supervisor.enabled")

        assert err is None
        load.assert_called_once_with(app_project)
        reload_daemon.assert_awaited_once_with(app_project)

    @pytest.mark.asyncio
    async def test_watchdog_key_without_a_scope_looks_up_components_under_the_single_project_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sova.dashboard.routers.settings import _reload_all_configs

        app_project = tmp_path / "app-project"
        app_project.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        cfg = MagicMock()
        app_watchdog = MagicMock()
        cwd_watchdog = MagicMock()
        components = {
            str(app_project.resolve()): {"watchdog": app_watchdog},
            str(elsewhere.resolve()): {"watchdog": cwd_watchdog},
        }

        with (
            patch("sova.dashboard.routers.settings.get_default_project_dir", return_value=app_project),
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("sova.dashboard.app.get_daemon_components", return_value=components),
        ):
            err = await _reload_all_configs(None, "watchdog.check_interval_seconds")

        assert err is None
        app_watchdog.reload_config.assert_called_once_with(cfg.watchdog, cfg.memory_guard)
        cwd_watchdog.reload_config.assert_not_called()

    @pytest.mark.asyncio
    async def test_integration_key_dispatches_to_merge_queue(self, tmp_path: Path) -> None:
        from sova.dashboard.routers.settings import _reload_all_configs

        cfg = MagicMock()
        mqm = MagicMock()

        project_dir = tmp_path
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
    async def test_llm_key_reloads_provider(self, tmp_path: Path) -> None:
        from sova.dashboard.routers.settings import _reload_all_configs

        cfg = MagicMock()

        project_dir = tmp_path

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("sova.dashboard.app.get_daemon_components", return_value={}),
            patch("sova.llm.client.reload_provider") as mock_reload,
        ):
            err = await _reload_all_configs(project_dir, "llm.model")

        assert err is None
        mock_reload.assert_called_once_with(cfg, project_dir)

    @pytest.mark.asyncio
    async def test_runtime_key_reloads_runtime(self, tmp_path: Path) -> None:
        from sova.dashboard.routers.settings import _reload_all_configs

        cfg = MagicMock()

        project_dir = tmp_path

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("sova.dashboard.app.get_daemon_components", return_value={}),
            patch("sova.ipc.runtime.reload_runtime") as mock_reload,
        ):
            err = await _reload_all_configs(project_dir, "agent.runtime")

        assert err is None
        mock_reload.assert_called_once_with(cfg)

    @pytest.mark.asyncio
    async def test_supervisor_key_dispatches_to_daemon_config(self, tmp_path: Path) -> None:
        from sova.dashboard.routers.settings import _reload_all_configs

        cfg = MagicMock()
        project_dir = tmp_path

        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("sova.dashboard.app.get_daemon_components", return_value={}),
            patch("sova.dashboard.routers.settings._reload_daemon_config", new_callable=AsyncMock) as mock_reload,
        ):
            err = await _reload_all_configs(project_dir, "supervisor.enabled")

        assert err is None
        mock_reload.assert_called_once_with(project_dir)

    @pytest.mark.asyncio
    async def test_oversight_key_dispatches_to_oversight_reload(self, tmp_path: Path) -> None:
        from sova.dashboard.routers.settings import _reload_all_configs

        cfg = MagicMock()
        project_dir = tmp_path

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
    async def test_unmatched_key_returns_none(self, tmp_path: Path) -> None:
        from sova.dashboard.routers.settings import _reload_all_configs

        project_dir = tmp_path

        with patch("sova.dashboard.app.get_daemon_components", return_value={}):
            err = await _reload_all_configs(project_dir, "some_unknown_key")

        assert err is None

    @pytest.mark.asyncio
    async def test_load_config_failure_returns_error_message(self, tmp_path: Path) -> None:
        from sova.dashboard.routers.settings import _reload_all_configs

        project_dir = tmp_path

        with patch("sova.config.loader.load_config", side_effect=RuntimeError("config parse error")):
            err = await _reload_all_configs(project_dir, "watchdog.enabled")

        assert err is not None
        assert "watchdog" in err

    @pytest.mark.asyncio
    async def test_reload_failure_returns_error_message(self, tmp_path: Path) -> None:
        from sova.dashboard.routers.settings import _reload_all_configs

        cfg = MagicMock()
        wd = MagicMock()
        wd.reload_config.side_effect = RuntimeError("boom")

        project_dir = tmp_path
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
