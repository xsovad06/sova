"""Telemetry ingestion API: receives run summaries from remote SOVA instances."""

from __future__ import annotations

import hmac
import os
import time
from collections import defaultdict, deque
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

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
    run_at: datetime


def _check_auth(authorization: Annotated[str | None, Header()] = None) -> None:
    """FastAPI dependency: validate the bearer token against the SOVA_TELEMETRY_TOKEN env var.

    Declared as a route dependency, so it runs before `_parse_ingest_body`: an unauthenticated
    caller gets 401/403/503, never a 422 that describes the schema or the JSON it sent.
    """
    expected = os.environ.get("SOVA_TELEMETRY_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="Telemetry ingestion not configured")

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid authorization header")

    token = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="Invalid telemetry token")


async def _parse_ingest_body(request: Request) -> TelemetryIngestRequest:
    """Parse the body only after `_check_auth` has passed.

    A body parameter would be decoded by FastAPI before any dependency runs, so malformed
    JSON from an unauthenticated caller would still get a 422. Validating here keeps the
    same 422 shape (locations under `body`) for an authenticated caller.
    """
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json" and not content_type.endswith("+json"):
        raise HTTPException(status_code=415, detail="Content-Type must be application/json")
    try:
        return TelemetryIngestRequest.model_validate_json(await request.body())
    except ValidationError as e:
        # pydantic reports the raw bytes as `input` for a body that is not valid UTF-8, and
        # FastAPI's 422 encoder cannot decode them (500). Normalize bytes to text first.
        raise RequestValidationError(
            [
                {
                    **err,
                    "loc": ("body", *err["loc"]),
                    "input": err["input"].decode("utf-8", errors="replace")
                    if isinstance(err.get("input"), (bytes, bytearray))
                    else err.get("input"),
                }
                for err in e.errors(include_url=False, include_context=False)
            ]
        ) from e


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
    dependencies=[Depends(_check_auth)],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": TelemetryIngestRequest.model_json_schema()}},
        }
    },
    responses={
        200: {"description": "Duplicate event (already ingested)"},
        401: {"description": "Missing or invalid authorization header"},
        403: {"description": "Invalid telemetry token"},
        429: {"description": "Rate limit exceeded"},
        503: {"description": "Telemetry ingestion not configured (no token set)"},
    },
)
async def ingest_telemetry(
    body: Annotated[TelemetryIngestRequest, Depends(_parse_ingest_body)],
) -> JSONResponse:
    """Ingest a single telemetry event from a remote SOVA instance."""
    _check_rate_limit(body.machine_id)

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
        run_at=body.run_at,
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
