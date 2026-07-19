"""Tests for sova.supervisor.watchdog -- agent watchdog."""

from __future__ import annotations

import asyncio
import contextlib
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
    def test_pipeline_not_adopted_within_timeout(self, _mock_alive: MagicMock) -> None:
        wd = _make_watchdog()
        run = _make_run(
            current_step="agent",
            started_at=datetime.now(timezone.utc) - timedelta(minutes=2),
        )
        findings = wd._detect_anomalies(run, datetime.now(timezone.utc), {})
        assert len(findings) == 0

    @patch("sova.supervisor.watchdog._is_process_alive", return_value=True)
    def test_pipeline_not_adopted_past_timeout(self, _mock_alive: MagicMock) -> None:
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
    def test_zombie_process(self, _mock_alive: MagicMock) -> None:
        wd = _make_watchdog()
        run = _make_run(pid=99999)
        findings = wd._detect_anomalies(run, datetime.now(timezone.utc), {})
        assert len(findings) == 1
        assert findings[0].signal == AnomalySignal.ZOMBIE_PROCESS
        assert findings[0].action == WatchdogAction.WARN

    @patch("sova.supervisor.watchdog._is_process_alive", return_value=True)
    def test_no_output_warn(self, _mock_alive: MagicMock) -> None:
        wd = _make_watchdog(no_output_warn_minutes=15, no_output_kill_minutes=25)
        now = datetime.now(timezone.utc)
        run = _make_run(started_at=now - timedelta(minutes=20))
        # No output lines recorded (empty dict), so baseline is started_at
        findings = wd._detect_anomalies(run, now, {})
        no_output = [f for f in findings if f.signal == AnomalySignal.NO_OUTPUT_WARN]
        assert len(no_output) == 1
        assert no_output[0].action == WatchdogAction.WARN

    @patch("sova.supervisor.watchdog._is_process_alive", return_value=True)
    def test_no_output_kill(self, _mock_alive: MagicMock) -> None:
        wd = _make_watchdog(no_output_warn_minutes=15, no_output_kill_minutes=25)
        now = datetime.now(timezone.utc)
        run = _make_run(started_at=now - timedelta(minutes=30))
        findings = wd._detect_anomalies(run, now, {})
        no_output_kill = [f for f in findings if f.signal == AnomalySignal.NO_OUTPUT_KILL]
        assert len(no_output_kill) == 1
        assert no_output_kill[0].action == WatchdogAction.KILL

    @patch("sova.supervisor.watchdog._is_process_alive", return_value=True)
    def test_no_output_uses_last_output_time(self, _mock_alive: MagicMock) -> None:
        wd = _make_watchdog(no_output_warn_minutes=15, no_output_kill_minutes=25)
        now = datetime.now(timezone.utc)
        # Run started 60 minutes ago, but last output was 5 minutes ago
        run = _make_run(run_id=1, started_at=now - timedelta(minutes=60))
        last_output_times = {1: now - timedelta(minutes=5)}
        findings = wd._detect_anomalies(run, now, last_output_times)
        no_output = [f for f in findings if "no_output" in f.signal.value]
        assert len(no_output) == 0

    @patch("sova.supervisor.watchdog._is_process_alive", return_value=True)
    def test_step_timeout_warn(self, _mock_alive: MagicMock) -> None:
        wd = _make_watchdog(step_warn_minutes=45)
        now = datetime.now(timezone.utc)
        run = _make_run(
            current_step="develop",
            started_at=now - timedelta(minutes=50),
        )
        # Pre-populate step tracking to simulate the step running for 50 minutes
        wd._step_started_at[(run.id, "develop")] = time.monotonic() - (50 * 60)
        findings = wd._detect_anomalies(run, now, {run.id: now - timedelta(minutes=1)})
        step_warn = [f for f in findings if f.signal == AnomalySignal.STEP_TIMEOUT_WARN]
        assert len(step_warn) == 1
        assert step_warn[0].action == WatchdogAction.WARN

    @patch("sova.supervisor.watchdog._is_process_alive", return_value=True)
    def test_no_anomalies_healthy_run(self, _mock_alive: MagicMock) -> None:
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

    @patch("sova.supervisor.watchdog.emit_safe")
    @patch("sova.supervisor.watchdog.get_session")
    async def test_kill_re_queries_status(self, mock_get_session: AsyncMock, _mock_emit: MagicMock) -> None:
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

    @patch("sova.supervisor.watchdog.emit_safe")
    @patch("sova.supervisor.watchdog.get_session")
    async def test_kill_calls_stop_agent(self, mock_get_session: AsyncMock, _mock_emit: MagicMock) -> None:
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

    @patch("sova.supervisor.watchdog.emit_safe")
    @patch("sova.supervisor.watchdog._is_process_alive", return_value=True)
    @patch("sova.supervisor.watchdog.get_session")
    async def test_scan_detects_no_output_warn(
        self, mock_get_session: AsyncMock, _mock_alive: MagicMock, _mock_emit: MagicMock
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

        async def _mock_execute(_stmt: object) -> MagicMock:
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
    async def test_start_creates_task(self) -> None:
        wd = _make_watchdog()
        task = wd.start()
        assert isinstance(task, asyncio.Task)
        assert wd._task is task
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def test_stop_cancels_task(self) -> None:
        wd = _make_watchdog()
        wd.start()
        await wd.stop()
        assert wd._task is None


# ---------------------------------------------------------------------------
# Config triple-registration test
# ---------------------------------------------------------------------------


class TestRunLoop:
    async def test_run_loop_initial_scan_error_is_caught(self) -> None:
        """Verify scan errors during initial startup scan are caught, not propagated."""
        wd = _make_watchdog(check_interval_seconds=3600)
        with patch.object(wd, "_scan_once", side_effect=RuntimeError("db error")):
            task = wd.start()
            # Give the loop time to run the initial scan and hit the error
            await asyncio.sleep(0.05)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def test_run_loop_cancellation_propagates(self) -> None:
        """CancelledError in the loop must propagate (not swallowed)."""
        wd = _make_watchdog(check_interval_seconds=3600)
        task = wd.start()
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_run_loop_runs_initial_scan_before_sleep(self) -> None:
        """Verify that _scan_once is called immediately, before the first sleep."""
        wd = _make_watchdog(check_interval_seconds=3600)
        scan_called = asyncio.Event()
        original_scan = wd._scan_once

        async def _track_scan() -> list[WatchdogFinding]:
            scan_called.set()
            return await original_scan()

        with (
            patch.object(wd, "_scan_once", side_effect=_track_scan),
            patch("sova.supervisor.watchdog.get_session") as mock_get_session,
        ):
            mock_session = AsyncMock()
            mock_result = MagicMock()
            mock_result.scalars.return_value.all.return_value = []
            mock_session.execute = AsyncMock(return_value=mock_result)
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=None)
            mock_get_session.return_value = mock_session

            task = wd.start()
            # Should fire within milliseconds, not after check_interval_seconds
            await asyncio.wait_for(scan_called.wait(), timeout=1.0)
            assert scan_called.is_set()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


class TestStopMethod:
    async def test_stop_when_no_task(self) -> None:
        """Calling stop() when no task is running should be a no-op."""
        wd = _make_watchdog()
        await wd.stop()  # should not raise
        assert wd._task is None

    async def test_stop_suppresses_cancelled_error(self) -> None:
        """stop() must suppress CancelledError from the awaited task."""
        wd = _make_watchdog(check_interval_seconds=3600)
        wd.start()
        assert wd._task is not None
        await wd.stop()
        assert wd._task is None


class TestKillAgentEdgeCases:
    @patch("sova.supervisor.watchdog.emit_safe")
    @patch("sova.supervisor.watchdog.get_session")
    async def test_kill_agent_run_not_found(self, mock_get_session: AsyncMock, _mock_emit: MagicMock) -> None:
        """If the run is gone when re-queried, kill should be a no-op."""
        wd = _make_watchdog()
        finding = WatchdogFinding(
            run_id=999,
            issue_number="42",
            signal=AnomalySignal.PIPELINE_NOT_ADOPTED,
            action=WatchdogAction.KILL,
            detail="test",
            metadata={},
        )

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.first.return_value = None  # run not found
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_get_session.return_value = mock_session

        with patch("sova.dashboard.services.agent_lifecycle.stop_agent", new_callable=AsyncMock) as mock_stop:
            await wd._execute_finding(finding)
            mock_stop.assert_not_called()

    @patch("sova.supervisor.watchdog.emit_safe")
    @patch("sova.supervisor.watchdog.get_session")
    async def test_kill_agent_stop_raises(self, mock_get_session: AsyncMock, _mock_emit: MagicMock) -> None:
        """If stop_agent raises, the error is logged but not propagated."""
        wd = _make_watchdog()
        finding = WatchdogFinding(
            run_id=1,
            issue_number="42",
            signal=AnomalySignal.NO_OUTPUT_KILL,
            action=WatchdogAction.KILL,
            detail="test",
            metadata={},
        )

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.first.return_value = ("running",)
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_get_session.return_value = mock_session

        with patch(
            "sova.dashboard.services.agent_lifecycle.stop_agent",
            new_callable=AsyncMock,
            side_effect=RuntimeError("stop failed"),
        ):
            await wd._execute_finding(finding)  # should not raise


class TestConfigValidation:
    def test_kill_must_exceed_warn(self) -> None:
        """no_output_kill_minutes must be greater than no_output_warn_minutes."""
        with pytest.raises(ValueError, match="no_output_kill_minutes"):
            WatchdogConfig(no_output_warn_minutes=20, no_output_kill_minutes=20)

    def test_kill_less_than_warn_rejected(self) -> None:
        with pytest.raises(ValueError, match="no_output_kill_minutes"):
            WatchdogConfig(no_output_warn_minutes=30, no_output_kill_minutes=15)

    def test_valid_thresholds_accepted(self) -> None:
        cfg = WatchdogConfig(no_output_warn_minutes=10, no_output_kill_minutes=20)
        assert cfg.no_output_warn_minutes == 10
        assert cfg.no_output_kill_minutes == 20


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
