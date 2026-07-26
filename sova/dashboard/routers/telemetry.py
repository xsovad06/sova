"""Telemetry ingestion API: receives run summaries from remote SOVA instances."""

from __future__ import annotations

import hmac
import os
import time
from collections import defaultdict, deque
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from sova.db.session import get_session
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.api.telemetry")

router = APIRouter(prefix="/telemetry", tags=["telemetry"])

_RATE_LIMIT_MAX = 100
_RATE_LIMIT_WINDOW = 60  # seconds
_RATE_LIMIT_RETRY_AFTER = 60
_rate_windows: dict[str, deque[float]] = defaultdict(deque)


class TelemetryIngestRequest(BaseModel):
    machine_id: str = Field(max_length=100)
    run_id: str = Field(max_length=100)
    project_slug: str = Field(max_length=100)
    role: str = Field(max_length=50)
    status: str = Field(max_length=30)
    issue_number: str | None = Field(default=None, max_length=50)
    pr_number: int | None = None
    cost_usd: float = 0.0
    duration_ms: int = 0
    step_outcomes: dict | None = None
    error_message: str | None = None
    run_at: str


def _check_auth(authorization: str | None) -> None:
    """Validate bearer token against SOVA_TELEMETRY_TOKEN env var."""
    expected = os.environ.get("SOVA_TELEMETRY_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="Telemetry ingestion not configured")

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid authorization header")

    token = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="Invalid telemetry token")


def _check_rate_limit(machine_id: str) -> None:
    """Enforce per-machine rate limit using in-memory sliding window."""
    now = time.monotonic()
    window = _rate_windows[machine_id]

    # Evict expired entries
    while window and window[0] <= now - _RATE_LIMIT_WINDOW:
        window.popleft()

    if len(window) >= _RATE_LIMIT_MAX:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded",
            headers={"Retry-After": str(_RATE_LIMIT_RETRY_AFTER)},
        )

    window.append(now)

    # Evict stale machine entries to prevent unbounded growth.
    # Run on every 50th unique machine to amortize cost while catching
    # machines whose deques expired without a follow-up request.
    if len(_rate_windows) > 50:
        stale = [k for k, v in _rate_windows.items() if not v or v[-1] <= now - _RATE_LIMIT_WINDOW]
        for k in stale:
            del _rate_windows[k]


@router.post(
    "/ingest",
    status_code=201,
    responses={
        200: {"description": "Duplicate event (already ingested)"},
        401: {"description": "Missing or invalid authorization header"},
        403: {"description": "Invalid telemetry token"},
        429: {"description": "Rate limit exceeded"},
        503: {"description": "Telemetry ingestion not configured (no token set)"},
    },
)
async def ingest_telemetry(
    body: TelemetryIngestRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Ingest a single telemetry event from a remote SOVA instance."""
    _check_auth(authorization)
    _check_rate_limit(body.machine_id)

    from datetime import datetime

    from sqlalchemy.exc import IntegrityError

    from sova.db.models import TelemetryEvent

    cost_decimal = Decimal(str(body.cost_usd))

    event = TelemetryEvent(
        machine_id=body.machine_id,
        run_id=body.run_id,
        project_slug=body.project_slug,
        role=body.role,
        status=body.status,
        issue_number=body.issue_number,
        pr_number=body.pr_number,
        cost_usd=cost_decimal,
        duration_ms=body.duration_ms,
        step_outcomes=body.step_outcomes,
        error_message=body.error_message,
        run_at=datetime.fromisoformat(body.run_at),
    )

    async with await get_session() as session:
        try:
            async with session.begin():
                session.add(event)
                await session.flush()
        except IntegrityError:
            log.debug("telemetry.duplicate", machine_id=body.machine_id, run_id=body.run_id)
            return JSONResponse(
                status_code=200,
                content={"status": "duplicate", "machine_id": body.machine_id, "run_id": body.run_id},
            )

    log.info("telemetry.ingested", machine_id=body.machine_id, run_id=body.run_id)
    return JSONResponse(
        status_code=201,
        content={"status": "ok", "machine_id": body.machine_id, "run_id": body.run_id},
    )
