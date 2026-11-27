"""Activity feed read side: paginated DB history, retention pruning and SSE framing.

The event bus that emits and persists events (``FeedService``, ``get_feed_service()``, ``emit_safe()``) lives in
``sova.work.feed``; these functions hold no instance state of it.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from sova.db.session import get_session
from sova.utils.logging import get_logger
from sova.work.feed import FeedEvent

log = get_logger(component="dashboard.feed")


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


async def history_page(
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
    except (OSError, RuntimeError, SQLAlchemyError):
        log.debug("feed.history_page_failed", exc_info=True)
        return [], False


async def prune_old_events(*, retention_days: int, project_dir: Path | None = None) -> int:
    """Delete persisted events older than retention_days. Returns rows deleted."""
    if retention_days < 1:
        return 0
    try:
        from sqlalchemy import delete

        from sova.db.models import FeedEventRecord

        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        async with await get_session(project_dir) as session:
            result = await session.execute(delete(FeedEventRecord).where(FeedEventRecord.created_at < cutoff))
            await session.commit()
            return result.rowcount or 0
    except (OSError, RuntimeError, SQLAlchemyError):
        log.debug("feed.prune_failed", exc_info=True)
        return 0


def to_sse(event: FeedEvent) -> str:
    try:
        data = json.dumps(event.to_dict())
    except (TypeError, ValueError):
        log.error("feed.serialize_failed", event_id=event.id, exc_info=True)
        fallback = {"id": event.id, "severity": "error", "title": "Feed serialization error", "category": "system"}
        data = json.dumps(fallback)
    return f"id: {event.id}\nevent: feed\ndata: {data}\n\n"
