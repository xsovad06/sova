"""Cost tracking queries -- CostRecord from the database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sova.db.models import CostRecord


async def get_summary(session: AsyncSession) -> dict:
    """Aggregate cost summary."""
    total = await session.scalar(select(func.sum(CostRecord.cost_usd))) or 0
    count = await session.scalar(select(func.count(CostRecord.id))) or 0

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_total = (
        await session.scalar(select(func.sum(CostRecord.cost_usd)).where(CostRecord.recorded_at >= today_start)) or 0
    )

    week_ago = now - timedelta(days=7)
    rolling_7d = (
        await session.scalar(select(func.sum(CostRecord.cost_usd)).where(CostRecord.recorded_at >= week_ago)) or 0
    )

    return {
        "total_cost_usd": round(float(total), 4),
        "total_invocations": count,
        "today_cost_usd": round(float(today_total), 4),
        "rolling_7d_usd": round(float(rolling_7d), 4),
    }


async def get_daily(session: AsyncSession, days: int = 14) -> list[dict]:
    """Daily cost totals for the last N days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    day_col = func.date(CostRecord.recorded_at).label("day")
    stmt = (
        select(day_col, func.sum(CostRecord.cost_usd).label("cost"))
        .where(CostRecord.recorded_at >= cutoff)
        .group_by(day_col)
        .order_by(day_col)
    )
    result = await session.execute(stmt)
    return [{"date": str(row.day), "cost_usd": round(float(row.cost), 4)} for row in result.all()]


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
            "cost_usd": round(float(row.cost), 4),
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
            "cost_usd": round(float(row.cost), 4),
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
            "cost_usd": round(float(row.cost), 4),
            "count": row.count,
            "tokens_in": row.tokens_in,
            "tokens_out": row.tokens_out,
        }
        for row in result.all()
    ]
