"""Reuse of a check-command run across pipeline call sites.

The developer pipeline's command-level instructions (``develop.md`` Step 6,
``simplify.md`` Step 3, ``review.md`` Step 7 run the project's fast check,
``pr.md`` Step 8 its full check) and DevelopStep's inner check loop (the full
check) can each repeat a check within a single autonomous cycle when nothing
in the worktree changed between calls. This module lets a later call site
skip a re-run when an earlier one in the same cycle already proved the
identical worktree content passes the same command.

Every call site records its own result from inside the ``<check> && sova
checks record`` chain, never as a separate step: an agent in a fix loop
typically re-runs only the failing tests, so a record outside that chain
would claim a full-check pass for content the full check never ran against.

Reuse is content-addressed (a hash of the merge base, the content of every
changed or untracked file, and the check command itself) and scoped to
autonomous pipeline invocations only via ``PIPELINE_STAGE_ENV_KEY``: a
standalone ``/develop``, ``/simplify``, ``/review``, or ``/pr`` invocation never sees that env var, so
``should_skip()`` always returns False for it and the command runs its documented check: the configured fast
check for develop, simplify and review (their own command text, falling back to the full check when no fast
check is configured), the full check for ``pr.md``.
DevelopStep's inner check loop is the one in-process caller: it uses
``has_recorded_pass()`` for its first run only, since it is itself the
pipeline.

A check's own untracked outputs (coverage reports, build artifacts) are not content it read.
The key is therefore taken from the worktree with those paths left out: ``should_skip()``
notes the untracked paths before the check runs, ``record_result()`` records
every path that did not exist before it as ``excluded`` (plus the paths earlier records in the
marker already excluded, and the outputs of the baseline run that ``seed_outputs()`` noted), and a
later lookup leaves the same paths out for as long as they stay
untracked. Once such a path is staged or committed it is content again and is hashed.

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

# Written by should_skip() on a miss and consumed by the record_result() call chained after the check:
# the untracked paths from just before the check ran, so its own outputs can be told apart afterwards.
PENDING_FILENAME = "check-pending.json"

# The marker keeps the latest record plus this many earlier ones, each keyed to its own content hash
# and command text. One record would let a fast check recorded after the inner loop's full check
# overwrite it, so a later call site could never reuse the full pass.
EARLIER_RECORDS_KEPT = 3

# Set by sova.core.agent_dispatch on every tool-using pipeline step's spawned
# agent, and nowhere else: a human's own interactive Claude Code session, and
# the dashboard's one-off start_command() spawn, both reach the AgentRuntime
# without going through agent_dispatch.py, so neither ever sees this var.
PIPELINE_STAGE_ENV_KEY = "SOVA_PIPELINE_STAGE"


def _marker_path(cwd: Path) -> Path:
    return cwd / ".claude" / "agent-control" / MARKER_FILENAME


def _pending_path(cwd: Path) -> Path:
    return cwd / ".claude" / "agent-control" / PENDING_FILENAME


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


async def _locate(cwd: Path) -> Path | None:
    """The repository top level, or None when git cannot answer.

    Every path in the key is relative to the top level, not to *cwd*: a check run from a
    subdirectory still reads files elsewhere in the repository (a root ``pyproject.toml``, a
    sibling package), and an edit there must change the key.
    """
    located = await run("git", "rev-parse", "--show-toplevel", cwd=cwd)
    if not located.success or not located.stdout.strip():
        return None
    return Path(located.stdout.strip())


# Advisory state this module writes. Matched at any depth: a call made from a subdirectory writes
# its marker under that subdirectory's ``.claude/agent-control/``, and the top-level listing must
# not see it as a worktree change.
_CONTROL_EXCLUDE = ":(exclude,glob)**/.claude/agent-control/**"


async def _untracked_paths(top: Path) -> set[str] | None:
    """The untracked, non-ignored paths in the repository (relative to *top*), or None when git cannot answer."""
    untracked = await run(
        "git", "ls-files", "-z", "--others", "--exclude-standard", "--", ".", _CONTROL_EXCLUDE, cwd=top
    )
    if not untracked.success:
        return None
    return {p for p in untracked.stdout.split("\0") if p}


async def _nested_repo_state(full: Path) -> str | None:
    """The state of a nested repository or submodule at *full*: its commit, or None when it is not provably clean.

    A commit alone would miss an uncommitted edit or a new file inside it, so a dirty nested
    repository reads as unhashable (no reuse) rather than as its commit.
    """
    if not (full / ".git").exists():
        # A submodule directory that was never checked out holds nothing a check could read.
        return "uninitialized"
    head = await run("git", "rev-parse", "HEAD", cwd=full)
    if not head.success or not head.stdout.strip():
        return None
    status = await run("git", "status", "--porcelain=v1", "-z", "--untracked-files=all", cwd=full)
    if not status.success or status.stdout.strip("\0\n "):
        return None
    return head.stdout.strip()


async def _path_states(top: Path, paths: list[str]) -> dict[str, str] | None:
    """Each path's state by what the file holds now, or None when any state cannot be read.

    Each path gets an explicit state, never a default:

    - a regular file: its ``git hash-object`` blob (one subprocess, nothing written to the
      object database), plus the executable bit as git's diff folds it in (a check can test it);
      no other stat data, such as a timestamp, is ever read
    - a symlink: its target string (``link:<target>``), which is what git tracks; hash-object
      would follow the link, failing on a link to a directory and missing a retarget to a file
      with identical bytes
    - a submodule or nested repository: ``repo:<checked-out commit>`` when it has no uncommitted
      change (a bump changes the key), else None (see _nested_repo_state())
    - a path missing from the worktree (deleted, or renamed away): ``absent``
    - anything else (a socket, a fifo): None, since the state cannot be read and must not hash
      like another one
    """
    states: dict[str, str] = {}
    to_hash: list[str] = []
    for path in paths:
        full = top / path.rstrip("/")
        if full.is_symlink():
            states[path] = f"link:{os.readlink(full)}"
        elif full.is_file():
            to_hash.append(path)
        elif full.is_dir():
            head = await _nested_repo_state(full)
            if head is None:
                return None
            states[path] = f"repo:{head}"
        elif full.exists():
            return None
        else:
            states[path] = "absent"
    if to_hash:
        hashed = await run("git", "hash-object", "--stdin-paths", cwd=top, stdin="\n".join(to_hash) + "\n")
        blobs = hashed.stdout.split()
        if not hashed.success or len(blobs) != len(to_hash):
            return None
        for path, blob in zip(to_hash, blobs):
            states[path] = f"{blob}{'x' if os.access(top / path, os.X_OK) else ''}"
    return states


async def _untracked_now(cwd: Path) -> set[str] | None:
    """Every untracked path in the repository, or None when git cannot answer."""
    top = await _locate(cwd)
    if top is None:
        return None
    return await _untracked_paths(top)


async def snapshot_untracked(cwd: Path) -> set[str] | None:
    """The untracked paths just before a check runs, to hand to record_result() as ``pre_untracked``.

    A path the check itself creates (a coverage report, a build artifact) is not content the check
    read, and a later run rewrites it with fresh bytes. Only the paths that did not exist before
    the run, or that an earlier record already treats as outputs, are excluded: a path that was
    already there is never taken out of the key on the strength of its content changing. Returns
    None when git cannot answer, which only disables that exclusion.
    """
    return await _untracked_now(cwd)


async def _changed_paths(top: Path, base: str) -> set[str] | None:
    """The paths that differ from *base* in commits, the index or the worktree, or None when git cannot answer."""
    changed = await run("git", "diff", "--name-only", "--no-renames", "-z", base, "--", ".", _CONTROL_EXCLUDE, cwd=top)
    if not changed.success:
        return None
    return {p for p in changed.stdout.split("\0") if p}


async def _tracked_fingerprint(cwd: Path) -> str | None:
    """Fingerprint of the merge base and every changed (not untracked) path, or None when git cannot answer.

    Ties a pending note to the moment it was written: an edit to tracked content since then
    means the note no longer describes what ran.
    """
    top = await _locate(cwd)
    base = await _diff_base(cwd)
    if top is None or base is None:
        return None
    changed = await _changed_paths(top, base)
    if changed is None:
        return None
    paths = sorted(changed)
    states = await _path_states(top, paths)
    if states is None:
        return None
    return "\n".join([base, *(f"{path}\x01{states[path]}" for path in paths)])


async def _content_fingerprint(cwd: Path, base: str, exclude_paths: frozenset[str] = frozenset()) -> str | None:
    """Fingerprint every file that differs from *base* or is untracked, by path and content hash.

    One path set, one rule: a path is hashed by what the file holds now (see _path_states()),
    whether git reports it as committed, staged, unstaged or untracked. Keeping those
    sources apart would make the key depend on git's bookkeeping: a new file
    moves from the untracked listing into the diff the moment CommitStep runs
    ``git add``, so the same bytes would hash differently on each side and a
    recorded pass would never be reused after a commit.

    Content, not just paths: an agent that creates a file and later edits it
    leaves the path listing identical while the file the check command reads
    has changed.

    The set covers the whole repository, not just *cwd*'s subtree: paths come from the top
    level (see _locate()). Every ``.claude/agent-control/`` directory is excluded because writing the
    marker there is not a worktree change: without the exclusion, recording a passing
    result would invalidate the very hash it was recorded under on any
    project that has not gitignored that directory.

    *exclude_paths* are untracked paths a recorded check wrote itself (see record_result());
    only a path that is still untracked is left out, so a rewrite of that output does not change the
    key, while one that has since been staged or committed counts as content again.
    """
    top = await _locate(cwd)
    if top is None:
        return None
    changed = await _changed_paths(top, base)
    untracked = await _untracked_paths(top)
    if changed is None or untracked is None:
        return None
    paths = sorted({*changed, *(untracked - exclude_paths)})
    states = await _path_states(top, paths)
    if states is None:
        return None
    return "\n".join(f"{path}\x01{states[path]}" for path in paths)


async def _compute_hash(cwd: Path, check_cmd: str, exclude_paths: frozenset[str] = frozenset()) -> str | None:
    """Hash the worktree's content and check_cmd, or None when git cannot report the state.

    Covers every change source DevelopStep.validate_output() already treats
    as "the worktree changed": committed, staged, unstaged and untracked, all
    through _content_fingerprint(). The merge base (_diff_base()) is part of
    the key, so a rebase onto a moved base invalidates reuse, but the position
    of origin/main itself never does.
    check_cmd is folded in too, so a mid-cycle change to the configured check
    command invalidates reuse instead of serving a stale result for a
    different command. *exclude_paths* leaves a recorded check's own outputs out.

    Returns None rather than folding a failed git call's empty output into
    the payload: a hash built from a partial answer can collide with another
    worktree's, so "could not determine the state" must never read as "the
    state matches" (AGENTS.md, "Failure must not look like success").
    """
    base = await _diff_base(cwd)
    if base is None:
        log.warning("check_cache.hash_unavailable", reason="no_head")
        return None
    fingerprint = await _content_fingerprint(cwd, base, exclude_paths)
    if fingerprint is None:
        log.warning("check_cache.hash_unavailable", reason="git_failed", base=base)
        return None
    payload = "\x00".join([base, fingerprint, check_cmd])
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


def _entries(marker: dict) -> list[dict]:
    """The latest record followed by the earlier ones the marker keeps (see EARLIER_RECORDS_KEPT).

    A marker that only carries seeded outputs (see seed_outputs()) holds no record yet.
    """
    earlier = marker.get("earlier")
    entries = [marker, *(e for e in earlier if isinstance(e, dict))] if isinstance(earlier, list) else [marker]
    return [e for e in entries if isinstance(e.get("hash"), str)]


def _seeded_of(marker: dict | None) -> frozenset[str]:
    raw = marker.get("seeded") if marker is not None else None
    return frozenset(p for p in raw if isinstance(p, str)) if isinstance(raw, list) else frozenset()


def is_pipeline_dispatch() -> bool:
    """Whether this process was spawned as a tool-using autonomous pipeline step.

    See PIPELINE_STAGE_ENV_KEY.
    """
    return os.environ.get(PIPELINE_STAGE_ENV_KEY, "").strip() == "1"


async def should_skip(cwd: Path, check_cmd: str) -> bool:
    """Return True when an identical check already passed earlier in this cycle.

    Always False outside an autonomous pipeline dispatch (see
    is_pipeline_dispatch()), and False for a failed or hash-mismatched
    marker: only a marker recorded as passed, for this exact worktree
    content and check command, is ever reused.

    A miss means the caller is about to run the check, so the untracked paths
    are noted first (see PENDING_FILENAME) for the record call chained after it.
    """
    if not is_pipeline_dispatch():
        return False
    # An older note describes a run that is over: never let it outlive this call.
    _pending_path(cwd).unlink(missing_ok=True)
    if await has_recorded_pass(cwd, check_cmd):
        return True
    await _write_pending(cwd, check_cmd)
    return False


async def has_recorded_pass(cwd: Path, check_cmd: str) -> bool:
    """Return True when a pass is recorded for this exact worktree content and check command.

    The content-hash lookup behind should_skip(), without its pipeline-dispatch
    gate: DevelopStep's own process never has PIPELINE_STAGE_ENV_KEY (only the
    agents it dispatches do), yet its inner loop re-runs the full check that a
    later call site, or its own earlier run, already recorded a pass for.

    Each record is compared on its own terms: the untracked outputs its check
    wrote (``excluded``) are left out of the current content too, so rewriting a
    coverage report after the run does not turn a hit into a miss.
    """
    marker = _read_marker(cwd)
    if marker is None:
        return False
    # The command text is part of every hash, so a record for another command can never match.
    passed = [
        entry for entry in _entries(marker) if entry.get("passed") is True and entry.get("check_cmd") == check_cmd
    ]
    hash_by_exclusion: dict[frozenset[str], str | None] = {}
    for entry in passed:
        excluded = _excluded_of(entry)
        if excluded not in hash_by_exclusion:
            hash_by_exclusion[excluded] = await _compute_hash(cwd, check_cmd, excluded)
        current = hash_by_exclusion[excluded]
        if current is not None and current == entry.get("hash"):
            return True
    return False


async def _write_pending(cwd: Path, check_cmd: str) -> None:
    """Note the untracked paths before a check runs. Advisory: a failure only costs a later hit.

    The note also carries a fingerprint of the tracked state (see _tracked_fingerprint()), so a record
    taken after that state changed (a failed check, then a code fix) does not trust it.
    """
    untracked = await _untracked_now(cwd)
    tracked = await _tracked_fingerprint(cwd)
    if untracked is None or tracked is None:
        _pending_path(cwd).unlink(missing_ok=True)
        return
    try:
        path = _pending_path(cwd)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"check_cmd": check_cmd, "tracked": tracked, "untracked": sorted(untracked)}), encoding="utf-8"
        )
    except OSError as exc:
        log.warning("check_cache.pending_failed", error=str(exc), check_cmd=check_cmd)


async def _take_pending(cwd: Path, check_cmd: str) -> set[str] | None:
    """Consume the pending note: the pre-check untracked state when it is still valid for *check_cmd*, else None.

    Single use, so a later record call without its own should_skip() never inherits
    an old note and treats a file an agent created since as a check output. Valid only while the
    tracked state is the one the note was written under: a note from a failed run, followed by a
    code fix, is discarded.
    """
    content = read_text_or_none(_pending_path(cwd))
    try:
        _pending_path(cwd).unlink(missing_ok=True)
    except OSError:
        return None
    if content is None:
        return None
    try:
        data = json.loads(content)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("check_cmd") != check_cmd or not isinstance(data.get("untracked"), list):
        return None
    tracked = data.get("tracked")
    if not isinstance(tracked, str) or tracked != await _tracked_fingerprint(cwd):
        return None
    return {p for p in data["untracked"] if isinstance(p, str)}


def _excluded_of(entry: dict) -> frozenset[str]:
    raw = entry.get("excluded")
    return frozenset(p for p in raw if isinstance(p, str)) if isinstance(raw, list) else frozenset()


async def record_result(
    cwd: Path,
    check_cmd: str,
    *,
    passed: bool,
    pre_untracked: set[str] | None = None,
    use_pending: bool = True,
) -> bool:
    """Persist the outcome of a check run, keyed to the worktree state that produced it.

    *pre_untracked* is the set of untracked paths from just before the check ran
    (snapshot_untracked(), or the note should_skip() left for the CLI call). A path
    untracked now that did not exist then was created by the check and is recorded as
    ``excluded``, together with the paths earlier records in the marker excluded and the
    paths seed_outputs() noted from the baseline run (an output of an earlier check that this
    one rewrites or leaves alone is still an output), so a later rewrite of it (a coverage
    report, a build stamp) does not change the key. A path that
    existed before the run is never excluded on the strength of its content changing: a
    formatter that rewrites a source file the agent created must not take it out of the key.
    An output that predates every record and was not seeded only costs a later reuse. Without a snapshot
    nothing is excluded, which only narrows reuse.

    *use_pending* is True only for the CLI call, which reads the note should_skip() left.
    An in-process caller passes False: when its own snapshot failed it means "exclude
    nothing", and must not consume a note left for another call.

    A new entry supersedes every earlier one for the same command whose content hash it
    matches, however that earlier entry's exclusions were chosen: the latest word on a
    command and content wins, so a later failure cannot be outvoted by an older pass.

    Returns whether the marker was written. Recording is advisory: an
    unhashable worktree or an unwritable marker path only costs a later call
    site its reuse, so neither is raised at the caller (``sova checks
    record`` runs chained after the real check command, and a non-zero exit
    there would report a passing check as a failing one).
    """
    if pre_untracked is None and use_pending:
        pre_untracked = await _take_pending(cwd, check_cmd)
    previous = _read_marker(cwd)
    earlier_entries = _entries(previous) if previous is not None else []
    excluded: frozenset[str] = frozenset()
    if pre_untracked is not None:
        now = await _untracked_now(cwd)
        if now is not None:
            known = frozenset().union(_seeded_of(previous), *(_excluded_of(e) for e in earlier_entries))
            carried = known & now
            created = frozenset(p for p in now if p not in pre_untracked)
            excluded = carried | created
    state_hash = await _compute_hash(cwd, check_cmd, excluded)
    if state_hash is None:
        log.warning("check_cache.record_skipped", reason="hash_unavailable", check_cmd=check_cmd)
        return False
    entry = {
        "hash": state_hash,
        "passed": passed,
        "check_cmd": check_cmd,
        "excluded": sorted(excluded),
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    current_by_exclusion: dict[frozenset[str], str | None] = {}
    earlier: list[dict] = []
    for e in earlier_entries:
        if e.get("hash") == state_hash:
            continue
        if e.get("check_cmd") == check_cmd:
            own = _excluded_of(e)
            if own not in current_by_exclusion:
                current_by_exclusion[own] = await _compute_hash(cwd, check_cmd, own)
            if current_by_exclusion[own] is not None and current_by_exclusion[own] == e.get("hash"):
                continue
        earlier.append({key: e[key] for key in entry if key in e})
    payload = {**entry, "earlier": earlier[:EARLIER_RECORDS_KEPT]}
    seeded = _seeded_of(previous)
    if seeded:
        payload["seeded"] = sorted(seeded)
    marker_path = _marker_path(cwd)
    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("check_cache.record_failed", error=str(exc), check_cmd=check_cmd)
        return False
    log.info("check_cache.recorded", passed=passed, check_cmd=check_cmd)
    return True


async def seed_outputs(cwd: Path, pre_untracked: set[str] | None) -> bool:
    """Note the untracked paths a run created since *pre_untracked*, as outputs every later record carries.

    CaptureBaselineStep runs the project's test suite before any record exists. What it leaves
    untracked (a coverage report, a build stamp) is output the later checks rewrite with fresh
    bytes, so without this a path that predates the first record would sit in every key and
    each check run would change it. Advisory: returns whether anything was written, and a failure
    only costs a later reuse.
    """
    if pre_untracked is None:
        return False
    now = await _untracked_now(cwd)
    if now is None:
        return False
    created = sorted(now - pre_untracked)
    if not created:
        return False
    previous = _read_marker(cwd) or {}
    payload = {**previous, "seeded": sorted(_seeded_of(previous) | set(created))}
    try:
        _marker_path(cwd).parent.mkdir(parents=True, exist_ok=True)
        _marker_path(cwd).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("check_cache.seed_failed", error=str(exc))
        return False
    return True


def clear_marker(cwd: Path) -> None:
    """Remove a worktree's marker and pending note so a worktree reused across cycles never inherits a stale one.

    Non-fatal: both call sites are pipeline steps whose own job (creating the
    worktree, capturing the baseline) must not fail because an advisory
    marker could not be deleted. A surviving marker still has to match the
    content hash before it is ever reused.
    """
    try:
        _marker_path(cwd).unlink(missing_ok=True)
        _pending_path(cwd).unlink(missing_ok=True)
    except OSError as exc:
        log.warning("check_cache.clear_failed", error=str(exc), cwd=str(cwd))
