"""Work item verdict resolution: SOVA verdict fetching, parsing, caching.

GitHub review markers (``sova-review``, ``sova-addressed``) are the sole
source of truth for a PR's SOVA verdict: parse_review_history() computes
verdict, anchor commit, addressed state, and address-cycle count from a PR's
review history in one pass, and resolve_sova_verdict() is a thin adapter over
it (fetch the history, cache the result).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from sova.utils.logging import get_logger
from sova.utils.review_markers import SOVA_ADDRESSED_MARKER_RE, parse_verdict_marker

log = get_logger(component="dashboard.work_item")


# Verdict cache: {(project_key, pr_number): (monotonic_timestamp, verdict_dict)}
# Positive results (has_sova_review=True) are stable: a review verdict doesn't change.
# Negative results expire quickly so newly posted reviews are detected within 30s.
# The project key is part of the cache key because PR numbers are only unique
# within a repository: in multi-project mode (one server process serving several
# projects) a bare pr_number would let project A's verdict answer for project B's
# PR of the same number.
_sova_verdict_cache: dict[tuple[str, int], tuple[float, dict]] = {}
_VERDICT_CACHE_POSITIVE_TTL = 300.0  # 5 minutes
_VERDICT_CACHE_NEGATIVE_TTL = 30.0  # 30 seconds


def clear_verdict_cache() -> None:
    """Clear the SOVA verdict cache. Intended for testing and cache invalidation."""
    _sova_verdict_cache.clear()


def invalidate_verdict(project_dir: Path | None, pr_number: int | None) -> None:
    """Drop one PR's cached verdict so the next resolve_sova_verdict() call recomputes it.

    Called on agent exit (the auto-handoff circuit breaker check) so a
    just-completed address cycle's round count is reflected immediately rather
    than waiting out the cache TTL. A no-op when pr_number is None or the PR has
    no cache entry: there is nothing to invalidate in either case.
    """
    if pr_number is None:
        return
    _sova_verdict_cache.pop(_cache_key(project_dir, pr_number), None)


_NO_REVIEW: dict = {
    "has_sova_review": False,
    "verdict": None,
    "finding_count": 0,
    "reviewed_at": None,
    "review_head_sha": None,
    "address_cycles": 0,
}

# A verdict that could not be looked up at all (GitHub unreachable, or no
# adapter to name the repo) is reported as _NO_REVIEW plus this flag, rather
# than as a plain "no review found". The two are indistinguishable in the
# fields above, yet they must not be treated the same by every caller:
# "no review yet" means address_cycles really is 0, while "could not look"
# means the count is unknown, and a safety budget keyed on it has to fail
# closed instead of reading the unknown as zero. Display callers may keep
# ignoring the flag; see _check_address_review_circuit_breaker().
UNRESOLVED_KEY = "history_unavailable"


def _unresolved() -> dict:
    """Return a no-review verdict marked as "could not be looked up"."""
    return {**_NO_REVIEW, UNRESOLVED_KEY: True}


def parse_review_history(reviews: list[dict]) -> dict:
    """Compute a SOVA verdict from a PR's review history, scanning for markers only.

    ``reviews`` entries carry ``state``, ``body`` and ``submitted_at`` (ISO
    8601; sorts correctly as a plain string, so no datetime parsing is needed):
    the shape ``PRReviewData.review_history`` returns. DISMISSED reviews are
    skipped entirely: a dismissed review's body no longer reflects reviewer
    intent, so it counts toward neither the verdict scan nor ``address_cycles``.

    Scans newest-first by ``submitted_at`` (the ordering key; array position is
    not trusted). ``address_cycles`` is the number of ``sova-addressed`` markers
    found, each marking one completed address cycle, capped at whatever the
    last-30 GraphQL window returned (an accepted limitation for very long-lived
    PRs). The first ``sova-addressed`` marker encountered in the scan means
    every verdict marker older than it has already been addressed, so the
    reported verdict is "addressed" rather than the stale pre-fix value. A
    review body with neither marker (a human review, or a SOVA review posted
    before markers existed) is skipped without affecting the scan, not treated
    as an error.

    The reviewed commit comes from the marker's own ``sha=`` anchor, never from
    the commit GitHub recorded the review against: an anchor is what the
    reviewer claims it reviewed, which is the only thing a staleness check can
    act on.
    """
    ordered = sorted(
        (r for r in reviews if r.get("state") != "DISMISSED"),
        key=lambda r: r.get("submitted_at") or "",
        reverse=True,
    )

    address_cycles = sum(1 for r in ordered if SOVA_ADDRESSED_MARKER_RE.search(r.get("body") or ""))

    addressed_after_verdict = False
    for r in ordered:
        body = r.get("body") or ""
        if SOVA_ADDRESSED_MARKER_RE.search(body):
            addressed_after_verdict = True
            continue

        found = parse_verdict_marker(body)
        if found is None:
            continue
        verdict, sha = found
        return {
            "has_sova_review": True,
            "verdict": "addressed" if addressed_after_verdict else verdict,
            "finding_count": 0,
            "reviewed_at": r.get("submitted_at") or None,
            "review_head_sha": None if addressed_after_verdict else sha,
            "address_cycles": address_cycles,
        }

    return {**_NO_REVIEW, "address_cycles": address_cycles}


def _cache_key(project_dir: Path | None, pr_number: int) -> tuple[str, int]:
    """Build the per-project cache key for a PR number."""
    return (str(project_dir) if project_dir else "", pr_number)


def _cache_get(project_dir: Path | None, pr_number: int) -> dict | None:
    """Return a live cached verdict for this PR, or None when absent or expired."""
    entry = _sova_verdict_cache.get(_cache_key(project_dir, pr_number))
    if entry is None:
        return None
    ts, cached = entry
    ttl = _VERDICT_CACHE_POSITIVE_TTL if cached.get("has_sova_review") else _VERDICT_CACHE_NEGATIVE_TTL
    if time.monotonic() - ts >= ttl:
        return None
    return dict(cached)


def _cache_put(project_dir: Path | None, pr_number: int, verdict: dict) -> None:
    if len(_sova_verdict_cache) > 1000:
        _sova_verdict_cache.clear()
    _sova_verdict_cache[_cache_key(project_dir, pr_number)] = (time.monotonic(), dict(verdict))


def build_verdict_adapter(project_dir: Path | None = None, *, config: Any = None) -> Any:
    """Build the task adapter whose repo/github_user resolve_sova_verdict() needs.

    Returns None when construction fails (adapter config missing or
    misconfigured), which resolve_sova_verdict() reports as "no review found"
    rather than raising: every verdict lookup is best-effort. Pass ``config``
    when one is already loaded to skip a redundant read.
    """
    try:
        from sova.adapters import create_adapter
        from sova.config.loader import load_config

        return create_adapter(config if config is not None else load_config(project_dir))
    except Exception:  # noqa: BLE001 (the adapter only supplies the repo; fail open to no verdict)
        log.debug("work_items.verdict_adapter_build_failed", exc_info=True)
        return None


async def resolve_verdict_for_pr(
    issue_number: str | None,
    *,
    pr_number: int | None,
    project_dir: Path | None = None,
    config: Any = None,
) -> dict:
    """Resolve one PR's verdict, building the adapter resolve_sova_verdict() needs.

    Every single-PR caller (the integration-gate check, its standalone
    endpoint, the review-completed gate, auto-handoff) wants a verdict and has
    no adapter in hand, so each one otherwise repeats the same
    build-then-resolve preamble. The batch path keeps calling
    resolve_sova_verdict() directly because it builds one adapter for the
    whole gather instead of one per PR, and pre-warms the cache with a single
    batched fetch first (see _prewarm_verdict_cache()).
    """
    return await resolve_sova_verdict(
        issue_number,
        pr_number=pr_number,
        project_dir=project_dir,
        adapter=build_verdict_adapter(project_dir, config=config),
    )


async def _fetch_review_history(pr_number: int, *, repo: str, github_user: str) -> list[dict] | None:
    """Fetch this PR's review history, or None when GitHub could not answer.

    Reuses get_pr_review_data() (the same per-repo batch call that already
    fetches thread and bot-CR-review data) rather than a separate REST call,
    so a verdict costs the same GraphQL shape the dashboard already pays for.

    ``None`` is a distinct answer from ``[]``: get_pr_review_data() does not
    raise on failure, it maps the PR to None (whole call failed, or the PR was
    absent from the response), and collapsing that into an empty list would
    report "this PR has no SOVA review and zero address cycles" every time
    GitHub is unreachable. See UNRESOLVED_KEY for why that distinction has to
    survive as far as the caller.
    """
    from sova.git.pr import get_pr_review_data

    data = await get_pr_review_data([pr_number], repo=repo, github_user=github_user)
    review_data = data.get(pr_number)
    return review_data.review_history if review_data is not None else None


async def resolve_sova_verdict(
    issue_number: str | None,
    *,
    pr_number: int | None,
    project_dir: Path | None = None,
    adapter: Any = None,
    use_cache: bool = True,
) -> dict:
    """Resolve a PR's SOVA review verdict from its GitHub review history.

    Thin adapter over parse_review_history(): both the dashboard
    (_fetch_sova_verdicts) and the supervisor (_refine_in_review_action) call
    it so the same PR cannot yield two different verdict dicts, and therefore
    cannot resolve to two different next actions via resolve_next_action().

    ``adapter`` supplies the repo/github_user needed to fetch the review
    history; single-PR callers get it via resolve_verdict_for_pr(). Without one
    (adapter unavailable), or without a PR number at all, this fails open to
    "no review found" rather than raising, matching every other best-effort PR
    lookup in this module. A lookup that could not run (no adapter, or the
    fetch raised) is additionally tagged ``history_unavailable`` and is *not*
    cached: the negative TTL would otherwise pin an unknown count at 0 for 30s,
    and the next poll should retry rather than re-serve the non-answer.

    The cache stores the full computed verdict, including ``address_cycles``:
    unlike the old multi-source assembly, every field here comes from the same
    review-history fetch, so a cache hit is exactly as fresh as the verdict it
    carries. invalidate_verdict() (called right after an address cycle
    completes) is what keeps a served verdict from lagging a just-completed
    cycle, not a per-call recompute.
    """
    if pr_number is None:
        return dict(_NO_REVIEW)

    if use_cache:
        cached = _cache_get(project_dir, pr_number)
        if cached is not None:
            return cached

    repo = getattr(adapter, "repo", None)
    if not repo:
        return _unresolved()

    github_user = getattr(adapter, "github_user", "") or ""
    try:
        reviews = await _fetch_review_history(pr_number, repo=repo, github_user=github_user)
    except Exception:  # noqa: BLE001 (verdict lookup is best-effort; failure yields no verdict)
        log.debug("work_items.review_history_fetch_failed", issue=issue_number, pr=pr_number, exc_info=True)
        return _unresolved()
    if reviews is None:
        log.debug("work_items.review_history_unavailable", issue=issue_number, pr=pr_number)
        return _unresolved()
    verdict = parse_review_history(reviews)

    if use_cache:
        _cache_put(project_dir, pr_number, verdict)

    return verdict


async def _prewarm_verdict_cache(pr_numbers: list[int], *, project_dir: Path | None, adapter: Any) -> None:
    """Resolve a whole poll's worth of verdicts with one GraphQL call.

    get_pr_review_data() batches by repo, but resolve_sova_verdict() can only
    ask it for the single PR it was given, so the per-PR path costs one
    GraphQL round trip each. Seeding the cache here collapses a poll over N
    PRs into one call; the per-PR calls that follow are then cache hits.

    Best-effort and fail-open by construction: anything not seeded (call
    failed, PR absent from the response, no adapter) just falls through to the
    per-PR path, which is what ran before this existed. Already-cached PRs are
    skipped so a warm positive entry is not refetched on every poll.
    """
    repo = getattr(adapter, "repo", None)
    if not repo:
        return
    wanted = sorted({n for n in pr_numbers if _cache_get(project_dir, n) is None})
    if not wanted:
        return

    from sova.git.pr import get_pr_review_data

    try:
        data = await get_pr_review_data(wanted, repo=repo, github_user=getattr(adapter, "github_user", "") or "")
    except Exception:  # noqa: BLE001 (pre-warm is an optimization; the per-PR path remains the fallback)
        log.debug("work_items.verdict_prewarm_failed", prs=wanted, exc_info=True)
        return

    for pr_number, review_data in data.items():
        if review_data is not None:
            _cache_put(project_dir, pr_number, parse_review_history(review_data.review_history))


async def _fetch_sova_verdicts(
    prs_by_issue: dict[str, dict],
    unlinked_prs: list[dict] | None = None,
    project_dir: Path | None = None,
) -> dict[str, dict]:
    """Batch-fetch SOVA reviewer verdicts for all issues and unlinked PRs.

    Thin batching wrapper over resolve_sova_verdict(), the canonical assembly
    path shared with the supervisor. Scoped to the current PR number so
    verdicts from prior PR revisions are excluded.

    Returns a dict of {issue_number: verdict_dict} for linked PRs and
    {"pr:{number}": verdict_dict} for unlinked standalone PRs.
    """
    # Build the adapter once before the gather so blocking config/adapter construction
    # does not run per-PR inside asyncio.gather. Non-fatal: a None adapter yields
    # "no review found" for every PR in this batch rather than an error.
    _adapter = build_verdict_adapter(project_dir)

    pr_numbers = [pr["number"] for pr in prs_by_issue.values() if pr.get("number")]
    pr_numbers += [pr["number"] for pr in unlinked_prs or [] if pr.get("number")]
    await _prewarm_verdict_cache(pr_numbers, project_dir=project_dir, adapter=_adapter)

    async def fetch_one(key: str, issue_num: str | None, pr_number: int | None) -> tuple[str, dict]:
        try:
            verdict = await resolve_sova_verdict(
                issue_num,
                pr_number=pr_number,
                project_dir=project_dir,
                adapter=_adapter,
            )
        except Exception:  # noqa: BLE001 (verdict lookup spans the GitHub review history fetch; failure yields no verdict)
            log.debug("work_items.verdict_fetch_failed", issue=issue_num, pr=pr_number, exc_info=True)
            return key, _unresolved()
        return key, verdict

    tasks = [fetch_one(issue, issue, pr.get("number")) for issue, pr in prs_by_issue.items()]
    for pr in unlinked_prs or []:
        pr_num = pr.get("number")
        if pr_num:
            tasks.append(fetch_one(f"pr:{pr_num}", None, pr_num))

    results = await asyncio.gather(*tasks)
    return dict(results)
