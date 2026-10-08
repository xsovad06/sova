"""Codex's RuntimeAdapter: skills only, no slash-command directory."""

from __future__ import annotations

from pathlib import Path

from sova.agents.base import RuntimeAdapter
from sova.commands.skill_render import SKILL_NAME_PREFIX, render_codex_skills


class CodexAdapter(RuntimeAdapter):
    """Renders canonical commands and skills into Codex's ``.agents/skills/`` tree.

    Codex has no slash-command concept, so ``commands_dir()`` returns
    ``None``: workflow commands reach Codex through the provider-neutral
    ``AGENTS.md`` instruction body it already discovers by convention (see
    ``_HEADLESS_PREAMBLE_CODEX`` in ``sova/ipc/runtime.py`` for the matching
    spawn-time preamble), as well as through a mechanically-rendered
    ``SKILL.md`` package per canonical command (``extra_skill_sources()``
    below), which is how Codex's own skill-discovery convention expects
    reusable workflows to be packaged.

    ``.agents/skills/`` is Codex's documented skill-discovery directory, and
    this repo's own copy already holds hand-authored, independently-maintained
    content under plain names (e.g. ``testing-patterns``) that predates this
    adapter. Rather than avoiding that directory entirely (as a prior version
    of this adapter did, targeting ``.codex/skills/`` instead), every
    command-derived entry here is written under ``sova-<name>`` via
    ``skill_name_prefix``, so it can never collide with that pre-existing
    content no matter what plain name it uses. A standalone, distributed
    skill from the shared ``skills/`` directory (e.g. ``design-taste``,
    ``issue-template``) is a different artifact class and installs under its
    own bare name instead: it keeps its namespace separate from
    ``sova-<name>`` on its own terms, and relies on
    ``materialize_combined_skill_sources()``'s existing-content check, not
    this prefix, if that bare name already collides with something already
    on disk (issue #1136).
    """

    @property
    def name(self) -> str:
        return "codex"

    def commands_dir(self, project_dir: Path) -> Path | None:
        return None

    def skills_dir(self, project_dir: Path) -> Path | None:
        return project_dir / ".agents" / "skills"

    @property
    def skill_name_prefix(self) -> str:
        return SKILL_NAME_PREFIX

    def extra_skill_sources(self, canonical_commands_dir: Path) -> dict[str, str]:
        return render_codex_skills(canonical_commands_dir, supports=self.supports_command)
