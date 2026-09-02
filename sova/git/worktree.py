"""Git worktree lifecycle management for SOVA."""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from sova.utils.logging import get_logger
from sova.utils.shell import ShellResult, run, run_checked, subprocess_error

log = get_logger(component="git.worktree")

WORKTREE_DIR = ".claude/worktrees"


@dataclass
class WorktreeInfo:
    """Information about a created worktree."""

    path: Path
    branch: str
    issue_id: str
    created_at: float = field(default_factory=time.time)


async def _prune_worktrees(project_dir: Path) -> None:
    """Run ``git worktree prune``, logging (but not raising) on failure."""
    result = await run("git", "worktree", "prune", cwd=project_dir)
    if not result.success:
        log.warning("worktree.prune_before_create_failed", stderr=result.stderr[:200])


async def _add_worktree(worktree_path: Path, branch: str, base_branch: str, project_dir: Path) -> ShellResult:
    """Run ``git worktree add <path> -b <branch> <base_branch>``."""
    return await run(
        "git",
        "worktree",
        "add",
        str(worktree_path),
        "-b",
        branch,
        base_branch,
        cwd=project_dir,
    )


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
            ensure_claude_artifacts(project_dir, worktree_path)
            return WorktreeInfo(path=worktree_path, branch=branch, issue_id=issue_id)
        # Stale or wrong-branch worktree -- remove and recreate
        log.info("worktree.stale_remove", path=str(worktree_path))
        await cleanup_worktree(worktree_path, cwd=project_dir)

    # Layer 1: proactively clear stale worktree registrations before creating
    await _prune_worktrees(project_dir)
    result = await _add_worktree(worktree_path, branch, base_branch, project_dir)

    if not result.success and "missing but already registered" in result.stderr:
        # Layer 2: a directory was cleaned up between prune and add. Prune
        # again to clear the newly-stale registration and retry once.
        await _prune_worktrees(project_dir)
        result = await _add_worktree(worktree_path, branch, base_branch, project_dir)
        if result.success:
            log.info("worktree.stale_registration_recovered", path=str(worktree_path), branch=branch)

    if not result.success:
        stderr = result.stderr
        if "a branch named" in stderr and "already exists" in stderr:
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
            if "is already checked out at" in stderr or "is already used by worktree" in stderr:
                log.warning("worktree.branch_checked_out_elsewhere", branch=branch, stderr=stderr[:200])
            raise subprocess_error(
                ("git", "worktree", "add", str(worktree_path), "-b", branch, base_branch),
                result,
            )

    if copy_files:
        _copy_worktree_files(project_dir, worktree_path, copy_files)

    ensure_claude_artifacts(project_dir, worktree_path)
    _ensure_compose_project_name(project_dir, worktree_path)

    return WorktreeInfo(
        path=worktree_path,
        branch=branch,
        issue_id=issue_id,
    )


async def find_worktree_by_branch(branch: str, *, cwd: Path | None = None) -> Path | None:
    """Find the worktree path where a branch is checked out.

    Parses ``git worktree list --porcelain`` output to build a branch-to-path
    mapping.  Returns the worktree path if found, or ``None``.
    """
    result = await run("git", "worktree", "list", "--porcelain", cwd=cwd)
    if not result.success:
        return None

    current_path: str | None = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree ") :]
        elif line.startswith("branch refs/heads/") and current_path is not None:
            wt_branch = line[len("branch refs/heads/") :]
            if wt_branch == branch:
                return Path(current_path)

    return None


async def resolve_worktree_conflict(
    branch: str,
    *,
    cwd: Path | None = None,
) -> Path | None:
    """Resolve a worktree conflict for a branch.

    If *branch* is checked out in a worktree, remove the stale worktree so the
    branch can be checked out elsewhere. Always runs ``git worktree prune``
    first to clean up broken references.

    Args:
        branch: The branch name to resolve conflicts for.
        cwd: Working directory for git commands.

    Returns:
        The path of the removed worktree, or None if no conflict existed.
    """
    prune_result = await run("git", "worktree", "prune", cwd=cwd)
    if not prune_result.success:
        log.warning("worktree.prune_failed", stderr=prune_result.stderr[:200])

    wt_path = await find_worktree_by_branch(branch, cwd=cwd)
    if wt_path is None:
        return None

    # Guard: never remove the main worktree (would destroy the repo)
    toplevel = await run("git", "rev-parse", "--show-toplevel", cwd=cwd)
    if not toplevel.success:
        log.error("worktree.toplevel_check_failed", branch=branch, stderr=toplevel.stderr[:200])
        raise RuntimeError(
            f"Cannot verify repository root for branch {branch!r}. Refusing to remove worktree {wt_path} (fail-closed)."
        )

    repo_root = Path(toplevel.stdout.strip()).resolve()
    if repo_root == wt_path.resolve():
        log.warning("worktree.conflict_is_main", branch=branch, path=str(wt_path))
        raise RuntimeError(
            f"Branch {branch!r} is checked out in the main worktree ({wt_path}). "
            "Cannot auto-resolve -- switch branches manually."
        )

    log.info("worktree.conflict_detected", branch=branch, path=str(wt_path))

    # If the directory doesn't exist, prune already cleaned it up
    if not wt_path.exists():
        log.info("worktree.conflict_stale_ref", branch=branch, path=str(wt_path))
        return wt_path

    # Guard: check if an agent is actively using this worktree
    active_pid = await _check_worktree_active_agent(wt_path, project_dir=repo_root)
    if active_pid is not None:
        msg = f"Cannot remove worktree {wt_path}: agent with PID {active_pid} is actively using it"
        log.error("worktree.conflict_active_agent", path=str(wt_path), pid=active_pid)
        raise RuntimeError(msg)

    await cleanup_worktree(wt_path, cwd=cwd)
    log.info("worktree.conflict_resolved", branch=branch, path=str(wt_path))
    return wt_path


async def _check_worktree_active_agent(worktree_path: Path, *, project_dir: Path | None = None) -> int | None:
    """Check if an agent is actively using a worktree.

    Queries the TaskRun DB table for non-terminal runs whose worktree_path
    matches, then verifies PID liveness via ``os.kill(pid, 0)``.

    Returns:
        The PID of the active agent, or ``None`` if no active agent found.
    """
    try:
        from sqlalchemy import select

        from sova.db.models import TaskRun
        from sova.db.session import get_session
    except ImportError:
        # DB module genuinely not installed (CLI-only context with no DB) --
        # no TaskRun tracking means no agent could own this worktree.
        return None

    _TERMINAL = frozenset({"done", "failed", "rejected", "interrupted", "paused"})
    resolved = str(worktree_path.resolve())

    try:
        async with await get_session(project_dir=project_dir) as session:
            stmt = select(TaskRun).where(
                TaskRun.worktree_path != "",
                TaskRun.worktree_path.isnot(None),
                TaskRun.status.notin_(_TERMINAL),
                TaskRun.pid.isnot(None),
            )
            result = await session.execute(stmt)
            runs = result.scalars().all()

            for run_record in runs:
                run_wt = str(Path(run_record.worktree_path).resolve())
                if run_wt == resolved and run_record.pid:
                    try:
                        os.kill(run_record.pid, 0)
                        return run_record.pid
                    except ProcessLookupError:
                        # PID is dead -- not an active agent
                        continue
                    except PermissionError:
                        # Process exists but we lack permission to signal it
                        return run_record.pid
    except Exception:
        log.warning("worktree.active_agent_check_failed", path=str(worktree_path), exc_info=True)
        raise RuntimeError(
            f"Cannot verify worktree safety for {worktree_path}: DB query failed. Refusing to remove (fail-closed)."
        )

    return None


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


_CLAUDE_DIRS = ("commands", "rules", "agent-memory", "skills")
_CLAUDE_FILES = ("CLAUDE.md", "settings.local.json", "settings.json")


def ensure_claude_artifacts(project_dir: Path, worktree_path: Path) -> None:
    """Copy .claude/ artifacts that are gitignored but needed by agents.

    Projects that gitignore ``.claude/`` (the standard pattern for SOVA-installed
    projects) end up with worktrees missing slash commands, rules, skills, and config.
    This copies the essential subset: never the database, worktrees dir, or
    ephemeral agent state.

    Safe to call repeatedly (idempotent via ``dirs_exist_ok=True``).
    Called at worktree creation and after operations that may destroy
    ``.claude/`` (e.g. rebase stash pop conflicts).
    """
    root_claude_md = project_dir / "CLAUDE.md"
    wt_claude_md = worktree_path / "CLAUDE.md"
    if root_claude_md.is_file() and not wt_claude_md.is_file():
        try:
            shutil.copy2(root_claude_md, wt_claude_md)
        except OSError:
            log.warning("worktree.copy_claude_md.failed", exc_info=True)

    claude_src = project_dir / ".claude"
    if not claude_src.is_dir():
        return

    claude_dst = worktree_path / ".claude"
    claude_dst.mkdir(exist_ok=True)

    for dirname in _CLAUDE_DIRS:
        src = claude_src / dirname
        if src.is_dir():
            try:
                shutil.copytree(src, claude_dst / dirname, dirs_exist_ok=True)
            except OSError:
                log.warning("worktree.copy_claude_dir.failed", dir=dirname, exc_info=True)

    for filename in _CLAUDE_FILES:
        src = claude_src / filename
        if src.is_file():
            try:
                shutil.copy2(src, claude_dst / filename)
            except OSError:
                log.warning("worktree.copy_claude_file.failed", file=filename, exc_info=True)


_copy_claude_artifacts = ensure_claude_artifacts


def _copy_worktree_files(project_dir: Path, worktree_path: Path, files: list[str]) -> None:
    """Copy files from the project root into a worktree.

    Files are copied; directories are symlinked (e.g. ``.venv``).
    """
    resolved_project = project_dir.resolve()
    resolved_worktree = worktree_path.resolve()
    for filename in files:
        src = (project_dir / filename).resolve()
        if not src.is_relative_to(resolved_project):
            log.warning("worktree.copy_file.path_traversal", file=filename)
            continue
        if not src.exists():
            continue
        dst_raw = worktree_path / filename
        dst_resolved = dst_raw.resolve()
        if not dst_resolved.is_relative_to(resolved_worktree):
            log.warning("worktree.copy_file.path_traversal", file=filename)
            continue
        dst_raw.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            if dst_raw.is_symlink() or dst_raw.is_dir():
                log.debug("worktree.symlink_dir.exists", file=filename)
            elif dst_raw.exists():
                raise FileExistsError(
                    f"Cannot symlink directory '{filename}': a regular file already exists at {dst_raw}"
                )
            else:
                dst_raw.symlink_to(src)
                log.debug("worktree.symlink_dir", file=filename)
        else:
            shutil.copy2(src, dst_raw)
            log.debug("worktree.copy_file", file=filename)


async def get_primary_worktree_root(cwd: Path | None = None) -> Path:
    """Return the primary (main) worktree root, even when called from a linked worktree.

    When the caller is already in the primary worktree, returns ``cwd``
    unchanged.  When the caller is inside a linked worktree (e.g. a
    ``.claude/worktrees/<id>`` directory), ``git rev-parse --git-common-dir``
    returns an absolute path to the shared ``.git`` directory inside the
    primary worktree.  In that case the primary root is its parent.
    """
    effective_cwd = cwd or Path.cwd()
    result = await run("git", "rev-parse", "--git-common-dir", cwd=effective_cwd)
    if not result.success:
        return effective_cwd
    common_dir = result.stdout.strip()
    common_path = Path(common_dir)
    if common_path.is_absolute():
        # Absolute path indicates we are inside a linked worktree.
        return common_path.parent
    return effective_cwd


_ISSUE_BRANCH_RE = re.compile(
    r"^(?:feat|fix|refactor|chore|test|docs)/"
    r"(?:issue-(?P<gh_number>\d+)|(?P<jira_key>[A-Z][A-Z0-9]+-\d+))"
)


@dataclass
class GCResult:
    """Result of an issue-aware garbage collection run."""

    worktrees_removed: int = 0
    branches_removed: int = 0
    stashes_found: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def extract_issue_from_branch(branch: str) -> tuple[str | None, str | None]:
    """Extract issue number or JIRA key from a branch name."""
    m = _ISSUE_BRANCH_RE.search(branch)
    if not m:
        return None, None
    return m.group("gh_number"), m.group("jira_key")


async def _fetch_closed_issues(project_dir: Path) -> set[str]:
    """Batch-fetch closed GitHub issue numbers."""
    result = await run(
        "gh",
        "issue",
        "list",
        "--state",
        "closed",
        "--limit",
        "200",
        "--json",
        "number",
        "--jq",
        ".[].number",
        cwd=project_dir,
    )
    if not result.success:
        log.warning("gc.fetch_closed_issues_failed", stderr=result.stderr[:200])
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


async def _list_local_branches(project_dir: Path) -> list[str]:
    """List local branch names (excluding current)."""
    result = await run("git", "branch", "--format=%(refname:short)", cwd=project_dir)
    if not result.success:
        return []
    current = await run("git", "branch", "--show-current", cwd=project_dir)
    if not current.success:
        return []
    current_branch = current.stdout.strip()
    return [b.strip() for b in result.stdout.splitlines() if b.strip() and b.strip() != current_branch]


async def _has_gone_upstream(branch: str, project_dir: Path) -> bool:
    """True only when the branch tracked a remote that no longer exists."""
    result = await run(
        "git",
        "for-each-ref",
        "--format=%(upstream)|%(upstream:track)",
        f"refs/heads/{branch}",
        cwd=project_dir,
    )
    if not result.success:
        return False
    upstream, _, track = result.stdout.strip().partition("|")
    return bool(upstream) and "gone" in track


async def _list_stashes(project_dir: Path) -> list[str]:
    """List git stash entries."""
    result = await run("git", "stash", "list", cwd=project_dir)
    if not result.success:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


async def cleanup_by_issue_state(
    *,
    project_dir: Path,
    dry_run: bool = False,
) -> GCResult:
    """Remove worktrees and branches for closed issues.

    Uses a batch query to GitHub to find closed issues, then removes
    worktrees and local branches that reference those issues. JIRA
    branches are only cleaned when their remote tracking branch is gone
    (no gh API for JIRA). Stashes are reported but never dropped.
    """
    gc_result = GCResult()
    closed_issues = await _fetch_closed_issues(project_dir)

    active_wt_branches: set[str] = set()
    wt_result = await run("git", "worktree", "list", "--porcelain", cwd=project_dir)
    if wt_result.success:
        for line in wt_result.stdout.splitlines():
            if line.startswith("branch refs/heads/"):
                active_wt_branches.add(line[len("branch refs/heads/") :])

    worktrees_dir = project_dir / WORKTREE_DIR
    if worktrees_dir.exists():
        for entry in worktrees_dir.iterdir():
            if not entry.is_dir():
                continue
            issue_id = entry.name
            if issue_id in closed_issues:
                active_pid = await _check_worktree_active_agent(entry, project_dir=project_dir)
                if active_pid is not None:
                    log.info("gc.worktree_skipped_active", path=str(entry), pid=active_pid)
                    continue
                dirty = await run("git", "status", "--porcelain", cwd=entry)
                if not dirty.success:
                    log.info("gc.worktree_skipped_status_unknown", path=str(entry), stderr=dirty.stderr[:200])
                    continue
                if dirty.stdout.strip():
                    log.info("gc.worktree_skipped_dirty", path=str(entry))
                    continue
                log.info("gc.worktree_closed_issue", path=str(entry), issue=issue_id)
                if not dry_run:
                    try:
                        await cleanup_worktree(entry, cwd=project_dir)
                    except Exception as exc:
                        gc_result.errors.append(f"Failed to remove worktree {entry}: {exc}")
                        continue
                gc_result.worktrees_removed += 1

    branches = await _list_local_branches(project_dir)
    for branch in branches:
        if branch in active_wt_branches:
            continue
        gh_num, jira_key = extract_issue_from_branch(branch)
        should_remove = False
        if gh_num and gh_num in closed_issues:
            should_remove = True
        elif jira_key and await _has_gone_upstream(branch, project_dir):
            should_remove = True
        if should_remove:
            log.info("gc.branch_cleanup", branch=branch, gh_issue=gh_num, jira_key=jira_key)
            if not dry_run:
                del_result = await run("git", "branch", "-d", branch, cwd=project_dir)
                if not del_result.success:
                    gc_result.errors.append(f"Failed to delete branch {branch}: {del_result.stderr.strip()}")
                    continue
            gc_result.branches_removed += 1

    stashes = await _list_stashes(project_dir)
    gc_result.stashes_found = stashes

    return gc_result
