"""Memory API -- agent memory entries from the database."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from sova.config.context import get_project_dir
from sova.config.loader import load_config
from sova.dashboard.services import memory_service
from sova.db.session import get_session
from sova.knowledge.sharing import export_memories as do_export
from sova.knowledge.sharing import import_memories as do_import
from sova.utils.logging import get_logger

router = APIRouter()
log = get_logger(component="dashboard.memory")


@router.get("/memory")
async def list_memories(
    q: str | None = None,
    category: str | None = None,
    tier: str | None = None,
    limit: int = 100,
):
    try:
        async with await get_session() as session:
            memories, total = await memory_service.list_memories(
                session,
                query=q,
                category=category,
                tier=tier,
                limit=limit,
            )
        return {"memories": memories, "total": total}
    except Exception:
        log.warning("memory.list.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch memories")


@router.post("/memory/export")
async def export_memories(dry_run: bool = False):
    try:
        project_dir = get_project_dir()
        cfg = load_config(project_dir)
        result = await do_export(
            cfg.shared_knowledge_path,
            dry_run=dry_run,
            categories=cfg.shared_knowledge_categories,
            repo=cfg.github_repo,
        )
        return {"exported": result.exported, "skipped": result.skipped, "entries": result.entries}
    except Exception:
        log.warning("memory.export.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to export memories")


@router.post("/memory/import")
async def import_memories(dry_run: bool = False):
    try:
        project_dir = get_project_dir()
        cfg = load_config(project_dir)
        result = await do_import(
            cfg.shared_knowledge_path,
            dry_run=dry_run,
            ignored_hashes=cfg.ignored_shared_hashes,
        )
        return {
            "imported": result.imported,
            "skipped": result.skipped,
            "ignored": result.ignored,
            "entries": result.entries,
        }
    except Exception:
        log.warning("memory.import.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to import memories")
