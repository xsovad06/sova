"""Branch management, commit, and push operations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from sova.utils.logging import get_logger
from sova.utils.shell import run, run_checked, subprocess_error

log = get_logger(component="git.branch")

_NO_REMOTE_REF_MARKERS = ("couldn't find remote ref",)
# Deliberately excludes the bare word "rejected": a push can be refused for
# reasons unrelated to divergence (a pre-receive hook declining it for
# secret scanning or branch protection commonly phrases its message with
# "rejected" too), and misclassifying that as a fast-forward/lease conflict
# would trigger a wasted divergence re-check and an unwarranted retry. Each
# remaining marker is specific to git's own fast-forward/lease rejection
# phrasing, including the untagged "contains work" hint git emits for a
# plain non-fast-forward rejection alongside the "(fetch first)" annotation.
_REJECTION_MARKERS = (
    "non-fast-forward",
    "fetch first",
    "stale info",
    "updates were rejected because the remote contains work",
)


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


async def _restore_stash(stashed: bool, cwd: Path | None = None) -> None:
    """Pop the stash if we stashed earlier; log a warning on failure."""
    if not stashed:
        return
    pop_result = await run("git", "stash", "pop", cwd=cwd)
    if not pop_result.success:
        log.warning("git.sync_branch.stash_pop_failed", stderr=(pop_result.stderr or "")[:200])


async def sync_branch(branch: str, cwd: Path | None = None) -> None:
    """Fetch from origin and reset the local base branch to match origin.

    Stashes uncommitted changes before checkout and restores them after.
    Uses ``git reset --hard`` instead of pull because this only targets the
    base branch (e.g. main) which should always match origin.
    """
    log.info("git.sync_branch", branch=branch)
    await run_checked("git", "fetch", "origin", branch, cwd=cwd)

    stash_result = await run("git", "stash", "--include-untracked", cwd=cwd)
    stashed = stash_result.success and "No local changes to save" not in stash_result.stdout

    checkout_result = await run("git", "checkout", branch, cwd=cwd)
    if not checkout_result.success:
        stderr = checkout_result.stderr
        if "already used by worktree" in stderr:
            # Branch is actively checked out in the primary worktree (not a
            # stale worktree). Update the local ref directly via fetch refspec
            # so downstream rebase/merge ops see an up-to-date local branch.
            log.warning("git.sync_branch.active_worktree", branch=branch)
            fetch_ref = await run("git", "fetch", "origin", f"{branch}:{branch}", cwd=cwd)
            if not fetch_ref.success:
                # Non-fast-forward or other failure: local ref stays as-is but
                # origin/{branch} was updated by the first fetch, which is
                # sufficient for rebase steps that target origin/{branch}.
                log.warning("git.sync_branch.ref_update_failed", branch=branch, stderr=(fetch_ref.stderr or "")[:200])
            await _restore_stash(stashed, cwd)
            return

        if "already checked out" in stderr:
            from sova.git.worktree import resolve_worktree_conflict

            log.warning("git.sync_branch.worktree_conflict", branch=branch)
            try:
                await resolve_worktree_conflict(branch, cwd=cwd)
            except RuntimeError as exc:
                log.warning("git.sync_branch.resolve_failed", branch=branch, error=str(exc))
                fetch_ref = await run("git", "fetch", "origin", f"{branch}:{branch}", cwd=cwd)
                if not fetch_ref.success:
                    stderr = (fetch_ref.stderr or "")[:200]
                    log.warning("git.sync_branch.ref_update_failed", branch=branch, stderr=stderr)
                await _restore_stash(stashed, cwd)
                return
            checkout_result = await run("git", "checkout", branch, cwd=cwd)

        if not checkout_result.success:
            await _restore_stash(stashed, cwd)
            raise subprocess_error(
                ("git", "checkout", branch),
                checkout_result,
            )

    await run_checked("git", "reset", "--hard", f"origin/{branch}", cwd=cwd)

    await _restore_stash(stashed, cwd)


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


async def commit(
    message: str,
    files: list[str] | None = None,
    cwd: Path | None = None,
    no_verify: bool = False,
) -> None:
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

    args = ["git", "commit", "-m", message]
    if no_verify:
        args.append("--no-verify")
    await run_checked(*args, cwd=cwd)


async def push(
    branch: str,
    *,
    force: bool = False,
    lease_sha: str | None = None,
    set_upstream: bool = False,
    cwd: Path | None = None,
    no_verify: bool = False,
) -> None:
    """Push a branch to origin.

    When ``force`` is set and ``lease_sha`` is provided, pushes with
    ``--force-with-lease=<branch>:<lease_sha>`` so a concurrent writer that
    has moved origin past the observed SHA causes the push to be refused
    instead of silently overwritten. Without a ``lease_sha``, falls back to
    a bare ``--force-with-lease``.
    """
    if not branch:
        raise RuntimeError("Cannot push: branch name is empty")

    log.info("git.push", branch=branch, force=force, lease_sha=lease_sha)

    args = ["git", "push", "origin", branch]
    if force:
        if lease_sha:
            args.append(f"--force-with-lease={branch}:{lease_sha}")
        else:
            args.append("--force-with-lease")
    if set_upstream:
        args.insert(2, "-u")
    if no_verify:
        args.append("--no-verify")

    await run_checked(*args, cwd=cwd)


class DivergenceStatus(StrEnum):
    """Result of comparing local HEAD against a branch's fetched remote tip."""

    NO_REMOTE_REF = "no_remote_ref"
    ANCESTOR = "ancestor"
    DIVERGED = "diverged"
    ERROR = "error"


@dataclass
class DivergenceCheck:
    """Outcome of :func:`check_branch_divergence`."""

    status: DivergenceStatus
    remote_sha: str | None = None
    head_sha: str | None = None
    detail: str = ""


def is_push_rejection(detail: str) -> bool:
    """Whether a push failure message describes a non-fast-forward rejection.

    Git phrases this differently across versions and push types (a plain
    non-fast-forward rejection vs. a force-with-lease "stale info" refusal),
    so this matches case-insensitively against a set of known substrings
    rather than one exact phrase.
    """
    lower = detail.lower()
    return any(marker in lower for marker in _REJECTION_MARKERS)


async def check_branch_divergence(branch: str, cwd: Path | None = None) -> DivergenceCheck:
    """Check whether local HEAD is a descendant of origin/{branch}.

    Fetches the branch fresh rather than trusting a possibly-stale
    remote-tracking ref, then asks ``merge-base --is-ancestor`` whether the
    fetched remote tip is reachable from HEAD. Used to decide whether a push
    needs ``--force-with-lease`` (:class:`~sova.core.steps.push.PushStep`)
    and to fail a doomed run early instead of discovering the conflict at
    push time (:class:`~sova.core.steps.assess.AssessStep`).

    The comparison is always against HEAD directly, not the branch ref, so
    this is well-defined even in a detached-HEAD worktree.
    """
    fetch_result = await run("git", "fetch", "origin", branch, cwd=cwd)
    if not fetch_result.success:
        stderr_lower = fetch_result.stderr.lower()
        if any(marker in stderr_lower for marker in _NO_REMOTE_REF_MARKERS):
            return DivergenceCheck(status=DivergenceStatus.NO_REMOTE_REF)
        log.warning(
            "git.check_branch_divergence.fetch_failed",
            branch=branch,
            stderr=fetch_result.stderr[:200],
        )
        return DivergenceCheck(status=DivergenceStatus.ERROR, detail=fetch_result.stderr[:200])

    remote_sha_result, head_sha_result = await asyncio.gather(
        run("git", "rev-parse", "FETCH_HEAD", cwd=cwd),
        run("git", "rev-parse", "HEAD", cwd=cwd),
    )
    if not remote_sha_result.success or not head_sha_result.success:
        log.warning("git.check_branch_divergence.rev_parse_failed", branch=branch)
        return DivergenceCheck(status=DivergenceStatus.ERROR, detail="Failed to resolve FETCH_HEAD or HEAD")

    remote_sha = remote_sha_result.stdout.strip()
    head_sha = head_sha_result.stdout.strip()

    ancestor_result = await run("git", "merge-base", "--is-ancestor", remote_sha, "HEAD", cwd=cwd)
    if ancestor_result.returncode == 0:
        return DivergenceCheck(status=DivergenceStatus.ANCESTOR, remote_sha=remote_sha, head_sha=head_sha)
    if ancestor_result.returncode == 1:
        return DivergenceCheck(status=DivergenceStatus.DIVERGED, remote_sha=remote_sha, head_sha=head_sha)

    log.warning(
        "git.check_branch_divergence.merge_base_failed",
        branch=branch,
        returncode=ancestor_result.returncode,
        stderr=ancestor_result.stderr[:200],
    )
    return DivergenceCheck(
        status=DivergenceStatus.ERROR,
        remote_sha=remote_sha,
        head_sha=head_sha,
        detail=ancestor_result.stderr[:200],
    )


async def list_dropped_commits(head_sha: str, remote_sha: str, cwd: Path | None = None) -> list[str]:
    """List commits reachable from ``remote_sha`` but not ``head_sha``.

    These are the commits a force-with-lease push against ``remote_sha``
    would discard from the branch's remote history.
    """
    result = await run("git", "log", "--oneline", f"{head_sha}..{remote_sha}", cwd=cwd)
    if not result.success:
        log.warning("git.list_dropped_commits.failed", stderr=result.stderr[:200])
        return []
    return [line for line in result.stdout.strip().splitlines() if line]
