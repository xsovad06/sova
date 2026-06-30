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
        """Take a single resource measurement of the process tree."""
        cpu = proc.cpu_percent()
        mem = proc.memory_info()
        rss = mem.rss
        vms = mem.vms

        io_read: int | None = None
        io_write: int | None = None
        try:
            io = proc.io_counters()
            io_read = io.read_bytes
            io_write = io.write_bytes
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
                        if io_read is not None:
                            io_read += child_io.read_bytes
                        if io_write is not None:
                            io_write += child_io.write_bytes
                    except (psutil.AccessDenied, NotImplementedError, AttributeError):
                        pass
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except psutil.NoSuchProcess:
            pass

        return ResourceSample(
            timestamp=time.monotonic(),
            cpu_percent=cpu,
            memory_rss_bytes=rss,
            memory_vms_bytes=vms,
            io_read_bytes=io_read,
            io_write_bytes=io_write,
            num_children=num_children,
        )
