"""Output stream service: per-run pub/sub for real-time output push via SSE.

Unlike FeedService (global event bus for dashboard-level events), this service
handles high-throughput, per-run output streaming.  Each log viewer subscribes
to a specific run_id and receives lines as they are buffered by the agent.
"""

from __future__ import annotations

import asyncio
import itertools

from sova.utils.logging import get_logger

log = get_logger(component="dashboard.output_stream")

_QUEUE_MAX = 200


class OutputStreamService:
    """Fan-out output lines to per-run SSE subscribers."""

    def __init__(self) -> None:
        # run_id -> {sub_id -> Queue}
        self._subscribers: dict[int, dict[int, asyncio.Queue[str]]] = {}
        self._sub_counter = itertools.count(1)
        # (run_id, sub_id) of subscribers that lost a line to a full queue since their stream last asked.
        self._overflowed: set[tuple[int, int]] = set()

    def subscribe(self, run_id: int) -> tuple[int, asyncio.Queue[str]]:
        """Register a subscriber for a run's output. Returns (sub_id, queue)."""
        sub_id = next(self._sub_counter)
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_QUEUE_MAX)
        run_subs = self._subscribers.setdefault(run_id, {})
        run_subs[sub_id] = queue
        log.debug("output_stream.subscribed", run_id=run_id, sub_id=sub_id)
        return sub_id, queue

    def unsubscribe(self, run_id: int, sub_id: int) -> None:
        """Remove a subscriber."""
        run_subs = self._subscribers.get(run_id)
        if run_subs is not None:
            run_subs.pop(sub_id, None)
            if not run_subs:
                del self._subscribers[run_id]
        self._overflowed.discard((run_id, sub_id))
        log.debug("output_stream.unsubscribed", run_id=run_id, sub_id=sub_id)

    def publish(self, run_id: int, line: str) -> None:
        """Push an output line to all subscribers of a given run."""
        run_subs = self._subscribers.get(run_id)
        if not run_subs:
            return
        for sub_id, queue in run_subs.items():
            try:
                queue.put_nowait(line)
            except asyncio.QueueFull:
                # Drop for slow consumers, but remember it: a stream that silently lost a line shows a log
                # with a hole in it, so the SSE endpoint reports the loss (take_overflow) and the viewer re-reads.
                if (run_id, sub_id) not in self._overflowed:
                    log.warning("output_stream.line_dropped", run_id=run_id, sub_id=sub_id)
                self._overflowed.add((run_id, sub_id))

    def take_overflow(self, run_id: int, sub_id: int) -> bool:
        """Whether this subscriber lost a line since the last call, clearing the mark."""
        key = (run_id, sub_id)
        if key not in self._overflowed:
            return False
        self._overflowed.discard(key)
        return True

    def has_subscribers(self, run_id: int) -> bool:
        run_subs = self._subscribers.get(run_id)
        return bool(run_subs)


_output_stream_service: OutputStreamService | None = None


def get_output_stream_service() -> OutputStreamService:
    global _output_stream_service
    if _output_stream_service is None:
        _output_stream_service = OutputStreamService()
    return _output_stream_service
