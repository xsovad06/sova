"""Dependabot auto-merge API: status, sweep trigger, and PR listing."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, BackgroundTasks, HTTPException

from sova.config.context import get_project_dir
from sova.config.loader import load_config
from sova.utils.logging import get_logger

router = APIRouter()
log = get_logger(component="dashboard.dependabot")


@router.get("/dependabot/status")
async def dependabot_status() -> dict:
    """Return current Dependabot auto-merge configuration and state."""
    cfg = load_config(get_project_dir())
    return {
        "enabled": cfg.dependabot.enabled,
        "poll_interval_seconds": cfg.dependabot.poll_interval_seconds,
        "auto_merge_groups": cfg.dependabot.auto_merge_groups,
        "require_approval_groups": cfg.dependabot.require_approval_groups,
        "approval_label": cfg.dependabot.approval_label,
    }


@router.get("/dependabot/prs")
async def list_dependabot_prs() -> dict:
    """List all open Dependabot PRs with their processing disposition."""
    from sova.git.pr import list_open_prs
    from sova.supervisor.dependabot import classify_dependabot_prs

    cfg = load_config(get_project_dir())
    if not cfg.github_repo:
        raise HTTPException(status_code=503, detail="No github_repo configured")

    try:
        all_prs = await list_open_prs(repo=cfg.github_repo, github_user=cfg.github_user)
    except Exception:
        log.warning("dependabot.list_prs.error", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch PRs") from None

    classified = classify_dependabot_prs(all_prs, cfg.dependabot)

    result = []
    for dpr, skip_reason in classified:
        result.append(
            {
                "number": dpr.number,
                "title": dpr.title,
                "url": dpr.url,
                "group": dpr.group,
                "has_major_bump": dpr.has_major_bump,
                "labels": dpr.labels,
                "skip_reason": skip_reason,
                "will_process": not bool(skip_reason),
            }
        )

    return {"prs": result, "total": len(result)}


@dataclass
class _SweepState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_results: list[dict] | None = None
    failed: bool = False
    acquired_at: float = 0.0


_sweep_states: dict[str, _SweepState] = {}


def _get_sweep_state(project_key: str) -> _SweepState:
    if project_key not in _sweep_states:
        _sweep_states[project_key] = _SweepState()
    return _sweep_states[project_key]


@router.post("/dependabot/sweep", status_code=202)
async def trigger_sweep(background_tasks: BackgroundTasks) -> dict:
    """Trigger an immediate Dependabot sweep (runs in background)."""
    from sova.supervisor.dependabot import sweep_dependabot_prs

    project_dir = get_project_dir()
    cfg = load_config(project_dir)
    if not cfg.github_repo:
        raise HTTPException(status_code=503, detail="No github_repo configured")

    project_key = str(project_dir)
    state = _get_sweep_state(project_key)

    if state.lock.locked():
        group_count = max(1, len(cfg.dependabot.auto_merge_groups or []))
        stale_timeout = cfg.dependabot.ci_poll_timeout_seconds * group_count + 60
        if state.acquired_at > 0 and (time.monotonic() - state.acquired_at) > stale_timeout:
            log.warning("dependabot.sweep_lock_stale", elapsed=time.monotonic() - state.acquired_at)
            state.lock.release()
        else:
            return {"status": "already_running"}

    await state.lock.acquire()
    state.acquired_at = time.monotonic()

    async def _run_sweep() -> None:
        state.last_results = None
        state.failed = False
        try:
            results = await sweep_dependabot_prs(
                project_dir=project_dir,
                repo=cfg.github_repo,
                github_user=cfg.github_user,
                config=cfg.dependabot,
                notification_config=cfg.notification,
            )
            state.last_results = [
                {
                    "pr_number": r.pr_number,
                    "title": r.title,
                    "action": r.action,
                    "reason": r.reason,
                }
                for r in results
            ]
        except Exception:
            log.warning("dependabot.sweep.error", exc_info=True)
            state.failed = True
        finally:
            state.lock.release()

    background_tasks.add_task(_run_sweep)
    return {"status": "started"}


@router.get("/dependabot/sweep/results")
async def get_sweep_results() -> dict:
    """Return the results of the last background sweep."""
    project_key = str(get_project_dir())
    state = _get_sweep_state(project_key)

    if state.lock.locked():
        return {"results": None, "status": "running"}

    if state.failed:
        return {"results": None, "status": "error"}

    if state.last_results is None:
        return {"results": None, "status": "no_results"}

    results = state.last_results
    return {
        "results": results,
        "summary": {
            "total": len(results),
            "merged": sum(1 for r in results if r["action"] == "merged"),
            "closed": sum(1 for r in results if r["action"] == "closed"),
            "skipped": sum(1 for r in results if r["action"] == "skipped"),
            "waiting": sum(1 for r in results if r["action"] == "waiting"),
            "errors": sum(1 for r in results if r["action"] == "error"),
        },
        "status": "complete",
    }
