"""Service-manager integration for the server process: systemd notify and watchdog, log reopen on SIGHUP.

Everything here is a no-op off the service manager that drives it (no ``NOTIFY_SOCKET``, no
``WATCHDOG_USEC``, no log env var, no SIGHUP on Windows), so a plain ``sova server start`` is unchanged.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import sys
import time
from collections.abc import Awaitable, Callable

from sova.utils.logging import get_logger

log = get_logger(component="scheduler.service")

# Paths the service manager opened for stdout and stderr, exported by the unit or plist so SIGHUP can
# reopen them after a log rotation. Unset (journald, a terminal) means there is nothing to reopen.
STDOUT_LOG_ENV = "SOVA_SERVER_LOG"
STDERR_LOG_ENV = "SOVA_SERVER_ERR_LOG"

# systemd asks for a ping within WatchdogSec. Pinging at a quarter of it, on a fixed schedule, leaves room for
# a probe that takes seconds and for a failed beat without the gap between pings ever reaching WatchdogSec.
_WATCHDOG_PING_FRACTION = 0.25

# Consecutive failed probes before a ping is withheld. One slow or failed probe (a SQLite write stall, a
# backup) must not restart the server; only a sustained failure should.
_WATCHDOG_FAILURES_BEFORE_WITHHOLD = 3


def is_service_managed() -> bool:
    """True when a service manager owns this process's log files (a log env var is set).

    SIGHUP is only taken over in that case. An interactive server keeps the default action, so closing
    its terminal still stops it instead of leaving a server on a dead tty that holds the port.
    """
    return bool(os.environ.get(STDOUT_LOG_ENV) or os.environ.get(STDERR_LOG_ENV))


def agents_outlive_server() -> bool:
    """True when a service manager runs this server, so a shutdown leaves its agents for the next one to adopt.

    Under systemd (``NOTIFY_SOCKET``) or launchd (the log env vars) the service restarts the server and
    startup recovery adopts what is still running. An interactive server (Ctrl+C, ``sova dashboard``) has no
    such successor, so it stops its agents on the way out instead of leaving them running unmanaged.
    """
    return bool(os.environ.get("NOTIFY_SOCKET")) or is_service_managed()


def sd_notify(state: str) -> bool:
    """Send *state* (for example ``READY=1``) to systemd's notify socket. False when not under systemd."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]  # abstract-namespace socket
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(state.encode("utf-8"))
    except OSError:
        log.warning("service.notify_failed", state=state, exc_info=True)
        return False
    return True


def watchdog_ping_interval() -> float | None:
    """Seconds between watchdog pings, or None when systemd set no watchdog for this process."""
    raw = os.environ.get("WATCHDOG_USEC")
    if not raw:
        return None
    try:
        usec = int(raw)
    except ValueError:
        log.warning("service.watchdog_usec_invalid", value=raw)
        return None
    watchdog_pid = os.environ.get("WATCHDOG_PID")
    if watchdog_pid and watchdog_pid != str(os.getpid()):
        return None
    if usec <= 0:
        return None
    return usec / 1_000_000 * _WATCHDOG_PING_FRACTION


async def watchdog_loop(interval: float, is_healthy: Callable[[], Awaitable[bool]]) -> None:
    """Ping systemd every *interval* seconds while *is_healthy* holds; a stalled server stops pinging.

    Ticks fall on a fixed monotonic schedule, so the time a probe takes is not added to the gap between
    pings. A ping is withheld only after ``_WATCHDOG_FAILURES_BEFORE_WITHHOLD`` consecutive failed
    probes, so a single stalled probe cannot make systemd restart the server.

    The probe is an extension point: the shipped one (``SOVAServer._is_live``) is constant-true, so today
    liveness is detected only by this task failing to run on a wedged event loop, and the failure counter
    is not reached in production.
    """
    failures = 0
    next_tick = time.monotonic()
    while True:
        try:
            healthy = await is_healthy()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 (a probe failure counts as an unhealthy beat rather than ending the loop)
            log.warning("service.watchdog_probe_failed", exc_info=True)
            healthy = False
        failures = 0 if healthy else failures + 1
        if failures < _WATCHDOG_FAILURES_BEFORE_WITHHOLD:
            sd_notify("WATCHDOG=1")
            if not healthy:
                log.warning("service.watchdog_probe_unhealthy", consecutive_failures=failures)
        else:
            log.warning("service.watchdog_ping_withheld", consecutive_failures=failures)
        next_tick += interval
        now = time.monotonic()
        if next_tick < now:
            next_tick = now  # fell behind (a stalled loop): resume the schedule instead of bursting pings
        await asyncio.sleep(next_tick - now)


def reopen_logs() -> None:
    """Reopen the stdout and stderr log files named by the log env vars, after a rotation moved them.

    Rebinds descriptors 1 and 2 to the fresh paths (``dup2``), so every handler already writing to
    them follows. Each stream is skipped when its env var is unset or it is a terminal.
    """
    for env_name, fd in ((STDOUT_LOG_ENV, 1), (STDERR_LOG_ENV, 2)):
        path = os.environ.get(env_name)
        if not path:
            log.info("service.log_reopen_skipped", reason="env_unset", env=env_name)
            continue
        if os.isatty(fd):
            log.info("service.log_reopen_skipped", reason="terminal", fd=fd)
            continue
        try:
            for stream in (sys.stdout, sys.stderr):
                if stream is not None:
                    stream.flush()
            new_fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                os.dup2(new_fd, fd)
            finally:
                os.close(new_fd)
        except OSError:
            log.warning("service.log_reopen_failed", path=path, fd=fd, exc_info=True)
            continue
        log.info("service.log_reopened", path=path, fd=fd)


def install_sighup_handler(loop: asyncio.AbstractEventLoop) -> bool:
    """Reopen the log files on SIGHUP. False where the platform has no SIGHUP or loop support for it."""
    sighup = getattr(signal, "SIGHUP", None)
    if sighup is None:
        return False
    try:
        loop.add_signal_handler(sighup, reopen_logs)
    except (RuntimeError, ValueError):  # NotImplementedError is a RuntimeError
        log.info("service.sighup_unavailable", exc_info=True)
        return False
    return True


def ignore_sighup() -> None:
    """Make SIGHUP harmless: its default action terminates the process.

    Used before the event loop handler exists and after it is gone (startup and shutdown), so a log
    rotation landing in those windows is dropped instead of killing the server. A no-op where the
    platform has no SIGHUP or off the main thread (``signal.signal`` raises ``ValueError`` there).
    """
    sighup = getattr(signal, "SIGHUP", None)
    if sighup is None:
        return
    try:
        signal.signal(sighup, signal.SIG_IGN)
    except (ValueError, OSError):
        log.info("service.sighup_ignore_unavailable", exc_info=True)


def remove_sighup_handler(loop: asyncio.AbstractEventLoop) -> None:
    """Undo ``install_sighup_handler``, leaving SIGHUP ignored rather than at its terminating default."""
    sighup = getattr(signal, "SIGHUP", None)
    if sighup is None:
        return
    try:
        loop.remove_signal_handler(sighup)
    except (RuntimeError, ValueError):  # NotImplementedError is a RuntimeError
        pass
    ignore_sighup()
