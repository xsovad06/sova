"""Memory queries -- Memory table from the database."""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sova.db.models import Memory


async def list_memories(
    session: AsyncSession,
    *,
    query: str | None = None,
    category: str | None = None,
    limit: int = 100,
) -> tuple[list[dict], int]:
    """List memory entries with optional search.

    Returns (entries, total_count).
    """
    stmt = select(Memory).where(Memory.superseded_by.is_(None))

    if query:
        pattern = f"%{query}%"
        stmt = stmt.where(
            or_(
                Memory.title.ilike(pattern),
                Memory.content.ilike(pattern),
                Memory.tags.ilike(pattern),
            )
        )
    if category:
        stmt = stmt.where(Memory.category == category)

    # Count before applying limit
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = await session.scalar(count_stmt) or 0

    stmt = stmt.order_by(Memory.updated_at.desc()).limit(min(limit, 500))
    result = await session.execute(stmt)

    entries = [
        {
            "id": m.id,
            "category": m.category,
            "title": m.title,
            "content": m.content,
            "tags": m.tags,
            "repo": m.repo,
            "tier": m.tier,
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "updated_at": m.updated_at.isoformat() if m.updated_at else None,
        }
        for m in result.scalars().all()
    ]
    return entries, total


async def get_memory_count(session: AsyncSession) -> int:
    """Total number of active (non-superseded) memories."""
    return await session.scalar(select(func.count(Memory.id)).where(Memory.superseded_by.is_(None))) or 0
