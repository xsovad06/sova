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
)

# Only the runtime state stays put while `sync_branch()` stashes the operator's work. `.claude/specs/` is
# stashed with the rest: an untracked spec that origin has since merged would otherwise make the checkout
# and reset abort with "untracked working tree file would be overwritten".
STASH_EXCLUDED_PATHS = RUNTIME_STATE_PATHS

# `:/` (repo root) rather than `.`: a bare `.` would narrow the stash to the cwd subtree.
STASH_PATHSPECS = (":/", *(pathspec_exclude(prefix) for prefix in STASH_EXCLUDED_PATHS))


def unstage_command(paths: tuple[str, ...]) -> tuple[str, ...]:
    """The ``git reset`` argv that drops *paths* from the index and leaves their contents on disk.

    Run before a pathspec stash that excludes *paths*: the stash still records the whole index, so a
    staged excluded file lands in the stash's index tree, and the later ``reset --keep`` turns it into an
    untracked file that makes ``git stash pop`` abort, stranding the operator's work in the stash.
    """
    return ("git", "reset", "-q", "--", *(pathspec_include(prefix) for prefix in paths))


# `git add -A` must never stage SOVA's runtime state (the live database can hold credentials, and
# `mcp_secret` is one), whether or not `sova install` wrote ignore rules for it. The rest of `.claude/`
# is staged as before.
STAGE_ALL_PATHSPECS = (":/", *(pathspec_exclude(prefix) for prefix in RUNTIME_STATE_PATHS))

# Lines `sova install` appends to `.git/info/exclude` (gitignore syntax, so the name prefixes become globs).
# Anchored with a leading `/` so they match only the project's own paths, like the pathspecs above, and not a
# same-named directory deeper in the tree (`vendor/x/.sova/`).
INFO_EXCLUDE_ENTRIES = tuple(f"/{pathspec_include(p)}" for p in RUNTIME_STATE_PATHS)
