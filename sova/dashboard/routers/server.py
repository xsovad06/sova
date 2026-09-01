"""Server lifecycle router: restart, drain, and preflight endpoints."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from sova.dashboard.project_context import get_project_dir
from sova.dashboard.services.agent_pool import list_all_pools
from sova.utils.logging import get_logger

router = APIRouter(tags=["server"])
log = get_logger(component="dashboard.server.router")

_DRAIN_POLL_INTERVAL = 2.0  # seconds between agent count checks during drain
_DRAIN_TIMEOUT_SECONDS = 1800  # 30 minutes


class RestartRequest(BaseModel):
    drain: bool = False
    force: bool = False


def count_active_agents() -> int:
    """Count active agents across all projects.

    Returns:
        Total number of active agents summed across all project pools.
        Returns 0 if agent pool enumeration fails.
    """
    try:
        total = 0
        for pa in list_all_pools().values():
            total += len(pa.agents)
        return total
    except Exception:
        log.warning("server.count_agents_failed", exc_info=True)
        return 0


def _is_supervisor_running(project_dir: Path | None) -> bool:
    """Check if supervisor daemon is currently running."""
    from sova.dashboard.routers.supervisor import _get_daemon

    daemon = _get_daemon(project_dir)
    return daemon is not None and daemon.running


async def _disable_supervisor(project_dir: Path | None) -> None:
    """Disable supervisor daemon for drain mode."""
    from sova.dashboard.routers.supervisor import _daemon_registry, _get_daemon

    daemon = _get_daemon(project_dir)
    if daemon is not None and daemon.running:
        await daemon.stop()
        log.info("server.supervisor_stopped_for_drain")

        # Remove from registry so it doesn't auto-restart
        key = str(project_dir.resolve()) if project_dir else None
        if key is None and len(_daemon_registry) == 1:
            key = next(iter(_daemon_registry))
        if key is not None:
            _daemon_registry.pop(key, None)


def _write_restart_marker(project_dir: Path | None) -> None:
    """Write restart-requested marker file with current PID."""
    try:
        resolved = project_dir or Path.cwd()
        marker_path = resolved / ".claude" / "agent-control" / "restart-requested"
        marker_path.parent.mkdir(parents=True, exist_ok=True)

        content = f"pid={os.getpid()}\ntimestamp={time.time()}\n"
        marker_path.write_text(content)
        log.info("server.restart_marker_written", path=str(marker_path))
    except OSError:
        log.warning("server.restart_marker_write_failed", exc_info=True)


def _send_sighup() -> bool:
    """Attempt to send SIGHUP to current process for graceful restart.

    Returns True if signal was sent successfully, False otherwise.
    """
    try:
        if not hasattr(signal, "SIGHUP") or signal.SIGHUP is None:
            return False
        pid = os.getpid()
        os.kill(pid, signal.SIGHUP)
        log.info("server.sighup_sent", pid=pid)
        return True
    except (OSError, AttributeError, TypeError):
        log.warning("server.sighup_failed", exc_info=True)
        return False


async def _wait_for_agents(
    project_dir: Path | None,
    initial_count: int,
    timeout: float = _DRAIN_TIMEOUT_SECONDS,
) -> dict:
    """Poll agent count until zero or timeout.

    Returns:
        {"action": "drained"} on success
        {"action": "drain_timeout", "remaining_agents": N} on timeout
        {"action": "drain_timeout", "remaining_agents": N, "warning": "..."}
            if agent count increased during drain
    """
    start = time.monotonic()
    last_count = initial_count

    while (time.monotonic() - start) < timeout:
        current_count = count_active_agents()

        if current_count == 0:
            return {"action": "drained"}

        # Warn if count increased (new agent started during drain)
        if current_count > last_count:
            log.warning(
                "server.drain.agent_count_increased",
                initial=initial_count,
                current=current_count,
            )
            return {
                "action": "drain_timeout",
                "remaining_agents": current_count,
                "warning": f"Agent count increased from {last_count} to {current_count} during drain",
            }

        last_count = current_count
        await asyncio.sleep(_DRAIN_POLL_INTERVAL)

    # Timeout
    return {
        "action": "drain_timeout",
        "remaining_agents": count_active_agents(),
    }


@router.get("/server/restart/preflight")
async def restart_preflight() -> dict:
    """Pre-flight check for restart: count active agents and supervisor state."""
    project_dir = get_project_dir()
    active_agents = count_active_agents()
    supervisor_running = _is_supervisor_running(project_dir)

    return {
        "active_agents": active_agents,
        "supervisor_running": supervisor_running,
    }


@router.post("/server/restart")
async def restart_server(req: RestartRequest) -> dict:
    """Restart the SOVA dashboard server.

    Phase 1 implementation: writes a marker file and attempts SIGHUP restart.
    Falls back to manual instruction if signal-based restart is unavailable.

    Args:
        req.drain: If True, disable supervisor and wait for agents to finish
                   before restarting (max 30 min timeout).
        req.force: If True with drain, kill remaining agents after timeout.

    Returns:
        {"action": "restarted"} on success
        {"action": "restart_required", "instruction": "..."}  on fallback
        {"action": "drained"} when drain completes successfully
        {"action": "drain_timeout", "remaining_agents": N} on drain timeout
    """
    project_dir = get_project_dir()

    # Write marker file for external tooling
    _write_restart_marker(project_dir)

    if req.drain:
        # Disable supervisor so no new agents spawn during drain
        await _disable_supervisor(project_dir)

        initial_count = count_active_agents()
        if initial_count > 0:
            log.info("server.drain.waiting", agent_count=initial_count)
            drain_result = await _wait_for_agents(project_dir, initial_count)

            if drain_result["action"] == "drain_timeout":
                if req.force:
                    # Verify agents are still running before killing
                    current_count = count_active_agents()
                    if current_count == 0:
                        log.info("server.drain.completed_during_timeout")
                        # Fall through to restart
                    else:
                        # Force mode: kill remaining agents
                        from sova.dashboard.services.agent_pool import list_all_pools

                        killed = 0
                        for pa in list_all_pools().values():
                            for agent_state in list(pa.agents.values()):
                                try:
                                    agent_state.process.stop()
                                    killed += 1
                                except Exception:
                                    log.warning("server.force_kill_failed", run_id=agent_state.run_id, exc_info=True)

                        log.info("server.force_killed_agents", count=killed)
                else:
                    # Return timeout response for user to choose
                    return drain_result

    # Attempt signal-based restart
    if _send_sighup():
        return {"action": "restarted"}

    # Fallback: manual instruction
    return {
        "action": "restart_required",
        "instruction": "Please restart the server manually with: sova server restart",
    }
