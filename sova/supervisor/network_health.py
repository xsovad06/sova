"""Machine-wide network connectivity state tracker.

Sibling of ``github_quota.py`` and deliberately the same shape: an in-memory
singleton, no DB persistence, feed events on state transitions. Two differences
from that module, both load-bearing:

* **Not identity-keyed.** A rate limit belongs to a GitHub account; reachability
  belongs to the machine, so there is one tracker rather than one per identity.
* **It answers "is the network up right now", never "was this run
  network-caused."** The failures that motivated this module happened inside
  agent *subprocesses*, whose own in-memory tracker dies with the child, so this
  tracker never observes them. Retry eligibility must therefore be decided from
  persisted error text via ``sova.utils.network.looks_like_network_outage``.
  Using this tracker for that would silently under-detect.
"""

from __future__ import annotations

import asyncio
import socket
import time
from dataclasses import dataclass

from sova.utils.logging import get_logger

log = get_logger(component="supervisor.network_health")

# Consecutive reactive failures before the network is declared down. One failed
# gh call is a blip and must not flap the operator-facing banner; a failed probe
# is decisive on its own (see probe_connectivity).
_FAILURE_THRESHOLD = 2

# Hosts the probe resolves. Two independent providers so one vendor's DNS or
# edge problem is not reported as the operator's internet being down.
_PROBE_HOSTS: tuple[str, ...] = ("api.github.com", "api.anthropic.com")
_PROBE_TIMEOUT_SECONDS = 3.0

# Minimum seconds between feed emissions of the same state, mirroring
# GitHubQuotaTracker's own throttle.
_EMIT_THROTTLE_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class ConnectivityStatus:
    """Snapshot of the current network reachability state."""

    is_down: bool
    down_for_seconds: float
    healthy_for_seconds: float
    consecutive_failures: int
    last_source: str


class ConnectivityTracker:
    """Tracks network reachability and provides a spawn-gating check."""

    def __init__(self, failure_threshold: int = _FAILURE_THRESHOLD) -> None:
        self._failure_threshold = failure_threshold
        self._consecutive_failures = 0
        self._is_down = False
        self._down_since: float | None = None
        self._healthy_since: float | None = time.monotonic()
        self._last_source = ""
        self._last_emit_at: float | None = None

    def record_failure(self, source: str, *, decisive: bool = False) -> None:
        """Record a failure attributable to the network being unreachable.

        ``decisive`` skips the consecutive-failure threshold, for evidence that
        is already conclusive on its own (the probe failing to resolve every
        host it tries).
        """
        self._consecutive_failures += 1
        self._last_source = source

        if self._is_down:
            return
        if not decisive and self._consecutive_failures < self._failure_threshold:
            log.debug(
                "connectivity.failure_below_threshold",
                source=source,
                failures=self._consecutive_failures,
                threshold=self._failure_threshold,
            )
            return

        self._is_down = True
        self._down_since = time.monotonic()
        self._healthy_since = None
        log.warning("connectivity.down", source=source, failures=self._consecutive_failures)
        self._emit("Network connection lost", source, down=True)

    def record_success(self, source: str) -> None:
        """Record any successful network call. Clears the failure streak."""
        self._consecutive_failures = 0
        self._last_source = source

        if not self._is_down:
            if self._healthy_since is None:
                self._healthy_since = time.monotonic()
            return

        down_for = self.get_status().down_for_seconds
        self._is_down = False
        self._down_since = None
        self._healthy_since = time.monotonic()
        log.info("connectivity.recovered", source=source, down_for_seconds=round(down_for))
        self._emit("Network connection restored", source, down=False)

    def is_down(self) -> bool:
        return self._is_down

    def healthy_for_seconds(self) -> float:
        """Seconds the network has been continuously healthy, 0.0 while down.

        The self-heal pass uses this as a flap guard: resuming work the instant
        a flapping connection first answers would just burn the retry budget.
        """
        if self._is_down or self._healthy_since is None:
            return 0.0
        return time.monotonic() - self._healthy_since

    def get_status(self) -> ConnectivityStatus:
        now = time.monotonic()
        down_for = now - self._down_since if self._down_since is not None else 0.0
        return ConnectivityStatus(
            is_down=self._is_down,
            down_for_seconds=down_for,
            healthy_for_seconds=self.healthy_for_seconds(),
            consecutive_failures=self._consecutive_failures,
            last_source=self._last_source,
        )

    def _emit(self, title: str, source: str, *, down: bool) -> None:
        now = time.monotonic()
        if self._last_emit_at is not None and now - self._last_emit_at < _EMIT_THROTTLE_SECONDS:
            return
        self._last_emit_at = now
        try:
            from sova.dashboard.services.feed_service import FeedEventSeverity, emit_safe

            detail = (
                "Agent spawning is paused. Interrupted runs resume automatically once the connection returns."
                if down
                else "GitHub and the LLM provider are reachable again."
            )
            emit_safe(
                title,
                severity=FeedEventSeverity.error if down else FeedEventSeverity.success,
                detail=f"{detail} (detected via {source})" if source else detail,
                category="connectivity",
            )
        except Exception:  # noqa: BLE001 (feed emission must never break connectivity tracking)
            log.debug("connectivity.emit_failed", exc_info=True)


_tracker = ConnectivityTracker()


def get_connectivity_tracker() -> ConnectivityTracker:
    """Return the process-wide connectivity tracker."""
    return _tracker


def reset() -> None:
    """Reset the tracker to a healthy state.

    For tests: this is process-wide mutable state that gates agent spawning, so
    one test recording a transport failure would otherwise block spawns in every
    test that follows. Sibling of ``sova.utils.log_dedup.reset``.
    """
    global _tracker
    _tracker = ConnectivityTracker()


def get_connectivity_status() -> ConnectivityStatus:
    return _tracker.get_status()


def track_connectivity(result: object, source: str = "github") -> None:
    """Record connectivity state from a ShellResult.

    Accepts any object with ``is_network_unreachable`` and ``success``
    attributes, avoiding an import from ``sova.utils.shell`` (same duck-typed
    contract as ``track_rate_limit``).

    A failed call that is *not* a transport failure is deliberately ignored
    rather than counted as a success: a 404 or a permission denial proves the
    request reached GitHub, but a malformed-argument failure that never left
    the process proves nothing either way.
    """
    if getattr(result, "is_network_unreachable", False):
        _tracker.record_failure(source)
    elif getattr(result, "success", False):
        _tracker.record_success(source)


async def probe_connectivity() -> bool:
    """Resolve the probe hosts and update the tracker. Returns True if reachable.

    DNS only: no authenticated request, so this costs no GitHub API quota and
    works regardless of credential state. Resolving *any* host counts as
    reachable; the network is reported down only when every host fails, which
    is already conclusive enough to skip the failure threshold.
    """
    loop = asyncio.get_running_loop()
    for host in _PROBE_HOSTS:
        try:
            await asyncio.wait_for(
                loop.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP),
                timeout=_PROBE_TIMEOUT_SECONDS,
            )
        except (OSError, asyncio.TimeoutError):
            log.debug("connectivity.probe_host_failed", host=host)
            continue
        _tracker.record_success(f"probe:{host}")
        return True

    _tracker.record_failure("probe", decisive=True)
    return False
