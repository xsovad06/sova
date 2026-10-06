"""Claude Code's RuntimeAdapter: the default, and the only one with a real commands_dir."""

from __future__ import annotations

from pathlib import Path

from sova.agents.base import RuntimeAdapter


class ClaudeCodeAdapter(RuntimeAdapter):
    """Renders canonical commands/skills into Claude Code's ``.claude/`` tree."""

    @property
    def name(self) -> str:
        return "claude-code"

    def commands_dir(self, project_dir: Path) -> Path | None:
        return project_dir / ".claude" / "commands"

    def skills_dir(self, project_dir: Path) -> Path | None:
        return project_dir / ".claude" / "skills"
