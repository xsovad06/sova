"""PR monitor -- background loop that polls open PRs for state changes.

Detects state transitions (CI pass/fail, approval, changes requested, ready
to merge), sends desktop notifications, and auto-retries rate-limited
CodeRabbit reviews when quota is available.

Integrated into the dashboard lifespan alongside the liveness sweep and
PR throttle loops.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from sova.dashboard.services.pr_service import _STATE_LABELS, ComputedPRState
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.config.models import NotificationConfig, PRMonitorConfig

log = get_logger(component="supervisor.pr_monitor")

_RATE_LIMIT_KEYWORDS = frozenset({"rate limit", "hourly quota", "usage limit"})
_CODERABBIT_LOGINS = frozenset({"coderabbitai", "coderabbit-ai[bot]", "coderabbitai[bot]"})

_NOTIFY_STATES: dict[str, str] = {
    ComputedPRState.APPROVED: "notify_on_approval",
    ComputedPRState.APPROVED_CI_GREEN: "notify_on_ready_to_merge",
    ComputedPRState.CHANGES_REQUESTED: "notify_on_changes_requested",
    ComputedPRState.CI_FAILED: "notify_on_ci_failure",
}


@dataclass
class PRSnapshot:
    """Minimal PR state snapshot for change detection."""

    number: int
    computed_state: str
    title: str
    rate_limited: bool = False


@dataclass
class PRMonitor:
    """Polls open PRs and fires notifications on state transitions."""

    project_dir: Path
    monitor_config: PRMonitorConfig
    notification_config: NotificationConfig
    repo: str
    github_user: str

    _last_state: dict[int, PRSnapshot] = field(default_factory=dict)
    _initialized: bool = False

    async def run_loop(self) -> None:
        """Main polling loop. Runs until cancelled."""
        log.info("pr_monitor.started", poll_interval=self.monitor_config.poll_interval)
        while True:
            try:
                await self._poll_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("pr_monitor.cycle_error", exc_info=True)
            await asyncio.sleep(self.monitor_config.poll_interval)

    async def _poll_cycle(self) -> None:
        """Single poll cycle: fetch PRs, detect transitions, act."""
        from sova.config.context import clear_project_context, set_project_context
        from sova.dashboard.services.pr_service import list_open_prs_with_state

        # list_open_prs_with_state() uses get_project_dir() from a ContextVar
        # that is normally set by request middleware.  Background tasks have no
        # request, so we set it explicitly.
        slug = self.repo.replace("/", "-")
        set_project_context(self.project_dir, slug)
        try:
            prs = await list_open_prs_with_state()
        finally:
            clear_project_context()
        current: dict[int, PRSnapshot] = {}

        for pr in prs:
            number = pr["number"]
            if self.monitor_config.auto_retry_coderabbit:
                try:
                    rate_limited = await _is_coderabbit_rate_limited(
                        number, repo=self.repo, github_user=self.github_user
                    )
                except Exception:
                    log.debug("pr_monitor.rate_limit_check_failed", pr=number, exc_info=True)
                    rate_limited = False
            else:
                rate_limited = False

            snapshot = PRSnapshot(
                number=number,
                computed_state=pr["computed_state"],
                title=pr["title"],
                rate_limited=rate_limited,
            )
            current[number] = snapshot

            if not self._initialized:
                continue

            prev = self._last_state.get(number)
            try:
                await self._handle_transition(prev, snapshot)
            except Exception:
                log.warning("pr_monitor.transition_error", pr=number, exc_info=True)

        self._last_state = current
        if not self._initialized:
            self._initialized = True
            log.info("pr_monitor.initialized", pr_count=len(current))

    async def _handle_transition(
        self,
        prev: PRSnapshot | None,
        curr: PRSnapshot,
    ) -> None:
        """Process a single PR's state change."""
        prev_state = prev.computed_state if prev else None

        if curr.computed_state != prev_state:
            self._maybe_notify(curr)

        was_rate_limited = prev.rate_limited if prev else False
        if was_rate_limited and not curr.rate_limited and self.monitor_config.auto_retry_coderabbit:
            await self._retry_coderabbit_review(curr.number)

    def _maybe_notify(self, snapshot: PRSnapshot) -> None:
        """Send a notification if the new state is one we care about."""
        config_flag = _NOTIFY_STATES.get(snapshot.computed_state)
        if not config_flag:
            return
        if not getattr(self.monitor_config, config_flag, False):
            return

        from sova.ipc.notifications import notify

        state_label = _STATE_LABELS.get(snapshot.computed_state, snapshot.computed_state)
        notify(
            self.notification_config,
            title="SOVA",
            subtitle=f"PR #{snapshot.number} {state_label}",
            message=snapshot.title,
            group=f"sova-pr-{snapshot.number}",
        )
        log.info(
            "pr_monitor.notified",
            pr=snapshot.number,
            state=snapshot.computed_state,
        )

    async def _retry_coderabbit_review(self, pr_number: int) -> None:
        """Post @coderabbitai review comment to trigger a re-review."""
        from sova.utils.gh import resolve_gh_env
        from sova.utils.shell import run

        log.info("pr_monitor.retry_coderabbit", pr=pr_number)
        env = await resolve_gh_env(self.github_user) if self.github_user else None
        result = await run(
            "gh",
            "pr",
            "comment",
            str(pr_number),
            "--repo",
            self.repo,
            "--body",
            "@coderabbitai review",
            env=env,
        )
        if not result.success:
            log.warning(
                "pr_monitor.retry_coderabbit_failed",
                pr=pr_number,
                stderr=result.stderr[:200],
            )


async def _is_coderabbit_rate_limited(
    pr_number: int,
    *,
    repo: str,
    github_user: str = "",
) -> bool:
    """Check if CodeRabbit's most recent comment on the PR indicates rate limiting."""
    import json

    from sova.utils.gh import resolve_gh_env
    from sova.utils.shell import run

    env = await resolve_gh_env(github_user) if github_user else None
    result = await run(
        "gh",
        "pr",
        "view",
        str(pr_number),
        "--repo",
        repo,
        "--json",
        "comments",
        env=env,
    )
    if not result.success:
        return False

    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return False

    comments = data.get("comments") or []
    for comment in reversed(comments):
        author = (comment.get("author") or {}).get("login", "").lower()
        if author not in _CODERABBIT_LOGINS:
            continue
        body = (comment.get("body") or "").lower()
        if any(kw in body for kw in _RATE_LIMIT_KEYWORDS):
            return True
        # Only check the most recent CodeRabbit comment
        break

    return False
