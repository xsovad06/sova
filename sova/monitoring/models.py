"""Data models for resource monitoring samples and summaries."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResourceSample:
    """A single point-in-time resource measurement for a process tree."""

    timestamp: float
    cpu_percent: float
    memory_rss_bytes: int
    memory_vms_bytes: int
    io_read_bytes: int | None
    io_write_bytes: int | None
    num_children: int


@dataclass(frozen=True, slots=True)
class ResourceSummary:
    """Aggregated summary computed from a series of ResourceSamples."""

    sample_count: int
    peak_cpu_percent: float
    avg_cpu_percent: float
    peak_memory_rss_bytes: int
    peak_memory_vms_bytes: int
    total_io_read_bytes: int | None
    total_io_write_bytes: int | None

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
        )

    @classmethod
    def from_samples(cls, samples: Sequence[ResourceSample]) -> ResourceSummary:
        if not samples:
            return cls.empty()

        cpu_values = [s.cpu_percent for s in samples]

        # I/O counters are cumulative -- total = last - first
        io_read: int | None = None
        io_write: int | None = None
        if samples[0].io_read_bytes is not None and samples[-1].io_read_bytes is not None:
            io_read = samples[-1].io_read_bytes - samples[0].io_read_bytes
        if samples[0].io_write_bytes is not None and samples[-1].io_write_bytes is not None:
            io_write = samples[-1].io_write_bytes - samples[0].io_write_bytes

        return cls(
            sample_count=len(samples),
            peak_cpu_percent=max(cpu_values),
            avg_cpu_percent=sum(cpu_values) / len(cpu_values),
            peak_memory_rss_bytes=max(s.memory_rss_bytes for s in samples),
            peak_memory_vms_bytes=max(s.memory_vms_bytes for s in samples),
            total_io_read_bytes=io_read,
            total_io_write_bytes=io_write,
        )
