"""Memory API -- agent memory entries from the database."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from sova.dashboard.services import memory_service
from sova.db.session import get_session
from sova.utils.logging import get_logger

router = APIRouter()
log = get_logger(component="dashboard.memory")


@router.get("/memory")
async def list_memories(
    q: str | None = None,
    category: str | None = None,
    limit: int = 100,
):
    try:
        async with await get_session() as session:
            memories, total = await memory_service.list_memories(
                session,
                query=q,
                category=category,
                limit=limit,
            )
        return {"memories": memories, "total": total}
    except Exception:
        log.warning("memory.list.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch memories")
