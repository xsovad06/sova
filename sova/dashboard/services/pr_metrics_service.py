"""PR lifecycle metrics: backfill from GitHub and aggregate for dashboard."""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from sova.utils.logging import get_logger

log = get_logger(component="dashboard.pr_metrics")

_CACHE_TTL = 300  # 5 minutes


class PRMetricsService:
    """Computes PR lifecycle metrics from PREvent records."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, Any]] = {}
        self._refresh_lock = asyncio.Lock()

    async def get_summary(self, *, days: int = 90, force: bool = False) -> dict:
        cache_key = f"summary:{days}"
        cached = self._get_cached(cache_key, force=force)
        if cached is not None:
            return cached

        async with self._refresh_lock:
            cached = self._get_cached(cache_key, force=force)
            if cached is not None:
                return cached

            result = await self._compute_summary(days)
            self._set_cached(cache_key, result)
            return result

    async def get_trends(self, *, days: int = 90, force: bool = False) -> list[dict]:
        cache_key = f"trends:{days}"
        cached = self._get_cached(cache_key, force=force)
        if cached is not None:
            return cached

        async with self._refresh_lock:
            cached = self._get_cached(cache_key, force=force)
            if cached is not None:
                return cached

            result = await self._compute_trends(days)
            self._set_cached(cache_key, result)
            return result

    async def get_author_stats(self, *, days: int = 90, force: bool = False) -> list[dict]:
        cache_key = f"authors:{days}"
        cached = self._get_cached(cache_key, force=force)
        if cached is not None:
            return cached

        async with self._refresh_lock:
            cached = self._get_cached(cache_key, force=force)
            if cached is not None:
                return cached

            result = await self._compute_author_stats(days)
            self._set_cached(cache_key, result)
            return result

    def invalidate(self) -> None:
        self._cache.clear()

    def _get_cached(self, key: str, *, force: bool = False) -> dict | list | None:
        if force:
            return None
        entry = self._cache.get(key)
        if entry and (time.monotonic() - entry[0]) < _CACHE_TTL:
            return entry[1]
        return None

    def _set_cached(self, key: str, value: dict | list) -> None:
        self._cache[key] = (time.monotonic(), value)

    async def _load_events(self, days: int) -> list[Any]:
        from sova.dashboard.project_context import get_project_dir
        from sova.db.models import PREvent
        from sova.db.session import get_session

        project_dir = get_project_dir() or Path.cwd()
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        async with await get_session(project_dir) as session:
            stmt = select(PREvent).where(PREvent.timestamp >= cutoff).order_by(PREvent.timestamp)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def _compute_summary(self, days: int) -> dict:
        events = await self._load_events(days)
        if not events:
            return _empty_summary()

        pr_events: dict[int, dict[str, datetime]] = defaultdict(dict)
        pr_metadata: dict[int, dict] = {}
        actors: dict[int, set[str]] = defaultdict(set)

        for ev in events:
            existing = pr_events[ev.pr_number].get(ev.event_type)
            if ev.event_type in ("reviewed", "approved"):
                if existing is None or ev.timestamp < existing:
                    pr_events[ev.pr_number][ev.event_type] = ev.timestamp
            else:
                pr_events[ev.pr_number][ev.event_type] = ev.timestamp
            if ev.actor:
                actors[ev.pr_number].add(ev.actor)
            if ev.metadata_json:
                pr_metadata[ev.pr_number] = ev.metadata_json

        cycle_times: list[float] = []
        wait_to_review: list[float] = []
        wait_to_merge: list[float] = []
        total_additions = 0
        total_deletions = 0
        merged_count = 0
        closed_count = 0

        for pr_num, evts in pr_events.items():
            opened_at = evts.get("opened")
            merged_at = evts.get("merged")
            closed_at = evts.get("closed")
            first_review = evts.get("reviewed") or evts.get("approved")
            approved_at = evts.get("approved")

            if opened_at and merged_at:
                cycle_times.append((merged_at - opened_at).total_seconds() / 3600)
                merged_count += 1

            if closed_at and not merged_at:
                closed_count += 1

            if opened_at and first_review:
                wait_to_review.append((first_review - opened_at).total_seconds() / 3600)

            if approved_at and merged_at:
                wait_to_merge.append((merged_at - approved_at).total_seconds() / 3600)

            if merged_at:
                meta = pr_metadata.get(pr_num, {})
                total_additions += meta.get("additions", 0)
                total_deletions += meta.get("deletions", 0)

        reviewer_counts = [len(a) - 1 for a in actors.values() if len(a) > 1]

        weeks = max(days / 7, 1)
        return {
            "cycle_time_median_hours": round(statistics.median(cycle_times), 1) if cycle_times else None,
            "wait_to_review_median_hours": round(statistics.median(wait_to_review), 1) if wait_to_review else None,
            "wait_to_merge_median_hours": round(statistics.median(wait_to_merge), 1) if wait_to_merge else None,
            "throughput_per_week": round(merged_count / weeks, 1),
            "reviewers_per_pr": round(statistics.mean(reviewer_counts), 1) if reviewer_counts else None,
            "total_prs_merged": merged_count,
            "total_prs_closed": closed_count,
            "total_additions": total_additions,
            "total_deletions": total_deletions,
        }

    async def _compute_trends(self, days: int) -> list[dict]:
        events = await self._load_events(days)
        if not events:
            return []

        pr_events: dict[int, dict[str, datetime]] = defaultdict(dict)
        pr_actors: dict[int, set[str]] = defaultdict(set)

        for ev in events:
            existing = pr_events[ev.pr_number].get(ev.event_type)
            if ev.event_type in ("reviewed", "approved"):
                if existing is None or ev.timestamp < existing:
                    pr_events[ev.pr_number][ev.event_type] = ev.timestamp
            else:
                pr_events[ev.pr_number][ev.event_type] = ev.timestamp
            if ev.actor:
                pr_actors[ev.pr_number].add(ev.actor)

        monthly: dict[str, dict] = defaultdict(
            lambda: {
                "merged_count": 0,
                "closed_count": 0,
                "cycle_times": [],
                "reviewer_counts": [],
            }
        )

        for pr_num, evts in pr_events.items():
            merged_at = evts.get("merged")
            opened_at = evts.get("opened")
            closed_at = evts.get("closed")

            if merged_at:
                month_key = merged_at.strftime("%Y-%m")
                monthly[month_key]["merged_count"] += 1
                if opened_at:
                    ct = (merged_at - opened_at).total_seconds() / 3600
                    monthly[month_key]["cycle_times"].append(ct)
                reviewer_count = len(pr_actors.get(pr_num, set())) - 1
                if reviewer_count > 0:
                    monthly[month_key]["reviewer_counts"].append(reviewer_count)
            elif closed_at:
                month_key = closed_at.strftime("%Y-%m")
                monthly[month_key]["closed_count"] += 1

        result = []
        for month_key in sorted(monthly.keys()):
            data = monthly[month_key]
            ct_list = data["cycle_times"]
            rv_list = data["reviewer_counts"]
            result.append(
                {
                    "month": month_key,
                    "merged_count": data["merged_count"],
                    "closed_count": data["closed_count"],
                    "avg_cycle_time_hours": round(statistics.mean(ct_list), 1) if ct_list else None,
                    "avg_reviewers": round(statistics.mean(rv_list), 1) if rv_list else None,
                }
            )
        return result

    async def _compute_author_stats(self, days: int) -> list[dict]:
        events = await self._load_events(days)
        if not events:
            return []

        pr_events: dict[int, dict[str, datetime]] = defaultdict(dict)
        pr_authors: dict[int, str] = {}
        pr_metadata: dict[int, dict] = {}

        for ev in events:
            pr_events[ev.pr_number][ev.event_type] = ev.timestamp
            if ev.event_type == "opened" and ev.actor:
                pr_authors[ev.pr_number] = ev.actor
            if ev.metadata_json:
                pr_metadata[ev.pr_number] = ev.metadata_json

        author_data: dict[str, dict] = defaultdict(
            lambda: {
                "prs_merged": 0,
                "cycle_times": [],
                "additions": 0,
                "deletions": 0,
            }
        )

        for pr_num, evts in pr_events.items():
            merged_at = evts.get("merged")
            opened_at = evts.get("opened")
            if not merged_at:
                continue

            author = pr_authors.get(pr_num, "unknown")
            author_data[author]["prs_merged"] += 1
            if opened_at:
                ct = (merged_at - opened_at).total_seconds() / 3600
                author_data[author]["cycle_times"].append(ct)

            meta = pr_metadata.get(pr_num, {})
            author_data[author]["additions"] += meta.get("additions", 0)
            author_data[author]["deletions"] += meta.get("deletions", 0)

        result = []
        for login, data in sorted(author_data.items(), key=lambda x: x[1]["prs_merged"], reverse=True):
            ct_list = data["cycle_times"]
            result.append(
                {
                    "login": login,
                    "prs_merged": data["prs_merged"],
                    "avg_cycle_time_hours": round(statistics.mean(ct_list), 1) if ct_list else None,
                    "additions": data["additions"],
                    "deletions": data["deletions"],
                }
            )
        return result


def _empty_summary() -> dict:
    return {
        "cycle_time_median_hours": None,
        "wait_to_review_median_hours": None,
        "wait_to_merge_median_hours": None,
        "throughput_per_week": 0,
        "reviewers_per_pr": None,
        "total_prs_merged": 0,
        "total_prs_closed": 0,
        "total_additions": 0,
        "total_deletions": 0,
    }


def _extract_pr_events(pr: dict, *, repo: str, cutoff: datetime) -> list[dict]:
    """Build lifecycle event dicts from a single GitHub PR record."""
    pr_num = pr["number"]
    author_login = pr.get("author", {}).get("login", "")
    meta = {
        "additions": pr.get("additions", 0),
        "deletions": pr.get("deletions", 0),
        "changed_files": pr.get("changedFiles", 0),
        "branch": pr.get("headRefName", ""),
        "title": pr.get("title", ""),
    }

    created_at = _parse_gh_ts(pr.get("createdAt"))
    if not created_at or created_at < cutoff:
        return []

    result: list[dict] = [
        {
            "pr_number": pr_num,
            "repo": repo,
            "event_type": "opened",
            "timestamp": created_at,
            "actor": author_login,
            "metadata_json": meta,
        }
    ]

    reviews = pr.get("latestReviews") or []
    first_approve_at: datetime | None = None
    first_approver: str = ""
    for rev in reviews:
        rev_ts = _parse_gh_ts(rev.get("submittedAt"))
        if not rev_ts:
            continue
        reviewer = rev.get("author", {}).get("login", "")
        if rev.get("state") == "APPROVED" and (first_approve_at is None or rev_ts < first_approve_at):
            first_approve_at = rev_ts
            first_approver = reviewer
        result.append(
            {
                "pr_number": pr_num,
                "repo": repo,
                "event_type": "reviewed",
                "timestamp": rev_ts,
                "actor": reviewer,
                "metadata_json": {"review_state": rev.get("state", "")},
            }
        )

    if first_approve_at:
        result.append(
            {
                "pr_number": pr_num,
                "repo": repo,
                "event_type": "approved",
                "timestamp": first_approve_at,
                "actor": first_approver,
            }
        )

    merged_at = _parse_gh_ts(pr.get("mergedAt"))
    if merged_at:
        result.append(
            {
                "pr_number": pr_num,
                "repo": repo,
                "event_type": "merged",
                "timestamp": merged_at,
                "actor": author_login,
                "metadata_json": meta,
            }
        )

    closed_at = _parse_gh_ts(pr.get("closedAt"))
    if closed_at and not merged_at:
        result.append(
            {
                "pr_number": pr_num,
                "repo": repo,
                "event_type": "closed",
                "timestamp": closed_at,
                "actor": author_login,
            }
        )

    return result


async def backfill_pr_events(*, repo: str, days: int = 90, github_user: str = "") -> int:
    """Backfill PR lifecycle events from GitHub history.

    Returns the number of events created.
    """
    from sova.dashboard.project_context import get_project_dir
    from sova.db.models import PREvent
    from sova.db.session import get_session
    from sova.utils.gh import resolve_gh_env
    from sova.utils.shell import run

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    events: list[dict] = []

    fields = (
        "number,title,createdAt,mergedAt,closedAt,author,additions,deletions,changedFiles,latestReviews,headRefName"
    )

    env = await resolve_gh_env(github_user)

    for state in ("merged", "closed"):
        cmd = ["gh", "pr", "list", "--repo", repo, "--state", state, "--json", fields, "--limit", "200"]
        result = await run(*cmd, env=env)
        if not result.success:
            log.warning("pr_metrics.backfill_failed", state=state, stderr=result.stderr[:200])
            continue

        try:
            prs = json.loads(result.stdout)
        except json.JSONDecodeError:
            log.warning("pr_metrics.backfill_parse_failed", state=state)
            continue

        for pr in prs:
            events.extend(_extract_pr_events(pr, repo=repo, cutoff=cutoff))

    if not events:
        return 0

    def _naive(ts: datetime) -> datetime:
        return ts.replace(tzinfo=None) if ts.tzinfo else ts

    project_dir = get_project_dir() or Path.cwd()
    async with await get_session(project_dir) as session:
        existing_rows = await session.execute(
            select(PREvent.pr_number, PREvent.repo, PREvent.event_type, PREvent.timestamp).where(PREvent.repo == repo)
        )
        existing_keys: set[tuple[int, str, str, datetime]] = {
            (row.pr_number, row.repo, row.event_type, _naive(row.timestamp)) for row in existing_rows
        }

        created = 0
        for ev in events:
            key = (ev["pr_number"], ev["repo"], ev["event_type"], _naive(ev["timestamp"]))
            if key in existing_keys:
                continue
            existing_keys.add(key)
            session.add(PREvent(**ev))
            created += 1
        if created:
            await session.commit()

    log.info("pr_metrics.backfill_complete", events_created=created, total_candidates=len(events))
    if created:
        _service.invalidate()
    return created


def _parse_gh_ts(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


_service = PRMetricsService()


async def get_summary(*, days: int = 90, force: bool = False) -> dict:
    return await _service.get_summary(days=days, force=force)


async def get_trends(*, days: int = 90, force: bool = False) -> list[dict]:
    return await _service.get_trends(days=days, force=force)


async def get_author_stats(*, days: int = 90, force: bool = False) -> list[dict]:
    return await _service.get_author_stats(days=days, force=force)
