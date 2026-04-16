"""Memory API — FTS5 search and markdown file access."""

from fastapi import APIRouter, Query, HTTPException
from app.services import memory_service

router = APIRouter()


@router.get("/memory/search")
async def search_memory(q: str = Query(..., min_length=1), limit: int = 20):
    return memory_service.search(q, min(limit, 100))


@router.get("/memory/tags")
async def get_tags():
    return memory_service.get_tags()


@router.get("/memory/files")
async def list_files():
    return memory_service.list_markdown_files()


@router.get("/memory/files/{name}")
async def get_file(name: str):
    result = memory_service.get_markdown_file(name)
    if not result:
        raise HTTPException(status_code=404, detail=f"Memory file '{name}' not found")
    return result
