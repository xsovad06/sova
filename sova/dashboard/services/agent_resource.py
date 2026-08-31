"""Agent resource monitoring: start, flush, finalize, energy compute, cancel.

Manages per-agent resource collectors (CPU, memory) and background I/O tasks.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sova.monitoring.models import ResourceSummary

from sova.dashboard.services.agent_pool import AgentState
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.control")

_background_tasks: set[asyncio.Task[None]] = set()


async def _cancel_agent_io_tasks(agent: AgentState) -> list[asyncio.Task]:
    """Cancel per-agent I/O tasks and stop the resource collector."""
    cancelled: list[asyncio.Task] = []
    for attr in ("reader_task", "stderr_task", "resource_flush_task"):
        task = getattr(agent, attr, None)
        if task is not None and not task.done():
            task.cancel()
            cancelled.append(task)
    if agent.resource_collector is not None:
        try:
            await asyncio.wait_for(agent.resource_collector.stop(), timeout=3.0)
        except Exception:
            log.warning("resource_collector.stop_failed", run_id=agent.run_id, exc_info=True)
    return cancelled


async def cancel_background_tasks() -> None:
    """Cancel ALL background tasks (per-agent I/O readers, resource flushers,
    wait/finalize, and state transition tasks).

    Called during lifespan shutdown to prevent orphaned subprocess I/O tasks
    and in-flight DB queries from blocking uvicorn reload.
    """
    from sova.dashboard.services.agent_pool import _projects

    all_tasks: list[asyncio.Task] = []
    for pa in _projects.values():
        for agent in pa.agents.values():
            all_tasks.extend(await _cancel_agent_io_tasks(agent))

    for t in _background_tasks:
        if not t.done():
            t.cancel()
            all_tasks.append(t)

    if all_tasks:
        try:
            await asyncio.wait_for(
                asyncio.gather(*all_tasks, return_exceptions=True),
                timeout=3.0,
            )
        except TimeoutError:
            log.warning(
                "cancel_background_tasks.timeout",
                pending=[t.get_name() for t in all_tasks if not t.done()],
            )
    _background_tasks.clear()


def _start_resource_monitoring(agent: AgentState, project_dir: Path, pid: int) -> None:
    """Start resource collector and writer for an agent (if monitoring is enabled)."""
    try:
        from sova.config.loader import load_config

        cfg = load_config(project_dir)
        if not cfg.monitoring.enabled:
            return
    except Exception:
        log.debug("resource_monitoring.config_load_failed", exc_info=True)
        return

    try:
        from sova.monitoring.collector import ResourceCollector
        from sova.monitoring.writer import ResourceWriter

        collector = ResourceCollector(pid=pid, interval=cfg.monitoring.interval)
        collector.start()
        writer = ResourceWriter(project_dir, agent.run_id)
        agent.resource_collector = collector
        agent.resource_writer = writer
        agent.resource_flush_task = asyncio.create_task(_resource_flush_loop(agent))
    except Exception:
        log.debug("resource_monitoring.start_failed", run_id=agent.run_id, exc_info=True)


async def _finalize_resource_monitoring(agent: AgentState) -> None:
    """Stop the collector, flush remaining samples, write summary, close writer."""
    try:
        # Cancel the periodic flush task
        if agent.resource_flush_task and not agent.resource_flush_task.done():
            agent.resource_flush_task.cancel()
            # gather(return_exceptions=True) suppresses the expected CancelledError
            # from the child task without swallowing our own cancellation.
            await asyncio.gather(agent.resource_flush_task, return_exceptions=True)

        collector = agent.resource_collector
        writer = agent.resource_writer
        if collector is None or writer is None:
            return

        summary = await collector.stop()

        # Drain any remaining samples from the deque
        while collector.samples:
            writer.add_sample(collector.samples.popleft())

        await writer.write_summary(summary)
        await writer.close()

        # Compute energy estimate and update the summary record
        await _compute_and_store_energy(agent.run_id, summary, agent.project_dir)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning("resource_monitoring.finalize_failed", run_id=agent.run_id, exc_info=True)


async def _compute_and_store_energy(
    run_id: int,
    summary: ResourceSummary,
    project_dir: Path | None,
) -> None:
    """Compute energy estimate from run duration and update the summary record."""
    from sova.db.models import ResourceSummaryRecord, TaskRun
    from sova.db.session import get_session
    from sova.monitoring.energy import estimate_energy

    try:
        async with await get_session(project_dir=project_dir) as session:
            task_run = await session.get(TaskRun, run_id)
            if task_run is None or task_run.started_at is None:
                return

            if task_run.ended_at is not None:
                duration = (task_run.ended_at - task_run.started_at).total_seconds()
            else:
                duration = (datetime.now(timezone.utc) - task_run.started_at).total_seconds()

            # Get config overrides
            tdp_override = None
            co2_grams_per_kwh = 436.0
            try:
                from sova.config.loader import load_config

                cfg = load_config(project_dir)
                tdp_override = cfg.monitoring.tdp_override
                co2_grams_per_kwh = cfg.monitoring.co2_grams_per_kwh
            except Exception:
                log.debug("energy.config_load_failed", run_id=run_id, exc_info=True)

            estimate = estimate_energy(
                avg_cpu_percent=summary.avg_cpu_percent,
                duration_seconds=duration,
                tdp_watts=tdp_override,
                co2_grams_per_kwh=co2_grams_per_kwh,
            )
            if estimate is None:
                return

            from sqlalchemy import select

            stmt = select(ResourceSummaryRecord).where(ResourceSummaryRecord.task_run_id == run_id)
            record = await session.scalar(stmt)
            if record is None:
                return

            record.energy_wh = estimate.energy_wh
            record.co2_grams = estimate.co2_grams
            record.chip_name = estimate.chip_name
            record.tdp_watts = estimate.tdp_watts
            await session.commit()
            log.debug(
                "energy.computed",
                run_id=run_id,
                energy_wh=estimate.energy_wh,
                chip=estimate.chip_name,
            )
    except Exception:
        log.debug("energy.compute_failed", run_id=run_id, exc_info=True)


async def _resource_flush_loop(agent: AgentState) -> None:
    """Periodically flush buffered resource samples to the database."""
    try:
        while True:
            await asyncio.sleep(30.0)
            collector = agent.resource_collector
            writer = agent.resource_writer
            if collector is None or writer is None:
                return
            # Drain with popleft to avoid losing samples appended during iteration
            while collector.samples:
                writer.add_sample(collector.samples.popleft())
            await writer.flush()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.debug("resource_flush_loop.failed", run_id=agent.run_id, exc_info=True)
