"""Output persistence -- write and read agent output via the database.

Each agent run's output lines are stored in the ``output_lines`` table,
keyed by ``task_run_id``.  The ``OutputWriter`` buffers lines in memory
and bulk-inserts them on flush (threshold-based or explicit close).

For live-streaming of active agents the dashboard uses the in-memory
``AgentState.output_lines`` deque -- this module handles only the
durable persistence layer that survives process restarts.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sova.utils.logging import get_logger

log = get_logger(component="output")

_DEFAULT_FLUSH_THRESHOLD = 50


class OutputWriter:
    """Buffered writer that persists output lines to the database."""

    def __init__(self, project_dir: Path, run_id: int, *, flush_threshold: int = _DEFAULT_FLUSH_THRESHOLD) -> None:
        self._project_dir = project_dir
        self._run_id = run_id
        self._flush_threshold = flush_threshold
        self._buffer: list[str] = []
        self._next_line_number: int = 0
        self._closed = False

    @property
    def run_id(self) -> int:
        return self._run_id

    def write_line(self, text: str) -> None:
        """Buffer a line for later DB persistence (synchronous, no I/O)."""
        if self._closed:
            return
        self._buffer.append(text.rstrip("\n"))

    def should_flush(self) -> bool:
        return len(self._buffer) >= self._flush_threshold

    async def flush(self) -> None:
        """Bulk-insert buffered lines to the database."""
        if not self._buffer:
            return

        from sqlalchemy import func, select

        from sova.db.models import OutputLine
        from sova.db.session import get_session

        # Seed counter from DB on first flush to avoid collisions on re-adopted runs
        if self._next_line_number == 0:
            try:
                async with await get_session(project_dir=self._project_dir) as session:
                    max_ln = await session.scalar(
                        select(func.max(OutputLine.line_number)).where(OutputLine.task_run_id == self._run_id)
                    )
                    if max_ln is not None:
                        self._next_line_number = max_ln + 1
            except Exception:
                log.warning("output_writer.seed_line_number_failed", run_id=self._run_id, exc_info=True)

        lines_to_flush = self._buffer[:]
        self._buffer.clear()

        records = [
            OutputLine(task_run_id=self._run_id, line_number=self._next_line_number + i, text=text)
            for i, text in enumerate(lines_to_flush)
        ]
        self._next_line_number += len(records)

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
            # was cancelled before writing, nothing landed and the lines must
            # be kept. task.cancelling() tells the two apart.
            task = asyncio.current_task()
            if task is None or task.cancelling() == 0:
                self._next_line_number -= len(records)
                self._buffer = [r.text for r in records] + self._buffer
            else:
                # The shielded insert is now running detached: nothing else
                # awaits it. If it later fails, observe that failure instead
                # of losing the batch silently.
                insert_task.add_done_callback(self._make_detached_failure_handler(records, lines_to_flush))
            raise
        except Exception:
            log.warning("output_writer.flush_failed", run_id=self._run_id, lines=len(records), exc_info=True)
            self._next_line_number -= len(records)
            self._buffer = [r.text for r in records] + self._buffer

    def _make_detached_failure_handler(
        self, records: list, lines_to_flush: list[str]
    ) -> Callable[[asyncio.Task], None]:
        """Build a done-callback that recovers a detached insert's failure.

        Runs after this writer's own flush() has already returned (or raised)
        following cancellation, so it must not assume the buffer is in the
        same shape it left it in: a later flush() can already have committed
        records using line numbers built on top of the ones this batch
        reserved. Rewinding ``_next_line_number`` here would let the retried
        batch reuse those already-committed numbers, and ``output_lines`` has
        a non-unique index, so a collision would make ``read_lines()``
        ordering ambiguous. Re-buffer the text only; the retry gets fresh
        line numbers from whatever ``_next_line_number`` has advanced to by
        then, leaving a gap instead of a duplicate.
        """

        def _on_done(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is None:
                return
            log.warning(
                "output_writer.detached_insert_failed",
                run_id=self._run_id,
                lines=len(records),
                exc_info=exc,
            )
            self._buffer = lines_to_flush + self._buffer

        return _on_done

    async def _insert(self, records: list) -> None:
        """Persist *records* in one transaction (must run to completion)."""
        from sova.db.session import get_session

        async with await get_session(project_dir=self._project_dir) as session:
            async with session.begin():
                session.add_all(records)

    async def close(self) -> None:
        """Flush remaining lines and mark closed."""
        if self._closed:
            return
        await self.flush()
        self._closed = True


async def read_lines(
    project_dir: Path,
    run_id: int,
    since: int = 0,
) -> tuple[list[str], int]:
    """Read output lines from the database.

    Returns ``(lines_from_offset, total_line_count)``.
    """
    from sqlalchemy import func, select

    from sova.db.models import OutputLine
    from sova.db.session import get_session

    try:
        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                total: int = (
                    await session.execute(select(func.count()).where(OutputLine.task_run_id == run_id))
                ).scalar() or 0

                if total == 0:
                    return [], 0

                rows = await session.execute(
                    select(OutputLine.text)
                    .where(OutputLine.task_run_id == run_id, OutputLine.line_number >= since)
                    .order_by(OutputLine.line_number)
                )
                lines = [row[0] for row in rows]
                return lines, total
    except Exception:
        log.warning("read_lines.failed", run_id=run_id, exc_info=True)
        return [], 0


def read_lines_from_file(path: Path, since: int = 0) -> tuple[list[str], int]:
    """Read output lines from a legacy log file (backward compat)."""
    if not path.exists():
        return [], 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            all_lines = [line.rstrip("\n") for line in f]
    except (OSError, UnicodeDecodeError):
        return [], 0
    total = len(all_lines)
    return all_lines[since:], total


async def cleanup_old_output(project_dir: Path, retention_days: int = 30) -> int:
    """Delete output lines for runs that ended more than *retention_days* ago.

    Returns the number of deleted rows.
    """
    from sqlalchemy import delete, select

    from sova.db.models import OutputLine, TaskRun
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

                result = await session.execute(delete(OutputLine).where(OutputLine.task_run_id.in_(old_run_ids)))
                deleted: int = result.rowcount
        log.info("output.cleanup", deleted=deleted, retention_days=retention_days)
        return deleted
    except Exception:
        log.warning("output.cleanup_failed", exc_info=True)
        return 0
