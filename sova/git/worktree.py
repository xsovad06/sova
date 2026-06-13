"""Git worktree lifecycle management for SOVA."""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from sova.utils.logging import get_logger
from sova.utils.shell import run, run_checked, subprocess_error

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
    branching from *base_branch*.  If the branch already exists (e.g. from a
    previous run), the existing branch is checked out into the worktree instead
    of creating a new one.

    Args:
        issue_id: Issue identifier (used for directory naming).
        branch: Branch name to create in the worktree.
        base_branch: Base branch to start from.
        project_dir: Root of the git repository.
        copy_files: Files to copy from the project root into the worktree
                    (e.g., ``.env``). Defaults to none.
    """
    if ".." in issue_id or "/" in issue_id or issue_id.startswith(("/", "\\")):
        raise ValueError(f"Invalid issue_id (path traversal): {issue_id!r}")

    worktree_path = project_dir / WORKTREE_DIR / issue_id
    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    log.info("worktree.create", issue=issue_id, path=str(worktree_path), branch=branch)

    # If the worktree directory already exists, check if it's valid and reusable
    if worktree_path.exists():
        head = await run("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=worktree_path)
        if head.success and head.stdout.strip() == branch:
            ahead = await run(
                "git",
                "rev-list",
                "--count",
                f"origin/{branch}..HEAD",
                cwd=worktree_path,
            )
            if ahead.success and ahead.stdout.strip() not in ("", "0"):
                log.warning(
                    "worktree.reuse_ahead_of_origin",
                    path=str(worktree_path),
                    branch=branch,
                    commits_ahead=ahead.stdout.strip(),
                )
            log.info("worktree.reuse", path=str(worktree_path), branch=branch)
            return WorktreeInfo(path=worktree_path, branch=branch, issue_id=issue_id)
        # Stale or wrong-branch worktree -- remove and recreate
        log.info("worktree.stale_remove", path=str(worktree_path))
        await cleanup_worktree(worktree_path, cwd=project_dir)

    # Try creating a new branch in a worktree
    result = await run(
        "git",
        "worktree",
        "add",
        str(worktree_path),
        "-b",
        branch,
        base_branch,
        cwd=project_dir,
    )

    if not result.success:
        if "a branch named" in result.stderr and "already exists" in result.stderr:
            # Branch exists from a previous run -- check it out into the worktree
            log.info("worktree.existing_branch", branch=branch)
            await run_checked(
                "git",
                "worktree",
                "add",
                str(worktree_path),
                branch,
                cwd=project_dir,
            )
        else:
            raise subprocess_error(
                ("git", "worktree", "add", str(worktree_path), "-b", branch, base_branch),
                result,
            )

    if copy_files:
        _copy_worktree_files(project_dir, worktree_path, copy_files)

    _ensure_compose_project_name(project_dir, worktree_path)

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


def _ensure_compose_project_name(project_dir: Path, worktree_path: Path) -> None:
    """Ensure Docker Compose uses the same project name in worktrees.

    If the project has a docker-compose.yml without an explicit ``name:``
    directive, inject ``COMPOSE_PROJECT_NAME`` into the worktree's ``.env``
    to prevent port collisions between the main repo and worktree containers.
    """
    compose_file = worktree_path / "docker-compose.yml"
    if not compose_file.exists():
        return

    # Check if compose file already has an explicit name
    content = compose_file.read_text()
    for line in content.splitlines():
        if line.strip().startswith("name:"):
            return

    # Derive project name from the main repo directory (matches Docker Compose default)
    project_name = project_dir.name.lower().replace("-", "_")
    env_file = worktree_path / ".env"

    # Append or create .env with COMPOSE_PROJECT_NAME
    existing = env_file.read_text() if env_file.exists() else ""
    if "COMPOSE_PROJECT_NAME" not in existing:
        separator = "\n" if existing and not existing.endswith("\n") else ""
        env_file.write_text(f"{existing}{separator}COMPOSE_PROJECT_NAME={project_name}\n")
        log.info("worktree.compose_project_name", project_name=project_name)


def _copy_worktree_files(project_dir: Path, worktree_path: Path, files: list[str]) -> None:
    """Copy files from the project root into a worktree."""
    resolved_project = project_dir.resolve()
    resolved_worktree = worktree_path.resolve()
    for filename in files:
        src = (project_dir / filename).resolve()
        if not src.is_relative_to(resolved_project):
            log.warning("worktree.copy_file.path_traversal", file=filename)
            continue
        if not src.exists():
            continue
        dst = (worktree_path / filename).resolve()
        if not dst.is_relative_to(resolved_worktree):
            log.warning("worktree.copy_file.path_traversal", file=filename)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        log.debug("worktree.copy_file", file=filename)
