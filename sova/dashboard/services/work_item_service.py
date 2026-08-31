"""Work item service: unified state model for the agents dashboard.

Merges four state sources (GitHub labels, PR status, handoff files, running agents)
into a single computed state per issue/PR. The task browser renders from this state
instead of computing actions independently.

Priority cascade: running agent > PR state (SOVA-verdict adjusted) > GitHub label.

Split into focused modules:
- work_state.py:  state enum, constants, compute_work_item_state, actions, sort
- work_verdict.py: verdict cache, verdict fetching, GitHub review parsing
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from sova.dashboard.services.work_state import (
    _AWAITING_APPROVAL as _AWAITING_APPROVAL,
)
from sova.dashboard.services.work_state import (
    _LABEL_STATE_MAP as _LABEL_STATE_MAP,
)
from sova.dashboard.services.work_state import (
    _PR_STATE_MAP as _PR_STATE_MAP,
)
from sova.dashboard.services.work_state import (
    _ROLE_LABELS as _ROLE_LABELS,
)
from sova.dashboard.services.work_state import (
    _SPEC_ACTION_IDS as _SPEC_ACTION_IDS,
)
from sova.dashboard.services.work_state import (
    _STATE_COLORS as _STATE_COLORS,
)
from sova.dashboard.services.work_state import (
    _STATE_LABELS as _STATE_LABELS,
)
from sova.dashboard.services.work_state import (
    _STATE_SORT_ORDER as _STATE_SORT_ORDER,
)
from sova.dashboard.services.work_state import (  # re-export facade
    WorkItemState as WorkItemState,
)
from sova.dashboard.services.work_state import (
    _apply_sova_verdict as _apply_sova_verdict,
)
from sova.dashboard.services.work_state import (
    _build_action as _build_action,
)
from sova.dashboard.services.work_state import (
    _get_actions as _get_actions,
)
from sova.dashboard.services.work_state import (
    _is_verdict_stale as _is_verdict_stale,
)
from sova.dashboard.services.work_state import (
    _normalize_iso as _normalize_iso,
)
from sova.dashboard.services.work_state import (
    _sort_items as _sort_items,
)
from sova.dashboard.services.work_state import (
    compute_work_item_state as compute_work_item_state,
)
from sova.dashboard.services.work_verdict import (  # re-export facade
    _SOVA_MARKER_RE as _SOVA_MARKER_RE,
)
from sova.dashboard.services.work_verdict import (
    _SOVA_VERDICT_LABEL_MAP as _SOVA_VERDICT_LABEL_MAP,
)
from sova.dashboard.services.work_verdict import (
    _SOVA_VERDICT_LINE_RE as _SOVA_VERDICT_LINE_RE,
)
from sova.dashboard.services.work_verdict import (
    _VERDICT_NORMALIZE as _VERDICT_NORMALIZE,
)
from sova.dashboard.services.work_verdict import (
    _extract_sova_verdict_from_labels as _extract_sova_verdict_from_labels,
)
from sova.dashboard.services.work_verdict import (
    _fetch_github_review_fallback as _fetch_github_review_fallback,
)
from sova.dashboard.services.work_verdict import (
    _fetch_sova_verdicts as _fetch_sova_verdicts,
)
from sova.dashboard.services.work_verdict import (
    _parse_sova_review_from_github as _parse_sova_review_from_github,
)
from sova.dashboard.services.work_verdict import (
    _sova_verdict_cache as _sova_verdict_cache,
)
from sova.dashboard.services.work_verdict import (
    clear_verdict_cache as clear_verdict_cache,
)
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.config.models import ProjectConfig

log = get_logger(component="dashboard.work_item")


# Handoff helpers -------------------------------------------------------------

_HANDOFF_STATES = frozenset({WorkItemState.SPEC_REVIEW})


def _extract_handoff_actions(handoff: dict | None, state: WorkItemState) -> list[dict]:
    if handoff and state in _HANDOFF_STATES:
        return handoff.get("next_actions", [])
    return []


def _extract_handoff_summary(handoff: dict | None, state: WorkItemState) -> str:
    if handoff and state in _HANDOFF_STATES:
        return handoff.get("summary", "")
    return ""


def _synthesize_spec_actions(issue_number: str) -> list[dict]:
    """Reconstruct spec handoff actions when the handoff file is missing."""
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


# Item builders ---------------------------------------------------------------


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
        running_agent=running,
        sova_verdict=sova_verdict if pr_data else None,
        external_reviews_enabled=external_reviews_enabled,
    )

    last_run = task.get("last_run")
    last_run_status = last_run.get("status") if last_run else None
    spec_handoff_actions: list[dict] = []
    if not running and pr_data is None and last_run and last_run_status == _AWAITING_APPROVAL:
        state = WorkItemState.SPEC_REVIEW
        primary = _build_action(
            "resume-approval",
            "Approve & Resume",
            "success",
            "resume_from_approval",
            {"run_id": last_run["id"]},
        )
        secondary = []
        if not handoff:
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


# Standalone PR items ---------------------------------------------------------


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


# Integration gates ----------------------------------------------------------


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


# Main entry point ------------------------------------------------------------


async def get_work_items(project_dir: Path | None = None) -> dict:
    """Assemble unified work items from all state sources.

    Returns: {items, running_count, slots_available, max_concurrent, github_user, jira_display_name, api_health}
    api_health: {status: "ok"} or {status: "rate_limited", detail, cooldown_seconds, hits}
    """
    from sova.dashboard.services.agent_pool import _get_project_agents

    slug = None
    if project_dir:
        slug = project_dir.name

    try:
        from sova.config.loader import load_config

        cfg = load_config(project_dir)
        external_reviews_enabled = cfg.external_reviews.enabled
    except Exception:
        log.warning("work_items.config_load_failed", project_dir=str(project_dir), exc_info=True)
        cfg = None
        external_reviews_enabled = True

    queue, prs, handoffs, agents_data = await _fetch_all_sources(
        project_dir=project_dir,
        slug=slug,
    )

    running_by_issue = _index_running_agents(agents_data)
    handoffs_by_issue = _index_handoffs(handoffs)
    prs_by_issue = _index_prs_by_issue(prs)

    labels_by_issue: dict[str, list[str]] = {}
    for task in queue:
        issue_num = str(task["issue"])
        task_labels = task.get("labels", [])
        if task_labels:
            labels_by_issue[issue_num] = task_labels

    unlinked_prs = [pr for pr in prs if not pr.get("linked_issue")]
    verdicts_by_issue = await _fetch_sova_verdicts(
        prs_by_issue, unlinked_prs=unlinked_prs, project_dir=project_dir, labels_by_issue=labels_by_issue
    )

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

    github_user = cfg.github_user if cfg else ""

    api_health: dict[str, object] = {"status": "ok"}
    try:
        from sova.supervisor.github_quota import get_github_quota_status

        gh_status = get_github_quota_status(github_user)
        if gh_status.is_limited:
            api_health = {
                "status": "rate_limited",
                "detail": "GitHub API rate limit exceeded. Data may be stale.",
                "cooldown_seconds": round(gh_status.cooldown_remaining_seconds),
                "hits": gh_status.hits_in_window,
            }
    except Exception:
        log.debug("work_items.quota_status_failed", exc_info=True)

    jira_display_name = cfg.task_source.jira_display_name if cfg else ""

    return {
        "items": items,
        "running_count": running_count,
        "slots_available": max(0, max_concurrent - running_count),
        "max_concurrent": max_concurrent,
        "github_user": github_user,
        "jira_display_name": jira_display_name,
        "api_health": api_health,
    }


# Fetch helpers ---------------------------------------------------------------


async def _fetch_all_sources(
    *,
    project_dir: Path | None,
    slug: str | None,
) -> tuple[list[dict], list[dict], list[dict], dict]:
    """Fetch queue, PRs, handoffs, and running agents concurrently."""
    from sova.dashboard.services.agent_lifecycle import get_unified_agents
    from sova.dashboard.services.handoff_service import get_all_handoffs
    from sova.dashboard.services.pr_service import list_open_prs_with_state
    from sova.dashboard.services.queue_service import get_priority_queue

    async def safe_queue() -> list[dict]:
        try:
            return await get_priority_queue(project_dir)
        except Exception:
            log.warning("work_items.queue_failed", exc_info=True)
            return []

    async def safe_prs() -> list[dict]:
        try:
            return await list_open_prs_with_state()
        except Exception:
            log.warning("work_items.prs_failed", exc_info=True)
            return []

    async def safe_agents() -> dict:
        try:
            return await get_unified_agents(slug)
        except Exception:
            log.warning("work_items.agents_failed", exc_info=True)
            return {"agents": [], "completed": []}

    def safe_handoffs() -> list[dict]:
        try:
            return get_all_handoffs(project_dir)
        except Exception:
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


# Formatters ------------------------------------------------------------------


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
        "state_color": _STATE_COLORS.get(state, _STATE_COLORS[WorkItemState.BACKLOG]),
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
