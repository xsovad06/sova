"""Reuse of a full check-command run across pipeline call sites.

The developer pipeline's command-level instructions (``develop.md`` Step 6,
``simplify.md`` Step 3, ``review.md`` Step 7, ``pr.md`` Step 8) each tell the
dispatched agent to run the project's full check command, duplicating the same
check within a single autonomous cycle when nothing in the worktree changed
between calls. This module lets a later call site skip a re-run when an
earlier one in the same cycle already proved the identical worktree content
passes.

Every call site records its own result from inside the ``<check> && sova
checks record`` chain, never as a separate step: an agent in a fix loop
typically re-runs only the failing tests, so a record outside that chain
would claim a full-check pass for content the full check never ran against.

Reuse is content-addressed (a hash of ``HEAD``, the working-tree diff, and
the check command itself) and scoped to autonomous pipeline invocations only
via ``PIPELINE_STAGE_ENV_KEY``: a standalone ``/develop``, ``/review``, or
``/pr`` invocation never sees that env var, so ``should_skip()`` always
returns False for it and the command's documented full check always runs.

The marker is a plain worktree-local JSON file under
``.claude/agent-control/``, matching the convention ``sova.ipc.handoff``
already uses for cross-process state.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from sova.utils.files import read_text_or_none
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="core.check_cache")

MARKER_FILENAME = "check-state.json"

# Set by sova.core.agent_dispatch on every tool-using pipeline step's spawned
# agent, and nowhere else: a human's own interactive Claude Code session, and
# the dashboard's one-off start_command() spawn, both reach the AgentRuntime
# without going through agent_dispatch.py, so neither ever sees this var.
PIPELINE_STAGE_ENV_KEY = "SOVA_PIPELINE_STAGE"


def _marker_path(cwd: Path) -> Path:
    return cwd / ".claude" / "agent-control" / MARKER_FILENAME


async def _diff_base(cwd: Path) -> str | None:
    """Resolve the commit the worktree diff is taken against, or None if git cannot answer.

    The merge base with the base branch, not HEAD: a diff taken against HEAD
    changes the moment CommitStep commits already-checked work (the pending
    diff becomes empty and HEAD moves), even though the files the check
    command reads are byte-identical. Keying on the cumulative
    merge-base-to-worktree diff instead makes committing, amending, and
    rearranging commits transparent, so review.md's recorded pass is still
    reusable by pr.md on the other side of CommitStep.

    Falls back to HEAD when no base-branch ref is reachable (a worktree with
    no remote, e.g. a fixture repo), which only narrows reuse.
    """
    head = await run("git", "rev-parse", "HEAD", cwd=cwd)
    if not head.success or not head.stdout.strip():
        return None
    candidates: list[str] = []
    symref = await run("git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=cwd)
    if symref.success and symref.stdout.strip():
        candidates.append(symref.stdout.strip())
    candidates += ["origin/main", "origin/master"]
    for ref in candidates:
        merge_base = await run("git", "merge-base", ref, "HEAD", cwd=cwd)
        if merge_base.success and merge_base.stdout.strip():
            return merge_base.stdout.strip()
    return head.stdout.strip()


async def _untracked_fingerprint(cwd: Path) -> str | None:
    """Fingerprint every untracked, non-ignored file by path and content hash.

    Content, not just paths: an agent that creates a file and later edits it
    leaves the path listing identical while the file the check command reads
    has changed, so a path-only fingerprint would report a stale pass as
    current. ``git hash-object`` does the hashing in one subprocess and
    writes nothing to the object database.

    ``.claude/agent-control/`` is excluded because writing the marker there
    is not a worktree change: without the exclusion, recording a passing
    result would invalidate the very hash it was recorded under on any
    project that has not gitignored that directory.
    """
    listed = await run(
        "git", "ls-files", "--others", "--exclude-standard", "--", ".", ":(exclude).claude/agent-control", cwd=cwd
    )
    if not listed.success:
        return None
    paths = [line for line in listed.stdout.splitlines() if line]
    if not paths:
        return ""
    hashed = await run("git", "hash-object", "--stdin-paths", cwd=cwd, stdin="\n".join(paths) + "\n")
    blobs = hashed.stdout.split()
    if not hashed.success or len(blobs) != len(paths):
        return None
    return "\n".join(f"{path}\x01{blob}" for path, blob in zip(paths, blobs))


async def _compute_hash(cwd: Path, check_cmd: str) -> str | None:
    """Hash the worktree's content and check_cmd, or None when git cannot report the state.

    Covers every change source DevelopStep.validate_output() already treats
    as "the worktree changed": committed, staged and unstaged (the diff
    against _diff_base()) and untracked (_untracked_fingerprint()).
    check_cmd is folded in too, so a mid-cycle change to the configured check
    command invalidates reuse instead of serving a stale result for a
    different command.

    Returns None rather than folding a failed git call's empty output into
    the payload: a hash built from a partial answer can collide with another
    worktree's, so "could not determine the state" must never read as "the
    state matches" (AGENTS.md, "Failure must not look like success").
    """
    base = await _diff_base(cwd)
    if base is None:
        log.warning("check_cache.hash_unavailable", reason="no_head")
        return None
    untracked = await _untracked_fingerprint(cwd)
    diff = await run("git", "diff", base, cwd=cwd)
    if untracked is None or not diff.success:
        log.warning("check_cache.hash_unavailable", reason="git_failed", base=base)
        return None
    payload = "\x00".join([base, untracked, diff.stdout, check_cmd])
    return hashlib.sha256(payload.encode("utf-8", errors="surrogateescape")).hexdigest()


def _read_marker(cwd: Path) -> dict | None:
    content = read_text_or_none(_marker_path(cwd))
    if content is None:
        return None
    try:
        data = json.loads(content)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def is_pipeline_dispatch() -> bool:
    """Whether this process was spawned as a tool-using autonomous pipeline step.

    See PIPELINE_STAGE_ENV_KEY.
    """
    return os.environ.get(PIPELINE_STAGE_ENV_KEY, "").strip() == "1"


async def should_skip(cwd: Path, check_cmd: str) -> bool:
    """Return True when an identical full check already passed earlier in this cycle.

    Always False outside an autonomous pipeline dispatch (see
    is_pipeline_dispatch()), and False for a failed or hash-mismatched
    marker: only a marker recorded as passed, for this exact worktree
    content and check command, is ever reused.
    """
    if not is_pipeline_dispatch():
        return False
    marker = _read_marker(cwd)
    if marker is None or marker.get("passed") is not True:
        return False
    current = await _compute_hash(cwd, check_cmd)
    return current is not None and marker.get("hash") == current


async def record_result(cwd: Path, check_cmd: str, *, passed: bool) -> bool:
    """Persist the outcome of a full check run, keyed to the worktree state that produced it.

    Returns whether the marker was written. Recording is advisory: an
    unhashable worktree or an unwritable marker path only costs a later call
    site its reuse, so neither is raised at the caller (``sova checks
    record`` runs chained after the real check command, and a non-zero exit
    there would report a passing check as a failing one).
    """
    state_hash = await _compute_hash(cwd, check_cmd)
    if state_hash is None:
        log.warning("check_cache.record_skipped", reason="hash_unavailable", check_cmd=check_cmd)
        return False
    payload = {
        "hash": state_hash,
        "passed": passed,
        "check_cmd": check_cmd,
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    marker_path = _marker_path(cwd)
    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("check_cache.record_failed", error=str(exc), check_cmd=check_cmd)
        return False
    log.info("check_cache.recorded", passed=passed, check_cmd=check_cmd)
    return True


def clear_marker(cwd: Path) -> None:
    """Remove a worktree's marker so a worktree reused across cycles never inherits a stale one.

    Non-fatal: both call sites are pipeline steps whose own job (creating the
    worktree, capturing the baseline) must not fail because an advisory
    marker could not be deleted. A surviving marker still has to match the
    content hash before it is ever reused.
    """
    try:
        _marker_path(cwd).unlink(missing_ok=True)
    except OSError as exc:
        log.warning("check_cache.clear_failed", error=str(exc), cwd=str(cwd))
