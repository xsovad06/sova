"""Work header figures: spend, run success rate and merged PRs over rolling 7-day windows.

Everything keys on ``TaskRun.started_at``, matching ``cost_service.get_summary()``. Total spend counts every
run in the window, still-running ones included; outcome counts and the per-outcome spend cover terminal runs
only. A success rate is success / (success + failure), the formula the Reliability page uses: halted runs
(paused by SOVA, stopped by the user) are counted beside it, never in it. ``get_run_stats()`` is database-only.
The merged-PR count comes from ``fetch_merged_prs()``, a cached ``gh`` call the router runs concurrently with
the database query; ``build_header_stats()`` joins the two.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import Row, select
from sqlalchemy.ext.asyncio import AsyncSession

from sova.config.loader import load_config
from sova.core.state import TASK_RUN_TERMINAL, RunOutcome, classify_run_outcome
from sova.dashboard.services.cost_service import local_midnight, timezone_name
from sova.dashboard.services.reliability_service import MIN_RUNS_FOR_RATE
from sova.db.models import FailureRecord, TaskRun
from sova.git.pr import list_merged_prs_since
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.overview_stats")

WINDOW_DAYS = 7
MONTH_DAYS = 30
UNKNOWN_REASON = "unknown"
# The role whose rate the Success tooltip reports beside the overall one.
DEVELOPER_ROLE = "developer"

# Two weeks of merges are bucketed per request, plus a day of margin for the UTC-day granularity of the search.
_MERGED_FETCH_DAYS = 2 * WINDOW_DAYS + 1
# Comfortably above the merge volume of that span. A result of exactly this size may have been cut short,
# so it is reported as unavailable rather than as a smaller count.
MERGED_FETCH_LIMIT = 300
# Short on purpose: the Success and Spend tiles wait for this fetch, and a failure is never cached.
MERGED_FETCH_TIMEOUT_SECONDS = 3.0
# Only what the count needs: up to MERGED_FETCH_LIMIT PR bodies would make the call slow enough to time out.
_MERGED_FETCH_FIELDS = "number,mergedAt,author"
MERGED_CACHE_TTL_SECONDS = 600.0

MergedStatus = Literal["ok", "unavailable", "unsupported"]


@dataclass(frozen=True)
class MergedPR:
    number: int
    merged_at: datetime
    is_bot: bool


@dataclass(frozen=True)
class MergedPRs:
    """The merged-PR answer: ``ok`` with the PRs, or why there are none to count."""

    status: MergedStatus
    prs: tuple[MergedPR, ...] = ()


@dataclass
class OutcomeWindow:
    """Finished-run counts and spend of one window, by outcome."""

    counts: Counter[RunOutcome] = field(default_factory=Counter)
    spend: dict[RunOutcome, Decimal] = field(default_factory=dict)
    total_spend: Decimal = Decimal("0")

    def add(self, outcome: RunOutcome | None, cost: Decimal) -> None:
        """Count a run's spend; ``outcome`` is None for a run that has not finished (spend only)."""
        self.total_spend += cost
        if outcome is not None:
            self.counts[outcome] += 1
            self.spend[outcome] = self.spend.get(outcome, Decimal("0")) + cost

    @property
    def rated(self) -> int:
        """Runs that count toward the success rate: successes plus failures."""
        return self.counts[RunOutcome.SUCCESS] + self.counts[RunOutcome.FAILURE]

    @property
    def unfinished_spend(self) -> Decimal:
        """Spend on failed, paused-by-SOVA and stopped runs: work that produced no result."""
        lost = (RunOutcome.FAILURE, RunOutcome.PAUSED_BY_SOVA, RunOutcome.STOPPED_BY_USER)
        return sum((self.spend.get(o, Decimal("0")) for o in lost), Decimal("0"))

    def summary(self) -> dict:
        """Counts per outcome, and the rate (None under ``MIN_RUNS_FOR_RATE`` rated runs, so the tile shows counts)."""
        rated = self.rated
        success = self.counts[RunOutcome.SUCCESS]
        return {
            "rated": rated,
            "success": success,
            "failure": self.counts[RunOutcome.FAILURE],
            "paused_by_sova": self.counts[RunOutcome.PAUSED_BY_SOVA],
            "stopped_by_user": self.counts[RunOutcome.STOPPED_BY_USER],
            "success_rate": round(success / rated, 4) if rated >= MIN_RUNS_FOR_RATE else None,
        }


@dataclass
class RunStats:
    """What the database says: windows keyed on ``started_at``, ending now."""

    now: datetime
    today_spend: Decimal
    week: OutcomeWindow
    previous_week: OutcomeWindow
    month: OutcomeWindow
    developer_week: OutcomeWindow
    developer_previous_week: OutcomeWindow
    paused_reasons: Counter[str]
    # Filled by ``with_sova_links()`` once the merged PRs are known; empty until then.
    sova_pr_numbers: frozenset[int] = frozenset()


def _utc(value: datetime) -> datetime:
    # SQLite returns naive datetimes (they hold UTC); PostgreSQL returns aware ones.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@dataclass
class _RunStatsBuilder:
    """Buckets one TaskRun row at a time into the windows ``get_run_stats()`` reports."""

    midnight: datetime
    month_start: datetime
    week_start: datetime
    previous_start: datetime
    today_spend: Decimal = Decimal("0")
    week: OutcomeWindow = field(default_factory=OutcomeWindow)
    previous_week: OutcomeWindow = field(default_factory=OutcomeWindow)
    month: OutcomeWindow = field(default_factory=OutcomeWindow)
    developer_week: OutcomeWindow = field(default_factory=OutcomeWindow)
    developer_previous_week: OutcomeWindow = field(default_factory=OutcomeWindow)
    paused_reasons: Counter[str] = field(default_factory=Counter)
    # Runs paused by SOVA whose reason lives in their latest FailureRecord, read after the loop in one query.
    paused_run_ids: list[int] = field(default_factory=list)

    def add(self, row: Row[Any]) -> None:
        started_at = _utc(row.started_at)
        cost = Decimal(row.total_cost_usd or 0)
        outcome = classify_run_outcome(row.status, row.termination_reason) if row.status in TASK_RUN_TERMINAL else None
        is_developer = row.role == DEVELOPER_ROLE
        if started_at >= self.midnight:
            self.today_spend += cost
        if started_at >= self.month_start:
            self.month.add(outcome, cost)
        if started_at >= self.week_start:
            self._add_to(self.week, self.developer_week, is_developer, outcome, cost)
            if outcome is RunOutcome.PAUSED_BY_SOVA:
                self._add_paused_reason(row)
        elif started_at >= self.previous_start:
            self._add_to(self.previous_week, self.developer_previous_week, is_developer, outcome, cost)

    @staticmethod
    def _add_to(
        window: OutcomeWindow,
        developer_window: OutcomeWindow,
        is_developer: bool,
        outcome: RunOutcome | None,
        cost: Decimal,
    ) -> None:
        window.add(outcome, cost)
        if is_developer:
            developer_window.add(outcome, cost)

    def _add_paused_reason(self, row: Row[Any]) -> None:
        if row.status == "paused":
            self.paused_run_ids.append(row.id)
        elif row.status == "awaiting_approval":
            self.paused_reasons["awaiting_approval"] += 1
        else:
            self.paused_reasons[row.termination_reason or UNKNOWN_REASON] += 1


async def get_run_stats(session: AsyncSession, *, zone: tzinfo = timezone.utc, now: datetime | None = None) -> RunStats:
    """Spend and outcome counts for today, the last 7 days, the 7 days before, and the last 30 days."""
    now = now or datetime.now(timezone.utc)
    builder = _RunStatsBuilder(
        midnight=local_midnight(now, zone),
        month_start=now - timedelta(days=MONTH_DAYS),
        week_start=now - timedelta(days=WINDOW_DAYS),
        previous_start=now - timedelta(days=2 * WINDOW_DAYS),
    )

    stmt = select(
        TaskRun.id,
        TaskRun.role,
        TaskRun.status,
        TaskRun.termination_reason,
        TaskRun.total_cost_usd,
        TaskRun.started_at,
    ).where(TaskRun.started_at >= min(builder.month_start, builder.midnight))
    for row in (await session.execute(stmt)).all():
        builder.add(row)

    for reason in await _latest_failure_types(session, builder.paused_run_ids, builder.week_start):
        builder.paused_reasons[reason] += 1

    return RunStats(
        now=now,
        today_spend=builder.today_spend,
        week=builder.week,
        previous_week=builder.previous_week,
        month=builder.month,
        developer_week=builder.developer_week,
        developer_previous_week=builder.developer_previous_week,
        paused_reasons=builder.paused_reasons,
    )


async def with_sova_links(session: AsyncSession, stats: RunStats, merged: MergedPRs) -> RunStats:
    """``stats`` with the merged PRs that a SOVA run is linked to, with no lookback on the run's age.

    The lookup is bounded by the merged PRs themselves (at most ``MERGED_FETCH_LIMIT``), so it never scans the
    whole run history.
    """
    numbers = {pr.number for pr in merged.prs}
    if merged.status != "ok" or not numbers:
        return stats
    stmt = select(TaskRun.pr_number).where(TaskRun.pr_number.in_(numbers)).distinct()
    linked = frozenset(number for (number,) in (await session.execute(stmt)).all())
    return replace(stats, sova_pr_numbers=linked)


async def _latest_failure_types(session: AsyncSession, run_ids: list[int], week_start: datetime) -> list[str]:
    """One failure type per paused run: its latest FailureRecord, or ``unknown`` when it has none.

    The records are selected with a subquery carrying the window predicate, not a bound list of run ids, so a busy
    window cannot exceed SQLite's bound-parameter ceiling.
    """
    if not run_ids:
        return []
    paused_in_window = select(TaskRun.id).where(TaskRun.status == "paused", TaskRun.started_at >= week_start)
    stmt = (
        select(FailureRecord.task_run_id, FailureRecord.failure_type)
        .where(FailureRecord.task_run_id.in_(paused_in_window))
        .order_by(FailureRecord.created_at, FailureRecord.id)
    )
    latest = {row.task_run_id: row.failure_type for row in (await session.execute(stmt)).all()}
    return [latest.get(run_id) or UNKNOWN_REASON for run_id in run_ids]


# Merged PRs are cached per repo: bucketing into windows and the "had a SOVA run" match happen per request, so
# the windows keep moving and new pr_number links show up without a refetch. A failed fetch is never cached.
_cache: dict[str, tuple[float, tuple[MergedPR, ...]]] = {}
_inflight: dict[str, asyncio.Task[tuple[MergedPR, ...] | None]] = {}


def reset_merged_cache() -> None:
    _cache.clear()
    _inflight.clear()


def _forget_inflight(repo: str, done: asyncio.Task) -> None:
    if _inflight.get(repo) is done:
        del _inflight[repo]


def _parse_merged(raw: list[dict]) -> tuple[MergedPR, ...]:
    parsed = []
    for pr in raw:
        try:
            merged_at = _utc(datetime.fromisoformat(str(pr["mergedAt"]).replace("Z", "+00:00")))
            number = int(pr["number"])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        # A PR with no author data (a deleted account) is counted: it is a bot only when GitHub says so.
        author = pr.get("author")
        is_bot = isinstance(author, dict) and bool(author.get("is_bot"))
        parsed.append(MergedPR(number=number, merged_at=merged_at, is_bot=is_bot))
    return tuple(parsed)


async def _refresh_merged(repo: str, github_user: str) -> tuple[MergedPR, ...] | None:
    """Fetch and cache the repo's recent merged PRs; None (cache untouched) when the answer is not trustworthy."""
    since = (datetime.now(timezone.utc) - timedelta(days=_MERGED_FETCH_DAYS)).date()
    try:
        async with asyncio.timeout(MERGED_FETCH_TIMEOUT_SECONDS):
            raw = await list_merged_prs_since(
                repo=repo,
                since=since,
                github_user=github_user,
                limit=MERGED_FETCH_LIMIT,
                fields=_MERGED_FETCH_FIELDS,
            )
        if not isinstance(raw, list):
            raise TypeError(f"gh returned {type(raw).__name__}, not a list of PRs")
        if len(raw) >= MERGED_FETCH_LIMIT:
            log.warning("overview_stats.merged_prs_truncated", repo=repo, limit=MERGED_FETCH_LIMIT)
            return None
        prs = _parse_merged(raw)
    except Exception:  # noqa: BLE001 (any gh or payload failure must only blank the merged tile)
        # Expected: PRLookupError, or OSError (no gh binary on PATH, or TimeoutError when gh runs past the deadline
        # and its process group is killed). Anything else (a non-list payload, a subprocess-layer RuntimeError) is
        # treated the same: only the merged tile is affected, never spend or success, and nothing is cached.
        log.warning("overview_stats.merged_prs_failed", repo=repo, exc_info=True)
        return None
    _cache[repo] = (time.monotonic(), prs)
    return prs


async def fetch_merged_prs(project_dir: Path | None) -> MergedPRs:
    """Recent merged PRs for the project's repo. Two callers on a cold cache share one ``gh`` call."""
    try:
        cfg = await asyncio.to_thread(load_config, project_dir)
    except Exception:  # noqa: BLE001 (the merged tile is optional context; a config failure reads as unavailable)
        log.warning("overview_stats.config_failed", exc_info=True)
        return MergedPRs("unavailable")
    # PRs merge in the GitHub repo whatever tracker holds the issues, so a Jira project with a repo counts too.
    if not cfg.github_repo:
        return MergedPRs("unsupported")

    repo = cfg.github_repo
    cached = _cache.get(repo)
    if cached is not None and time.monotonic() - cached[0] < MERGED_CACHE_TTL_SECONDS:
        return MergedPRs("ok", cached[1])

    task = _inflight.get(repo)
    if task is None:
        task = asyncio.create_task(_refresh_merged(repo, cfg.github_user))
        _inflight[repo] = task
        # Only remove this task's own entry: reset_merged_cache() may have let a newer fetch register since.
        task.add_done_callback(lambda done, repo=repo: _forget_inflight(repo, done))
    # Shielded: one caller going away must not cancel the fetch the others wait on.
    prs = await asyncio.shield(task)
    return MergedPRs("unavailable") if prs is None else MergedPRs("ok", prs)


def _money(value: Decimal) -> Decimal:
    return round(value, 4)


def _cost_per_pr(spend: Decimal, count: int) -> Decimal | None:
    return _money(spend / count) if count > 0 else None


def _merged_section(merged: MergedPRs, stats: RunStats) -> dict:
    if merged.status != "ok":
        return {
            "status": merged.status,
            "count": None,
            "previous_count": None,
            "bots_left_out": None,
            "with_sova_run": None,
            "cost_per_pr_usd": None,
            "previous_cost_per_pr_usd": None,
        }
    week_start = stats.now - timedelta(days=WINDOW_DAYS)
    previous_start = stats.now - timedelta(days=2 * WINDOW_DAYS)
    this_week_all = [pr for pr in merged.prs if pr.merged_at >= week_start]
    this_week = [pr for pr in this_week_all if not pr.is_bot]
    last_week = [pr for pr in merged.prs if not pr.is_bot and previous_start <= pr.merged_at < week_start]
    return {
        "status": "ok",
        "count": len(this_week),
        "previous_count": len(last_week),
        "bots_left_out": len(this_week_all) - len(this_week),
        "with_sova_run": sum(1 for pr in this_week if pr.number in stats.sova_pr_numbers),
        "cost_per_pr_usd": _cost_per_pr(stats.week.total_spend, len(this_week)),
        "previous_cost_per_pr_usd": _cost_per_pr(stats.previous_week.total_spend, len(last_week)),
    }


def build_header_stats(stats: RunStats, merged: MergedPRs, zone: tzinfo) -> dict:
    """The ``GET /overview/stats`` payload."""
    week = stats.week
    return {
        "timezone": {"name": timezone_name(zone), "abbreviation": stats.now.astimezone(zone).tzname()},
        "spend": {
            "today_usd": _money(stats.today_spend),
            "week_usd": _money(week.total_spend),
            "previous_week_usd": _money(stats.previous_week.total_spend),
            "month_usd": _money(stats.month.total_spend),
            "month_daily_usd": _money(stats.month.total_spend / MONTH_DAYS),
            "unfinished_usd": _money(week.unfinished_spend),
            "unfinished_share": round(float(week.unfinished_spend / week.total_spend), 4) if week.total_spend else None,
        },
        "outcomes": {
            "week": week.summary(),
            "previous_week": stats.previous_week.summary(),
            "month": stats.month.summary(),
            "developer": {
                "week": stats.developer_week.summary(),
                "previous_week": stats.developer_previous_week.summary(),
            },
            "paused_reasons": dict(stats.paused_reasons.most_common()),
        },
        "merged": _merged_section(merged, stats),
    }
