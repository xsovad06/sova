"""Ownership gate: blocks when issue/PR is owned by a different user.

A thin caller of ``sova.core.ownership.check_ownership()``, the check every dashboard start path also runs.
"""

from __future__ import annotations

from sova.adapters.base import TaskAdapter
from sova.core.ownership import check_ownership
from sova.git.pr import PRInfo
from sova.supervisor.gates import BlockReason


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
    compare_assignees: bool = True,
) -> tuple[BlockReason | None, int | None]:
    """Check if issue/PR is owned by the configured github_user.

    For development actions, checks issue assignee.
    For review actions (integrate), checks PR author instead (handles teammate takeover).
    *compare_assignees* is ``assignees_are_logins(config.task_source.type)``: False on a Jira project, whose
    assignee is not a GitHub login, so only the PR author is compared there (as on the dashboard).
    Fail-open on API errors (log warning and proceed).

    Returns (block_reason, discovered_pr_number). The PR number is populated when
    the gate checks a PR (integrate) and can be reused by execute_decision
    to avoid a duplicate API call.
    """
    result = await check_ownership(
        issue,
        github_user=github_user,
        check_pr=is_integrate,
        respect_ownership=respect_ownership,
        repo=repo,
        known_pr_info=known_pr_info,
        adapter=adapter,
        task_assignees=task_assignees,
        compare_assignees=compare_assignees,
    )
    discovered_pr = result.pr_number if is_integrate else None
    if result.refused:
        return BlockReason(gate="ownership", detail=result.detail), discovered_pr
    return None, discovered_pr
