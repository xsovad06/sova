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
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.adapters.base import TaskAdapter

log = get_logger(component="dashboard.api.issues")

router = APIRouter(prefix="/issues", tags=["issues"])


class PriorityRequest(BaseModel):
    priority: Priority


def _invalidate_ranking_caches(project_dir: pathlib.Path | None, adapter: TaskAdapter) -> None:
    """Drop every cache the next board request would rank from, so it sees the new priority."""
    from sova.config.context import resolve_project_dir
    from sova.dashboard.services.agent_pool import get_default_project_dir
    from sova.dashboard.services.queue_service import invalidate_queue_cache
    from sova.dashboard.services.work_item_service import forget_graph
    from sova.supervisor.dependency_graph import invalidate_graph_cache

    invalidate_graph_cache(getattr(adapter, "repo", "") or getattr(adapter, "project_key", "") or "")
    invalidate_queue_cache(resolve_project_dir(project_dir, default_project_dir=get_default_project_dir()))
    # forget_graph() resolves the project itself, so the board and every other caller share the one entry it drops.
    forget_graph(project_dir)


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

    _invalidate_ranking_caches(project_dir, adapter)
    return {"issue": number, "priority": body.priority.value}
