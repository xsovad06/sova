"""Cancellation safety for the buffered DB writers.

Regression coverage for the write-lock deadlock: an agent exit cancels the
output and resource flush tasks, and a flush cancelled between INSERT and
COMMIT used to abandon its SQLAlchemy connection. SQLite keeps the write lock
held by an abandoned connection forever, which bricks the whole project
database: every later write (task_runs finalization, the liveness sweep,
new agent registration) fails with "database is locked" and no agent can run.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

import pytest

from sova.core.output import OutputWriter
from sova.db.models import TaskRun
from sova.db.session import get_session, init_db_for_project
from sova.monitoring.models import ResourceSample, ResourceSummary
from sova.monitoring.writer import ResourceWriter

# Cancel points spanning the INSERT/COMMIT window. The pre-fix code stranded
# the write lock on most of these; any single stranded lock is a hard failure.
_CANCEL_DELAYS = (0.001, 0.002, 0.004, 0.006, 0.010)


@pytest.fixture(autouse=True)
async def _dispose_engines():
    """Dispose engines after each test.

    ``sova.db.session._engines`` is a module-global cache, but pytest-asyncio
    gives every test its own event loop. Without disposal the pooled aiosqlite
    connections (and their worker threads) outlive the loop they were created
    on and make later tests in the same process non-deterministic.
    """
    yield
    from sova.db.session import close_db

    await close_db()


async def _make_project(tmp_path: Path, name: str) -> Path:
    project = tmp_path / name
    (project / ".claude").mkdir(parents=True)
    await init_db_for_project(project)
    async with await get_session(project_dir=project) as session:
        async with session.begin():
            session.add(TaskRun(id=1, issue_number="1", role="developer", status="running"))
    return project


def _write_lock_available(project: Path) -> bool:
    """True if an independent connection can still take SQLite's write lock."""
    conn = sqlite3.connect(str(project / ".claude" / "sova.db"), timeout=3)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def _row_count(project: Path, table: str) -> int:
    conn = sqlite3.connect(str(project / ".claude" / "sova.db"), timeout=3)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.OperationalError:
        return -1
    finally:
        conn.close()


async def _cancel_mid_flush(flush_coro_factory, delay: float) -> None:
    task = asyncio.create_task(flush_coro_factory())
    await asyncio.sleep(delay)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The shielded insert outlives the cancelled outer task. Wait for it by
    # draining the loop rather than sleeping a fixed interval, so the test does
    # not depend on how fast this machine commits.
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.wait(pending, timeout=10)


@pytest.mark.asyncio
@pytest.mark.parametrize("delay", _CANCEL_DELAYS)
async def test_output_writer_cancel_does_not_strand_write_lock(tmp_path: Path, delay: float) -> None:
    project = await _make_project(tmp_path, f"out{delay}")
    writer = OutputWriter(project, 1, flush_threshold=1)
    for i in range(1500):
        writer.write_line("x" * 200 + str(i))

    await _cancel_mid_flush(writer.flush, delay)

    assert _write_lock_available(project), (
        f"cancelling OutputWriter.flush() after {delay}s left the SQLite write lock held; "
        "the connection was abandoned mid-transaction"
    )
    # The shield exists so a started transaction still completes despite the
    # cancel: all 1500 rows or none (an early cancel can land before the insert
    # begins). A partial count would mean a torn, half-committed batch.
    assert _row_count(project, "output_lines") in (0, 1500)


@pytest.mark.asyncio
async def test_output_writer_rebuffers_when_insert_itself_is_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Distinguish "our task was cancelled" from "the shielded insert was".

    ResourceWriter.flush() re-buffers only when task.cancelling() == 0 (the
    insert was cancelled independently of the outer flush task, so nothing
    landed). OutputWriter.flush() must do the same, not just re-raise
    unconditionally, or a lines-buffer drop with no re-queue and no warning
    goes unnoticed for the more valuable of the two data streams.
    """
    project = await _make_project(tmp_path, "insert-cancel")
    writer = OutputWriter(project, 1, flush_threshold=1)
    writer.write_line("keep-me")

    async def _raise_cancelled(_records: list) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(writer, "_insert", _raise_cancelled)

    # The outer task (this test) is never itself cancelled, so
    # task.cancelling() == 0: this is "the insert was cancelled", not "we
    # were", and the line must be re-queued rather than dropped.
    with pytest.raises(asyncio.CancelledError):
        await writer.flush()

    assert [text for text, _ in writer._buffer] == ["keep-me"]


@pytest.mark.asyncio
async def test_output_writer_timed_flush_rearms_after_its_insert_is_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timed flush whose insert alone was cancelled keeps the lines and schedules the next attempt.

    Without the re-arm the batch would sit in the buffer until the next write_line(), flush() or close().
    """
    project = await _make_project(tmp_path, "timer-rearm")
    writer = OutputWriter(project, 1, flush_interval=0.01)
    real_insert = writer._insert
    attempts = 0

    async def _cancel_first(records: list) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise asyncio.CancelledError
        await real_insert(records)

    monkeypatch.setattr(writer, "_insert", _cancel_first)
    writer.write_line("retry-me")
    # The buffer empties when the retry starts, before its insert commits, so wait on the committed row instead.
    for _ in range(500):
        if attempts >= 2 and _row_count(project, "output_lines") == 1:
            break
        await asyncio.sleep(0.01)

    assert attempts >= 2, "the timer was not re-armed after the insert was cancelled"
    assert _row_count(project, "output_lines") == 1
    await writer.close()


@pytest.mark.asyncio
async def test_output_writer_timed_flush_propagates_its_own_cancellation(tmp_path: Path) -> None:
    """Cancelling the timed flush task itself still propagates instead of re-arming the timer."""
    project = await _make_project(tmp_path, "timer-cancel")
    writer = OutputWriter(project, 1, flush_interval=3600)
    writer.write_line("line")
    writer._timer_handle.cancel()
    writer._timer_handle = None
    task = asyncio.create_task(writer._timed_flush())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.wait(pending, timeout=10)
    assert writer._timer_handle is None
    await writer.close()


@pytest.mark.asyncio
async def test_output_writer_timed_flush_crash_does_not_stop_close_from_persisting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timed flush that raises outside the insert is logged and re-armed, and close() still flushes."""
    project = await _make_project(tmp_path, "timer-crash")
    writer = OutputWriter(project, 1, flush_interval=0.01)
    real_flush = writer._flush_locked
    attempts = 0

    async def _crash_first() -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("seeding blew up")
        return await real_flush()

    monkeypatch.setattr(writer, "_flush_locked", _crash_first)
    writer.write_line("survives")
    for _ in range(200):
        if attempts >= 1:
            break
        await asyncio.sleep(0.01)
    assert attempts >= 1
    await asyncio.sleep(0.02)  # let the crashed task finish and its re-arm land
    await writer.close()

    assert writer._buffer == []
    assert writer._timer_handle is None
    assert _row_count(project, "output_lines") == 1


@pytest.mark.asyncio
async def test_output_writer_close_leaves_no_timer_when_a_line_arrives_during_a_timed_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write_line() during close()'s wait for the running timed flush cannot leave a new timer scheduled."""
    project = await _make_project(tmp_path, "timer-close-race")
    writer = OutputWriter(project, 1, flush_interval=0.01)
    real_insert = writer._insert
    in_flush = asyncio.Event()
    release = asyncio.Event()

    async def _slow_insert(records: list) -> None:
        in_flush.set()
        await release.wait()
        await real_insert(records)

    monkeypatch.setattr(writer, "_insert", _slow_insert)
    writer.write_line("first")
    await asyncio.wait_for(in_flush.wait(), timeout=5)
    closing = asyncio.create_task(writer.close())
    await asyncio.sleep(0.01)
    writer.write_line("during close")  # close() is waiting on the timed flush
    release.set()
    await closing

    assert writer._timer_handle is None
    assert _row_count(project, "output_lines") == 2


@pytest.mark.asyncio
async def test_output_writer_timer_backs_off_while_flushes_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Consecutive failed timed flushes lengthen the retry delay, and a success resets it."""
    project = await _make_project(tmp_path, "timer-backoff")
    writer = OutputWriter(project, 1, flush_interval=5.0)
    delays: list[float] = []
    loop = asyncio.get_running_loop()
    real_call_later = loop.call_later

    def _record(delay: float, callback, *args):
        delays.append(delay)
        return real_call_later(3600, callback, *args)

    monkeypatch.setattr(loop, "call_later", _record)
    writer.write_line("a")
    writer._timer_handle.cancel()
    writer._timer_handle = None
    writer._failed_flushes = 2
    writer._ensure_timer()
    assert delays[-1] == 20.0
    writer._timer_handle.cancel()
    writer._timer_handle = None
    writer._failed_flushes = 20
    writer._ensure_timer()
    assert delays[-1] == 60.0
    writer._timer_handle.cancel()
    writer._timer_handle = None
    await writer.close()


@pytest.mark.asyncio
async def test_output_writer_two_writers_on_one_run_store_unique_contiguous_line_numbers(tmp_path: Path) -> None:
    """The engine and the dashboard both persist a pipeline run's output; their flushes must not collide."""
    project = await _make_project(tmp_path, "two-writers")
    engine_writer = OutputWriter(project, 1)
    dashboard_writer = OutputWriter(project, 1)
    for round_no in range(4):
        engine_writer.write_line(f"engine {round_no} a")
        engine_writer.write_line(f"engine {round_no} b")
        dashboard_writer.write_line(f"dashboard {round_no}")
        # Alternate, and flush concurrently on odd rounds: both read the stored maximum at the same moment.
        if round_no % 2:
            await asyncio.gather(engine_writer.flush(), dashboard_writer.flush())
        else:
            await engine_writer.flush()
            await dashboard_writer.flush()
    await engine_writer.close()
    await dashboard_writer.close()

    conn = sqlite3.connect(str(project / ".claude" / "sova.db"))
    try:
        rows = conn.execute("SELECT line_number, text FROM output_lines WHERE task_run_id = 1").fetchall()
    finally:
        conn.close()
    assert sorted(n for n, _ in rows) == list(range(12))
    assert len({text for _, text in rows}) == 12


@pytest.mark.asyncio
async def test_output_writer_explicit_flush_success_resets_the_backoff(tmp_path: Path) -> None:
    """A successful flush from any caller (not only the timer) ends the failure streak."""
    project = await _make_project(tmp_path, "backoff-reset")
    writer = OutputWriter(project, 1)
    writer._failed_flushes = 3
    writer.write_line("a")
    assert await writer.flush() is True
    assert writer._failed_flushes == 0
    with patch.object(writer, "_insert", side_effect=RuntimeError("db down")):
        writer.write_line("b")
        assert await writer.flush() is False
        assert await writer.flush() is False
    assert writer._failed_flushes == 2
    await writer.close()


@pytest.mark.asyncio
async def test_output_writer_close_persists_every_accepted_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader still writing while close() flushes cannot leave an accepted line behind in the buffer."""
    project = await _make_project(tmp_path, "close-race")
    writer = OutputWriter(project, 1)
    real_insert = writer._insert

    async def _insert_while_reader_writes(records: list) -> None:
        writer.write_line("written during the final flush")
        await real_insert(records)

    monkeypatch.setattr(writer, "_insert", _insert_while_reader_writes)
    writer.write_line("before close")
    await writer.close()

    assert writer._buffer == []
    assert writer._timer_handle is None
    assert _row_count(project, "output_lines") == 1


@pytest.mark.asyncio
async def test_output_writer_close_stops_after_a_failed_final_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A final flush that fails keeps its lines once and returns, rather than retrying in a tight loop."""
    project = await _make_project(tmp_path, "close-fail")
    writer = OutputWriter(project, 1)
    attempts = 0

    async def _fail(_records: list) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("db down")

    monkeypatch.setattr(writer, "_insert", _fail)
    writer.write_line("kept")
    await writer.close()

    assert attempts == 1
    assert [text for text, _ in writer._buffer] == ["kept"]


@pytest.mark.asyncio
async def test_output_writer_detached_insert_failure_is_observed_and_rebuffered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A detached insert that fails after our own task was cancelled must not vanish silently.

    When task.cancelling() > 0 the shielded insert keeps running independently
    of the cancelled flush() call. If that detached insert later raises,
    nothing else is awaiting it; the done-callback must observe the failure
    and re-queue the batch instead of losing it. Line numbers are allocated
    inside the insert transaction, so the retry lands on the next free number:
    the failed batch left no gap.
    """
    project = await _make_project(tmp_path, "detached-failure")
    writer = OutputWriter(project, 1, flush_threshold=1)
    writer.write_line("keep-me-too")
    real_insert = writer._insert

    insert_failed = asyncio.Event()

    async def _insert_then_fail(_records: list) -> None:
        await asyncio.sleep(0.02)
        insert_failed.set()
        raise RuntimeError("simulated detached failure")

    monkeypatch.setattr(writer, "_insert", _insert_then_fail)

    task = asyncio.create_task(writer.flush())
    await asyncio.sleep(0)  # let flush() run up to the (synchronous) asyncio.shield() await
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.wait_for(insert_failed.wait(), timeout=5)
    await asyncio.sleep(0)  # let the done-callback run

    assert [text for text, _ in writer._buffer] == ["keep-me-too"]

    # The retry takes the first free number: the failed batch stored nothing, so there is no hole.
    monkeypatch.setattr(writer, "_insert", real_insert)
    await writer.flush()
    from sova.core.output import read_lines

    lines, times, total = await read_lines(project, 1)
    assert (lines, total) == (["keep-me-too"], 1)
    assert len(times) == len(lines)
    assert all(t is not None for t in times)
    async with await get_session(project_dir=project) as session:
        from sqlalchemy import select

        from sova.db.models import OutputLine

        numbers = list(
            (await session.execute(select(OutputLine.line_number).where(OutputLine.task_run_id == 1))).scalars()
        )
    assert sorted(numbers) == list(range(len(numbers))) == [0]


@pytest.mark.asyncio
async def test_output_writer_detached_failure_does_not_reuse_committed_line_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retried batch must never collide with line numbers a later flush already committed.

    Sequence: flush() #1 is cancelled and its detached insert keeps running. Before it fails,
    flush() #2 runs normally and commits line 0. Only then does flush() #1's detached insert fail.
    The re-buffered line must take the next free number (1): no collision, no gap.
    """
    project = await _make_project(tmp_path, "no-line-reuse")
    writer = OutputWriter(project, 1, flush_threshold=1)

    first_insert_started = asyncio.Event()
    release_first_insert = asyncio.Event()
    real_insert = writer._insert

    async def _insert_first_blocks(records: list) -> None:
        first_insert_started.set()
        await release_first_insert.wait()
        raise RuntimeError("simulated detached failure")

    writer.write_line("first")
    monkeypatch.setattr(writer, "_insert", _insert_first_blocks)
    task = asyncio.create_task(writer.flush())
    await first_insert_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # A second, uncancelled flush commits normally while the first is still detached.
    monkeypatch.setattr(writer, "_insert", real_insert)
    writer.write_line("second")
    await writer.flush()

    # Now let the first (detached) insert fail and re-buffer its line.
    release_first_insert.set()
    await asyncio.sleep(0.01)

    assert [text for text, _ in writer._buffer] == ["first"]

    await writer.flush()  # retry: must land on line 1, right after "second" (line 0), with no gap or collision
    await writer.close()

    from sova.core.output import read_lines

    lines, times, total = await read_lines(project, 1)
    assert total == 2
    assert lines == ["second", "first"]  # "first" retried after "second" already committed line 0
    # A retried insert keeps the time its line was received, so "first" (written before "second") is not re-stamped.
    assert len(times) == len(lines)
    assert all(t is not None for t in times)
    assert times[1] <= times[0]


@pytest.mark.asyncio
async def test_output_writer_cancelled_close_keeps_the_timer_armable(tmp_path: Path) -> None:
    """A close() cancelled while it waits for a running timed flush leaves the writer open and still timed."""
    project = await _make_project(tmp_path, "close-cancel")
    writer = OutputWriter(project, 1, flush_interval=0.01)
    flush_started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_flush() -> bool:
        flush_started.set()
        await release.wait()
        return True

    with patch.object(writer, "flush", _slow_flush):
        writer.write_line("a")
        await asyncio.wait_for(flush_started.wait(), timeout=5)
        closer = asyncio.create_task(writer.close())
        await asyncio.sleep(0.01)
        closer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closer
        release.set()
        await asyncio.sleep(0.01)

    assert not writer._closed
    writer.write_line("b")
    assert writer._timer_handle is not None
    await writer.close()


@pytest.mark.asyncio
async def test_resource_writer_detached_flush_failure_is_observed_and_rebuffered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ResourceWriter.flush() must recover a detached insert failure the same way."""
    project = await _make_project(tmp_path, "detached-resource-failure")
    writer = ResourceWriter(project, 1, flush_threshold=1)
    sample = ResourceSample(
        timestamp=1.0,
        cpu_percent=1.0,
        memory_rss_bytes=1,
        memory_vms_bytes=1,
        io_read_bytes=1,
        io_write_bytes=1,
        num_children=0,
        num_threads=1,
    )
    writer.add_sample(sample)

    insert_failed = asyncio.Event()

    async def _insert_then_fail(_records: list) -> None:
        await asyncio.sleep(0.02)
        insert_failed.set()
        raise RuntimeError("simulated detached failure")

    monkeypatch.setattr(writer, "_insert", _insert_then_fail)

    task = asyncio.create_task(writer.flush())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.wait_for(insert_failed.wait(), timeout=5)
    await asyncio.sleep(0)

    assert writer._buffer == [sample]


@pytest.mark.asyncio
async def test_resource_writer_detached_summary_failure_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """write_summary() must observe a detached summary insert failure, not drop it silently."""
    project = await _make_project(tmp_path, "detached-summary-failure")
    writer = ResourceWriter(project, 1, flush_threshold=1)
    summary = ResourceSummary(
        sample_count=1,
        peak_cpu_percent=1.0,
        avg_cpu_percent=1.0,
        peak_memory_rss_bytes=1,
        peak_memory_vms_bytes=1,
        total_io_read_bytes=1,
        total_io_write_bytes=1,
        peak_num_threads=1,
    )

    insert_failed = asyncio.Event()

    async def _insert_summary_then_fail(_record: object) -> None:
        await asyncio.sleep(0.02)
        insert_failed.set()
        raise RuntimeError("simulated detached summary failure")

    monkeypatch.setattr(writer, "_insert_summary", _insert_summary_then_fail)

    with patch("sova.monitoring.writer.log.warning") as mock_warning:
        task = asyncio.create_task(writer.write_summary(summary))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        await asyncio.wait_for(insert_failed.wait(), timeout=5)
        await asyncio.sleep(0)  # let the done-callback run

    mock_warning.assert_called_once_with(
        "resource_writer.detached_summary_failed", run_id=1, exc_info=mock_warning.call_args.kwargs["exc_info"]
    )


@pytest.mark.asyncio
async def test_resource_writer_summary_cancel_does_not_strand_write_lock(tmp_path: Path) -> None:
    """write_summary runs on the same cancellable agent-exit path as flush.

    _finalize_resource_monitoring writes the summary from _wait_and_finalize,
    which is cancelled on shutdown, so an unshielded summary write strands the
    lock exactly like an unshielded flush.
    """
    project = await _make_project(tmp_path, "summary")
    writer = ResourceWriter(project, 1, flush_threshold=1)
    summary = ResourceSummary(
        sample_count=1,
        peak_cpu_percent=1.0,
        avg_cpu_percent=1.0,
        peak_memory_rss_bytes=1,
        peak_memory_vms_bytes=1,
        total_io_read_bytes=1,
        total_io_write_bytes=1,
        peak_num_threads=1,
    )

    task = asyncio.create_task(writer.write_summary(summary))
    await asyncio.sleep(0)
    task.cancel()
    # A one-row insert can finish before the cancel lands, so either outcome is
    # valid here. What must hold in both is that the lock was not stranded.
    with suppress(asyncio.CancelledError):
        await task
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.wait(pending, timeout=10)

    assert _write_lock_available(project), "cancelling write_summary() left the SQLite write lock held"


@pytest.mark.asyncio
@pytest.mark.parametrize("delay", _CANCEL_DELAYS)
async def test_resource_writer_cancel_does_not_strand_write_lock(tmp_path: Path, delay: float) -> None:
    project = await _make_project(tmp_path, f"res{delay}")
    writer = ResourceWriter(project, 1, flush_threshold=1)
    # Stay under ResourceWriter._MAX_BUFFER_SIZE so no sample is dropped.
    for i in range(900):
        writer.add_sample(
            ResourceSample(
                timestamp=float(i),
                cpu_percent=1.0,
                memory_rss_bytes=1,
                memory_vms_bytes=1,
                io_read_bytes=1,
                io_write_bytes=1,
                num_children=0,
                num_threads=1,
            )
        )

    await _cancel_mid_flush(writer.flush, delay)

    assert _write_lock_available(project), (
        f"cancelling ResourceWriter.flush() after {delay}s left the SQLite write lock held; "
        "the connection was abandoned mid-transaction"
    )
    assert _row_count(project, "resource_samples") in (0, 900)
