"""RuntimeAdapter abstraction: where a coding-agent runtime reads its artifacts from.

Mirrors ``sova.ipc.runtime.AgentRuntime``, which decides how to *spawn* a
coding agent process for a given backend. This is the installation-side
counterpart: it decides where that backend's workflow commands and skills
live on disk. The two are kept separate because a project's ``agent.runtime``
governs which backend SOVA spawns for autonomous pipeline agents, while the
artifact directories here are read by whatever tool (interactive or
autonomous) that runtime's own convention points at.

Rendering/copying the canonical ``commands/`` and ``skills/`` content into a
runtime's directory is delegated to ``sova.commands.distribution``, which is
already generic over ``(source_dir, target_dir)`` pairs: this module only
supplies the per-runtime target directories and restriction rules.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sova.commands.catalog import CommandEntry


class RuntimeAdapter(ABC):
    """Where a given coding-agent runtime expects rendered artifacts."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Runtime identifier, matching ``AgentConfig.runtime`` (e.g. 'claude-code', 'codex')."""
        ...

    @abstractmethod
    def commands_dir(self, project_dir: Path) -> Path | None:
        """Directory this runtime reads workflow commands from, or ``None``.

        ``None`` means this runtime has no slash-command concept of its own
        (e.g. Codex): its commands reach it through the provider-neutral
        instruction surface (``AGENTS.md``) instead of a directory of
        per-command files.
        """
        ...

    @abstractmethod
    def skills_dir(self, project_dir: Path) -> Path | None:
        """Directory this runtime reads reusable skills from, or ``None``."""
        ...

    @property
    def skill_name_prefix(self) -> str:
        """Prefix applied to every command-derived skill name this runtime installs.

        Empty by default. A runtime whose skills directory risks colliding
        with pre-existing, independently-maintained content under a plain
        name (e.g. Codex's ``.agents/skills/``) overrides this so every
        command-derived entry (``extra_skill_sources()``) is written under a
        name nothing else could already occupy. A standalone, distributed
        skill from the shared ``skills/`` directory is a different artifact
        class and always installs under its own bare name regardless of this
        prefix, instead relying on a per-directory existing-content check
        (``materialize_combined_skill_sources()``) the first time it would
        collide with something already there (issue #1136).
        """
        return ""

    def extra_skill_sources(self, canonical_commands_dir: Path) -> dict[str, str]:
        """Additional ``{skill_name: SKILL.md content}`` this runtime wants beyond the shared ``skills/`` directory.

        Empty by default. Overridden by a runtime that mechanically derives
        extra skills from something other than the hand-authored
        ``skills/`` directory (e.g. Codex renders every canonical command
        in ``canonical_commands_dir`` into its own skill; see
        ``CodexAdapter``).
        """
        return {}

    def supports_command(self, entry: CommandEntry) -> bool:
        """Whether a canonical command applies to this runtime.

        A command with no ``runtimes`` restriction in its frontmatter
        applies everywhere. One that names a restricted list only applies
        to a runtime whose name appears in it.
        """
        if not entry.runtimes:
            return True
        return self.name in entry.runtimes
