"""Models API: read-only LLM/runtime model enumeration."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException

from sova.config.context import resolve_project_dir
from sova.dashboard.services import models_service
from sova.dashboard.services.agent_pool import get_default_project_dir
from sova.utils.logging import get_logger

router = APIRouter()
log = get_logger(component="dashboard.models")


@router.get(
    "/models/available",
    responses={
        429: {"description": "Refresh rate limit exceeded"},
        503: {"description": "Model enumeration unavailable"},
    },
)
async def available_models(layer: Literal["llm", "runtime"], refresh: bool = False) -> dict:
    project_dir = resolve_project_dir(default_project_dir=get_default_project_dir())
    try:
        return await models_service.get_available_models(project_dir, layer=layer, refresh=refresh)
    except models_service.ModelsRefreshRateLimitedError as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    except Exception as exc:  # noqa: BLE001 (HTTP boundary: any internal failure becomes a 503)
        log.warning("models.available.error", exc_info=True)
        raise HTTPException(status_code=503, detail="Failed to fetch available models") from exc
