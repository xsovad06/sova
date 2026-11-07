"""Tests for sova.scheduler.service: systemd notify and watchdog, log reopen on SIGHUP."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from sova.scheduler import service


@pytest.fixture
def notify_socket(monkeypatch: pytest.MonkeyPatch):
    # A short path under /tmp: AF_UNIX paths are capped near 100 bytes, which pytest's tmp_path exceeds on macOS.
    with tempfile.TemporaryDirectory(dir="/tmp") as short_dir, socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        path = os.path.join(short_dir, "n.sock")
        sock.bind(path)
        sock.settimeout(2)
        monkeypatch.setenv("NOTIFY_SOCKET", path)
        yield sock


class TestSdNotify:
    def test_sends_the_state_to_the_notify_socket(self, notify_socket: socket.socket) -> None:
        assert service.sd_notify("READY=1") is True
        assert notify_socket.recv(64) == b"READY=1"

    def test_is_a_no_op_off_systemd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
        assert service.sd_notify("READY=1") is False

    def test_a_dead_socket_is_reported_not_raised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A short path: pytest's tmp_path exceeds the AF_UNIX limit on macOS, which would test "path too long".
        with tempfile.TemporaryDirectory(dir="/tmp") as short_dir:
            monkeypatch.setenv("NOTIFY_SOCKET", os.path.join(short_dir, "missing.sock"))
            assert service.sd_notify("READY=1") is False

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="abstract-namespace sockets are Linux-only")
    def test_an_abstract_namespace_socket_receives_the_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        name = f"sova-test-{os.getpid()}"
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.bind("\0" + name)
            sock.settimeout(2)
            monkeypatch.setenv("NOTIFY_SOCKET", "@" + name)
            assert service.sd_notify("READY=1") is True
            assert sock.recv(64) == b"READY=1"


class TestWatchdogInterval:
    def test_absent_means_no_ping_task(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WATCHDOG_USEC", raising=False)
        assert service.watchdog_ping_interval() is None

    def test_pings_at_a_quarter_of_the_configured_interval(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WATCHDOG_USEC", "60000000")
        monkeypatch.delenv("WATCHDOG_PID", raising=False)
        assert service.watchdog_ping_interval() == 60 * service._WATCHDOG_PING_FRACTION

    @pytest.mark.parametrize("raw", ["abc", "0", "-5"])
    def test_unusable_values_disable_the_watchdog(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("WATCHDOG_USEC", raw)
        monkeypatch.delenv("WATCHDOG_PID", raising=False)
        assert service.watchdog_ping_interval() is None

    def test_a_watchdog_meant_for_this_pid_is_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WATCHDOG_USEC", "60000000")
        monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))
        assert service.watchdog_ping_interval() == 15.0

    def test_a_watchdog_meant_for_another_pid_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WATCHDOG_USEC", "60000000")
        monkeypatch.setenv("WATCHDOG_PID", str(os.getpid() + 1))
        assert service.watchdog_ping_interval() is None


class TestWatchdogLoop:
    @staticmethod
    async def _run_verdicts(verdicts: list[bool | Exception]) -> list[str]:
        """Drive ``watchdog_loop`` (interval 0) through *verdicts* and return the pings sent per probe."""
        feed = iter(verdicts)
        exhausted = asyncio.Event()
        sent: list[str] = []

        async def is_healthy() -> bool:
            try:
                verdict = next(feed)
            except StopIteration:
                exhausted.set()
                await asyncio.sleep(3600)
                raise
            if isinstance(verdict, Exception):
                raise verdict
            return verdict

        with patch.object(service, "sd_notify", side_effect=lambda state: sent.append(state) or True):
            task = asyncio.create_task(service.watchdog_loop(0, is_healthy))
            await asyncio.wait_for(exhausted.wait(), timeout=5)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return sent

    async def test_a_single_failed_probe_does_not_withhold_the_ping(self) -> None:
        sent = await self._run_verdicts([True, False, True])
        assert sent == ["WATCHDOG=1"] * 3

    async def test_sustained_failures_withhold_the_ping_and_recovery_resumes_it(self) -> None:
        n = service._WATCHDOG_FAILURES_BEFORE_WITHHOLD
        sent = await self._run_verdicts([False] * n + [True])
        # the first n-1 failed beats still ping, the n-th is withheld, and the recovery pings again
        assert sent == ["WATCHDOG=1"] * (n - 1) + ["WATCHDOG=1"]
        assert len(sent) == n

    async def test_a_probe_error_counts_as_a_failure_and_keeps_looping(self) -> None:
        n = service._WATCHDOG_FAILURES_BEFORE_WITHHOLD
        sent = await self._run_verdicts([RuntimeError("probe broke")] * n + [True])
        assert len(sent) == n  # n-1 tolerated failures plus the recovery ping; the n-th was withheld

    async def test_one_slow_failed_probe_keeps_the_ping_gap_under_the_watchdog_timeout(self) -> None:
        """The probe's own duration is not added to the interval: one 2s failed probe never reaches WatchdogSec."""
        watchdog_sec = 60.0
        interval = watchdog_sec * service._WATCHDOG_PING_FRACTION
        clock = [0.0]
        pings: list[float] = []
        probes = iter([True, True, False, True, True, True, True])

        async def is_healthy() -> bool:
            verdict = next(probes)
            clock[0] += 2.0 if not verdict else 0.01  # the failed probe takes 2s to time out
            return verdict

        async def fake_sleep(delay: float) -> None:
            clock[0] += delay
            if len(pings) >= 6:
                raise asyncio.CancelledError

        with (
            patch.object(service.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(service.asyncio, "sleep", new=fake_sleep),
            patch.object(service, "sd_notify", side_effect=lambda _s: pings.append(clock[0]) or True),
        ):
            with pytest.raises(asyncio.CancelledError):
                await service.watchdog_loop(interval, is_healthy)

        gaps = [b - a for a, b in zip(pings, pings[1:])]
        assert gaps
        assert max(gaps) < watchdog_sec


class TestReopenLogs:
    def test_unset_env_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(service.STDOUT_LOG_ENV, raising=False)
        monkeypatch.delenv(service.STDERR_LOG_ENV, raising=False)
        with patch.object(service.os, "dup2") as dup2:
            service.reopen_logs()
        dup2.assert_not_called()

    def test_a_terminal_is_left_alone(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(service.STDOUT_LOG_ENV, str(tmp_path / "out.log"))
        monkeypatch.delenv(service.STDERR_LOG_ENV, raising=False)
        with patch.object(service.os, "isatty", return_value=True), patch.object(service.os, "dup2") as dup2:
            service.reopen_logs()
        dup2.assert_not_called()
        assert not (tmp_path / "out.log").exists()

    @pytest.mark.parametrize(("env_name", "fd"), [(service.STDOUT_LOG_ENV, 1), (service.STDERR_LOG_ENV, 2)])
    def test_rebinds_the_descriptor_to_the_fresh_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_name: str, fd: int
    ) -> None:
        log_path = tmp_path / "out.log"
        for name in (service.STDOUT_LOG_ENV, service.STDERR_LOG_ENV):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv(env_name, str(log_path))
        seen: list[tuple[int, int]] = []

        def check(src: int, dst: int) -> None:
            seen.append((dst, os.fstat(src).st_ino))

        with (
            patch.object(service.os, "isatty", return_value=False),
            patch.object(service.os, "dup2", side_effect=check),
        ):
            service.reopen_logs()

        assert seen == [(fd, log_path.stat().st_ino)]

    def test_both_streams_are_rebound_to_their_own_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        out_path, err_path = tmp_path / "out.log", tmp_path / "err.log"
        monkeypatch.setenv(service.STDOUT_LOG_ENV, str(out_path))
        monkeypatch.setenv(service.STDERR_LOG_ENV, str(err_path))
        seen: dict[int, int] = {}

        def check(src: int, dst: int) -> None:
            seen[dst] = os.fstat(src).st_ino

        with (
            patch.object(service.os, "isatty", return_value=False),
            patch.object(service.os, "dup2", side_effect=check),
        ):
            service.reopen_logs()

        assert seen == {1: out_path.stat().st_ino, 2: err_path.stat().st_ino}

    def test_an_unwritable_path_is_logged_not_raised(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        missing_dir = tmp_path / "missing-dir"
        monkeypatch.setenv(service.STDOUT_LOG_ENV, str(missing_dir / "out.log"))
        monkeypatch.delenv(service.STDERR_LOG_ENV, raising=False)
        with patch.object(service.os, "isatty", return_value=False), patch.object(service.os, "dup2") as dup2:
            service.reopen_logs()

        dup2.assert_not_called()
        assert not missing_dir.exists()


class TestSighupHandler:
    async def test_installs_and_removes_the_handler(self) -> None:
        loop = asyncio.get_running_loop()
        with patch.object(loop, "add_signal_handler") as add, patch.object(loop, "remove_signal_handler") as remove:
            assert service.install_sighup_handler(loop) is True
            service.remove_sighup_handler(loop)
        add.assert_called_once_with(signal.SIGHUP, service.reopen_logs)
        remove.assert_called_once_with(signal.SIGHUP)

    @pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="no SIGHUP on this platform")
    async def test_a_real_sighup_reopens_the_logs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(service.STDOUT_LOG_ENV, str(tmp_path / "out.log"))
        loop = asyncio.get_running_loop()
        previous = signal.getsignal(signal.SIGHUP)
        reopened = asyncio.Event()
        try:
            with patch.object(service, "reopen_logs", side_effect=reopened.set) as reopen:
                # install_sighup_handler binds the function at call time, so it sees the patched mock
                assert service.install_sighup_handler(loop) is True
                os.kill(os.getpid(), signal.SIGHUP)
                await asyncio.wait_for(reopened.wait(), timeout=5)
            reopen.assert_called_once_with()
        finally:
            service.remove_sighup_handler(loop)
            signal.signal(signal.SIGHUP, previous)

    @pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="no SIGHUP on this platform")
    async def test_removing_the_handler_leaves_sighup_ignored_not_terminating(self) -> None:
        loop = asyncio.get_running_loop()
        previous = signal.getsignal(signal.SIGHUP)
        try:
            service.install_sighup_handler(loop)
            service.remove_sighup_handler(loop)
            assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
        finally:
            signal.signal(signal.SIGHUP, previous)

    @pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="no SIGHUP on this platform")
    def test_ignore_sighup_sets_the_ignore_disposition(self) -> None:
        previous = signal.getsignal(signal.SIGHUP)
        try:
            service.ignore_sighup()
            assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
        finally:
            signal.signal(signal.SIGHUP, previous)

    async def test_a_platform_without_sighup_is_not_fatal(self) -> None:
        loop = asyncio.get_running_loop()
        with patch.object(service.signal, "SIGHUP", None, create=True):
            assert service.install_sighup_handler(loop) is False
            service.remove_sighup_handler(loop)

    @pytest.mark.parametrize("error", [NotImplementedError, RuntimeError, ValueError])
    async def test_install_sighup_handler_swallows_loop_errors(self, error: type[Exception]) -> None:
        loop = asyncio.get_running_loop()
        with patch.object(loop, "add_signal_handler", side_effect=error):
            assert service.install_sighup_handler(loop) is False

    @pytest.mark.parametrize("error", [NotImplementedError, RuntimeError, ValueError])
    async def test_remove_sighup_handler_swallows_loop_errors(self, error: type[Exception]) -> None:
        loop = asyncio.get_running_loop()
        with patch.object(loop, "remove_signal_handler", side_effect=error) as remove:
            service.remove_sighup_handler(loop)
        remove.assert_called_once()


class TestIsServiceManaged:
    def test_false_with_no_log_env_var(self, monkeypatch) -> None:
        monkeypatch.delenv("SOVA_SERVER_LOG", raising=False)
        monkeypatch.delenv("SOVA_SERVER_ERR_LOG", raising=False)
        assert service.is_service_managed() is False

    @pytest.mark.parametrize("name", ["SOVA_SERVER_LOG", "SOVA_SERVER_ERR_LOG"])
    def test_true_when_either_log_env_var_is_set(self, monkeypatch, name: str) -> None:
        monkeypatch.delenv("SOVA_SERVER_LOG", raising=False)
        monkeypatch.delenv("SOVA_SERVER_ERR_LOG", raising=False)
        monkeypatch.setenv(name, "/tmp/sova.log")
        assert service.is_service_managed() is True
