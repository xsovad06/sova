"""Dependency graph API router -- DAG visualization, readiness, and chain queries."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from sova.adapters.base import TaskState
from sova.dashboard.project_context import get_project_dir
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.api.dependencies")

router = APIRouter(prefix="/dependencies", tags=["dependencies"])


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


@router.get(
    "/graph",
    responses={
        500: {"description": "Failed to build dependency graph"},
        503: {"description": "Project configuration unavailable"},
    },
)
async def get_graph(milestone: str = ""):
    """Build and return the full dependency graph.

    Optional ``milestone`` query param filters to issues in that milestone.
    Dependencies outside the milestone are still fetched individually.
    """
    try:
        graph = await _build_graph(milestone)
        return graph.to_dict()
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
async def get_ready(milestone: str = ""):
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
async def get_chain(issue_number: int, milestone: str = ""):
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
