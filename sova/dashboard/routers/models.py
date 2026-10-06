"""Models API: read-only LLM/runtime model enumeration."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException

from sova.config.context import get_project_dir
from sova.dashboard.services import models_service
from sova.utils.logging import get_logger

router = APIRouter()
log = get_logger(component="dashboard.models")


@router.get(
    "/models/available",
    responses={
        429: {"description": "Refresh rate limit exceeded"},
        501: {"description": "layer=runtime enumeration is not implemented"},
        503: {"description": "Model enumeration unavailable"},
    },
)
async def available_models(layer: Literal["llm", "runtime"], refresh: bool = False) -> dict:
    if layer == "runtime":
        raise HTTPException(status_code=501, detail="layer=runtime enumeration is not implemented")

    project_dir = get_project_dir() or Path.cwd()
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
