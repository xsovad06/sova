"""Resource collector for agent subprocess trees.

Samples CPU, memory, and I/O metrics at a configurable interval using
an asyncio background task (fire-and-forget pattern from notifications.py).
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

import psutil

from sova.monitoring.models import ResourceSample, ResourceSummary
from sova.utils.logging import get_logger

log = get_logger(component="monitoring.collector")

# 4 hours at 5s interval = 2880 samples (~160 KB). Generous cap that
# covers most agent runs while preventing unbounded growth.
_MAX_SAMPLES = 4096


class ResourceCollector:
    """Collects resource metrics for a process tree by PID.

    Usage::

        collector = ResourceCollector(pid=agent_process.pid)
        collector.start()
        # ... agent runs ...
        summary = await collector.stop()
    """

    def __init__(self, pid: int, interval: float = 5.0) -> None:
        self.pid = pid
        self.interval = interval
        self.samples: deque[ResourceSample] = deque(maxlen=_MAX_SAMPLES)
        self._task: asyncio.Task[None] | None = None
        self._create_time: float | None = None
        self._stop_event: asyncio.Event | None = None
        # Per-PID I/O baselines for delta computation. Tracks cumulative
        # counters from the previous sample so that children dying between
        # samples don't produce negative totals.
        self._prev_io: dict[int, tuple[int, int]] = {}

    def start(self) -> None:
        """Start the background sampling loop."""
        if self._task is not None and not self._task.done():
            return

        try:
            proc = psutil.Process(self.pid)
            self._create_time = proc.create_time()
            # Prime cpu_percent() so the first real sample returns a
            # meaningful value instead of 0.0 (psutil needs a prior
            # measurement as baseline).
            proc.cpu_percent()
        except psutil.NoSuchProcess:
            log.warning("collector.start_failed", pid=self.pid, reason="process_not_found")
            return

        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._sample_loop(proc))

    async def stop(self) -> ResourceSummary:
        """Stop sampling and return the summary."""
        if self._task is not None and not self._task.done():
            if self._stop_event is not None:
                self._stop_event.set()
            await self._task
        self._task = None
        self._stop_event = None
        return self.get_summary()

    def get_summary(self) -> ResourceSummary:
        """Compute summary from samples collected so far."""
        return ResourceSummary.from_samples(self.samples)

    async def _sample_loop(self, proc: psutil.Process) -> None:
        """Sample the process tree at regular intervals."""
        stop = self._stop_event
        while stop is not None and not stop.is_set():
            try:
                # PID reuse check
                if proc.create_time() != self._create_time:
                    log.warning("collector.pid_reuse", pid=self.pid)
                    return

                sample = self._take_sample(proc)
                self.samples.append(sample)
            except psutil.NoSuchProcess:
                log.info("collector.process_exited", pid=self.pid, samples=len(self.samples))
                return

            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval)
                return  # stop event was set
            except TimeoutError:
                pass  # interval elapsed, continue sampling

    def _take_sample(self, proc: psutil.Process) -> ResourceSample:
        """Take a single resource measurement of the process tree.

        I/O values are **deltas since the previous sample**, computed per-PID.
        This avoids incorrect (negative) totals when child processes die
        between samples and their cumulative counters disappear from the tree.

        CPU measurements for newly-discovered child processes will be 0.0 in
        their first sample as psutil requires a baseline measurement.
        """
        cpu = proc.cpu_percent()
        mem = proc.memory_info()
        rss = mem.rss
        vms = mem.vms

        # Collect per-PID I/O and compute deltas against previous sample.
        # has_any_io tracks whether at least one process reported I/O.
        io_delta_read = 0
        io_delta_write = 0
        has_any_io = False
        current_io: dict[int, tuple[int, int]] = {}

        try:
            io = proc.io_counters()
            current_io[self.pid] = (io.read_bytes, io.write_bytes)
            has_any_io = True
        except (psutil.AccessDenied, NotImplementedError, AttributeError):
            pass

        num_children = 0
        try:
            children = proc.children(recursive=True)
            num_children = len(children)
            for child in children:
                try:
                    child_cpu = child.cpu_percent()
                    cpu += child_cpu
                    child_mem = child.memory_info()
                    rss += child_mem.rss
                    vms += child_mem.vms
                    try:
                        child_io = child.io_counters()
                        current_io[child.pid] = (child_io.read_bytes, child_io.write_bytes)
                        has_any_io = True
                    except (psutil.AccessDenied, NotImplementedError, AttributeError):
                        pass
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except psutil.NoSuchProcess:
            pass

        # Compute deltas for each PID that has both a previous and current reading
        for pid, (cur_r, cur_w) in current_io.items():
            if pid in self._prev_io:
                prev_r, prev_w = self._prev_io[pid]
                io_delta_read += cur_r - prev_r
                io_delta_write += cur_w - prev_w

        self._prev_io = current_io

        return ResourceSample(
            timestamp=time.monotonic(),
            cpu_percent=cpu,
            memory_rss_bytes=rss,
            memory_vms_bytes=vms,
            io_read_bytes=io_delta_read if has_any_io else None,
            io_write_bytes=io_delta_write if has_any_io else None,
            num_children=num_children,
        )
