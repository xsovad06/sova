"""GitHub API rate limit state tracker.

Lightweight in-memory singleton that records rate limit hits detected by the
shell layer and exposes a cooldown check for the supervisor and dashboard.
No DB persistence, no external API calls.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sova.utils.logging import get_logger

log = get_logger(component="supervisor.github_quota")

_DEFAULT_COOLDOWN_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class GitHubQuotaStatus:
    """Snapshot of the current GitHub API rate limit state."""

    is_limited: bool
    last_hit_at: float | None
    hits_in_window: int
    cooldown_remaining_seconds: float


class GitHubQuotaTracker:
    """Tracks GitHub API rate limit hits and provides a cooldown gate."""

    def __init__(self, cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS) -> None:
        self._cooldown_seconds = cooldown_seconds
        self._last_hit_at: float | None = None
        self._hits_in_window: int = 0
        self._was_limited = False
        self._last_emit_at: float | None = None

    def record_rate_limit_hit(self) -> None:
        """Record a rate limit error from a gh CLI call."""
        now = time.monotonic()
        self._last_hit_at = now
        self._hits_in_window += 1
        self._was_limited = True

        if self._last_emit_at is None or now - self._last_emit_at > 60.0:
            self._last_emit_at = now
            self._emit_hit_event()

        log.warning(
            "github_quota.rate_limited",
            hits=self._hits_in_window,
            cooldown_s=self._cooldown_seconds,
        )

    def record_success(self) -> None:
        """Record a successful API call. Emits recovery event on transition."""
        if self._was_limited and not self.should_skip():
            self._was_limited = False
            self._hits_in_window = 0
            self._emit_recovery_event()

    def should_skip(self) -> bool:
        """Return True if within the cooldown window after a rate limit hit."""
        if self._last_hit_at is None:
            return False
        return (time.monotonic() - self._last_hit_at) < self._cooldown_seconds

    def get_status(self) -> GitHubQuotaStatus:
        remaining = 0.0
        if self._last_hit_at is not None:
            remaining = max(0.0, self._cooldown_seconds - (time.monotonic() - self._last_hit_at))
        return GitHubQuotaStatus(
            is_limited=self.should_skip(),
            last_hit_at=self._last_hit_at,
            hits_in_window=self._hits_in_window,
            cooldown_remaining_seconds=remaining,
        )

    @staticmethod
    def _emit_hit_event() -> None:
        try:
            from sova.dashboard.services.feed_service import FeedEventSeverity, emit_safe

            emit_safe(
                "GitHub API rate limit exceeded",
                severity=FeedEventSeverity.error,
                detail="Dashboard data may be stale until the limit resets. The supervisor will pause spawning.",
                category="rate_limit",
            )
        except Exception:
            log.debug("emit_hit_event.failed", exc_info=True)

    @staticmethod
    def _emit_recovery_event() -> None:
        try:
            from sova.dashboard.services.feed_service import FeedEventSeverity, emit_safe

            emit_safe(
                "GitHub API rate limit recovered",
                severity=FeedEventSeverity.success,
                detail="API access restored. Dashboard data will refresh on the next poll.",
                category="rate_limit",
            )
        except Exception:
            log.debug("emit_recovery_event.failed", exc_info=True)


_trackers: dict[str, GitHubQuotaTracker] = {}
_DEFAULT_KEY = "__default__"


def get_github_quota_tracker(identity: str = "") -> GitHubQuotaTracker:
    """Return a per-identity rate limit tracker (or the default one)."""
    key = identity or _DEFAULT_KEY
    if key not in _trackers:
        _trackers[key] = GitHubQuotaTracker()
    return _trackers[key]


def get_github_quota_status(identity: str = "") -> GitHubQuotaStatus:
    """Return the quota status for a given GitHub identity."""
    return get_github_quota_tracker(identity).get_status()


def track_rate_limit(result: object, identity: str = "") -> None:
    """Record rate limit state from a gh CLI ShellResult.

    Accepts any object with ``is_rate_limited`` and ``success`` attributes
    (i.e. ShellResult) to avoid importing from sova.utils.shell.
    """
    tracker = get_github_quota_tracker(identity)
    if getattr(result, "is_rate_limited", False):
        tracker.record_rate_limit_hit()
    elif getattr(result, "success", False):
        tracker.record_success()
