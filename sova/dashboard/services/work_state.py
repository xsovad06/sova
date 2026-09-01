"""Work item state machine: state enum, labels, colors, actions, compute.

Contains the pure-logic state machine for computing a work item's unified
dashboard state from GitHub labels, PR status, running agents, and SOVA verdicts.
"""

from __future__ import annotations

from enum import StrEnum

from sova.core.state import TaskStatus


class WorkItemState(StrEnum):
    """Unified dashboard state for a work item (issue or standalone PR)."""

    # Pre-development
    BACKLOG = "backlog"
    NEEDS_SPEC = "needs_spec"
    TRIAGED = "triaged"
    RESEARCHED = "researched"

    # Active development
    IN_PROGRESS = "in_progress"
    AGENT_RUNNING = "agent_running"

    # PR lifecycle
    PR_DRAFT = "pr_draft"
    PR_CI_RUNNING = "pr_ci_running"
    PR_CI_FAILED = "pr_ci_failed"
    PR_AWAITING_REVIEW = "pr_awaiting_review"
    # kept for backward compat; new code should use PR_SOVA_CHANGES or PR_EXTERNAL_CHANGES
    PR_CHANGES_REQUESTED = "pr_changes_requested"
    # SOVA reviewer said revise/block; developer agent addresses via handoff
    PR_SOVA_CHANGES = "pr_sova_changes"
    # external reviewer (CodeRabbit/human) requested changes; /address-pr command handles thread management
    PR_EXTERNAL_CHANGES = "pr_external_changes"
    PR_REVIEW_ADDRESSED = "pr_review_addressed"
    PR_APPROVED = "pr_approved"
    PR_READY_TO_MERGE = "pr_ready_to_merge"
    PR_SOVA_PENDING = "pr_sova_pending"

    # Handoff states
    SPEC_REVIEW = "spec_review"

    # Terminal
    MERGED = "merged"
    DONE = "done"
    HUMAN_ONLY = "human_only"


_STATE_LABELS: dict[WorkItemState, str] = {
    WorkItemState.BACKLOG: "Backlog",
    WorkItemState.NEEDS_SPEC: "Needs Spec",
    WorkItemState.TRIAGED: "Triaged",
    WorkItemState.RESEARCHED: "Researched",
    WorkItemState.IN_PROGRESS: "In Progress",
    WorkItemState.AGENT_RUNNING: "Agent Running",
    WorkItemState.PR_DRAFT: "Draft PR",
    WorkItemState.PR_CI_RUNNING: "CI Running",
    WorkItemState.PR_CI_FAILED: "CI Failed",
    WorkItemState.PR_AWAITING_REVIEW: "Awaiting Review",
    WorkItemState.PR_CHANGES_REQUESTED: "Changes Requested",
    WorkItemState.PR_SOVA_CHANGES: "SOVA Changes Requested",
    WorkItemState.PR_EXTERNAL_CHANGES: "Changes Requested",
    WorkItemState.PR_REVIEW_ADDRESSED: "Review Addressed",
    WorkItemState.PR_APPROVED: "Approved",
    WorkItemState.PR_READY_TO_MERGE: "Ready to Merge",
    WorkItemState.PR_SOVA_PENDING: "Sova Review Pending",
    WorkItemState.SPEC_REVIEW: "Spec Review",
    WorkItemState.MERGED: "Merged",
    WorkItemState.DONE: "Done",
    WorkItemState.HUMAN_ONLY: "Human Only",
}

_CLR_GRAY = "bg-gray-600/30 text-gray-400"
_CLR_YELLOW = "bg-accent-yellow/20 text-accent-yellow"
_CLR_PEACH = "bg-accent-peach/20 text-accent-peach"
_CLR_GREEN = "bg-accent-green/20 text-accent-green"
_CLR_GREEN_STRONG = "bg-accent-green/30 text-accent-green"

_STATE_COLORS: dict[WorkItemState, str] = {
    WorkItemState.BACKLOG: _CLR_GRAY,
    WorkItemState.NEEDS_SPEC: _CLR_YELLOW,
    WorkItemState.TRIAGED: _CLR_YELLOW,
    WorkItemState.RESEARCHED: "bg-accent-purple/20 text-accent-purple",
    WorkItemState.IN_PROGRESS: "bg-accent/20 text-accent",
    WorkItemState.AGENT_RUNNING: _CLR_YELLOW,
    WorkItemState.PR_DRAFT: _CLR_GRAY,
    WorkItemState.PR_CI_RUNNING: _CLR_YELLOW,
    WorkItemState.PR_CI_FAILED: "bg-accent-red/20 text-accent-red",
    WorkItemState.PR_AWAITING_REVIEW: "bg-accent/20 text-accent",
    WorkItemState.PR_CHANGES_REQUESTED: _CLR_PEACH,
    WorkItemState.PR_SOVA_CHANGES: _CLR_PEACH,
    WorkItemState.PR_EXTERNAL_CHANGES: _CLR_PEACH,
    WorkItemState.PR_REVIEW_ADDRESSED: "bg-accent-lavender/20 text-accent-lavender",
    WorkItemState.PR_APPROVED: _CLR_GREEN,
    WorkItemState.PR_READY_TO_MERGE: _CLR_GREEN,
    WorkItemState.PR_SOVA_PENDING: _CLR_PEACH,
    WorkItemState.SPEC_REVIEW: _CLR_PEACH,
    WorkItemState.MERGED: _CLR_GREEN_STRONG,
    WorkItemState.DONE: _CLR_GREEN_STRONG,
    WorkItemState.HUMAN_ONLY: "bg-gray-600/30 text-gray-500",
}

_SPEC_ACTION_IDS = frozenset({"approve-spec", "revise-spec", "skip-spec", "reject-spec"})
_AWAITING_APPROVAL = TaskStatus.AWAITING_APPROVAL

_ROLE_LABELS: dict[str, str] = {
    "developer": "Developing",
    "reviewer": "Reviewing",
    "researcher": "Researching",
    "triage": "Triaging",
    "command:address-pr": "Addressing",
    "command:integrate-pr": "Integrating",
    "command:review-pr": "Reviewing",
    "command:after-merge": "Cleaning up",
    "command:spec": "Writing Spec",
}

_PR_STATE_MAP: dict[str, WorkItemState] = {
    "draft": WorkItemState.PR_DRAFT,
    "ci_running": WorkItemState.PR_CI_RUNNING,
    "ci_failed": WorkItemState.PR_CI_FAILED,
    "awaiting_review": WorkItemState.PR_AWAITING_REVIEW,
    "changes_requested": WorkItemState.PR_EXTERNAL_CHANGES,
    "review_addressed": WorkItemState.PR_REVIEW_ADDRESSED,
    "approved": WorkItemState.PR_APPROVED,
    "approved_ci_green": WorkItemState.PR_READY_TO_MERGE,
}

_LABEL_STATE_MAP: dict[str, WorkItemState] = {
    "backlog": WorkItemState.BACKLOG,
    "triaged": WorkItemState.TRIAGED,
    "researched": WorkItemState.RESEARCHED,
    "in_progress": WorkItemState.IN_PROGRESS,
    "in_review": WorkItemState.PR_AWAITING_REVIEW,
    "needs_spec": WorkItemState.NEEDS_SPEC,
    "human_only": WorkItemState.HUMAN_ONLY,
    "done": WorkItemState.DONE,
}


def _build_action(
    action_id: str,
    label: str,
    style: str,
    handler: str,
    handler_args: dict,
) -> dict:
    return {
        "id": action_id,
        "label": label,
        "style": style,
        "handler": handler,
        "handler_args": handler_args,
    }


def _get_actions(
    state: WorkItemState,
    *,
    issue_number: str | None,
    pr_number: int | None,
) -> tuple[dict | None, list[dict]]:
    """Return (primary_action, secondary_actions) for the given state."""
    i = issue_number or ""
    p = pr_number or 0

    def agent(aid: str, label: str, style: str, role: str) -> dict:
        args: dict = {"role": role}
        if i:
            args["issue"] = i
        if p:
            args["pr"] = p
        return _build_action(aid, label, style, "start_agent", args)

    def cmd(aid: str, label: str, style: str, command: str, *, pr_only: bool = False) -> dict:
        args: dict = {"command": command}
        if not pr_only and i:
            args["issue"] = i
        if p:
            args["pr"] = p
        return _build_action(aid, label, style, "run_command", args)

    S = WorkItemState
    review = cmd("review_pr", "Review PR", "neutral", "review-pr")
    address = cmd("address_pr", "Address PR", "neutral", "address-pr")
    integrate = cmd("integrate", "Integrate PR", "neutral", "integrate-pr")

    actions: dict[WorkItemState, tuple[dict | None, list[dict]]] = {
        S.BACKLOG: (agent("triage", "Triage", "warning", "triage"), []),
        S.NEEDS_SPEC: (agent("research", "Research", "purple", "researcher"), []),
        S.TRIAGED: (agent("research", "Research", "purple", "researcher"), []),
        S.RESEARCHED: (agent("develop", "Develop", "primary", "developer"), []),
        S.IN_PROGRESS: (agent("resume", "Resume", "primary", "developer"), []),
        S.PR_DRAFT: (cmd("review_pr", "Review", "neutral", "review-pr"), [address]),
        S.PR_CI_RUNNING: (cmd("review_pr", "Review", "neutral", "review-pr"), [address]),
        S.PR_CI_FAILED: (cmd("address_pr", "Address PR", "danger", "address-pr"), [review]),
        S.PR_AWAITING_REVIEW: (cmd("review_pr", "Review", "success", "review-pr"), [address, integrate]),
        S.PR_SOVA_PENDING: (cmd("review_pr", "Review PR", "warning", "review-pr"), [address, integrate]),
        S.PR_CHANGES_REQUESTED: (agent("address_review", "Address", "warning", "developer"), [review, integrate]),
        S.PR_SOVA_CHANGES: (agent("address_review", "Address", "warning", "developer"), [review, integrate]),
        S.PR_EXTERNAL_CHANGES: (cmd("address_pr", "Address PR", "warning", "address-pr"), [review, integrate]),
        S.PR_REVIEW_ADDRESSED: (cmd("review_pr", "Review", "purple", "review-pr"), [address, integrate]),
        S.PR_APPROVED: (cmd("integrate", "Integrate", "success", "integrate-pr"), [review, address]),
        S.PR_READY_TO_MERGE: (
            cmd("integrate", "Integrate", "success", "integrate-pr"),
            [review, address],
        ),
        S.MERGED: (cmd("after_merge", "Post-Merge", "purple", "after-merge"), []),
    }
    return actions.get(state, (None, []))


def compute_work_item_state(
    *,
    task_state: str | None,
    pr_data: dict | None,
    running_agent: dict | None,
    sova_verdict: dict | None = None,
    external_reviews_enabled: bool = True,
) -> WorkItemState:
    """Compute the unified dashboard state for a work item.

    Priority: running agent > PR state (adjusted by SOVA verdict) > GitHub label.
    """
    if running_agent is not None:
        return WorkItemState.AGENT_RUNNING

    if pr_data is not None:
        pr_state_raw = pr_data.get("state", "OPEN")
        if pr_state_raw == "MERGED":
            return WorkItemState.MERGED
        computed = pr_data.get("computed_state", "")
        mapped = _PR_STATE_MAP.get(computed)
        if mapped is not None:
            return _apply_sova_verdict(
                mapped,
                sova_verdict,
                latest_approval_at=pr_data.get("latest_approval_at"),
                external_reviews_enabled=external_reviews_enabled,
                unresolved_thread_count=_unresolved_thread_count(pr_data),
            )

    if task_state is not None:
        return _LABEL_STATE_MAP.get(task_state, WorkItemState.BACKLOG)

    return WorkItemState.BACKLOG


def _unresolved_thread_count(pr_data: dict) -> int:
    """Return the unresolved review thread count from enriched PR data.

    Lazy import avoids pulling pr_service's git/asyncio dependencies into this
    pure-logic module at import time.
    """
    from sova.dashboard.services.pr_service import get_unresolved_thread_count

    return get_unresolved_thread_count(pr_data)


# States where an Integrate button would be the primary action without SOVA override.
_INTEGRATE_STATES = frozenset(
    {
        WorkItemState.PR_APPROVED,
        WorkItemState.PR_READY_TO_MERGE,
    }
)

# States that SOVA verdict "revise"/"block" should downgrade to PR_SOVA_CHANGES.
_VERDICT_OVERRIDEABLE = frozenset(
    {
        WorkItemState.PR_AWAITING_REVIEW,
        WorkItemState.PR_APPROVED,
        WorkItemState.PR_READY_TO_MERGE,
        WorkItemState.PR_EXTERNAL_CHANGES,
    }
)


def _normalize_iso(ts: str) -> str:
    """Normalize ISO 8601 timestamps so string comparison works across formats."""
    return ts.replace("Z", "+00:00")


def _is_verdict_stale(sova_verdict: dict, latest_approval_at: str | None) -> bool:
    """Return True if a GitHub approval was submitted after the SOVA review."""
    if not latest_approval_at:
        return False
    reviewed_at = sova_verdict.get("reviewed_at") or ""
    if not reviewed_at:
        return False
    return _normalize_iso(latest_approval_at) > _normalize_iso(reviewed_at)


def _apply_sova_verdict(
    mapped: WorkItemState,
    sova_verdict: dict | None,
    *,
    latest_approval_at: str | None = None,
    external_reviews_enabled: bool = True,
    unresolved_thread_count: int = 0,
) -> WorkItemState:
    """Adjust a GitHub-derived PR state using the SOVA reviewer verdict."""
    if sova_verdict is None:
        return mapped

    has_review = sova_verdict.get("has_sova_review", False)
    verdict = sova_verdict.get("verdict")

    if not has_review and mapped in _INTEGRATE_STATES:
        return WorkItemState.PR_SOVA_PENDING if external_reviews_enabled else WorkItemState.PR_AWAITING_REVIEW

    if has_review and verdict == "post_failed" and mapped in _INTEGRATE_STATES:
        return WorkItemState.PR_AWAITING_REVIEW

    if has_review and verdict in ("revise", "block") and mapped in _VERDICT_OVERRIDEABLE:
        if _is_verdict_stale(sova_verdict, latest_approval_at):
            return mapped
        return WorkItemState.PR_SOVA_CHANGES

    if has_review and verdict == "approve":
        integrate_bound = mapped in _INTEGRATE_STATES or mapped == WorkItemState.PR_AWAITING_REVIEW
        if integrate_bound and unresolved_thread_count > 0:
            # SOVA approved, but unresolved review threads (from any reviewer) remain:
            # do not surface Integrate until they're resolved via /address-pr.
            return WorkItemState.PR_EXTERNAL_CHANGES
        if mapped == WorkItemState.PR_AWAITING_REVIEW:
            return WorkItemState.PR_APPROVED

    return mapped


_STATE_SORT_ORDER: dict[str, int] = {
    WorkItemState.AGENT_RUNNING: 0,
    WorkItemState.SPEC_REVIEW: 1,
    WorkItemState.PR_READY_TO_MERGE: 2,
    WorkItemState.PR_APPROVED: 2,
    WorkItemState.PR_CI_FAILED: 3,
    WorkItemState.PR_CHANGES_REQUESTED: 3,
    WorkItemState.PR_SOVA_CHANGES: 3,
    WorkItemState.PR_EXTERNAL_CHANGES: 3,
    WorkItemState.PR_SOVA_PENDING: 3,
}


def _sort_items(items: list[dict]) -> None:
    """Sort: running first, then handoff pending, then by priority."""
    items.sort(
        key=lambda i: (
            _STATE_SORT_ORDER.get(i["state"], 10),
            i["priority"],
        )
    )
