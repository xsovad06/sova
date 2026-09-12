"""Structural validation for the plugins/sova/ Claude Code plugin package.

Validates the package SOVA publishes to the Claude AI Helpers Marketplace
(https://github.com/openshift-eng/ai-helpers) against that marketplace's
documented plugin.json / OWNERS / command frontmatter conventions, and
against SOVA's own root-level marketplace.json used for direct install.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

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


class TestPluginCommands:
    def test_at_least_six_commands(self) -> None:
        assert len(_command_files()) >= 6

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
