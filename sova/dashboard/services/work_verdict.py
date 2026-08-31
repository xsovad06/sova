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
            # Epoch sentinel: _is_verdict_stale() treats any real human approval as newer.
            return {
                "has_sova_review": True,
                "verdict": verdict,
                "finding_count": 0,
                "reviewed_at": "1970-01-01T00:00:00Z",
            }
    return None


_SOVA_MARKER_RE = re.compile(r"<!--\s*sova-review:\s*(approve|revise|block)\s*-->", re.IGNORECASE)
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

    def _verdict_dict(verdict: str, submitted_at: str) -> dict:
        return {"has_sova_review": True, "verdict": verdict, "finding_count": 0, "reviewed_at": submitted_at}

    for review in sorted(reviews, key=lambda r: r.submitted_at, reverse=True):
        if review.state == "DISMISSED":
            continue
        body = review.body or ""

        # Marker path: explicit machine-readable tag emitted by _format_findings_body.
        m = _SOVA_MARKER_RE.search(body)
        if m:
            return _verdict_dict(m.group(1).lower(), review.submitted_at)

        # Heuristic fallback: detect SOVA's characteristic review body structure.
        # Matches reviews from the /review-pr command before the marker was added.
        if "## PR Summary" in body and "## Verdict" in body:
            # Scope to the ## Verdict section to avoid matching bold lines in ## Findings.
            verdict_section = body.split("## Verdict", 1)[-1]
            verdict_match = _SOVA_VERDICT_LINE_RE.search(verdict_section)
            if verdict_match:
                verdict = _VERDICT_NORMALIZE.get(verdict_match.group(1).lower(), "revise")
                return _verdict_dict(verdict, review.submitted_at)

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
    except Exception:
        log.debug("work_items.github_review_fallback_failed", pr=pr_number, exc_info=True)
        return None


async def _fetch_sova_verdicts(
    prs_by_issue: dict[str, dict],
    unlinked_prs: list[dict] | None = None,
    project_dir: Path | None = None,
    labels_by_issue: dict[str, list[str]] | None = None,
) -> dict[str, dict]:
    """Batch-fetch SOVA reviewer verdicts for all issues and unlinked PRs.

    Checks issue labels first (sova:approved/revise/block) as the primary source,
    then falls back to DB lookup and GitHub review marker scan.

    Scoped to the current PR number so verdicts from prior PR revisions are excluded.
    Returns a dict of {issue_number: verdict_dict} for linked PRs and
    {"pr:{number}": verdict_dict} for unlinked standalone PRs.
    """
    from sova.dashboard.services.agent_recovery import get_sova_review_verdict

    # Build the adapter once before the gather so blocking config/adapter construction
    # does not run per-PR inside asyncio.gather. Non-fatal: if this fails the fallback
    # is simply skipped for all PRs in this batch.
    _fallback_adapter: Any = None
    try:
        from sova.adapters import create_adapter
        from sova.config.loader import load_config

        cfg = load_config(project_dir)
        _fallback_adapter = create_adapter(cfg)
    except Exception:
        log.debug("work_items.github_fallback_adapter_build_failed", exc_info=True)

    async def fetch_one(key: str, issue_num: str | None, pr_number: int | None) -> tuple[str, dict]:
        try:
            # Primary source: sova:* label on the issue (zero-cost, already fetched).
            if issue_num and labels_by_issue:
                issue_labels = labels_by_issue.get(issue_num, [])
                label_verdict = _extract_sova_verdict_from_labels(issue_labels)
                if label_verdict is not None:
                    if pr_number is not None:
                        if len(_sova_verdict_cache) > 1000:
                            _sova_verdict_cache.clear()
                        _sova_verdict_cache[pr_number] = (time.monotonic(), label_verdict)
                    return key, label_verdict

            if pr_number is not None:
                cached_entry = _sova_verdict_cache.get(pr_number)
                if cached_entry is not None:
                    ts, cached = cached_entry
                    ttl = _VERDICT_CACHE_POSITIVE_TTL if cached.get("has_sova_review") else _VERDICT_CACHE_NEGATIVE_TTL
                    if time.monotonic() - ts < ttl:
                        return key, dict(cached)

            # Fallback: DB lookup + GitHub review marker scan.
            verdict = await get_sova_review_verdict(issue_num, pr_number=pr_number, project_dir=project_dir)
            if not verdict.get("has_sova_review") and pr_number is not None and _fallback_adapter is not None:
                gh_verdict = await _fetch_github_review_fallback(pr_number, _fallback_adapter)
                if gh_verdict is not None:
                    verdict = gh_verdict

            if pr_number is not None:
                if len(_sova_verdict_cache) > 1000:
                    _sova_verdict_cache.clear()
                _sova_verdict_cache[pr_number] = (time.monotonic(), verdict)

            return key, verdict
        except Exception:
            log.debug("work_items.verdict_fetch_failed", issue=issue_num, pr=pr_number, exc_info=True)
            return key, {"has_sova_review": False, "verdict": None, "finding_count": 0, "reviewed_at": None}

    tasks = [fetch_one(issue, issue, pr.get("number")) for issue, pr in prs_by_issue.items()]
    for pr in unlinked_prs or []:
        pr_num = pr.get("number")
        if pr_num:
            tasks.append(fetch_one(f"pr:{pr_num}", None, pr_num))

    results = await asyncio.gather(*tasks)
    return dict(results)
