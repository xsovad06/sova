"""Activity feed service -- in-memory event bus with SSE streaming.

Captures significant dashboard operations (agent lifecycle, batch progress,
handoff state changes) and streams them to connected browser tabs via SSE.

Phase 1: in-memory ring buffer, no DB persistence.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sova.utils.logging import get_logger

log = get_logger(component="dashboard.feed")


class FeedEventSeverity(str, Enum):
    info = "info"
    success = "success"
    warning = "warning"
    error = "error"


@dataclass
class FeedEvent:
    id: int
    severity: FeedEventSeverity
    title: str
    detail: str | None = None
    category: str = "system"
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "severity": self.severity.value,
            "title": self.title,
            "detail": self.detail,
            "category": self.category,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
        }


_BUFFER_SIZE = 500
_QUEUE_MAX = 100


class FeedService:
    """Singleton event bus: ring buffer + fan-out to SSE subscribers."""

    def __init__(self) -> None:
        self._buffer: deque[FeedEvent] = deque(maxlen=_BUFFER_SIZE)
        self._subscribers: dict[int, asyncio.Queue[FeedEvent]] = {}
        self._id_counter = itertools.count(1)
        self._sub_counter = 0

    def emit(
        self,
        title: str,
        *,
        severity: FeedEventSeverity = FeedEventSeverity.info,
        detail: str | None = None,
        category: str = "system",
        metadata: dict[str, Any] | None = None,
    ) -> FeedEvent:
        event = FeedEvent(
            id=next(self._id_counter),
            severity=severity,
            title=title,
            detail=detail,
            category=category,
            metadata=metadata or {},
        )
        self._buffer.append(event)

        for queue in self._subscribers.values():
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # drop for slow consumers

        log.debug("Feed event emitted", event_id=event.id, title=title)
        return event

    def subscribe(self) -> tuple[int, asyncio.Queue[FeedEvent]]:
        self._sub_counter += 1
        sub_id = self._sub_counter
        queue: asyncio.Queue[FeedEvent] = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._subscribers[sub_id] = queue
        return sub_id, queue

    def unsubscribe(self, sub_id: int) -> None:
        self._subscribers.pop(sub_id, None)

    def history(self, since_id: int = 0) -> tuple[list[FeedEvent], bool]:
        """Return events after since_id and whether a gap was detected."""
        oldest_id = self._buffer[0].id if self._buffer else 0
        gap = since_id > 0 and oldest_id > 0 and since_id < oldest_id
        return [e for e in self._buffer if e.id > since_id], gap

    def to_sse(self, event: FeedEvent) -> str:
        try:
            data = json.dumps(event.to_dict())
        except (TypeError, ValueError):
            log.error("feed.serialize_failed", event_id=event.id, exc_info=True)
            fallback = {"id": event.id, "severity": "error", "title": "Feed serialization error", "category": "system"}
            data = json.dumps(fallback)
        return f"id: {event.id}\nevent: feed\ndata: {data}\n\n"


_feed_service: FeedService | None = None


def get_feed_service() -> FeedService:
    global _feed_service
    if _feed_service is None:
        _feed_service = FeedService()
    return _feed_service


def emit_safe(
    title: str,
    *,
    severity: FeedEventSeverity = FeedEventSeverity.info,
    detail: str | None = None,
    category: str = "system",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Emit a feed event, swallowing exceptions (non-fatal side effect)."""
    try:
        get_feed_service().emit(
            title,
            severity=severity,
            detail=detail,
            category=category,
            metadata=metadata,
        )
    except Exception:
        log.debug("feed.emit_safe_failed", exc_info=True)
