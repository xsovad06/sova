"""Clear untracked files that a branch switch or reset to ``ref`` would refuse to overwrite.

A stash that leaves SOVA's runtime files in place (``sync_branch``, the address-review rebase, the dashboard's
branch-freeing stash) cannot move an untracked file out of git's way, and ``git checkout`` / ``reset --keep`` /
``rebase`` abort with "untracked working tree file would be overwritten" when the target ref tracks the same path.
Two cases reach that state:

* A spec: SpecStep writes it untracked into the primary checkout and the PR commits it from a worktree, so once the
  PR merges it is untracked locally and tracked upstream. Stashing the local copy makes ``git stash pop`` abort
  ("already exists, no checkout") and strands a stash entry, so the copy is cleared before stashing instead.
* A runtime file that a PR leaked into history (``.sova/test-baseline.json``).

Both helpers are best effort on git failures (the caller's own git command then reports the real error), and raise
only where an operator has to decide something.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from sova.git.artifact_exclusions import RUNTIME_STATE_PATHS, SOVA_STATE_DIR, pathspec_include
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
