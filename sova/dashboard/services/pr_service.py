"""PR tracker service -- lists open PRs with computed lifecycle state."""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.config.models import ProjectConfig

log = get_logger(component="dashboard.pr_service")

_ISSUE_LINK_RE = re.compile(r"(?:closes|fixes|resolves)\s+#(\d+)", re.IGNORECASE)
_JIRA_MARKDOWN_RE = re.compile(r"\[([A-Z]+-\d+)\]\(https?://")
_JIRA_PLAIN_RE = re.compile(r"JIRA:\s*https?://\S+/browse/[A-Z]+-(\d+)")
_TITLE_ISSUE_RE = re.compile(r"[(\[]#(\d+)[)\]]")
_TITLE_JIRA_KEY_RE = re.compile(r"\[[A-Z]+-(\d+)\]")
_BRANCH_ISSUE_RE = re.compile(r"(?:^|/)issue-(\d+)(?=$|[-_/])")
_BRANCH_JIRA_KEY_RE = re.compile(r"(?:^|/)[A-Z]+-(\d+)(?=$|[-_/])")

_PR_CACHE_TTL = 120  # seconds (shared across supervisor, PR monitor, dashboard)
_pr_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}

_last_known_states: dict[int, str] = {}
_bg_tasks: set[asyncio.Task[None]] = set()

_COMPUTED_TO_EVENT: dict[str, str] = {
    "approved": "approved",
    "approved_ci_green": "approved",
    "ci_failed": "ci_failed",
    "changes_requested": "reviewed",
    "review_addressed": "reviewed",
}


class ComputedPRState(StrEnum):
    DRAFT = "draft"
    CONFLICTED = "conflicted"
    CI_RUNNING = "ci_running"
    CI_FAILED = "ci_failed"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED_CI_GREEN = "approved_ci_green"
    APPROVED = "approved"
    REVIEW_ADDRESSED = "review_addressed"
    AWAITING_REVIEW = "awaiting_review"


_STATE_LABELS: dict[str, str] = {
    "draft": "Draft",
    "conflicted": "Conflicts",
    "ci_running": "CI Running",
    "ci_failed": "CI Failed",
    "changes_requested": "Changes Requested",
    "approved_ci_green": "Ready to Merge",
    "approved": "Approved",
    "review_addressed": "In Review",
    "awaiting_review": "Awaiting Review",
}


def parse_linked_issue(body: str | None) -> int | None:
    """Extract the first issue number from PR body.

    Supports GitHub syntax (Closes/Fixes/Resolves #N) and JIRA syntax
    ([PROJ-42](https://...) or JIRA: https://.../browse/PROJ-42).
    """
    if not body:
        return None
    m = _ISSUE_LINK_RE.search(body)
    if m:
        return int(m.group(1))
    m = _JIRA_MARKDOWN_RE.search(body)
    if m:
        return int(m.group(1).split("-")[-1])
    m = _JIRA_PLAIN_RE.search(body)
    if m:
        return int(m.group(1))
    return None


def _extract_linked_issue(raw: dict) -> int | None:
    """Extract linked issue from closingIssuesReferences, PR body, title, or branch.

    Priority: closingIssuesReferences (accurate, excludes PR-to-PR refs)
    > body keywords (Closes/Fixes/Resolves #N, JIRA links)
    > title (#N or [PROJ-N]) > branch (issue-N or PROJ-N).
    """
    refs = raw.get("closingIssuesReferences") or []
    if refs:
        return refs[0].get("number")
    from_body = parse_linked_issue(raw.get("body"))
    if from_body is not None:
        return from_body
    title = raw.get("title") or ""
    m = _TITLE_ISSUE_RE.search(title)
    if m:
        return int(m.group(1))
    m = _TITLE_JIRA_KEY_RE.search(title)
    if m:
        return int(m.group(1))
    branch = raw.get("headRefName") or ""
    m = _BRANCH_ISSUE_RE.search(branch)
    if m:
        return int(m.group(1))
    m = _BRANCH_JIRA_KEY_RE.search(branch)
    if m:
        return int(m.group(1))
    return None


def _extract_all_linked_issues(raw: dict) -> list[int]:
    """Return all issue numbers linked to a PR from closingIssuesReferences or body."""
    refs = raw.get("closingIssuesReferences") or []
    if refs:
        return [r.get("number") for r in refs if r.get("number") is not None]
    single = parse_linked_issue(raw.get("body"))
    if single is not None:
        return [single]
    return []


def _summarize_ci(rollup: list[dict] | None) -> str:
    """Summarize statusCheckRollup contexts into a single CI status string.

    Handles both CheckRun (status/conclusion) and StatusContext (state) entries.
    """
    if not rollup:
        return "none"
    states = set()
    for ctx in rollup:
        is_status_context = ctx.get("__typename") == "StatusContext"
        if is_status_context:
            sc_state = (ctx.get("state") or "").upper()
            if sc_state == "SUCCESS":
                states.add("passed")
            elif sc_state in ("FAILURE", "ERROR"):
                states.add("failed")
            elif sc_state == "PENDING":
                states.add("pending")
            continue

        status = (ctx.get("status") or "").upper()
        conclusion = (ctx.get("conclusion") or "").upper()
        if conclusion in (
            "FAILURE",
            "ERROR",
            "TIMED_OUT",
            "STARTUP_FAILURE",
            "ACTION_REQUIRED",
            "STALE",
        ):
            states.add("failed")
        elif conclusion == "SUCCESS":
            states.add("passed")
        elif conclusion in ("SKIPPED", "NEUTRAL", "CANCELLED"):
            states.add("skipped")
        elif status == "COMPLETED":
            states.add("passed")
        else:
            states.add("pending")
    if "failed" in states:
        return "failed"
    if "pending" in states:
        return "pending"
    if "passed" in states:
        return "passed"
    if "skipped" in states:
        return "passed"
    return "none"


def _is_bot_login(login: str) -> bool:
    """Check if a login belongs to a bot account: [bot] suffix or a known CodeRabbit alias."""
    from sova.adapters.external_reviews import DEFAULT_CODERABBIT_AUTHORS

    login = login.lower()
    return login.endswith("[bot]") or login in DEFAULT_CODERABBIT_AUTHORS


def _is_bot_review(review: dict) -> bool:
    """Check if a review is from a bot account."""
    author = review.get("author") or {}
    return _is_bot_login(author.get("login") or "")


def _extract_cr_reviews(latest_reviews: list[dict]) -> list[dict]:
    """Extract CHANGES_REQUESTED reviews from latest_reviews."""
    return [r for r in latest_reviews if r.get("state") == "CHANGES_REQUESTED"]


def _should_unblock_bot_reviews(
    cr_reviews: list[dict],
    all_threads_resolved: bool | None,
    ci_status: str,
    mergeable: str,
    superseded_by_new_commit: bool = False,
) -> str | None:
    """Check if bot CHANGES_REQUESTED reviews with resolved threads should unblock the PR.

    Returns APPROVED_CI_GREEN if all conditions are met and CI is green + mergeable,
    APPROVED if conditions are met but CI is not green or not mergeable,
    None if any condition fails: no CR reviews, threads unresolved or unknown, human
    CR present, or the bot review has not been superseded by a newer commit (new
    commits do not auto-dismiss a bot review on GitHub, so this is required in
    addition to, not instead of, resolved/vacuous-zero threads).
    """
    if not cr_reviews:
        return None
    if not all_threads_resolved:
        return None
    if not all(_is_bot_review(r) for r in cr_reviews):
        return None
    if not superseded_by_new_commit:
        return None
    if ci_status == "passed" and mergeable == "MERGEABLE":
        return ComputedPRState.APPROVED_CI_GREEN
    return ComputedPRState.APPROVED


def compute_pr_state(
    *,
    is_draft: bool,
    review_decision: str,
    ci_status: str,
    mergeable: str,
    latest_reviews: list[dict] | None = None,
    all_threads_resolved: bool | None = False,
    superseded_by_new_commit: bool = False,
) -> str:
    """Derive a single computed state from PR signals."""
    if is_draft:
        return ComputedPRState.DRAFT
    if mergeable == "CONFLICTING":
        return ComputedPRState.CONFLICTED
    if ci_status == "pending":
        return ComputedPRState.CI_RUNNING
    if ci_status == "failed":
        return ComputedPRState.CI_FAILED
    if review_decision == "CHANGES_REQUESTED":
        if latest_reviews:
            cr_reviews = _extract_cr_reviews(latest_reviews)
            unblock_state = _should_unblock_bot_reviews(
                cr_reviews, all_threads_resolved, ci_status, mergeable, superseded_by_new_commit
            )
            if unblock_state:
                return unblock_state
        return ComputedPRState.CHANGES_REQUESTED
    if review_decision == "APPROVED" and ci_status == "passed" and mergeable == "MERGEABLE":
        return ComputedPRState.APPROVED_CI_GREEN
    if review_decision == "APPROVED":
        return ComputedPRState.APPROVED
    if latest_reviews:
        cr_reviews = _extract_cr_reviews(latest_reviews)
        if cr_reviews:
            unblock_state = _should_unblock_bot_reviews(
                cr_reviews, all_threads_resolved, ci_status, mergeable, superseded_by_new_commit
            )
            if unblock_state:
                return unblock_state
            return ComputedPRState.CHANGES_REQUESTED
        if all_threads_resolved and ci_status == "passed" and mergeable == "MERGEABLE":
            return ComputedPRState.APPROVED_CI_GREEN
        if all_threads_resolved:
            return ComputedPRState.APPROVED
        return ComputedPRState.REVIEW_ADDRESSED
    if ci_status == "passed" and mergeable == "MERGEABLE":
        return ComputedPRState.APPROVED_CI_GREEN
    return ComputedPRState.AWAITING_REVIEW


def _age_seconds(created: str, now: float) -> int:
    """Compute age in seconds from an ISO 8601 timestamp."""
    if not created:
        return 0
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        return int(now - dt.timestamp())
    except (ValueError, TypeError):
        return 0


def _extract_review_logins(latest_reviews: list[dict] | None) -> list[str]:
    """Extract unique reviewer logins from latest reviews."""
    logins: set[str] = set()
    for rev in latest_reviews or []:
        login = (rev.get("author") or {}).get("login") or ""
        if login:
            logins.add(login)
    return sorted(logins)


def _extract_latest_approval_at(latest_reviews: list[dict] | None) -> str | None:
    """Return the most recent human-approved review's submittedAt timestamp, or None.

    Excludes bot approvals (CodeRabbit or any other bot): a bot's approval must
    never be mistaken for a human sign-off. Mirrors the login-based bot check
    in sova/supervisor/gates/review_completed.py:_has_human_approval().
    """
    best: str | None = None
    for rev in latest_reviews or []:
        if rev.get("state") != "APPROVED" or _is_bot_review(rev):
            continue
        ts = rev.get("submittedAt") or ""
        if ts and (best is None or ts > best):
            best = ts
    return best


def _extract_pr_labels(raw: dict) -> list[str]:
    """Extract label names from raw PR data."""
    return [lbl.get("name", "") for lbl in (raw.get("labels") or [])]


def _extract_pr_assignees(raw: dict) -> list[str]:
    """Extract non-empty assignee logins from raw PR data."""
    return [a.get("login", "") for a in (raw.get("assignees") or []) if a.get("login")]


def _count_pr_commits(raw: dict) -> int:
    """Count commits on the PR, tolerating a non-list `commits` field."""
    commits_node = raw.get("commits") or []
    return len(commits_node) if isinstance(commits_node, list) else 0


def _enrich_pr(raw: dict, now: float) -> dict:
    """Transform a raw gh pr list entry into a PR tracker dict."""
    ci_status = _summarize_ci(raw.get("statusCheckRollup"))
    review_decision = raw.get("reviewDecision") or ""
    is_draft = bool(raw.get("isDraft"))
    mergeable = raw.get("mergeable") or ""
    latest_reviews = raw.get("latestReviews") or None
    thread_counts = raw.get("_thread_counts")
    if thread_counts is None:
        thread_total: int | None = None
        thread_resolved: int | None = None
        all_threads_resolved: bool | None = None
    else:
        thread_total, thread_resolved = thread_counts
        all_threads_resolved = thread_total == 0 or thread_resolved >= thread_total
    superseded_by_new_commit = bool(raw.get("_superseded_by_new_commit", False))

    computed = compute_pr_state(
        is_draft=is_draft,
        review_decision=review_decision,
        ci_status=ci_status,
        mergeable=mergeable,
        latest_reviews=latest_reviews,
        all_threads_resolved=all_threads_resolved,
        superseded_by_new_commit=superseded_by_new_commit,
    )

    author = raw.get("author") or {}
    labels = _extract_pr_labels(raw)
    pr_assignees = _extract_pr_assignees(raw)
    commit_count = _count_pr_commits(raw)

    return {
        "number": raw["number"],
        "title": raw.get("title", ""),
        "branch": raw.get("headRefName", ""),
        "head_sha": raw.get("headRefOid", "") or "",
        "url": raw.get("url", ""),
        "state": raw.get("state", "OPEN"),
        "computed_state": computed,
        "state_label": _STATE_LABELS.get(computed, computed),
        "review_decision": review_decision,
        "ci_status": ci_status,
        "mergeable": mergeable,
        "is_draft": is_draft,
        "author": author.get("login", ""),
        "linked_issue": _extract_linked_issue(raw),
        "linked_issues": _extract_all_linked_issues(raw),
        "age_seconds": _age_seconds(raw.get("createdAt") or "", now),
        "updated_at": raw.get("updatedAt") or "",
        "labels": labels,
        "thread_total": thread_total,
        "thread_resolved": thread_resolved,
        "review_logins": _extract_review_logins(latest_reviews),
        "latest_reviews": latest_reviews or [],
        "latest_approval_at": _extract_latest_approval_at(latest_reviews),
        "additions": raw.get("additions") or 0,
        "deletions": raw.get("deletions") or 0,
        "changed_files": raw.get("changedFiles") or 0,
        "assignees": pr_assignees,
        "commit_count": commit_count,
    }


def _gate(name: str, *, enabled: bool, passed: bool, reason: str = "") -> dict:
    return {"name": name, "enabled": enabled, "passed": passed, "reason": reason}


def _check_ci_gate(enabled: bool, ci_status: str) -> dict:
    if not enabled:
        return _gate("ci_passed", enabled=False, passed=True)
    passed = ci_status == "passed"
    return _gate("ci_passed", enabled=True, passed=passed, reason="" if passed else f"CI status is '{ci_status}'")


def _check_coderabbit_from_pr_data(pr_data: dict) -> bool:
    """Check if CodeRabbit reviewed using pre-fetched review_logins from enriched PR data."""
    from sova.adapters.external_reviews import DEFAULT_CODERABBIT_AUTHORS

    review_logins = set(pr_data.get("review_logins") or [])
    return bool(review_logins & DEFAULT_CODERABBIT_AUTHORS)


def get_unresolved_thread_count(pr_data: dict) -> int | None:
    """Return the number of unresolved review threads from enriched PR data.

    Extracts from the cached PR data (thread_total, thread_resolved) without
    making additional API calls. Returns 0 when no threads exist or the keys
    are simply absent (e.g. a synthetic test dict or an older cache shape),
    and None only when a key is explicitly present with value None (fetch
    succeeded but the count is genuinely unknown), so unknown is never
    conflated with zero and vice versa.
    """
    if "thread_total" not in pr_data or "thread_resolved" not in pr_data:
        return 0
    total = pr_data.get("thread_total")
    resolved = pr_data.get("thread_resolved")
    if total is None or resolved is None:
        return None
    return max(0, total - resolved)


def _check_threads_from_pr_data(pr_data: dict) -> dict:
    """Check thread resolution using pre-fetched thread counts from enriched PR data."""
    unresolved = get_unresolved_thread_count(pr_data)
    if unresolved is None:
        return _gate(
            "threads_resolved",
            enabled=True,
            passed=False,
            reason="thread resolution state unknown, cannot verify",
        )
    total = pr_data.get("thread_total", 0)
    if unresolved == 0:
        return _gate("threads_resolved", enabled=True, passed=True)
    return _gate(
        "threads_resolved",
        enabled=True,
        passed=False,
        reason=f"{unresolved} of {total} threads unresolved",
    )


async def check_integration_gates(
    *,
    pr_data: dict,
    issue_number: str | None,
    config: ProjectConfig,
    project_dir: Path | None = None,
    sova_verdict: dict | None = None,
) -> dict:
    """Check all configured integration gates for a PR.

    Uses pre-fetched data from enriched PR dicts (review_logins, thread_total,
    thread_resolved).  check_coderabbit and check_threads inspect only the
    supplied pr_data and do not fall back to API calls when fields are absent.

    ``sova_verdict`` is the verdict already assembled by resolve_sova_verdict()
    for this PR.  When supplied it is used as-is: the gate that decides whether
    the Integrate button is enabled must read the same verdict that decided the
    button exists at all, otherwise the canonical assembly path (#991) and this
    gate can disagree on the same PR.  Callers without one (the standalone
    /gates endpoint) fall back to the DB-only lookup.

    Returns a dict with:
      - passed: bool (all enabled gates passed)
      - gates: list of {name, enabled, passed, reason}
    """
    gates_cfg = config.integration_gates

    # CI gate is synchronous: no API call needed
    ci_gate = _check_ci_gate(gates_cfg.ci_passed, pr_data.get("ci_status", "none"))

    # SOVA review gate: requires async DB query
    async def check_sova_review() -> dict:
        if not gates_cfg.sova_reviewed:
            return _gate("sova_reviewed", enabled=False, passed=True)
        if not issue_number and pr_data.get("number") is None:
            return _gate("sova_reviewed", enabled=True, passed=True, reason="No linked issue or PR (skipped)")

        verdict = sova_verdict
        if verdict is None:
            from sova.dashboard.services.agent_recovery import get_sova_review_verdict

            verdict = await get_sova_review_verdict(
                issue_number, pr_number=pr_data.get("number"), project_dir=project_dir
            )
        if not verdict.get("has_sova_review"):
            return _gate("sova_reviewed", enabled=True, passed=False, reason="No SOVA review found")
        v = verdict.get("verdict", "")
        if v == "approve":
            return _gate("sova_reviewed", enabled=True, passed=True)
        return _gate(
            "sova_reviewed",
            enabled=True,
            passed=False,
            reason=f"SOVA review verdict: {v} ({verdict.get('finding_count', 0)} findings)",
        )

    # CodeRabbit gate: inspects pre-fetched review_logins; no API fallback
    def check_coderabbit() -> dict:
        if not gates_cfg.coderabbit_reviewed:
            return _gate("coderabbit_reviewed", enabled=False, passed=True)
        if _check_coderabbit_from_pr_data(pr_data):
            return _gate("coderabbit_reviewed", enabled=True, passed=True)
        return _gate("coderabbit_reviewed", enabled=True, passed=False, reason="No CodeRabbit review found")

    # Threads gate: inspects pre-fetched thread counts; no API fallback
    def check_threads() -> dict:
        if not gates_cfg.threads_resolved:
            return _gate("threads_resolved", enabled=False, passed=True)
        return _check_threads_from_pr_data(pr_data)

    # Only SOVA review needs async; rest are synchronous using pre-fetched data
    sova_gate = await check_sova_review()
    cr_gate = check_coderabbit()
    thr_gate = check_threads()

    gates = [ci_gate, sova_gate, cr_gate, thr_gate]
    return {"passed": all(g["passed"] for g in gates), "gates": gates}


async def get_pr_mergeability_map() -> dict[int, str]:
    """Build {issue_number: mergeable_status} from open PRs.

    A PR may close multiple issues (via closingIssuesReferences); all linked
    issues receive the PR's mergeable status.  When multiple PRs reference the
    same issue, CONFLICTING wins (worst-case: if any PR conflicts, the issue is
    blocked).

    Returns an empty dict on any failure (fail-open).
    """
    try:
        prs = await list_open_prs_with_state()
    except (RuntimeError, OSError):
        log.debug("mergeability_map.fetch_failed", exc_info=True)
        return {}
    result: dict[int, str] = {}
    for pr in prs:
        status = pr.get("mergeable", "")
        for issue_num in pr.get("linked_issues") or []:
            existing = result.get(issue_num)
            if existing == "CONFLICTING" or status == "CONFLICTING":
                result[issue_num] = "CONFLICTING"
            else:
                result[issue_num] = status
    return result


async def list_open_prs_with_state(project_dir: Path | None = None, *, raise_on_error: bool = False) -> list[dict]:
    """List all open PRs with computed state. Cached per-repo for 120s.

    When raise_on_error=True, config/retrieval failures propagate instead of
    returning [] (so callers can distinguish "no PRs" from "retrieval failed").
    """
    from sova.config.loader import load_config
    from sova.dashboard.project_context import get_project_dir
    from sova.git.pr import get_pr_review_data, list_open_prs

    if project_dir is None:
        project_dir = get_project_dir() or Path.cwd()

    try:
        cfg = load_config(project_dir)
    except Exception:  # noqa: BLE001 (logged then optionally re-raised; the caller decides)
        log.warning("pr_service.config_load_failed", project_dir=str(project_dir), exc_info=True)
        if raise_on_error:
            raise
        return []

    if not cfg.github_repo:
        return []

    repo = cfg.github_repo
    cache_key = (repo, cfg.github_user)
    now = time.monotonic()
    cached = _pr_cache.get(cache_key)
    if cached and (now - cached[0]) < _PR_CACHE_TTL:
        return cached[1]
    raw_prs = await list_open_prs(repo=repo, github_user=cfg.github_user)

    pr_numbers = [p["number"] for p in raw_prs]
    try:
        review_data = await get_pr_review_data(pr_numbers, repo=repo, github_user=cfg.github_user)
    except (RuntimeError, OSError):
        log.warning("pr_service.thread_counts_failed", exc_info=True)
        review_data = {}
    for pr in raw_prs:
        rd = review_data.get(pr["number"])
        if rd is None:
            pr["_thread_counts"] = None
            pr["_superseded_by_new_commit"] = False
        else:
            pr["_thread_counts"] = (rd.thread_total, rd.thread_resolved)
            pr["_superseded_by_new_commit"] = rd.bot_cr_superseded

    wall_now = time.time()
    result = [_enrich_pr(pr, wall_now) for pr in raw_prs]
    result.sort(key=lambda p: p["number"], reverse=True)

    _pr_cache[cache_key] = (now, result)
    log.info("pr_service.refreshed", repo=repo, count=len(result))

    task = asyncio.ensure_future(_record_state_transitions(result, repo=repo, project_dir=project_dir))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)

    return result


async def _record_state_transitions(prs: list[dict], *, repo: str, project_dir: Path) -> None:
    """Detect state changes and write PREvent rows (fire-and-forget)."""
    from sova.db.models import PREvent
    from sova.db.session import get_session

    events_to_write: list[dict] = []
    for pr in prs:
        pr_num = pr["number"]
        state = pr["computed_state"]
        prev = _last_known_states.get(pr_num)
        _last_known_states[pr_num] = state

        if prev is None:
            continue
        if state == prev:
            continue

        event_type = _COMPUTED_TO_EVENT.get(state)
        if not event_type:
            continue

        events_to_write.append(
            {
                "pr_number": pr_num,
                "repo": repo,
                "event_type": event_type,
                "timestamp": (
                    datetime.fromisoformat(pr["updated_at"]) if pr.get("updated_at") else datetime.now(timezone.utc)
                ),
                "actor": pr.get("author", ""),
                "metadata_json": {"computed_state": state, "prev_state": prev},
            }
        )

    if not events_to_write:
        return

    try:
        for ev in events_to_write:
            try:
                async with await get_session(project_dir) as session:
                    session.add(PREvent(**ev))
                    await session.commit()
            except (OSError, RuntimeError, SQLAlchemyError):
                log.debug("pr_service.event_write_conflict", pr=ev.get("pr_number"), exc_info=True)
    except (OSError, RuntimeError, SQLAlchemyError):
        log.debug("pr_service.event_record_failed", exc_info=True)
