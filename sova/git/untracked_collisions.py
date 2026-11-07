"""Clear untracked files that a branch switch or reset to ``ref`` would refuse to overwrite.

A stash that leaves SOVA's runtime files in place (``sync_branch``, the address-review rebase, the dashboard's
branch-freeing stash) cannot move an untracked file out of git's way, and ``git checkout`` / ``reset --keep`` /
``rebase`` abort with "untracked working tree file would be overwritten" when the target ref tracks the same path.
Two cases reach that state:

* A spec: SpecStep writes it untracked into the primary checkout and the PR commits it from a worktree, so once the
  PR merges it is untracked locally and tracked upstream. Stashing the local copy makes ``git stash pop`` abort
  ("already exists, no checkout") and strands a stash entry, so the copy is cleared before stashing instead.
* A runtime file that a PR leaked into history (``.sova/test-baseline.json``).

``runtime_stash_pathspecs()`` builds those stashes' pathspecs, dropping the excludes git would refuse.

The collision helpers are best effort on git failures (the caller's own git command then reports the real error), and
raise only where an operator has to decide something. ``runtime_stash_pathspecs()`` is the exception: it raises when
its probe fails, since a stash run on guessed pathspecs can write an entry and then fail.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from sova.git.artifact_exclusions import (
    RUNTIME_STATE_PATHS,
    SOVA_STATE_DIR,
    is_under_any,
    pathspec_include,
    stash_pathspecs,
)
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="git.untracked_collisions")

SPECS_DIR = ".claude/specs/"
# Where a local spec that differs from the upstream copy is moved: under the runtime state dir, so it is neither
# stashed nor committed, and nothing that globs `.claude/specs/` mistakes it for a live spec.
DISPLACED_SPECS_DIR = f"{SOVA_STATE_DIR}displaced-specs/"


async def _untracked(paths: tuple[str, ...], cwd: Path | None, *, include_ignored: bool) -> list[str] | None:
    """Untracked paths under *paths* (relative to *cwd*), or ``None`` when git failed."""
    args = ["git", "ls-files", "-z", "--others"]
    if not include_ignored:
        args.append("--exclude-standard")
    result = await run(*args, "--", *paths, cwd=cwd)
    if not result.success:
        log.warning("git.untracked_collisions.scan_failed", stderr=(result.stderr or "")[:200])
        return None
    return [p for p in result.stdout.split("\0") if p]


async def _tracked_blobs(ref: str, dirs: tuple[str, ...], cwd: Path | None) -> dict[str, str] | None:
    """Map each path under the directories *dirs* that *ref* tracks to its blob id, or ``None`` when git failed.

    ``git ls-tree`` pathspecs are literal prefixes (no wildcards), so callers filter the result themselves.
    """
    result = await run("git", "ls-tree", "-r", "-z", ref, "--", *dirs, cwd=cwd)
    if not result.success:
        log.warning("git.untracked_collisions.ls_tree_failed", ref=ref, stderr=(result.stderr or "")[:200])
        return None
    blobs: dict[str, str] = {}
    for entry in filter(None, result.stdout.split("\0")):
        meta, _, path = entry.partition("\t")
        blobs[path] = meta.split()[2] if len(meta.split()) >= 3 else ""
    return blobs


async def _hash_files(paths: list[str], cwd: Path | None) -> list[str] | None:
    """Blob ids of *paths* from one ``git hash-object`` call, in order, or ``None`` when git failed."""
    result = await run("git", "hash-object", "--", *paths, cwd=cwd)
    hashes = result.stdout.split()
    if not result.success or len(hashes) != len(paths):
        log.warning("git.untracked_collisions.hash_failed", stderr=(result.stderr or "")[:200])
        return None
    return hashes


def _displaced_name(rel: str, blob: str) -> str:
    """A destination file name for the local copy of *rel*, unique per path and content."""
    path = Path(rel)
    digest = hashlib.sha1(rel.encode("utf-8"), usedforsecurity=False).hexdigest()[:6]
    return f"{path.stem}.{digest}.local-{blob[:8]}{path.suffix}"


async def set_aside_conflicting_specs(ref: str, cwd: Path | None = None) -> list[str]:
    """Clear each untracked ``.claude/specs/`` file that *ref* tracks, so a later stash and pop cannot collide.

    A copy byte-identical to upstream is deleted (the checkout restores it). A copy whose content differs, which
    is the usual case for a reviewed spec because the reviewer appends a "Review Rationale" section in the
    worktree, is moved to ``.sova/displaced-specs/`` so the operator's text stays recoverable. Returns the
    destination paths of the moved copies (relative to *cwd*).
    """
    local_paths = await _untracked((SPECS_DIR,), cwd, include_ignored=False)
    if not local_paths:
        return []
    upstream = await _tracked_blobs(ref, (SPECS_DIR,), cwd)
    if not upstream:
        return []
    colliding = [rel for rel in local_paths if rel in upstream]
    if not colliding:
        return []
    local_blobs = await _hash_files(colliding, cwd)
    if local_blobs is None:
        return []

    base = Path(cwd) if cwd is not None else Path.cwd()
    moved: list[str] = []
    for rel, blob in zip(colliding, local_blobs, strict=True):
        if blob == upstream[rel]:
            (base / rel).unlink(missing_ok=True)
            log.info("git.untracked_collisions.spec_already_upstream", path=rel)
            continue
        dest = base / DISPLACED_SPECS_DIR / _displaced_name(rel, blob)
        dest.parent.mkdir(parents=True, exist_ok=True)
        (base / rel).replace(dest)
        moved.append(str(dest.relative_to(base)))
        log.warning("git.untracked_collisions.spec_displaced", path=rel, moved_to=moved[-1], ref=ref)
    return moved


async def clear_conflicting_runtime_files(ref: str, cwd: Path | None = None) -> list[str]:
    """Delete untracked ``.sova/`` files that *ref* tracks, and raise for any other untracked runtime path it tracks.

    A ``.sova/`` file (the test baseline) is recomputed on the next run, so deleting it is safe. Any other
    runtime path (the live database, the log, ``mcp_secret``) is never overwritten or deleted here: the error
    names it so the operator can decide. Returns the deleted paths.
    """
    runtime_specs = tuple(pathspec_include(prefix) for prefix in RUNTIME_STATE_PATHS)
    local_paths = await _untracked(runtime_specs, cwd, include_ignored=True)
    if not local_paths:
        return []
    upstream = await _tracked_blobs(ref, (".claude/", SOVA_STATE_DIR), cwd)
    if not upstream:
        return []
    colliding = [rel for rel in local_paths if rel in upstream]

    protected = [rel for rel in colliding if not rel.startswith(SOVA_STATE_DIR)]
    if protected:
        raise RuntimeError(
            f"Cannot switch to {ref}: it tracks SOVA runtime file(s) that exist locally as untracked files: "
            f"{', '.join(protected)}. SOVA will not overwrite them. Move or remove them (back up the database "
            "first if you need it), then retry."
        )

    base = Path(cwd) if cwd is not None else Path.cwd()
    for rel in colliding:
        (base / rel).unlink(missing_ok=True)
        log.warning("git.untracked_collisions.runtime_file_removed", path=rel, ref=ref)
    return colliding


_IGNORED_HEADER = "The following paths are ignored"


def _reported_ignored_paths(stderr: str) -> frozenset[str]:
    """The paths ``git add`` lists under its "The following paths are ignored" header (``LC_ALL=C`` output)."""
    lines = stderr.splitlines()
    start = next((i + 1 for i, line in enumerate(lines) if line.startswith(_IGNORED_HEADER)), len(lines))
    paths: set[str] = set()
    for line in lines[start:]:
        if not line.strip() or line.startswith("hint:"):
            break
        paths.add(line.strip())
    return frozenset(paths)


async def runtime_stash_pathspecs(
    excluded: tuple[str, ...], cwd: Path | None = None, *, timeout: float = 300
) -> tuple[str, ...]:
    """Pathspecs for a ``git stash --include-untracked`` that leaves the runtime path prefixes *excluded* in place.

    A pathspec stash re-adds its paths with ``git add``, which exits 1 with "The following paths are ignored" when
    an exclude's literal part, or a directory above it, is an ignored path, after the stash entry is already
    written. ``sova install`` ignores every runtime path, so the full excludes fail whenever there is work to stash.
    ``git add --dry-run`` with the same pathspecs fails the same way without side effects, and its list decides
    which excludes to drop: the stash skips ignored files on its own. A dropped name-prefix exclude can still cover
    files that are not ignored (``.claude/sova.db-wal`` when only ``.claude/sova.db`` is), and those are excluded
    one by one. A tracked file under an ignored directory cannot be excluded at all: the ``.sova/`` baseline is stashed
    and restored with the operator's work, and any other modified one is refused (see below).

    Raises ``RuntimeError`` before anything is stashed when the stash cannot be built safely: the probe or the file
    scan failed (a timeout included), or a modified tracked runtime file (the live database, the log) lies under an
    ignored path, where no exclude can keep it out of the stash. A probe that fails without naming an ignored path
    is not "nothing ignored": running the stash on the full excludes could write a stash entry and then fail.
    *timeout* bounds each git call, so a caller on a request path can hold the probe to its own budget.
    """
    pathspecs = stash_pathspecs(excluded)
    probe = await run(
        "git", "add", "--dry-run", "--", *pathspecs, cwd=cwd, timeout=timeout, env={**os.environ, "LC_ALL": "C"}
    )
    if probe.success:
        return pathspecs
    ignored = _reported_ignored_paths(probe.stderr or "")
    if not ignored:
        raise RuntimeError(f"Cannot build the stash pathspecs: the git add probe failed: {(probe.stderr or '')[:300]}")
    includes = tuple(pathspec_include(prefix) for prefix in excluded if is_under_any(prefix.rstrip("/"), ignored))
    listed = await run(
        "git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *includes, cwd=cwd, timeout=timeout
    )
    if not listed.success:
        raise RuntimeError(
            f"Cannot build the stash pathspecs: the git ls-files scan failed: {(listed.stderr or '')[:300]}"
        )
    keep = tuple(rel for rel in listed.stdout.split("\0") if rel and not is_under_any(rel, ignored))
    await _refuse_modified_tracked_runtime_files(includes, ignored, cwd, timeout)
    return stash_pathspecs(excluded, ignored, keep)


async def _refuse_modified_tracked_runtime_files(
    includes: tuple[str, ...], ignored: frozenset[str], cwd: Path | None, timeout: float
) -> None:
    """Raise when a modified tracked runtime file lies under an ignored path, so the stash would move it.

    Git refuses an exclude for such a file, so the stash would replace the live database or log under the running
    server. Staged-only changes count too: the callers unstage first, but a failed unstage must not let one through.
    The ``.sova/`` baseline is recomputed on the next run and is stashed and restored like operator work.
    """
    changed: list[str] = []
    for scan in (("ls-files", "-z", "--modified"), ("diff", "-z", "--name-only", "--cached")):
        result = await run("git", *scan, "--", *includes, cwd=cwd, timeout=timeout)
        if not result.success:
            raise RuntimeError(
                f"Cannot build the stash pathspecs: the git {scan[0]} scan failed: {(result.stderr or '')[:300]}"
            )
        changed.extend(result.stdout.split("\0"))
    protected = sorted(
        {rel for rel in changed if rel and is_under_any(rel, ignored) and not rel.startswith(SOVA_STATE_DIR)}
    )
    if protected:
        raise RuntimeError(
            f"Cannot stash: SOVA runtime file(s) are tracked and modified, and git cannot leave them in place: "
            f"{', '.join(protected)}. SOVA will not stash them. Commit or restore them (back up the database first "
            "if you need it), then retry."
        )
