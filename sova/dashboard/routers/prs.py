"""PR tracker API router."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from sova.config.loader import load_config
from sova.dashboard.project_context import get_project_dir
from sova.dashboard.services.pr_service import check_integration_gates, list_open_prs_with_state
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.prs")

router = APIRouter(prefix="/prs", tags=["prs"])


class PRSuggestionRequest(BaseModel):
    deterministic_state: str
    deterministic_action_id: str
    pr_computed_state: str = ""
    has_sova_review: bool = False
    sova_verdict: str | None = None
    mergeable: str = "UNKNOWN"
    review_decision: str | None = None
    ci_passed: bool = False
    external_reviews_enabled: bool = True


class PRFeedbackRequest(BaseModel):
    pr_number: int
    issue_number: str | None = None
    deterministic_state: str
    deterministic_action_id: str
    llm_action_id: str
    llm_reasoning: str = ""
    user_choice: str


@router.get("/open")
async def get_open_prs(author_filter: str | None = Query(None, pattern="^(mine|all)$")) -> dict:
    """List all open PRs with computed lifecycle state.

    Pass ``author_filter=mine`` or ``author_filter=all`` to override the
    configured ``dashboard.pr_author_filter`` for this request.
    """
    prs = await list_open_prs_with_state(author_filter_override=author_filter)
    return {"prs": prs}


@router.get(
    "/{pr_number}/gates",
    responses={
        400: {"description": "Failed to load project configuration"},
        404: {"description": "PR not found"},
    },
)
async def get_integration_gates(pr_number: int) -> dict:
    """Check integration gate status for a specific PR."""
    project_dir = get_project_dir() or Path.cwd()

    try:
        cfg = load_config(project_dir)
    except Exception as exc:
        log.warning("prs.gates.config_error", pr=pr_number, exc_info=True)
        raise HTTPException(status_code=400, detail="Failed to load project configuration") from exc

    prs = await list_open_prs_with_state()
    pr_data = next((p for p in prs if p["number"] == pr_number), None)
    if not pr_data:
        raise HTTPException(status_code=404, detail=f"PR #{pr_number} not found")

    issue_number = str(pr_data["linked_issue"]) if pr_data.get("linked_issue") else None

    return await check_integration_gates(pr_data=pr_data, issue_number=issue_number, config=cfg)


@router.post(
    "/{pr_number}/suggestion",
    responses={
        200: {"description": "LLM suggestion (may agree or disagree with deterministic)"},
        204: {"description": "No suggestion available (no API key or LLM error)"},
    },
)
async def get_pr_action_suggestion(pr_number: int, body: PRSuggestionRequest) -> dict:
    """Get the LLM's suggested next action for a PR.

    Returns 204 when ANTHROPIC_API_KEY is not set or the LLM call fails.
    When 200, the response always includes a 'disagrees' boolean. The UI should
    only render the suggestion widget when disagrees=True.
    """
    from sova.dashboard.services.llm_suggestion_service import get_llm_suggestion

    result = await get_llm_suggestion(
        pr_number=pr_number,
        deterministic_state=body.deterministic_state,
        deterministic_action_id=body.deterministic_action_id,
        pr_computed_state=body.pr_computed_state,
        has_sova_review=body.has_sova_review,
        sova_verdict=body.sova_verdict,
        mergeable=body.mergeable,
        review_decision=body.review_decision,
        ci_passed=body.ci_passed,
        external_reviews_enabled=body.external_reviews_enabled,
    )
    if result is None:
        return Response(status_code=204)
    return result


@router.post(
    "/feedback",
    status_code=201,
    responses={
        201: {"description": "Feedback recorded"},
        400: {"description": "Invalid user_choice"},
    },
)
async def submit_pr_action_feedback(body: PRFeedbackRequest) -> dict:
    """Record user feedback on a disagreeing LLM suggestion.

    user_choice must be one of: "deterministic", "llm", or a valid action_id
    from the PR action set (review_pr, address_review, address_pr, integrate).
    """
    from sova.dashboard.services.llm_suggestion_service import _PR_ACTION_LABELS

    valid_choices = {"deterministic", "llm", "wrong"} | set(_PR_ACTION_LABELS.keys())
    if body.user_choice not in valid_choices:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid user_choice '{body.user_choice}'. Must be one of: {sorted(valid_choices)}",
        )

    from sova.db.models import ActionFeedback
    from sova.db.session import get_session

    project_dir = get_project_dir() or Path.cwd()
    async with await get_session(project_dir) as session:
        record = ActionFeedback(
            pr_number=body.pr_number,
            issue_number=body.issue_number,
            project_slug=str(project_dir.name) if project_dir else "",
            deterministic_state=body.deterministic_state,
            deterministic_action_id=body.deterministic_action_id,
            llm_action_id=body.llm_action_id,
            llm_reasoning=body.llm_reasoning,
            user_choice=body.user_choice,
            feedback_at=datetime.now(timezone.utc),
        )
        session.add(record)
        await session.commit()
        await session.refresh(record)

    log.info(
        "action_feedback.recorded",
        pr=body.pr_number,
        deterministic=body.deterministic_action_id,
        llm=body.llm_action_id,
        choice=body.user_choice,
    )
    return {"id": record.id}
