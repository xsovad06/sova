"""Tests for sova.supervisor.watchdog -- agent watchdog."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.config.models import WatchdogConfig
from sova.supervisor.watchdog import (
    AgentWatchdog,
    AnomalySignal,
    WatchdogAction,
    WatchdogFinding,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: object) -> WatchdogConfig:
    defaults = {
        "enabled": True,
        "check_interval_seconds": 10,
        "pipeline_adopt_timeout_minutes": 5,
        "no_output_warn_minutes": 15,
        "no_output_kill_minutes": 25,
        "step_warn_minutes": 45,
        "cooldown_minutes": 10,
    }
    defaults.update(overrides)
    return WatchdogConfig(**defaults)


def _make_run(
    run_id: int = 1,
    status: str = "running",
    current_step: str | None = "develop",
    pid: int | None = 12345,
    started_at: datetime | None = None,
    issue_number: str | None = "42",
) -> MagicMock:
    run = MagicMock()
    run.id = run_id
    run.status = status
    run.current_step = current_step
    run.pid = pid
    run.started_at = started_at or datetime.now(timezone.utc)
    run.issue_number = issue_number
    return run


def _make_watchdog(**config_overrides: object) -> AgentWatchdog:
    cfg = _make_config(**config_overrides)
    return AgentWatchdog(config=cfg, project_dir=Path("/fake"))


# ---------------------------------------------------------------------------
# WatchdogConfig tests
# ---------------------------------------------------------------------------


class TestWatchdogConfig:
    def test_defaults(self) -> None:
        cfg = WatchdogConfig()
        assert cfg.enabled is False
        assert cfg.check_interval_seconds == 600
        assert cfg.pipeline_adopt_timeout_minutes == 5
        assert cfg.no_output_warn_minutes == 15
        assert cfg.no_output_kill_minutes == 25
        assert cfg.step_warn_minutes == 45
        assert cfg.cooldown_minutes == 10

    def test_custom_values(self) -> None:
        cfg = WatchdogConfig(
            enabled=True,
            check_interval_seconds=300,
            no_output_kill_minutes=30,
        )
        assert cfg.enabled is True
        assert cfg.check_interval_seconds == 300
        assert cfg.no_output_kill_minutes == 30


# ---------------------------------------------------------------------------
# WatchdogFinding dataclass tests
# ---------------------------------------------------------------------------


class TestWatchdogFinding:
    def test_frozen(self) -> None:
        f = WatchdogFinding(
            run_id=1,
            issue_number="42",
            signal=AnomalySignal.NO_OUTPUT_WARN,
            action=WatchdogAction.WARN,
            detail="test",
            metadata={},
        )
        assert f.run_id == 1
        with pytest.raises(AttributeError):
            f.run_id = 2  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Anomaly detection tests
# ---------------------------------------------------------------------------


class TestDetectAnomalies:
    @patch("sova.supervisor.watchdog._is_process_alive", return_value=True)
    def test_pipeline_not_adopted_within_timeout(self, mock_alive: MagicMock) -> None:
        wd = _make_watchdog()
        run = _make_run(
            current_step="agent",
            started_at=datetime.now(timezone.utc) - timedelta(minutes=2),
        )
        findings = wd._detect_anomalies(run, datetime.now(timezone.utc), {})
        assert len(findings) == 0

    @patch("sova.supervisor.watchdog._is_process_alive", return_value=True)
    def test_pipeline_not_adopted_past_timeout(self, mock_alive: MagicMock) -> None:
        wd = _make_watchdog(pipeline_adopt_timeout_minutes=5)
        run = _make_run(
            current_step="agent",
            started_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
        findings = wd._detect_anomalies(run, datetime.now(timezone.utc), {})
        assert len(findings) == 1
        assert findings[0].signal == AnomalySignal.PIPELINE_NOT_ADOPTED
        assert findings[0].action == WatchdogAction.KILL

    @patch("sova.supervisor.watchdog._is_process_alive", return_value=False)
    def test_zombie_process(self, mock_alive: MagicMock) -> None:
        wd = _make_watchdog()
        run = _make_run(pid=99999)
        findings = wd._detect_anomalies(run, datetime.now(timezone.utc), {})
        assert len(findings) == 1
        assert findings[0].signal == AnomalySignal.ZOMBIE_PROCESS
        assert findings[0].action == WatchdogAction.WARN

    @patch("sova.supervisor.watchdog._is_process_alive", return_value=True)
    def test_no_output_warn(self, mock_alive: MagicMock) -> None:
        wd = _make_watchdog(no_output_warn_minutes=15, no_output_kill_minutes=25)
        now = datetime.now(timezone.utc)
        run = _make_run(started_at=now - timedelta(minutes=20))
        # No output lines recorded (empty dict), so baseline is started_at
        findings = wd._detect_anomalies(run, now, {})
        no_output = [f for f in findings if f.signal == AnomalySignal.NO_OUTPUT_WARN]
        assert len(no_output) == 1
        assert no_output[0].action == WatchdogAction.WARN

    @patch("sova.supervisor.watchdog._is_process_alive", return_value=True)
    def test_no_output_kill(self, mock_alive: MagicMock) -> None:
        wd = _make_watchdog(no_output_warn_minutes=15, no_output_kill_minutes=25)
        now = datetime.now(timezone.utc)
        run = _make_run(started_at=now - timedelta(minutes=30))
        findings = wd._detect_anomalies(run, now, {})
        no_output_kill = [f for f in findings if f.signal == AnomalySignal.NO_OUTPUT_KILL]
        assert len(no_output_kill) == 1
        assert no_output_kill[0].action == WatchdogAction.KILL

    @patch("sova.supervisor.watchdog._is_process_alive", return_value=True)
    def test_no_output_uses_last_output_time(self, mock_alive: MagicMock) -> None:
        wd = _make_watchdog(no_output_warn_minutes=15, no_output_kill_minutes=25)
        now = datetime.now(timezone.utc)
        # Run started 60 minutes ago, but last output was 5 minutes ago
        run = _make_run(run_id=1, started_at=now - timedelta(minutes=60))
        last_output_times = {1: now - timedelta(minutes=5)}
        findings = wd._detect_anomalies(run, now, last_output_times)
        no_output = [f for f in findings if "no_output" in f.signal.value]
        assert len(no_output) == 0

    @patch("sova.supervisor.watchdog._is_process_alive", return_value=True)
    def test_step_timeout_warn(self, mock_alive: MagicMock) -> None:
        wd = _make_watchdog(step_warn_minutes=45)
        now = datetime.now(timezone.utc)
        run = _make_run(
            current_step="develop",
            started_at=now - timedelta(minutes=50),
        )
        findings = wd._detect_anomalies(run, now, {run.id: now - timedelta(minutes=1)})
        step_warn = [f for f in findings if f.signal == AnomalySignal.STEP_TIMEOUT_WARN]
        assert len(step_warn) == 1
        assert step_warn[0].action == WatchdogAction.WARN

    @patch("sova.supervisor.watchdog._is_process_alive", return_value=True)
    def test_no_anomalies_healthy_run(self, mock_alive: MagicMock) -> None:
        wd = _make_watchdog()
        now = datetime.now(timezone.utc)
        run = _make_run(
            current_step="develop",
            started_at=now - timedelta(minutes=2),
        )
        last_output_times = {run.id: now - timedelta(seconds=30)}
        findings = wd._detect_anomalies(run, now, last_output_times)
        assert len(findings) == 0


# ---------------------------------------------------------------------------
# Cooldown tests
# ---------------------------------------------------------------------------


class TestCooldowns:
    def test_cooldown_prevents_duplicate_alerts(self) -> None:
        wd = _make_watchdog(cooldown_minutes=10)
        finding = WatchdogFinding(
            run_id=1,
            issue_number="42",
            signal=AnomalySignal.NO_OUTPUT_WARN,
            action=WatchdogAction.WARN,
            detail="test",
            metadata={},
        )
        assert not wd._is_on_cooldown(finding)
        wd._record_cooldown(finding)
        assert wd._is_on_cooldown(finding)

    def test_different_signals_independent(self) -> None:
        wd = _make_watchdog(cooldown_minutes=10)
        warn = WatchdogFinding(
            run_id=1,
            issue_number="42",
            signal=AnomalySignal.NO_OUTPUT_WARN,
            action=WatchdogAction.WARN,
            detail="test",
            metadata={},
        )
        kill = WatchdogFinding(
            run_id=1,
            issue_number="42",
            signal=AnomalySignal.NO_OUTPUT_KILL,
            action=WatchdogAction.KILL,
            detail="test",
            metadata={},
        )
        wd._record_cooldown(warn)
        assert wd._is_on_cooldown(warn)
        assert not wd._is_on_cooldown(kill)

    def test_prune_removes_inactive_runs(self) -> None:
        wd = _make_watchdog()
        wd._cooldowns = {
            (1, "no_output_warn"): time.monotonic(),
            (2, "zombie_process"): time.monotonic(),
        }
        wd._prune_cooldowns({1})  # run 2 no longer active
        assert (1, "no_output_warn") in wd._cooldowns
        assert (2, "zombie_process") not in wd._cooldowns


# ---------------------------------------------------------------------------
# Execute finding tests
# ---------------------------------------------------------------------------


class TestExecuteFinding:
    @pytest.mark.asyncio
    @patch("sova.supervisor.watchdog.emit_safe")
    async def test_warn_only_emits_feed_event(self, mock_emit: MagicMock) -> None:
        wd = _make_watchdog()
        finding = WatchdogFinding(
            run_id=1,
            issue_number="42",
            signal=AnomalySignal.NO_OUTPUT_WARN,
            action=WatchdogAction.WARN,
            detail="slow run",
            metadata={"minutes_silent": 16.0},
        )
        await wd._execute_finding(finding)
        mock_emit.assert_called_once()
        call_kwargs = mock_emit.call_args
        assert "watchdog" in call_kwargs.kwargs["category"]

    @pytest.mark.asyncio
    @patch("sova.supervisor.watchdog.emit_safe")
    @patch("sova.supervisor.watchdog.get_session")
    async def test_kill_re_queries_status(self, mock_get_session: AsyncMock, mock_emit: MagicMock) -> None:
        wd = _make_watchdog()
        finding = WatchdogFinding(
            run_id=1,
            issue_number="42",
            signal=AnomalySignal.PIPELINE_NOT_ADOPTED,
            action=WatchdogAction.KILL,
            detail="pipeline bypass",
            metadata={},
        )

        # Simulate run already terminal when re-queried
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.first.return_value = ("done",)
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_get_session.return_value = mock_session

        with patch("sova.dashboard.services.agent_lifecycle.stop_agent", new_callable=AsyncMock) as mock_stop:
            await wd._execute_finding(finding)
            mock_stop.assert_not_called()  # should skip because status is terminal

    @pytest.mark.asyncio
    @patch("sova.supervisor.watchdog.emit_safe")
    @patch("sova.supervisor.watchdog.get_session")
    async def test_kill_calls_stop_agent(self, mock_get_session: AsyncMock, mock_emit: MagicMock) -> None:
        wd = _make_watchdog()
        finding = WatchdogFinding(
            run_id=1,
            issue_number="42",
            signal=AnomalySignal.PIPELINE_NOT_ADOPTED,
            action=WatchdogAction.KILL,
            detail="pipeline bypass",
            metadata={},
        )

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.first.return_value = ("running",)
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_get_session.return_value = mock_session

        with patch("sova.dashboard.services.agent_lifecycle.stop_agent", new_callable=AsyncMock) as mock_stop:
            await wd._execute_finding(finding)
            mock_stop.assert_called_once_with(run_id=1)


# ---------------------------------------------------------------------------
# Scan once integration tests
# ---------------------------------------------------------------------------


class TestScanOnce:
    @pytest.mark.asyncio
    @patch("sova.supervisor.watchdog.get_session")
    async def test_scan_no_active_runs(self, mock_get_session: AsyncMock) -> None:
        wd = _make_watchdog()

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_get_session.return_value = mock_session

        findings = await wd._scan_once()
        assert findings == []

    @pytest.mark.asyncio
    @patch("sova.supervisor.watchdog.emit_safe")
    @patch("sova.supervisor.watchdog._is_process_alive", return_value=True)
    @patch("sova.supervisor.watchdog.get_session")
    async def test_scan_detects_no_output_warn(
        self, mock_get_session: AsyncMock, mock_alive: MagicMock, mock_emit: MagicMock
    ) -> None:
        wd = _make_watchdog(no_output_warn_minutes=15, no_output_kill_minutes=25)
        now = datetime.now(timezone.utc)
        run = _make_run(
            run_id=1,
            started_at=now - timedelta(minutes=20),
            pid=12345,
        )

        # First call returns active runs, second returns output times
        mock_session = AsyncMock()
        call_count = 0

        async def _mock_execute(stmt):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.scalars.return_value.all.return_value = [run]
            else:
                result.all.return_value = []  # no output lines
            return result

        mock_session.execute = _mock_execute
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_get_session.return_value = mock_session

        findings = await wd._scan_once()
        assert len(findings) == 1
        assert findings[0].signal == AnomalySignal.NO_OUTPUT_WARN


# ---------------------------------------------------------------------------
# Start / stop lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_creates_task(self) -> None:
        wd = _make_watchdog()
        task = wd.start()
        assert isinstance(task, asyncio.Task)
        assert wd._task is task
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self) -> None:
        wd = _make_watchdog()
        wd.start()
        await wd.stop()
        assert wd._task is None


# ---------------------------------------------------------------------------
# Config triple-registration test
# ---------------------------------------------------------------------------


class TestConfigRegistration:
    def test_watchdog_in_project_config(self) -> None:
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        assert hasattr(cfg, "watchdog")
        assert cfg.watchdog.enabled is False

    def test_watchdog_in_nested_sections(self) -> None:
        from sova.config.loader import _NESTED_SECTIONS

        assert "watchdog" in _NESTED_SECTIONS

    def test_watchdog_in_settings_meta(self) -> None:
        from sova.dashboard.settings_meta import _META_BY_KEY, GROUP_ORDER, GROUPS

        assert "watchdog" in GROUPS
        assert "watchdog" in GROUP_ORDER
        assert "watchdog.enabled" in _META_BY_KEY

    def test_all_config_fields_have_meta(self) -> None:
        from sova.dashboard.settings_meta import _META_BY_KEY

        expected_keys = [
            "watchdog.enabled",
            "watchdog.check_interval_seconds",
            "watchdog.pipeline_adopt_timeout_minutes",
            "watchdog.no_output_warn_minutes",
            "watchdog.no_output_kill_minutes",
            "watchdog.step_warn_minutes",
            "watchdog.cooldown_minutes",
        ]
        for key in expected_keys:
            assert key in _META_BY_KEY, f"Missing settings meta for {key}"
