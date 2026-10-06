"""Tests for the sova/agents/ RuntimeAdapter abstraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from sova.agents.base import RuntimeAdapter
from sova.agents.claude_code import ClaudeCodeAdapter
from sova.agents.codex import CodexAdapter
from sova.agents.registry import create_runtime_adapter
from sova.agents.sync import sync_runtime_skills, warn_orphaned_runtime_artifacts
from sova.commands.catalog import CommandEntry
from sova.commands.manifest import MANIFEST_FILENAME
from sova.config.models import ProjectConfig


class TestClaudeCodeAdapter:
    def test_name(self) -> None:
        assert ClaudeCodeAdapter().name == "claude-code"

    def test_commands_dir(self, tmp_path: Path) -> None:
        assert ClaudeCodeAdapter().commands_dir(tmp_path) == tmp_path / ".claude" / "commands"

    def test_skills_dir(self, tmp_path: Path) -> None:
        assert ClaudeCodeAdapter().skills_dir(tmp_path) == tmp_path / ".claude" / "skills"


class TestCodexAdapter:
    def test_name(self) -> None:
        assert CodexAdapter().name == "codex"

    def test_commands_dir_is_none(self, tmp_path: Path) -> None:
        """Codex has no slash-command concept: commands fold into AGENTS.md instead."""
        assert CodexAdapter().commands_dir(tmp_path) is None

    def test_skills_dir(self, tmp_path: Path) -> None:
        assert CodexAdapter().skills_dir(tmp_path) == tmp_path / ".codex" / "skills"


class TestCreateRuntimeAdapter:
    def test_claude_code(self) -> None:
        assert isinstance(create_runtime_adapter("claude-code"), ClaudeCodeAdapter)

    def test_codex(self) -> None:
        assert isinstance(create_runtime_adapter("codex"), CodexAdapter)

    def test_unknown_runtime_falls_back_to_claude_code(self) -> None:
        """An unrecognized value must never raise: it falls back silently."""
        assert isinstance(create_runtime_adapter("some-future-runtime"), ClaudeCodeAdapter)

    def test_aider_falls_back_to_claude_code(self) -> None:
        """Aider has no adapter yet (documented gap); it must not error or lose artifacts."""
        assert isinstance(create_runtime_adapter("aider"), ClaudeCodeAdapter)

    def test_empty_string_falls_back_to_claude_code(self) -> None:
        assert isinstance(create_runtime_adapter(""), ClaudeCodeAdapter)


class TestSupportsCommand:
    def _entry(self, runtimes: list[str]) -> CommandEntry:
        return CommandEntry(
            name="x",
            description="d",
            category="core",
            user_invocable=True,
            path=Path("x.md"),
            runtimes=runtimes,
        )

    def test_unrestricted_command_supported_by_every_adapter(self) -> None:
        entry = self._entry([])
        assert ClaudeCodeAdapter().supports_command(entry) is True
        assert CodexAdapter().supports_command(entry) is True

    def test_restricted_command_supported_only_by_named_runtime(self) -> None:
        entry = self._entry(["claude-code"])
        assert ClaudeCodeAdapter().supports_command(entry) is True
        assert CodexAdapter().supports_command(entry) is False


class TestSyncRuntimeSkills:
    @pytest.fixture
    def skills_src_dir(self, tmp_path: Path) -> Path:
        src = tmp_path / "canonical-skills"
        skill_dir = src / "a-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: a-skill\n---\nBody.\n", encoding="utf-8")
        return src

    def test_claude_code_runtime_is_a_noop(self, tmp_path: Path, skills_src_dir: Path) -> None:
        """Claude's .claude/skills/ is already installed unconditionally by the caller."""
        cfg = ProjectConfig()
        assert cfg.agent.runtime == "claude-code"
        result = sync_runtime_skills(skills_src_dir, tmp_path, cfg)
        assert result is None
        assert not (tmp_path / ".claude").exists()

    def test_codex_runtime_installs_into_codex_skills(self, tmp_path: Path, skills_src_dir: Path) -> None:
        cfg = ProjectConfig()
        cfg.agent.runtime = "codex"
        result = sync_runtime_skills(skills_src_dir, tmp_path, cfg)
        assert result is not None
        assert result.updated == 1
        assert result.conflicts == []
        assert (tmp_path / ".codex" / "skills" / "a-skill" / "SKILL.md").is_file()

    def test_codex_runtime_second_call_updates_not_duplicates(self, tmp_path: Path, skills_src_dir: Path) -> None:
        cfg = ProjectConfig()
        cfg.agent.runtime = "codex"
        sync_runtime_skills(skills_src_dir, tmp_path, cfg)
        result = sync_runtime_skills(skills_src_dir, tmp_path, cfg)
        assert result is not None
        assert result.updated == 0
        assert result.skipped == 1
        assert result.conflicts == []

    def test_codex_runtime_preserves_preexisting_unmanaged_skill(self, tmp_path: Path, skills_src_dir: Path) -> None:
        """A skill directory that already existed before SOVA ever managed it must not be clobbered."""
        target = tmp_path / ".codex" / "skills" / "a-skill"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("---\nname: a-skill\n---\nHand-authored, not canonical.\n", encoding="utf-8")

        cfg = ProjectConfig()
        cfg.agent.runtime = "codex"
        result = sync_runtime_skills(skills_src_dir, tmp_path, cfg)

        assert result is not None
        assert result.conflicts == ["a-skill/SKILL.md"]
        assert "Hand-authored, not canonical." in (target / "SKILL.md").read_text(encoding="utf-8")

    def test_codex_runtime_force_overwrites_preexisting_unmanaged_skill(
        self, tmp_path: Path, skills_src_dir: Path
    ) -> None:
        target = tmp_path / ".codex" / "skills" / "a-skill"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("---\nname: a-skill\n---\nHand-authored, not canonical.\n", encoding="utf-8")

        cfg = ProjectConfig()
        cfg.agent.runtime = "codex"
        result = sync_runtime_skills(skills_src_dir, tmp_path, cfg, force=True)

        assert result is not None
        assert result.conflicts == []
        assert result.updated == 1
        assert "Hand-authored, not canonical." not in (target / "SKILL.md").read_text(encoding="utf-8")


class TestWarnOrphanedRuntimeArtifacts:
    def test_no_warning_when_nothing_installed(self, tmp_path: Path) -> None:
        cfg = ProjectConfig()
        assert warn_orphaned_runtime_artifacts(tmp_path, cfg) == []

    def test_warns_when_switching_away_from_codex(self, tmp_path: Path) -> None:
        codex_skills = tmp_path / ".codex" / "skills"
        codex_skills.mkdir(parents=True)
        (codex_skills / MANIFEST_FILENAME).write_text("{}", encoding="utf-8")

        cfg = ProjectConfig()  # back to claude-code
        warnings = warn_orphaned_runtime_artifacts(tmp_path, cfg)
        assert len(warnings) == 1
        assert "codex" in warnings[0]
        assert str(codex_skills) in warnings[0]

    def test_no_warning_while_codex_still_configured(self, tmp_path: Path) -> None:
        codex_skills = tmp_path / ".codex" / "skills"
        codex_skills.mkdir(parents=True)
        (codex_skills / MANIFEST_FILENAME).write_text("{}", encoding="utf-8")

        cfg = ProjectConfig()
        cfg.agent.runtime = "codex"
        assert warn_orphaned_runtime_artifacts(tmp_path, cfg) == []

    def test_claude_code_directory_never_flagged_as_orphaned(self, tmp_path: Path) -> None:
        """Claude's .claude/skills/ is read by the interactive session too, not just agent.runtime."""
        claude_skills = tmp_path / ".claude" / "skills"
        claude_skills.mkdir(parents=True)
        (claude_skills / MANIFEST_FILENAME).write_text("{}", encoding="utf-8")

        cfg = ProjectConfig()
        cfg.agent.runtime = "codex"
        assert warn_orphaned_runtime_artifacts(tmp_path, cfg) == []


def test_runtime_adapter_is_abstract() -> None:
    with pytest.raises(TypeError):
        RuntimeAdapter()  # type: ignore[abstract]


def test_adapter_registry_keys_are_valid_agent_runtime_values() -> None:
    """Every ADAPTERS key must be a real agent.runtime value, or it's unreachable dead code.

    This is the inverse of the registry's own documented fallback (an
    unrecognized agent.runtime falls back to ClaudeCodeAdapter silently):
    nothing previously guarded the other direction, so a typo'd or stale key
    in ADAPTERS would sit there forever with no config value ever able to
    select it.
    """
    from typing import get_args

    from sova.agents.registry import ADAPTERS
    from sova.config.models import AgentConfig

    literal_values = set(get_args(AgentConfig.model_fields["runtime"].annotation))
    assert set(ADAPTERS) <= literal_values
