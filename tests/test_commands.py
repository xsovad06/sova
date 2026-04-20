"""Tests for SOVA command distribution and adaptation system."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def canonical_dir(tmp_path: Path) -> Path:
    """Create a fake canonical commands directory with sample commands."""
    cmd_dir = tmp_path / "canonical"
    cmd_dir.mkdir()

    # A core workflow command with template variables
    (cmd_dir / "develop.md").write_text(
        "---\n"
        "name: develop\n"
        "description: Develop a feature.\n"
        "user-invocable: true\n"
        "category: core\n"
        "---\n"
        "\n"
        "Run `{{ lint_cmd }}` to lint.\n"
        "Run `{{ test_cmd }}` to test.\n"
        "Scopes: {{ scopes }}\n"
    )

    # An autonomous-only command
    (cmd_dir / "ship-pr.md").write_text(
        "---\n"
        "name: ship-pr\n"
        "description: Ship a PR.\n"
        "user-invocable: true\n"
        "category: autonomous\n"
        "---\n"
        "\n"
        "Ship it.\n"
    )

    # A project management command (no template vars)
    (cmd_dir / "standup.md").write_text(
        "---\n"
        "name: standup\n"
        "description: Daily standup.\n"
        "user-invocable: true\n"
        "category: management\n"
        "---\n"
        "\n"
        "Show standup.\n"
    )

    return cmd_dir


@pytest.fixture()
def target_dir(tmp_path: Path) -> Path:
    """Create a fake target project commands directory."""
    cmd_dir = tmp_path / "target" / ".claude" / "commands"
    cmd_dir.mkdir(parents=True)
    return cmd_dir


# ---------------------------------------------------------------------------
# catalog.py -- command discovery and classification
# ---------------------------------------------------------------------------


class TestCatalog:
    def test_discover_commands(self, canonical_dir: Path) -> None:
        """discover() finds all .md files with valid frontmatter."""
        from sova.commands.catalog import discover

        commands = discover(canonical_dir)
        assert len(commands) == 3
        names = {c.name for c in commands}
        assert names == {"develop", "ship-pr", "standup"}

    def test_command_entry_fields(self, canonical_dir: Path) -> None:
        """CommandEntry has expected fields from frontmatter."""
        from sova.commands.catalog import discover

        commands = {c.name: c for c in discover(canonical_dir)}
        dev = commands["develop"]
        assert dev.description == "Develop a feature."
        assert dev.category == "core"
        assert dev.user_invocable is True
        assert dev.path == canonical_dir / "develop.md"

    def test_discover_skips_non_md(self, tmp_path: Path) -> None:
        """discover() ignores non-.md files."""
        from sova.commands.catalog import discover

        (tmp_path / "readme.txt").write_text("not a command")
        (tmp_path / "valid.md").write_text(
            "---\nname: valid\ndescription: ok\nuser-invocable: true\ncategory: core\n---\nContent.\n"
        )
        commands = discover(tmp_path)
        assert len(commands) == 1
        assert commands[0].name == "valid"

    def test_discover_skips_missing_frontmatter(self, tmp_path: Path) -> None:
        """discover() skips .md files without valid YAML frontmatter."""
        from sova.commands.catalog import discover

        (tmp_path / "bad.md").write_text("# No frontmatter here\nJust content.\n")
        commands = discover(tmp_path)
        assert len(commands) == 0

    def test_get_canonical_dir(self) -> None:
        """get_canonical_dir() returns the repo's commands/ directory."""
        from sova.commands.catalog import get_canonical_dir

        result = get_canonical_dir()
        assert result.name == "commands"
        assert result.is_dir()

    def test_classify_commands(self, canonical_dir: Path) -> None:
        """classify() groups commands by category."""
        from sova.commands.catalog import classify, discover

        commands = discover(canonical_dir)
        groups = classify(commands)
        assert "core" in groups
        assert "autonomous" in groups
        assert "management" in groups
        assert len(groups["core"]) == 1
        assert groups["core"][0].name == "develop"


# ---------------------------------------------------------------------------
# templates.py -- template rendering
# ---------------------------------------------------------------------------


class TestTemplates:
    def test_render_replaces_variables(self) -> None:
        """render_command() replaces template variables with config values."""
        from sova.commands.templates import render_command

        content = "Run `{{ lint_cmd }}` to lint.\nRun `{{ test_cmd }}` to test.\n"
        variables = {"lint_cmd": "ruff check .", "test_cmd": "pytest"}
        result = render_command(content, variables)
        assert "ruff check ." in result
        assert "pytest" in result
        assert "{{" not in result

    def test_render_preserves_unknown_variables(self) -> None:
        """render_command() leaves unknown variables as-is (undefined=keep)."""
        from sova.commands.templates import render_command

        content = "Run `{{ lint_cmd }}` and {{ unknown_var }}.\n"
        variables = {"lint_cmd": "make lint"}
        result = render_command(content, variables)
        assert "make lint" in result
        assert "{{ unknown_var }}" in result

    def test_render_empty_variables(self) -> None:
        """render_command() with empty dict returns original content."""
        from sova.commands.templates import render_command

        content = "No variables here.\n"
        result = render_command(content, {})
        assert result == content

    def test_build_variables_from_config(self) -> None:
        """build_variables() extracts template vars from ProjectConfig."""
        from sova.commands.templates import build_variables
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(test_cmd="pytest", lint_cmd="ruff check .")
        variables = build_variables(cfg)
        assert variables["test_cmd"] == "pytest"
        assert variables["lint_cmd"] == "ruff check ."

    def test_build_variables_includes_scopes(self) -> None:
        """build_variables() includes scopes from config when available."""
        from sova.commands.templates import build_variables
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        variables = build_variables(cfg)
        assert "scopes" in variables

    def test_render_scopes_list(self) -> None:
        """render_command() handles scopes as a comma-separated string."""
        from sova.commands.templates import render_command

        content = "Scopes: {{ scopes }}\n"
        variables = {"scopes": "agent, dashboard, cli"}
        result = render_command(content, variables)
        assert "agent, dashboard, cli" in result


# ---------------------------------------------------------------------------
# manifest.py -- manifest tracking
# ---------------------------------------------------------------------------


class TestManifest:
    def test_create_manifest(self, target_dir: Path) -> None:
        """create_manifest() writes a valid JSON manifest."""
        from sova.commands.manifest import create_manifest

        entries = {"develop.md": "abc123", "standup.md": "def456"}
        create_manifest(target_dir, entries)

        manifest_path = target_dir / ".sova-manifest.json"
        assert manifest_path.exists()
        data = json.loads(manifest_path.read_text())
        assert data["version"] == 1
        assert data["commands"]["develop.md"]["hash"] == "abc123"
        assert data["commands"]["develop.md"]["managed"] is True

    def test_read_manifest(self, target_dir: Path) -> None:
        """read_manifest() loads and parses manifest JSON."""
        from sova.commands.manifest import create_manifest, read_manifest

        entries = {"develop.md": "abc123"}
        create_manifest(target_dir, entries)

        manifest = read_manifest(target_dir)
        assert manifest is not None
        assert "develop.md" in manifest.commands
        assert manifest.commands["develop.md"].hash == "abc123"

    def test_read_manifest_missing_file(self, target_dir: Path) -> None:
        """read_manifest() returns None if manifest doesn't exist."""
        from sova.commands.manifest import read_manifest

        manifest = read_manifest(target_dir)
        assert manifest is None

    def test_update_manifest_entry(self, target_dir: Path) -> None:
        """update_manifest() updates individual entries."""
        from sova.commands.manifest import create_manifest, read_manifest, update_manifest

        create_manifest(target_dir, {"develop.md": "old_hash"})
        update_manifest(target_dir, "develop.md", "new_hash")

        manifest = read_manifest(target_dir)
        assert manifest is not None
        assert manifest.commands["develop.md"].hash == "new_hash"

    def test_manifest_tracks_managed_vs_local(self, target_dir: Path) -> None:
        """Manifest distinguishes managed (SOVA) commands from local ones."""
        from sova.commands.manifest import create_manifest, read_manifest

        entries = {"develop.md": "abc123"}
        create_manifest(target_dir, entries)

        manifest = read_manifest(target_dir)
        assert manifest is not None
        assert manifest.commands["develop.md"].managed is True

    def test_file_hash(self) -> None:
        """file_hash() returns consistent SHA-256 for file content."""
        from sova.commands.manifest import file_hash

        assert file_hash("hello world\n") == file_hash("hello world\n")
        assert file_hash("a") != file_hash("b")


# ---------------------------------------------------------------------------
# distribution.py -- install/update/diff
# ---------------------------------------------------------------------------


class TestDistribution:
    def test_install_commands(self, canonical_dir: Path, target_dir: Path) -> None:
        """install_commands() copies and adapts commands to target."""
        from sova.commands.distribution import install_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(test_cmd="pytest", lint_cmd="ruff check .")
        result = install_commands(canonical_dir, target_dir, cfg)

        assert (target_dir / "develop.md").exists()
        assert (target_dir / "standup.md").exists()
        assert result.installed > 0

        # Verify template rendering
        content = (target_dir / "develop.md").read_text()
        assert "ruff check ." in content
        assert "pytest" in content

    def test_install_creates_manifest(self, canonical_dir: Path, target_dir: Path) -> None:
        """install_commands() creates a manifest tracking installed commands."""
        from sova.commands.distribution import install_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        manifest_path = target_dir / ".sova-manifest.json"
        assert manifest_path.exists()

    def test_install_preserves_local_commands(self, canonical_dir: Path, target_dir: Path) -> None:
        """install_commands() does not touch project-specific commands."""
        from sova.commands.distribution import install_commands
        from sova.config.models import ProjectConfig

        # Create a local command
        (target_dir / "my-custom.md").write_text("# My Custom Command\n")

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        # Local command preserved
        assert (target_dir / "my-custom.md").exists()
        assert (target_dir / "my-custom.md").read_text() == "# My Custom Command\n"

    def test_install_skips_autonomous_by_default(self, canonical_dir: Path, target_dir: Path) -> None:
        """install_commands() skips autonomous commands unless opted in."""
        from sova.commands.distribution import install_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg, include_autonomous=False)

        assert not (target_dir / "ship-pr.md").exists()
        assert (target_dir / "develop.md").exists()

    def test_install_includes_autonomous_when_opted_in(self, canonical_dir: Path, target_dir: Path) -> None:
        """install_commands() includes autonomous commands when opted in."""
        from sova.commands.distribution import install_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg, include_autonomous=True)

        assert (target_dir / "ship-pr.md").exists()

    def test_update_changes_only_modified(self, canonical_dir: Path, target_dir: Path) -> None:
        """update_commands() only writes commands whose source changed."""
        from sova.commands.distribution import install_commands, update_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        # Update source
        (canonical_dir / "develop.md").write_text(
            "---\nname: develop\ndescription: Updated.\nuser-invocable: true\ncategory: core\n---\n\nNew content.\n"
        )

        import time

        time.sleep(0.01)  # ensure mtime differs

        result = update_commands(canonical_dir, target_dir, cfg)
        assert result.updated >= 1
        assert result.skipped >= 1

    def test_update_detects_customized_commands(self, canonical_dir: Path, target_dir: Path) -> None:
        """update_commands() detects when user modified a managed command."""
        from sova.commands.distribution import install_commands, update_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        # User customizes a managed command
        (target_dir / "standup.md").write_text("# My Custom Standup\n")

        # Source also changed
        (canonical_dir / "standup.md").write_text(
            "---\nname: standup\ndescription: Updated standup.\nuser-invocable: true\ncategory: management\n---\n\n"
            "New standup.\n"
        )

        result = update_commands(canonical_dir, target_dir, cfg)
        assert len(result.conflicts) >= 1
        assert "standup.md" in result.conflicts

    def test_diff_commands(self, canonical_dir: Path, target_dir: Path) -> None:
        """diff_commands() shows what changed since last install."""
        from sova.commands.distribution import diff_commands, install_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        # Source changed
        (canonical_dir / "develop.md").write_text(
            "---\nname: develop\ndescription: Updated.\nuser-invocable: true\ncategory: core\n---\n\nNew content.\n"
        )

        diff = diff_commands(canonical_dir, target_dir, cfg)
        assert len(diff.changed) >= 1

    def test_diff_detects_new_commands(self, canonical_dir: Path, target_dir: Path) -> None:
        """diff_commands() detects new commands in canonical that aren't installed."""
        from sova.commands.distribution import diff_commands, install_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        # Add a new command to canonical
        (canonical_dir / "new-cmd.md").write_text(
            "---\nname: new-cmd\ndescription: Brand new.\nuser-invocable: true\ncategory: core\n---\n\nNew.\n"
        )

        diff = diff_commands(canonical_dir, target_dir, cfg)
        assert "new-cmd.md" in diff.new

    def test_list_commands(self, canonical_dir: Path, target_dir: Path) -> None:
        """list_commands() shows canonical and local commands."""
        from sova.commands.distribution import install_commands, list_commands
        from sova.config.models import ProjectConfig

        # Add a local command
        (target_dir / "my-custom.md").write_text("# Custom\n")

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        listing = list_commands(target_dir)
        managed_names = {e.filename for e in listing.managed}
        local_names = {e.filename for e in listing.local}

        assert "develop.md" in managed_names
        assert "my-custom.md" in local_names


# ---------------------------------------------------------------------------
# CLI integration (sova commands list/diff/update)
# ---------------------------------------------------------------------------


class TestCommandsCLI:
    def test_commands_list(self) -> None:
        """sova commands list runs without error."""
        from typer.testing import CliRunner

        from sova.cli.app import app

        runner = CliRunner()
        result = runner.invoke(app, ["commands", "list", "--help"])
        assert result.exit_code == 0

    def test_commands_diff(self) -> None:
        """sova commands diff runs without error."""
        from typer.testing import CliRunner

        from sova.cli.app import app

        runner = CliRunner()
        result = runner.invoke(app, ["commands", "diff", "--help"])
        assert result.exit_code == 0

    def test_commands_update(self) -> None:
        """sova commands update runs without error."""
        from typer.testing import CliRunner

        from sova.cli.app import app

        runner = CliRunner()
        result = runner.invoke(app, ["commands", "update", "--help"])
        assert result.exit_code == 0
