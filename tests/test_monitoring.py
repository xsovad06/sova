"""Tests for sova.monitoring -- resource collector, models, writer, and cleanup."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sova.monitoring.collector import ResourceCollector
from sova.monitoring.models import ResourceSample, ResourceSummary


async def _wait_for_samples(collector: ResourceCollector, n: int, timeout: float = 2.0) -> None:
    """Poll until collector has at least *n* samples or *timeout* expires."""
    deadline = asyncio.get_event_loop().time() + timeout
    while len(collector.samples) < n:
        if asyncio.get_event_loop().time() > deadline:
            break
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# ResourceSample / ResourceSummary dataclass tests
# ---------------------------------------------------------------------------


class TestResourceSample:
    def test_fields(self) -> None:
        s = ResourceSample(
            timestamp=1000.0,
            cpu_percent=50.0,
            memory_rss_bytes=1024,
            memory_vms_bytes=2048,
            io_read_bytes=100,
            io_write_bytes=200,
            num_children=2,
        )
        assert s.cpu_percent == 50.0
        assert s.memory_rss_bytes == 1024
        assert s.io_read_bytes == 100
        assert s.num_children == 2

    def test_io_fields_optional(self) -> None:
        s = ResourceSample(
            timestamp=1.0,
            cpu_percent=0.0,
            memory_rss_bytes=0,
            memory_vms_bytes=0,
            io_read_bytes=None,
            io_write_bytes=None,
            num_children=0,
        )
        assert s.io_read_bytes is None
        assert s.io_write_bytes is None


class TestResourceSummary:
    def test_zero_samples(self) -> None:
        s = ResourceSummary.empty()
        assert s.sample_count == 0
        assert s.peak_memory_rss_bytes == 0
        assert s.avg_cpu_percent == 0.0

    def test_from_samples(self) -> None:
        # I/O values are per-sample deltas; total = sum of all deltas
        samples = [
            ResourceSample(1.0, 10.0, 100, 200, 50, 60, 1),
            ResourceSample(2.0, 30.0, 300, 400, 150, 160, 2),
            ResourceSample(3.0, 20.0, 200, 300, 100, 110, 1),
        ]
        summary = ResourceSummary.from_samples(samples)
        assert summary.sample_count == 3
        assert summary.peak_memory_rss_bytes == 300
        assert summary.peak_memory_vms_bytes == 400
        assert summary.avg_cpu_percent == pytest.approx(20.0)
        assert summary.peak_cpu_percent == 30.0
        # I/O deltas summed: 50 + 150 + 100 = 300
        assert summary.total_io_read_bytes == 300
        # 60 + 160 + 110 = 330
        assert summary.total_io_write_bytes == 330

    def test_from_samples_with_none_io(self) -> None:
        samples = [
            ResourceSample(1.0, 10.0, 100, 200, None, None, 0),
            ResourceSample(2.0, 20.0, 200, 300, None, None, 0),
        ]
        summary = ResourceSummary.from_samples(samples)
        assert summary.total_io_read_bytes is None
        assert summary.total_io_write_bytes is None

    def test_from_samples_empty_list(self) -> None:
        summary = ResourceSummary.from_samples([])
        assert summary.sample_count == 0
        assert summary.peak_memory_rss_bytes == 0

    def test_from_samples_io_delta(self) -> None:
        """I/O totals are the sum of per-sample deltas."""
        samples = [
            ResourceSample(1.0, 10.0, 100, 200, 1000, 2000, 0),
            ResourceSample(2.0, 20.0, 200, 300, 1500, 2800, 0),
        ]
        summary = ResourceSummary.from_samples(samples)
        assert summary.total_io_read_bytes == 2500
        assert summary.total_io_write_bytes == 4800

    def test_from_samples_io_child_death_no_negative(self) -> None:
        """When children die between samples, I/O deltas stay non-negative.

        With per-PID delta tracking, a child dying removes its delta
        contribution (becomes 0) rather than producing a negative total.
        """
        samples = [
            # First sample: parent(100) + child(500) = delta 600
            ResourceSample(1.0, 10.0, 1024, 2048, 600, 600, 1),
            # Second sample: child died, only parent delta = 50
            ResourceSample(2.0, 10.0, 512, 1024, 50, 50, 0),
        ]
        summary = ResourceSummary.from_samples(samples)
        # Sum of deltas: 600 + 50 = 650 (not negative)
        assert summary.total_io_read_bytes == 650
        assert summary.total_io_write_bytes == 650


# ---------------------------------------------------------------------------
# ResourceCollector tests
# ---------------------------------------------------------------------------


def _make_mock_process(
    pid: int = 123,
    create_time: float = 1000.0,
    cpu_percent: float = 25.0,
    rss: int = 1024,
    vms: int = 2048,
    children: list | None = None,
    io_read: int | None = 500,
    io_write: int | None = 600,
    num_threads: int = 4,
) -> MagicMock:
    proc = MagicMock()
    proc.pid = pid
    proc.create_time.return_value = create_time
    proc.cpu_percent.return_value = cpu_percent
    proc.num_threads.return_value = num_threads

    mem = MagicMock()
    mem.rss = rss
    mem.vms = vms
    proc.memory_info.return_value = mem

    if io_read is not None:
        io = MagicMock()
        io.read_bytes = io_read
        io.write_bytes = io_write
        proc.io_counters.return_value = io
    else:
        proc.io_counters.side_effect = psutil.AccessDenied(pid)

    proc.children.return_value = children or []
    return proc


class TestResourceCollector:
    def test_init(self) -> None:
        collector = ResourceCollector(pid=123, interval=2.0)
        assert collector.pid == 123
        assert collector.interval == 2.0
        assert len(collector.samples) == 0

    @pytest.mark.asyncio
    async def test_collect_single_sample(self) -> None:
        mock_proc = _make_mock_process()
        with patch("sova.monitoring.collector.psutil.Process", return_value=mock_proc):
            collector = ResourceCollector(pid=123, interval=0.5)

            # First call is the priming call in start(), then one real
            # sample, then process dies.
            mock_proc.cpu_percent.side_effect = [0.0, 25.0, psutil.NoSuchProcess(123)]

            collector.start()
            await _wait_for_samples(collector, 1)
            await collector.stop()

            assert len(collector.samples) >= 1
            sample = collector.samples[0]
            assert sample.cpu_percent == 25.0
            assert sample.memory_rss_bytes == 1024

    @pytest.mark.asyncio
    async def test_stop_signals_cooperative_shutdown(self) -> None:
        """stop() sets the stop event for cooperative shutdown (not cancellation)."""
        mock_proc = _make_mock_process()
        with patch("sova.monitoring.collector.psutil.Process", return_value=mock_proc):
            collector = ResourceCollector(pid=123, interval=0.05)
            collector.start()
            await _wait_for_samples(collector, 1)
            summary = await collector.stop()

            assert summary.sample_count >= 1
            assert collector._task is None

    @pytest.mark.asyncio
    async def test_process_dies_mid_run_returns_summary(self) -> None:
        """Process dying mid-sampling returns collected samples."""
        mock_proc = _make_mock_process()
        # Priming call succeeds, first real sample succeeds,
        # second sample's create_time check succeeds but cpu_percent dies.
        mock_proc.cpu_percent.side_effect = [
            0.0,  # start() priming
            25.0,  # first sample succeeds
            psutil.NoSuchProcess(123),  # second sample dies
        ]
        mock_proc.create_time.side_effect = [1000.0, 1000.0, 1000.0]

        with patch("sova.monitoring.collector.psutil.Process", return_value=mock_proc):
            collector = ResourceCollector(pid=123, interval=0.01)
            collector.start()
            await _wait_for_samples(collector, 1)
            summary = await collector.stop()

            # At least one successful sample before death
            assert summary.sample_count >= 1

    @pytest.mark.asyncio
    async def test_pid_reuse_detection(self) -> None:
        mock_proc = _make_mock_process(create_time=1000.0)
        # After first sample, create_time changes (PID reuse)
        mock_proc.create_time.side_effect = [1000.0, 1000.0, 9999.0]

        with patch("sova.monitoring.collector.psutil.Process", return_value=mock_proc):
            collector = ResourceCollector(pid=123, interval=0.05)
            collector.start()
            await _wait_for_samples(collector, 1)
            summary = await collector.stop()

            # Should have stopped after detecting PID reuse
            assert summary.sample_count >= 1

    @pytest.mark.asyncio
    async def test_access_denied_on_child(self) -> None:
        child = MagicMock()
        child.pid = 456
        child.cpu_percent.side_effect = psutil.AccessDenied(456)
        child.memory_info.side_effect = psutil.AccessDenied(456)
        child.io_counters.side_effect = psutil.AccessDenied(456)

        mock_proc = _make_mock_process(children=[child])

        with patch("sova.monitoring.collector.psutil.Process", return_value=mock_proc):
            collector = ResourceCollector(pid=123, interval=0.05)
            collector.start()
            await _wait_for_samples(collector, 1)
            summary = await collector.stop()

            # Should still have samples from the parent
            assert summary.sample_count >= 1

    @pytest.mark.asyncio
    async def test_io_access_denied_sets_none(self) -> None:
        mock_proc = _make_mock_process(io_read=None, io_write=None)

        with patch("sova.monitoring.collector.psutil.Process", return_value=mock_proc):
            collector = ResourceCollector(pid=123, interval=0.05)
            collector.start()
            await _wait_for_samples(collector, 1)
            summary = await collector.stop()

            assert summary.sample_count >= 1
            assert collector.samples[0].io_read_bytes is None

    @pytest.mark.asyncio
    async def test_children_aggregated(self) -> None:
        child_mem = MagicMock()
        child_mem.rss = 512
        child_mem.vms = 1024
        child_io = MagicMock()
        child_io.read_bytes = 100
        child_io.write_bytes = 200

        child = MagicMock()
        child.pid = 456
        child.cpu_percent.return_value = 10.0
        child.memory_info.return_value = child_mem
        child.io_counters.return_value = child_io
        child.num_threads.return_value = 3

        mock_proc = _make_mock_process(children=[child])

        with patch("sova.monitoring.collector.psutil.Process", return_value=mock_proc):
            collector = ResourceCollector(pid=123, interval=0.05)
            collector.start()
            await _wait_for_samples(collector, 1)
            summary = await collector.stop()

            assert summary.sample_count >= 1
            sample = collector.samples[0]
            # Parent (1024) + child (512)
            assert sample.memory_rss_bytes == 1536
            assert sample.num_children == 1
            # First sample: I/O delta is 0 (no previous baseline to diff against)
            assert sample.io_read_bytes == 0
            assert sample.io_write_bytes == 0

    @pytest.mark.asyncio
    async def test_get_summary_while_running(self) -> None:
        mock_proc = _make_mock_process()
        with patch("sova.monitoring.collector.psutil.Process", return_value=mock_proc):
            collector = ResourceCollector(pid=123, interval=0.05)
            collector.start()
            await _wait_for_samples(collector, 1)

            summary = collector.get_summary()
            assert summary.sample_count >= 1

            await collector.stop()

    @pytest.mark.asyncio
    async def test_double_start_is_noop(self) -> None:
        mock_proc = _make_mock_process()
        with patch("sova.monitoring.collector.psutil.Process", return_value=mock_proc):
            collector = ResourceCollector(pid=123, interval=0.05)
            collector.start()
            task1 = collector._task
            collector.start()  # should not create new task
            assert collector._task is task1
            await collector.stop()

    @pytest.mark.asyncio
    async def test_double_stop_is_safe(self) -> None:
        mock_proc = _make_mock_process()
        with patch("sova.monitoring.collector.psutil.Process", return_value=mock_proc):
            collector = ResourceCollector(pid=123, interval=0.05)
            collector.start()
            await _wait_for_samples(collector, 1)
            await collector.stop()
            summary = await collector.stop()  # should not raise
            assert summary.sample_count >= 0

    @pytest.mark.asyncio
    async def test_sample_loop_process_exit_during_sample(self) -> None:
        """Cover the NoSuchProcess except branch in _sample_loop."""
        mock_proc = _make_mock_process()
        # First create_time() call is for start(), second is the PID-reuse
        # check in _sample_loop which then calls _take_sample -> cpu_percent
        # raises NoSuchProcess to simulate process dying mid-sample.
        mock_proc.create_time.side_effect = [1000.0, 1000.0]
        mock_proc.cpu_percent.side_effect = [0.0, psutil.NoSuchProcess(123)]

        with patch("sova.monitoring.collector.psutil.Process", return_value=mock_proc):
            collector = ResourceCollector(pid=123, interval=0.01)
            collector.start()
            # Task will exit quickly since process dies on first sample
            await asyncio.sleep(0.05)
            summary = await collector.stop()
            assert summary.sample_count == 0

    def test_take_sample_child_io_access_denied(self) -> None:
        """Cover child io_counters except branch (lines 131-132)."""
        child_mem = MagicMock()
        child_mem.rss = 512
        child_mem.vms = 1024

        child = MagicMock()
        child.pid = 456
        child.cpu_percent.return_value = 5.0
        child.memory_info.return_value = child_mem
        child.io_counters.side_effect = NotImplementedError

        mock_proc = _make_mock_process(children=[child])
        collector = ResourceCollector(pid=123)
        collector._create_time = 1000.0
        sample = collector._take_sample(mock_proc)
        # First sample: delta is 0 (no baseline), but I/O is not None
        assert sample.io_read_bytes == 0
        assert sample.io_write_bytes == 0
        assert sample.num_children == 1
        assert sample.memory_rss_bytes == 1024 + 512

    def test_take_sample_children_raises_no_such_process(self) -> None:
        """Cover proc.children() raising NoSuchProcess (lines 135-136)."""
        mock_proc = _make_mock_process()
        mock_proc.children.side_effect = psutil.NoSuchProcess(123)

        collector = ResourceCollector(pid=123)
        collector._create_time = 1000.0
        sample = collector._take_sample(mock_proc)
        # Should still return a valid sample from parent data
        assert sample.cpu_percent == 25.0
        assert sample.memory_rss_bytes == 1024
        assert sample.num_children == 0

    def test_take_sample_io_delta_across_samples(self) -> None:
        """Per-PID I/O delta tracking produces correct totals across samples."""
        mock_proc = _make_mock_process(io_read=100, io_write=200)
        collector = ResourceCollector(pid=123)
        collector._create_time = 1000.0

        # First sample: establishes baseline, delta = 0
        s1 = collector._take_sample(mock_proc)
        assert s1.io_read_bytes == 0
        assert s1.io_write_bytes == 0

        # Update I/O counters for second sample
        io2 = MagicMock()
        io2.read_bytes = 300
        io2.write_bytes = 500
        mock_proc.io_counters.return_value = io2

        s2 = collector._take_sample(mock_proc)
        assert s2.io_read_bytes == 200  # 300 - 100
        assert s2.io_write_bytes == 300  # 500 - 200

    def test_take_sample_child_dies_no_negative_io(self) -> None:
        """When a child dies between samples, I/O delta is never negative."""
        child_mem = MagicMock()
        child_mem.rss = 512
        child_mem.vms = 1024
        child_io = MagicMock()
        child_io.read_bytes = 500
        child_io.write_bytes = 600

        child = MagicMock()
        child.pid = 456
        child.cpu_percent.return_value = 10.0
        child.memory_info.return_value = child_mem
        child.io_counters.return_value = child_io

        mock_proc = _make_mock_process(children=[child], io_read=100, io_write=200)
        collector = ResourceCollector(pid=123)
        collector._create_time = 1000.0

        # First sample: baseline established for both PIDs
        s1 = collector._take_sample(mock_proc)
        assert s1.io_read_bytes == 0  # first sample, no delta

        # Child dies, parent I/O increases
        mock_proc.children.return_value = []
        io2 = MagicMock()
        io2.read_bytes = 150
        io2.write_bytes = 250
        mock_proc.io_counters.return_value = io2

        s2 = collector._take_sample(mock_proc)
        # Only parent delta: 150 - 100 = 50 (child's PID gone, no negative)
        assert s2.io_read_bytes == 50
        assert s2.io_write_bytes == 50

    def test_take_sample_parent_io_denied_child_succeeds(self) -> None:
        """When parent I/O is denied but child succeeds, child I/O is tracked."""
        child_mem = MagicMock()
        child_mem.rss = 512
        child_mem.vms = 1024
        child_io = MagicMock()
        child_io.read_bytes = 100
        child_io.write_bytes = 200

        child = MagicMock()
        child.pid = 456
        child.cpu_percent.return_value = 5.0
        child.memory_info.return_value = child_mem
        child.io_counters.return_value = child_io

        mock_proc = _make_mock_process(io_read=None, io_write=None, children=[child])
        collector = ResourceCollector(pid=123)
        collector._create_time = 1000.0

        s1 = collector._take_sample(mock_proc)
        # Child I/O is tracked even though parent I/O failed
        assert s1.io_read_bytes is not None
        assert s1.io_write_bytes is not None

    @pytest.mark.asyncio
    async def test_start_zombie_process(self) -> None:
        """ZombieProcess (subclass of NoSuchProcess) is caught by start()."""
        with patch(
            "sova.monitoring.collector.psutil.Process",
            side_effect=psutil.ZombieProcess(123),
        ):
            collector = ResourceCollector(pid=123)
            collector.start()
            assert collector._task is None

    @pytest.mark.asyncio
    async def test_start_access_denied(self) -> None:
        """AccessDenied during start() is handled gracefully."""
        with patch(
            "sova.monitoring.collector.psutil.Process",
            side_effect=psutil.AccessDenied(123),
        ):
            collector = ResourceCollector(pid=123)
            collector.start()
            assert collector._task is None

    def test_rolling_aggregates_cover_full_period(self) -> None:
        """get_summary() uses rolling aggregates, not just the sample window."""
        mock_proc = _make_mock_process()
        collector = ResourceCollector(pid=123)
        collector._create_time = 1000.0

        # Simulate adding samples and verify aggregates track the full history
        s1 = collector._take_sample(mock_proc)
        collector.samples.append(s1)
        collector._update_aggregates(s1)

        # Change mock values for second sample
        mem2 = MagicMock()
        mem2.rss = 4096
        mem2.vms = 8192
        mock_proc.memory_info.return_value = mem2
        mock_proc.cpu_percent.return_value = 80.0

        s2 = collector._take_sample(mock_proc)
        collector.samples.append(s2)
        collector._update_aggregates(s2)

        summary = collector.get_summary()
        assert summary.sample_count == 2
        assert summary.peak_cpu_percent == 80.0
        assert summary.peak_memory_rss_bytes == 4096
        assert summary.avg_cpu_percent == pytest.approx((25.0 + 80.0) / 2)

    def test_rolling_aggregates_survive_eviction(self) -> None:
        """Rolling aggregates remain accurate after samples are evicted."""
        mock_proc = _make_mock_process(rss=9999, cpu_percent=99.0)
        collector = ResourceCollector(pid=123)
        collector._create_time = 1000.0

        # Use a tiny deque to force eviction
        from collections import deque

        collector.samples = deque(maxlen=2)

        # Take 3 samples -- first will be evicted
        s1 = collector._take_sample(mock_proc)
        collector.samples.append(s1)
        collector._update_aggregates(s1)

        mock_proc.cpu_percent.return_value = 10.0
        mem2 = MagicMock()
        mem2.rss = 100
        mem2.vms = 200
        mock_proc.memory_info.return_value = mem2

        s2 = collector._take_sample(mock_proc)
        collector.samples.append(s2)
        collector._update_aggregates(s2)

        s3 = collector._take_sample(mock_proc)
        collector.samples.append(s3)
        collector._update_aggregates(s3)

        # s1 (peak) is evicted from deque, but rolling aggregates keep it
        assert len(collector.samples) == 2
        summary = collector.get_summary()
        assert summary.sample_count == 3
        assert summary.peak_cpu_percent == 99.0  # from evicted s1
        assert summary.peak_memory_rss_bytes == 9999  # from evicted s1

    def test_thread_count_in_sample(self) -> None:
        """Thread count is captured in samples."""
        mock_proc = _make_mock_process(num_threads=8)
        collector = ResourceCollector(pid=123)
        collector._create_time = 1000.0

        sample = collector._take_sample(mock_proc)
        assert sample.num_threads == 8

    def test_thread_count_with_children(self) -> None:
        """Thread count aggregates parent and child threads."""
        child_mem = MagicMock()
        child_mem.rss = 512
        child_mem.vms = 1024
        child = MagicMock()
        child.pid = 456
        child.cpu_percent.return_value = 5.0
        child.memory_info.return_value = child_mem
        child.io_counters.side_effect = NotImplementedError
        child.num_threads.return_value = 3

        mock_proc = _make_mock_process(children=[child], num_threads=4)
        collector = ResourceCollector(pid=123)
        collector._create_time = 1000.0

        sample = collector._take_sample(mock_proc)
        assert sample.num_threads == 7  # parent 4 + child 3

    def test_peak_threads_in_summary(self) -> None:
        """Summary tracks peak thread count."""
        samples = [
            ResourceSample(1.0, 10.0, 100, 200, 0, 0, 0, num_threads=4),
            ResourceSample(2.0, 20.0, 200, 300, 0, 0, 0, num_threads=12),
            ResourceSample(3.0, 15.0, 150, 250, 0, 0, 0, num_threads=8),
        ]
        summary = ResourceSummary.from_samples(samples)
        assert summary.peak_num_threads == 12


# ---------------------------------------------------------------------------
# MonitoringConfig tests
# ---------------------------------------------------------------------------


class TestMonitoringConfig:
    def test_defaults(self) -> None:
        from sova.config.models import MonitoringConfig

        cfg = MonitoringConfig()
        assert cfg.enabled is True
        assert cfg.interval == 5.0
        assert cfg.interval > 0

    def test_custom_values(self) -> None:
        from sova.config.models import MonitoringConfig

        cfg = MonitoringConfig(enabled=True, interval=2.0)
        assert cfg.enabled is True
        assert cfg.interval == 2.0

    def test_in_project_config(self) -> None:
        from sova.config.models import MonitoringConfig, ProjectConfig

        cfg = ProjectConfig(monitoring=MonitoringConfig(enabled=True))
        assert cfg.monitoring.enabled is True

    def test_settings_meta_registered(self) -> None:
        from sova.dashboard.settings_meta import GROUP_ORDER, GROUPS, get_meta

        assert "monitoring" in GROUPS
        assert "monitoring" in GROUP_ORDER
        assert get_meta("monitoring.enabled") is not None
        assert get_meta("monitoring.interval") is not None

    def test_loader_nested_section(self) -> None:
        from sova.config.loader import _NESTED_SECTIONS

        assert "monitoring" in _NESTED_SECTIONS


# ---------------------------------------------------------------------------
# DB fixtures for writer and cleanup tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=False)
async def db():
    """Initialize an in-memory DB for resource writer tests."""
    from sova.db.session import close_db, init_db

    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


@pytest.fixture
async def session(db) -> AsyncSession:
    from sova.db.session import get_session

    return await get_session()


async def _create_run(session: AsyncSession, run_id: int, **kwargs) -> None:
    from sova.db.models import TaskRun

    async with session.begin():
        session.add(TaskRun(id=run_id, role="developer", status="running", **kwargs))


# ---------------------------------------------------------------------------
# ResourceWriter tests
# ---------------------------------------------------------------------------


class TestResourceWriter:
    @pytest.mark.asyncio
    async def test_add_and_flush(self, session: AsyncSession) -> None:
        from sqlalchemy import select

        from sova.db.models import ResourceSampleRecord
        from sova.db.session import get_session
        from sova.monitoring.writer import ResourceWriter

        await _create_run(session, 1)

        writer = ResourceWriter(project_dir=None, run_id=1)
        sample = ResourceSample(
            timestamp=writer._base_monotonic + 5.0,
            cpu_percent=50.0,
            memory_rss_bytes=1024,
            memory_vms_bytes=2048,
            io_read_bytes=100,
            io_write_bytes=200,
            num_children=1,
            num_threads=4,
        )
        writer.add_sample(sample)
        await writer.flush()

        async with await get_session() as s:
            async with s.begin():
                rows = (
                    (await s.execute(select(ResourceSampleRecord).where(ResourceSampleRecord.task_run_id == 1)))
                    .scalars()
                    .all()
                )
        assert len(rows) == 1
        assert rows[0].cpu_percent == 50.0
        assert rows[0].memory_rss_bytes == 1024
        assert rows[0].io_read_bytes == 100
        assert rows[0].num_threads == 4

    @pytest.mark.asyncio
    async def test_should_flush_threshold(self) -> None:
        from sova.monitoring.writer import ResourceWriter

        writer = ResourceWriter(project_dir=None, run_id=1, flush_threshold=3)
        assert not writer.should_flush()
        for i in range(3):
            writer.add_sample(
                ResourceSample(
                    timestamp=float(i),
                    cpu_percent=10.0,
                    memory_rss_bytes=100,
                    memory_vms_bytes=200,
                    io_read_bytes=None,
                    io_write_bytes=None,
                    num_children=0,
                )
            )
        assert writer.should_flush()

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self, session: AsyncSession) -> None:
        from sova.monitoring.writer import ResourceWriter

        await _create_run(session, 2)
        writer = ResourceWriter(project_dir=None, run_id=2)
        writer.add_sample(
            ResourceSample(
                timestamp=writer._base_monotonic,
                cpu_percent=10.0,
                memory_rss_bytes=100,
                memory_vms_bytes=200,
                io_read_bytes=None,
                io_write_bytes=None,
                num_children=0,
            )
        )
        await writer.close()
        await writer.close()  # should not raise

    @pytest.mark.asyncio
    async def test_add_sample_after_close_ignored(self) -> None:
        from sova.monitoring.writer import ResourceWriter

        writer = ResourceWriter(project_dir=None, run_id=1)
        await writer.close()
        writer.add_sample(
            ResourceSample(
                timestamp=0.0,
                cpu_percent=10.0,
                memory_rss_bytes=100,
                memory_vms_bytes=200,
                io_read_bytes=None,
                io_write_bytes=None,
                num_children=0,
            )
        )
        assert len(writer._buffer) == 0

    @pytest.mark.asyncio
    async def test_write_summary(self, session: AsyncSession) -> None:
        from sqlalchemy import select

        from sova.db.models import ResourceSummaryRecord
        from sova.db.session import get_session
        from sova.monitoring.writer import ResourceWriter

        await _create_run(session, 3)
        writer = ResourceWriter(project_dir=None, run_id=3)
        summary = ResourceSummary(
            sample_count=10,
            peak_cpu_percent=80.0,
            avg_cpu_percent=40.0,
            peak_memory_rss_bytes=4096,
            peak_memory_vms_bytes=8192,
            total_io_read_bytes=1000,
            total_io_write_bytes=2000,
            peak_num_threads=12,
        )
        await writer.write_summary(summary)

        async with await get_session() as s:
            async with s.begin():
                row = (
                    await s.execute(select(ResourceSummaryRecord).where(ResourceSummaryRecord.task_run_id == 3))
                ).scalar_one()
        assert row.sample_count == 10
        assert row.peak_cpu_percent == 80.0
        assert row.peak_num_threads == 12

    @pytest.mark.asyncio
    async def test_write_summary_empty(self, session: AsyncSession) -> None:
        from sqlalchemy import select

        from sova.db.models import ResourceSummaryRecord
        from sova.db.session import get_session
        from sova.monitoring.writer import ResourceWriter

        await _create_run(session, 4)
        writer = ResourceWriter(project_dir=None, run_id=4)
        await writer.write_summary(ResourceSummary.empty())

        async with await get_session() as s:
            async with s.begin():
                row = (
                    await s.execute(select(ResourceSummaryRecord).where(ResourceSummaryRecord.task_run_id == 4))
                ).scalar_one()
        assert row.sample_count == 0

    @pytest.mark.asyncio
    async def test_monotonic_to_wallclock_conversion(self) -> None:
        from sova.monitoring.writer import ResourceWriter

        writer = ResourceWriter(project_dir=None, run_id=1)
        ts = writer._base_monotonic + 10.0
        wallclock = writer._to_wallclock(ts)
        expected = writer._base_wallclock + timedelta(seconds=10.0)
        assert abs((wallclock - expected).total_seconds()) < 0.01

    @pytest.mark.asyncio
    async def test_flush_empty_buffer_noop(self) -> None:
        from sova.monitoring.writer import ResourceWriter

        writer = ResourceWriter(project_dir=None, run_id=1)
        await writer.flush()  # should not raise


# ---------------------------------------------------------------------------
# Retention cleanup tests
# ---------------------------------------------------------------------------


class TestResourceCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_deletes_old_samples_and_summaries(self, session: AsyncSession) -> None:
        from sqlalchemy import func, select

        from sova.db.models import ResourceSampleRecord, ResourceSummaryRecord
        from sova.db.session import get_session
        from sova.monitoring.writer import ResourceWriter, cleanup_old_resources

        old_time = datetime.now(timezone.utc) - timedelta(days=60)
        await _create_run(session, 10, ended_at=old_time)
        await _create_run(session, 11)  # no ended_at, should be kept

        writer_old = ResourceWriter(project_dir=None, run_id=10)
        writer_old.add_sample(
            ResourceSample(
                timestamp=writer_old._base_monotonic,
                cpu_percent=10.0,
                memory_rss_bytes=100,
                memory_vms_bytes=200,
                io_read_bytes=None,
                io_write_bytes=None,
                num_children=0,
            )
        )
        await writer_old.flush()
        await writer_old.write_summary(ResourceSummary.empty())

        writer_new = ResourceWriter(project_dir=None, run_id=11)
        writer_new.add_sample(
            ResourceSample(
                timestamp=writer_new._base_monotonic,
                cpu_percent=20.0,
                memory_rss_bytes=200,
                memory_vms_bytes=400,
                io_read_bytes=None,
                io_write_bytes=None,
                num_children=0,
            )
        )
        await writer_new.flush()

        deleted = await cleanup_old_resources(project_dir=None, retention_days=30)
        assert deleted == 2  # 1 sample + 1 summary for run 10

        async with await get_session() as s:
            async with s.begin():
                remaining_samples = (await s.execute(select(func.count()).select_from(ResourceSampleRecord))).scalar()
                remaining_summaries = (
                    await s.execute(select(func.count()).select_from(ResourceSummaryRecord))
                ).scalar()
        assert remaining_samples == 1  # run 11's sample
        assert remaining_summaries == 0  # run 11 had no summary

    @pytest.mark.asyncio
    async def test_cleanup_no_old_runs(self, session: AsyncSession) -> None:
        from sova.monitoring.writer import cleanup_old_resources

        deleted = await cleanup_old_resources(project_dir=None, retention_days=30)
        assert deleted == 0


# ---------------------------------------------------------------------------
# ORM model tests
# ---------------------------------------------------------------------------


class TestResourceOrmModels:
    @pytest.mark.asyncio
    async def test_cascade_delete(self, session: AsyncSession) -> None:
        """TaskRun deletion cascades to resource records via ORM."""
        from sqlalchemy import func, select
        from sqlalchemy.orm import selectinload

        from sova.db.models import ResourceSampleRecord, ResourceSummaryRecord, TaskRun
        from sova.db.session import get_session
        from sova.monitoring.writer import ResourceWriter

        await _create_run(session, 20)
        writer = ResourceWriter(project_dir=None, run_id=20)
        writer.add_sample(
            ResourceSample(
                timestamp=writer._base_monotonic,
                cpu_percent=30.0,
                memory_rss_bytes=300,
                memory_vms_bytes=600,
                io_read_bytes=50,
                io_write_bytes=100,
                num_children=0,
            )
        )
        await writer.flush()
        await writer.write_summary(
            ResourceSummary(
                sample_count=1,
                peak_cpu_percent=30.0,
                avg_cpu_percent=30.0,
                peak_memory_rss_bytes=300,
                peak_memory_vms_bytes=600,
                total_io_read_bytes=50,
                total_io_write_bytes=100,
            )
        )

        # Use ORM delete (loads then deletes) to trigger cascade
        async with await get_session() as s:
            async with s.begin():
                run = await s.get(
                    TaskRun,
                    20,
                    options=[selectinload(TaskRun.resource_samples), selectinload(TaskRun.resource_summary)],
                )
                await s.delete(run)

        async with await get_session() as s:
            async with s.begin():
                samples = (
                    await s.execute(
                        select(func.count())
                        .select_from(ResourceSampleRecord)
                        .where(ResourceSampleRecord.task_run_id == 20)
                    )
                ).scalar()
                summaries = (
                    await s.execute(
                        select(func.count())
                        .select_from(ResourceSummaryRecord)
                        .where(ResourceSummaryRecord.task_run_id == 20)
                    )
                ).scalar()
        assert samples == 0
        assert summaries == 0

    @pytest.mark.asyncio
    async def test_taskrun_relationships(self, session: AsyncSession) -> None:
        """TaskRun has resource_samples and resource_summary relationships."""
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from sova.db.models import TaskRun
        from sova.db.session import get_session
        from sova.monitoring.writer import ResourceWriter

        await _create_run(session, 21)
        writer = ResourceWriter(project_dir=None, run_id=21)
        writer.add_sample(
            ResourceSample(
                timestamp=writer._base_monotonic,
                cpu_percent=10.0,
                memory_rss_bytes=100,
                memory_vms_bytes=200,
                io_read_bytes=None,
                io_write_bytes=None,
                num_children=0,
            )
        )
        await writer.flush()
        await writer.write_summary(ResourceSummary.empty())

        async with await get_session() as s:
            async with s.begin():
                run = (
                    await s.execute(
                        select(TaskRun)
                        .where(TaskRun.id == 21)
                        .options(selectinload(TaskRun.resource_samples), selectinload(TaskRun.resource_summary))
                    )
                ).scalar_one()
        assert len(run.resource_samples) == 1
        assert run.resource_summary is not None


# ---------------------------------------------------------------------------
# Lifecycle integration tests
# ---------------------------------------------------------------------------


class TestLifecycleResourceIntegration:
    def test_agent_state_has_resource_fields(self) -> None:
        from sova.dashboard.services.agent_pool import AgentState

        agent = AgentState.__dataclass_fields__
        assert "resource_collector" in agent
        assert "resource_writer" in agent
        assert "resource_flush_task" in agent

    @pytest.mark.asyncio
    async def test_start_resource_monitoring_disabled(self) -> None:
        """When monitoring is disabled, no collector is started."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_lifecycle import _start_resource_monitoring
        from sova.dashboard.services.agent_pool import AgentState

        mock_process = MagicMock()
        mock_process.pid = 999
        agent = AgentState(run_id=1, issue="1", role="developer", process=mock_process)

        with patch("sova.config.loader.load_config") as mock_cfg:
            mock_cfg.return_value.monitoring.enabled = False
            _start_resource_monitoring(agent, Path("/tmp/test"), 999)

        assert agent.resource_collector is None
        assert agent.resource_writer is None

    @pytest.mark.asyncio
    async def test_finalize_resource_monitoring_no_collector(self) -> None:
        """Finalizing with no collector is a no-op."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_lifecycle import _finalize_resource_monitoring
        from sova.dashboard.services.agent_pool import AgentState

        mock_process = MagicMock()
        mock_process.pid = 999
        agent = AgentState(run_id=1, issue="1", role="developer", process=mock_process)
        await _finalize_resource_monitoring(agent)  # should not raise

    @pytest.mark.asyncio
    async def test_finalize_resource_monitoring_full(self, session: AsyncSession) -> None:
        """Finalize stops collector, flushes, writes summary."""
        from unittest.mock import AsyncMock, MagicMock

        from sova.dashboard.services.agent_lifecycle import _finalize_resource_monitoring
        from sova.dashboard.services.agent_pool import AgentState
        from sova.monitoring.writer import ResourceWriter

        await _create_run(session, 30)

        mock_process = MagicMock()
        mock_process.pid = 999
        agent = AgentState(run_id=30, issue="1", role="developer", process=mock_process)

        # Mock collector
        mock_collector = MagicMock()
        summary = ResourceSummary(
            sample_count=5,
            peak_cpu_percent=60.0,
            avg_cpu_percent=30.0,
            peak_memory_rss_bytes=2048,
            peak_memory_vms_bytes=4096,
            total_io_read_bytes=500,
            total_io_write_bytes=1000,
            peak_num_threads=8,
        )
        mock_collector.stop = AsyncMock(return_value=summary)
        mock_collector.samples = []

        writer = ResourceWriter(project_dir=None, run_id=30)
        agent.resource_collector = mock_collector
        agent.resource_writer = writer

        await _finalize_resource_monitoring(agent)

        mock_collector.stop.assert_awaited_once()

        from sqlalchemy import select

        from sova.db.models import ResourceSummaryRecord
        from sova.db.session import get_session

        async with await get_session() as s:
            async with s.begin():
                row = (
                    await s.execute(select(ResourceSummaryRecord).where(ResourceSummaryRecord.task_run_id == 30))
                ).scalar_one()
        assert row.sample_count == 5
        assert row.peak_cpu_percent == 60.0

    @pytest.mark.asyncio
    async def test_finalize_resource_monitoring_cancels_flush_task(self) -> None:
        """Finalize cancels and awaits the flush task, catching its CancelledError."""
        from unittest.mock import MagicMock

        from sova.dashboard.services.agent_lifecycle import _finalize_resource_monitoring
        from sova.dashboard.services.agent_pool import AgentState

        mock_process = MagicMock()
        mock_process.pid = 999
        agent = AgentState(run_id=1, issue="1", role="developer", process=mock_process)

        # Create a real asyncio task that will raise CancelledError when awaited
        async def _hang() -> None:
            await asyncio.sleep(3600)

        flush_task = asyncio.create_task(_hang())
        agent.resource_flush_task = flush_task
        agent.resource_collector = None
        agent.resource_writer = None

        await _finalize_resource_monitoring(agent)
        assert flush_task.cancelled()

    @pytest.mark.asyncio
    async def test_resource_flush_loop_reraises_cancelled(self) -> None:
        """_resource_flush_loop re-raises CancelledError for proper task cleanup."""
        from sova.dashboard.services.agent_lifecycle import _resource_flush_loop
        from sova.dashboard.services.agent_pool import AgentState

        mock_process = MagicMock()
        mock_process.pid = 999
        agent = AgentState(run_id=1, issue="1", role="developer", process=mock_process)

        task = asyncio.create_task(_resource_flush_loop(agent))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_resource_flush_loop_exception_logged(self) -> None:
        """_resource_flush_loop catches non-cancellation exceptions gracefully."""
        from unittest.mock import AsyncMock, MagicMock

        from sova.dashboard.services.agent_lifecycle import _resource_flush_loop
        from sova.dashboard.services.agent_pool import AgentState

        mock_process = MagicMock()
        mock_process.pid = 999
        agent = AgentState(run_id=1, issue="1", role="developer", process=mock_process)

        mock_writer = AsyncMock()
        mock_writer.flush = AsyncMock(side_effect=RuntimeError("boom"))
        agent.resource_writer = mock_writer

        mock_collector = MagicMock()
        mock_collector.samples.__bool__ = MagicMock(return_value=False)
        agent.resource_collector = mock_collector

        async def _sleep_then_raise(_delay: float) -> None:
            raise RuntimeError("boom")

        with patch("sova.dashboard.services.agent_lifecycle.asyncio.sleep", side_effect=_sleep_then_raise):
            await _resource_flush_loop(agent)  # should return via except branch, not raise

    @pytest.mark.asyncio
    async def test_start_resource_monitoring_enabled(self) -> None:
        """When monitoring is enabled, collector, writer, and flush task are created."""
        from collections import deque

        from sova.dashboard.services.agent_lifecycle import _start_resource_monitoring
        from sova.dashboard.services.agent_pool import AgentState

        mock_process = MagicMock()
        mock_process.pid = 999
        agent = AgentState(run_id=1, issue="1", role="developer", process=mock_process)

        mock_collector = MagicMock()
        mock_collector.samples = deque()
        mock_writer = MagicMock()

        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch(
                "sova.monitoring.writer.ResourceWriter",
                return_value=mock_writer,
            ),
            patch(
                "sova.monitoring.collector.ResourceCollector",
                return_value=mock_collector,
            ),
        ):
            mock_cfg.return_value.monitoring.enabled = True
            mock_cfg.return_value.monitoring.interval = 5.0
            _start_resource_monitoring(agent, Path("/tmp/test"), 999)

        assert agent.resource_collector is mock_collector
        assert agent.resource_writer is mock_writer
        assert agent.resource_flush_task is not None
        mock_collector.start.assert_called_once()
        # Cleanup the background task
        agent.resource_flush_task.cancel()
        await asyncio.gather(agent.resource_flush_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_start_resource_monitoring_import_error(self) -> None:
        """When collector import fails, no collector is created (graceful degradation)."""
        from sova.dashboard.services.agent_lifecycle import _start_resource_monitoring
        from sova.dashboard.services.agent_pool import AgentState

        mock_process = MagicMock()
        mock_process.pid = 999
        agent = AgentState(run_id=1, issue="1", role="developer", process=mock_process)

        with (
            patch("sova.config.loader.load_config") as mock_cfg,
            patch(
                "sova.monitoring.collector.ResourceCollector",
                side_effect=ImportError("no psutil"),
            ),
        ):
            mock_cfg.return_value.monitoring.enabled = True
            mock_cfg.return_value.monitoring.interval = 5.0
            _start_resource_monitoring(agent, Path("/tmp/test"), 999)

        assert agent.resource_collector is None

    @pytest.mark.asyncio
    async def test_finalize_resource_monitoring_drains_remaining_samples(self, session: AsyncSession) -> None:
        """Finalize drains leftover samples from the collector deque."""
        from collections import deque
        from unittest.mock import AsyncMock, MagicMock

        from sova.dashboard.services.agent_lifecycle import _finalize_resource_monitoring
        from sova.dashboard.services.agent_pool import AgentState
        from sova.monitoring.writer import ResourceWriter

        await _create_run(session, 31)

        mock_process = MagicMock()
        mock_process.pid = 999
        agent = AgentState(run_id=31, issue="1", role="developer", process=mock_process)

        mock_collector = MagicMock()
        remaining = deque(
            [
                ResourceSample(
                    timestamp=1.0,
                    cpu_percent=10.0,
                    memory_rss_bytes=100,
                    memory_vms_bytes=200,
                    io_read_bytes=0,
                    io_write_bytes=0,
                    num_children=0,
                ),
                ResourceSample(
                    timestamp=2.0,
                    cpu_percent=20.0,
                    memory_rss_bytes=200,
                    memory_vms_bytes=400,
                    io_read_bytes=0,
                    io_write_bytes=0,
                    num_children=0,
                ),
            ]
        )
        mock_collector.stop = AsyncMock(return_value=ResourceSummary.empty())
        mock_collector.samples = remaining

        writer = ResourceWriter(project_dir=None, run_id=31)
        agent.resource_collector = mock_collector
        agent.resource_writer = writer

        await _finalize_resource_monitoring(agent)

        assert len(remaining) == 0  # all drained

    @pytest.mark.asyncio
    async def test_finalize_resource_monitoring_exception_caught(self) -> None:
        """Finalize catches exceptions and does not propagate them."""
        from unittest.mock import AsyncMock, MagicMock

        from sova.dashboard.services.agent_lifecycle import _finalize_resource_monitoring
        from sova.dashboard.services.agent_pool import AgentState

        mock_process = MagicMock()
        mock_process.pid = 999
        agent = AgentState(run_id=1, issue="1", role="developer", process=mock_process)

        mock_collector = MagicMock()
        mock_collector.stop = AsyncMock(side_effect=RuntimeError("collector crash"))
        mock_collector.samples = []
        agent.resource_collector = mock_collector
        agent.resource_writer = MagicMock()

        # Should not raise
        await _finalize_resource_monitoring(agent)

    @pytest.mark.asyncio
    async def test_resource_flush_loop_drains_and_flushes(self) -> None:
        """_resource_flush_loop drains samples from collector and flushes writer."""
        from collections import deque
        from unittest.mock import AsyncMock, MagicMock

        from sova.dashboard.services.agent_lifecycle import _resource_flush_loop
        from sova.dashboard.services.agent_pool import AgentState

        mock_process = MagicMock()
        mock_process.pid = 999
        agent = AgentState(run_id=1, issue="1", role="developer", process=mock_process)

        sample = ResourceSample(
            timestamp=1.0,
            cpu_percent=10.0,
            memory_rss_bytes=100,
            memory_vms_bytes=200,
            io_read_bytes=0,
            io_write_bytes=0,
            num_children=0,
        )

        mock_writer = MagicMock()
        mock_writer.flush = AsyncMock()
        agent.resource_writer = mock_writer

        mock_collector = MagicMock()
        samples_deque = deque([sample])
        mock_collector.samples = samples_deque
        agent.resource_collector = mock_collector

        call_count = 0

        async def _controlled_sleep(_delay: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                # Exit the loop after first flush by setting collector to None
                agent.resource_collector = None

        with patch("sova.dashboard.services.agent_lifecycle.asyncio.sleep", side_effect=_controlled_sleep):
            await _resource_flush_loop(agent)

        mock_writer.add_sample.assert_called_once_with(sample)
        mock_writer.flush.assert_awaited_once()

    def test_cascade_constant_used_in_models(self) -> None:
        """_CASCADE_ALL_DELETE_ORPHAN constant replaces duplicated literals."""
        from sova.db.models import _CASCADE_ALL_DELETE_ORPHAN

        assert _CASCADE_ALL_DELETE_ORPHAN == "all, delete-orphan"


# ---------------------------------------------------------------------------
# Writer error-path tests
# ---------------------------------------------------------------------------


class TestResourceWriterErrorPaths:
    def test_buffer_overflow_drops_oldest(self) -> None:
        """When buffer exceeds MAX_BUFFER_SIZE, oldest samples are dropped."""
        from sova.monitoring.writer import _MAX_BUFFER_SIZE, ResourceWriter

        writer = ResourceWriter(project_dir=None, run_id=1, flush_threshold=9999)
        # Fill buffer to max + extra
        for i in range(_MAX_BUFFER_SIZE + 5):
            writer.add_sample(
                ResourceSample(
                    timestamp=float(i),
                    cpu_percent=1.0,
                    memory_rss_bytes=100,
                    memory_vms_bytes=200,
                    io_read_bytes=0,
                    io_write_bytes=0,
                    num_children=0,
                )
            )
        assert len(writer._buffer) == _MAX_BUFFER_SIZE

    @pytest.mark.asyncio
    async def test_flush_failure_requeues_samples(self) -> None:
        """When DB insert fails, samples are re-added to the buffer for retry."""
        from sova.monitoring.writer import ResourceWriter

        writer = ResourceWriter(project_dir=None, run_id=1)
        sample = ResourceSample(
            timestamp=writer._base_monotonic,
            cpu_percent=10.0,
            memory_rss_bytes=100,
            memory_vms_bytes=200,
            io_read_bytes=0,
            io_write_bytes=0,
            num_children=0,
        )
        writer.add_sample(sample)

        with patch("sova.db.session.get_session", side_effect=RuntimeError("db down")):
            await writer.flush()

        # Sample should be re-added to buffer
        assert len(writer._buffer) == 1

    @pytest.mark.asyncio
    async def test_write_summary_failure_caught(self) -> None:
        """write_summary catches DB errors gracefully."""
        from sova.monitoring.writer import ResourceWriter

        writer = ResourceWriter(project_dir=None, run_id=1)
        summary = ResourceSummary.empty()

        with patch("sova.db.session.get_session", side_effect=RuntimeError("db down")):
            await writer.write_summary(summary)  # should not raise

    @pytest.mark.asyncio
    async def test_cleanup_failure_returns_zero(self) -> None:
        """cleanup_old_resources returns 0 on exception."""
        from sova.monitoring.writer import cleanup_old_resources

        with patch("sova.db.session.get_session", side_effect=RuntimeError("db down")):
            result = await cleanup_old_resources(project_dir=None, retention_days=30)

        assert result == 0

    @pytest.mark.asyncio
    async def test_flush_cancellation_requeues_samples(self) -> None:
        """CancelledError during DB write re-queues samples instead of dropping them."""
        from unittest.mock import AsyncMock

        from sova.monitoring.writer import ResourceWriter

        writer = ResourceWriter(project_dir=None, run_id=1)
        sample = ResourceSample(
            timestamp=writer._base_monotonic,
            cpu_percent=10.0,
            memory_rss_bytes=100,
            memory_vms_bytes=200,
            io_read_bytes=0,
            io_write_bytes=0,
            num_children=0,
        )
        writer.add_sample(sample)

        # Simulate CancelledError during the DB session context manager
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(side_effect=asyncio.CancelledError)
        mock_get_session = AsyncMock(return_value=mock_session)

        with patch("sova.db.session.get_session", mock_get_session):
            with pytest.raises(asyncio.CancelledError):
                await writer.flush()

        # Samples must be re-queued, not lost
        assert len(writer._buffer) == 1
