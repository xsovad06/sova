"""Work item verdict resolution: SOVA verdict fetching, parsing, caching.

Handles fetching SOVA review verdicts from labels, DB, and GitHub PR reviews.
Includes the verdict cache and GitHub review marker parsing.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.adapters.base import PRReview

log = get_logger(component="dashboard.work_item")


# Verdict cache: {pr_number: (monotonic_timestamp, verdict_dict)}
# Positive results (has_sova_review=True) are stable: a review verdict doesn't change.
# Negative results expire quickly so newly posted reviews are detected within 30s.
_sova_verdict_cache: dict[int, tuple[float, dict]] = {}
_VERDICT_CACHE_POSITIVE_TTL = 300.0  # 5 minutes
_VERDICT_CACHE_NEGATIVE_TTL = 30.0  # 30 seconds


def clear_verdict_cache() -> None:
    """Clear the SOVA verdict cache. Intended for testing and cache invalidation."""
    _sova_verdict_cache.clear()


_SOVA_VERDICT_LABEL_MAP: dict[str, str] = {
    "sova:approved": "approve",
    "sova:revise": "revise",
    "sova:block": "block",
}


def _extract_sova_verdict_from_labels(labels: list[str]) -> dict | None:
    """Extract a SOVA review verdict from issue labels.

    Returns a verdict dict matching get_sova_review_verdict()'s shape, or None
    if no sova:* label is present. If multiple sova:* labels exist (should not
    happen), takes the first match.
    """
    for label in labels:
        verdict = _SOVA_VERDICT_LABEL_MAP.get(label)
        if verdict is not None:
            # Labels carry no commit SHA: unanchored, not stale.
            return {
                "has_sova_review": True,
                "verdict": verdict,
                "finding_count": 0,
                "reviewed_at": None,
                "review_head_sha": None,
            }
    return None


_SOVA_MARKER_RE = re.compile(
    r"<!--\s*sova-review:\s*(approve|revise|block)(?:\s+sha=([0-9a-f]{7,40}))?\s*-->", re.IGNORECASE
)
# Matches the natural-language verdict line from /review-pr command output and older pipeline output.
_SOVA_VERDICT_LINE_RE = re.compile(
    r"^\*\*(Approve|Request changes|Block|Comment only)\b",
    re.IGNORECASE | re.MULTILINE,
)
_VERDICT_NORMALIZE = {
    "approve": "approve",
    "request changes": "revise",
    "block": "block",
    "comment only": "approve",
}


def _parse_sova_review_from_github(reviews: list[PRReview]) -> dict | None:
    """Scan GitHub PR reviews for a cross-instance SOVA review.

    Processes reviews newest-first. Skips DISMISSED reviews (superseded).
    Tries the machine-readable marker first, then falls back to detecting
    SOVA's characteristic body structure for reviews posted before the
    marker was introduced.

    Returns a verdict dict matching get_sova_review_verdict()'s shape, or None.
    """

    def _verdict_dict(verdict: str, submitted_at: str, review_head_sha: str | None) -> dict:
        return {
            "has_sova_review": True,
            "verdict": verdict,
            "finding_count": 0,
            "reviewed_at": submitted_at,
            "review_head_sha": review_head_sha,
        }

    for review in sorted(reviews, key=lambda r: r.submitted_at, reverse=True):
        if review.state == "DISMISSED":
            continue
        body = review.body or ""

        # Marker path: explicit machine-readable tag emitted by _format_findings_body.
        m = _SOVA_MARKER_RE.search(body)
        if m:
            return _verdict_dict(m.group(1).lower(), review.submitted_at, m.group(2))

        # Heuristic fallback: detect SOVA's characteristic review body structure.
        # Matches reviews from the /review-pr command before the marker was added.
        if "## PR Summary" in body and "## Verdict" in body:
            # Scope to the ## Verdict section to avoid matching bold lines in ## Findings.
            verdict_section = body.split("## Verdict", 1)[-1]
            verdict_match = _SOVA_VERDICT_LINE_RE.search(verdict_section)
            if verdict_match:
                verdict = _VERDICT_NORMALIZE.get(verdict_match.group(1).lower(), "revise")
                return _verdict_dict(verdict, review.submitted_at, None)

    return None


async def _fetch_github_review_fallback(pr_number: int, adapter: Any) -> dict | None:
    """Fetch GitHub reviews and scan for a cross-instance SOVA review marker.

    Called only when the local DB has no SOVA review record for this PR.
    This handles the case where a second SOVA instance (different machine/user)
    ran the review and its TaskRun lives in a different database.

    The adapter is built once by _fetch_sova_verdicts and shared across all PR lookups
    so that blocking config/adapter construction does not run per-PR inside asyncio.gather.
    """
    try:
        reviews = await adapter.get_pr_reviews(pr_number)
        return _parse_sova_review_from_github(reviews)
    except Exception:  # noqa: BLE001 (adapter raises AdapterError/ValueError too; stay fail-open)
        log.debug("work_items.github_review_fallback_failed", pr=pr_number, exc_info=True)
        return None


_NO_REVIEW: dict = {
    "has_sova_review": False,
    "verdict": None,
    "finding_count": 0,
    "reviewed_at": None,
    "run_status": None,
    "review_head_sha": None,
}


def _cache_get(pr_number: int) -> dict | None:
    """Return a live cached verdict for this PR, or None when absent or expired."""
    entry = _sova_verdict_cache.get(pr_number)
    if entry is None:
        return None
    ts, cached = entry
    ttl = _VERDICT_CACHE_POSITIVE_TTL if cached.get("has_sova_review") else _VERDICT_CACHE_NEGATIVE_TTL
    if time.monotonic() - ts >= ttl:
        return None
    return dict(cached)


def _cache_put(pr_number: int, verdict: dict) -> None:
    if len(_sova_verdict_cache) > 1000:
        _sova_verdict_cache.clear()
    _sova_verdict_cache[pr_number] = (time.monotonic(), dict(verdict))


def _merge_label_verdict(db_verdict: dict, label_verdict: dict | None) -> dict:
    """Reconcile the local DB verdict with the cross-machine sova:* issue label.

    The two sources answer the same question with different strengths. The DB
    record is strictly richer: it carries the reviewed commit SHA (#987), the
    finding counts, and whether an address cycle has since superseded the
    review (#988). The label is coarser (verdict value only, no anchor) but is
    the only source that survives a review run on another machine.

    So the label wins only where it actually adds information: when the local
    DB has no record at all, or when it disagrees with the DB (which means some
    other instance reviewed more recently than anything this machine knows
    about). When the DB agrees, or reports "addressed", the DB wins, because a
    completed address cycle never clears the reviewer's label and a label taken
    at face value there would re-route an already-addressed PR back to
    address-review.
    """
    if label_verdict is None:
        return db_verdict
    if not db_verdict.get("has_sova_review"):
        return label_verdict
    db_value = db_verdict.get("verdict")
    if db_value in ("addressed", label_verdict.get("verdict")):
        return db_verdict
    return label_verdict


async def resolve_sova_verdict(
    issue_number: str | None,
    *,
    pr_number: int | None,
    project_dir: Path | None = None,
    issue_labels: list[str] | None = None,
    fallback_adapter: Any = None,
    use_cache: bool = True,
) -> dict:
    """Assemble a SOVA review verdict from every available source.

    This is the single canonical assembly path: both the dashboard
    (_fetch_sova_verdicts) and the supervisor (_refine_in_review_action) call
    it so the same PR cannot yield two different verdict dicts, and therefore
    cannot resolve to two different next actions via resolve_next_action().

    Sources, in order: the PR-keyed verdict cache, the local DB
    (get_sova_review_verdict), the issue's sova:* label reconciled against the
    DB by _merge_label_verdict(), and finally a GitHub PR review marker scan
    for reviews posted by an instance whose DB this machine cannot see.
    """
    from sova.dashboard.services.agent_recovery import get_sova_review_verdict

    if pr_number is not None and use_cache:
        cached = _cache_get(pr_number)
        if cached is not None:
            return cached

    try:
        verdict = await get_sova_review_verdict(issue_number, pr_number=pr_number, project_dir=project_dir)
    except Exception:  # noqa: BLE001 (verdict lookup spans DB and tracker; failure yields no verdict)
        log.debug("work_items.db_verdict_failed", issue=issue_number, pr=pr_number, exc_info=True)
        verdict = dict(_NO_REVIEW)

    verdict = _merge_label_verdict(verdict, _extract_sova_verdict_from_labels(issue_labels or []))

    if not verdict.get("has_sova_review") and pr_number is not None and fallback_adapter is not None:
        gh_verdict = await _fetch_github_review_fallback(pr_number, fallback_adapter)
        if gh_verdict is not None:
            verdict = gh_verdict

    if pr_number is not None and use_cache:
        _cache_put(pr_number, verdict)

    return verdict


async def _fetch_sova_verdicts(
    prs_by_issue: dict[str, dict],
    unlinked_prs: list[dict] | None = None,
    project_dir: Path | None = None,
    labels_by_issue: dict[str, list[str]] | None = None,
) -> dict[str, dict]:
    """Batch-fetch SOVA reviewer verdicts for all issues and unlinked PRs.

    Thin batching wrapper over resolve_sova_verdict(), the canonical assembly
    path shared with the supervisor. Scoped to the current PR number so
    verdicts from prior PR revisions are excluded.

    Returns a dict of {issue_number: verdict_dict} for linked PRs and
    {"pr:{number}": verdict_dict} for unlinked standalone PRs.
    """
    # Build the adapter once before the gather so blocking config/adapter construction
    # does not run per-PR inside asyncio.gather. Non-fatal: if this fails the fallback
    # is simply skipped for all PRs in this batch.
    _fallback_adapter: Any = None
    try:
        from sova.adapters import create_adapter
        from sova.config.loader import load_config

        cfg = load_config(project_dir)
        _fallback_adapter = create_adapter(cfg)
    except Exception:  # noqa: BLE001 (fallback adapter is optional; verdict lookup uses other sources)
        log.debug("work_items.github_fallback_adapter_build_failed", exc_info=True)

    async def fetch_one(key: str, issue_num: str | None, pr_number: int | None) -> tuple[str, dict]:
        labels = labels_by_issue.get(issue_num, []) if (issue_num and labels_by_issue) else []
        try:
            verdict = await resolve_sova_verdict(
                issue_num,
                pr_number=pr_number,
                project_dir=project_dir,
                issue_labels=labels,
                fallback_adapter=_fallback_adapter,
            )
        except Exception:  # noqa: BLE001 (verdict lookup spans labels, DB and GitHub; failure yields no verdict)
            log.debug("work_items.verdict_fetch_failed", issue=issue_num, pr=pr_number, exc_info=True)
            return key, dict(_NO_REVIEW)
        return key, verdict

    tasks = [fetch_one(issue, issue, pr.get("number")) for issue, pr in prs_by_issue.items()]
    for pr in unlinked_prs or []:
        pr_num = pr.get("number")
        if pr_num:
            tasks.append(fetch_one(f"pr:{pr_num}", None, pr_num))

    results = await asyncio.gather(*tasks)
    return dict(results)
