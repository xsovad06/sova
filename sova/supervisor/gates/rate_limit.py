"""GitHub API rate limit gate: blocks when API rate limit is exhausted."""

from __future__ import annotations

from sova.supervisor.gates import BlockReason
from sova.utils.logging import get_logger

log = get_logger(component="supervisor.gates.rate_limit")


def check_github_rate_limit_gate(github_user: str) -> BlockReason | None:
    """Check if GitHub API rate limit is exhausted. Fail-open."""
    try:
        from sova.supervisor.github_quota import get_github_quota_tracker

        tracker = get_github_quota_tracker(github_user)
        if tracker.should_skip():
            status = tracker.get_status()
            return BlockReason(
                gate="rate_limit",
                detail=f"GitHub API rate limited (cooldown: {status.cooldown_remaining_seconds:.0f}s remaining)",
            )
    except Exception:
        log.debug("rate_limit_gate.check_failed", exc_info=True)
    return None
