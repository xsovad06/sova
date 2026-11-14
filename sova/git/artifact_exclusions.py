"""Shared git pathspec/regex helpers for excluding mirrored agent infrastructure.

Leaf module (only depends on ``sova.agents.registry``) so every step and git
helper that needs to tell "the agent's own work" apart from inherited
``.claude/``/``.sova/``/runtime-skills mirror churn shares one definition
instead of drifting. Hoisted out of three near-identical copies in
``sova/core/steps/develop.py``, ``sova/core/steps/rearrange_commits.py`` and
``sova/git/worktree.py`` (issue #1090 review), each of which independently
derived the same prefixes from ``artifact_exclusion_prefixes()``.
"""

from __future__ import annotations

import re

from sova.agents.registry import artifact_exclusion_prefixes

# `.claude/` and `.sova/` hold agent infrastructure the pipeline itself mirrors
# into every worktree (ensure_claude_artifacts); `artifact_exclusion_prefixes()`
# adds every registered RuntimeAdapter's own mirrored skills directory on top
# (e.g. Codex's `.agents/skills/`). None of it is the agent's own work, so
# churn there must never count as "uncommitted changes" or "untracked work".
# SOVA's per-project state dir (the test baseline lives here).
SOVA_STATE_DIR = ".sova/"

RUNTIME_ARTIFACT_PREFIXES = frozenset({".claude/", SOVA_STATE_DIR}) | artifact_exclusion_prefixes()

IGNORABLE_UNTRACKED_RE = re.compile("|".join(rf"^{re.escape(prefix)}" for prefix in sorted(RUNTIME_ARTIFACT_PREFIXES)))


def pathspec_include(prefix: str) -> str:
    """Build a git pathspec matching *prefix*.

    A bare name-prefix (no trailing ``/``, e.g. ``.agents/skills/sova-``) needs
    a trailing ``*`` to match everything under matching directories: git's
    default (non-literal) pathspec wildcard matching already treats ``*`` as
    matching any characters including ``/``, so no explicit ``:(glob)`` magic
    is required.
    """
    return prefix if prefix.endswith("/") else f"{prefix}*"


def pathspec_exclude(prefix: str) -> str:
    """Build a git pathspec excluding *prefix* (see ``pathspec_include``)."""
    return f":(exclude){pathspec_include(prefix)}"


# Pathspecs excluded from "did the agent leave work uncommitted?" checks.
# IGNORABLE_UNTRACKED_RE above already applies the same rule to untracked
# files.
EXCLUDED_PATHSPECS = tuple(pathspec_exclude(prefix) for prefix in sorted(RUNTIME_ARTIFACT_PREFIXES))


# SOVA's own runtime files: the database (and its -wal/-shm/-journal siblings), its log, the
# agent-control/output dirs, worktrees and the `.sova/` baseline dir. Git-ignored in SOVA's own
# repository; `sova install` writes the ignorable ones into `.git/info/exclude` for other projects.
# Prefixes without a trailing `/` are name prefixes (`.claude/sova.db` covers `sova.db-wal` too).
RUNTIME_STATE_PATHS = (
    SOVA_STATE_DIR,
    ".claude/sova.db",
    ".claude/sova.log",
    ".claude/agent-control/",
    ".claude/agent-output/",
    ".claude/worktrees/",
    ".claude/mcp_secret",
    # Migration 044's one-time dump of the removed memory tables: free-text agent output that must never be
    # committed or pushed from a target project (a name prefix, so it covers `memories-export-<timestamp>.json`).
    ".claude/memories-export-",
)

# Only the runtime state stays put while `sync_branch()` stashes the operator's work. `.claude/specs/` is
# stashed with the rest: an untracked spec that origin has since merged would otherwise make the checkout
# and reset abort with "untracked working tree file would be overwritten".
STASH_EXCLUDED_PATHS = RUNTIME_STATE_PATHS


def exclude_pathspecs(
    excluded: tuple[str, ...],
    ignored: frozenset[str] = frozenset(),
    keep: tuple[str, ...] = (),
    extra: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """The pathspecs for a whole-tree ``git add -A`` or ``git stash --include-untracked`` that skips *excluded*.

    *ignored* holds the paths git reported as ignored for these pathspecs (``untracked_collisions``' probe): an
    exclude whose literal part is one of them, or lies under one, is dropped, since git refuses the command for it.
    *keep* names files a dropped exclude still has to leave out; each is excluded by its literal path.
    A dropped name-prefix exclude whose directory is not itself ignored (``.claude/sova.db`` ignored, ``.claude/``
    not) also gets a ``<prefix>-*`` glob exclude, which git accepts and which covers a sidecar (``sova.db-wal``)
    created after *keep* was listed. *extra* is appended as is (pathspecs with no literal part, which git never
    refuses, such as ``:(exclude,glob)**/.env``).

    ``:/`` (repo root) rather than ``.``: a bare ``.`` would narrow the command to the cwd subtree.
    """
    return (
        ":/",
        *(pathspec_exclude(prefix) for prefix in excluded if not is_under_any(prefix.rstrip("/"), ignored)),
        *(f":(exclude,glob){prefix}-*" for prefix in excluded if _needs_sidecar_exclude(prefix, ignored)),
        *(f":(exclude,literal){path}" for path in keep),
        *extra,
    )


def _needs_sidecar_exclude(prefix: str, ignored: frozenset[str]) -> bool:
    """Whether a dropped name-prefix exclude still needs a ``<prefix>-*`` exclude for its not-ignored sidecars."""
    if prefix.endswith("/") or not is_under_any(prefix, ignored):
        return False
    parent = prefix.rpartition("/")[0]
    return not (parent and is_under_any(parent, ignored))


def is_under_any(path: str, dirs: frozenset[str]) -> bool:
    """Whether *path* is one of *dirs* or lies under one of them."""
    return any(path == d or path.startswith(f"{d}/") for d in dirs)


def unstage_command(paths: tuple[str, ...]) -> tuple[str, ...]:
    """The ``git reset`` argv that drops *paths* from the index and leaves their contents on disk.

    Run before a pathspec stash that excludes *paths*: the stash still records the whole index, so a
    staged excluded file lands in the stash's index tree, and the later ``reset --keep`` turns it into an
    untracked file that makes ``git stash pop`` abort, stranding the operator's work in the stash. Run after
    a ``git add -A`` that excludes *paths* (``stage_all_except()``), to drop a tracked file under an ignored
    directory, which no exclude can keep out.
    """
    return ("git", "reset", "-q", "--", *(pathspec_include(prefix) for prefix in paths))


# The address-review rebase stash runs in a worktree and leaves only the runtime state in place; `.claude/specs/` is
# stashed with the rest, since a tracked spec with uncommitted edits left behind would make `git rebase` refuse to
# run. A name of its own (not branch.commit()'s RUNTIME_STATE_PATHS argument), so a later change to what
# `git add -A` skips does not silently change what the rebase stash moves.
WORKTREE_STASH_EXCLUDED_PATHS = RUNTIME_STATE_PATHS

# Lines `sova install` appends to `.git/info/exclude` (gitignore syntax, so the name prefixes become globs).
# Anchored with a leading `/` so they match only the project's own paths, like the pathspecs above, and not a
# same-named directory deeper in the tree (`vendor/x/.sova/`).
INFO_EXCLUDE_ENTRIES = tuple(f"/{pathspec_include(p)}" for p in RUNTIME_STATE_PATHS)
