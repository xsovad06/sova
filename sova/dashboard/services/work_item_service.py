"""Work item service -- unified state model for the agents dashboard.

Merges four state sources (GitHub labels, PR status, handoff files, running agents)
into a single computed state per issue/PR. The task browser renders from this state
instead of computing actions independently.

Priority cascade: running agent > handoff action > PR state (SOVA-verdict adjusted) > GitHub label.
"""

from __future__ import annotations

import asyncio
import re
import time
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sova.core.state import TaskStatus
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.adapters.base import PRReview
    from sova.config.models import ProjectConfig

log = get_logger(component="dashboard.work_item")


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
    HANDOFF_PENDING = "handoff_pending"
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
    WorkItemState.HANDOFF_PENDING: "Action Required",
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
    WorkItemState.HANDOFF_PENDING: _CLR_PEACH,
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
        S.NEEDS_SPEC: (cmd("spec", "Spec", "warning", "spec"), []),
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
    handoff: dict | None,
    running_agent: dict | None,
    sova_verdict: dict | None = None,
    external_reviews_enabled: bool = True,
) -> WorkItemState:
    """Compute the unified dashboard state for a work item.

    Priority: running agent > handoff (unless SOVA blocks) > PR state (adjusted by SOVA verdict) > GitHub label.

    sova_verdict is the result of get_sova_review_verdict() scoped to the current PR.
    When present it overrides the raw GitHub state:
      - Verdict "revise"/"block": override overrideable states to PR_SOVA_CHANGES (developer agent),
        unless a human approved on GitHub after the SOVA review (stale verdict).
      - Verdict from GitHub (CHANGES_REQUESTED with no SOVA override): PR_EXTERNAL_CHANGES (command).
      - No SOVA review + externally approved PR + external_reviews_enabled: override to PR_SOVA_PENDING.
      - No SOVA review + externally approved PR + not external_reviews_enabled: stay PR_AWAITING_REVIEW
        so "Review" is shown directly (no inline comments to address on bot-free projects).
      - SOVA approved but GitHub has no formal approval (owner self-reviews post as COMMENT):
        upgrade PR_AWAITING_REVIEW to PR_APPROVED so "Integrate PR" is shown instead of "Review".
    """
    if running_agent is not None:
        return WorkItemState.AGENT_RUNNING

    if handoff and handoff.get("status") == "awaiting_action":
        pr_computed = (pr_data or {}).get("computed_state", "")
        # A blocking SOVA verdict overrides the handoff: the developer agent
        # may claim "findings addressed" while the review still stands.
        sova_blocks = False
        if sova_verdict and sova_verdict.get("verdict") in ("block", "revise"):
            latest_approval_at = (pr_data or {}).get("latest_approval_at")
            if not _is_verdict_stale(sova_verdict, latest_approval_at):
                sova_blocks = True
        if pr_computed not in ("changes_requested", "ci_failed") and not sova_blocks:
            next_actions = handoff.get("next_actions", [])
            action_ids = {a.get("id") or a.get("action") or a.get("command") or "" for a in next_actions}
            if action_ids & _SPEC_ACTION_IDS:
                return WorkItemState.SPEC_REVIEW
            return WorkItemState.HANDOFF_PENDING
        if sova_blocks and pr_data is None:
            return WorkItemState.PR_SOVA_CHANGES

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
            )

    if task_state is not None:
        return _LABEL_STATE_MAP.get(task_state, WorkItemState.BACKLOG)

    return WorkItemState.BACKLOG


# States where an Integrate button would be the primary action without SOVA override.
_INTEGRATE_STATES = frozenset({WorkItemState.PR_APPROVED, WorkItemState.PR_READY_TO_MERGE})

# States that SOVA verdict "revise"/"block" should downgrade to PR_SOVA_CHANGES.
# Excludes PR_DRAFT/PR_CI_*/PR_SOVA_CHANGES (already showing correct action).
# Includes PR_EXTERNAL_CHANGES: SOVA revise overrides an external reviewer's changes-requested.
_VERDICT_OVERRIDEABLE = frozenset(
    {
        WorkItemState.PR_AWAITING_REVIEW,
        WorkItemState.PR_APPROVED,
        WorkItemState.PR_READY_TO_MERGE,
        WorkItemState.PR_EXTERNAL_CHANGES,
    }
)


def _normalize_iso(ts: str) -> str:
    """Normalize ISO 8601 timestamps so string comparison works across formats.

    GitHub uses 'Z' suffix, Python's isoformat() uses '+00:00'. Replace 'Z' with
    '+00:00' so both formats sort identically via string comparison.
    """
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
) -> WorkItemState:
    """Adjust a GitHub-derived PR state using the SOVA reviewer verdict.

    Four adjustments:
      1. No SOVA review + integrate-bound state + external_reviews_enabled: PR_SOVA_PENDING so users
         trigger SOVA review before integrating.
      2. No SOVA review + integrate-bound state + not external_reviews_enabled: return PR_AWAITING_REVIEW
         so "Review" is shown directly (no bot inline comments to address on reviewer-free projects).
      3. SOVA verdict is revise/block: downgrade overrideable states to PR_SOVA_CHANGES (developer agent),
         unless a human approved on GitHub after the SOVA review (stale verdict -- return mapped).
      4. SOVA approved but GitHub has no formal approval (owner self-reviews post as COMMENT,
         not APPROVED; GitHub forbids self-approval): upgrade PR_AWAITING_REVIEW to
         PR_APPROVED so "Integrate PR" is shown instead of "Review".
    """
    if sova_verdict is None:
        return mapped

    has_review = sova_verdict.get("has_sova_review", False)
    verdict = sova_verdict.get("verdict")

    if not has_review and mapped in _INTEGRATE_STATES:
        return WorkItemState.PR_SOVA_PENDING if external_reviews_enabled else WorkItemState.PR_AWAITING_REVIEW

    if has_review and verdict in ("revise", "block") and mapped in _VERDICT_OVERRIDEABLE:
        if _is_verdict_stale(sova_verdict, latest_approval_at):
            return mapped
        return WorkItemState.PR_SOVA_CHANGES

    if has_review and verdict == "approve" and mapped == WorkItemState.PR_AWAITING_REVIEW:
        return WorkItemState.PR_APPROVED

    return mapped


_HANDOFF_STATES = frozenset({WorkItemState.HANDOFF_PENDING, WorkItemState.SPEC_REVIEW})


def _extract_handoff_actions(handoff: dict | None, state: WorkItemState) -> list[dict]:
    if handoff and state in _HANDOFF_STATES:
        return handoff.get("next_actions", [])
    return []


def _extract_handoff_summary(handoff: dict | None, state: WorkItemState) -> str:
    if handoff and state in _HANDOFF_STATES:
        return handoff.get("summary", "")
    return ""


def _synthesize_spec_actions(issue_number: str) -> list[dict]:
    """Reconstruct spec handoff actions when the handoff file is missing.

    The handoff file may be cleared by unrelated agent runs. Synthesize
    the standard approve/reject actions so the dashboard still shows
    actionable buttons for awaiting_approval specs.
    """
    return [
        {
            "id": "approve-spec",
            "label": "Approve Spec",
            "description": "Accept the spec and proceed to development",
            "style": "approve",
            "mode": "agent",
            "args": {"issue": issue_number, "role": "developer"},
        },
        {
            "id": "reject-spec",
            "label": "Reject",
            "description": "Reject spec and mark issue as needs_spec",
            "style": "danger",
            "mode": "shell",
            "args": {"issue": issue_number},
        },
    ]


def _build_task_item(
    task: dict,
    pr_data: dict | None,
    running: dict | None,
    handoff: dict | None,
    sova_verdict: dict | None = None,
    *,
    external_reviews_enabled: bool = True,
) -> dict:
    """Build a work item from a queue task and its linked PR/handoff/agent."""
    issue_num = str(task["issue"])
    pr_number = None
    if pr_data:
        pr_number = pr_data.get("number")
    elif task.get("last_run") and task["last_run"].get("pr_number"):
        pr_number = task["last_run"]["pr_number"]

    state = compute_work_item_state(
        task_state=task.get("state"),
        pr_data=pr_data,
        handoff=handoff,
        running_agent=running,
        sova_verdict=sova_verdict if pr_data else None,
        external_reviews_enabled=external_reviews_enabled,
    )

    # Synthesize a resume action for awaiting_approval runs without handoff
    last_run = task.get("last_run")
    last_run_status = last_run.get("status") if last_run else None
    spec_handoff_actions: list[dict] = []
    if not handoff and not running and last_run and last_run_status == _AWAITING_APPROVAL:
        state = WorkItemState.SPEC_REVIEW
        primary = _build_action(
            "resume-approval",
            "Approve & Resume",
            "success",
            "resume_from_approval",
            {"run_id": last_run["id"]},
        )
        secondary = []
        spec_handoff_actions = _synthesize_spec_actions(issue_num)
    else:
        primary, secondary = _get_actions(state, issue_number=issue_num, pr_number=pr_number)

    return _build_item(
        issue_number=issue_num,
        pr_number=pr_number,
        title=task.get("title", ""),
        url=task.get("url", ""),
        state=state,
        primary_action=primary,
        secondary_actions=secondary,
        handoff_actions=spec_handoff_actions or _extract_handoff_actions(handoff, state),
        handoff_summary=_extract_handoff_summary(handoff, state),
        running_agent=_format_running_agent(running) if running else None,
        pr_details=_format_pr_details(pr_data) if pr_data else None,
        sova_context=_format_sova_context(sova_verdict if pr_data else None),
        labels=task.get("labels", []),
        priority=task.get("priority", 99),
        priority_label=task.get("priority_label", ""),
        last_run=task.get("last_run"),
        created_at=task.get("created_at", ""),
        assignees=task.get("assignees", []),
        jira_key=task.get("jira_key", ""),
        issue_type=task.get("issue_type", ""),
        last_failed=bool(task.get("last_run") and task["last_run"].get("status") == "failed"),
        story_points=task.get("story_points"),
        sprint=task.get("sprint", ""),
        components=task.get("components", []),
        jira_status=task.get("jira_status", ""),
        jira_priority=task.get("jira_priority", ""),
        updated_at=task.get("updated_at", ""),
    )


def _build_pr_item(
    pr: dict,
    running: dict | None,
    handoff: dict | None,
    issue_num: str | None,
    sova_verdict: dict | None = None,
    *,
    external_reviews_enabled: bool = True,
) -> dict:
    """Build a work item from a standalone or unlinked PR."""
    state = compute_work_item_state(
        task_state=None,
        pr_data=pr,
        handoff=handoff,
        running_agent=running,
        sova_verdict=sova_verdict,
        external_reviews_enabled=external_reviews_enabled,
    )
    primary, secondary = _get_actions(state, issue_number=issue_num, pr_number=pr["number"])

    return _build_item(
        issue_number=issue_num,
        pr_number=pr["number"],
        title=pr.get("title", ""),
        url=pr.get("url", ""),
        state=state,
        primary_action=primary,
        secondary_actions=secondary,
        handoff_actions=_extract_handoff_actions(handoff, state),
        handoff_summary=_extract_handoff_summary(handoff, state),
        running_agent=_format_running_agent(running) if running else None,
        pr_details=_format_pr_details(pr),
        sova_context=_format_sova_context(sova_verdict),
        labels=pr.get("labels", []),
        priority=-2,
        priority_label="",
        last_run=None,
        created_at="",
        assignees=pr.get("assignees", []),
        jira_key="",
        issue_type="",
        last_failed=False,
        story_points=None,
        sprint="",
        components=[],
        jira_status="",
        jira_priority="",
        updated_at=pr.get("updated_at", ""),
    )


def _append_standalone_pr_items(
    items: list[dict],
    prs: list[dict],
    linked_issue_numbers: set[str],
    running_by_issue: dict[str, dict],
    handoffs_by_issue: dict[str, dict],
    verdicts_by_issue: dict[str, dict] | None = None,
    *,
    external_reviews_enabled: bool = True,
) -> None:
    """Add work items for PRs not already covered by a queue task."""
    for pr in prs:
        issue_num = str(pr.get("linked_issue", "")) if pr.get("linked_issue") else None
        if issue_num and issue_num in linked_issue_numbers:
            continue
        if issue_num:
            linked_issue_numbers.add(issue_num)
        pr_key = issue_num if issue_num else f"pr:{pr['number']}"
        running = running_by_issue.get(pr_key)
        handoff = handoffs_by_issue.get(pr_key) if not issue_num else handoffs_by_issue.get(issue_num)
        if verdicts_by_issue is not None:
            if issue_num:
                verdict = verdicts_by_issue.get(issue_num)
            else:
                verdict = verdicts_by_issue.get(f"pr:{pr['number']}")
        else:
            verdict = None
        items.append(
            _build_pr_item(
                pr, running, handoff, issue_num, sova_verdict=verdict, external_reviews_enabled=external_reviews_enabled
            )
        )


def _find_integrate_action(item: dict) -> dict | None:
    """Find the integrate action in primary or secondary actions."""
    primary = item.get("primary_action")
    if primary and primary.get("id") == "integrate":
        return primary
    for sa in item.get("secondary_actions", []):
        if sa.get("id") == "integrate":
            return sa
    return None


async def _attach_integration_gates(
    items: list[dict],
    prs_by_issue: dict[str, dict],
    config: ProjectConfig | None,
) -> None:
    """Check integration gates for items with integrate actions and attach results."""
    if config is None:
        return

    gates_cfg = config.integration_gates
    if not (
        gates_cfg.ci_passed or gates_cfg.sova_reviewed or gates_cfg.coderabbit_reviewed or gates_cfg.threads_resolved
    ):
        return

    from sova.dashboard.services.pr_service import check_integration_gates

    async def check_item(item: dict) -> None:
        action = _find_integrate_action(item)
        if not action:
            return
        pr_data = prs_by_issue.get(item.get("issue_number") or "")
        if not pr_data and item.get("pr_details"):
            pr_data = item["pr_details"]
        if not pr_data:
            return
        try:
            result = await check_integration_gates(
                pr_data=pr_data,
                issue_number=item.get("issue_number"),
                config=config,
            )
            action["gate_result"] = result
        except Exception:
            log.warning("work_items.gate_check_failed", issue=item.get("issue_number"), exc_info=True)
            action["gate_result"] = {"passed": False, "gates": [], "error": "Gate check failed"}

    tasks = [check_item(item) for item in items if _find_integrate_action(item)]
    if tasks:
        await asyncio.gather(*tasks)


async def get_work_items(project_dir: Path | None = None) -> dict:
    """Assemble unified work items from all state sources.

    Returns: {items: [...], running_count, slots_available, max_concurrent}
    """
    from sova.dashboard.services.agent_pool import _get_project_agents

    slug = None
    if project_dir:
        slug = project_dir.name

    # Load config early so external_reviews_enabled is available when building items.
    try:
        from sova.config.loader import load_config

        cfg = load_config(project_dir)
        external_reviews_enabled = cfg.external_reviews.enabled
    except Exception:
        log.warning("work_items.config_load_failed", project_dir=str(project_dir), exc_info=True)
        cfg = None
        external_reviews_enabled = True  # Safe default: assume external reviewers exist

    queue, prs, handoffs, agents_data = await _fetch_all_sources(
        project_dir=project_dir,
        slug=slug,
    )

    running_by_issue = _index_running_agents(agents_data)
    handoffs_by_issue = _index_handoffs(handoffs)
    prs_by_issue = _index_prs_by_issue(prs)

    # Pre-fetch SOVA verdicts for all issues with open PRs and unlinked standalone PRs.
    # Scoped to current PR number so stale verdicts from prior PR revisions are excluded.
    unlinked_prs = [pr for pr in prs if not pr.get("linked_issue")]
    verdicts_by_issue = await _fetch_sova_verdicts(prs_by_issue, unlinked_prs=unlinked_prs, project_dir=project_dir)

    linked_issue_numbers: set[str] = set()

    items: list[dict] = []
    for task in queue:
        issue_num = str(task["issue"])
        pr_data = prs_by_issue.get(issue_num)
        if pr_data:
            linked_issue_numbers.add(issue_num)
        items.append(
            _build_task_item(
                task,
                pr_data,
                running_by_issue.get(issue_num),
                handoffs_by_issue.get(issue_num),
                sova_verdict=verdicts_by_issue.get(issue_num) if pr_data else None,
                external_reviews_enabled=external_reviews_enabled,
            )
        )

    _append_standalone_pr_items(
        items,
        prs,
        linked_issue_numbers,
        running_by_issue,
        handoffs_by_issue,
        verdicts_by_issue=verdicts_by_issue,
        external_reviews_enabled=external_reviews_enabled,
    )

    _sort_items(items)

    await _attach_integration_gates(items, prs_by_issue, cfg)

    pa = _get_project_agents(slug)
    max_concurrent = pa.max_concurrent if pa else 3
    running_count = sum(1 for i in items if i["state"] == WorkItemState.AGENT_RUNNING)

    return {
        "items": items,
        "running_count": running_count,
        "slots_available": max(0, max_concurrent - running_count),
        "max_concurrent": max_concurrent,
    }


# Verdict cache: {pr_number: (monotonic_timestamp, verdict_dict)}
# Positive results (has_sova_review=True) are stable -- a review verdict doesn't change.
# Negative results expire quickly so newly posted reviews are detected within 30s.
_sova_verdict_cache: dict[int, tuple[float, dict]] = {}
_VERDICT_CACHE_POSITIVE_TTL = 300.0  # 5 minutes
_VERDICT_CACHE_NEGATIVE_TTL = 30.0  # 30 seconds


def clear_verdict_cache() -> None:
    """Clear the SOVA verdict cache. Intended for testing and cache invalidation."""
    _sova_verdict_cache.clear()


_SOVA_MARKER_RE = re.compile(r"<!--\s*sova-review:\s*(approve|revise|block)\s*-->", re.IGNORECASE)
# Matches the natural-language verdict line from /review-pr command output and older pipeline output.
_SOVA_VERDICT_LINE_RE = re.compile(
    r"^\*\*(Approve|Request changes|Block|Comment only)\b",
    re.IGNORECASE | re.MULTILINE,
)
_VERDICT_NORMALIZE = {
    "approve": "approve",
    "request changes": "revise",
    "block": "block",
    "comment only": "approve",
}


def _parse_sova_review_from_github(reviews: list[PRReview]) -> dict | None:
    """Scan GitHub PR reviews for a cross-instance SOVA review.

    Processes reviews newest-first. Skips DISMISSED reviews (superseded).
    Tries the machine-readable marker first, then falls back to detecting
    SOVA's characteristic body structure for reviews posted before the
    marker was introduced.

    Returns a verdict dict matching get_sova_review_verdict()'s shape, or None.
    """

    def _verdict_dict(verdict: str, submitted_at: str) -> dict:
        return {"has_sova_review": True, "verdict": verdict, "finding_count": 0, "reviewed_at": submitted_at}

    for review in sorted(reviews, key=lambda r: r.submitted_at, reverse=True):
        if review.state == "DISMISSED":
            continue
        body = review.body or ""

        # Marker path: explicit machine-readable tag emitted by _format_findings_body.
        m = _SOVA_MARKER_RE.search(body)
        if m:
            return _verdict_dict(m.group(1).lower(), review.submitted_at)

        # Heuristic fallback: detect SOVA's characteristic review body structure.
        # Matches reviews from the /review-pr command before the marker was added.
        if "## PR Summary" in body and "## Verdict" in body:
            # Scope to the ## Verdict section to avoid matching bold lines in ## Findings.
            verdict_section = body.split("## Verdict", 1)[-1]
            verdict_match = _SOVA_VERDICT_LINE_RE.search(verdict_section)
            if verdict_match:
                verdict = _VERDICT_NORMALIZE.get(verdict_match.group(1).lower(), "revise")
                return _verdict_dict(verdict, review.submitted_at)

    return None


async def _fetch_github_review_fallback(pr_number: int, adapter: Any) -> dict | None:
    """Fetch GitHub reviews and scan for a cross-instance SOVA review marker.

    Called only when the local DB has no SOVA review record for this PR.
    This handles the case where a second SOVA instance (different machine/user)
    ran the review and its TaskRun lives in a different database.

    The adapter is built once by _fetch_sova_verdicts and shared across all PR lookups
    so that blocking config/adapter construction does not run per-PR inside asyncio.gather.
    """
    try:
        reviews = await adapter.get_pr_reviews(pr_number)
        return _parse_sova_review_from_github(reviews)
    except Exception:
        log.debug("work_items.github_review_fallback_failed", pr=pr_number, exc_info=True)
        return None


async def _fetch_sova_verdicts(
    prs_by_issue: dict[str, dict],
    unlinked_prs: list[dict] | None = None,
    project_dir: Path | None = None,
) -> dict[str, dict]:
    """Batch-fetch SOVA reviewer verdicts for all issues and unlinked PRs.

    Scoped to the current PR number so verdicts from prior PR revisions are excluded.
    Returns a dict of {issue_number: verdict_dict} for linked PRs and
    {"pr:{number}": verdict_dict} for unlinked standalone PRs.

    When the local DB has no SOVA review, falls back to scanning GitHub reviews
    for the cross-instance SOVA review marker. This handles the case where a
    second SOVA instance ran the review and its TaskRun is in a different database.
    """
    from sova.dashboard.services.agent_recovery import get_sova_review_verdict

    # Build the adapter once before the gather so blocking config/adapter construction
    # does not run per-PR inside asyncio.gather. Non-fatal: if this fails the fallback
    # is simply skipped for all PRs in this batch.
    _fallback_adapter: Any = None
    try:
        from sova.adapters import create_adapter
        from sova.config.loader import load_config

        cfg = load_config(project_dir)
        _fallback_adapter = create_adapter(cfg)
    except Exception:
        log.debug("work_items.github_fallback_adapter_build_failed", exc_info=True)

    async def fetch_one(key: str, issue_num: str | None, pr_number: int | None) -> tuple[str, dict]:
        try:
            if pr_number is not None:
                cached_entry = _sova_verdict_cache.get(pr_number)
                if cached_entry is not None:
                    ts, cached = cached_entry
                    ttl = _VERDICT_CACHE_POSITIVE_TTL if cached.get("has_sova_review") else _VERDICT_CACHE_NEGATIVE_TTL
                    if time.monotonic() - ts < ttl:
                        return key, dict(cached)

            verdict = await get_sova_review_verdict(issue_num, pr_number=pr_number, project_dir=project_dir)
            if not verdict.get("has_sova_review") and pr_number is not None and _fallback_adapter is not None:
                gh_verdict = await _fetch_github_review_fallback(pr_number, _fallback_adapter)
                if gh_verdict is not None:
                    verdict = gh_verdict

            if pr_number is not None:
                if len(_sova_verdict_cache) > 1000:
                    _sova_verdict_cache.clear()
                _sova_verdict_cache[pr_number] = (time.monotonic(), verdict)

            return key, verdict
        except Exception:
            log.debug("work_items.verdict_fetch_failed", issue=issue_num, pr=pr_number, exc_info=True)
            return key, {"has_sova_review": False, "verdict": None, "finding_count": 0, "reviewed_at": None}

    tasks = [fetch_one(issue, issue, pr.get("number")) for issue, pr in prs_by_issue.items()]
    for pr in unlinked_prs or []:
        pr_num = pr.get("number")
        if pr_num:
            tasks.append(fetch_one(f"pr:{pr_num}", None, pr_num))

    results = await asyncio.gather(*tasks)
    return dict(results)


async def _fetch_all_sources(
    *,
    project_dir: Path | None,
    slug: str | None,
) -> tuple[list[dict], list[dict], list[dict], dict]:
    """Fetch queue, PRs, handoffs, and running agents concurrently."""
    import asyncio

    from sova.dashboard.services.agent_lifecycle import get_unified_agents
    from sova.dashboard.services.handoff_service import get_all_handoffs
    from sova.dashboard.services.pr_service import list_open_prs_with_state
    from sova.dashboard.services.queue_service import get_priority_queue

    async def safe_queue() -> list[dict]:
        try:
            return await get_priority_queue(project_dir)
        except Exception:  # noqa: BLE001 -- aggregate endpoint must not fail if one source is down
            log.warning("work_items.queue_failed", exc_info=True)
            return []

    async def safe_prs() -> list[dict]:
        try:
            return await list_open_prs_with_state()
        except Exception:  # noqa: BLE001 -- aggregate endpoint must not fail if one source is down
            log.warning("work_items.prs_failed", exc_info=True)
            return []

    async def safe_agents() -> dict:
        try:
            return await get_unified_agents(slug)
        except Exception:  # noqa: BLE001 -- aggregate endpoint must not fail if one source is down
            log.warning("work_items.agents_failed", exc_info=True)
            return {"agents": [], "completed": []}

    def safe_handoffs() -> list[dict]:
        try:
            return get_all_handoffs(project_dir)
        except Exception:  # noqa: BLE001 -- aggregate endpoint must not fail if one source is down
            log.warning("work_items.handoffs_failed", exc_info=True)
            return []

    queue_task = asyncio.create_task(safe_queue())
    prs_task = asyncio.create_task(safe_prs())
    agents_task = asyncio.create_task(safe_agents())

    handoffs = safe_handoffs()
    queue = await queue_task
    prs = await prs_task
    agents_data = await agents_task

    return queue, prs, handoffs, agents_data


def _index_running_agents(agents_data: dict) -> dict[str, dict]:
    """Index running agents by issue number (or pr:<number> for standalone PRs)."""
    result: dict[str, dict] = {}
    for agent in agents_data.get("agents", []):
        issue = str(agent.get("issue") or "")
        if issue and issue.isdigit():
            result[issue] = agent
        pr_num = agent.get("pr_number")
        if pr_num is not None:
            result[f"pr:{pr_num}"] = agent
    return result


def _index_handoffs(handoffs: list[dict]) -> dict[str, dict]:
    """Index handoffs by issue number (or pr:<number> for standalone PRs), keeping only awaiting_action."""
    result: dict[str, dict] = {}
    for h in handoffs:
        if h.get("status") != "awaiting_action":
            continue
        issue = str(h.get("issue", ""))
        if issue and issue.isdigit():
            result[issue] = h
        pr_num = h.get("pr_number")
        if pr_num:
            result[f"pr:{pr_num}"] = h
    return result


def _index_prs_by_issue(prs: list[dict]) -> dict[str, dict]:
    """Index PRs by linked issue number."""
    result: dict[str, dict] = {}
    for pr in prs:
        linked = pr.get("linked_issue")
        if linked is not None:
            result[str(linked)] = pr
    return result


def _format_running_agent(agent: dict) -> dict:
    role = agent.get("role", "")
    return {
        "run_id": agent.get("run_id"),
        "role": role,
        "role_label": _ROLE_LABELS.get(role, "Running"),
        "elapsed_seconds": agent.get("elapsed_seconds", 0),
    }


def _format_sova_context(sova_verdict: dict | None) -> dict:
    """Extract the SOVA review context needed by the LLM suggestion service."""
    if not sova_verdict:
        return {"has_sova_review": False, "verdict": None}
    return {
        "has_sova_review": bool(sova_verdict.get("has_sova_review", False)),
        "verdict": sova_verdict.get("verdict"),
    }


def _format_pr_details(pr: dict) -> dict:
    return {
        "number": pr.get("number"),
        "computed_state": pr.get("computed_state", ""),
        "ci_status": pr.get("ci_status", ""),
        "review_decision": pr.get("review_decision", ""),
        "state_label": pr.get("state_label", ""),
        "url": pr.get("url", ""),
        "mergeable": pr.get("mergeable", ""),
        "author": pr.get("author", ""),
        "age_seconds": pr.get("age_seconds", 0),
        "is_draft": pr.get("is_draft", False),
        "additions": pr.get("additions", 0),
        "deletions": pr.get("deletions", 0),
        "changed_files": pr.get("changed_files", 0),
        "thread_total": pr.get("thread_total", 0),
        "thread_resolved": pr.get("thread_resolved", 0),
        "review_logins": pr.get("review_logins", []),
        "assignees": pr.get("assignees", []),
        "updated_at": pr.get("updated_at", ""),
        "commit_count": pr.get("commit_count", 0),
    }


def _build_item(**kwargs: object) -> dict:
    state: WorkItemState = kwargs["state"]  # type: ignore[assignment]
    return {
        "issue_number": kwargs["issue_number"],
        "pr_number": kwargs["pr_number"],
        "title": kwargs["title"],
        "url": kwargs["url"],
        "state": state.value,
        "state_label": _STATE_LABELS.get(state, state.value),
        "state_color": _STATE_COLORS.get(state, _CLR_GRAY),
        "primary_action": kwargs["primary_action"],
        "secondary_actions": kwargs["secondary_actions"],
        "handoff_actions": kwargs["handoff_actions"],
        "handoff_summary": kwargs.get("handoff_summary", ""),
        "running_agent": kwargs["running_agent"],
        "pr_details": kwargs["pr_details"],
        "sova_context": kwargs.get("sova_context") or {"has_sova_review": False, "verdict": None},
        "labels": kwargs["labels"],
        "priority": kwargs["priority"],
        "priority_label": kwargs["priority_label"],
        "last_run": kwargs["last_run"],
        "created_at": kwargs["created_at"],
        "assignees": kwargs["assignees"],
        "jira_key": kwargs["jira_key"],
        "issue_type": kwargs.get("issue_type", ""),
        "last_failed": kwargs.get("last_failed", False),
        "story_points": kwargs.get("story_points"),
        "sprint": kwargs.get("sprint", ""),
        "components": kwargs.get("components", []),
        "jira_status": kwargs.get("jira_status", ""),
        "jira_priority": kwargs.get("jira_priority", ""),
        "updated_at": kwargs.get("updated_at", ""),
    }


_STATE_SORT_ORDER: dict[str, int] = {
    WorkItemState.AGENT_RUNNING: 0,
    WorkItemState.HANDOFF_PENDING: 1,
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
