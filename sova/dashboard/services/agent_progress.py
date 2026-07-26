"""Agent pipeline progress -- step tracking and variant detection.

Separated from agent_lifecycle to isolate pipeline progress computation.
"""

from __future__ import annotations

from sova.core.steps import (
    get_address_review_step_names,
    get_developer_step_names,
    get_planner_step_names,
    get_researcher_step_names,
)

DEVELOPER_PIPELINE = get_developer_step_names()
ADDRESS_REVIEW_PIPELINE = get_address_review_step_names()
RESEARCHER_PIPELINE = get_researcher_step_names()
PLANNER_PIPELINE = get_planner_step_names()

_ADDRESS_REVIEW_ONLY = frozenset(
    {"ensure_worktree", "rebase", "address_review", "rearrange_commits", "handoff_to_user"}
)
_STANDALONE_ROLES = frozenset({"reviewer"})
_RESEARCHER_ONLY = frozenset({"fetch_task", "research"})
_PLANNER_ONLY = frozenset({"scan_project", "generate_tasks", "validate_tasks"})


def _detect_pipeline(current_step: str | None, role: str | None, pr_number: int | None) -> tuple[list[str], str]:
    """Return (pipeline_steps, variant_name) for the given run context."""
    if role == "planner" or (current_step is not None and current_step in _PLANNER_ONLY):
        return PLANNER_PIPELINE, "planner"
    if role == "researcher" or (current_step is not None and current_step in _RESEARCHER_ONLY):
        return RESEARCHER_PIPELINE, "researcher"

    is_address_review = (current_step in (None, "agent") and role == "developer" and pr_number is not None) or (
        current_step is not None and current_step in _ADDRESS_REVIEW_ONLY
    )
    if is_address_review:
        return ADDRESS_REVIEW_PIPELINE, "address_review"

    return DEVELOPER_PIPELINE, "developer"


def get_step_progress(current_step: str | None, *, role: str | None = None, pr_number: int | None = None) -> dict:
    """Compute step index from current_step name.

    Uses role+pr_number only when current_step is None or "agent" (the
    dashboard outer-process TaskRun sentinel). WorkflowEngine TaskRuns
    progress through real step names and acquire pr_number mid-pipeline
    via _sync_task_run_context, so gating on current_step avoids false
    positives for developer runs that created a PR.
    """
    is_command = role is not None and (role.startswith("command:") or role in _STANDALONE_ROLES)
    if is_command:
        return {
            "step_index": 0,
            "total_steps": 1,
            "steps": ["running"],
            "pipeline_variant": "command",
        }

    pipeline, variant = _detect_pipeline(current_step, role, pr_number)

    if current_step is None or current_step == "agent":
        idx = 0
    else:
        try:
            idx = pipeline.index(current_step)
        except ValueError:
            idx = 0

    return {
        "step_index": idx,
        "total_steps": len(pipeline),
        "steps": pipeline,
        "pipeline_variant": variant,
    }
