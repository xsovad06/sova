"""Cost tracking queries: CostRecord + TaskRun from the database, plus conversation turns (never a TaskRun)."""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone, tzinfo
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sova.config.loader import load_config
from sova.db.models import ConversationMessage, CostRecord, TaskRun
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.costs")

# An IANA name is letters, digits and `_+-/`. Anything else (a path, a dotted escape, a long string) is
# refused before ZoneInfo sees it, because ZoneInfo reads files named by its argument.
_TZ_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+){0,2}")
_MAX_TZ_NAME_LENGTH = 64


def resolve_timezone(tz_name: str | None) -> tzinfo:
    """The zone named by ``tz_name``, or UTC for a missing, unknown, malformed or path-like name."""
    if tz_name and len(tz_name) <= _MAX_TZ_NAME_LENGTH and _TZ_NAME_RE.fullmatch(tz_name):
        try:
            return ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            log.debug("costs.timezone_unknown", tz=tz_name)
    return timezone.utc


def timezone_name(zone: tzinfo) -> str:
    """The IANA name to echo back for ``zone`` ("UTC" for the fallback)."""
    return getattr(zone, "key", None) or "UTC"


def local_midnight(now: datetime, zone: tzinfo) -> datetime:
    """Midnight of ``now``'s calendar day in ``zone``, as an aware UTC datetime.

    The wall-clock midnight is built on the zone-aware local date and then converted, so on a day with 23 or
    25 hours "today" still starts at local midnight rather than at a fixed 24-hour offset.
    """
    local_date = now.astimezone(zone).date()
    return datetime.combine(local_date, time.min, tzinfo=zone).astimezone(timezone.utc)


_TURN_COST = ConversationMessage.cost_usd
_TURN_AT = ConversationMessage.created_at


async def _turn_cost(session: AsyncSession, since: datetime | None = None) -> Decimal:
    """Conversation turn cost (feed-panel tabs, issue #1544), all time or since *since*."""
    stmt = select(func.sum(_TURN_COST))
    if since is not None:
        stmt = stmt.where(_TURN_AT >= since)
    return Decimal(await session.scalar(stmt) or 0)


async def get_summary(session: AsyncSession, zone: tzinfo = timezone.utc) -> dict:
    """Aggregate cost summary. "Today" starts at midnight in ``zone`` (UTC unless the caller passes one).

    Uses TaskRun.total_cost_usd for totals (always written, even for paused runs)
    and TaskRun count for invocations.  CostRecord is used for per-model/phase breakdowns.
    Conversation turn cost is added to every total (``conversation_cost_usd`` shows its share).
    """
    conversation_total = await _turn_cost(session)
    total = Decimal(await session.scalar(select(func.sum(TaskRun.total_cost_usd))) or 0) + conversation_total
    count = await session.scalar(select(func.count(TaskRun.id))) or 0

    now = datetime.now(timezone.utc)
    today_start = local_midnight(now, zone)
    today_total = Decimal(
        await session.scalar(select(func.sum(TaskRun.total_cost_usd)).where(TaskRun.started_at >= today_start)) or 0
    ) + await _turn_cost(session, today_start)

    week_ago = now - timedelta(days=7)
    rolling_7d = Decimal(
        await session.scalar(select(func.sum(TaskRun.total_cost_usd)).where(TaskRun.started_at >= week_ago)) or 0
    ) + await _turn_cost(session, week_ago)

    tokens_saved = int(await session.scalar(select(func.sum(CostRecord.tokens_saved))) or 0)

    return {
        "total_cost_usd": round(Decimal(total or 0), 4),
        "total_invocations": count,
        "today_cost_usd": round(Decimal(today_total or 0), 4),
        "rolling_7d_usd": round(Decimal(rolling_7d or 0), 4),
        "conversation_cost_usd": round(conversation_total, 4),
        "tokens_saved": tokens_saved,
        "compression_savings_usd": _compression_savings_usd(tokens_saved),
    }


def _compression_savings_usd(tokens_saved: int) -> Decimal:
    """Estimate USD saved by compression using the configured model's input rate.

    Uses a single representative input rate (the configured default model's) from
    the Anthropic rate card. Unknown or unconfigured models fall back to 0.
    """
    if tokens_saved <= 0:
        return Decimal("0")

    from sova.llm.models import input_rate_per_mtok

    try:
        cfg = load_config()
        model = cfg.llm.model or cfg.agent.model
    except Exception:  # noqa: BLE001 (savings display is best-effort; any config failure yields no estimate)
        log.debug("costs.compression_rate_lookup_failed", exc_info=True)
        return Decimal("0")

    rate = input_rate_per_mtok(model)
    return round(Decimal(tokens_saved) * rate / Decimal("1_000_000"), 4)


async def get_daily(session: AsyncSession, days: int = 14) -> list[dict]:
    """Daily cost totals for the last N days.

    Uses TaskRun.total_cost_usd (consistent with summary) rather than
    CostRecord, which misses costs from the dashboard control_service path.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    day_col = func.date(TaskRun.started_at).label("day")
    stmt = (
        select(day_col, func.sum(TaskRun.total_cost_usd).label("cost"))
        .where(TaskRun.started_at >= cutoff, TaskRun.total_cost_usd > 0)
        .group_by(day_col)
        .order_by(day_col)
    )
    by_day: dict[str, Decimal] = {}
    for row in (await session.execute(stmt)).all():
        by_day[str(row.day)] = Decimal(row.cost or 0)
    turn_day = func.date(_TURN_AT).label("day")
    turns = (
        select(turn_day, func.sum(_TURN_COST).label("cost"))
        .where(_TURN_AT >= cutoff, _TURN_COST > 0)
        .group_by(turn_day)
    )
    for row in (await session.execute(turns)).all():
        by_day[str(row.day)] = by_day.get(str(row.day), Decimal(0)) + Decimal(row.cost or 0)
    return [{"date": day, "cost_usd": round(cost, 4)} for day, cost in sorted(by_day.items())]


async def get_monthly_projection(session: AsyncSession, days: int = 30) -> dict:
    """Project monthly LLM cost from the trailing window of usage.

    Sums ``TaskRun.total_cost_usd`` over the last ``days`` days, derives a daily
    average from the observed activity span, and scales it to a 30-day month.
    Returns ``insufficient_data=True`` (rather than zero/NaN) when there is no
    cost recorded in the window.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    stmt = select(
        func.sum(TaskRun.total_cost_usd).label("total"),
        func.min(TaskRun.started_at).label("first_at"),
    ).where(TaskRun.started_at >= cutoff, TaskRun.total_cost_usd > 0)
    row = (await session.execute(stmt)).one()
    turns = (
        await session.execute(
            select(func.sum(_TURN_COST).label("total"), func.min(_TURN_AT).label("first_at")).where(
                _TURN_AT >= cutoff, _TURN_COST > 0
            )
        )
    ).one()

    total = Decimal(row.total or 0) + Decimal(turns.total or 0)
    starts = [at if at.tzinfo else at.replace(tzinfo=timezone.utc) for at in (row.first_at, turns.first_at) if at]
    if total <= 0 or not starts:
        return {
            "insufficient_data": True,
            "window_days": days,
            "window_total_usd": Decimal("0"),
            "daily_avg_usd": Decimal("0"),
            "projected_monthly_usd": Decimal("0"),
            "observed_days": 0,
        }

    first_at = min(starts)

    elapsed_days = (now - first_at).total_seconds() / 86400.0
    observed_days = min(days, max(1, round(elapsed_days)))
    daily_avg = total / Decimal(observed_days)
    projected = daily_avg * Decimal(30)

    return {
        "insufficient_data": False,
        "window_days": days,
        "window_total_usd": round(total, 4),
        "daily_avg_usd": round(daily_avg, 4),
        "projected_monthly_usd": round(projected, 2),
        "observed_days": observed_days,
    }


async def get_by_issue(session: AsyncSession) -> list[dict]:
    """Cost breakdown by issue, highest cost first."""
    stmt = (
        select(
            CostRecord.issue,
            func.sum(CostRecord.cost_usd).label("cost"),
            func.count(CostRecord.id).label("invocations"),
            func.sum(CostRecord.input_tokens).label("tokens_in"),
            func.sum(CostRecord.output_tokens).label("tokens_out"),
        )
        .group_by(CostRecord.issue)
        .order_by(func.sum(CostRecord.cost_usd).desc())
    )
    result = await session.execute(stmt)
    return [
        {
            "issue": row.issue or "unknown",
            "cost_usd": round(Decimal(row.cost or 0), 4),
            "invocations": row.invocations,
            "tokens_in": row.tokens_in,
            "tokens_out": row.tokens_out,
        }
        for row in result.all()
    ]


async def get_by_phase(session: AsyncSession) -> list[dict]:
    """Cost breakdown by workflow phase."""
    stmt = (
        select(
            CostRecord.phase,
            func.sum(CostRecord.cost_usd).label("cost"),
            func.count(CostRecord.id).label("count"),
        )
        .group_by(CostRecord.phase)
        .order_by(func.sum(CostRecord.cost_usd).desc())
    )
    result = await session.execute(stmt)
    return [
        {
            "phase": row.phase,
            "cost_usd": round(Decimal(row.cost or 0), 4),
            "count": row.count,
        }
        for row in result.all()
    ]


async def get_by_model(session: AsyncSession) -> list[dict]:
    """Cost breakdown by model."""
    stmt = (
        select(
            CostRecord.model,
            func.sum(CostRecord.cost_usd).label("cost"),
            func.count(CostRecord.id).label("count"),
            func.sum(CostRecord.input_tokens).label("tokens_in"),
            func.sum(CostRecord.output_tokens).label("tokens_out"),
        )
        .group_by(CostRecord.model)
        .order_by(func.sum(CostRecord.cost_usd).desc())
    )
    result = await session.execute(stmt)
    return [
        {
            "model": row.model,
            "cost_usd": round(Decimal(row.cost or 0), 4),
            "count": row.count,
            "tokens_in": row.tokens_in,
            "tokens_out": row.tokens_out,
        }
        for row in result.all()
    ]


async def get_by_routing(session: AsyncSession) -> list[dict]:
    """Cost breakdown by model selection reason."""
    reason_col = case(
        (CostRecord.model_selection_reason.is_(None), "untracked"),
        (CostRecord.model_selection_reason == "", "untracked"),
        else_=CostRecord.model_selection_reason,
    ).label("reason")
    stmt = (
        select(
            reason_col,
            func.sum(CostRecord.cost_usd).label("cost"),
            func.count(CostRecord.id).label("count"),
            func.sum(CostRecord.input_tokens).label("tokens_in"),
            func.sum(CostRecord.output_tokens).label("tokens_out"),
        )
        .group_by(reason_col)
        .order_by(func.sum(CostRecord.cost_usd).desc())
    )
    result = await session.execute(stmt)
    return [
        {
            "reason": row.reason,
            "cost_usd": round(Decimal(row.cost or 0), 4),
            "count": row.count,
            "tokens_in": row.tokens_in,
            "tokens_out": row.tokens_out,
        }
        for row in result.all()
    ]
