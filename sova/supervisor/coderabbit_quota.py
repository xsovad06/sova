"""CodeRabbit rate-limit quota tracking service.

Tracks CodeRabbit review events to enforce per-hour quota limits.
Queries GitHub API for review history and caches locally in the DB.
Consumed by the dashboard (Work header tile) and the PR throttle step (#294).

CodeRabbit's real limit depends on the repository's GitHub star count, so the effective
limit is derived here, where the star count cached by ``sync_from_github()`` is available.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sova.adapters.external_reviews import _DEFAULT_CODERABBIT_AUTHORS
from sova.config.models import CodeRabbitQuotaConfig
from sova.db.models import CodeRabbitEvent
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="supervisor.coderabbit_quota")

_SYNC_CACHE_TTL = 300.0  # 5 minutes
_sync_cache: dict[str, float] = {}  # repo -> last sync monotonic timestamp
_sync_locks: dict[str, asyncio.Lock] = {}  # repo -> single-flight lock
_stars_cache: dict[str, int] = {}  # repo -> star count from the last sync that could read it (absent: unknown)
_background_syncs: dict[str, asyncio.Task[None]] = {}  # repo -> the running background sync
# A failed sync or star read writes no cache entry, so "stale" alone would retry on every request while GitHub
# refuses calls. The last attempt time bounds that to one attempt per backoff window.
_SYNC_RETRY_BACKOFF = 60.0
_sync_attempted: dict[str, float] = {}  # repo -> monotonic time a background sync was last started
_stars_attempted: dict[str, float] = {}  # repo -> monotonic time the star count was last read
_star_locks: dict[str, asyncio.Lock] = {}  # repo -> single-flight lock for the star read

_AUTO_REVIEW_MIN_STARS = 10  # below this CodeRabbit does not auto-review and allows one manual review an hour
_BELOW_MIN_STARS_LIMIT = 1
_PLAN_LIMITS = {"free": 4, "pro": 5, "pro_plus": 10}  # reviews an hour once auto-review applies; oss scales with stars
_OSS_MAX_LIMIT = 10


@dataclass(frozen=True, slots=True)
class QuotaStatus:
    """Current CodeRabbit quota state."""

    enabled: bool
    reviews_in_window: int
    reviews_per_hour: int
    can_create_pr: bool
    next_available_minutes: float | None
    window_minutes: int
    synced_at: str | None = None
    auto_review_eligible: bool = False
    stars: int | None = None
    # What the plan allows an hour from 10 stars on, so the tile can state the rule without a table of its own.
    limit_from_10_stars: int = _BELOW_MIN_STARS_LIMIT
    # True when the plan's limit grows with the star count (oss), so "from 10 stars" is a floor, not the limit.
    limit_scales_with_stars: bool = False
    # True when reviews_per_hour is stored, so the limit is the operator's and the star rule does not apply.
    limit_is_explicit: bool = False


def auto_review_eligible(plan: str, stars: int | None) -> bool:
    """Whether CodeRabbit reviews each PR by itself: always on a paid plan, from 10 stars on free and oss."""
    if plan in ("pro", "pro_plus"):
        return True
    return stars is not None and stars >= _AUTO_REVIEW_MIN_STARS


def plan_review_limit(plan: str, stars: int | None) -> int:
    """Reviews an hour for *plan* at *stars*. An unknown star count (None) takes the below-10-stars value."""
    if plan in ("pro", "pro_plus"):
        return _PLAN_LIMITS[plan]
    if stars is None or stars < _AUTO_REVIEW_MIN_STARS:
        return _BELOW_MIN_STARS_LIMIT
    if plan == "oss":
        return min(_OSS_MAX_LIMIT, stars // 10)
    return _PLAN_LIMITS.get(plan, _BELOW_MIN_STARS_LIMIT)


def effective_reviews_per_hour(config: CodeRabbitQuotaConfig, stars: int | None) -> int:
    """The limit in force: a stored ``reviews_per_hour`` (0 included) wins, else the plan's limit at *stars*."""
    if config.reviews_per_hour_is_explicit:
        return config.reviews_per_hour or 0
    return plan_review_limit(config.plan, stars)


async def ensure_star_count(repo: str, *, github_user: str = "") -> None:
    """Fill the star cache for *repo* when it is empty, so a process that never synced still sees the stars.

    The cache is per process, and the gate runs in processes (a spawned run, a server just restarted) that
    have not synced. Single-flight, and a failed read is not retried within ``_SYNC_RETRY_BACKOFF``, so a
    rate-limited GitHub is not asked on every gate check. A failure leaves the count unknown (below-10 rule).
    """
    if not repo or repo in _stars_cache or _star_read_backing_off(repo, github_user):
        return
    async with _star_locks.setdefault(repo, asyncio.Lock()):
        if repo in _stars_cache or _star_read_backing_off(repo, github_user):
            return
        await _refresh_star_count(repo, github_user=github_user)


def _star_read_backing_off(repo: str, github_user: str) -> bool:
    from sova.supervisor.github_quota import get_github_quota_tracker

    last = _stars_attempted.get(repo)
    if last is not None and (time.monotonic() - last) < _SYNC_RETRY_BACKOFF:
        return True
    return get_github_quota_tracker(github_user).should_skip()


def get_cached_stars(repo: str) -> int | None:
    """Return the star count the last sync read for *repo*, or None when it never could."""
    return _stars_cache.get(repo)


def is_sync_stale(repo: str) -> bool:
    """True when *repo* never synced or its last sync is older than the sync cache TTL."""
    last_sync = _sync_cache.get(repo)
    return last_sync is None or (time.monotonic() - last_sync) >= _SYNC_CACHE_TTL


def get_last_sync_iso(repo: str = "") -> str | None:
    """Return the wall-clock ISO timestamp of the last successful sync for *repo*.

    Used by the API to let the client compute a live countdown without
    re-fetching from the server.
    """
    ts = _sync_cache.get(repo)
    if ts is None:
        return None
    age = time.monotonic() - ts
    wall = datetime.now(timezone.utc) - timedelta(seconds=age)
    return wall.isoformat().replace("+00:00", "Z")


async def get_quota_status(
    session: AsyncSession,
    config: CodeRabbitQuotaConfig,
    *,
    project_slug: str = "",
    repo: str = "",
    github_user: str = "",
    fetch_stars: bool = True,
) -> QuotaStatus:
    """Check current CodeRabbit quota availability.

    With *fetch_stars* (the default, for the callers that enforce the gate) an empty star cache is filled first,
    so the limit does not depend on whether this process happened to sync. A caller that must not wait on GitHub
    (the dashboard's status read) passes ``fetch_stars=False`` and answers from what the cache holds.
    """
    if fetch_stars and config.enabled and not config.reviews_per_hour_is_explicit and config.plan in ("free", "oss"):
        await ensure_star_count(repo, github_user=github_user)
    synced_at = get_last_sync_iso(repo)
    stars = get_cached_stars(repo)
    limit = effective_reviews_per_hour(config, stars)
    rule = {
        "auto_review_eligible": auto_review_eligible(config.plan, stars),
        "stars": stars,
        "limit_from_10_stars": plan_review_limit(config.plan, _AUTO_REVIEW_MIN_STARS),
        "limit_scales_with_stars": config.plan == "oss",
        "limit_is_explicit": config.reviews_per_hour_is_explicit,
    }

    if not config.enabled:
        return QuotaStatus(
            enabled=False,
            reviews_in_window=0,
            reviews_per_hour=limit,
            can_create_pr=True,
            next_available_minutes=None,
            window_minutes=config.window_minutes,
            synced_at=synced_at,
            **rule,
        )

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=config.window_minutes)

    events = await _get_review_events_in_window(session, window_start, project_slug)
    count = len(events)

    if limit == 0:
        return QuotaStatus(
            enabled=True,
            reviews_in_window=count,
            reviews_per_hour=0,
            can_create_pr=True,
            next_available_minutes=None,
            window_minutes=config.window_minutes,
            synced_at=synced_at,
            **rule,
        )

    can_create = count < limit

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
        reviews_per_hour=limit,
        can_create_pr=can_create,
        next_available_minutes=next_available,
        window_minutes=config.window_minutes,
        synced_at=synced_at,
        **rule,
    )


async def sync_from_github(
    session: AsyncSession,
    repo: str,
    config: CodeRabbitQuotaConfig,
    *,
    project_slug: str = "",
    github_user: str = "",
    force: bool = False,
) -> int:
    """Fetch recent CodeRabbit reviews from GitHub and cache in DB.

    Returns the number of new events recorded. Skips the API call when the
    last successful sync was within ``_SYNC_CACHE_TTL`` seconds, unless
    *force* is True.
    """
    if not config.enabled or not repo:
        return 0

    now = time.monotonic()
    last_sync = _sync_cache.get(repo)
    if not force and last_sync is not None and (now - last_sync) < _SYNC_CACHE_TTL:
        log.debug("sync_from_github.cached", repo=repo, age_s=round(now - last_sync, 1))
        return 0

    lock = _sync_locks.setdefault(repo, asyncio.Lock())
    async with lock:
        # Recheck after acquiring: another task may have synced while we waited
        if not force and not is_sync_stale(repo):
            return 0

        await _refresh_star_count(repo, github_user=github_user)

        try:
            reviews = await _fetch_coderabbit_reviews_from_github(
                repo, github_user=github_user, window_minutes=config.window_minutes
            )
        except (RuntimeError, OSError):
            log.warning("sync_from_github.api_failed", repo=repo, exc_info=True)
            return 0

        if reviews is None:
            return 0

        if not reviews:
            _sync_cache[repo] = time.monotonic()
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
        _sync_cache[repo] = time.monotonic()
        log.info("sync_from_github.done", repo=repo, new_events=new_count, total_fetched=len(reviews))
        return new_count


def start_background_sync(
    repo: str,
    config: CodeRabbitQuotaConfig,
    *,
    project_dir: Path,
    project_slug: str = "",
    github_user: str = "",
) -> bool:
    """Start a non-forced sync for *repo* without waiting for it, unless one is running or just failed.

    No sync starts within ``_SYNC_RETRY_BACKOFF`` of the last attempt, or while GitHub is rate limited, so a
    failing sync (which caches nothing and so stays stale) is not retried on every request. The caller answers
    from the stored count at once. Returns True when a sync was started. The task opens its
    own session in *project_dir*, which the request that started it will have closed by then.
    """
    from sova.supervisor.github_quota import get_github_quota_tracker

    running = _background_syncs.get(repo)
    if running is not None and not running.done():
        return False
    last_attempt = _sync_attempted.get(repo)
    if last_attempt is not None and (time.monotonic() - last_attempt) < _SYNC_RETRY_BACKOFF:
        return False
    if get_github_quota_tracker(github_user).should_skip():
        return False
    _sync_attempted[repo] = time.monotonic()

    async def _run() -> None:
        from sova.db.session import get_session

        try:
            async with await get_session(project_dir=project_dir) as session:
                await sync_from_github(session, repo, config, project_slug=project_slug, github_user=github_user)
        except Exception:  # noqa: BLE001 (a failed refresh must not surface: the stored figures stay on screen)
            log.warning("background_sync.failed", repo=repo, exc_info=True)

    def _forget(done: asyncio.Task[None]) -> None:
        # A newer sync may already have replaced this one: only drop our own entry.
        if _background_syncs.get(repo) is done:
            del _background_syncs[repo]

    task = asyncio.create_task(_run())
    _background_syncs[repo] = task
    task.add_done_callback(_forget)
    return True


async def _refresh_star_count(repo: str, *, github_user: str = "") -> None:
    """Read *repo*'s star count into the cache. A failed read clears it: unknown, never a stale larger count."""
    stars = await _fetch_star_count(repo, github_user=github_user)
    _stars_attempted[repo] = time.monotonic()
    if stars is None:
        _stars_cache.pop(repo, None)
    else:
        _stars_cache[repo] = stars


async def _fetch_star_count(repo: str, *, github_user: str = "") -> int | None:
    """Fetch the repository's stargazer count with gh. None when gh is unavailable, unauthenticated or limited."""
    from sova.supervisor.github_quota import track_rate_limit
    from sova.utils.gh import resolve_gh_env

    try:
        env = await resolve_gh_env(github_user) if github_user else None
        result = await run("gh", "api", f"repos/{repo}", "--jq", ".stargazers_count", env=env)
    except (RuntimeError, OSError):
        log.warning("fetch_stars.failed", repo=repo, exc_info=True)
        return None
    track_rate_limit(result, github_user)
    if not result.success:
        log.warning("fetch_stars.failed", repo=repo, stderr=result.stderr[:200])
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        log.warning("fetch_stars.bad_output", repo=repo, output=result.stdout[:50])
        return None


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
) -> list[dict] | None:
    """Fetch recent PRs and their CodeRabbit reviews from GitHub API.

    Only fetches PRs updated within ``window_minutes`` (default 60) to
    minimise API calls.  Returns dicts with: pr_number, review_id, submitted_at.
    Returns None on API failure (distinct from [] which means no reviews found).
    """
    from sova.utils.gh import resolve_gh_env

    env = await resolve_gh_env(github_user) if github_user else None

    # The GitHub "List pull requests" API does NOT support a `since`
    # parameter.  We fetch the most recently updated PRs and filter
    # review timestamps client-side in _fetch_reviews_for_pr.
    pr_result = await run(
        "gh",
        "api",
        f"repos/{repo}/pulls",
        "-q",
        r'.[] | "\(.number) \(.updated_at)"',
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
        env=env,
    )
    if not pr_result.success:
        log.warning("fetch_prs.failed", repo=repo, stderr=pr_result.stderr[:200])
        return None

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    pr_numbers: list[int] = []
    for line in pr_result.stdout.strip().splitlines():
        parts = line.strip().split(None, 1)
        if not parts or not parts[0].isdigit():
            continue
        # Filter PRs not updated within the window
        if len(parts) == 2:
            try:
                updated = datetime.fromisoformat(parts[1].replace("Z", "+00:00"))
                if updated < cutoff:
                    continue
            except (ValueError, TypeError):
                pass
        pr_numbers.append(int(parts[0]))

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
