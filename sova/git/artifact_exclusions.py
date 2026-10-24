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
RUNTIME_ARTIFACT_PREFIXES = frozenset({".claude/", ".sova/"}) | artifact_exclusion_prefixes()

IGNORABLE_UNTRACKED_RE = re.compile("|".join(rf"^{re.escape(prefix)}" for prefix in sorted(RUNTIME_ARTIFACT_PREFIXES)))


def pathspec_exclude(prefix: str) -> str:
    """Build a git pathspec excluding *prefix*.

    A bare name-prefix (no trailing ``/``, e.g. ``.agents/skills/sova-``) needs
    a trailing ``*`` to exclude everything under matching directories: git's
    default (non-literal) pathspec wildcard matching already treats ``*`` as
    matching any characters including ``/``, so no explicit ``:(glob)`` magic
    is required.
    """
    if prefix.endswith("/"):
        return f":(exclude){prefix}"
    return f":(exclude){prefix}*"


# Pathspecs excluded from "did the agent leave work uncommitted?" checks.
# IGNORABLE_UNTRACKED_RE above already applies the same rule to untracked
# files.
EXCLUDED_PATHSPECS = tuple(pathspec_exclude(prefix) for prefix in sorted(RUNTIME_ARTIFACT_PREFIXES))
