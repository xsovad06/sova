"""Activity feed router: SSE stream, history, and briefing endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from starlette.requests import Request
from starlette.responses import StreamingResponse

from sova.dashboard.services.feed_service import FeedService, get_feed_service
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.feed.router")

router = APIRouter(tags=["feed"])


@router.get("/feed/stream")
async def feed_stream(request: Request) -> StreamingResponse:
    """SSE endpoint: streams FeedEvents to the browser."""
    feed = get_feed_service()
    sub_id, queue = feed.subscribe()

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            yield "event: connected\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield feed.to_sse(event)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            feed.unsubscribe(sub_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/feed/history")
async def feed_history(
    feed: Annotated[FeedService, Depends(get_feed_service)],
    since_id: int = Query(0, ge=0),
    before_id: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    """Return feed events.

    Two modes:
    - ``before_id`` > 0: backward pagination from the DB (infinite scroll).
      Returns older persisted events (oldest-first) plus a ``has_more`` flag.
    - otherwise: reconnection gap-fill from the in-memory buffer, returning
      events after ``since_id`` and a ``gap_detected`` flag when the buffer
      has already dropped events the client missed.
    """
    if before_id > 0:
        events, has_more = await feed.history_page(before_id=before_id, limit=limit)
        return {"events": events, "has_more": has_more}

    buffered, gap = feed.history(since_id=since_id)
    result: dict = {"events": [e.to_dict() for e in buffered]}
    if gap:
        result["gap_detected"] = True
        # Backfill from the DB so the client recovers events dropped from the
        # ring buffer during a long disconnect. Paginate until we reach since_id
        # or run out of persisted events, so no events are permanently lost.
        all_backfill: list[dict] = []
        cursor = buffered[0].id if buffered else None
        total = 0
        max_events = 5000
        while cursor is not None and total < max_events:
            older, has_more = await feed.history_page(before_id=cursor, limit=limit)
            older = [e for e in older if e["id"] > since_id]
            total += len(older)
            all_backfill = older + all_backfill
            if not has_more or not older:
                break
            cursor = older[0]["id"]
        result["events"] = all_backfill + result["events"]
    return result


@router.get("/feed/briefing")
async def feed_briefing() -> dict[str, Any]:
    """Generate a morning briefing by aggregating awareness providers.

    Returns an empty briefing (never a 500) when awareness is disabled, no
    providers are configured, or all providers fail, so the client can always
    render the briefing card gracefully.
    """
    empty: dict[str, Any] = {
        "generated_at": None,
        "attention_items": [],
        "informational_items": [],
        "schedule": [],
        "provider_statuses": [],
    }
    try:
        from datetime import datetime, timezone

        from sova.awareness import create_providers
        from sova.awareness.briefing import BriefingService
        from sova.config.context import get_project_dir
        from sova.config.loader import load_config
        from sova.dashboard.services.agent_pool import get_default_project_dir

        project_dir = get_project_dir() or get_default_project_dir()
        cfg = load_config(project_dir)
        if not cfg.awareness.enabled or not cfg.awareness.providers:
            return empty

        providers = create_providers(cfg.awareness)
        if not providers:
            return empty

        start_of_day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        briefing = await BriefingService(providers).generate_briefing(since=start_of_day)
        return _serialize_briefing(briefing)
    except Exception:
        log.debug("feed.briefing_failed", exc_info=True)
        return empty


def _serialize_item(item: Any) -> dict[str, Any]:
    result = {
        "id": item.id,
        "provider": item.provider,
        "category": item.category.value if hasattr(item.category, "value") else str(item.category),
        "title": item.title,
        "body": item.body,
        "source_url": item.source_url,
        "timestamp": item.timestamp.isoformat() if item.timestamp else None,
        "urgency": item.urgency,
        "action_hint": item.action_hint,
    }
    occurrence_count = getattr(item, "occurrence_count", 0)
    if occurrence_count:
        result["occurrence_count"] = occurrence_count
    metadata = getattr(item, "metadata", {})
    if metadata.get("is_recurring_exception"):
        result["is_recurring_exception"] = True
    if metadata.get("recurring_event_id"):
        result["recurring_event_id"] = metadata["recurring_event_id"]
    return result


def _serialize_briefing(briefing: Any) -> dict[str, Any]:
    return {
        "generated_at": briefing.generated_at.isoformat() if briefing.generated_at else None,
        "attention_items": [_serialize_item(i) for i in briefing.attention_items],
        "informational_items": [_serialize_item(i) for i in briefing.informational_items],
        "schedule": [_serialize_item(i) for i in briefing.schedule],
        "provider_statuses": [
            {"name": s.name, "ok": s.ok, "message": s.message, "items_fetched": s.items_fetched}
            for s in briefing.provider_statuses
        ],
    }
