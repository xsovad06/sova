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
# its directories under (e.g. ".codex" from ".codex/skills"). Never touched
# on disk.
_ROOT_PROBE = Path("/__sova_project_root__")


def artifact_root_names() -> frozenset[str]:
    """Top-level directory names every registered runtime adapter writes artifacts under.

    Used to keep "did the agent leave work uncommitted?" gate checks (the
    untracked-file regex and committed-pathspec exclusions in
    ``sova/core/steps/rearrange_commits.py`` and ``sova/core/steps/develop.py``)
    in sync with whatever directories the adapters actually target, computed
    from the live registry rather than hand-listed, so a future adapter can't
    reintroduce the #1090 failure mode (agent infrastructure churn mistaken
    for the agent's own uncommitted work) for a new directory the gates don't
    yet know about.
    """
    names: set[str] = set()
    for adapter_cls in ADAPTERS.values():
        adapter = adapter_cls()
        for resolver in (adapter.commands_dir, adapter.skills_dir):
            target = resolver(_ROOT_PROBE)
            if target is not None:
                names.add(target.relative_to(_ROOT_PROBE).parts[0])
    return frozenset(names)


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
