"""CodeRabbit rate-limit quota tracking service.

Tracks CodeRabbit review events to enforce per-hour quota limits.
Queries GitHub API for review history and caches locally in the DB.
Consumed by the dashboard (widget) and future PR throttle step (#294).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sova.adapters.external_reviews import _DEFAULT_CODERABBIT_AUTHORS
from sova.config.models import CodeRabbitQuotaConfig
from sova.db.models import CodeRabbitEvent
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="supervisor.coderabbit_quota")


@dataclass(frozen=True, slots=True)
class QuotaStatus:
    """Current CodeRabbit quota state."""

    enabled: bool
    reviews_in_window: int
    reviews_per_hour: int
    can_create_pr: bool
    next_available_minutes: float | None
    window_minutes: int


async def get_quota_status(
    session: AsyncSession,
    config: CodeRabbitQuotaConfig,
    *,
    project_slug: str = "",
) -> QuotaStatus:
    """Check current CodeRabbit quota availability."""
    if not config.enabled:
        return QuotaStatus(
            enabled=False,
            reviews_in_window=0,
            reviews_per_hour=config.reviews_per_hour,
            can_create_pr=True,
            next_available_minutes=None,
            window_minutes=config.window_minutes,
        )

    if config.reviews_per_hour == 0:
        return QuotaStatus(
            enabled=True,
            reviews_in_window=0,
            reviews_per_hour=0,
            can_create_pr=True,
            next_available_minutes=None,
            window_minutes=config.window_minutes,
        )

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=config.window_minutes)

    events = await _get_review_events_in_window(session, window_start, project_slug)
    count = len(events)
    can_create = count < config.reviews_per_hour

    next_available: float | None = None
    if not can_create and events:
        oldest = min(e.recorded_at for e in events)
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        slot_time = oldest + timedelta(minutes=config.window_minutes)
        remaining = (slot_time - now).total_seconds() / 60.0
        next_available = max(0.0, remaining)

    return QuotaStatus(
        enabled=True,
        reviews_in_window=count,
        reviews_per_hour=config.reviews_per_hour,
        can_create_pr=can_create,
        next_available_minutes=next_available,
        window_minutes=config.window_minutes,
    )


async def sync_from_github(
    session: AsyncSession,
    repo: str,
    config: CodeRabbitQuotaConfig,
    *,
    project_slug: str = "",
    github_user: str = "",
) -> int:
    """Fetch recent CodeRabbit reviews from GitHub and cache in DB.

    Returns the number of new events recorded.
    """
    if not config.enabled or not repo:
        return 0

    try:
        reviews = await _fetch_coderabbit_reviews_from_github(
            repo, github_user=github_user, window_minutes=config.window_minutes
        )
    except Exception:
        log.warning("sync_from_github.api_failed", repo=repo, exc_info=True)
        return 0

    if not reviews:
        return 0

    new_count = 0
    for review in reviews:
        inserted = await _upsert_event(
            session,
            pr_number=review["pr_number"],
            event_type="review",
            review_id=review["review_id"],
            recorded_at=review["submitted_at"],
            project_slug=project_slug,
        )
        if inserted:
            new_count += 1

    await session.commit()
    log.info("sync_from_github.done", repo=repo, new_events=new_count, total_fetched=len(reviews))
    return new_count


async def record_event(
    session: AsyncSession,
    *,
    pr_number: int,
    event_type: str,
    review_id: str,
    recorded_at: datetime | None = None,
    project_slug: str = "",
) -> bool:
    """Record a CodeRabbit event directly (e.g., from a webhook or step observation).

    Returns True if a new row was inserted, False if deduplicated.
    """
    if recorded_at is None:
        recorded_at = datetime.now(timezone.utc)
    inserted = await _upsert_event(session, pr_number, event_type, review_id, recorded_at, project_slug)
    await session.commit()
    return inserted


async def _get_review_events_in_window(
    session: AsyncSession,
    window_start: datetime,
    project_slug: str,
) -> list[CodeRabbitEvent]:
    """Query review events within the rolling window."""
    stmt = (
        select(CodeRabbitEvent)
        .where(
            CodeRabbitEvent.event_type == "review",
            CodeRabbitEvent.recorded_at > window_start,
            CodeRabbitEvent.project_slug == project_slug,
        )
        .order_by(CodeRabbitEvent.recorded_at)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _upsert_event(
    session: AsyncSession,
    pr_number: int,
    event_type: str,
    review_id: str,
    recorded_at: datetime,
    project_slug: str,
) -> bool:
    """Insert event if not already present. Returns True if inserted."""
    existing = await session.execute(
        select(CodeRabbitEvent.id).where(
            CodeRabbitEvent.review_id == review_id,
            CodeRabbitEvent.project_slug == project_slug,
        )
    )
    if existing.scalar_one_or_none() is not None:
        return False

    session.add(
        CodeRabbitEvent(
            pr_number=pr_number,
            event_type=event_type,
            review_id=review_id,
            recorded_at=recorded_at,
            project_slug=project_slug,
        )
    )
    await session.flush()
    return True


async def _fetch_coderabbit_reviews_from_github(
    repo: str,
    *,
    github_user: str = "",
    window_minutes: int = 60,
) -> list[dict]:
    """Fetch recent PRs and their CodeRabbit reviews from GitHub API.

    Only fetches PRs updated within ``window_minutes`` (default 60) to
    minimise API calls.  Returns dicts with: pr_number, review_id, submitted_at.
    """
    from sova.utils.gh import resolve_gh_env

    env = await resolve_gh_env(github_user) if github_user else None

    # Only fetch PRs updated within the quota window -- reduces API calls
    # from up to 30+20 to typically <10 for low-traffic repos.
    since = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

    pr_result = await run(
        "gh",
        "api",
        f"repos/{repo}/pulls",
        "-q",
        ".[].number",
        "--method",
        "GET",
        "-f",
        "state=all",
        "-f",
        "per_page=30",
        "-f",
        "sort=updated",
        "-f",
        "direction=desc",
        "-f",
        f"since={since}",
        env=env,
    )
    if not pr_result.success:
        log.warning("fetch_prs.failed", repo=repo, stderr=pr_result.stderr[:200])
        return []

    pr_numbers: list[int] = []
    for line in pr_result.stdout.strip().splitlines():
        line = line.strip()
        if line.isdigit():
            pr_numbers.append(int(line))

    if not pr_numbers:
        return []

    sem = asyncio.Semaphore(5)

    async def _fetch_limited(pr_num: int) -> list[dict]:
        async with sem:
            return await _fetch_reviews_for_pr(repo, pr_num, env=env)

    results = await asyncio.gather(
        *[_fetch_limited(pr_num) for pr_num in pr_numbers],
        return_exceptions=True,
    )
    reviews: list[dict] = []
    for result in results:
        if isinstance(result, list):
            reviews.extend(result)

    return reviews


async def _fetch_reviews_for_pr(
    repo: str,
    pr_number: int,
    *,
    env: dict[str, str] | None = None,
) -> list[dict]:
    """Fetch CodeRabbit reviews for a single PR."""
    result = await run(
        "gh",
        "api",
        f"repos/{repo}/pulls/{pr_number}/reviews",
        "--paginate",
        env=env,
    )
    if not result.success:
        log.debug("fetch_reviews.failed", pr=pr_number, stderr=result.stderr[:200])
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.debug("fetch_reviews.bad_json", pr=pr_number)
        return []

    if not isinstance(data, list):
        return []

    reviews: list[dict] = []
    for r in data:
        user = r.get("user", {})
        login = (user.get("login") or "").lower()
        if login not in _DEFAULT_CODERABBIT_AUTHORS:
            continue

        state = r.get("state", "")
        submitted_at_str = r.get("submitted_at", "")
        review_id = str(r.get("id", ""))

        if not state or not submitted_at_str or not review_id:
            continue

        # Only actual reviews count toward quota (not PENDING)
        if state.upper() == "PENDING":
            continue

        try:
            submitted_at = datetime.fromisoformat(submitted_at_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            log.debug("fetch_reviews.bad_date", pr=pr_number, date=submitted_at_str)
            continue

        reviews.append(
            {
                "pr_number": pr_number,
                "review_id": review_id,
                "submitted_at": submitted_at,
            }
        )

    return reviews
