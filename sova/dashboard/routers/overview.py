"""Overview API: aggregated dashboard data."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Query

from sova.config.context import get_project_dir
from sova.dashboard.services import cost_service, overview_stats_service, run_service
from sova.dashboard.services.overview_stats_service import MergedPRs
from sova.db.session import get_session
from sova.utils.logging import get_logger

router = APIRouter()
log = get_logger(component="dashboard.overview")


@router.get("/overview", response_model=None, responses={500: {"description": "Internal error"}})
async def overview() -> dict:
    async with await get_session() as session:
        runs = await run_service.get_run_summary(session)
        costs = await cost_service.get_summary(session)

    return {
        "runs": runs,
        "costs": costs,
    }


@router.get("/overview/stats", response_model=None, responses={500: {"description": "Internal error"}})
async def overview_stats(
    tz: Annotated[str, Query(max_length=200, description="IANA zone for 'today'; an invalid one reads as UTC")] = "UTC",
) -> dict:
    """Work header figures: spend, run success rate and merged PRs over rolling 7-day windows."""
    zone = cost_service.resolve_timezone(tz)
    # The gh call runs while the database is read; it owns no session and reports its own failure.
    merged_task = asyncio.create_task(overview_stats_service.fetch_merged_prs(get_project_dir()))
    try:
        async with await get_session() as session:
            run_stats = await overview_stats_service.get_run_stats(session, zone=zone)
    except BaseException:
        merged_task.cancel()
        raise
    try:
        merged = await merged_task
    except Exception:  # noqa: BLE001 (the merged tile is optional: any failure must not take Success and Spend down)
        log.warning("overview_stats.merged_task_failed", exc_info=True)
        merged = MergedPRs("unavailable")
    # A second, short session: the merged numbers are known only now, and the lookup is bounded by them.
    async with await get_session() as session:
        run_stats = await overview_stats_service.with_sova_links(session, run_stats, merged)
    return overview_stats_service.build_header_stats(run_stats, merged, zone)
