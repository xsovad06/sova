"""Titles for the grouped run history, from the work-items snapshot or a bounded, cached tracker lookup.

A title is decoration: it never delays the table and never fails it. The snapshot is read without starting a
build, then a cache, then the tracker under one deadline for the whole request. A timeout, an error and a missing
title are the same answer (no title, so the page shows the bare number). The deadline covers everything a lookup
does (config read, adapter build, every ``get_task``), and the adapter is closed in the background so a hung client
cannot hold the response. A miss, and equally a failed or timed-out lookup, is remembered for a short window: while
a tracker is down or slow the next requests answer at once from what is known, and a later request retries.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from pathlib import Path

from sova.config.loader import load_config
from sova.dashboard.services.snapshot_service import peek_snapshot
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.work_titles")

# A title found is kept this long (titles rarely change); a miss only briefly, so a recovered tracker is retried soon.
TITLE_TTL_SECONDS = 3600.0
MISS_TTL_SECONDS = 30.0
# One deadline for every lookup a request starts, and at most this many at once (each is a ``gh`` subprocess).
LOOKUP_DEADLINE_SECONDS = 2.5
_LOOKUP_CONCURRENCY = 8
_CACHE_LIMIT = 2048

_Key = tuple[str, str, str]  # project, "issue" or "pr", number or tracker key
_cache: dict[_Key, tuple[float, str | None]] = {}
# Keys whose last lookup failed or ran out of time, with when the tracker may be asked again. Kept apart from
# ``_cache`` so a failure never overwrites (or renews) a title that was found earlier.
_retry_after: dict[_Key, float] = {}
# Closes still running after their request returned (held so the tasks are not garbage collected mid-flight).
_closing: set[asyncio.Task] = set()


def reset_title_cache() -> None:
    """Forget every cached title, miss and failure. Test-only."""
    _cache.clear()
    _retry_after.clear()


def _remember(key: _Key, title: str | None) -> None:
    if len(_cache) >= _CACHE_LIMIT:
        _cache.clear()
    _cache[key] = (time.monotonic(), title or None)
    _retry_after.pop(key, None)


def _remember_failure(key: _Key) -> None:
    if len(_retry_after) >= _CACHE_LIMIT:
        _retry_after.clear()
    _retry_after[key] = time.monotonic() + MISS_TTL_SECONDS


def _cached(key: _Key) -> tuple[bool, str | None]:
    """Whether the cache answers for ``key`` (no need to ask the tracker), and with what (None is no title)."""
    now = time.monotonic()
    entry = _cache.get(key)
    title = entry[1] if entry else None
    if _retry_after.get(key, 0.0) > now:
        return True, title
    if entry is None:
        return False, None
    ttl = TITLE_TTL_SECONDS if title else MISS_TTL_SECONDS
    return now - entry[0] < ttl, title


def _from_snapshot(project_dir: Path | None, issues: set[str], prs: set[int]) -> tuple[dict[str, str], dict[int, str]]:
    snapshot = peek_snapshot("work_items", project_dir)
    issue_titles: dict[str, str] = {}
    pr_titles: dict[int, str] = {}
    if snapshot is None:
        return issue_titles, pr_titles
    for item in snapshot.payload.get("items", []):
        title = item.get("title")
        if not title:
            continue
        issue = str(item.get("issue_number") or "")
        if issue in issues:
            issue_titles[issue] = title
        elif not issue and item.get("pr_number") in prs:
            pr_titles[item["pr_number"]] = title
    return issue_titles, pr_titles


def _close_in_background(adapter: object) -> None:
    """Close ``adapter`` without making the request wait for it (a hung client must not delay the table)."""
    from sova.adapters import close_adapter

    async def close() -> None:
        try:
            async with asyncio.timeout(LOOKUP_DEADLINE_SECONDS):
                await close_adapter(adapter)
        except Exception:  # noqa: BLE001 (closing a title lookup's adapter is best effort)
            log.warning("work_titles.close_failed", exc_info=True)

    task = asyncio.create_task(close())
    _closing.add(task)
    task.add_done_callback(_closing.discard)


async def _lookup_all(
    project_dir: Path | None, issues: list[str], prs: list[int], issue_titles: dict[str, str], pr_titles: dict[int, str]
) -> None:
    """Fill the two dicts from the tracker as answers arrive; whatever is still missing stays out.

    The caller bounds the whole call (config read, adapter build and every lookup) with one deadline.
    """
    from sova.adapters import create_adapter
    from sova.git.pr import get_pr_title_body_state

    config = await asyncio.to_thread(load_config, project_dir)
    adapter = create_adapter(config)
    gate = asyncio.Semaphore(_LOOKUP_CONCURRENCY)

    async def issue_title(issue: str) -> None:
        async with gate:
            issue_titles[issue] = (await adapter.get_task(issue)).title

    async def pr_title(pr: int) -> None:
        async with gate:
            pr_titles[pr] = (
                await get_pr_title_body_state(pr, repo=config.github_repo, github_user=config.github_user)
            )[0]

    try:
        results = await asyncio.gather(
            *(issue_title(i) for i in issues), *(pr_title(p) for p in prs), return_exceptions=True
        )
        for result in results:
            if isinstance(result, Exception):
                log.warning("work_titles.lookup_failed", error=repr(result))
    finally:
        _close_in_background(adapter)


async def resolve_titles(
    project_dir: Path | None, issues: Iterable[str], prs: Iterable[int]
) -> tuple[dict[str, str], dict[int, str]]:
    """Titles for these issues (number or tracker key) and PRs; one that has none is left out of the result."""
    wanted_issues, wanted_prs = set(issues), set(prs)
    issue_titles, pr_titles = _from_snapshot(project_dir, wanted_issues, wanted_prs)
    project = str(project_dir)
    missing_issues: list[str] = []
    missing_prs: list[int] = []
    # An expired title still shows, and is asked for again; it stays if the tracker does not answer.
    for issue in wanted_issues - issue_titles.keys():
        fresh, title = _cached((project, "issue", issue))
        if title:
            issue_titles[issue] = title
        if not fresh:
            missing_issues.append(issue)
    for pr in wanted_prs - pr_titles.keys():
        fresh, title = _cached((project, "pr", str(pr)))
        if title:
            pr_titles[pr] = title
        if not fresh:
            missing_prs.append(pr)
    if not (missing_issues or missing_prs):
        return issue_titles, pr_titles

    found_issues: dict[str, str] = {}
    found_prs: dict[int, str] = {}
    try:
        async with asyncio.timeout(LOOKUP_DEADLINE_SECONDS):
            await _lookup_all(project_dir, missing_issues, missing_prs, found_issues, found_prs)
    except TimeoutError:
        log.warning("work_titles.deadline_passed", asked=len(missing_issues) + len(missing_prs))
    except Exception:  # noqa: BLE001 (a title is decoration: any failure leaves bare numbers)
        log.warning("work_titles.lookup_unavailable", exc_info=True)
    # An answer is remembered as such (an empty title is the tracker's answer: no title). A lookup that failed or ran
    # out of time left no entry in found_*: it is remembered as unavailable for the miss window, so requests inside
    # it skip the tracker, and it neither renews nor drops a stale title, which keeps showing meanwhile.
    for issue in missing_issues:
        key = (project, "issue", issue)
        if issue in found_issues:
            _remember(key, found_issues[issue])
        else:
            _remember_failure(key)
    for pr in missing_prs:
        key = (project, "pr", str(pr))
        if pr in found_prs:
            _remember(key, found_prs[pr])
        else:
            _remember_failure(key)
    issue_titles.update({k: v for k, v in found_issues.items() if v})
    pr_titles.update({k: v for k, v in found_prs.items() if v})
    return issue_titles, pr_titles
