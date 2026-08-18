"""Dependabot auto-merge: batch-process bot dependency PRs.

Detects open Dependabot PRs, waits for CI to pass, and auto-merges
(squash) without spawning any LLM agents. Major version bumps are
skipped. Configurable group-level approval gates (e.g., django-ecosystem
requires a label before merge).

Runs as a background loop in the dashboard lifespan or as a one-shot
sweep via ``sova maintain``.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.config.models import DependabotConfig, NotificationConfig

log = get_logger(component="supervisor.dependabot")

_DEPENDABOT_LOGINS = frozenset({"dependabot[bot]", "app/dependabot"})

_MAJOR_BUMP_RE = re.compile(
    r"(?:from|bump)\s+[<>=~^]*(\d+)(?:\.\S*)?\s+to\s+[<>=~^]*(\d+)(?:\.\S*)?(?:\s|$)",
    re.IGNORECASE,
)

_RANGE_MAJOR_RE = re.compile(
    r"(?:from|update\b.*\brequirement\s+from)\s+(.+?)\s+to\s+(.+?)(?:\s|$)",
    re.IGNORECASE,
)
_VERSION_EXTRACT_RE = re.compile(r"(\d+)(?:\.[^\s,]*)?")


def _extract_range_major(range_spec: str) -> int | None:
    """Extract the dominant major version from a comparator range like >=6.7,<7.0."""
    versions = _VERSION_EXTRACT_RE.findall(range_spec)
    if not versions:
        return None
    return min(int(v) for v in versions)


_GROUP_RE = re.compile(
    r"the\s+([\w-]+)\s+group",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class DependabotPR:
    """Minimal representation of a Dependabot PR for processing."""

    number: int
    title: str
    url: str
    labels: list[str]
    group: str
    has_major_bump: bool


@dataclass
class MergeResult:
    """Outcome of processing a single Dependabot PR."""

    pr_number: int
    title: str
    action: str  # "merged", "closed", "skipped", "waiting", "error"
    reason: str = ""


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def is_dependabot_pr(pr: dict) -> bool:
    """Check if a PR was authored by Dependabot."""
    author = pr.get("author") or {}
    login = (author.get("login") or "").lower()
    return login in _DEPENDABOT_LOGINS


def _detect_major_bump(title: str, body: str = "") -> bool:
    """Detect major version bumps in a PR title or body.

    Matches patterns like "bump X from 4.2.1 to 5.0.0" where the major
    version number changes. Also inspects the body for grouped PRs whose
    per-package version ranges appear only in the body.
    """
    text = f"{title}\n{body}" if body else title
    for match in _MAJOR_BUMP_RE.finditer(text):
        old_major, new_major = match.group(1), match.group(2)
        if old_major != new_major:
            return True
    for match in _RANGE_MAJOR_RE.finditer(text):
        old_range, new_range = match.group(1), match.group(2)
        if "," not in old_range and "," not in new_range:
            continue
        old_major = _extract_range_major(old_range)
        new_major = _extract_range_major(new_range)
        if old_major is not None and new_major is not None and old_major != new_major:
            return True
    return False


def _detect_group(title: str) -> str:
    """Extract the Dependabot group name from a PR title.

    Returns empty string for ungrouped PRs.
    """
    match = _GROUP_RE.search(title)
    return match.group(1) if match else ""


def _parse_dependabot_pr(pr: dict) -> DependabotPR:
    """Parse a raw gh JSON PR dict into a DependabotPR."""
    title = pr.get("title", "")
    body = pr.get("body", "") or ""
    labels = [(lbl.get("name") or "") if isinstance(lbl, dict) else str(lbl) for lbl in (pr.get("labels") or [])]
    return DependabotPR(
        number=pr["number"],
        title=title,
        url=pr.get("url", ""),
        labels=labels,
        group=_detect_group(title),
        has_major_bump=_detect_major_bump(title, body),
    )


def _should_skip(
    pr: DependabotPR,
    config: DependabotConfig,
) -> str:
    """Return a skip reason, or empty string if the PR should be processed."""
    if pr.has_major_bump:
        return "major version bump detected"

    if pr.group and pr.group in config.require_approval_groups:
        if config.approval_label not in pr.labels:
            return f"group '{pr.group}' requires '{config.approval_label}' label"
        return ""

    if config.auto_merge_groups and pr.group and pr.group not in config.auto_merge_groups:
        return f"group '{pr.group}' is not in auto_merge_groups"

    return ""


def classify_dependabot_prs(
    raw_prs: list[dict],
    config: DependabotConfig,
) -> list[tuple[DependabotPR, str]]:
    """Parse raw gh PR dicts and classify each Dependabot PR.

    Returns a list of (DependabotPR, skip_reason) tuples. skip_reason is
    empty if the PR should be processed.
    """
    results: list[tuple[DependabotPR, str]] = []
    for pr in raw_prs:
        if not is_dependabot_pr(pr):
            continue
        dpr = _parse_dependabot_pr(pr)
        skip_reason = _should_skip(dpr, config)
        results.append((dpr, skip_reason))
    return results


# ---------------------------------------------------------------------------
# CI polling
# ---------------------------------------------------------------------------


async def _wait_for_ci(
    pr_number: int,
    *,
    repo: str,
    github_user: str,
    poll_interval: float,
    timeout: float,
    no_checks_grace_period: float = 120,
) -> str:
    """Poll CI checks until all pass, any fail, or timeout.

    Returns one of: "passed", "failed", "timeout", "error".
    """
    from sova.git.pr import CheckConclusion, CheckStatus, get_ci_checks

    deadline = time.monotonic() + timeout
    empty_deadline = time.monotonic() + no_checks_grace_period

    while time.monotonic() < deadline:
        checks = await get_ci_checks(pr_number, repo=repo, github_user=github_user)
        if checks is None:
            return "error"

        if not checks:
            if time.monotonic() >= empty_deadline:
                return "passed"
            await asyncio.sleep(poll_interval)
            continue

        all_completed = all(c.status == CheckStatus.COMPLETED for c in checks)
        if all_completed:
            any_failed = any(c.conclusion in (CheckConclusion.FAILURE, CheckConclusion.TIMED_OUT) for c in checks)
            if any_failed:
                return "failed"

            all_success = all(
                c.conclusion in (CheckConclusion.SUCCESS, CheckConclusion.SKIPPED, CheckConclusion.NEUTRAL)
                for c in checks
            )
            if all_success:
                return "passed"

            log.info(
                "dependabot.inconclusive_checks",
                pr=pr_number,
                conclusions=[c.conclusion for c in checks],
            )
            await asyncio.sleep(poll_interval)
            continue

        await asyncio.sleep(poll_interval)

    return "timeout"


# ---------------------------------------------------------------------------
# Merge / close operations
# ---------------------------------------------------------------------------


async def _merge_pr(
    pr_number: int,
    *,
    repo: str,
    github_user: str,
) -> str:
    """Squash-merge a PR via gh CLI.

    Returns "merged" if the PR is confirmed merged, "pending" if auto-merge
    was enabled but the PR is still open, or "error" on failure.
    """
    from sova.utils.gh import resolve_gh_env
    from sova.utils.shell import run

    env = await resolve_gh_env(github_user)
    result = await run(
        "gh",
        "pr",
        "merge",
        str(pr_number),
        "--repo",
        repo,
        "--squash",
        "--auto",
        "--delete-branch",
        env=env,
    )

    from sova.supervisor.github_quota import track_rate_limit

    track_rate_limit(result, github_user)

    if not result.success:
        log.warning(
            "dependabot.merge_failed",
            pr=pr_number,
            stderr=result.stderr[:200],
        )
        return "error"

    state = await _check_pr_state(pr_number, repo=repo, github_user=github_user)
    if state == "MERGED":
        log.info("dependabot.merged", pr=pr_number, repo=repo)
        return "merged"

    log.info("dependabot.auto_merge_pending", pr=pr_number, repo=repo)
    return "pending"


async def _check_pr_state(
    pr_number: int,
    *,
    repo: str,
    github_user: str,
) -> str:
    """Check the current state of a PR. Returns 'MERGED', 'OPEN', 'CLOSED', or 'unknown'."""
    from sova.utils.gh import resolve_gh_env
    from sova.utils.shell import run

    env = await resolve_gh_env(github_user)
    result = await run(
        "gh",
        "pr",
        "view",
        str(pr_number),
        "--repo",
        repo,
        "--json",
        "state",
        "--jq",
        ".state",
        env=env,
    )
    if not result.success:
        return "unknown"
    return result.stdout.strip()


async def _close_pr_with_comment(
    pr_number: int,
    *,
    repo: str,
    github_user: str,
    reason: str,
) -> bool:
    """Close a PR with an explanatory comment. Returns True on success."""
    from sova.utils.gh import resolve_gh_env
    from sova.utils.shell import run

    env = await resolve_gh_env(github_user)

    comment_result = await run(
        "gh",
        "pr",
        "comment",
        str(pr_number),
        "--repo",
        repo,
        "--body",
        f"CI failed on dependency update: {reason}\n\nClosing until next Dependabot run.",
        env=env,
    )
    from sova.supervisor.github_quota import track_rate_limit

    track_rate_limit(comment_result, github_user)

    close_result = await run(
        "gh",
        "pr",
        "close",
        str(pr_number),
        "--repo",
        repo,
        env=env,
    )
    track_rate_limit(close_result, github_user)

    if not close_result.success:
        log.warning(
            "dependabot.close_failed",
            pr=pr_number,
            stderr=close_result.stderr[:200],
        )
        return False

    log.info("dependabot.closed", pr=pr_number, reason=reason)
    return True


# ---------------------------------------------------------------------------
# Cost recording
# ---------------------------------------------------------------------------


async def _record_dependabot_cost(
    pr_number: int,
    action: str,
    *,
    project_dir: Path,
) -> None:
    """Record a zero-cost entry for a Dependabot action."""
    from decimal import Decimal

    from sova.db.models import CostRecord
    from sova.db.session import get_session

    try:
        async with await get_session(project_dir=project_dir) as session:
            record = CostRecord(
                model="dependabot",
                cost_usd=Decimal("0"),
                input_tokens=0,
                output_tokens=0,
                phase="dependabot",
                issue=str(pr_number),
            )
            session.add(record)
            await session.commit()
    except Exception:
        log.warning("dependabot.cost_record_failed", pr=pr_number, exc_info=True)
        return

    log.debug("dependabot.cost_recorded", pr=pr_number, action=action)


# ---------------------------------------------------------------------------
# Single-PR processing
# ---------------------------------------------------------------------------


async def _process_pr(
    pr: DependabotPR,
    *,
    repo: str,
    github_user: str,
    config: DependabotConfig,
    project_dir: Path,
    notification_config: NotificationConfig | None = None,
    no_checks_grace_period: int = 120,
) -> MergeResult:
    """Process a single Dependabot PR through the full lifecycle."""
    skip_reason = _should_skip(pr, config)
    if skip_reason:
        log.info("dependabot.skipped", pr=pr.number, reason=skip_reason)
        return MergeResult(pr.number, pr.title, "skipped", skip_reason)

    ci_result = await _wait_for_ci(
        pr.number,
        repo=repo,
        github_user=github_user,
        poll_interval=config.ci_poll_interval_seconds,
        timeout=config.ci_poll_timeout_seconds,
        no_checks_grace_period=no_checks_grace_period,
    )

    if ci_result == "passed":
        merge_state = await _merge_pr(pr.number, repo=repo, github_user=github_user)
        if merge_state == "merged":
            await _record_dependabot_cost(pr.number, "merged", project_dir=project_dir)
            _emit_feed_event(pr.number, pr.title, "merged", repo=repo)
            _notify_action(pr.number, pr.title, "merged", config=notification_config)
            return MergeResult(pr.number, pr.title, "merged")
        if merge_state == "pending":
            return MergeResult(pr.number, pr.title, "waiting", "auto-merge enabled, awaiting branch protection")
        return MergeResult(pr.number, pr.title, "error", "merge command failed")

    if ci_result == "failed":
        closed = await _close_pr_with_comment(
            pr.number,
            repo=repo,
            github_user=github_user,
            reason="CI checks did not pass",
        )
        if closed:
            await _record_dependabot_cost(pr.number, "closed", project_dir=project_dir)
            _emit_feed_event(pr.number, pr.title, "closed_ci_failed", repo=repo)
            _notify_action(pr.number, pr.title, "closed (CI failed)", config=notification_config)
            return MergeResult(pr.number, pr.title, "closed", "CI failed")
        return MergeResult(pr.number, pr.title, "error", "close command failed")

    if ci_result == "timeout":
        log.warning("dependabot.ci_timeout", pr=pr.number)
        return MergeResult(pr.number, pr.title, "waiting", "CI timed out, will retry next cycle")

    return MergeResult(pr.number, pr.title, "error", f"CI check returned: {ci_result}")


# ---------------------------------------------------------------------------
# Feed + notification helpers
# ---------------------------------------------------------------------------


def _emit_feed_event(
    pr_number: int,
    title: str,
    action: str,
    *,
    repo: str,
) -> None:
    """Emit a dashboard feed event (non-fatal)."""
    try:
        from sova.dashboard.services.feed_service import FeedEventSeverity, emit_safe

        severity_map = {
            "merged": FeedEventSeverity.success,
            "closed_ci_failed": FeedEventSeverity.warning,
            "skipped": FeedEventSeverity.info,
        }
        severity = severity_map.get(action, FeedEventSeverity.info)
        action_label = {
            "merged": "auto-merged",
            "closed_ci_failed": "closed (CI failed)",
            "skipped": "skipped",
        }.get(action, action)

        emit_safe(
            f"Dependabot PR #{pr_number} {action_label}",
            severity=severity,
            detail=title,
            category="dependabot",
            metadata={"pr_number": pr_number, "repo": repo, "action": action},
        )
    except Exception:
        log.debug("dependabot.feed_event_failed", pr=pr_number, exc_info=True)


def _notify_action(
    pr_number: int,
    title: str,
    action: str,
    *,
    config: NotificationConfig | None,
) -> None:
    """Send a desktop notification (non-fatal)."""
    if config is None:
        return
    try:
        from sova.ipc.notifications import notify

        notify(
            config,
            title="SOVA",
            subtitle=f"Dependabot PR #{pr_number} {action}",
            message=title,
            group=f"sova-dependabot-{pr_number}",
        )
    except Exception:
        log.debug("dependabot.notify_failed", pr=pr_number, exc_info=True)


# ---------------------------------------------------------------------------
# Batch sweep (one-shot or called from loop)
# ---------------------------------------------------------------------------


async def sweep_dependabot_prs(
    *,
    project_dir: Path,
    repo: str,
    github_user: str,
    config: DependabotConfig,
    notification_config: NotificationConfig | None = None,
) -> list[MergeResult]:
    """Sweep all open Dependabot PRs and process them.

    Returns a list of results, one per Dependabot PR found.
    """
    from sova.git.pr import list_open_prs
    from sova.supervisor.github_quota import get_github_quota_tracker

    if not config.enabled:
        log.info("dependabot.sweep_skipped_disabled")
        return []

    tracker = get_github_quota_tracker(github_user)
    if tracker.should_skip():
        log.info("dependabot.sweep_skipped_rate_limited")
        return []

    all_prs = await list_open_prs(repo=repo, github_user=github_user)
    dependabot_prs = [_parse_dependabot_pr(pr) for pr in all_prs if is_dependabot_pr(pr)]

    if not dependabot_prs:
        log.info("dependabot.sweep_no_prs")
        return []

    log.info("dependabot.sweep_found", count=len(dependabot_prs))

    from sova.config.loader import load_config

    grace_period = load_config(project_dir).ci.no_checks_grace_period

    sem = asyncio.Semaphore(3)

    async def _bounded_process(dpr: DependabotPR) -> MergeResult:
        async with sem:
            try:
                return await _process_pr(
                    dpr,
                    repo=repo,
                    github_user=github_user,
                    config=config,
                    project_dir=project_dir,
                    notification_config=notification_config,
                    no_checks_grace_period=grace_period,
                )
            except Exception:
                log.warning("dependabot.process_error", pr=dpr.number, exc_info=True)
                return MergeResult(dpr.number, dpr.title, "error", "unhandled exception")

    results = list(await asyncio.gather(*[_bounded_process(dpr) for dpr in dependabot_prs]))

    merged = sum(1 for r in results if r.action == "merged")
    closed = sum(1 for r in results if r.action == "closed")
    skipped = sum(1 for r in results if r.action == "skipped")
    log.info(
        "dependabot.sweep_complete",
        total=len(results),
        merged=merged,
        closed=closed,
        skipped=skipped,
    )

    return results


# ---------------------------------------------------------------------------
# Background loop (dashboard lifespan)
# ---------------------------------------------------------------------------


@dataclass
class DependabotMonitor:
    """Polls open PRs and auto-merges Dependabot dependency updates."""

    project_dir: Path
    config: DependabotConfig
    notification_config: NotificationConfig
    repo: str
    github_user: str

    _last_results: list[MergeResult] = field(default_factory=list)

    async def run_loop(self) -> None:
        """Main polling loop. Runs until cancelled."""
        log.info(
            "dependabot.monitor_started",
            poll_interval=self.config.poll_interval_seconds,
        )
        while True:
            try:
                self._last_results = await sweep_dependabot_prs(
                    project_dir=self.project_dir,
                    repo=self.repo,
                    github_user=self.github_user,
                    config=self.config,
                    notification_config=self.notification_config,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("dependabot.cycle_error", exc_info=True)
            await asyncio.sleep(self.config.poll_interval_seconds)

    @property
    def last_results(self) -> list[MergeResult]:
        return list(self._last_results)


def create_monitors_for_projects() -> list[DependabotMonitor]:
    """Create DependabotMonitor instances for all registered projects."""
    from sova.config.loader import load_config
    from sova.config.registry import list_projects

    monitors: list[DependabotMonitor] = []
    for path_str in list_projects().values():
        p = Path(path_str)
        if not p.is_dir():
            continue
        try:
            pcfg = load_config(p)
        except Exception:
            log.warning("dependabot.config_load_failed", project=str(p), exc_info=True)
            continue
        if not pcfg.dependabot.enabled or not pcfg.github_repo:
            continue
        monitors.append(
            DependabotMonitor(
                project_dir=p,
                config=pcfg.dependabot,
                notification_config=pcfg.notification,
                repo=pcfg.github_repo,
                github_user=pcfg.github_user,
            )
        )
    return monitors
