"""Branch management, commit, and push operations."""

from __future__ import annotations

from pathlib import Path

from sova.utils.logging import get_logger
from sova.utils.shell import run, run_checked, subprocess_error

log = get_logger(component="git.branch")


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
    """Fetch from origin and pull the latest changes for a branch.

    If the checkout fails because the branch is already checked out in a
    worktree, automatically resolves the conflict by removing the stale
    worktree and retrying.
    """
    log.info("git.sync_branch", branch=branch)
    await run_checked("git", "fetch", "origin", branch, cwd=cwd)

    checkout_result = await run("git", "checkout", branch, cwd=cwd)
    if not checkout_result.success:
        if "already checked out" in checkout_result.stderr:
            from sova.git.worktree import resolve_worktree_conflict

            log.warning("git.sync_branch.worktree_conflict", branch=branch)
            try:
                await resolve_worktree_conflict(branch, cwd=cwd)
            except RuntimeError as exc:
                raise RuntimeError(f"Failed to resolve worktree conflict for branch {branch}: {exc}") from exc
            checkout_result = await run("git", "checkout", branch, cwd=cwd)

        if not checkout_result.success:
            raise subprocess_error(
                ("git", "checkout", branch),
                checkout_result,
            )

    await run_checked("git", "pull", "origin", branch, cwd=cwd)


async def rebase(base: str, cwd: Path | None = None) -> None:
    """Rebase the current branch onto a base branch."""
    log.info("git.rebase", base=base)
    await run_checked("git", "rebase", base, cwd=cwd)


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
