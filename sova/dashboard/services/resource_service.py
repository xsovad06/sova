"""Resource monitoring queries -- summary, samples, and live metrics."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections import deque
from datetime import timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import psutil

if TYPE_CHECKING:
    from sova.monitoring.collector import ResourceCollector
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sova.dashboard.services.agent_pool import _get_project_agents
from sova.db.models import ResourceSampleRecord, ResourceSummaryRecord, TaskRun
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.resources")

_SYSTEM_HISTORY_MAX = 60

_system_metrics_history: deque[dict] = deque(maxlen=_SYSTEM_HISTORY_MAX)
_history_lock = threading.Lock()


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
        # (total - available) matches Activity Monitor and is consistent with mem.percent.
        memory_used = mem.total - mem.available
        memory_percent = mem.percent
        cpu_count = psutil.cpu_count()
    except (psutil.Error, OSError, RuntimeError) as e:
        log.warning("system_metrics.psutil_error", error_type=type(e).__name__, exc_info=True)
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

    with _history_lock:
        _system_metrics_history.append(
            {
                "cpu_percent": cpu_percent,
                "memory_percent": memory_percent,
                "timestamp": time.time(),
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


def get_system_metrics_history() -> list[dict]:
    """Return the accumulated system metrics history (up to 5 minutes)."""
    with _history_lock:
        return list(_system_metrics_history)


def _extract_latest_metrics(collector: ResourceCollector | None) -> tuple[float | None, int | None]:
    """Extract latest CPU and memory from a resource collector."""
    if collector is None or not collector.samples:
        return None, None
    latest = collector.samples[-1]
    return latest.cpu_percent, latest.memory_rss_bytes


def _summary_to_dict(s: ResourceSummaryRecord) -> dict:
    d = {
        "sample_count": s.sample_count,
        "peak_cpu_percent": float(s.peak_cpu_percent),
        "avg_cpu_percent": float(s.avg_cpu_percent),
        "peak_memory_rss_bytes": s.peak_memory_rss_bytes,
        "peak_memory_vms_bytes": s.peak_memory_vms_bytes,
        "total_io_read_bytes": s.total_io_read_bytes,
        "total_io_write_bytes": s.total_io_write_bytes,
        "peak_num_threads": s.peak_num_threads,
    }
    if s.energy_wh is not None:
        d["energy_wh"] = float(s.energy_wh)
        d["co2_grams"] = float(s.co2_grams) if s.co2_grams is not None else None
        d["chip_name"] = s.chip_name
        d["tdp_watts"] = float(s.tdp_watts) if s.tdp_watts is not None else None
    return d


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


def get_cross_project_metrics(project_dir: Path | None = None) -> dict:
    """Get aggregated metrics from all SOVA dashboards on this machine.

    Reads JSON snapshots written by each dashboard's MetricsSnapshotWriter.
    """
    from sova.monitoring.cross_project import read_cross_project_metrics

    resolved = (project_dir or Path.cwd()).resolve()
    return read_cross_project_metrics(resolved)


async def get_total_energy(project_dir: Path | None = None) -> dict:
    """Get total energy consumption across all runs with energy data."""
    from sova.db.session import get_session

    try:
        async with await get_session(project_dir=project_dir) as session:
            from sqlalchemy import func

            result = await session.execute(
                select(
                    func.sum(ResourceSummaryRecord.energy_wh),
                    func.sum(ResourceSummaryRecord.co2_grams),
                    func.count(ResourceSummaryRecord.id),
                ).where(ResourceSummaryRecord.energy_wh.isnot(None))
            )
            row = result.one()
            total = Decimal(str(row[0])) if row[0] is not None else Decimal("0")
            total_co2 = Decimal(str(row[1])) if row[1] is not None else Decimal("0")
            count = row[2]
        return {
            "total_energy_wh": str(round(total, 4)),
            "total_co2_grams": str(round(total_co2, 4)),
            "run_count": count,
        }
    except Exception:
        log.warning("total_energy.query_failed", exc_info=True)
        raise


async def get_capacity_recommendation(project_dir: Path | None = None) -> dict:
    """Get a capacity recommendation based on historical resource data."""
    from dataclasses import asdict

    from sova.config.loader import load_config
    from sova.db.session import get_session
    from sova.monitoring.advisor import recommend_capacity

    try:
        cfg = load_config(project_dir)
    except Exception:
        log.warning("capacity.config_load_failed", exc_info=True)
        raise

    current_max = getattr(cfg, "max_parallel_agents", 3)
    safety_margin = cfg.monitoring.safety_margin

    # Fetch historical summaries from DB
    summaries: list[dict] = []
    latest_time = None
    try:
        async with await get_session(project_dir=project_dir) as session:
            stmt = (
                select(ResourceSummaryRecord)
                .where(ResourceSummaryRecord.sample_count > 0)
                .order_by(ResourceSummaryRecord.created_at.desc())
                .limit(50)
            )
            result = await session.execute(stmt)
            records = result.scalars().all()
            for r in records:
                summaries.append(
                    {
                        "avg_cpu_percent": float(r.avg_cpu_percent),
                        "peak_cpu_percent": float(r.peak_cpu_percent),
                        "peak_memory_rss_bytes": r.peak_memory_rss_bytes,
                    }
                )
            if records:
                latest_time = records[0].created_at
    except Exception:
        log.warning("capacity.db_query_failed", exc_info=True)
        raise

    # System metrics
    try:
        cpu_count = psutil.cpu_count() or 1
        mem = psutil.virtual_memory()
        cpu_percent = psutil.cpu_percent(interval=None) or 0.0
    except Exception:
        cpu_count = 1
        mem = None
        cpu_percent = 0.0

    total_memory = mem.total if mem else 0
    memory_percent = mem.percent if mem else 0.0

    # Cross-project metrics
    cross_cpu = 0.0
    try:
        resolved = (project_dir or Path.cwd()).resolve()
        cross = await asyncio.to_thread(get_cross_project_metrics, resolved)
        totals = cross.get("machine_totals", {})
        cross_cpu = totals.get("total_agent_cpu_percent", 0.0)
    except Exception:
        log.warning("capacity.cross_project_failed", exc_info=True)

    rec = recommend_capacity(
        summaries=summaries,
        current_max=current_max,
        cpu_count=cpu_count,
        total_memory_bytes=total_memory,
        current_cpu_percent=cpu_percent,
        current_memory_percent=memory_percent,
        cross_project_cpu_percent=cross_cpu,
        safety_margin=safety_margin,
        latest_summary_time=latest_time,
    )
    return asdict(rec)


def _downsample(samples: list, limit: int) -> list:
    """Take every Nth sample to fit within limit."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    if len(samples) <= limit:
        return samples
    step = len(samples) / limit
    return [samples[int(i * step)] for i in range(limit)]
