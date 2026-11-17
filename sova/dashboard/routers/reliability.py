"""Reliability API: success rate, failure taxonomy, spend by outcome, and usage by role and step."""

from __future__ import annotations

from fastapi import APIRouter, Query

from sova.dashboard.services import reliability_service
from sova.db.session import get_session

router = APIRouter()


@router.get(
    "/reliability/success-by-role",
    response_model=None,
    responses={500: {"description": "Internal error"}},
)
async def success_by_role(days: int = Query(default=30, ge=1, le=365)) -> list[dict]:
    async with await get_session() as session:
        return await reliability_service.get_success_by_role(session, days)


@router.get(
    "/reliability/failure-taxonomy",
    response_model=None,
    responses={500: {"description": "Internal error"}},
)
async def failure_taxonomy(days: int = Query(default=30, ge=1, le=365)) -> list[dict]:
    async with await get_session() as session:
        return await reliability_service.get_failure_taxonomy(session, days)


@router.get(
    "/reliability/spend-by-outcome",
    response_model=None,
    responses={500: {"description": "Internal error"}},
)
async def spend_by_outcome(days: int = Query(default=30, ge=1, le=365)) -> dict:
    async with await get_session() as session:
        return await reliability_service.get_spend_by_outcome(session, days)


@router.get(
    "/reliability/usage-by-role-month",
    response_model=None,
    responses={500: {"description": "Internal error"}},
)
async def usage_by_role(months: int = Query(default=6, ge=1, le=24)) -> list[dict]:
    async with await get_session() as session:
        return await reliability_service.get_usage_by_role(session, months)


@router.get(
    "/reliability/usage-by-step",
    response_model=None,
    responses={500: {"description": "Internal error"}},
)
async def usage_by_step(
    days: int = Query(default=30, ge=1, le=365),
    role: str | None = Query(default=None, max_length=100),
) -> list[dict]:
    async with await get_session() as session:
        return await reliability_service.get_usage_by_step(session, days, role)
