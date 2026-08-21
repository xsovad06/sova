"""Failure analysis API endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from sova.config.context import get_project_slug
from sova.dashboard.services import failure_analysis_service
from sova.db.session import get_session

router = APIRouter(prefix="/failure-analysis", tags=["failure-analysis"])


@router.get("/breakdown")
async def get_failure_breakdown(
    project_slug: str | None = Depends(get_project_slug),
) -> dict[str, Any]:
    """Get accurate failure rate breakdown.

    Excludes operational failures (user dismissals, stale recovery)
    from the true pipeline failure rate.
    """
    async with await get_session() as session:
        breakdown = await failure_analysis_service.analyze_failures(session, project_slug)
    return {
        "total_runs": breakdown.total_runs,
        "done_runs": breakdown.done_runs,
        "failed_runs": breakdown.failed_runs,
        "interrupted_runs": breakdown.interrupted_runs,
        "rejected_runs": breakdown.rejected_runs,
        "operational_failures": breakdown.operational_failures,
        "true_pipeline_failures": breakdown.true_pipeline_failures,
        "pipeline_failure_rate": round(breakdown.pipeline_failure_rate, 1),
        "top_step_failures": breakdown.top_step_failures,
        "top_error_patterns": breakdown.top_error_patterns,
    }


@router.get("/categories")
async def get_failure_categories(
    project_slug: str | None = Depends(get_project_slug),
) -> dict[str, int]:
    """Get failure counts by category."""
    async with await get_session() as session:
        return await failure_analysis_service.get_failure_category_counts(session, project_slug)
