"""Output persistence: write and read agent output via the database.

Each agent run's output lines are stored in the ``output_lines`` table,
keyed by ``task_run_id``.  The ``OutputWriter`` buffers lines in memory
and bulk-inserts them on flush (threshold, timer or explicit close).

For live-streaming of active agents the dashboard uses the in-memory
``AgentState.output_lines`` deque. This module handles only the
durable persistence layer that survives process restarts.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sova.db.session import get_session
from sova.utils.formatting import strip_ansi
from sova.utils.logging import get_logger

log = get_logger(component="output")

_DEFAULT_FLUSH_THRESHOLD = 50
_DEFAULT_FLUSH_INTERVAL_SECONDS = 5.0
_MAX_BACKOFF_INTERVAL_SECONDS = 60.0
# First key of the PostgreSQL advisory lock that serialises a run's line numbering (the run id is the second).
_OUTPUT_LINE_LOCK_NS = 0x4F4C  # "OL"


class OutputWriter:
    """Buffered writer that persists output lines to the database.

    A line's ``created_at`` is the moment ``write_line()`` received it, not the moment its batch was
    flushed. Lines flush when the buffer reaches the threshold, ``flush_interval`` seconds after a line is
    buffered while the writer is open, and on ``close()``.

    The timed flush is a loop ``call_later`` handle, not a sleeping task: a task exists only while a timed
    flush runs, so an idle or never-closed writer leaves no pending task behind for anything that drains
    the loop's tasks (an agent exit, a test) to wait on.
    """

    def __init__(
        self,
        project_dir: Path,
        run_id: int,
        *,
        flush_threshold: int = _DEFAULT_FLUSH_THRESHOLD,
        flush_interval: float = _DEFAULT_FLUSH_INTERVAL_SECONDS,
    ) -> None:
        self._project_dir = project_dir
        self._run_id = run_id
        self._flush_threshold = flush_threshold
        self._flush_interval = flush_interval
        self._buffer: list[tuple[str, datetime]] = []
        self._closed = False
        self._closing = False
        self._failed_flushes = 0  # consecutive failed flushes (any caller), drives the timed retry back-off
        self._flush_lock = asyncio.Lock()
        self._timer_handle: asyncio.TimerHandle | None = None
        self._timer: asyncio.Task[None] | None = None  # the latest timed flush

    @property
    def run_id(self) -> int:
        return self._run_id

    def write_line(self, text: str) -> None:
        """Buffer a line, stamped now, for later DB persistence (synchronous, no I/O).

        ANSI escapes are stripped first, and a line left empty by that (or empty to begin with) is dropped.
        """
        if self._closed:
            return
        text = strip_ansi(text).rstrip("\n")
        if not text:
            return
        self._buffer.append((text, datetime.now(timezone.utc)))
        self._ensure_timer()

    def should_flush(self) -> bool:
        return len(self._buffer) >= self._flush_threshold

    def _ensure_timer(self) -> None:
        """Schedule a timed flush when a loop is running and none is already scheduled."""
        if self._closing or self._closed or self._timer_handle is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        # A database that stays down backs the retry off (doubling, capped) instead of retrying every interval.
        cap = max(self._flush_interval, _MAX_BACKOFF_INTERVAL_SECONDS)
        delay = min(self._flush_interval * (2 ** min(self._failed_flushes, 10)), cap)
        self._timer_handle = loop.call_later(delay, self._on_timer)

    def _on_timer(self) -> None:
        self._timer_handle = None
        if self._closed or (self._timer is not None and not self._timer.done()):
            # A timed flush still running re-schedules itself if lines remain after it.
            return
        self._timer = asyncio.get_running_loop().create_task(self._timed_flush())

    async def _timed_flush(self) -> None:
        try:
            await self.flush()  # _flush_locked() keeps the failure count for every caller
        except asyncio.CancelledError:
            if (task := asyncio.current_task()) is not None and task.cancelling():
                raise
            # Only the insert was cancelled: flush() kept its lines, so retry them on the next interval.
        except Exception:  # noqa: BLE001 (fail-open: a crashing timed flush must still re-arm and let close() flush)
            log.warning("output_writer.timed_flush_failed", run_id=self._run_id, exc_info=True)
        if self._buffer:
            self._ensure_timer()

    def _stop_timer_handle(self) -> None:
        if self._timer_handle is not None:
            self._timer_handle.cancel()
            self._timer_handle = None

    async def _stop_timer(self) -> None:
        """Cancel the scheduled timed flush and wait for one already running to finish."""
        self._stop_timer_handle()
        timer, self._timer = self._timer, None
        if timer is None or timer is asyncio.current_task():
            return
        try:
            await timer
        except asyncio.CancelledError:
            # The timed flush itself was cancelled (its insert stays shielded): only our own
            # cancellation propagates.
            if (task := asyncio.current_task()) is not None and task.cancelling():
                raise
        except Exception:  # noqa: BLE001 (already logged by _timed_flush: a failed timer must not block close())
            pass

    async def flush(self) -> bool:
        """Bulk-insert buffered lines to the database.

        Returns False when the insert failed and the lines were kept in the buffer for a later retry.
        """
        async with self._flush_lock:
            return await self._flush_locked()

    async def _flush_locked(self) -> bool:
        if not self._buffer:
            return True

        from sova.db.models import OutputLine

        lines_to_flush = self._buffer[:]
        self._buffer.clear()

        # line_number is a placeholder: _insert() numbers the batch inside its locked transaction.
        records = [
            OutputLine(task_run_id=self._run_id, line_number=i, text=text, created_at=stamp)
            for i, (text, stamp) in enumerate(lines_to_flush)
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
            # was cancelled before writing, nothing landed and the lines must
            # be kept. task.cancelling() tells the two apart.
            task = asyncio.current_task()
            if task is None or task.cancelling() == 0:
                self._buffer = lines_to_flush + self._buffer
            else:
                # The shielded insert is now running detached: nothing else
                # awaits it. If it later fails, observe that failure instead
                # of losing the batch silently.
                insert_task.add_done_callback(self._make_detached_failure_handler(records, lines_to_flush))
            raise
        except Exception:  # noqa: BLE001 (fail-open: DB write failure must not lose buffered output lines)
            log.warning("output_writer.flush_failed", run_id=self._run_id, lines=len(records), exc_info=True)
            self._buffer = lines_to_flush + self._buffer
            self._failed_flushes += 1
            return False
        self._failed_flushes = 0
        return True

    def _make_detached_failure_handler(
        self, records: list, lines_to_flush: list[tuple[str, datetime]]
    ) -> Callable[[asyncio.Task], None]:
        """Build a done-callback that recovers a detached insert's failure.

        Runs after this writer's own flush() has already returned (or raised)
        following cancellation, so it must not assume the buffer is in the
        same shape it left it in. Re-buffer the text only: line numbers are
        allocated inside ``_insert()``'s locked transaction, so the retry takes
        the next free number and neither duplicates nor leaves a gap.
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
        """Persist *records* in one transaction (must run to completion).

        A dashboard-spawned pipeline run has two writers on one ``task_run_id`` (the engine's, in the
        ``sova run --run-id`` process, and the dashboard's), each counting line numbers from its own memory.
        The run's write lock is taken first, then the stored maximum is read in the same transaction and the
        batch is numbered from the next free number (the locked transaction is the only source of numbers), so
        concurrent flushes from two processes can never store the same ``line_number`` and a failed batch
        leaves no gap.
        """
        from sqlalchemy import func, select, text

        from sova.db.models import OutputLine, TaskRun

        async with await get_session(project_dir=self._project_dir) as session:
            async with session.begin():
                if session.get_bind().dialect.name == "sqlite":
                    # pysqlite opens its transaction lazily on the first DML, which is after the read below.
                    await session.execute(text("BEGIN IMMEDIATE"))
                elif session.get_bind().dialect.name == "postgresql":
                    # A transaction-scoped advisory lock keyed on the run id: needs no task_runs row. The
                    # two-key form namespaces it, so another lock on a small integer cannot contend with a run id.
                    await session.execute(
                        text("SELECT pg_advisory_xact_lock(:ns, :run_id)"),
                        {"ns": _OUTPUT_LINE_LOCK_NS, "run_id": self._run_id},
                    )
                else:
                    # Serialises the run's writers on the task run row (a no-op when the row is absent).
                    await session.execute(select(TaskRun.id).where(TaskRun.id == self._run_id).with_for_update())
                stored_max = await session.scalar(
                    select(func.max(OutputLine.line_number)).where(OutputLine.task_run_id == self._run_id)
                )
                first = stored_max + 1 if stored_max is not None else 0
                for i, record in enumerate(records):
                    record.line_number = first + i
                session.add_all(records)

    async def close(self) -> None:
        """Flush remaining lines and mark closed."""
        if self._closed:
            return
        self._closing = True  # no new timer may be scheduled while the running one is awaited
        try:
            await self._stop_timer()
        except BaseException:
            # Cancelled while waiting for a running timed flush: the writer stays open, so it must keep its timer.
            self._closing = False
            raise
        # Closed before the final flush, so a line can no longer slip in behind the flush's snapshot and be
        # left buffered: every line write_line() accepted is in the buffer now, and later ones are rejected.
        self._closed = True
        self._stop_timer_handle()
        while self._buffer:
            if not await self.flush():
                break  # the flush failed and kept its lines: retrying at once would spin


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
    except Exception:  # noqa: BLE001 (fail-open: output read must not crash the caller)
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
    except Exception:  # noqa: BLE001 (fail-open: retention cleanup must not crash the caller)
        log.warning("output.cleanup_failed", exc_info=True)
        return 0
