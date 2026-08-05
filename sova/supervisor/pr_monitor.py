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

from sova.config.models import PRMonitorConfig
from sova.dashboard.services.pr_service import _STATE_LABELS, ComputedPRState
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.config.models import NotificationConfig
    from sova.utils.shell import ShellResult

log = get_logger(component="supervisor.pr_monitor")

_RATE_LIMIT_KEYWORDS = frozenset({"rate limit", "hourly quota", "usage limit"})


def _track_gh_rate_limit_pr_monitor(result: "ShellResult", github_user: str = "") -> None:
    """Record rate limit state from a gh CLI call in the PR monitor."""
    from sova.supervisor.github_quota import track_rate_limit

    track_rate_limit(result, github_user)


_CODERABBIT_LOGINS = frozenset({"coderabbitai", "coderabbit-ai[bot]", "coderabbitai[bot]"})

_NOTIFY_STATES: dict[str, str] = {
    ComputedPRState.APPROVED: "notify_on_approval",
    ComputedPRState.APPROVED_CI_GREEN: "notify_on_ready_to_merge",
    ComputedPRState.CHANGES_REQUESTED: "notify_on_changes_requested",
    ComputedPRState.CI_FAILED: "notify_on_ci_failure",
}

# Validate that every config flag in _NOTIFY_STATES is a real PRMonitorConfig field
for _flag in _NOTIFY_STATES.values():
    if _flag not in PRMonitorConfig.model_fields:
        raise AttributeError(f"_NOTIFY_STATES references unknown PRMonitorConfig field: {_flag!r}")
del _flag


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
        from sova.supervisor.github_quota import get_github_quota_tracker

        tracker = get_github_quota_tracker(self.github_user)
        if tracker.should_skip():
            log.info("pr_monitor.skipped_rate_limited")
            return

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

        # Parallelize rate limit checks across all PRs
        rate_limits: dict[int, bool] = {}
        if self.monitor_config.auto_retry_coderabbit and prs:
            tasks = {
                pr["number"]: asyncio.ensure_future(
                    _is_coderabbit_rate_limited(pr["number"], repo=self.repo, github_user=self.github_user)
                )
                for pr in prs
            }
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)
            for number, result in zip(tasks.keys(), results):
                if isinstance(result, BaseException):
                    log.debug("pr_monitor.rate_limit_check_failed", pr=number, exc_info=result)
                    rate_limits[number] = False
                else:
                    rate_limits[number] = result

        # Recheck: _is_coderabbit_rate_limited calls above may have
        # triggered a rate limit hit during the gather.
        if tracker.should_skip():
            log.info("pr_monitor.skipped_rate_limited_mid_cycle")
            return

        for pr in prs:
            number = pr["number"]
            snapshot = PRSnapshot(
                number=number,
                computed_state=pr["computed_state"],
                title=pr["title"],
                rate_limited=rate_limits.get(number, False),
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
        try:
            env = await resolve_gh_env(self.github_user) if self.github_user else None
        except Exception:
            log.warning("pr_monitor.gh_env_failed", pr=pr_number, exc_info=True)
            return
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
        _track_gh_rate_limit_pr_monitor(result, self.github_user)
        if not result.success:
            log.warning(
                "pr_monitor.retry_coderabbit_failed",
                pr=pr_number,
                stderr=result.stderr[:200],
            )


def create_monitors_for_projects() -> list[PRMonitor]:
    """Create PRMonitor instances for all registered projects with monitoring enabled.

    Returns monitors for projects that have valid config, pr_monitor enabled,
    and a github_repo configured. Logs warnings for projects that fail config
    loading and silently skips disabled/unconfigured ones.
    """
    from sova.config.loader import load_config
    from sova.config.registry import list_projects

    monitors: list[PRMonitor] = []
    for path_str in list_projects().values():
        p = Path(path_str)
        if not p.is_dir():
            continue
        try:
            pcfg = load_config(p)
        except Exception:
            log.warning("pr_monitor.config_load_failed", project=str(p), exc_info=True)
            continue
        if not pcfg.pr_monitor.enabled or not pcfg.github_repo:
            continue
        monitors.append(
            PRMonitor(
                project_dir=p,
                monitor_config=pcfg.pr_monitor,
                notification_config=pcfg.notification,
                repo=pcfg.github_repo,
                github_user=pcfg.github_user,
            )
        )
    return monitors


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
    _track_gh_rate_limit_pr_monitor(result, github_user)
    if not result.success:
        return False

    try:
        data = json.loads(result.stdout)
    except ValueError:
        return False

    comments = data.get("comments") or []
    # GitHub API returns comments oldest-first; reverse to check newest first
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
