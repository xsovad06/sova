"""Issues API router: changes to one tracker issue (today, its priority)."""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from sova.adapters.base import AdapterError, PriorityUnavailableError
from sova.adapters.priority import Priority
from sova.config.context import get_project_dir
from sova.config.loader import load_config
from sova.utils.formatting import normalize_issue_key
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.adapters.base import TaskAdapter

log = get_logger(component="dashboard.api.issues")

router = APIRouter(prefix="/issues", tags=["issues"])


class PriorityRequest(BaseModel):
    priority: Priority


def _invalidate_ranking_caches(project_dir: pathlib.Path | None, adapter: TaskAdapter) -> pathlib.Path:
    """Drop every cache the next board request would rank from, so it sees the new priority.

    Returns the resolved project: the board snapshots are keyed by it (``get_board``), so the refresh here and the
    writer's wait in ``set_priority`` must name the same project.
    """
    from sova.config.context import resolve_project_dir
    from sova.dashboard.services.agent_pool import get_default_project_dir
    from sova.dashboard.services.queue_service import invalidate_queue_cache
    from sova.dashboard.services.work_board_service import refresh_board_snapshots
    from sova.dashboard.services.work_item_service import forget_graph
    from sova.supervisor.dependency_graph import invalidate_graph_cache

    resolved = resolve_project_dir(project_dir, default_project_dir=get_default_project_dir())
    invalidate_graph_cache(getattr(adapter, "repo", "") or getattr(adapter, "project_key", "") or "")
    invalidate_queue_cache(resolved)
    # forget_graph() resolves the project itself, so the board and every other caller share the one entry it drops.
    forget_graph(project_dir)
    # The boards keep answering from before the change until their rebuild (on a fresh graph) lands.
    refresh_board_snapshots(resolved)
    return resolved


@router.put(
    "/{number}/priority",
    responses={
        400: {"description": "The tracker has no such priority level; the issue is unchanged"},
        502: {"description": "The tracker rejected or failed the update"},
        503: {"description": "Project configuration unavailable"},
    },
)
async def set_priority(number: Annotated[int, Path(gt=0)], body: PriorityRequest) -> dict:
    """Set an issue's priority to one level of the ``priority:`` taxonomy, replacing its current one.

    GitHub ends with exactly one ``priority:`` label, the target added before the stale ones are removed. Jira
    sets its native priority field to the project's own name for the level, and answers 400 when it has none.
    The adapter is built for this request, so any client it opened is closed on every exit path.
    """
    from sova.adapters import close_adapter, create_adapter

    project_dir = get_project_dir()
    try:
        adapter = create_adapter(load_config(project_dir))
    except (ValueError, KeyError, OSError, SQLAlchemyError):
        log.error("issues.priority_config_unavailable", issue=number, exc_info=True)
        raise HTTPException(status_code=503, detail="Project configuration unavailable")

    try:
        await adapter.set_priority(str(number), body.priority)
    except PriorityUnavailableError as exc:
        log.info("issues.priority_unavailable", issue=number, priority=body.priority.value, error=exc.user_message)
        raise HTTPException(status_code=400, detail=exc.user_message)
    except (AdapterError, RuntimeError, OSError) as exc:
        log.error("issues.priority_update_failed", issue=number, priority=body.priority.value, exc_info=True)
        raise HTTPException(status_code=502, detail="Failed to update the issue priority") from exc
    finally:
        await close_adapter(adapter)

    # The tracker has the new priority, so a failed refresh or wait is logged, never answered as an error.
    try:
        resolved = _invalidate_ranking_caches(project_dir, adapter)
        from sova.dashboard.services.work_board_service import board_rebuilds_settled

        # This client reloads its board next: it waits (bounded) for the rebuild, every other tab keeps its board.
        await board_rebuilds_settled(resolved)
    except Exception:  # noqa: BLE001 (the write succeeded; the board catches up on its rebuild cadence)
        log.warning("issues.priority_board_refresh_failed", issue=number, exc_info=True)
    return {"issue": number, "priority": body.priority.value}


@router.get(
    "/explain",
    responses={
        400: {"description": "Neither or both of issue and run_id were given, or the run has no issue"},
        404: {"description": "No run has that id"},
    },
)
async def explain_issue(issue: str | None = None, run_id: int | None = None) -> dict:
    """What SOVA knows about one issue (the read behind the ``sova_explain_issue`` MCP tool). Read-only."""
    from sova.config.context import resolve_project_dir
    from sova.dashboard.services.agent_pool import get_default_project_dir
    from sova.dashboard.services.mcp_service import ExplainRequestError, RunNotFoundError
    from sova.dashboard.services.mcp_service import explain_issue as explain

    project_dir = resolve_project_dir(get_project_dir(), default_project_dir=get_default_project_dir())
    try:
        cleaned = normalize_issue_key(issue) if issue is not None else None
    except ValueError as e:
        raise HTTPException(status_code=400, detail="issue must be an issue number or key") from e
    try:
        return await explain(cleaned, run_id, project_dir)
    except RunNotFoundError as e:
        raise HTTPException(status_code=404, detail=e.user_message) from e
    except ExplainRequestError as e:
        raise HTTPException(status_code=400, detail=e.user_message) from e
