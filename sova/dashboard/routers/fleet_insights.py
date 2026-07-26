"""Fleet Insights API router: cross-project analytics from FleetService."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException

from sova.dashboard.services.fleet_service import FleetService
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.api.fleet_insights")

router = APIRouter(prefix="/fleet-insights", tags=["fleet-insights"])


def _get_fleet_service() -> FleetService:
    return FleetService()


def _decimal_to_float(obj: object) -> object:
    """Convert Decimal values to float for JSON serialization."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _decimal_to_float(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimal_to_float(item) for item in obj]
    return obj


@router.get("/data")
async def get_fleet_insights(
    force: bool = False,
    service: FleetService = Depends(_get_fleet_service),
) -> dict[str, object]:
    """Return aggregated fleet insights across all registered projects."""
    try:
        insights = await service.get_insights(force_refresh=force)
    except Exception:
        log.error("Failed to load fleet insights", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Fleet insights temporarily unavailable",
        )
    return _decimal_to_float(asdict(insights))
