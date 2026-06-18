"""PR tracker service -- lists open PRs with computed lifecycle state."""

from __future__ import annotations

import re
import time
from datetime import datetime
from enum import StrEnum

from sova.utils.logging import get_logger

log = get_logger(component="dashboard.pr_service")

_ISSUE_LINK_RE = re.compile(r"(?:closes|fixes|resolves)\s+#(\d+)", re.IGNORECASE)

_PR_CACHE_TTL = 25  # seconds
_pr_cache: dict[str, tuple[float, list[dict]]] = {}


class ComputedPRState(StrEnum):
    DRAFT = "draft"
    CI_RUNNING = "ci_running"
    CI_FAILED = "ci_failed"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED_CI_GREEN = "approved_ci_green"
    APPROVED = "approved"
    AWAITING_REVIEW = "awaiting_review"


_STATE_LABELS: dict[str, str] = {
    "draft": "Draft",
    "ci_running": "CI Running",
    "ci_failed": "CI Failed",
    "changes_requested": "Changes Requested",
    "approved_ci_green": "Ready to Merge",
    "approved": "Approved",
    "awaiting_review": "Awaiting Review",
}


def parse_linked_issue(body: str | None) -> int | None:
    """Extract the first issue number from Closes/Fixes/Resolves #N in PR body."""
    if not body:
        return None
    m = _ISSUE_LINK_RE.search(body)
    return int(m.group(1)) if m else None


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
        if status in ("PENDING", "IN_PROGRESS", "QUEUED"):
            states.add("pending")
        elif conclusion in ("FAILURE", "ERROR", "TIMED_OUT", "STARTUP_FAILURE"):
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
    return "none"


def compute_pr_state(
    *,
    is_draft: bool,
    review_decision: str,
    ci_status: str,
    mergeable: str,
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
    return ComputedPRState.AWAITING_REVIEW


def _enrich_pr(raw: dict, now: float) -> dict:
    """Transform a raw gh pr list entry into a PR tracker dict."""
    body = raw.get("body") or ""
    ci_status = _summarize_ci(raw.get("statusCheckRollup"))
    review_decision = raw.get("reviewDecision") or ""
    is_draft = bool(raw.get("isDraft"))
    mergeable = raw.get("mergeable") or ""

    created = raw.get("createdAt") or ""
    age_seconds = 0
    if created:
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            age_seconds = int(now - dt.timestamp())
        except (ValueError, TypeError):
            pass

    computed = compute_pr_state(
        is_draft=is_draft,
        review_decision=review_decision,
        ci_status=ci_status,
        mergeable=mergeable,
    )

    author = raw.get("author") or {}
    labels = [lbl.get("name", "") for lbl in (raw.get("labels") or [])]

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
        "linked_issue": parse_linked_issue(body),
        "age_seconds": age_seconds,
        "labels": labels,
    }


async def list_open_prs_with_state() -> list[dict]:
    """List all open PRs with computed state. Cached per-repo for 25s."""
    from sova.config.loader import load_config
    from sova.dashboard.project_context import get_project_dir
    from sova.git.pr import list_open_prs

    project_dir = get_project_dir()
    if not project_dir:
        return []

    try:
        cfg = load_config(project_dir)
    except Exception:
        return []

    if not cfg.github_repo:
        return []

    repo = cfg.github_repo
    now = time.monotonic()
    cached = _pr_cache.get(repo)
    if cached and (now - cached[0]) < _PR_CACHE_TTL:
        return cached[1]

    raw_prs = await list_open_prs(repo=repo, github_user=cfg.github_user)
    wall_now = time.time()
    result = [_enrich_pr(pr, wall_now) for pr in raw_prs]
    result.sort(key=lambda p: p["number"], reverse=True)

    _pr_cache[repo] = (now, result)
    log.info("pr_service.refreshed", repo=repo, count=len(result))
    return result
