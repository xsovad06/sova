"""Tests for SOVA command distribution and adaptation system."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
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
    (cmd_dir / "review-pr.md").write_text(
        "---\nname: review-pr\ndescription: Review a PR.\nuser-invocable: true\n"
        "category: autonomous\n---\n\nReview it.\n"
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


@pytest.fixture
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
        assert names == {"develop", "review-pr", "standup"}

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

    def test_parse_inputs_outputs(self, tmp_path: Path) -> None:
        """discover() parses inputs and outputs from YAML list frontmatter."""
        from sova.commands.catalog import discover

        (tmp_path / "cmd.md").write_text(
            "---\n"
            "name: cmd\n"
            "description: Test command\n"
            "user-invocable: true\n"
            "category: core\n"
            "inputs:\n"
            "  - issue_number\n"
            "  - branch_name\n"
            "outputs:\n"
            "  - files_changed\n"
            "  - test_results\n"
            "---\n"
            "\nContent.\n"
        )
        commands = discover(tmp_path)
        assert len(commands) == 1
        assert commands[0].inputs == ["issue_number", "branch_name"]
        assert commands[0].outputs == ["files_changed", "test_results"]

    def test_parse_empty_inputs_outputs(self, tmp_path: Path) -> None:
        """discover() handles commands without inputs/outputs gracefully."""
        from sova.commands.catalog import discover

        (tmp_path / "cmd.md").write_text(
            "---\nname: cmd\ndescription: No IO\nuser-invocable: true\ncategory: core\n---\nContent.\n"
        )
        commands = discover(tmp_path)
        assert len(commands) == 1
        assert commands[0].inputs == []
        assert commands[0].outputs == []

    def test_parse_yaml_simple_lists(self) -> None:
        """_parse_yaml_simple handles mixed scalar and list values."""
        from sova.commands.catalog import _parse_yaml_simple

        text = "name: test\ndescription: A test\ninputs:\n  - a\n  - b\noutputs:\n  - c\ncategory: core"
        result = _parse_yaml_simple(text)
        assert result["name"] == "test"
        assert result["inputs"] == ["a", "b"]
        assert result["outputs"] == ["c"]
        assert result["category"] == "core"


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

    def test_build_variables_check_cmd_explicit(self) -> None:
        """build_variables() uses explicit check_cmd when configured."""
        from sova.commands.templates import build_variables
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(test_cmd="pytest", lint_cmd="ruff check .", check_cmd="make check")
        variables = build_variables(cfg)
        assert variables["check_cmd"] == "make check"

    def test_build_variables_check_cmd_fallback(self) -> None:
        """build_variables() composes check_cmd from lint + test when not set."""
        from sova.commands.templates import build_variables
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(test_cmd="pytest", lint_cmd="ruff check .")
        variables = build_variables(cfg)
        assert variables["check_cmd"] == "ruff check . && pytest"

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

        assert not (target_dir / "review-pr.md").exists()
        assert (target_dir / "develop.md").exists()

    def test_install_includes_autonomous_when_opted_in(self, canonical_dir: Path, target_dir: Path) -> None:
        """install_commands() includes autonomous commands when opted in."""
        from sova.commands.distribution import install_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg, include_autonomous=True)

        assert (target_dir / "review-pr.md").exists()

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

    def test_update_non_utf8_local_file_does_not_raise(self, canonical_dir: Path, target_dir: Path) -> None:
        """update_commands() must not crash reading a locally-corrupted (non-UTF-8) managed file."""
        from sova.commands.distribution import install_commands, update_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        # Corrupt the installed file with invalid UTF-8 bytes.
        (target_dir / "standup.md").write_bytes(b"\xff\xfe not valid utf-8")

        # Source also changed, so the file is a candidate for update.
        (canonical_dir / "standup.md").write_text(
            "---\nname: standup\ndescription: Updated standup.\nuser-invocable: true\ncategory: management\n---\n\n"
            "New standup.\n"
        )

        result = update_commands(canonical_dir, target_dir, cfg, filenames=["standup.md"], force=True)
        assert result.updated == 1
        assert (target_dir / "standup.md").read_text(encoding="utf-8").endswith("New standup.\n")

    def test_update_non_utf8_local_file_is_conflict_when_not_forced(
        self, canonical_dir: Path, target_dir: Path
    ) -> None:
        """A locally-corrupted (unreadable) managed file must be reported as a conflict,

        not silently overwritten, when force=False (including the default body-less
        "Sync All" flow).
        """
        from sova.commands.distribution import install_commands, update_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        original_bytes = b"\xff\xfe not valid utf-8"
        (target_dir / "standup.md").write_bytes(original_bytes)

        # Source also changed, so the file is a candidate for update.
        (canonical_dir / "standup.md").write_text(
            "---\nname: standup\ndescription: Updated standup.\nuser-invocable: true\ncategory: management\n---\n\n"
            "New standup.\n"
        )

        result = update_commands(canonical_dir, target_dir, cfg, force=False)
        assert "standup.md" in result.conflicts
        assert (target_dir / "standup.md").read_bytes() == original_bytes

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

    def test_diff_unreadable_canonical_file_skipped_not_raised(self, canonical_dir: Path, target_dir: Path) -> None:
        """A canonical command file with invalid UTF-8 bytes is skipped, not raised.

        Regression test: diff_commands() must not let UnicodeDecodeError propagate
        from a single corrupted canonical file (bad checkout, non-UTF-8 edit), since
        that would fail the whole diff (and the dashboard's installation-review
        endpoint). Commands are discovered via discover(), which parses frontmatter
        from every canonical file up front and already skips unreadable ones (see
        catalog._parse_command_file), so a corrupted canonical command file is
        indistinguishable from one deleted upstream and surfaces as "removed"
        (the pre-existing, non-crashing classification for "not present in the
        current discover() scan"), rather than raising.
        """
        from sova.commands.distribution import diff_commands, install_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        (canonical_dir / "develop.md").write_bytes(b"\xff\xfe not valid utf-8")

        diff = diff_commands(canonical_dir, target_dir, cfg)
        assert "develop.md" not in diff.changed
        assert "develop.md" not in diff.new
        assert "develop.md" in diff.removed

    def test_diff_unreadable_canonical_guideline_skipped_not_raised(
        self, guidelines_dir: Path, rules_dir: Path
    ) -> None:
        """A canonical guideline file with invalid UTF-8 bytes is skipped, not raised.

        Regression test: unlike commands (pre-filtered by discover()'s own
        frontmatter parse, see the sibling command test above), guidelines are
        collected by a plain glob with no read (_collect_guidelines()), so this
        exercises _diff_files()'s own unreadable-canonical-file guard directly: the
        filename stays out of new/changed/removed entirely, since it's still added
        to canonical_names before the failed read.
        """
        from sova.commands.distribution import diff_guidelines, install_guidelines
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(github_repo="owner/myapp", test_cmd="pytest", lint_cmd="ruff check .")
        install_guidelines(guidelines_dir, rules_dir, cfg)

        (guidelines_dir / "security.md").write_bytes(b"\xff\xfe not valid utf-8")

        diff = diff_guidelines(guidelines_dir, rules_dir, cfg)
        assert "security.md" not in diff.changed
        assert "security.md" not in diff.new
        assert "security.md" not in diff.removed

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


class TestUpdateFilenamesAllowList:
    def test_filenames_restricts_to_subset(self, canonical_dir: Path, target_dir: Path) -> None:
        """update_commands() with filenames only updates the named subset."""
        from sova.commands.distribution import install_commands, update_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        (canonical_dir / "develop.md").write_text(
            "---\nname: develop\ndescription: Updated.\nuser-invocable: true\ncategory: core\n---\n\nNew develop.\n"
        )
        (canonical_dir / "standup.md").write_text(
            "---\nname: standup\ndescription: Updated.\nuser-invocable: true\ncategory: management\n---\n\n"
            "New standup.\n"
        )

        result = update_commands(canonical_dir, target_dir, cfg, filenames=["develop.md"])

        assert result.updated == 1
        assert "New develop." in (target_dir / "develop.md").read_text()
        assert "New standup." not in (target_dir / "standup.md").read_text()

    def test_empty_filenames_updates_nothing(self, canonical_dir: Path, target_dir: Path) -> None:
        """update_commands() with filenames=[] is an explicit empty selection."""
        from sova.commands.distribution import install_commands, update_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        (canonical_dir / "develop.md").write_text(
            "---\nname: develop\ndescription: Updated.\nuser-invocable: true\ncategory: core\n---\n\nNew develop.\n"
        )

        result = update_commands(canonical_dir, target_dir, cfg, filenames=[])

        assert result.updated == 0
        assert "New develop." not in (target_dir / "develop.md").read_text()

    def test_empty_filenames_on_fresh_target_creates_no_manifest(self, canonical_dir: Path, target_dir: Path) -> None:
        """An explicit empty selection on a never-installed target must not create a manifest."""
        from sova.commands.distribution import update_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()

        result = update_commands(canonical_dir, target_dir, cfg, filenames=[])

        assert result.updated == 0
        assert not (target_dir / ".sova-manifest.json").exists()

    def test_unknown_filename_is_a_noop(self, canonical_dir: Path, target_dir: Path) -> None:
        """A filenames entry not present in the canonical source updates nothing for it."""
        from sova.commands.distribution import install_commands, update_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        result = update_commands(canonical_dir, target_dir, cfg, filenames=["does-not-exist.md"])

        assert result.updated == 0

    def test_filenames_with_force_overrides_conflict(self, canonical_dir: Path, target_dir: Path) -> None:
        """filenames + force=True updates a locally modified file instead of flagging a conflict."""
        from sova.commands.distribution import install_commands, update_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        (target_dir / "standup.md").write_text("# Locally customized\n")
        (canonical_dir / "standup.md").write_text(
            "---\nname: standup\ndescription: Updated.\nuser-invocable: true\ncategory: management\n---\n\n"
            "New standup.\n"
        )

        result = update_commands(canonical_dir, target_dir, cfg, filenames=["standup.md"], force=True)

        assert result.updated == 1
        assert result.conflicts == []
        assert "New standup." in (target_dir / "standup.md").read_text()

    def test_filenames_with_force_restores_local_edit_when_canonical_unchanged(
        self, canonical_dir: Path, target_dir: Path
    ) -> None:
        """force=True restores a locally-modified file even when canonical hasn't changed.

        Selective sync (the review-changes modal) presents a locally-modified
        file as a "local_modified" conflict the user can accept, which sends
        force=True. The canonical file itself may not have changed since
        install: the old "canonical unchanged" skip fired before force was
        ever consulted, silently no-opping the restore.
        """
        from sova.commands.distribution import install_commands, update_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        (target_dir / "standup.md").write_text("# Locally customized\n")

        result = update_commands(canonical_dir, target_dir, cfg, filenames=["standup.md"], force=True)

        assert result.updated == 1
        assert result.conflicts == []
        assert "# Locally customized" not in (target_dir / "standup.md").read_text()

    def test_filenames_with_force_restores_locally_deleted_when_canonical_unchanged(
        self, canonical_dir: Path, target_dir: Path
    ) -> None:
        """force=True restores a locally-deleted file even when canonical hasn't changed."""
        from sova.commands.distribution import install_commands, update_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        (target_dir / "standup.md").unlink()

        result = update_commands(canonical_dir, target_dir, cfg, filenames=["standup.md"], force=True)

        assert result.updated == 1
        assert result.conflicts == []
        assert (target_dir / "standup.md").is_file()

    def test_force_with_no_local_drift_is_a_noop_skip(self, canonical_dir: Path, target_dir: Path) -> None:
        """force=True on a file with no local drift and no canonical change still skips."""
        from sova.commands.distribution import install_commands, update_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        result = update_commands(canonical_dir, target_dir, cfg, filenames=["standup.md"], force=True)

        assert result.updated == 0
        assert result.skipped >= 1
        assert result.conflicts == []

    def test_force_no_op_skip_repairs_stale_manifest_hash(self, canonical_dir: Path, target_dir: Path) -> None:
        """force=True on a no-op skip (installed content already matches canonical)
        still repairs a stale manifest hash, so a later non-force sync doesn't
        keep reporting a conflict for a file that is actually already clean.
        """
        from sova.commands.distribution import install_commands, update_commands
        from sova.commands.manifest import read_manifest, update_manifest
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        # Simulate a stale manifest entry: the installed file already matches
        # canonical content, but the manifest still records an old hash.
        update_manifest(target_dir, "standup.md", "stale-hash-does-not-match-content")

        result = update_commands(canonical_dir, target_dir, cfg, filenames=["standup.md"], force=True)

        assert result.updated == 0
        assert result.skipped >= 1
        assert result.conflicts == []

        manifest = read_manifest(target_dir)
        assert manifest is not None
        assert manifest.commands["standup.md"].hash != "stale-hash-does-not-match-content"

        # A subsequent non-force sync must no longer see this as a conflict.
        rerun = update_commands(canonical_dir, target_dir, cfg, filenames=["standup.md"])
        assert rerun.conflicts == []

    def test_non_force_repairs_stale_manifest_when_content_already_matches(
        self, canonical_dir: Path, target_dir: Path
    ) -> None:
        """A non-force sync must also repair a stale manifest hash when the
        installed file already matches canonical content, not just report a
        conflict. Without this, "Sync All" (non-force) reports a false
        conflict for a file the review modal cannot even surface, because
        _build_category_diffs already classifies it as clean.
        """
        from sova.commands.distribution import install_commands, update_commands
        from sova.commands.manifest import read_manifest, update_manifest
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        # Simulate a stale manifest entry: the installed file already matches
        # canonical content, but the manifest still records an old hash (e.g.
        # a prior sync crashed between write_text and update_manifest).
        update_manifest(target_dir, "standup.md", "stale-hash-does-not-match-content")

        result = update_commands(canonical_dir, target_dir, cfg, filenames=["standup.md"])

        assert result.updated == 0
        assert result.skipped >= 1
        assert result.conflicts == []

        manifest = read_manifest(target_dir)
        assert manifest is not None
        assert manifest.commands["standup.md"].hash != "stale-hash-does-not-match-content"

    def test_guidelines_filenames_restricts_to_subset(self, guidelines_dir: Path, rules_dir: Path) -> None:
        """update_guidelines() with filenames only updates the named subset."""
        from sova.commands.distribution import install_guidelines, update_guidelines
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_guidelines(guidelines_dir, rules_dir, cfg)

        (guidelines_dir / "security.md").write_text("# Updated security\n")
        (guidelines_dir / "testing.md").write_text("# Updated testing\n")

        result = update_guidelines(guidelines_dir, rules_dir, cfg, filenames=["security.md"])

        assert result.updated == 1
        assert "Updated security" in (rules_dir / "security.md").read_text()
        assert "Updated testing" not in (rules_dir / "testing.md").read_text()


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


# ---------------------------------------------------------------------------
# Guidelines distribution
# ---------------------------------------------------------------------------


@pytest.fixture
def guidelines_dir(tmp_path: Path) -> Path:
    """Create a fake guidelines directory with sample templates."""
    guide_dir = tmp_path / "guidelines"
    guide_dir.mkdir()

    (guide_dir / "security.md").write_text(
        "# Security Guidelines for {{ project_name }}\n\n"
        "Run `{{ test_cmd }}` to verify.\n"
        "Base branch: {{ base_branch }}\n"
    )

    (guide_dir / "testing.md").write_text("# Testing Guidelines\n\nUse `{{ lint_cmd }}` for linting.\n")

    return guide_dir


@pytest.fixture
def rules_dir(tmp_path: Path) -> Path:
    """Create a fake target project rules directory."""
    rd = tmp_path / "target" / ".claude" / "rules"
    rd.mkdir(parents=True)
    return rd


class TestGuidelines:
    def test_install_guidelines(self, guidelines_dir: Path, rules_dir: Path) -> None:
        """install_guidelines() copies and renders guideline templates."""
        from sova.commands.distribution import install_guidelines
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(github_repo="owner/myapp", test_cmd="pytest", lint_cmd="ruff check .")
        result = install_guidelines(guidelines_dir, rules_dir, cfg)

        assert result.installed == 2
        assert (rules_dir / "security.md").exists()
        assert (rules_dir / "testing.md").exists()

    def test_install_guidelines_renders_variables(self, guidelines_dir: Path, rules_dir: Path) -> None:
        """Template variables in guidelines are replaced with config values."""
        from sova.commands.distribution import install_guidelines
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(
            github_repo="owner/myapp",
            test_cmd="pytest",
            lint_cmd="ruff check .",
            base_branch="develop",
        )
        install_guidelines(guidelines_dir, rules_dir, cfg)

        content = (rules_dir / "security.md").read_text()
        assert "myapp" in content
        assert "pytest" in content
        assert "develop" in content
        assert "{{ project_name }}" not in content
        assert "{{ test_cmd }}" not in content

    def test_install_guidelines_creates_manifest(self, guidelines_dir: Path, rules_dir: Path) -> None:
        """install_guidelines() creates a manifest in the rules directory."""
        from sova.commands.distribution import install_guidelines
        from sova.commands.manifest import read_manifest
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(github_repo="owner/myapp", test_cmd="pytest", lint_cmd="ruff check .")
        install_guidelines(guidelines_dir, rules_dir, cfg)

        manifest = read_manifest(rules_dir)
        assert manifest is not None
        assert "security.md" in manifest.commands
        assert "testing.md" in manifest.commands

    def test_update_guidelines_incremental(self, guidelines_dir: Path, rules_dir: Path) -> None:
        """update_guidelines() only updates changed guidelines."""
        from sova.commands.distribution import install_guidelines, update_guidelines
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(github_repo="owner/myapp", test_cmd="pytest", lint_cmd="ruff check .")
        install_guidelines(guidelines_dir, rules_dir, cfg)

        # Update without changes
        result = update_guidelines(guidelines_dir, rules_dir, cfg)
        assert result.updated == 0
        assert result.skipped == 2

        # Modify source
        (guidelines_dir / "security.md").write_text("# Updated security guide for {{ project_name }}\n")
        result = update_guidelines(guidelines_dir, rules_dir, cfg)
        assert result.updated == 1
        assert result.skipped == 1

    def test_update_guidelines_detects_conflicts(self, guidelines_dir: Path, rules_dir: Path) -> None:
        """update_guidelines() detects user-modified guidelines."""
        from sova.commands.distribution import install_guidelines, update_guidelines
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(github_repo="owner/myapp", test_cmd="pytest", lint_cmd="ruff check .")
        install_guidelines(guidelines_dir, rules_dir, cfg)

        # User modifies installed file AND source changes
        (rules_dir / "security.md").write_text("# My custom security guide\n")
        (guidelines_dir / "security.md").write_text("# New upstream version for {{ project_name }}\n")

        result = update_guidelines(guidelines_dir, rules_dir, cfg)
        assert "security.md" in result.conflicts

        forced = update_guidelines(guidelines_dir, rules_dir, cfg, force=True)
        assert "security.md" not in forced.conflicts
        assert forced.updated == 1

    def test_install_guidelines_empty_dir(self, tmp_path: Path, rules_dir: Path) -> None:
        """install_guidelines() handles missing guidelines directory gracefully."""
        from sova.commands.distribution import install_guidelines
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(github_repo="owner/myapp", test_cmd="pytest", lint_cmd="ruff check .")
        result = install_guidelines(tmp_path / "nonexistent", rules_dir, cfg)
        assert result.installed == 0

    def test_get_guidelines_dir(self) -> None:
        """get_guidelines_dir() returns the repo's guidelines/ directory."""
        from sova.commands.catalog import get_guidelines_dir

        guidelines_dir = get_guidelines_dir()
        assert guidelines_dir.name == "guidelines"
        assert guidelines_dir.is_dir()
        # guidelines/ lives at repo root, alongside the sova/ package
        assert (guidelines_dir.parent / "sova").is_dir()

    def test_build_variables_includes_project_name(self) -> None:
        """build_variables() includes project_name derived from github_repo."""
        from sova.commands.templates import build_variables
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(github_repo="myorg/cool-project", test_cmd="pytest", lint_cmd="ruff")
        variables = build_variables(cfg)
        assert variables["project_name"] == "cool-project"

    def test_build_variables_project_name_fallback(self) -> None:
        """build_variables() uses github_repo as-is when no slash present."""
        from sova.commands.templates import build_variables
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(github_repo="", test_cmd="pytest", lint_cmd="ruff")
        variables = build_variables(cfg)
        assert variables["project_name"] == "project"

    def test_build_variables_project_name_trailing_slash(self) -> None:
        """build_variables() handles trailing-slash repos gracefully."""
        from sova.commands.templates import build_variables
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(github_repo="owner/", test_cmd="pytest", lint_cmd="ruff")
        variables = build_variables(cfg)
        assert variables["project_name"] == "owner"


# ---------------------------------------------------------------------------
# diff_guidelines -- guideline diff detection
# ---------------------------------------------------------------------------


class TestDiffGuidelines:
    def test_diff_no_manifest_all_new(self, guidelines_dir: Path, rules_dir: Path) -> None:
        """diff_guidelines() reports all guidelines as new when no manifest exists."""
        from sova.commands.distribution import diff_guidelines
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(github_repo="owner/myapp", test_cmd="pytest", lint_cmd="ruff check .")
        diff = diff_guidelines(guidelines_dir, rules_dir, cfg)
        assert set(diff.new) == {"security.md", "testing.md"}
        assert diff.changed == []
        assert diff.removed == []

    def test_diff_up_to_date(self, guidelines_dir: Path, rules_dir: Path) -> None:
        """diff_guidelines() returns empty diff when installed matches canonical."""
        from sova.commands.distribution import diff_guidelines, install_guidelines
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(github_repo="owner/myapp", test_cmd="pytest", lint_cmd="ruff check .")
        install_guidelines(guidelines_dir, rules_dir, cfg)

        diff = diff_guidelines(guidelines_dir, rules_dir, cfg)
        assert diff.changed == []
        assert diff.new == []
        assert diff.removed == []

    def test_diff_detects_changed(self, guidelines_dir: Path, rules_dir: Path) -> None:
        """diff_guidelines() detects when a canonical guideline has changed."""
        from sova.commands.distribution import diff_guidelines, install_guidelines
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(github_repo="owner/myapp", test_cmd="pytest", lint_cmd="ruff check .")
        install_guidelines(guidelines_dir, rules_dir, cfg)

        (guidelines_dir / "security.md").write_text("# Updated security guide\n")

        diff = diff_guidelines(guidelines_dir, rules_dir, cfg)
        assert "security.md" in diff.changed
        assert "testing.md" not in diff.changed

    def test_diff_detects_new(self, guidelines_dir: Path, rules_dir: Path) -> None:
        """diff_guidelines() detects new guidelines added to canonical."""
        from sova.commands.distribution import diff_guidelines, install_guidelines
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(github_repo="owner/myapp", test_cmd="pytest", lint_cmd="ruff check .")
        install_guidelines(guidelines_dir, rules_dir, cfg)

        (guidelines_dir / "performance.md").write_text("# Performance Guidelines\n")

        diff = diff_guidelines(guidelines_dir, rules_dir, cfg)
        assert "performance.md" in diff.new

    def test_diff_detects_removed(self, guidelines_dir: Path, rules_dir: Path) -> None:
        """diff_guidelines() detects guidelines removed from canonical."""
        from sova.commands.distribution import diff_guidelines, install_guidelines
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(github_repo="owner/myapp", test_cmd="pytest", lint_cmd="ruff check .")
        install_guidelines(guidelines_dir, rules_dir, cfg)

        (guidelines_dir / "testing.md").unlink()

        diff = diff_guidelines(guidelines_dir, rules_dir, cfg)
        assert "testing.md" in diff.removed

    def test_diff_empty_guidelines_dir(self, tmp_path: Path, rules_dir: Path) -> None:
        """diff_guidelines() returns empty diff when no canonical guidelines exist."""
        from sova.commands.distribution import diff_guidelines
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(github_repo="owner/myapp", test_cmd="pytest", lint_cmd="ruff check .")
        diff = diff_guidelines(tmp_path / "nonexistent", rules_dir, cfg)
        assert diff.changed == []
        assert diff.new == []
        assert diff.removed == []


# ---------------------------------------------------------------------------
# Skills distribution
# ---------------------------------------------------------------------------


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    """Create a fake skills directory with sample skill subdirectories."""
    sd = tmp_path / "skills"
    sd.mkdir()

    alpha = sd / "alpha"
    alpha.mkdir()
    (alpha / "SKILL.md").write_text("# Alpha Skill\n\nRun `{{ test_cmd }}` to verify.\n")

    beta = sd / "beta"
    beta.mkdir()
    (beta / "SKILL.md").write_text("# Beta Skill\n\nLint with `{{ lint_cmd }}`.\n")

    # A directory without SKILL.md should be ignored
    gamma = sd / "gamma"
    gamma.mkdir()
    (gamma / "README.md").write_text("# Not a skill\n")

    return sd


@pytest.fixture
def skills_target(tmp_path: Path) -> Path:
    """Create a fake target project skills directory."""
    st = tmp_path / "target" / ".claude" / "skills"
    st.mkdir(parents=True)
    return st


class TestSkillsDistribution:
    def test_collect_skills(self, skills_dir: Path) -> None:
        """_collect_skills() finds only directories with SKILL.md."""
        from sova.commands.distribution import _collect_skills

        files = _collect_skills(skills_dir)
        keys = [k for k, _ in files]
        assert "alpha/SKILL.md" in keys
        assert "beta/SKILL.md" in keys
        assert not any("gamma" in k for k in keys)

    def test_collect_skills_empty(self, tmp_path: Path) -> None:
        """_collect_skills() returns empty list for missing directory."""
        from sova.commands.distribution import _collect_skills

        assert _collect_skills(tmp_path / "nonexistent") == []

    def test_install_skills(self, skills_dir: Path, skills_target: Path) -> None:
        """install_skills() copies skill files into subdirectories."""
        from sova.commands.distribution import install_skills
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(test_cmd="pytest", lint_cmd="ruff check .")
        result = install_skills(skills_dir, skills_target, cfg)

        assert result.installed == 2
        assert (skills_target / "alpha" / "SKILL.md").exists()
        assert (skills_target / "beta" / "SKILL.md").exists()

        content = (skills_target / "alpha" / "SKILL.md").read_text()
        assert "pytest" in content

    def test_install_skills_renders_variables(self, skills_dir: Path, skills_target: Path) -> None:
        """Template variables in skills are replaced with config values."""
        from sova.commands.distribution import install_skills
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(test_cmd="pytest", lint_cmd="ruff check .")
        install_skills(skills_dir, skills_target, cfg)

        content = (skills_target / "beta" / "SKILL.md").read_text()
        assert "ruff check ." in content
        assert "{{ lint_cmd }}" not in content

    def test_update_skills_incremental(self, skills_dir: Path, skills_target: Path) -> None:
        """update_skills() only updates changed skills."""
        from sova.commands.distribution import install_skills, update_skills
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(test_cmd="pytest", lint_cmd="ruff check .")
        install_skills(skills_dir, skills_target, cfg)

        result = update_skills(skills_dir, skills_target, cfg)
        assert result.updated == 0
        assert result.skipped == 2

        (skills_dir / "alpha" / "SKILL.md").write_text("# Updated alpha\n")
        result = update_skills(skills_dir, skills_target, cfg)
        assert result.updated == 1
        assert result.skipped == 1

    def test_diff_skills(self, skills_dir: Path, skills_target: Path) -> None:
        """diff_skills() detects changed and new skills."""
        from sova.commands.distribution import diff_skills, install_skills
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(test_cmd="pytest", lint_cmd="ruff check .")
        install_skills(skills_dir, skills_target, cfg)

        (skills_dir / "alpha" / "SKILL.md").write_text("# Changed\n")
        diff = diff_skills(skills_dir, skills_target, cfg)
        assert "alpha/SKILL.md" in diff.changed

    def test_collect_skills_skips_non_directories(self, skills_dir: Path) -> None:
        """_collect_skills() skips files that are not directories."""
        from sova.commands.distribution import _collect_skills

        # Add a regular file in the skills directory
        (skills_dir / "stray-file.txt").write_text("not a skill")
        files = _collect_skills(skills_dir)
        keys = [k for k, _ in files]
        assert not any("stray-file" in k for k in keys)
        assert "alpha/SKILL.md" in keys

    def test_update_skills_empty_dir(self, tmp_path: Path, skills_target: Path) -> None:
        """update_skills() handles missing skills directory gracefully."""
        from sova.commands.distribution import update_skills
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(test_cmd="pytest", lint_cmd="ruff check .")
        result = update_skills(tmp_path / "nonexistent", skills_target, cfg)
        assert result.updated == 0

    def test_install_skills_empty_dir(self, tmp_path: Path, skills_target: Path) -> None:
        """install_skills() handles missing skills directory gracefully."""
        from sova.commands.distribution import install_skills
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(test_cmd="pytest", lint_cmd="ruff check .")
        result = install_skills(tmp_path / "nonexistent", skills_target, cfg)
        assert result.installed == 0


# ---------------------------------------------------------------------------
# Reverse diff (drift detection)
# ---------------------------------------------------------------------------


class TestReverseDiff:
    def test_no_manifest(self, canonical_dir: Path, target_dir: Path) -> None:
        """reverse_diff_commands() returns empty when no manifest exists."""
        from sova.commands.distribution import reverse_diff_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        result = reverse_diff_commands(canonical_dir, target_dir, cfg)
        assert result.modified == []
        assert result.deleted == []
        assert result.unmanaged == []

    def test_no_local_changes(self, canonical_dir: Path, target_dir: Path) -> None:
        """reverse_diff_commands() returns empty when nothing was modified."""
        from sova.commands.distribution import install_commands, reverse_diff_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        result = reverse_diff_commands(canonical_dir, target_dir, cfg)
        assert result.modified == []
        assert result.deleted == []

    def test_detects_modified(self, canonical_dir: Path, target_dir: Path) -> None:
        """reverse_diff_commands() detects locally modified managed files."""
        from sova.commands.distribution import install_commands, reverse_diff_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        (target_dir / "standup.md").write_text("# My improved standup\n")

        result = reverse_diff_commands(canonical_dir, target_dir, cfg)
        modified_names = [e.filename for e in result.modified]
        assert "standup.md" in modified_names

        entry = next(e for e in result.modified if e.filename == "standup.md")
        assert "My improved standup" in entry.local_content
        assert "Show standup." in entry.canonical_content

    def test_detects_deleted(self, canonical_dir: Path, target_dir: Path) -> None:
        """reverse_diff_commands() detects managed files deleted locally."""
        from sova.commands.distribution import install_commands, reverse_diff_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        (target_dir / "standup.md").unlink()

        result = reverse_diff_commands(canonical_dir, target_dir, cfg)
        assert "standup.md" in result.deleted

    def test_detects_unmanaged(self, canonical_dir: Path, target_dir: Path) -> None:
        """reverse_diff_commands() lists files not tracked by manifest."""
        from sova.commands.distribution import install_commands, reverse_diff_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        (target_dir / "my-local.md").write_text("# Local command\n")

        result = reverse_diff_commands(canonical_dir, target_dir, cfg)
        assert "my-local.md" in result.unmanaged

    def test_upstream_also_changed(self, canonical_dir: Path, target_dir: Path) -> None:
        """DriftEntry.upstream_also_changed is True when canonical also differs from manifest."""
        from sova.commands.distribution import install_commands, reverse_diff_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        (target_dir / "standup.md").write_text("# Local changes\n")
        (canonical_dir / "standup.md").write_text(
            "---\nname: standup\ndescription: Updated.\nuser-invocable: true\ncategory: management\n---\n\nUpstream.\n"
        )

        result = reverse_diff_commands(canonical_dir, target_dir, cfg)
        entry = next(e for e in result.modified if e.filename == "standup.md")
        assert entry.upstream_also_changed is True

    def test_upstream_not_changed(self, canonical_dir: Path, target_dir: Path) -> None:
        """DriftEntry.upstream_also_changed is False when only local was modified."""
        from sova.commands.distribution import install_commands, reverse_diff_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        (target_dir / "standup.md").write_text("# Local only\n")

        result = reverse_diff_commands(canonical_dir, target_dir, cfg)
        entry = next(e for e in result.modified if e.filename == "standup.md")
        assert entry.upstream_also_changed is False

    def test_canonical_removed(self, canonical_dir: Path, target_dir: Path) -> None:
        """When canonical file is removed but local exists, upstream_also_changed is True."""
        from sova.commands.distribution import install_commands, reverse_diff_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        (target_dir / "standup.md").write_text("# Modified locally\n")
        (canonical_dir / "standup.md").unlink()

        result = reverse_diff_commands(canonical_dir, target_dir, cfg)
        entry = next(e for e in result.modified if e.filename == "standup.md")
        assert entry.upstream_also_changed is True
        assert entry.canonical_content == ""
        assert entry.canonical_removed is True

    def test_unreadable_local_file_skipped_not_raised(self, canonical_dir: Path, target_dir: Path) -> None:
        """A managed local file with invalid UTF-8 bytes is skipped, not raised.

        Regression test: reverse_diff_commands() must not let UnicodeDecodeError
        propagate from a single corrupted managed file, since that would fail the
        whole diff (and the dashboard's installation-review endpoint) instead of
        just omitting the one unreadable entry.
        """
        from sova.commands.distribution import install_commands, reverse_diff_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        (target_dir / "standup.md").write_bytes(b"\xff\xfe not valid utf-8")

        result = reverse_diff_commands(canonical_dir, target_dir, cfg)
        assert "standup.md" not in [e.filename for e in result.modified]

    def test_unreadable_canonical_file_treated_as_removed(self, canonical_dir: Path, target_dir: Path) -> None:
        """A canonical source file with invalid UTF-8 bytes degrades like canonical_removed=True.

        Regression test: reverse_diff_commands() must not let UnicodeDecodeError
        propagate from a corrupted canonical file, since that would fail the whole
        diff (and the dashboard's installation-review endpoint). There's nothing to
        diff the local drift against, so it's treated the same as an upstream
        removal rather than aborting.
        """
        from sova.commands.distribution import install_commands, reverse_diff_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        (target_dir / "standup.md").write_text("# Modified locally\n")
        (canonical_dir / "standup.md").write_bytes(b"\xff\xfe not valid utf-8")

        result = reverse_diff_commands(canonical_dir, target_dir, cfg)
        entry = next(e for e in result.modified if e.filename == "standup.md")
        assert entry.canonical_removed is True
        assert entry.canonical_content == ""
        assert entry.upstream_also_changed is True

    def test_unreadable_canonical_guideline_treated_as_removed(self, guidelines_dir: Path, rules_dir: Path) -> None:
        """A canonical guideline file with invalid UTF-8 bytes degrades like canonical_removed=True.

        Regression test: unlike commands (whose corrupted canonical file is already
        filtered out by discover() before _reverse_diff_files() ever sees it, so
        canonical_removed=True is reached via the pre-existing "not in
        canonical_lookup" branch, see the sibling command test above), guidelines
        are collected by a plain glob with no pre-read, so this exercises
        _reverse_diff_files()'s own read-guard branch directly: the try/except
        around raw_canonical = canonical_path.read_text(...).
        """
        from sova.commands.distribution import install_guidelines, reverse_diff_guidelines
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(github_repo="owner/myapp", test_cmd="pytest", lint_cmd="ruff check .")
        install_guidelines(guidelines_dir, rules_dir, cfg)

        (rules_dir / "security.md").write_text("# Locally modified\n")
        (guidelines_dir / "security.md").write_bytes(b"\xff\xfe not valid utf-8")

        result = reverse_diff_guidelines(guidelines_dir, rules_dir, cfg)
        entry = next(e for e in result.modified if e.filename == "security.md")
        assert entry.canonical_removed is True
        assert entry.canonical_content == ""
        assert entry.upstream_also_changed is True

    def test_canonical_still_present_is_not_canonical_removed(self, canonical_dir: Path, target_dir: Path) -> None:
        """canonical_removed distinguishes "upstream changed" from "upstream removed":
        both set upstream_also_changed=True, but only the latter means the filename
        is gone from source_files and a "sync" of it would be a no-op."""
        from sova.commands.distribution import install_commands, reverse_diff_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        (target_dir / "standup.md").write_text("# Modified locally\n")
        (canonical_dir / "standup.md").write_text(
            "---\nname: standup\ndescription: Standup.\nuser-invocable: true\n"
            "category: management\n---\n\nAlso changed upstream.\n"
        )

        result = reverse_diff_commands(canonical_dir, target_dir, cfg)
        entry = next(e for e in result.modified if e.filename == "standup.md")
        assert entry.upstream_also_changed is True
        assert entry.canonical_removed is False

    def test_reverse_diff_guidelines(self, tmp_path: Path) -> None:
        """reverse_diff_guidelines() works for guidelines (same engine)."""
        from sova.commands.distribution import install_guidelines, reverse_diff_guidelines
        from sova.config.models import ProjectConfig

        guidelines_dir = tmp_path / "guidelines"
        guidelines_dir.mkdir()
        (guidelines_dir / "arch.md").write_text("Architecture: {{ project_name }}\n")

        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()

        cfg = ProjectConfig(github_repo="org/myapp")
        install_guidelines(guidelines_dir, rules_dir, cfg)

        (rules_dir / "arch.md").write_text("Architecture: myapp with extra notes\n")

        result = reverse_diff_guidelines(guidelines_dir, rules_dir, cfg)
        assert len(result.modified) == 1
        assert result.modified[0].filename == "arch.md"


# ---------------------------------------------------------------------------
# Reverse template rendering
# ---------------------------------------------------------------------------


class TestReverseRender:
    def test_basic(self) -> None:
        """reverse_render() replaces known values with template placeholders."""
        from sova.commands.templates import reverse_render

        content = "Run pytest to lint.\n"
        variables = {"test_cmd": "pytest"}
        result = reverse_render(content, variables)
        assert "{{ test_cmd }}" in result
        assert "pytest" not in result

    def test_multiple_variables(self) -> None:
        """reverse_render() handles multiple variables."""
        from sova.commands.templates import reverse_render

        content = "Run ruff check . then pytest.\n"
        variables = {"lint_cmd": "ruff check .", "test_cmd": "pytest"}
        result = reverse_render(content, variables)
        assert "{{ lint_cmd }}" in result
        assert "{{ test_cmd }}" in result

    def test_longer_values_first(self) -> None:
        """reverse_render() replaces longer values first to avoid partial matches."""
        from sova.commands.templates import reverse_render

        content = "Run ruff check . && pytest to check.\n"
        variables = {"check_cmd": "ruff check . && pytest", "lint_cmd": "ruff check ."}
        result = reverse_render(content, variables)
        assert "{{ check_cmd }}" in result

    def test_empty_variables(self) -> None:
        """reverse_render() is a no-op with empty dict."""
        from sova.commands.templates import reverse_render

        content = "Some content.\n"
        result = reverse_render(content, {})
        assert result == content

    def test_no_match(self) -> None:
        """reverse_render() leaves content unchanged when no values match."""
        from sova.commands.templates import reverse_render

        content = "Nothing to replace here.\n"
        variables = {"test_cmd": "pytest"}
        result = reverse_render(content, variables)
        assert result == content

    def test_overlapping_value_and_placeholder_name(self) -> None:
        """reverse_render() does not corrupt placeholders when values overlap names."""
        from sova.commands.templates import reverse_render

        content = "Clone org/repo for the repo"
        variables = {"github_repo": "org/repo", "project_name": "repo"}
        result = reverse_render(content, variables)
        assert result == "Clone {{ github_repo }} for the {{ project_name }}"


# ---------------------------------------------------------------------------
# CLI: drift and backport subcommands
# ---------------------------------------------------------------------------


class TestDriftCLI:
    def test_drift_help(self) -> None:
        """sova commands drift --help runs without error."""
        from typer.testing import CliRunner

        from sova.cli.app import app

        runner = CliRunner()
        result = runner.invoke(app, ["commands", "drift", "--help"])
        assert result.exit_code == 0
        assert "reverse diff" in result.output.lower()

    def _invoke_drift(self, canonical_dir: Path, target_dir: Path, extra_args: list[str] | None = None) -> object:
        """Helper to invoke drift_cmd with patched canonical dir and config."""
        from unittest.mock import patch

        from typer.testing import CliRunner

        from sova.cli.commands.commands import app as commands_app
        from sova.config.models import ProjectConfig

        runner = CliRunner()
        project_root = target_dir.parent.parent
        args = ["drift", "--project", str(project_root)] + (extra_args or [])
        with (
            patch("sova.cli.commands.commands.get_canonical_dir", return_value=canonical_dir),
            patch("sova.cli.commands.commands.load_config", return_value=ProjectConfig()),
        ):
            return runner.invoke(commands_app, args)

    def test_drift_no_drift(self, canonical_dir: Path, target_dir: Path) -> None:
        """sova commands drift shows clean message when no modifications exist."""
        from sova.commands.distribution import install_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        result = self._invoke_drift(canonical_dir, target_dir)
        assert result.exit_code == 0
        assert "No local drift" in result.output

    def test_drift_shows_modified(self, canonical_dir: Path, target_dir: Path) -> None:
        """sova commands drift shows modified files with diff output."""
        from sova.commands.distribution import install_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        (target_dir / "standup.md").write_text("# My improved standup\nNew content here.\n")

        result = self._invoke_drift(canonical_dir, target_dir)
        assert result.exit_code == 0
        assert "standup.md" in result.output
        assert "Locally modified" in result.output

    def test_drift_no_show_diff(self, canonical_dir: Path, target_dir: Path) -> None:
        """sova commands drift --no-show-diff shows only file names."""
        from sova.commands.distribution import install_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        (target_dir / "standup.md").write_text("# Changed\n")

        result = self._invoke_drift(canonical_dir, target_dir, ["--no-show-diff"])
        assert result.exit_code == 0
        assert "standup.md" in result.output

    def test_drift_shows_unmanaged(self, canonical_dir: Path, target_dir: Path) -> None:
        """sova commands drift shows unmanaged files."""
        from sova.commands.distribution import install_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        (target_dir / "my-local.md").write_text("# Local\n")

        result = self._invoke_drift(canonical_dir, target_dir)
        assert result.exit_code == 0
        assert "my-local.md" in result.output
        assert "Unmanaged" in result.output

    def test_drift_shows_deleted(self, canonical_dir: Path, target_dir: Path) -> None:
        """sova commands drift shows deleted managed files."""
        from sova.commands.distribution import install_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        (target_dir / "standup.md").unlink()

        result = self._invoke_drift(canonical_dir, target_dir)
        assert result.exit_code == 0
        assert "standup.md" in result.output
        assert "deleted" in result.output.lower()

    def test_drift_upstream_also_changed(self, canonical_dir: Path, target_dir: Path) -> None:
        """sova commands drift annotates when upstream also changed."""
        from sova.commands.distribution import install_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        (target_dir / "standup.md").write_text("# Local\n")
        (canonical_dir / "standup.md").write_text(
            "---\nname: standup\ndescription: Changed.\nuser-invocable: true\ncategory: management\n---\n\nNew.\n"
        )

        result = self._invoke_drift(canonical_dir, target_dir)
        assert result.exit_code == 0
        assert "upstream also changed" in result.output

    def test_backport_help(self) -> None:
        """sova commands backport --help runs without error."""
        from typer.testing import CliRunner

        from sova.cli.app import app

        runner = CliRunner()
        result = runner.invoke(app, ["commands", "backport", "--help"])
        assert result.exit_code == 0
        assert "back-port" in result.output.lower()

    def test_backport_dry_run(self, canonical_dir: Path, target_dir: Path) -> None:
        """sova commands backport --dry-run shows content without writing."""
        from unittest.mock import patch

        from sova.commands.distribution import install_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(test_cmd="pytest", lint_cmd="ruff check .")
        install_commands(canonical_dir, target_dir, cfg)

        (target_dir / "standup.md").write_text("# Improved standup with pytest\n")

        from typer.testing import CliRunner

        from sova.cli.commands.commands import app as commands_app

        runner = CliRunner()
        project_root = target_dir.parent.parent
        with patch("sova.cli.commands.commands.get_canonical_dir", return_value=canonical_dir):
            result = runner.invoke(
                commands_app, ["backport", "standup.md", "--dry-run", "--project", str(project_root)]
            )
        assert result.exit_code == 0
        assert "Would write to" in result.output

    def test_backport_file_not_found(self, canonical_dir: Path, target_dir: Path) -> None:
        """sova commands backport reports error for missing files."""
        from sova.commands.distribution import install_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        from typer.testing import CliRunner

        from sova.cli.commands.commands import app as commands_app

        runner = CliRunner()
        project_root = target_dir.parent.parent
        result = runner.invoke(commands_app, ["backport", "nonexistent.md", "--project", str(project_root)])
        assert result.exit_code == 1

    def test_backport_invalid_kind(self, canonical_dir: Path, target_dir: Path) -> None:
        """sova commands backport reports error for invalid kind."""
        from sova.commands.distribution import install_commands
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        install_commands(canonical_dir, target_dir, cfg)

        from typer.testing import CliRunner

        from sova.cli.commands.commands import app as commands_app

        runner = CliRunner()
        project_root = target_dir.parent.parent
        result = runner.invoke(
            commands_app, ["backport", "standup.md", "--kind", "invalid", "--project", str(project_root)]
        )
        assert result.exit_code == 1
