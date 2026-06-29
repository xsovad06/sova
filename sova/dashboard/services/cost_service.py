"""Cost tracking queries -- CostRecord + TaskRun from the database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sova.db.models import CostRecord, TaskRun


async def get_summary(session: AsyncSession) -> dict:
    """Aggregate cost summary.

    Uses TaskRun.total_cost_usd for totals (always written, even for paused runs)
    and TaskRun count for invocations.  CostRecord is used for per-model/phase breakdowns.
    """
    total = await session.scalar(select(func.sum(TaskRun.total_cost_usd))) or 0
    count = await session.scalar(select(func.count(TaskRun.id))) or 0

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_total = (
        await session.scalar(select(func.sum(TaskRun.total_cost_usd)).where(TaskRun.started_at >= today_start)) or 0
    )

    week_ago = now - timedelta(days=7)
    rolling_7d = (
        await session.scalar(select(func.sum(TaskRun.total_cost_usd)).where(TaskRun.started_at >= week_ago)) or 0
    )

    return {
        "total_cost_usd": round(Decimal(str(total or 0)), 4),
        "total_invocations": count,
        "today_cost_usd": round(Decimal(str(today_total or 0)), 4),
        "rolling_7d_usd": round(Decimal(str(rolling_7d or 0)), 4),
    }


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
    result = await session.execute(stmt)
    return [{"date": str(row.day), "cost_usd": round(Decimal(str(row.cost or 0)), 4)} for row in result.all()]


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
            "cost_usd": round(Decimal(str(row.cost or 0)), 4),
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
            "cost_usd": round(Decimal(str(row.cost or 0)), 4),
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
            "cost_usd": round(Decimal(str(row.cost or 0)), 4),
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
            "cost_usd": round(Decimal(str(row.cost or 0)), 4),
            "count": row.count,
            "tokens_in": row.tokens_in,
            "tokens_out": row.tokens_out,
        }
        for row in result.all()
    ]
