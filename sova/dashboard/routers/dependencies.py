"""Dependency graph API router -- DAG visualization, readiness, and chain queries."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from sova.adapters.base import TaskState
from sova.dashboard.project_context import get_project_dir
from sova.dashboard.services.pr_service import ComputedPRState
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.api.dependencies")

router = APIRouter(prefix="/dependencies", tags=["dependencies"])

# Simplified PR state strings for the graph API.  Mapped from ComputedPRState
# so internal enum names are not leaked to the frontend.
_PR_STATE_MAP: dict[str, str] = {
    ComputedPRState.APPROVED_CI_GREEN: "approved_ci_green",
    ComputedPRState.APPROVED: "approved_ci_green",
    ComputedPRState.CHANGES_REQUESTED: "changes_requested",
    ComputedPRState.REVIEW_ADDRESSED: "awaiting_review",
    ComputedPRState.CI_RUNNING: "ci_running",
    ComputedPRState.CI_FAILED: "ci_failed",
    ComputedPRState.AWAITING_REVIEW: "awaiting_review",
    ComputedPRState.DRAFT: "draft",
}

# PR-state-aware actions for IN_REVIEW nodes.  Overrides _STATE_ACTIONS when
# a PR is linked and its state is known.
_PR_STATE_ACTIONS: dict[str, list[dict]] = {
    "approved_ci_green": [{"id": "integrate-pr", "label": "Integrate PR", "role": "integrate-pr"}],
    "changes_requested": [{"id": "address-pr", "label": "Address PR", "role": "address-pr"}],
    "ci_failed": [{"id": "address-pr", "label": "Fix CI", "role": "address-pr"}],
    "ci_running": [],
    "awaiting_review": [],
    "draft": [],
}


class _ConfigError(Exception):
    """Raised when project config or adapter creation fails."""


async def _build_graph(milestone: str = ""):
    """Build a dependency graph from the current project's adapter."""
    from sova.adapters import create_adapter
    from sova.config.loader import load_config
    from sova.supervisor.dependency_graph import build_dependency_graph

    try:
        project_dir = get_project_dir()
        cfg = load_config(project_dir)
        adapter = create_adapter(cfg)
    except Exception as exc:
        raise _ConfigError(str(exc)) from exc
    return await build_dependency_graph(adapter, milestone=milestone)


async def _fetch_pr_map() -> dict[int, dict]:
    """Build {issue_number: pr_info} from open PRs.  Fail-open: returns {} on error."""
    from sova.dashboard.services.pr_service import list_open_prs_with_state

    try:
        prs = await list_open_prs_with_state(author_filter_override="all")
    except Exception:
        log.warning("dependency_graph.pr_fetch_failed", exc_info=True)
        return {}

    result: dict[int, dict] = {}
    for pr in prs:
        for issue_num in pr.get("linked_issues") or []:
            # Multiple PRs per issue: keep the most recent (highest number)
            if issue_num not in result or pr["number"] > result[issue_num]["pr_number"]:
                computed = pr.get("computed_state", "")
                pr_state = _PR_STATE_MAP.get(computed, "awaiting_review")
                result[issue_num] = {
                    "pr_number": pr["number"],
                    "pr_url": pr.get("url", ""),
                    "pr_state": pr_state,
                    "pr_state_label": pr.get("state_label", ""),
                }

    return result


@router.get(
    "/graph",
    responses={
        500: {"description": "Failed to build dependency graph"},
        503: {"description": "Project configuration unavailable"},
    },
)
async def get_graph(milestone: str = "") -> dict:
    """Build and return the full dependency graph.

    Optional ``milestone`` query param filters to issues in that milestone.
    Dependencies outside the milestone are still fetched individually.
    """
    try:
        graph, pr_map = await asyncio.gather(
            _build_graph(milestone),
            _fetch_pr_map(),
        )
        return graph.to_dict(pr_map=pr_map, pr_state_actions=_PR_STATE_ACTIONS)
    except _ConfigError:
        log.error("Project config/adapter error for dependency graph", exc_info=True)
        raise HTTPException(status_code=503, detail="Project configuration unavailable")
    except Exception:
        log.error("Failed to build dependency graph", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to build dependency graph")


@router.get(
    "/ready",
    responses={
        500: {"description": "Failed to get ready tasks"},
        503: {"description": "Project configuration unavailable"},
    },
)
async def get_ready(milestone: str = "") -> dict:
    """Return issues whose dependencies are all satisfied (ready to work on)."""
    try:
        graph = await _build_graph(milestone)
    except _ConfigError:
        log.error("Project config/adapter error for ready tasks", exc_info=True)
        raise HTTPException(status_code=503, detail="Project configuration unavailable")
    except Exception:
        log.error("Failed to get ready tasks", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to get ready tasks")

    ready_ids = graph.get_ready_tasks()
    ready_tasks = []
    for tid in ready_ids:
        task = graph.get_task(tid)
        if task:
            ready_tasks.append(
                {
                    "id": tid,
                    "title": task.title,
                    "state": task.state.value,
                    "milestone": task.milestone,
                }
            )
    return {"ready": ready_tasks}


@router.get(
    "/chain/{issue_number}",
    responses={
        404: {"description": "Issue not found in graph"},
        500: {"description": "Failed to get chain"},
        503: {"description": "Project configuration unavailable"},
    },
)
async def get_chain(issue_number: int, milestone: str = "") -> dict:
    """Return the transitive dependency chain for a specific issue."""
    try:
        graph = await _build_graph(milestone)
    except _ConfigError:
        log.error("Project config/adapter error for chain #%d", issue_number, exc_info=True)
        raise HTTPException(status_code=503, detail="Project configuration unavailable")
    except Exception:
        log.error("Failed to get chain for #%d", issue_number, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get chain for #{issue_number}")

    if not graph.has_task(issue_number):
        raise HTTPException(status_code=404, detail=f"Issue #{issue_number} not found in graph")

    chain = graph.get_chain(issue_number)
    chain_tasks = []
    for tid in chain:
        task = graph.get_task(tid)
        if task:
            chain_tasks.append(
                {
                    "id": tid,
                    "title": task.title,
                    "state": task.state.value,
                    "done": task.state == TaskState.DONE,
                }
            )
    return {"issue": issue_number, "chain": chain_tasks}
