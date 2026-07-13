"""Resource monitoring queries -- summary, samples, and live metrics."""

from __future__ import annotations

import os
from datetime import timezone

import psutil
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sova.dashboard.services.agent_pool import _get_project_agents
from sova.db.models import ResourceSampleRecord, ResourceSummaryRecord, TaskRun
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.resources")


async def get_resource_summary(session: AsyncSession, run_id: int) -> dict | None:
    """Get the persisted resource summary for a run, or None if absent."""
    run = await session.get(TaskRun, run_id)
    if run is None:
        return None
    summary = await session.scalar(select(ResourceSummaryRecord).where(ResourceSummaryRecord.task_run_id == run_id))
    if summary is None:
        return {"run_id": run_id, "summary": None}
    return {
        "run_id": run_id,
        "summary": _summary_to_dict(summary),
    }


async def get_resource_samples(
    session: AsyncSession,
    run_id: int,
    *,
    limit: int = 500,
) -> dict | None:
    """Get time-series resource samples for a run, downsampled if needed."""
    run = await session.get(TaskRun, run_id)
    if run is None:
        return None

    stmt = (
        select(ResourceSampleRecord)
        .where(ResourceSampleRecord.task_run_id == run_id)
        .order_by(ResourceSampleRecord.sampled_at.asc())
    )
    result = await session.execute(stmt)
    all_samples = result.scalars().all()

    samples = _downsample(all_samples, limit)

    return {
        "run_id": run_id,
        "total_count": len(all_samples),
        "returned_count": len(samples),
        "samples": [_sample_to_dict(s) for s in samples],
    }


def get_live_metrics(run_id: int, slug: str | None = None) -> dict | None:
    """Get live CPU/memory from the in-memory collector for a running agent."""
    pa = _get_project_agents(slug)
    agent = pa.agents.get(run_id)
    if agent is None:
        return None
    cpu, mem = _extract_latest_metrics(agent.resource_collector)
    return {"run_id": run_id, "cpu_percent": cpu, "memory_rss_bytes": mem}


def get_system_info() -> dict:
    """Get static system info (CPU count, total memory)."""
    return {
        "cpu_count": psutil.cpu_count(),
        "total_memory_bytes": psutil.virtual_memory().total,
    }


def get_system_metrics(slug: str | None = None) -> dict:
    """Get real-time system metrics and per-agent resource data.

    No DB queries -- reads from psutil and the in-memory agent pool.
    """
    try:
        cpu_percent: float | None = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        memory_total = mem.total
        memory_used = mem.used
        memory_percent = mem.percent
        cpu_count = psutil.cpu_count()
    except Exception:
        return {"available": False}

    load_avg: list[float] | None = None
    if hasattr(os, "getloadavg"):
        try:
            load_avg = list(os.getloadavg())
        except OSError:
            pass

    pa = _get_project_agents(slug)
    agents = []
    for agent in pa.agents.values():
        agent_cpu, agent_mem = _extract_latest_metrics(agent.resource_collector)
        agents.append(
            {
                "run_id": agent.run_id,
                "issue": agent.issue,
                "role": agent.role,
                "cpu_percent": agent_cpu,
                "memory_rss_bytes": agent_mem,
            }
        )

    return {
        "available": True,
        "system": {
            "cpu_percent": cpu_percent,
            "cpu_count": cpu_count,
            "memory_total_bytes": memory_total,
            "memory_used_bytes": memory_used,
            "memory_percent": memory_percent,
            "load_avg": load_avg,
        },
        "agents": agents,
        "agent_slots": {
            "used": len(pa.agents),
            "max": pa.max_concurrent,
        },
    }


def _extract_latest_metrics(collector: object) -> tuple[float | None, int | None]:
    """Extract latest CPU and memory from a resource collector."""
    if collector is None or not collector.samples:
        return None, None
    latest = collector.samples[-1]
    return latest.cpu_percent, latest.memory_rss_bytes


def _summary_to_dict(s: ResourceSummaryRecord) -> dict:
    return {
        "sample_count": s.sample_count,
        "peak_cpu_percent": float(s.peak_cpu_percent),
        "avg_cpu_percent": float(s.avg_cpu_percent),
        "peak_memory_rss_bytes": s.peak_memory_rss_bytes,
        "peak_memory_vms_bytes": s.peak_memory_vms_bytes,
        "total_io_read_bytes": s.total_io_read_bytes,
        "total_io_write_bytes": s.total_io_write_bytes,
        "peak_num_threads": s.peak_num_threads,
    }


def _sample_to_dict(s: ResourceSampleRecord) -> dict:
    ts = s.sampled_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return {
        "sampled_at": ts.isoformat(),
        "cpu_percent": float(s.cpu_percent),
        "memory_rss_bytes": s.memory_rss_bytes,
        "memory_vms_bytes": s.memory_vms_bytes,
        "io_read_bytes": s.io_read_bytes,
        "io_write_bytes": s.io_write_bytes,
        "num_children": s.num_children,
        "num_threads": s.num_threads,
    }


def _downsample(samples: list, limit: int) -> list:
    """Take every Nth sample to fit within limit."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    if len(samples) <= limit:
        return samples
    step = len(samples) / limit
    return [samples[int(i * step)] for i in range(limit)]
