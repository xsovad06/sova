"""Activity feed router -- SSE stream and history endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from starlette.requests import Request
from starlette.responses import StreamingResponse

from sova.dashboard.services.feed_service import FeedService, get_feed_service

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
) -> dict:
    """Return buffered events after the given ID (for reconnection gap-fill)."""
    events, gap = feed.history(since_id=since_id)
    result: dict = {"events": [e.to_dict() for e in events]}
    if gap:
        result["gap_detected"] = True
    return result
