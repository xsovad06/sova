"""Memory API -- agent memory entries from the database."""

from __future__ import annotations

from fastapi import APIRouter

from sova.dashboard.services import memory_service
from sova.db.session import get_session

router = APIRouter()


@router.get("/memory")
async def list_memories(
    q: str | None = None,
    category: str | None = None,
    limit: int = 100,
):
    async with await get_session() as session:
        memories, total = await memory_service.list_memories(
            session,
            query=q,
            category=category,
            limit=limit,
        )
    return {"memories": memories, "total": total}
