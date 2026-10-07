"""RuntimeAdapter factory."""

from __future__ import annotations

from pathlib import Path

from sova.agents.base import RuntimeAdapter
from sova.agents.claude_code import ClaudeCodeAdapter
from sova.agents.codex import CodexAdapter
from sova.utils.logging import get_logger

log = get_logger(component="agents.registry")

ADAPTERS: dict[str, type[RuntimeAdapter]] = {
    "claude-code": ClaudeCodeAdapter,
    "codex": CodexAdapter,
}

# A path used only to read off the first path component an adapter resolves
# its directories under (e.g. ".agents" from ".agents/skills"). Never touched
# on disk.
_ROOT_PROBE = Path("/__sova_project_root__")


def artifact_exclusion_prefixes() -> frozenset[str]:
    """Relative directory prefixes safe to treat as mirrored agent infrastructure, never the agent's own work.

    Used to keep "did the agent leave work uncommitted?" gate checks (the
    untracked-file regex and committed-pathspec exclusions in
    ``sova/core/steps/rearrange_commits.py`` and ``sova/core/steps/develop.py``)
    in sync with whatever directories the adapters actually target, computed
    from the live registry rather than hand-listed, so a future adapter can't
    reintroduce the #1090 failure mode (agent infrastructure churn mistaken
    for the agent's own uncommitted work) for a new directory the gates don't
    yet know about.

    Returns a whole-directory prefix (always suffixed ``/``) per adapter
    directory, never a narrower name-prefix within it. Deliberately does NOT
    stop at an adapter's ``skill_name_prefix`` subtree: ``_mirror_runtime_skills()``
    (``sova/git/worktree.py``) copies an adapter's entire ``skills_dir()`` into
    every worktree, not just the SOVA-managed, prefixed entries inside it, so a
    narrower exclusion left hand-authored, unmanaged content sharing that
    directory (e.g. this repo's own ``.agents/skills/testing-patterns``) looking
    like the agent's own uncommitted work the moment it was mirrored into a
    worktree. ``skill_name_prefix`` still does its own, separate job wherever
    ``install_skills()``/``update_skills()`` write into a shared directory:
    avoiding a destructive overwrite of unmanaged content there. The two
    mechanisms solve different problems and are allowed to disagree on
    granularity.
    """
    prefixes: set[str] = set()
    for adapter_cls in ADAPTERS.values():
        adapter = adapter_cls()
        commands = adapter.commands_dir(_ROOT_PROBE)
        if commands is not None:
            prefixes.add(f"{commands.relative_to(_ROOT_PROBE).as_posix()}/")
        skills = adapter.skills_dir(_ROOT_PROBE)
        if skills is not None:
            prefixes.add(f"{skills.relative_to(_ROOT_PROBE).as_posix()}/")
    return frozenset(prefixes)


def create_runtime_adapter(runtime: str) -> RuntimeAdapter:
    """Resolve a ``RuntimeAdapter`` for an ``agent.runtime`` value.

    Unlike ``sova.ipc.runtime.create_runtime()``, this never raises: a value
    with no adapter of its own (unset, unrecognized, or "aider", which has
    no documented adapter path yet) falls back to ``ClaudeCodeAdapter`` so
    artifact installation keeps working exactly as it did before this
    abstraction existed.
    """
    cls = ADAPTERS.get(runtime)
    if cls is None:
        log.debug("agents.adapter_fallback", runtime=runtime)
        return ClaudeCodeAdapter()
    return cls()
