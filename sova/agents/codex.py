"""Codex's RuntimeAdapter: skills only, no slash-command directory."""

from __future__ import annotations

from pathlib import Path

from sova.agents.base import RuntimeAdapter


class CodexAdapter(RuntimeAdapter):
    """Renders canonical skills into Codex's own ``.codex/`` tree.

    Codex has no slash-command concept, so ``commands_dir()`` returns
    ``None``: workflow commands reach Codex through the provider-neutral
    ``AGENTS.md`` instruction body it already discovers by convention (see
    ``_HEADLESS_PREAMBLE_CODEX`` in ``sova/ipc/runtime.py`` for the matching
    spawn-time preamble), not as a directory of per-command files.

    ``.codex/skills/`` is deliberately its own directory rather than
    ``.agents/skills/``: this repo's ``.agents/skills/`` already holds
    hand-authored, independently-maintained skill content (predating this
    adapter) that happens to share filenames with canonical skills (e.g.
    ``testing-patterns``), so installing SOVA-managed skills there risks a
    ``--force`` sync destroying committed, unmanaged content. A target that
    cannot collide with any pre-existing source tree is the safer choice.
    """

    @property
    def name(self) -> str:
        return "codex"

    def commands_dir(self, project_dir: Path) -> Path | None:
        return None

    def skills_dir(self, project_dir: Path) -> Path | None:
        return project_dir / ".codex" / "skills"
