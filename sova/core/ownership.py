"""Who owns a PR or issue: the one ownership check the supervisor gate and every dashboard start path share.

A PR is owned by its author and an issue by its assignees, both read from the tracker. A write (start, resume,
review, address, integrate, handoff execute, auto-chain) on a PR or issue owned by another login than
``github_user`` is refused; ``supervisor.respect_ownership = false`` turns the check off everywhere. An
unreadable author or assignee list is unknown and never refuses (logged with ``exc_info``).

Lives in ``sova.core`` so ``sova.supervisor`` and ``sova.dashboard`` both import it downward.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from sova.adapters.base import TaskAdapter
from sova.git.pr import PRInfo, find_pr_for_issue, get_pr_author
from sova.utils.gh import same_login
from sova.utils.logging import get_logger

log = get_logger(component="core.ownership")


class Ownership(StrEnum):
    """Outcome of :func:`check_ownership`."""

    ALLOWED = "allowed"  # owned by github_user, or by nobody
    OTHER = "other"  # owned by another login: refuse the write
    UNKNOWN = "unknown"  # the owner could not be read: fails open
    OFF = "off"  # respect_ownership is false, or no github_user is configured


@dataclass(frozen=True, slots=True)
class OwnershipCheck:
    """One ownership answer: *detail* names the owner when ``ownership`` is OTHER.

    *pr_number* is the PR the check read, when it found one, so a caller can reuse it.
    """

    ownership: Ownership
    owner: str = ""
    detail: str = ""
    pr_number: int | None = None

    @property
    def refused(self) -> bool:
        return self.ownership is Ownership.OTHER


def assignees_are_logins(task_source_type: str) -> bool:
    """Whether a tracker's assignees are GitHub logins, so comparing them with ``github_user`` means anything.

    False for Jira, whose assignee is a display name or account id: only the forge PR author is compared there.
    Every caller of :func:`check_ownership` passes this as *compare_assignees*, so the supervisor and the
    dashboard apply one rule.
    """
    return task_source_type != "jira"


def pr_owned_by_other(author: str, github_user: str) -> bool:
    """True when a PR's *author* is known and is not *github_user* (case-insensitive).

    A bot or a deleted ("ghost") account is another login. An empty author or user is unknown: False.
    """
    return bool(author and github_user and not same_login(author, github_user))


def issue_owned_by_other(assignees: Sequence[str], github_user: str) -> bool:
    """True when an issue is assigned, and only to logins other than *github_user*. Unassigned is False."""
    return bool(github_user and assignees and not any(same_login(name, github_user) for name in assignees))


async def check_ownership(
    issue: str | int | None,
    *,
    github_user: str,
    check_pr: bool,
    respect_ownership: bool = True,
    repo: str = "",
    pr_number: int | None = None,
    known_pr_info: PRInfo | None = None,
    adapter: TaskAdapter | None = None,
    task_assignees: Sequence[str] | None = None,
    compare_assignees: bool = True,
) -> OwnershipCheck:
    """Whether *github_user* may write to the PR or issue an action is about to act on.

    With *check_pr* (review, address, integrate, any run on a known PR) the PR author decides: *known_pr_info*,
    else the author of *pr_number*, else the open PR found for *issue*. When no PR or author is found, or without
    *check_pr*, the issue's assignees decide (*task_assignees*, else read through *adapter*) unless
    *compare_assignees* is False (a Jira project: its assignee is not a GitHub login).

    Any failure is logged and returns UNKNOWN. A caller bounds the lookups with ``asyncio.timeout()`` around
    the call, and treats its expiry as UNKNOWN too.
    """
    if not respect_ownership:
        return OwnershipCheck(Ownership.OFF)
    if not github_user:
        log.warning("ownership.github_user_not_configured", issue=issue)
        return OwnershipCheck(Ownership.OFF)
    try:
        return await _evaluate(
            issue,
            github_user=github_user,
            check_pr=check_pr,
            repo=repo,
            pr_number=pr_number,
            known_pr_info=known_pr_info,
            adapter=adapter,
            task_assignees=task_assignees,
            compare_assignees=compare_assignees,
        )
    except Exception:  # noqa: BLE001 (fail open: an unreadable owner must not block, and is logged)
        log.warning("ownership.check_failed", issue=issue, pr=pr_number, exc_info=True)
        return OwnershipCheck(Ownership.UNKNOWN, pr_number=pr_number)


async def _evaluate(
    issue: str | int | None,
    *,
    github_user: str,
    check_pr: bool,
    repo: str,
    pr_number: int | None,
    known_pr_info: PRInfo | None,
    adapter: TaskAdapter | None,
    task_assignees: Sequence[str] | None,
    compare_assignees: bool,
) -> OwnershipCheck:
    found_pr = pr_number
    if check_pr:
        author, found_pr = await _pr_author(
            issue, github_user=github_user, repo=repo, pr_number=pr_number, known_pr_info=known_pr_info
        )
        if author:
            return _pr_ownership(author, found_pr, github_user)

    if not compare_assignees:
        return OwnershipCheck(Ownership.UNKNOWN, pr_number=found_pr)
    if task_assignees is not None:
        assignees = list(task_assignees)
    elif adapter is not None and issue:
        assignees = list((await adapter.get_task(str(issue))).assignees)
    else:
        return OwnershipCheck(Ownership.UNKNOWN, pr_number=found_pr)

    if issue_owned_by_other(assignees, github_user):
        owners = ", ".join(assignees)
        return OwnershipCheck(
            Ownership.OTHER,
            owner=owners,
            detail=f"Issue #{issue} is assigned to {owners} (not {github_user})",
            pr_number=found_pr,
        )
    return OwnershipCheck(Ownership.ALLOWED, pr_number=found_pr)


def _pr_ownership(author: str, pr_number: int | None, github_user: str) -> OwnershipCheck:
    """The answer a known PR *author* gives: OTHER naming the author, else ALLOWED."""
    if pr_owned_by_other(author, github_user):
        pr = f"PR #{pr_number}" if pr_number else "The PR"
        return OwnershipCheck(
            Ownership.OTHER,
            owner=author,
            detail=f"{pr} is owned by {author} (not {github_user})",
            pr_number=pr_number,
        )
    return OwnershipCheck(Ownership.ALLOWED, owner=author, pr_number=pr_number)


async def _pr_author(
    issue: str | int | None,
    *,
    github_user: str,
    repo: str,
    pr_number: int | None,
    known_pr_info: PRInfo | None,
) -> tuple[str, int | None]:
    """The PR's author and number; ``""`` for the author when no PR or no author is known."""
    if known_pr_info is not None:
        return known_pr_info.author_login, known_pr_info.number
    if not repo:
        return "", pr_number
    if pr_number:
        return await get_pr_author(pr_number, repo=repo, github_user=github_user), pr_number
    if not issue:
        return "", None
    pr = await find_pr_for_issue(str(issue), repo=repo, github_user=github_user)
    return (pr.author_login, pr.number) if pr else ("", None)
