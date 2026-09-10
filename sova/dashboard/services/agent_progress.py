"""Agent pipeline progress -- step tracking and variant detection.

Separated from agent_lifecycle to isolate pipeline progress computation.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from sova.core.steps import (
    get_address_review_step_names,
    get_developer_step_names,
    get_planner_step_names,
    get_researcher_step_names,
)
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.agent_progress")

DEVELOPER_PIPELINE = get_developer_step_names()
ADDRESS_REVIEW_PIPELINE = get_address_review_step_names()
RESEARCHER_PIPELINE = get_researcher_step_names()
PLANNER_PIPELINE = get_planner_step_names()

_BUILTIN_PIPELINES: dict[str, list[str]] = {
    "developer": DEVELOPER_PIPELINE,
    "address_review": ADDRESS_REVIEW_PIPELINE,
    "researcher": RESEARCHER_PIPELINE,
    "planner": PLANNER_PIPELINE,
}

# Projects may override step pipelines via [pipelines] in sova.toml, so the
# progress bar cannot assume the built-in lists. load_config() reads TOML plus
# the settings DB, which is too expensive for a per-agent polling path, hence
# the short TTL cache (config edits show up on the next expiry).
_PIPELINE_CACHE_TTL_SECONDS = 60.0
_pipeline_cache: dict[str, tuple[float, dict[str, list[str]]]] = {}


async def _resolve_pipelines(project_dir: Path | None) -> dict[str, list[str]]:
    """Return the step name lists for each variant, honouring project config.

    Falls back to the built-in pipelines when no project is known or the
    config cannot be read: progress display must never break a run listing.
    """
    if project_dir is None:
        return _BUILTIN_PIPELINES

    cache_key = str(project_dir)
    now = time.monotonic()
    cached = _pipeline_cache.get(cache_key)
    if cached is not None and now - cached[0] < _PIPELINE_CACHE_TTL_SECONDS:
        return cached[1]

    resolved = dict(_BUILTIN_PIPELINES)
    try:
        from sova.config.loader import load_config

        pipelines = (await asyncio.to_thread(load_config, project_dir)).pipelines
        for variant in ("developer", "address_review", "researcher"):
            configured = getattr(pipelines, variant, None)
            if configured:
                resolved[variant] = list(configured)
    except Exception:
        log.warning("agent_progress.pipeline_config_load_failed", project_dir=cache_key, exc_info=True)

    _pipeline_cache[cache_key] = (now, resolved)
    return resolved


_ADDRESS_REVIEW_ONLY = frozenset(
    {"ensure_worktree", "rebase", "address_review", "rearrange_commits", "handoff_to_user"}
)
_STANDALONE_ROLES = frozenset({"reviewer"})
_RESEARCHER_ONLY = frozenset({"fetch_task", "research"})
_PLANNER_ONLY = frozenset({"scan_project", "generate_tasks", "validate_tasks"})


def _detect_pipeline(
    current_step: str | None,
    role: str | None,
    pr_number: int | None,
    pipelines: dict[str, list[str]] | None = None,
) -> tuple[list[str], str]:
    """Return (pipeline_steps, variant_name) for the given run context.

    Defaults to the built-in step lists when no resolved pipelines are given.
    """
    pipelines = pipelines or _BUILTIN_PIPELINES
    if role == "planner" or (current_step is not None and current_step in _PLANNER_ONLY):
        return pipelines["planner"], "planner"
    if role == "researcher" or (current_step is not None and current_step in _RESEARCHER_ONLY):
        return pipelines["researcher"], "researcher"

    is_address_review = (current_step in (None, "agent") and role == "developer" and pr_number is not None) or (
        current_step is not None and current_step in _ADDRESS_REVIEW_ONLY
    )
    if is_address_review:
        return pipelines["address_review"], "address_review"

    return pipelines["developer"], "developer"


async def get_step_progress(
    current_step: str | None,
    *,
    role: str | None = None,
    pr_number: int | None = None,
    project_dir: Path | None = None,
) -> dict:
    """Compute step index from current_step name.

    Uses role+pr_number only when current_step is None or "agent" (the
    dashboard outer-process TaskRun sentinel). WorkflowEngine TaskRuns
    progress through real step names and acquire pr_number mid-pipeline
    via _sync_task_run_context, so gating on current_step avoids false
    positives for developer runs that created a PR.

    Pass project_dir to honour a project's [pipelines] config overrides;
    without it the built-in step lists are used.
    """
    is_command = role is not None and (role.startswith("command:") or role in _STANDALONE_ROLES)
    if is_command:
        return {
            "step_index": 0,
            "total_steps": 1,
            "steps": ["running"],
            "pipeline_variant": "command",
        }

    pipeline, variant = _detect_pipeline(current_step, role, pr_number, await _resolve_pipelines(project_dir))

    if current_step is None or current_step == "agent":
        idx = 0
    else:
        try:
            idx = pipeline.index(current_step)
        except ValueError:
            idx = 0

    return {
        "step_index": idx,
        "total_steps": len(pipeline),
        "steps": pipeline,
        "pipeline_variant": variant,
    }
