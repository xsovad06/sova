"""Git and GitHub CLI operations for SOVA."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from sova.llm.client import invoke_command
from sova.llm.models import LLMResult
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


@dataclass
class RebaseResult:
    """Outcome of a rebase-with-conflict-resolution attempt."""

    success: bool
    conflicts_resolved: int = 0
    error: str = ""


async def _get_conflicted_files(cwd: Path | None = None) -> list[str]:
    """Return list of files with merge conflicts (unmerged paths)."""
    result = await run("git", "diff", "--name-only", "--diff-filter=U", cwd=cwd)
    if not result.success or not result.stdout.strip():
        return []
    return [f.strip() for f in result.stdout.strip().splitlines() if f.strip()]


async def _resolve_conflicts_with_llm(
    conflicted_files: list[str],
    *,
    cwd: Path,
    model: str | None = None,
    max_budget_usd: Decimal | None = None,
) -> LLMResult:
    """Invoke the LLM to resolve merge conflicts in the given files."""
    file_list = "\n".join(f"- `{f}`" for f in conflicted_files)
    prompt = (
        "The following files have git merge conflicts (<<<<<<< / ======= / >>>>>>> markers). "
        "Resolve each conflict by choosing the correct code or merging both sides as appropriate. "
        "Keep the code correct and all tests passing. Do NOT leave any conflict markers.\n\n"
        f"Conflicted files:\n{file_list}\n\n"
        "After resolving, stage each file with `git add`."
    )
    return await invoke_command(prompt, model=model, cwd=cwd, max_budget_usd=max_budget_usd)


async def rebase_with_conflict_resolution(
    base: str,
    *,
    cwd: Path,
    model: str | None = None,
    max_budget_usd: Decimal | None = None,
    max_attempts: int = 3,
) -> tuple[RebaseResult, Decimal]:
    """Rebase onto *base*, using the LLM to resolve conflicts if needed.

    Returns a (RebaseResult, cost_usd) tuple.  On unrecoverable failure the
    rebase is aborted so the worktree is never left in a broken state.
    """
    cost = Decimal("0")

    fetch = await run("git", "fetch", "origin", base, cwd=cwd)
    if not fetch.success:
        return RebaseResult(success=False, error=f"Fetch failed: {fetch.stderr[:200]}"), cost

    result = await run("git", "rebase", f"origin/{base}", cwd=cwd)
    if result.success:
        return RebaseResult(success=True), cost

    conflicts_resolved = 0
    for attempt in range(1, max_attempts + 1):
        conflicted = await _get_conflicted_files(cwd=cwd)
        if not conflicted:
            cont = await run("git", "rebase", "--continue", cwd=cwd)
            if cont.success:
                return RebaseResult(success=True, conflicts_resolved=conflicts_resolved), cost
            break

        log.info("git.rebase.resolving_conflicts", files=conflicted, attempt=attempt)
        try:
            llm_result = await _resolve_conflicts_with_llm(
                conflicted,
                cwd=cwd,
                model=model,
                max_budget_usd=max_budget_usd,
            )
            cost += llm_result.cost_usd
        except RuntimeError as exc:
            log.warning("git.rebase.llm_failed", attempt=attempt, error=str(exc))
            await run("git", "rebase", "--abort", cwd=cwd)
            return RebaseResult(success=False, conflicts_resolved=conflicts_resolved, error=str(exc)), cost

        remaining = await _get_conflicted_files(cwd=cwd)
        if remaining:
            log.warning("git.rebase.unresolved", remaining=remaining, attempt=attempt)
            if attempt == max_attempts:
                await run("git", "rebase", "--abort", cwd=cwd)
                return RebaseResult(
                    success=False,
                    conflicts_resolved=conflicts_resolved,
                    error=f"Unresolved conflicts after {max_attempts} attempts: {', '.join(remaining)}",
                ), cost
            continue

        conflicts_resolved += len(conflicted)
        env = {**os.environ, "GIT_EDITOR": "true"}
        cont = await run("git", "rebase", "--continue", cwd=cwd, env=env)
        if cont.success:
            return RebaseResult(success=True, conflicts_resolved=conflicts_resolved), cost

    await run("git", "rebase", "--abort", cwd=cwd)
    error = "Rebase could not be completed"
    return RebaseResult(success=False, conflicts_resolved=conflicts_resolved, error=error), cost


# ---------------------------------------------------------------------------
# Commit and push
# ---------------------------------------------------------------------------


_SUSPICIOUS_PATHS = frozenset(
    {
        ".venv",
        ".env",
        ".env.local",
        "credentials.json",
        ".secrets",
        "node_modules",
        ".DS_Store",
        "__pycache__",
    }
)


async def commit(message: str, files: list[str] | None = None, cwd: Path | None = None) -> None:
    """Stage files and create a commit."""
    log.info("git.commit", message=message[:80])

    if files:
        await run_checked("git", "add", *files, cwd=cwd)
    else:
        await run_checked("git", "add", "-A", cwd=cwd)

    staged = await run("git", "diff", "--cached", "--name-only", cwd=cwd)
    if staged.success:
        bad = [
            f
            for f in staged.stdout.strip().splitlines()
            if any(part in _SUSPICIOUS_PATHS for part in Path(f.strip()).parts)
        ]
        if bad:
            for f in bad:
                await run("git", "reset", "HEAD", "--", f, cwd=cwd)
            raise RuntimeError(f"Refusing to commit suspicious files: {', '.join(bad)}")

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


async def assign_pr(pr_number: int, *, assignee: str, repo: str, github_user: str = "") -> None:
    """Assign a pull request to a user via gh CLI."""
    log.info("git.assign_pr", pr=pr_number, assignee=assignee)
    env = await resolve_gh_env(github_user)
    result = await run(
        "gh",
        "pr",
        "edit",
        str(pr_number),
        "--repo",
        repo,
        "--add-assignee",
        assignee,
        env=env,
    )
    if not result.success:
        log.warning("git.assign_pr.failed", pr=pr_number, stderr=result.stderr[:200])


async def find_pr_for_issue(issue_id: str, *, repo: str, github_user: str = "") -> PRInfo | None:
    """Find an open PR linked to an issue via gh CLI search.

    Searches for PRs whose body contains 'Closes #N' (or variants) and
    verifies the match to avoid false positives from free-text search.
    """
    log.info("git.find_pr_for_issue", issue=issue_id, repo=repo)
    issue_num = issue_id.lstrip("#").strip()
    env = await resolve_gh_env(github_user)
    result = await run(
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--search",
        f"#{issue_num} in:body",
        "--json",
        "number,url,body,headRefName",
        "--limit",
        "5",
        env=env,
    )
    if not result.success:
        log.warning("git.find_pr_for_issue.failed", stderr=result.stderr[:200])
        return None

    try:
        prs = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    import re

    link_pattern = re.compile(rf"(?:closes|fixes|resolves)\s+#?{re.escape(issue_num)}\b", re.IGNORECASE)
    branch_pattern = f"issue-{issue_num}"

    for pr in prs:
        body = pr.get("body", "") or ""
        head = pr.get("headRefName", "") or ""
        if link_pattern.search(body) or branch_pattern in head:
            return PRInfo(number=pr["number"], url=pr.get("url", ""))

    return None


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


_GH_STATE_MAP: dict[str, tuple[CheckStatus, CheckConclusion | None]] = {
    "SUCCESS": (CheckStatus.COMPLETED, CheckConclusion.SUCCESS),
    "FAILURE": (CheckStatus.COMPLETED, CheckConclusion.FAILURE),
    "ERROR": (CheckStatus.COMPLETED, CheckConclusion.FAILURE),
    "STARTUP_FAILURE": (CheckStatus.COMPLETED, CheckConclusion.FAILURE),
    "PENDING": (CheckStatus.IN_PROGRESS, None),
    "SKIPPING": (CheckStatus.COMPLETED, CheckConclusion.SKIPPED),
    "CANCELLED": (CheckStatus.COMPLETED, CheckConclusion.CANCELLED),
    "EXPECTED": (CheckStatus.COMPLETED, CheckConclusion.NEUTRAL),
}


async def get_pr_diff(pr_number: int, *, repo: str, github_user: str = "") -> str:
    """Fetch the full diff of a pull request via gh CLI."""
    env = await resolve_gh_env(github_user)
    result = await run(
        "gh",
        "pr",
        "diff",
        str(pr_number),
        "--repo",
        repo,
        env=env,
    )
    if not result.success:
        raise RuntimeError(f"Failed to get diff for PR #{pr_number}: {result.stderr[:200]}")
    return result.stdout


async def get_pr_files(pr_number: int, *, repo: str, github_user: str = "") -> list[str]:
    """Fetch the list of changed file paths in a pull request."""
    env = await resolve_gh_env(github_user)
    result = await run(
        "gh",
        "pr",
        "diff",
        str(pr_number),
        "--repo",
        repo,
        "--name-only",
        env=env,
    )
    if not result.success:
        raise RuntimeError(f"Failed to get files for PR #{pr_number}: {result.stderr[:200]}")
    return [f for f in result.stdout.strip().splitlines() if f.strip()]


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
        "name,state,link",
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
        state_raw = item.get("state", "PENDING")
        status, conclusion = _GH_STATE_MAP.get(state_raw, (CheckStatus.IN_PROGRESS, None))

        checks.append(
            CICheck(
                name=item["name"],
                status=status,
                conclusion=conclusion,
                details_url=item.get("link", ""),
            )
        )

    return checks
