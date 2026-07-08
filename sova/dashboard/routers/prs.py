"""PR tracker API router."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from sova.config.loader import load_config
from sova.dashboard.project_context import get_project_dir
from sova.dashboard.services.pr_service import check_integration_gates, list_open_prs_with_state

router = APIRouter(prefix="/prs", tags=["prs"])


@router.get("/open")
async def get_open_prs() -> dict:
    """List all open PRs with computed lifecycle state."""
    prs = await list_open_prs_with_state()
    return {"prs": prs}


@router.get(
    "/{pr_number}/gates",
    responses={
        400: {"description": "No project configured"},
        404: {"description": "PR not found"},
    },
)
async def get_integration_gates(pr_number: int) -> dict:
    """Check integration gate status for a specific PR."""
    project_dir = get_project_dir()
    if not project_dir:
        raise HTTPException(status_code=400, detail="No project configured")

    try:
        cfg = load_config(project_dir)
    except Exception:
        raise HTTPException(status_code=400, detail="Failed to load project configuration")

    prs = await list_open_prs_with_state()
    pr_data = next((p for p in prs if p["number"] == pr_number), None)
    if not pr_data:
        raise HTTPException(status_code=404, detail=f"PR #{pr_number} not found")

    issue_number = str(pr_data["linked_issue"]) if pr_data.get("linked_issue") else None

    return await check_integration_gates(pr_data=pr_data, issue_number=issue_number, config=cfg)
