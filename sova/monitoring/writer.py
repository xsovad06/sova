"""Resource persistence -- batch-insert samples and write summaries to the database.

Modeled after ``sova.core.output.OutputWriter``.  Buffers ``ResourceSample``
objects in memory and bulk-inserts them on flush (threshold-based or explicit
close).  The ``close()`` method also persists the final ``ResourceSummary``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sova.monitoring.models import ResourceSample, ResourceSummary
from sova.utils.logging import get_logger

log = get_logger(component="monitoring.writer")

_DEFAULT_FLUSH_THRESHOLD = 6  # ~30s at 5s interval
_DEFAULT_RETENTION_DAYS = 30
_MAX_BUFFER_SIZE = 1000  # Drop oldest samples if DB is persistently down


class ResourceWriter:
    """Buffered writer that persists resource samples and summaries to the database."""

    def __init__(
        self, project_dir: Path | None, run_id: int, *, flush_threshold: int = _DEFAULT_FLUSH_THRESHOLD
    ) -> None:
        self._project_dir = project_dir
        self._run_id = run_id
        self._flush_threshold = flush_threshold
        self._buffer: list[ResourceSample] = []
        self._closed = False
        # Monotonic-to-wallclock conversion baseline
        self._base_monotonic = time.monotonic()
        self._base_wallclock = datetime.now(timezone.utc)

    def add_sample(self, sample: ResourceSample) -> None:
        """Buffer a sample for later DB persistence (synchronous, no I/O)."""
        if self._closed:
            return
        self._buffer.append(sample)
        # Prevent unbounded growth if the DB is persistently unavailable.
        if len(self._buffer) > _MAX_BUFFER_SIZE:
            dropped = len(self._buffer) - _MAX_BUFFER_SIZE
            del self._buffer[:dropped]
            log.warning("resource_writer.buffer_overflow", run_id=self._run_id, dropped=dropped)

    def should_flush(self) -> bool:
        return len(self._buffer) >= self._flush_threshold

    def _to_wallclock(self, monotonic_ts: float) -> datetime:
        """Convert a monotonic timestamp to a wall-clock datetime."""
        return self._base_wallclock + timedelta(seconds=monotonic_ts - self._base_monotonic)

    async def flush(self) -> None:
        """Bulk-insert buffered samples to the database."""
        if not self._buffer:
            return

        from sova.db.models import ResourceSampleRecord

        samples_to_flush = self._buffer[:]
        self._buffer.clear()

        records = [
            ResourceSampleRecord(
                task_run_id=self._run_id,
                sampled_at=self._to_wallclock(s.timestamp),
                cpu_percent=s.cpu_percent,
                memory_rss_bytes=s.memory_rss_bytes,
                memory_vms_bytes=s.memory_vms_bytes,
                io_read_bytes=s.io_read_bytes,
                io_write_bytes=s.io_write_bytes,
                num_children=s.num_children,
                num_threads=s.num_threads,
            )
            for s in samples_to_flush
        ]

        insert_task = asyncio.ensure_future(self._insert(records))
        try:
            # Shielded: cancellation must never abandon an in-flight transaction.
            # An agent exit cancels this task, and a connection dropped between
            # INSERT and COMMIT keeps SQLite's write lock forever, which bricks
            # the whole project database for every other writer.
            await asyncio.shield(insert_task)
        except asyncio.CancelledError:
            # Two different cancellations reach here and need opposite handling.
            # If our own task was cancelled (an agent exiting), the shielded
            # insert keeps running to completion, so re-buffering would
            # duplicate the rows it is about to commit. If the insert itself
            # was cancelled before writing, nothing landed and the samples must
            # be kept. task.cancelling() tells the two apart.
            task = asyncio.current_task()
            if task is None or task.cancelling() == 0:
                self._buffer = samples_to_flush + self._buffer
            else:
                # The shielded insert is now running detached: nothing else
                # awaits it. If it later fails, observe that failure instead
                # of losing the batch silently.
                insert_task.add_done_callback(self._make_detached_flush_failure_handler(samples_to_flush))
            raise
        except Exception:
            log.warning("resource_writer.flush_failed", run_id=self._run_id, samples=len(records), exc_info=True)
            # Re-add to buffer for retry on next flush
            self._buffer = samples_to_flush + self._buffer

    def _make_detached_flush_failure_handler(
        self, samples_to_flush: list[ResourceSample]
    ) -> Callable[[asyncio.Task], None]:
        """Build a done-callback that recovers a detached flush insert's failure.

        Runs after this writer's own flush() has already returned (or raised)
        following cancellation, so it must not assume the buffer is in the
        same shape it left it in.
        """

        def _on_done(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is None:
                return
            log.warning(
                "resource_writer.detached_insert_failed",
                run_id=self._run_id,
                samples=len(samples_to_flush),
                exc_info=exc,
            )
            self._buffer = samples_to_flush + self._buffer

        return _on_done

    async def _insert(self, records: list) -> None:
        """Persist *records* in one transaction (must run to completion)."""
        from sova.db.session import get_session

        async with await get_session(project_dir=self._project_dir) as session:
            async with session.begin():
                session.add_all(records)

    async def write_summary(self, summary: ResourceSummary) -> None:
        """Persist the final resource summary to the database."""
        from sova.db.models import ResourceSummaryRecord

        record = ResourceSummaryRecord(
            task_run_id=self._run_id,
            sample_count=summary.sample_count,
            peak_cpu_percent=summary.peak_cpu_percent,
            avg_cpu_percent=summary.avg_cpu_percent,
            peak_memory_rss_bytes=summary.peak_memory_rss_bytes,
            peak_memory_vms_bytes=summary.peak_memory_vms_bytes,
            total_io_read_bytes=summary.total_io_read_bytes,
            total_io_write_bytes=summary.total_io_write_bytes,
            peak_num_threads=summary.peak_num_threads,
        )

        summary_task = asyncio.ensure_future(self._insert_summary(record))
        try:
            # Shielded for the same reason as flush(): write_summary runs from
            # _finalize_resource_monitoring, which _wait_and_finalize calls on
            # the agent-exit path, and that task is cancelled on shutdown.
            await asyncio.shield(summary_task)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling() > 0:
                # Our own task was cancelled; the summary insert keeps running
                # detached. Observe its result so a later failure is logged
                # instead of silently dropped.
                summary_task.add_done_callback(self._make_detached_summary_failure_handler())
            raise
        except Exception:
            log.warning("resource_writer.summary_failed", run_id=self._run_id, exc_info=True)

    def _make_detached_summary_failure_handler(self) -> Callable[[asyncio.Task], None]:
        def _on_done(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is None:
                return
            log.warning("resource_writer.detached_summary_failed", run_id=self._run_id, exc_info=exc)

        return _on_done

    async def _insert_summary(self, record: object) -> None:
        """Persist the summary in one transaction (must run to completion)."""
        from sova.db.session import get_session

        async with await get_session(project_dir=self._project_dir) as session:
            async with session.begin():
                session.add(record)

    async def close(self) -> None:
        """Flush remaining samples and mark closed."""
        if self._closed:
            return
        self._closed = True
        await self.flush()


async def cleanup_old_resources(project_dir: Path | None, retention_days: int = _DEFAULT_RETENTION_DAYS) -> int:
    """Delete resource samples and summaries for runs older than *retention_days*.

    Returns the total number of deleted rows.
    """
    from sqlalchemy import delete, select

    from sova.db.models import ResourceSampleRecord, ResourceSummaryRecord, TaskRun
    from sova.db.session import get_session

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    try:
        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                old_run_ids = [
                    row[0]
                    for row in await session.execute(
                        select(TaskRun.id).where(TaskRun.ended_at.isnot(None), TaskRun.ended_at < cutoff)
                    )
                ]
                if not old_run_ids:
                    return 0

                samples_result = await session.execute(
                    delete(ResourceSampleRecord).where(ResourceSampleRecord.task_run_id.in_(old_run_ids))
                )
                summaries_result = await session.execute(
                    delete(ResourceSummaryRecord).where(ResourceSummaryRecord.task_run_id.in_(old_run_ids))
                )
                deleted: int = samples_result.rowcount + summaries_result.rowcount
        log.info("resource.cleanup", deleted=deleted, retention_days=retention_days)
        return deleted
    except Exception:
        log.warning("resource.cleanup_failed", exc_info=True)
        return 0
