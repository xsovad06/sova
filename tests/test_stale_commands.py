"""Stale managed-command detection (issue #1251).

The #1251 incident ran a squash recipe whose fix had shipped in SOVA but never
reached the project, because nothing noticed that the project's installed copy
of `/address-pr` predated it. A safety-critical command (one that rewrites,
pushes or merges a PR branch) whose installed copy is out of date now blocks
the run, at the supervisor's gate and at `start_command()`, instead of
silently executing the unfixed recipe. A sync that overwrites an installed
command logs it, so the overwrite is traceable.

Staleness is judged against the copy a run would actually execute
(`_resolve_command_prompt()` prefers `.claude/commands/<name>.md`); when no
copy is installed, the run receives the freshly rendered canonical recipe, so
there is nothing stale to block.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from sova.agents.claude_code import ClaudeCodeAdapter
from sova.commands.catalog import get_canonical_dir
from sova.commands.distribution import install_commands, update_commands
from sova.commands.manifest import ManifestEntry, file_hash, read_manifest, write_manifest
from sova.commands.staleness import SAFETY_CRITICAL_COMMANDS, find_stale_command
from sova.config.models import ProjectConfig


def _commands_dir(project: Path) -> Path:
    return project / ".claude" / "commands"


@pytest.fixture
def cfg() -> ProjectConfig:
    return ProjectConfig()


@pytest.fixture
def project(tmp_path: Path, cfg: ProjectConfig) -> Path:
    """A project with every canonical command freshly installed."""
    install_commands(get_canonical_dir(), _commands_dir(tmp_path), cfg, adapter=ClaudeCodeAdapter())
    return tmp_path


def _age_manifest_entry(project: Path, filename: str, old_content: str) -> None:
    """Make an installed command look synced from an older canonical version, with no local edits."""
    commands = _commands_dir(project)
    (commands / filename).write_text(old_content, encoding="utf-8")
    manifest = read_manifest(commands)
    assert manifest is not None
    manifest.commands[filename] = ManifestEntry(hash=file_hash(old_content), managed=True)
    write_manifest(commands, manifest)


class TestFindStaleCommand:
    def test_safety_critical_set_covers_every_history_rewriting_command(self) -> None:
        assert SAFETY_CRITICAL_COMMANDS == {"address-pr", "pr", "rearrange-commits", "review-full", "integrate-pr"}
        canonical = {p.stem for p in get_canonical_dir().glob("*.md")}
        assert canonical >= SAFETY_CRITICAL_COMMANDS, "a safety-critical command was renamed or removed"

    @pytest.mark.parametrize("command", sorted(SAFETY_CRITICAL_COMMANDS))
    def test_fresh_install_is_current(self, project: Path, cfg: ProjectConfig, command: str) -> None:
        assert find_stale_command(project, command, cfg) is None

    def test_copy_synced_from_older_canonical_is_stale(self, project: Path, cfg: ProjectConfig) -> None:
        _age_manifest_entry(project, "address-pr.md", "old recipe: git reset --soft origin/$BASE\n")

        reason = find_stale_command(project, "address-pr", cfg)

        assert reason is not None
        assert "/address-pr" in reason
        assert "sova commands update" in reason
        assert str(project) in reason

    def test_installed_copy_without_manifest_is_stale(self, project: Path, cfg: ProjectConfig) -> None:
        """No sync record means no evidence the project ever received the fixed recipe."""
        (_commands_dir(project) / ".sova-manifest.json").unlink()

        reason = find_stale_command(project, "integrate-pr", cfg)

        assert reason is not None
        assert "/integrate-pr" in reason

    def test_installed_copy_missing_from_manifest_is_stale(self, project: Path, cfg: ProjectConfig) -> None:
        commands = _commands_dir(project)
        manifest = read_manifest(commands)
        assert manifest is not None
        del manifest.commands["pr.md"]
        write_manifest(commands, manifest)

        assert find_stale_command(project, "pr", cfg) is not None

    def test_uninstalled_command_is_never_stale(self, tmp_path: Path, cfg: ProjectConfig) -> None:
        """Without an installed copy the run gets the freshly rendered canonical recipe."""
        assert find_stale_command(tmp_path, "address-pr", cfg) is None

    def test_local_customization_of_a_current_copy_is_not_stale(self, project: Path, cfg: ProjectConfig) -> None:
        """A local edit on top of the current canonical is the project's choice, not a missed fix."""
        target = _commands_dir(project) / "rearrange-commits.md"
        target.write_text(target.read_text(encoding="utf-8") + "\nProject-specific note.\n", encoding="utf-8")

        assert find_stale_command(project, "rearrange-commits", cfg) is None

    def test_non_safety_command_is_never_checked(self, project: Path, cfg: ProjectConfig) -> None:
        _age_manifest_entry(project, "standup.md", "old standup\n")

        assert find_stale_command(project, "standup", cfg) is None


class TestBranchCopyStaleness:
    """A project that commits `.claude/commands/` runs the worktree branch's own copy.

    `ensure_claude_artifacts()` never overwrites a tracked file, so a branch
    forked before a recipe fix keeps the old copy even when the primary
    checkout is current.
    """

    @pytest.fixture
    def worktree(self, tmp_path: Path, cfg: ProjectConfig) -> Path:
        wt = tmp_path / "worktree"
        install_commands(get_canonical_dir(), _commands_dir(wt), cfg, adapter=ClaudeCodeAdapter())
        return wt

    def test_outdated_branch_copy_is_stale_with_a_rebase_fix(
        self, project: Path, worktree: Path, cfg: ProjectConfig
    ) -> None:
        _age_manifest_entry(worktree, "address-pr.md", "old recipe: git reset --soft origin/$BASE\n")

        assert find_stale_command(project, "address-pr", cfg) is None
        reason = find_stale_command(project, "address-pr", cfg, run_dir=worktree)

        assert reason is not None
        assert str(worktree) in reason
        assert "Rebase the branch" in reason
        assert "sova commands update" not in reason

    def test_current_branch_copy_passes(self, project: Path, worktree: Path, cfg: ProjectConfig) -> None:
        assert find_stale_command(project, "integrate-pr", cfg, run_dir=worktree) is None

    def test_branch_copy_is_ignored_when_the_primary_checkout_has_none(
        self, tmp_path: Path, worktree: Path, cfg: ProjectConfig
    ) -> None:
        """Without a primary copy the prompt is inlined from SOVA, so the branch copy never runs."""
        primary = tmp_path / "primary"
        primary.mkdir()
        _age_manifest_entry(worktree, "pr.md", "old pr recipe\n")

        assert find_stale_command(primary, "pr", cfg, run_dir=worktree) is None

    def test_run_dir_equal_to_project_dir_keeps_the_sync_fix(self, project: Path, cfg: ProjectConfig) -> None:
        _age_manifest_entry(project, "address-pr.md", "old recipe\n")

        reason = find_stale_command(project, "address-pr", cfg, run_dir=project)

        assert reason is not None
        assert "sova commands update" in reason

    def test_config_is_loaded_from_the_project_not_the_worktree(
        self, project: Path, worktree: Path, cfg: ProjectConfig
    ) -> None:
        """A worktree has no sova.db; loading config there would render with defaults."""
        with patch("sova.config.loader.load_config", return_value=cfg) as mock_load:
            find_stale_command(project, "address-pr", run_dir=worktree)

        mock_load.assert_called_once_with(project)


class TestStaleCommandsGate:
    def test_stale_command_blocks(self, project: Path, cfg: ProjectConfig) -> None:
        from sova.supervisor.gates.stale_commands import check_stale_commands_gate

        _age_manifest_entry(project, "address-pr.md", "old recipe\n")

        block = check_stale_commands_gate(project, "address-pr", cfg)

        assert block is not None
        assert block.gate == "stale_commands"
        assert "/address-pr" in block.detail

    def test_current_command_passes(self, project: Path, cfg: ProjectConfig) -> None:
        from sova.supervisor.gates.stale_commands import check_stale_commands_gate

        assert check_stale_commands_gate(project, "integrate-pr", cfg) is None

    def test_unevaluable_gate_fails_open(self, project: Path, cfg: ProjectConfig) -> None:
        from sova.supervisor.gates.stale_commands import check_stale_commands_gate

        with patch("sova.supervisor.gates.stale_commands.find_stale_command", side_effect=RuntimeError("boom")):
            assert check_stale_commands_gate(project, "address-pr", cfg) is None


class TestStaleCommandPreSpawnCheck:
    def test_stale_command_blocks(self, project: Path, cfg: ProjectConfig) -> None:
        from sova.dashboard.services.agent_validation import check_stale_command

        _age_manifest_entry(project, "rearrange-commits.md", "old recipe\n")

        with patch("sova.config.loader.load_config", return_value=cfg):
            block = check_stale_command(project, "rearrange-commits")

        assert block is not None
        assert block["code"] == "stale_command"
        assert "/rearrange-commits" in block["error"]

    def test_current_command_passes(self, project: Path, cfg: ProjectConfig) -> None:
        from sova.dashboard.services.agent_validation import check_stale_command

        with patch("sova.config.loader.load_config", return_value=cfg):
            assert check_stale_command(project, "review-full") is None

    def test_non_safety_command_skips_config_load(self, project: Path) -> None:
        """Every dashboard command passes through here: only the safety-critical ones may pay for a config load."""
        from sova.dashboard.services.agent_validation import check_stale_command

        with patch("sova.config.loader.load_config") as mock_load:
            assert check_stale_command(project, "agent-resume") is None
        mock_load.assert_not_called()

    def test_unevaluable_check_fails_open(self, project: Path) -> None:
        from sova.dashboard.services.agent_validation import check_stale_command

        with patch("sova.config.loader.load_config", side_effect=RuntimeError("boom")):
            assert check_stale_command(project, "address-pr") is None


class TestStartCommandStaleBlock:
    async def test_start_command_returns_block_without_spawning(self) -> None:
        from sova.dashboard.services.agent_lifecycle import start_command

        block = {"code": "stale_command", "error": "/address-pr is out of date"}
        capture_sha = AsyncMock()
        with (
            patch(
                "sova.dashboard.services.agent_lifecycle._get_project_agents",
                return_value=MagicMock(project_dir=Path("/tmp")),
            ),
            patch(
                "sova.dashboard.services.agent_lifecycle._resolve_command_context",
                new_callable=AsyncMock,
                return_value=(7, "42"),
            ),
            patch("sova.dashboard.services.agent_lifecycle.check_memory_pressure", return_value=(None, None)),
            patch("sova.dashboard.services.agent_lifecycle.check_network_connectivity", return_value=(None, None)),
            patch("sova.dashboard.services.agent_lifecycle.check_stale_command", return_value=block) as mock_stale,
            patch("sova.dashboard.services.agent_db._capture_pr_head_sha", capture_sha),
        ):
            result = await start_command("address-pr", {"pr": 7})

        assert result == block
        mock_stale.assert_called_once_with(Path("/tmp"), "address-pr")
        capture_sha.assert_not_awaited()

    async def test_start_command_blocks_an_outdated_copy_on_the_worktree_branch(self, tmp_path: Path) -> None:
        import asyncio

        from sova.dashboard.services.agent_lifecycle import start_command

        project_dir = tmp_path / "primary"
        worktree = tmp_path / "worktree"
        project_dir.mkdir()
        worktree.mkdir()
        pa = MagicMock(project_dir=project_dir, agents={}, max_concurrent=3, _lock=asyncio.Lock())
        block = {"code": "stale_command", "error": "branch copy of /address-pr is out of date"}
        create_run = AsyncMock()
        with (
            patch("sova.dashboard.services.agent_lifecycle._get_project_agents", return_value=pa),
            patch(
                "sova.dashboard.services.agent_lifecycle._resolve_command_context",
                new_callable=AsyncMock,
                return_value=(7, "42"),
            ),
            patch("sova.dashboard.services.agent_lifecycle.check_memory_pressure", return_value=(None, None)),
            patch("sova.dashboard.services.agent_lifecycle.check_network_connectivity", return_value=(None, None)),
            patch(
                "sova.dashboard.services.agent_lifecycle.check_stale_command", side_effect=[None, block]
            ) as mock_stale,
            patch("sova.dashboard.services.agent_db._capture_pr_head_sha", AsyncMock(return_value="abc")),
            patch("sova.dashboard.services.agent_lifecycle._check_issue_conflict", AsyncMock(return_value=None)),
            patch("sova.dashboard.services.agent_lifecycle._evict_completed_for_issue"),
            patch("sova.dashboard.services.agent_lifecycle._resolve_branch_name", AsyncMock(return_value="feat/x")),
            patch("sova.dashboard.services.agent_lifecycle._resolve_issue_worktree", AsyncMock(return_value=worktree)),
            patch("sova.dashboard.services.agent_lifecycle._create_task_run", create_run),
        ):
            result = await start_command("address-pr", {"pr": 7})

        assert result == block
        assert mock_stale.call_args_list[1].args == (project_dir, "address-pr", worktree)
        create_run.assert_not_awaited()


class TestSyncOverwriteLog:
    def test_overwriting_an_out_of_date_copy_is_logged(self, project: Path, cfg: ProjectConfig) -> None:
        old = "old recipe\n"
        _age_manifest_entry(project, "address-pr.md", old)

        with capture_logs() as logs:
            result = update_commands(get_canonical_dir(), _commands_dir(project), cfg, adapter=ClaudeCodeAdapter())

        assert "address-pr.md" in result.updated_files
        events = [e for e in logs if e["event"] == "commands.sync.overwritten"]
        assert len(events) == 1
        assert events[0]["filename"] == "address-pr.md"
        assert events[0]["old_hash"] == file_hash(old)
        new_text = (_commands_dir(project) / "address-pr.md").read_text(encoding="utf-8")
        assert events[0]["new_hash"] == file_hash(new_text)

    def test_up_to_date_sync_logs_no_overwrite(self, project: Path, cfg: ProjectConfig) -> None:
        with capture_logs() as logs:
            update_commands(get_canonical_dir(), _commands_dir(project), cfg, adapter=ClaudeCodeAdapter())

        assert not [e for e in logs if e["event"] == "commands.sync.overwritten"]

    def test_forced_overwrite_of_an_untracked_copy_is_logged(self, project: Path, cfg: ProjectConfig) -> None:
        commands = _commands_dir(project)
        custom = "hand-written pr recipe\n"
        (commands / "pr.md").write_text(custom, encoding="utf-8")
        manifest = read_manifest(commands)
        assert manifest is not None
        del manifest.commands["pr.md"]
        write_manifest(commands, manifest)

        with capture_logs() as logs:
            update_commands(get_canonical_dir(), commands, cfg, force=True, adapter=ClaudeCodeAdapter())

        events = [e for e in logs if e["event"] == "commands.sync.overwritten"]
        assert [e["filename"] for e in events] == ["pr.md"]
        assert events[0]["old_hash"] == file_hash(custom)
