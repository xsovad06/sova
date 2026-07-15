"""Cross-project metrics sharing via file-based JSON snapshots.

Each dashboard periodically writes a JSON snapshot of its resource usage to
``~/.sova/metrics/{slug}.json``.  A reader aggregates all snapshots, filtering
stale entries.  This enables the resource widget to show machine-wide resource
usage across all running SOVA dashboards.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psutil

from sova.utils.logging import get_logger

log = get_logger(component="monitoring.cross_project")

_DEFAULT_METRICS_DIR = Path.home() / ".sova" / "metrics"
_WRITE_INTERVAL = 10.0  # seconds between snapshot writes
_STALE_THRESHOLD = 30.0  # seconds before a snapshot is considered stale


def _slugify(project_dir: Path) -> str:
    """Derive a filesystem-safe slug from a project directory path."""
    return str(project_dir).replace("/", "_").replace("\\", "_").strip("_")


class MetricsSnapshotWriter:
    """Periodically writes a JSON snapshot of this dashboard's metrics."""

    def __init__(
        self,
        project_dir: Path,
        project_name: str,
        dashboard_port: int,
        *,
        get_metrics_fn: Callable[[], dict[str, Any]] | None = None,
        metrics_dir: Path | None = None,
    ) -> None:
        self._project_dir = project_dir
        self._project_name = project_name
        self._dashboard_port = dashboard_port
        self._get_metrics = get_metrics_fn
        self._metrics_dir = metrics_dir or _DEFAULT_METRICS_DIR
        self._slug = _slugify(project_dir)
        self._snapshot_path = self._metrics_dir / f"{self._slug}.json"
        self._task: asyncio.Task | None = None
        self._enabled = True
        self._pid = os.getpid()

    def start(self) -> None:
        """Start the background snapshot writer."""
        try:
            self._metrics_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("cross_project.mkdir_failed", path=str(self._metrics_dir), error=str(e))
            self._enabled = False
            return

        # Write one snapshot immediately so the widget has data on startup
        try:
            self._write_snapshot()
        except Exception:
            log.warning("cross_project.initial_write_error", slug=self._slug, exc_info=True)

        self._task = asyncio.create_task(self._write_loop())
        log.info("cross_project.writer_started", slug=self._slug)

    async def stop(self) -> None:
        """Stop the writer and clean up the snapshot file."""
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

        try:
            self._snapshot_path.unlink(missing_ok=True)
        except OSError:
            pass
        log.info("cross_project.writer_stopped", slug=self._slug)

    async def _write_loop(self) -> None:
        """Write snapshots at a fixed interval."""
        while True:
            await asyncio.sleep(_WRITE_INTERVAL)
            try:
                self._write_snapshot()
            except Exception:
                log.warning("cross_project.write_error", slug=self._slug, exc_info=True)

    def _write_snapshot(self) -> None:
        """Write a single metric snapshot atomically."""
        if not self._enabled:
            return

        if self._get_metrics is None:
            from sova.dashboard.services.resource_service import get_system_metrics

            data = get_system_metrics()
        else:
            data = self._get_metrics()

        if not data.get("available"):
            return

        snapshot = {
            "timestamp": time.time(),
            "pid": self._pid,
            "project_name": self._project_name,
            "project_dir": str(self._project_dir),
            "dashboard_port": self._dashboard_port,
            "system": data.get("system", {}),
            "agents": data.get("agents", []),
            "agent_slots": data.get("agent_slots", {}),
        }

        # Atomic write: write to temp file, then rename
        try:
            fd, tmp_path = tempfile.mkstemp(dir=str(self._metrics_dir), suffix=".tmp", prefix=self._slug)
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(snapshot, f)
                os.replace(tmp_path, str(self._snapshot_path))
            except BaseException:
                # Clean up temp file on failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as e:
            log.warning("cross_project.atomic_write_failed", slug=self._slug, error=str(e))
            self._enabled = False


def read_cross_project_metrics(
    current_project_dir: Path,
    *,
    metrics_dir: Path | None = None,
) -> dict:
    """Read and aggregate metrics from all running SOVA dashboards.

    Returns a dict with:
      - ``this_project``: metrics for the current project (or None)
      - ``other_projects``: list of metrics from other projects
      - ``machine_totals``: aggregated machine-wide totals
    """
    mdir = metrics_dir or _DEFAULT_METRICS_DIR
    current_slug = _slugify(current_project_dir)
    now = time.time()

    this_project: dict | None = None
    other_projects: list[dict] = []

    if not mdir.is_dir():
        return {
            "this_project": None,
            "other_projects": [],
            "machine_totals": _empty_totals(),
        }

    for path in mdir.glob("*.json"):
        snapshot = _read_snapshot(path, now)
        if snapshot is None:
            continue

        slug = path.stem
        if slug == current_slug:
            this_project = snapshot
        else:
            other_projects.append(snapshot)

    all_projects = ([this_project] if this_project else []) + other_projects
    totals = _aggregate_totals(all_projects)

    return {
        "this_project": this_project,
        "other_projects": other_projects,
        "machine_totals": totals,
    }


def _read_snapshot(path: Path, now: float) -> dict | None:
    """Read and validate a single snapshot file. Returns None if stale or corrupt."""
    try:
        raw = path.read_text()
    except OSError:
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    # Fast-path: if the writing process is dead, the snapshot is stale
    pid = data.get("pid")
    if isinstance(pid, int) and not psutil.pid_exists(pid):
        return None

    ts = data.get("timestamp")
    if not isinstance(ts, (int, float)):
        return None

    age = now - ts
    if age > _STALE_THRESHOLD:
        return None

    return {
        "project_name": data.get("project_name", "Unknown"),
        "project_dir": data.get("project_dir", ""),
        "dashboard_port": data.get("dashboard_port"),
        "agents": data.get("agents", []),
        "agent_slots": data.get("agent_slots", {}),
        "system": data.get("system", {}),
        "age_seconds": round(age, 1),
    }


def _aggregate_totals(projects: list[dict]) -> dict:
    """Aggregate resource totals across all projects."""
    if not projects:
        return _empty_totals()

    total_agents_used = 0
    total_agents_max = 0
    total_agent_cpu = 0.0
    total_agent_memory = 0

    for p in projects:
        slots = p.get("agent_slots", {})
        total_agents_used += slots.get("used", 0)
        total_agents_max += slots.get("max", 0)
        for agent in p.get("agents", []):
            cpu = agent.get("cpu_percent")
            mem = agent.get("memory_rss_bytes")
            if isinstance(cpu, (int, float)):
                total_agent_cpu += cpu
            if isinstance(mem, (int, float)):
                total_agent_memory += int(mem)

    return {
        "project_count": len(projects),
        "total_agents_used": total_agents_used,
        "total_agents_max": total_agents_max,
        "total_agent_cpu_percent": round(total_agent_cpu, 1),
        "total_agent_memory_bytes": total_agent_memory,
    }


def _empty_totals() -> dict:
    return {
        "project_count": 0,
        "total_agents_used": 0,
        "total_agents_max": 0,
        "total_agent_cpu_percent": 0.0,
        "total_agent_memory_bytes": 0,
    }
