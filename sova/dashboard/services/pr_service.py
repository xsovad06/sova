"""PR tracker service -- lists open PRs with computed lifecycle state."""

from __future__ import annotations

import re
import time
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.config.models import ProjectConfig

log = get_logger(component="dashboard.pr_service")

_ISSUE_LINK_RE = re.compile(r"(?:closes|fixes|resolves)\s+#(\d+)", re.IGNORECASE)
_JIRA_MARKDOWN_RE = re.compile(r"\[([A-Z]+-\d+)\]\(https?://")
_JIRA_PLAIN_RE = re.compile(r"JIRA:\s*https?://\S+/browse/[A-Z]+-(\d+)")

_PR_CACHE_TTL = 25  # seconds
_pr_cache: dict[str, tuple[float, list[dict]]] = {}


class ComputedPRState(StrEnum):
    DRAFT = "draft"
    CI_RUNNING = "ci_running"
    CI_FAILED = "ci_failed"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED_CI_GREEN = "approved_ci_green"
    APPROVED = "approved"
    REVIEW_ADDRESSED = "review_addressed"
    AWAITING_REVIEW = "awaiting_review"


_STATE_LABELS: dict[str, str] = {
    "draft": "Draft",
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
    """Extract linked issue from closingIssuesReferences (accurate) or PR body (fallback).

    closingIssuesReferences only contains real issues, not PRs, so it avoids
    the false positive where 'Closes #N' references another PR.
    """
    refs = raw.get("closingIssuesReferences") or []
    if refs:
        return refs[0].get("number")
    return parse_linked_issue(raw.get("body"))


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
            "CANCELLED",
            "ACTION_REQUIRED",
            "STALE",
        ):
            states.add("failed")
        elif conclusion == "SUCCESS":
            states.add("passed")
        elif conclusion in ("SKIPPED", "NEUTRAL"):
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


def compute_pr_state(
    *,
    is_draft: bool,
    review_decision: str,
    ci_status: str,
    mergeable: str,
    latest_reviews: list[dict] | None = None,
    all_threads_resolved: bool = False,
) -> str:
    """Derive a single computed state from PR signals."""
    if is_draft:
        return ComputedPRState.DRAFT
    if ci_status == "pending":
        return ComputedPRState.CI_RUNNING
    if ci_status == "failed":
        return ComputedPRState.CI_FAILED
    if review_decision == "CHANGES_REQUESTED":
        return ComputedPRState.CHANGES_REQUESTED
    if review_decision == "APPROVED" and ci_status == "passed" and mergeable == "MERGEABLE":
        return ComputedPRState.APPROVED_CI_GREEN
    if review_decision == "APPROVED":
        return ComputedPRState.APPROVED
    if latest_reviews:
        has_active_changes_requested = any(r.get("state") == "CHANGES_REQUESTED" for r in latest_reviews)
        if has_active_changes_requested:
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
    """Return the most recent APPROVED review's submittedAt timestamp, or None."""
    best: str | None = None
    for rev in latest_reviews or []:
        if rev.get("state") == "APPROVED":
            ts = rev.get("submittedAt") or ""
            if ts and (best is None or ts > best):
                best = ts
    return best


def _enrich_pr(raw: dict, now: float) -> dict:
    """Transform a raw gh pr list entry into a PR tracker dict."""
    ci_status = _summarize_ci(raw.get("statusCheckRollup"))
    review_decision = raw.get("reviewDecision") or ""
    is_draft = bool(raw.get("isDraft"))
    mergeable = raw.get("mergeable") or ""
    latest_reviews = raw.get("latestReviews") or None
    thread_total, thread_resolved = raw.get("_thread_counts", (0, 0))
    all_threads_resolved = thread_total > 0 and thread_resolved >= thread_total

    computed = compute_pr_state(
        is_draft=is_draft,
        review_decision=review_decision,
        ci_status=ci_status,
        mergeable=mergeable,
        latest_reviews=latest_reviews,
        all_threads_resolved=all_threads_resolved,
    )

    author = raw.get("author") or {}
    labels = [lbl.get("name", "") for lbl in (raw.get("labels") or [])]
    assignee_nodes = raw.get("assignees") or []
    pr_assignees = [a.get("login", "") for a in assignee_nodes if a.get("login")]
    commits_node = raw.get("commits") or []
    commit_count = len(commits_node) if isinstance(commits_node, list) else 0

    return {
        "number": raw["number"],
        "title": raw.get("title", ""),
        "branch": raw.get("headRefName", ""),
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
        "age_seconds": _age_seconds(raw.get("createdAt") or "", now),
        "updated_at": raw.get("updatedAt") or "",
        "labels": labels,
        "thread_total": thread_total,
        "thread_resolved": thread_resolved,
        "review_logins": _extract_review_logins(latest_reviews),
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


def _check_threads_from_pr_data(pr_data: dict) -> dict:
    """Check thread resolution using pre-fetched thread counts from enriched PR data."""
    total = pr_data.get("thread_total", 0)
    resolved = pr_data.get("thread_resolved", 0)
    if total == 0 or resolved >= total:
        return _gate("threads_resolved", enabled=True, passed=True)
    return _gate(
        "threads_resolved",
        enabled=True,
        passed=False,
        reason=f"{total - resolved} of {total} threads unresolved",
    )


async def check_integration_gates(
    *,
    pr_data: dict,
    issue_number: str | None,
    config: ProjectConfig,
) -> dict:
    """Check all configured integration gates for a PR.

    Uses pre-fetched data from enriched PR dicts (review_logins, thread_total,
    thread_resolved) when available, falling back to API calls only when needed.

    Returns a dict with:
      - passed: bool (all enabled gates passed)
      - gates: list of {name, enabled, passed, reason}
    """
    gates_cfg = config.integration_gates

    # CI gate is synchronous -- no API call needed
    ci_gate = _check_ci_gate(gates_cfg.ci_passed, pr_data.get("ci_status", "none"))

    # SOVA review gate -- requires async DB query
    async def check_sova_review() -> dict:
        if not gates_cfg.sova_reviewed:
            return _gate("sova_reviewed", enabled=False, passed=True)
        if not issue_number:
            return _gate("sova_reviewed", enabled=True, passed=True, reason="No linked issue (skipped)")
        from sova.dashboard.services.agent_recovery import get_sova_review_verdict

        verdict = await get_sova_review_verdict(issue_number)
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

    # CodeRabbit gate -- use pre-fetched review_logins when available
    def check_coderabbit() -> dict:
        if not gates_cfg.coderabbit_reviewed:
            return _gate("coderabbit_reviewed", enabled=False, passed=True)
        if _check_coderabbit_from_pr_data(pr_data):
            return _gate("coderabbit_reviewed", enabled=True, passed=True)
        return _gate("coderabbit_reviewed", enabled=True, passed=False, reason="No CodeRabbit review found")

    # Threads gate -- use pre-fetched thread counts when available
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


async def list_open_prs_with_state(author_filter_override: Literal["mine", "all"] | None = None) -> list[dict]:
    """List all open PRs with computed state. Cached per-repo for 25s.

    When *author_filter_override* is ``"mine"`` or ``"all"``, it takes
    precedence over the ``dashboard.pr_author_filter`` config value.
    """
    from sova.config.loader import load_config
    from sova.dashboard.project_context import get_project_dir
    from sova.git.pr import get_review_thread_counts, list_open_prs

    project_dir = get_project_dir()
    if not project_dir:
        return []

    try:
        cfg = load_config(project_dir)
    except Exception:
        log.warning("pr_service.config_load_failed", project_dir=str(project_dir), exc_info=True)
        return []

    if not cfg.github_repo:
        return []

    repo = cfg.github_repo
    effective_filter = author_filter_override or cfg.dashboard.pr_author_filter
    author = cfg.github_user if effective_filter == "mine" else None
    cache_key = f"{repo}:{author or ''}"
    now = time.monotonic()
    cached = _pr_cache.get(cache_key)
    if cached and (now - cached[0]) < _PR_CACHE_TTL:
        return cached[1]
    raw_prs = await list_open_prs(repo=repo, github_user=cfg.github_user, author=author)

    pr_numbers = [p["number"] for p in raw_prs]
    try:
        thread_counts = await get_review_thread_counts(pr_numbers, repo=repo, github_user=cfg.github_user)
    except Exception:
        log.warning("pr_service.thread_counts_failed", exc_info=True)
        thread_counts = {}
    for pr in raw_prs:
        pr["_thread_counts"] = thread_counts.get(pr["number"], (0, 0))

    wall_now = time.time()
    result = [_enrich_pr(pr, wall_now) for pr in raw_prs]
    result.sort(key=lambda p: p["number"], reverse=True)

    _pr_cache[cache_key] = (now, result)
    log.info("pr_service.refreshed", repo=repo, count=len(result))
    return result
