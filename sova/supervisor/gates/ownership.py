"""Ownership gate: blocks when issue/PR is owned by a different user."""

from __future__ import annotations

from sova.adapters.base import TaskAdapter
from sova.git.pr import PRInfo, find_pr_for_issue
from sova.supervisor.gates import BlockReason
from sova.utils.logging import get_logger

log = get_logger(component="supervisor.gates.ownership")

_NOT_COMPUTED = object()


async def check_ownership_gate(
    issue: int,
    is_integrate: bool,
    *,
    respect_ownership: bool,
    github_user: str,
    repo: str,
    adapter: TaskAdapter,
    task_assignees: list[str] | None = None,
    known_pr_info: PRInfo | None = None,
) -> tuple[BlockReason | None, int | None]:
    """Check if issue/PR is owned by the configured github_user.

    For development actions, checks issue assignee.
    For review actions (integrate), checks PR author instead (handles teammate takeover).
    Fail-open on API errors (log warning and proceed).

    Returns (block_reason, discovered_pr_number). The PR number is populated when
    the gate checks a PR (integrate) and can be reused by execute_decision
    to avoid a duplicate API call.
    """
    if not respect_ownership:
        return None, None

    if not github_user:
        log.warning("ownership_gate.github_user_not_configured", issue=issue)
        return None, None

    try:
        discovered_pr: int | None = None
        if is_integrate:
            pr_result, discovered_pr = await _check_pr_ownership(
                issue, github_user=github_user, repo=repo, known_pr_info=known_pr_info
            )
            if pr_result is not _NOT_COMPUTED:
                return pr_result, discovered_pr

        block = await _check_issue_ownership(issue, github_user, adapter, task_assignees)
        return block, discovered_pr

    except Exception:
        log.warning("ownership_gate.check_failed", issue=issue, exc_info=True)
        return None, None


async def _check_pr_ownership(
    issue: int,
    *,
    github_user: str,
    repo: str,
    known_pr_info: PRInfo | None = None,
) -> tuple[BlockReason | object | None, int | None]:
    """Check PR author ownership.

    Returns (gate_result, pr_number) where gate_result is:
    BlockReason if blocked, None if authorized,
    _NOT_COMPUTED if should fall through.
    """
    if not repo:
        return _NOT_COMPUTED, None

    pr = known_pr_info or await find_pr_for_issue(str(issue), repo=repo, github_user=github_user)
    if not pr or not pr.author_login:
        return _NOT_COMPUTED, pr.number if pr else None

    if pr.author_login != github_user:
        return BlockReason(
            gate="ownership",
            detail=f"PR #{pr.number} is owned by {pr.author_login} (not {github_user})",
        ), pr.number
    return None, pr.number


async def _check_issue_ownership(
    issue: int,
    github_user: str,
    adapter: TaskAdapter,
    task_assignees: list[str] | None,
) -> BlockReason | None:
    """Check issue assignee ownership. Returns None if unassigned or assigned to self."""
    if task_assignees is not None:
        assignees = task_assignees
    else:
        task = await adapter.get_task(str(issue))
        assignees = task.assignees

    if not assignees or github_user in assignees:
        return None

    assignee_str = ", ".join(assignees)
    return BlockReason(
        gate="ownership",
        detail=f"Issue #{issue} is assigned to {assignee_str} (not {github_user})",
    )
