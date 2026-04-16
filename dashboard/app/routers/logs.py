"""Logs API — structured log access."""

from fastapi import APIRouter, Query

from app.services import log_service

router = APIRouter()


@router.get("/logs")
async def get_logs(
    level: str | None = None,
    component: str | None = None,
    since: str | None = None,
    search: str | None = Query(None, alias="q"),
    limit: int = 200,
    offset: int = 0,
):
    return log_service.get_logs(level, component, since, search, min(limit, 1000), offset)


@router.get("/logs/components")
async def get_components():
    return log_service.get_components()


@router.get("/logs/counts")
async def get_counts():
    return log_service.get_counts()
