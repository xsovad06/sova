"""Fleet Insights API router: cross-project analytics from FleetService."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from sova.dashboard.services.fleet_service import FleetService, build_issue_draft
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.api.fleet_insights")

router = APIRouter(prefix="/fleet-insights", tags=["fleet-insights"])


# Pydantic models at module scope (required: from __future__ import annotations
# makes type annotations lazy strings, and FastAPI resolves them via module globals).


class IssueDraftResponse(BaseModel):
    title: str
    body: str
    labels: list[str]


class ProposeIssueRequest(BaseModel):
    title: str
    body: str
    labels: list[str]


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
    except Exception as exc:
        log.error("Failed to load fleet insights", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Fleet insights temporarily unavailable",
        ) from exc
    return _decimal_to_float(asdict(insights))


@router.get("/issue-draft")
async def get_issue_draft(
    step_name: str,
    service: FleetService = Depends(_get_fleet_service),
) -> IssueDraftResponse:
    """Return a pre-filled issue draft for a fleet failure pattern."""
    try:
        insights = await service.get_insights()
    except Exception as exc:
        log.error("Failed to load fleet insights for draft", exc_info=True)
        raise HTTPException(status_code=503, detail="Fleet insights temporarily unavailable") from exc

    draft = build_issue_draft(step_name, insights)
    if draft is None:
        raise HTTPException(status_code=404, detail=f"No failure data for step '{step_name}'")

    return IssueDraftResponse(
        title=str(draft["title"]),
        body=str(draft["body"]),
        labels=list(draft["labels"]),
    )


@router.post("/propose-issue")
async def propose_issue(req: ProposeIssueRequest) -> dict[str, object]:
    """Create a GitHub issue on the SOVA repo from fleet failure data."""
    from sova.adapters.github import GitHubAdapter
    from sova.config.loader import load_config
    from sova.config.models import FleetConfig
    from sova.dashboard.project_context import get_project_dir

    project_dir = get_project_dir()
    github_user = ""
    sova_repo = FleetConfig().sova_repo

    if project_dir:
        try:
            cfg = load_config(project_dir)
            github_user = cfg.github_user
            sova_repo = cfg.fleet.sova_repo
        except Exception:
            log.warning("propose_issue.config_load_failed", exc_info=True)

    adapter = GitHubAdapter(repo=sova_repo, github_user=github_user)

    try:
        task = await adapter.create_issue(
            title=req.title,
            body=req.body,
            labels=req.labels,
        )
    except RuntimeError as exc:
        error_msg = str(exc)[:500]
        log.warning("propose_issue.create_failed", error=error_msg, exc_info=True)
        return {"ok": False, "error": error_msg}

    return {"ok": True, "issue_number": int(task.id), "issue_url": task.url}
