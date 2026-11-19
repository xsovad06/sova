"""Resolve the checkout that owns a directory's configuration.

A linked git worktree carries no config of its own: ``.claude/sova.db`` is gitignored and ``sova.toml`` is
frequently untracked. Both the LLM client and ``load_config()`` resolve a worktree to its primary checkout through
this one helper, so the settings database and the Keychain entry scoped to it always name the same directory.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=256)
def resolve_config_root(start: Path) -> Path:
    """Walk *start* up to the checkout that owns its config.

    Cached because config is loaded once per LLM call and ``_resolve_primary_root``
    shells out to git. The cache is bounded because worktree paths are ephemeral
    (one per issue, removed on cleanup), so an unbounded map would only ever grow.
    Process-local, so a config source created after the first lookup is not
    picked up until restart.

    A directory that holds its own config source is used as-is, so a project
    installed inside a monorepo subtree keeps resolving to itself. Only a
    directory with neither source resolves upward, which is the worktree case.
    """
    if (start / "sova.toml").exists() or (start / ".claude" / "sova.db").exists():
        return start

    from sova.llm.provider import _resolve_primary_root

    return _resolve_primary_root(start) or start


def owning_config_dir(project_dir: Path) -> Path:
    """The directory whose config *project_dir* uses: itself, or the primary checkout for a linked worktree.

    A linked worktree has a ``.git`` file (a normal checkout has a directory), so only that case pays for the
    config-source probe and git lookup; every other directory is returned untouched.
    """
    project_dir = project_dir.resolve()
    if (project_dir / ".git").is_file():
        return resolve_config_root(project_dir)
    return project_dir
