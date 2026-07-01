"""Data models for resource monitoring samples and summaries."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResourceSample:
    """A single point-in-time resource measurement for a process tree.

    I/O values are per-interval deltas (computed per-PID in ResourceCollector).
    CPU measurements for newly-discovered child processes will be 0.0 in their
    first sample as psutil requires a baseline measurement.
    """

    timestamp: float
    cpu_percent: float
    memory_rss_bytes: int
    memory_vms_bytes: int
    io_read_bytes: int | None
    io_write_bytes: int | None
    num_children: int
    num_threads: int = 0


@dataclass(frozen=True, slots=True)
class ResourceSummary:
    """Aggregated summary computed from a series of ResourceSamples.

    When constructed via ``from_samples``, only the in-memory sample window
    is considered.  ``ResourceCollector.get_summary()`` uses rolling aggregates
    that cover the entire monitoring period even when early samples have been
    evicted from the bounded deque.
    """

    sample_count: int
    peak_cpu_percent: float
    avg_cpu_percent: float
    peak_memory_rss_bytes: int
    peak_memory_vms_bytes: int
    total_io_read_bytes: int | None
    total_io_write_bytes: int | None
    peak_num_threads: int = 0

    @classmethod
    def empty(cls) -> ResourceSummary:
        return cls(
            sample_count=0,
            peak_cpu_percent=0.0,
            avg_cpu_percent=0.0,
            peak_memory_rss_bytes=0,
            peak_memory_vms_bytes=0,
            total_io_read_bytes=None,
            total_io_write_bytes=None,
            peak_num_threads=0,
        )

    @classmethod
    def from_samples(cls, samples: Sequence[ResourceSample]) -> ResourceSummary:
        if not samples:
            return cls.empty()

        cpu_values = [s.cpu_percent for s in samples]

        # Each sample's I/O values are per-interval deltas (computed per-PID
        # in ResourceCollector). Total I/O during monitoring = sum of deltas.
        io_read: int | None = None
        io_write: int | None = None
        io_samples = [s for s in samples if s.io_read_bytes is not None]
        if io_samples:
            io_read = sum(s.io_read_bytes for s in io_samples)  # type: ignore[arg-type]
            io_write = sum(s.io_write_bytes for s in io_samples)  # type: ignore[arg-type]

        return cls(
            sample_count=len(samples),
            peak_cpu_percent=max(cpu_values),
            avg_cpu_percent=sum(cpu_values) / len(cpu_values),
            peak_memory_rss_bytes=max(s.memory_rss_bytes for s in samples),
            peak_memory_vms_bytes=max(s.memory_vms_bytes for s in samples),
            total_io_read_bytes=io_read,
            total_io_write_bytes=io_write,
            peak_num_threads=max(s.num_threads for s in samples),
        )
