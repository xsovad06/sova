"""Git worktree lifecycle management for SOVA."""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from sova.utils.logging import get_logger
from sova.utils.shell import run, run_checked

log = get_logger(component="git.worktree")

WORKTREE_DIR = ".claude/worktrees"


@dataclass
class WorktreeInfo:
    """Information about a created worktree."""

    path: Path
    branch: str
    issue_id: str
    created_at: float = field(default_factory=time.time)


async def create_worktree(
    *,
    issue_id: str,
    branch: str,
    base_branch: str,
    project_dir: Path,
    copy_files: list[str] | None = None,
) -> WorktreeInfo:
    """Create a git worktree for an issue.

    Creates the worktree in ``<project_dir>/.claude/worktrees/<issue_id>``,
    branching from *base_branch*.

    Args:
        issue_id: Issue identifier (used for directory naming).
        branch: Branch name to create in the worktree.
        base_branch: Base branch to start from.
        project_dir: Root of the git repository.
        copy_files: Files to copy from the project root into the worktree
                    (e.g., ``.env``). Defaults to none.
    """
    worktree_path = project_dir / WORKTREE_DIR / issue_id
    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    log.info("worktree.create", issue=issue_id, path=str(worktree_path), branch=branch)

    await run_checked(
        "git",
        "worktree",
        "add",
        str(worktree_path),
        "-b",
        branch,
        base_branch,
        cwd=project_dir,
    )

    if copy_files:
        _copy_worktree_files(project_dir, worktree_path, copy_files)

    return WorktreeInfo(
        path=worktree_path,
        branch=branch,
        issue_id=issue_id,
    )


async def cleanup_worktree(worktree_path: Path, *, cwd: Path | None = None) -> None:
    """Remove a git worktree and clean up its directory."""
    log.info("worktree.cleanup", path=str(worktree_path))

    result = await run("git", "worktree", "remove", str(worktree_path), "--force", cwd=cwd)

    if not result.success:
        log.warning("worktree.cleanup.git_failed", stderr=result.stderr[:200])
        # Fall back to manual removal if git worktree remove fails
        if worktree_path.exists():
            shutil.rmtree(worktree_path)
            await run("git", "worktree", "prune", cwd=cwd)


async def list_worktrees(*, cwd: Path | None = None) -> list[str]:
    """List all git worktrees as raw output lines."""
    result = await run("git", "worktree", "list", cwd=cwd)
    if not result.success:
        return []

    return [line for line in result.stdout.strip().splitlines() if line.strip()]


async def cleanup_stale_worktrees(
    *,
    project_dir: Path,
    ttl_days: int = 3,
) -> int:
    """Remove worktrees that have exceeded their TTL.

    Args:
        project_dir: Root of the git repository.
        ttl_days: Maximum age in days before a worktree is considered stale.

    Returns the number of worktrees removed.
    """
    worktrees_dir = project_dir / WORKTREE_DIR
    if not worktrees_dir.exists():
        return 0

    now = time.time()
    removed = 0

    for entry in worktrees_dir.iterdir():
        if not entry.is_dir():
            continue

        age_days = (now - entry.stat().st_mtime) / 86400

        if age_days > ttl_days:
            log.info("worktree.stale", path=str(entry), age_days=round(age_days, 1))
            await cleanup_worktree(entry, cwd=project_dir)
            removed += 1

    return removed


def _copy_worktree_files(project_dir: Path, worktree_path: Path, files: list[str]) -> None:
    """Copy files from the project root into a worktree."""
    for filename in files:
        src = project_dir / filename
        if src.exists():
            dst = worktree_path / filename
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            log.debug("worktree.copy_file", file=filename)
