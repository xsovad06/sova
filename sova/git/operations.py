"""Git and GitHub CLI operations for SOVA."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from sova.utils.gh import resolve_gh_env
from sova.utils.logging import get_logger
from sova.utils.shell import run, run_checked

log = get_logger(component="git.operations")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class CheckStatus(StrEnum):
    """GitHub Actions check run status."""

    QUEUED = "QUEUED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"


class CheckConclusion(StrEnum):
    """GitHub Actions check run conclusion."""

    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    NEUTRAL = "NEUTRAL"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    ACTION_REQUIRED = "ACTION_REQUIRED"
    SKIPPED = "SKIPPED"


@dataclass
class PRInfo:
    """Minimal PR info returned after creation."""

    number: int
    url: str


@dataclass
class PRStatus:
    """Current status of a pull request."""

    number: int
    state: str
    mergeable: str
    review_decision: str
    url: str
    title: str

    @property
    def is_open(self) -> bool:
        return self.state == "OPEN"

    @property
    def is_mergeable(self) -> bool:
        return self.mergeable == "MERGEABLE"

    @property
    def is_approved(self) -> bool:
        return self.review_decision == "APPROVED"


@dataclass
class CICheck:
    """A single CI check result."""

    name: str
    status: CheckStatus
    conclusion: CheckConclusion | None
    details_url: str

    @property
    def is_completed(self) -> bool:
        return self.status == CheckStatus.COMPLETED

    @property
    def is_passed(self) -> bool:
        return self.is_completed and self.conclusion == CheckConclusion.SUCCESS


# ---------------------------------------------------------------------------
# Branch management
# ---------------------------------------------------------------------------


async def get_current_branch(cwd: Path | None = None) -> str:
    """Get the name of the currently checked-out branch."""
    result = await run("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)
    if not result.success:
        raise RuntimeError(f"Failed to get current branch: {result.stderr[:200]}")
    return result.stdout.strip()


async def create_branch(name: str, base: str, cwd: Path | None = None) -> None:
    """Create a new branch from a base branch."""
    log.info("git.create_branch", name=name, base=base)
    await run_checked("git", "checkout", base, cwd=cwd)
    await run_checked("git", "checkout", "-b", name, cwd=cwd)


async def sync_branch(branch: str, cwd: Path | None = None) -> None:
    """Fetch from origin and pull the latest changes for a branch."""
    log.info("git.sync_branch", branch=branch)
    await run_checked("git", "fetch", "origin", branch, cwd=cwd)
    await run_checked("git", "checkout", branch, cwd=cwd)
    await run_checked("git", "pull", "origin", branch, cwd=cwd)


async def rebase(base: str, cwd: Path | None = None) -> None:
    """Rebase the current branch onto a base branch."""
    log.info("git.rebase", base=base)
    await run_checked("git", "rebase", base, cwd=cwd)


# ---------------------------------------------------------------------------
# Commit and push
# ---------------------------------------------------------------------------


async def commit(message: str, files: list[str] | None = None, cwd: Path | None = None) -> None:
    """Stage files and create a commit."""
    log.info("git.commit", message=message[:80])

    if files:
        await run_checked("git", "add", *files, cwd=cwd)
    else:
        await run_checked("git", "add", "-A", cwd=cwd)

    await run_checked("git", "commit", "-m", message, cwd=cwd)


async def push(
    branch: str,
    *,
    force: bool = False,
    set_upstream: bool = False,
    cwd: Path | None = None,
) -> None:
    """Push a branch to origin."""
    log.info("git.push", branch=branch, force=force)

    args = ["git", "push", "origin", branch]
    if force:
        args.append("--force-with-lease")
    if set_upstream:
        args.insert(2, "-u")

    await run_checked(*args, cwd=cwd)


# ---------------------------------------------------------------------------
# PR management (via gh CLI)
# ---------------------------------------------------------------------------


async def create_pr(
    *,
    title: str,
    body: str,
    base: str,
    head: str,
    repo: str,
    github_user: str = "",
) -> PRInfo:
    """Create a pull request via gh CLI and return its info."""
    log.info("git.create_pr", title=title[:80], base=base, head=head)

    env = await resolve_gh_env(github_user)
    result = await run(
        "gh",
        "pr",
        "create",
        "--repo",
        repo,
        "--title",
        title,
        "--body",
        body,
        "--base",
        base,
        "--head",
        head,
        env=env,
    )

    if not result.success:
        raise RuntimeError(f"Failed to create PR: {result.stderr[:200]}")

    # gh pr create outputs the PR URL (e.g., https://github.com/user/repo/pull/42)
    pr_url = result.stdout.strip()
    try:
        pr_number = int(pr_url.rstrip("/").split("/")[-1])
    except (ValueError, IndexError) as exc:
        raise RuntimeError(f"Failed to parse PR number from: {pr_url}") from exc

    return PRInfo(number=pr_number, url=pr_url)


async def get_pr_status(pr_number: int, *, repo: str, github_user: str = "") -> PRStatus:
    """Get the current status of a pull request."""
    env = await resolve_gh_env(github_user)
    result = await run(
        "gh",
        "pr",
        "view",
        str(pr_number),
        "--repo",
        repo,
        "--json",
        "number,state,mergeable,reviewDecision,url,title",
        env=env,
    )

    if not result.success:
        raise RuntimeError(f"Failed to get PR #{pr_number}: {result.stderr[:200]}")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse PR #{pr_number} response: {result.stdout[:200]}") from exc

    return PRStatus(
        number=data["number"],
        state=data.get("state", ""),
        mergeable=data.get("mergeable", ""),
        review_decision=data.get("reviewDecision", "") or "",
        url=data.get("url", ""),
        title=data.get("title", ""),
    )


async def get_ci_checks(pr_number: int, *, repo: str, github_user: str = "") -> list[CICheck]:
    """Get CI check results for a pull request."""
    env = await resolve_gh_env(github_user)
    result = await run(
        "gh",
        "pr",
        "checks",
        str(pr_number),
        "--repo",
        repo,
        "--json",
        "name,status,conclusion,detailsUrl",
        env=env,
    )

    if not result.success:
        log.warning("git.ci_checks.failed", pr=pr_number, stderr=result.stderr[:200])
        return []

    try:
        checks_data = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.warning("git.ci_checks.bad_json", stdout=result.stdout[:200])
        return []

    checks: list[CICheck] = []
    for item in checks_data:
        conclusion_raw = item.get("conclusion")
        conclusion = CheckConclusion(conclusion_raw) if conclusion_raw else None

        checks.append(
            CICheck(
                name=item["name"],
                status=CheckStatus(item["status"]),
                conclusion=conclusion,
                details_url=item.get("detailsUrl", ""),
            )
        )

    return checks
