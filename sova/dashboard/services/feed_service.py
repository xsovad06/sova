"""Activity feed service: in-memory event bus with SSE streaming + DB history.

Captures significant dashboard operations (agent lifecycle, batch progress,
handoff state changes) and streams them to connected browser tabs via SSE.

The in-memory ring buffer drives SSE fan-out and fast reconnection gap-fill.
Events are also persisted to the ``feed_events`` table so the chat-style
cockpit timeline (issue #852) has durable history that survives page reloads
and server restarts, and can be paginated backward (infinite scroll).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
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


def _record_to_dict(record: Any) -> dict[str, Any]:
    """Serialize a FeedEventRecord row into the same shape as FeedEvent.to_dict()."""
    created = record.created_at
    if isinstance(created, datetime):
        ts = created.timestamp()
    else:
        ts = time.time()
    return {
        "id": record.id,
        "severity": record.severity,
        "title": record.title,
        "detail": record.detail,
        "category": record.category,
        "metadata": record.metadata_json or {},
        "timestamp": ts,
    }


class FeedService:
    """Singleton event bus: ring buffer + fan-out to SSE subscribers + DB history."""

    def __init__(self) -> None:
        self._buffer: deque[FeedEvent] = deque(maxlen=_BUFFER_SIZE)
        self._subscribers: dict[int, asyncio.Queue[FeedEvent]] = {}
        self._id_counter = itertools.count(1)
        self._sub_counter = 0
        # Holds references to in-flight persistence tasks so they are not
        # garbage-collected before they finish (asyncio only holds weak refs).
        self._pending_persist: set[asyncio.Task[None]] = set()

    async def init_counter(self, project_dir: Path | None = None) -> None:
        """Advance the in-memory id counter past the DB max id at startup.

        The in-memory ``FeedEvent.id`` and the persisted ``FeedEventRecord.id``
        share one id space: ``_persist()`` writes the event's id as the primary
        key. Without seeding, the counter would restart at 1 after a process
        restart and collide with already-persisted rows, corrupting client-side
        dedup, ordering, and the pagination cursor.

        Safe to call once per project (multi-project mode): the counter only
        advances, never rewinds, so after seeding from every project it sits
        above the highest persisted id across all of them. Only counts not yet
        handed out are affected, so concurrent emit() during startup is safe.
        """
        try:
            from sqlalchemy import func, select

            from sova.db.models import FeedEventRecord
            from sova.db.session import get_session

            async with await get_session(project_dir) as session:
                max_id = (await session.execute(select(func.max(FeedEventRecord.id)))).scalar()
            if max_id:
                current = next(self._id_counter)
                self._id_counter = itertools.count(max(current, max_id + 1))
        except Exception:
            log.debug("feed.init_counter_failed", exc_info=True)

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

        self._schedule_persist(event)
        log.debug("Feed event emitted", event_id=event.id, title=title)
        return event

    def _schedule_persist(self, event: FeedEvent) -> None:
        """Persist the event to the DB as a non-blocking background task.

        No-op when there is no running event loop (e.g. sync CLI contexts):
        persistence is a best-effort side effect, never fatal to emit().
        The active project_dir is captured now because the background task
        runs outside the request's contextvar scope.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        from sova.config.context import get_project_dir

        project_dir = get_project_dir()
        task = loop.create_task(self._persist(event, project_dir))
        self._pending_persist.add(task)
        task.add_done_callback(self._pending_persist.discard)

    async def drain_persist(self) -> None:
        """Await all in-flight persistence tasks. Call before close_db()."""
        if self._pending_persist:
            await asyncio.gather(*self._pending_persist, return_exceptions=True)

    async def _persist(self, event: FeedEvent, project_dir: Path | None) -> None:
        try:
            from sova.db.models import FeedEventRecord
            from sova.db.session import get_session

            async with await get_session(project_dir) as session:
                record = FeedEventRecord(
                    id=event.id,  # keep the in-memory id and DB id in one space
                    severity=event.severity.value,
                    title=event.title[:500],
                    detail=event.detail,
                    category=event.category[:64],
                    metadata_json=event.metadata or None,
                    created_at=datetime.fromtimestamp(event.timestamp, tz=timezone.utc),
                )
                session.add(record)
                await session.commit()
        except Exception:
            log.debug("feed.persist_failed", event_id=event.id, exc_info=True)

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

    async def history_page(
        self,
        *,
        before_id: int | None = None,
        limit: int = 50,
        project_dir: Path | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return a page of persisted events for backward pagination.

        Events are returned oldest-first (chat order). ``before_id`` is an
        exclusive cursor: only events with a smaller id are returned, so the
        client passes the id of its oldest loaded event to load older ones.
        The second tuple element indicates whether more history exists.
        """
        limit = max(1, min(limit, 200))
        try:
            from sqlalchemy import select

            from sova.db.models import FeedEventRecord
            from sova.db.session import get_session

            async with await get_session(project_dir) as session:
                stmt = select(FeedEventRecord)
                if before_id is not None and before_id > 0:
                    stmt = stmt.where(FeedEventRecord.id < before_id)
                # Fetch one extra to detect whether more history remains.
                stmt = stmt.order_by(FeedEventRecord.id.desc()).limit(limit + 1)
                rows = list((await session.execute(stmt)).scalars().all())

            has_more = len(rows) > limit
            rows = rows[:limit]
            # Return oldest-first for natural chat rendering.
            rows.reverse()
            return [_record_to_dict(r) for r in rows], has_more
        except Exception:
            log.debug("feed.history_page_failed", exc_info=True)
            return [], False

    async def prune_old_events(self, *, retention_days: int, project_dir: Path | None = None) -> int:
        """Delete persisted events older than retention_days. Returns rows deleted."""
        if retention_days < 1:
            return 0
        try:
            from sqlalchemy import delete

            from sova.db.models import FeedEventRecord
            from sova.db.session import get_session

            cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
            async with await get_session(project_dir) as session:
                result = await session.execute(delete(FeedEventRecord).where(FeedEventRecord.created_at < cutoff))
                await session.commit()
                return result.rowcount or 0
        except Exception:
            log.debug("feed.prune_failed", exc_info=True)
            return 0

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
