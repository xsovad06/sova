"""PR tracker API router."""

from __future__ import annotations

from fastapi import APIRouter

from sova.dashboard.services.pr_service import list_open_prs_with_state

router = APIRouter(prefix="/prs", tags=["prs"])


@router.get("/open")
async def get_open_prs() -> dict:
    """List all open PRs with computed lifecycle state."""
    prs = await list_open_prs_with_state()
    return {"prs": prs}
