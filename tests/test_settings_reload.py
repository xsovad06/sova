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

        with patch("sova.supervisor.daemon.SupervisorDaemon._poll_progression", new_callable=AsyncMock) as mock_prog:
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

    def test_llm_provider_key_returns_restart_required(self) -> None:
        from sova.dashboard.routers.settings import _RESTART_REQUIRED_PREFIXES

        key = "llm.provider"
        assert any(key == p or key.startswith(p) for p in _RESTART_REQUIRED_PREFIXES)

    def test_watch_key_returns_restart_required(self) -> None:
        from sova.dashboard.routers.settings import _RESTART_REQUIRED_PREFIXES

        key = "watch.poll_interval"
        assert any(key == p or key.startswith(p) for p in _RESTART_REQUIRED_PREFIXES)

    def test_database_url_returns_restart_required(self) -> None:
        from sova.dashboard.routers.settings import _RESTART_REQUIRED_PREFIXES

        key = "database_url"
        assert any(key == p or key.startswith(p) for p in _RESTART_REQUIRED_PREFIXES)


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
