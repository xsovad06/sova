"""Structural validation for the plugins/sova/ Claude Code plugin package.

Validates the package SOVA publishes to the Claude AI Helpers Marketplace
(https://github.com/openshift-eng/ai-helpers) against that marketplace's
documented plugin.json / OWNERS / command frontmatter conventions, and
against SOVA's own root-level marketplace.json used for direct install.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path

import pytest

from sova.commands import marketplace_export

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_DIR = _REPO_ROOT / "plugins" / "sova"
_COMMANDS_DIR = _PLUGIN_DIR / "commands"
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)
_TEMPLATE_VAR_RE = re.compile(r"\{\{\s*\w+\s*\}\}")
_REQUIRED_SECTIONS = ("## Name", "## Synopsis", "## Description", "## Implementation")


def _parse_frontmatter(path: Path) -> tuple[dict[str, str], str]:
    content = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(content)
    assert match, f"{path} is missing YAML frontmatter delimited by '---'"
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        fields[key.strip()] = value.strip().strip('"')
    return fields, match.group(2)


def _command_files() -> list[Path]:
    return sorted(_COMMANDS_DIR.glob("*.md"))


class TestPluginManifest:
    def test_plugin_json_is_valid(self) -> None:
        path = _PLUGIN_DIR / ".claude-plugin" / "plugin.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["name"] == "sova"
        assert data["description"]
        assert re.match(r"^\d+\.\d+\.\d+$", data["version"])
        assert data["author"]["name"]

    def test_plugin_json_version_matches_pyproject(self) -> None:
        """plugin.json's version must be derived from pyproject.toml, not hand-set.

        Prevents the version drift called out in issue #324's review: a hardcoded
        "1.0.0" that never tracked the package's actual version.
        """
        path = _PLUGIN_DIR / ".claude-plugin" / "plugin.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert data["version"] == pyproject["project"]["version"]

    def test_owners_file_present(self) -> None:
        content = (_PLUGIN_DIR / "OWNERS").read_text(encoding="utf-8")
        assert "approvers:" in content
        assert "reviewers:" in content
        assert "ai-helpers-admins" in content

    def test_readme_present(self) -> None:
        content = (_PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
        assert "/plugin install sova@" in content


class TestRootMarketplace:
    def test_marketplace_json_registers_sova_plugin(self) -> None:
        path = _REPO_ROOT / ".claude-plugin" / "marketplace.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        plugins = {p["name"]: p for p in data["plugins"]}
        assert "sova" in plugins
        assert plugins["sova"]["source"] == "./plugins/sova"

    def test_marketplace_json_matches_plugin_json(self) -> None:
        """description/version must be copied from plugin.json, not hand-duplicated.

        plugin.json is the single source of truth for these fields (see
        marketplace_export._sync_marketplace_json); a hand-edited marketplace.json
        entry would silently diverge from the plugin it describes.
        """
        marketplace_data = json.loads((_REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
        plugin_data = json.loads((_PLUGIN_DIR / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        entry = next(p for p in marketplace_data["plugins"] if p["name"] == "sova")
        assert entry["description"] == plugin_data["description"]
        assert entry["version"] == plugin_data["version"]


class TestPluginCommands:
    def test_command_files_match_selected_commands(self) -> None:
        stems = {p.stem for p in _command_files()}
        assert stems == set(marketplace_export.SELECTED_COMMANDS)

    @pytest.mark.parametrize("path", _command_files(), ids=lambda p: p.stem)
    def test_frontmatter_has_required_fields(self, path: Path) -> None:
        fields, _ = _parse_frontmatter(path)
        assert fields.get("description"), f"{path} frontmatter missing 'description'"
        assert "argument-hint" in fields, f"{path} frontmatter missing 'argument-hint'"

    @pytest.mark.parametrize("path", _command_files(), ids=lambda p: p.stem)
    def test_body_has_required_sections(self, path: Path) -> None:
        _, body = _parse_frontmatter(path)
        for section in _REQUIRED_SECTIONS:
            assert section in body, f"{path} is missing required section '{section}'"

    @pytest.mark.parametrize("path", _command_files(), ids=lambda p: p.stem)
    def test_name_section_matches_plugin_prefix(self, path: Path) -> None:
        _, body = _parse_frontmatter(path)
        assert f"sova:{path.stem}" in body, f"{path} 'Name' section must be 'sova:{path.stem}'"

    @pytest.mark.parametrize("path", _command_files(), ids=lambda p: p.stem)
    def test_no_sova_template_variables_leak(self, path: Path) -> None:
        content = path.read_text(encoding="utf-8")
        assert not _TEMPLATE_VAR_RE.search(content), (
            f"{path} contains an unresolved SOVA template variable "
            "(e.g. {{ check_cmd }}); standalone marketplace users have no renderer for it"
        )

    @pytest.mark.parametrize("path", _command_files(), ids=lambda p: p.stem)
    def test_no_hard_sova_dependencies(self, path: Path) -> None:
        content = path.read_text(encoding="utf-8")
        for forbidden in ("sova.toml", "sova install", "SOVA pipeline"):
            assert forbidden not in content, f"{path} references SOVA-specific setup: {forbidden!r}"


class TestNoDrift:
    """Guards against plugins/sova/commands/*.md drifting from commands/*.md.

    Issue #324's review flagged that hand-authored plugin commands have no
    mechanism to catch them silently diverging from the canonical source they're
    supposed to package. marketplace_export.render_command() mechanically derives
    the marketplace file from the canonical commands/{name}.md; if a maintainer
    edits either side without running `make marketplace`, this test fails.
    """

    @pytest.mark.parametrize("name", marketplace_export.SELECTED_COMMANDS)
    def test_generated_command_matches_source(self, name: str) -> None:
        checked_in = (_COMMANDS_DIR / f"{name}.md").read_text(encoding="utf-8")
        regenerated = marketplace_export.render_command(name)
        assert checked_in == regenerated, (
            f"plugins/sova/commands/{name}.md is out of sync with commands/{name}.md. "
            "Run `make marketplace` and commit the result."
        )

    def test_manifest_source_hashes_match_current_commands(self) -> None:
        manifest = json.loads((_PLUGIN_DIR / ".marketplace-manifest.json").read_text(encoding="utf-8"))
        for rel_path, recorded_hash in manifest["source_hashes"].items():
            actual = hashlib.sha256((_REPO_ROOT / rel_path).read_bytes()).hexdigest()
            assert actual == recorded_hash, (
                f"{rel_path} changed since the marketplace package was last generated. "
                "Run `make marketplace` and commit the result."
            )

    def test_manifest_covers_exactly_the_selected_commands(self) -> None:
        manifest = json.loads((_PLUGIN_DIR / ".marketplace-manifest.json").read_text(encoding="utf-8"))
        recorded = {Path(p).stem for p in manifest["source_hashes"]}
        assert recorded == set(marketplace_export.SELECTED_COMMANDS)


class TestExportEndToEnd:
    """Exercises export()/main() against an isolated fixture repo, not the real one.

    The structural tests above only read files already generated by a prior
    `make marketplace` run; none of them call export()/main() themselves, leaving
    the write path (export, _sync_marketplace_json, _build_plugin_json, main) and
    render_command's error branch uncovered. Every path constant is monkeypatched
    to a tmp_path fixture so this never touches the real repo tree.
    """

    @staticmethod
    def _write_command(path: Path, description: str = "Test command") -> None:
        path.write_text(
            f"---\nname: test\ndescription: {description}\ncategory: core\n---\n## Name\n\ntest\n\nBody text.\n",
            encoding="utf-8",
        )

    def _isolate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str = "9.9.9") -> dict[str, Path]:
        repo_root = tmp_path
        commands_dir = repo_root / "commands"
        commands_dir.mkdir()
        plugin_dir = repo_root / "plugins" / "sova"
        plugin_commands_dir = plugin_dir / "commands"
        plugin_json_path = plugin_dir / ".claude-plugin" / "plugin.json"
        plugin_json_path.parent.mkdir(parents=True)
        marketplace_json_path = repo_root / ".claude-plugin" / "marketplace.json"
        marketplace_json_path.parent.mkdir(parents=True)
        manifest_path = plugin_dir / ".marketplace-manifest.json"
        pyproject_path = repo_root / "pyproject.toml"

        for name in marketplace_export.SELECTED_COMMANDS:
            self._write_command(commands_dir / f"{name}.md")

        marketplace_json_path.write_text(
            json.dumps(
                {
                    "plugins": [
                        {
                            "name": "sova",
                            "description": "stale",
                            "version": "0.0.0",
                            "source": "./plugins/sova",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        pyproject_path.write_text(f'[project]\nname = "sova"\nversion = "{version}"\n', encoding="utf-8")

        monkeypatch.setattr(marketplace_export, "_REPO_ROOT", repo_root)
        monkeypatch.setattr(marketplace_export, "_COMMANDS_DIR", commands_dir)
        monkeypatch.setattr(marketplace_export, "_PLUGIN_DIR", plugin_dir)
        monkeypatch.setattr(marketplace_export, "_PLUGIN_COMMANDS_DIR", plugin_commands_dir)
        monkeypatch.setattr(marketplace_export, "_PLUGIN_JSON_PATH", plugin_json_path)
        monkeypatch.setattr(marketplace_export, "_MARKETPLACE_JSON_PATH", marketplace_json_path)
        monkeypatch.setattr(marketplace_export, "_MANIFEST_PATH", manifest_path)
        monkeypatch.setattr(marketplace_export, "_PYPROJECT_PATH", pyproject_path)

        return {
            "plugin_commands_dir": plugin_commands_dir,
            "plugin_json_path": plugin_json_path,
            "marketplace_json_path": marketplace_json_path,
            "manifest_path": manifest_path,
        }

    def test_export_writes_plugin_json_marketplace_json_and_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        paths = self._isolate(tmp_path, monkeypatch)

        hashes = marketplace_export.export()

        assert marketplace_export.read_pyproject_version() == "9.9.9"

        plugin_json = json.loads(paths["plugin_json_path"].read_text(encoding="utf-8"))
        assert plugin_json["name"] == "sova"
        assert plugin_json["version"] == "9.9.9"

        marketplace_json = json.loads(paths["marketplace_json_path"].read_text(encoding="utf-8"))
        entry = marketplace_json["plugins"][0]
        assert entry["version"] == "9.9.9"
        assert entry["description"] == plugin_json["description"]
        assert entry["source"] == "./plugins/sova"  # untouched marketplace-only field

        manifest = json.loads(paths["manifest_path"].read_text(encoding="utf-8"))
        assert manifest["version"] == "9.9.9"
        assert set(hashes) == {f"commands/{name}.md" for name in marketplace_export.SELECTED_COMMANDS}
        assert manifest["source_hashes"] == hashes

        for name in marketplace_export.SELECTED_COMMANDS:
            assert (paths["plugin_commands_dir"] / f"{name}.md").exists()

    def test_export_deletes_obsolete_command_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        paths = self._isolate(tmp_path, monkeypatch)
        paths["plugin_commands_dir"].mkdir(parents=True, exist_ok=True)
        stale_path = paths["plugin_commands_dir"] / "retired-command.md"
        self._write_command(stale_path)

        marketplace_export.export()

        assert not stale_path.exists()
        stems = {p.stem for p in paths["plugin_commands_dir"].glob("*.md")}
        assert stems == set(marketplace_export.SELECTED_COMMANDS)

    def test_main_prints_export_summary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._isolate(tmp_path, monkeypatch)

        marketplace_export.main()

        out = capsys.readouterr().out
        assert f"Exported {len(marketplace_export.SELECTED_COMMANDS)} commands" in out

    def test_render_command_raises_on_missing_frontmatter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        commands_dir = tmp_path / "commands"
        commands_dir.mkdir()
        (commands_dir / "develop.md").write_text("no frontmatter here\n", encoding="utf-8")
        monkeypatch.setattr(marketplace_export, "_COMMANDS_DIR", commands_dir)

        with pytest.raises(ValueError, match="no valid frontmatter"):
            marketplace_export.render_command("develop")
