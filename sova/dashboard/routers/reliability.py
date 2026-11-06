"""Reliability API: success rate, failure taxonomy, spend by outcome, and usage by role and step."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from sova.dashboard.services import reliability_service
from sova.db.session import get_session
from sova.utils.logging import get_logger

router = APIRouter()
log = get_logger(component="dashboard.reliability")


@router.get(
    "/reliability/success-by-role",
    response_model=None,
    responses={500: {"description": "Failed to fetch success rate by role"}},
)
async def success_by_role(days: int = Query(default=30, ge=1, le=365)) -> list[dict]:
    try:
        async with await get_session() as session:
            return await reliability_service.get_success_by_role(session, days)
    except Exception:  # noqa: BLE001 (HTTP boundary: any internal failure becomes a 500)
        log.warning("reliability.success_by_role.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch success rate by role")


@router.get(
    "/reliability/failure-taxonomy",
    response_model=None,
    responses={500: {"description": "Failed to fetch failure taxonomy"}},
)
async def failure_taxonomy(days: int = Query(default=30, ge=1, le=365)) -> list[dict]:
    try:
        async with await get_session() as session:
            return await reliability_service.get_failure_taxonomy(session, days)
    except Exception:  # noqa: BLE001 (HTTP boundary: any internal failure becomes a 500)
        log.warning("reliability.failure_taxonomy.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch failure taxonomy")


@router.get(
    "/reliability/spend-by-outcome",
    response_model=None,
    responses={500: {"description": "Failed to fetch spend by outcome"}},
)
async def spend_by_outcome(days: int = Query(default=30, ge=1, le=365)) -> dict:
    try:
        async with await get_session() as session:
            return await reliability_service.get_spend_by_outcome(session, days)
    except Exception:  # noqa: BLE001 (HTTP boundary: any internal failure becomes a 500)
        log.warning("reliability.spend_by_outcome.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch spend by outcome")


@router.get(
    "/reliability/usage-by-role-month",
    response_model=None,
    responses={500: {"description": "Failed to fetch usage by role"}},
)
async def usage_by_role(months: int = Query(default=6, ge=1, le=24)) -> list[dict]:
    try:
        async with await get_session() as session:
            return await reliability_service.get_usage_by_role(session, months)
    except Exception:  # noqa: BLE001 (HTTP boundary: any internal failure becomes a 500)
        log.warning("reliability.usage_by_role.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch usage by role")


@router.get(
    "/reliability/usage-by-step",
    response_model=None,
    responses={500: {"description": "Failed to fetch usage by step"}},
)
async def usage_by_step(
    days: int = Query(default=30, ge=1, le=365),
    role: str | None = Query(default=None, max_length=100),
) -> list[dict]:
    try:
        async with await get_session() as session:
            return await reliability_service.get_usage_by_step(session, days, role)
    except Exception:  # noqa: BLE001 (HTTP boundary: any internal failure becomes a 500)
        log.warning("reliability.usage_by_step.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch usage by step")
