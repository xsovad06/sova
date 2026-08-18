"""Dependency graph API router -- DAG visualization, readiness, and chain queries."""

from __future__ import annotations

import asyncio
from pathlib import Path

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
        prs = await list_open_prs_with_state()
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


def _parse_issue_int(raw: str | None) -> int | None:
    """Parse an issue string like '507', '#507', or None to an int."""
    if not raw:
        return None
    try:
        return int(str(raw).lstrip("#").strip())
    except (ValueError, TypeError):
        return None


async def _fetch_agent_map() -> dict[int, dict]:
    """Build {issue_number: agent_info} from running agents.  Fail-open."""
    from sova.dashboard.services.agent_lifecycle import get_unified_agents

    try:
        data = await get_unified_agents()
    except Exception:
        log.warning("dependency_graph.agent_fetch_failed", exc_info=True)
        return {}

    result: dict[int, dict] = {}
    for agent in data.get("agents", []):
        issue = _parse_issue_int(agent.get("issue"))
        if issue is None:
            continue
        run_id = agent.get("run_id", 0)
        if issue not in result or run_id > result[issue]["run_id"]:
            result[issue] = {
                "run_id": run_id,
                "role": agent.get("role", ""),
                "status": agent.get("status", "running"),
                "elapsed_seconds": agent.get("elapsed_seconds", 0),
            }
    return result


def _fetch_handoff_map() -> dict[int, dict]:
    """Build {issue_number: handoff_info} from pending handoffs.  Fail-open."""
    from sova.dashboard.services.handoff_service import get_all_handoffs

    try:
        handoffs = get_all_handoffs()
    except Exception:
        log.warning("dependency_graph.handoff_fetch_failed", exc_info=True)
        return {}

    result: dict[int, dict] = {}
    for h in handoffs:
        if h.get("status") != "awaiting_action":
            continue
        issue = _parse_issue_int(h.get("issue"))
        if issue is None:
            continue
        actions = h.get("next_actions") or []
        next_action = actions[0].get("label", "") if actions else ""
        if issue not in result:
            result[issue] = {"next_action": next_action}
    return result


async def _fetch_last_run_map() -> dict[int, dict]:
    """Build {issue_number: last_run_info} from most recent terminal TaskRuns.  Fail-open."""
    try:
        from sqlalchemy import func, select

        from sova.core.state import TASK_RUN_TERMINAL
        from sova.dashboard.project_context import get_project_dir
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        project_dir = get_project_dir()
        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                subq = (
                    select(
                        TaskRun.issue_number,
                        func.max(TaskRun.id).label("max_id"),
                    )
                    .where(
                        TaskRun.status.in_(TASK_RUN_TERMINAL),
                        TaskRun.issue_number.isnot(None),
                        TaskRun.issue_number != "",
                    )
                    .group_by(TaskRun.issue_number)
                    .subquery()
                )
                stmt = select(TaskRun.issue_number, TaskRun.id, TaskRun.status).join(
                    subq,
                    (TaskRun.issue_number == subq.c.issue_number) & (TaskRun.id == subq.c.max_id),
                )
                rows = (await session.execute(stmt)).all()

        result: dict[int, dict] = {}
        for row in rows:
            issue = _parse_issue_int(row.issue_number)
            if issue is not None:
                result[issue] = {"run_id": row.id, "status": row.status}
        return result
    except Exception:
        log.warning("dependency_graph.last_run_fetch_failed", exc_info=True)
        return {}


def _enrich_with_queue_position(graph_dict: dict, project_dir: str | Path) -> None:
    """Add ``queue_position`` (1-indexed) to nodes that appear in the task queue.

    Fail-open: config or iteration errors are silently swallowed so the graph
    response is still returned without queue data.
    """
    from sova.config.loader import load_config

    try:
        cfg = load_config(project_dir)
        queue = list(cfg.supervisor.task_queue)
    except Exception:
        log.warning("dependency_graph.queue_config_load_failed", exc_info=True)
        return

    if not queue:
        return

    pos_map = {issue_num: idx + 1 for idx, issue_num in enumerate(queue)}
    for node in graph_dict.get("nodes", []):
        node_id = node.get("id")
        if node_id is None:
            log.warning("dependency_graph.node_missing_id", extra={"node": node})
            continue
        qp = pos_map.get(node_id)
        if qp is not None:
            node["queue_position"] = qp


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

    Nodes are post-enriched with ``queue_position`` (1-indexed) when the issue
    appears in the supervisor task queue.
    """
    try:
        graph, pr_map, agent_map, last_run_map = await asyncio.gather(
            _build_graph(milestone),
            _fetch_pr_map(),
            _fetch_agent_map(),
            _fetch_last_run_map(),
        )
        handoff_map = _fetch_handoff_map()
        project_dir = get_project_dir()
        result = graph.to_dict(
            pr_map=pr_map,
            pr_state_actions=_PR_STATE_ACTIONS,
            agent_map=agent_map,
            handoff_map=handoff_map,
            last_run_map=last_run_map,
            project_dir=project_dir,
        )
        _enrich_with_queue_position(result, project_dir)
        return result
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
