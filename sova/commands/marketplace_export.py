"""Export SOVA's canonical commands/ directory to the plugins/sova/ marketplace package.

SOVA's `commands/` directory (28+ files) is the single source of truth for its
distributable workflow commands. `plugins/sova/` is a subset of those commands,
repackaged in the format the Claude AI Helpers Marketplace
(https://github.com/openshift-eng/ai-helpers) expects (`## Name` / `## Synopsis` /
`## Description` / `## Implementation` sections, plus `argument-hint` and `example`
frontmatter fields instead of SOVA's `name` / `user-invocable`).

This module mechanically derives the marketplace package from the canonical
commands and from `pyproject.toml`, so it cannot drift the way independently
hand-authored copies did (issue #324). Run it via `make marketplace`.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path

from sova.commands.catalog import parse_frontmatter

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_COMMANDS_DIR = _REPO_ROOT / "commands"
_PLUGIN_DIR = _REPO_ROOT / "plugins" / "sova"
_PLUGIN_COMMANDS_DIR = _PLUGIN_DIR / "commands"
_PLUGIN_JSON_PATH = _PLUGIN_DIR / ".claude-plugin" / "plugin.json"
_MARKETPLACE_JSON_PATH = _REPO_ROOT / ".claude-plugin" / "marketplace.json"
_MANIFEST_PATH = _PLUGIN_DIR / ".marketplace-manifest.json"
_PYPROJECT_PATH = _REPO_ROOT / "pyproject.toml"

PLUGIN_NAME = "sova"
PLUGIN_DESCRIPTION = (
    "Autonomous AI-assisted development workflow commands: TDD development, "
    "spec-first planning, pre-push review, PR creation, and systematic debugging"
)
PLUGIN_AUTHOR = "SOVA"

# The 6 commands selected for standalone marketplace publication (issue #324): the
# self-contained workflow commands that need no SOVA infrastructure (DB, handoff
# system, adapters) and therefore work in any Claude Code project.
SELECTED_COMMANDS = ["develop", "spec", "review", "pr", "debug", "test"]

# Illustrative example invocations. These cannot be derived from a command's frontmatter
# or body (they require picking a realistic argument), so they are the one piece of
# per-command metadata authored by hand rather than mechanically extracted from the source.
_EXAMPLES = {
    "develop": "/sova:develop Add rate limiting to the login endpoint",
    "spec": "/sova:spec Add rate limiting to the login endpoint",
    "review": "/sova:review",
    "pr": "/sova:pr",
    "debug": "/sova:debug Login returns 500 when the email has a plus sign",
    "test": "/sova:test",
}

# Mechanical text substitutions applied to every canonical command body: SOVA template
# variables (rendered by SOVA's own distribution system at install time, meaningless to a
# standalone marketplace user) and SOVA-specific config file references are replaced with
# generic, project-agnostic wording.
_SUBSTITUTIONS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\{\{\s*check_cmd\s*\}\}"),
        "the project's CI-equivalent check command (see its Makefile, package.json scripts, or CI config)",
    ),
    (
        re.compile(r"\{\{\s*lint_cmd\s*\}\}"),
        "the project's lint command (see its Makefile, package.json scripts, or CI config)",
    ),
    (
        re.compile(r"\{\{\s*test_cmd\s*\}\}"),
        "the project's test command (see its Makefile, package.json scripts, or CI config)",
    ),
    (
        re.compile(r"reading `sova\.toml` \(if it exists\) and checking"),
        "reading the project's own config file, if present, and checking",
    ),
    (re.compile(r"or no sova\.toml"), "or no such config file"),
    (
        re.compile(r"Read `sova\.toml` to check `\[task_source\] type` if it exists\."),
        "Read the project's own config file, if present, to check `[task_source] type`.",
    ),
    (re.compile(r"Check `sova\.toml` for"), "Check the project's own config file, if present, for"),
]


def read_pyproject_version() -> str:
    """Read `[project].version` from pyproject.toml, avoiding a hardcoded, drift-prone value."""
    data = tomllib.loads(_PYPROJECT_PATH.read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def _apply_substitutions(body: str) -> str:
    for pattern, replacement in _SUBSTITUTIONS:
        body = pattern.sub(replacement, body)
    return body


_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_DOUBLE_DASH_RE = re.compile(r" -{2} ")


def _dedash_prose(text: str) -> str:
    """Replace space-dash-dash-space prose separators (AGENTS.md forbids them, see invariants/no-double-dash.sh).

    Fenced code blocks are left untouched, mirroring the invariant's own exemption for them:
    a real shell `--` flag or example inside a fence must not be rewritten.
    """
    in_fence = False
    lines = []
    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            lines.append(line)
            continue
        lines.append(line if in_fence else _DOUBLE_DASH_RE.sub(": ", line))
    return "\n".join(lines)


def _argument_hint(inputs: list[str]) -> str:
    return "<" + "|".join(inputs) + ">" if inputs else ""


def _as_str_list(value: object) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def render_command(name: str) -> str:
    """Mechanically transform a canonical commands/{name}.md into marketplace format."""
    source = _COMMANDS_DIR / f"{name}.md"
    parsed = parse_frontmatter(source.read_text(encoding="utf-8"))
    if parsed is None:
        raise ValueError(f"{source} has no valid frontmatter")
    fields, body = parsed

    description = _dedash_prose(str(fields.get("description", "")))
    category = str(fields.get("category", "core"))
    inputs = _as_str_list(fields.get("inputs"))
    outputs = _as_str_list(fields.get("outputs"))
    argument_hint = _argument_hint(inputs)
    example = _EXAMPLES[name]
    body = _dedash_prose(_apply_substitutions(body)).strip("\n")

    frontmatter_lines = [
        "---",
        f"description: {description}",
        f'argument-hint: "{argument_hint}"',
        f'example: "{example}"',
        f"category: {category}",
    ]
    if inputs:
        frontmatter_lines.append("inputs:")
        frontmatter_lines.extend(f"  - {i}" for i in inputs)
    if outputs:
        frontmatter_lines.append("outputs:")
        frontmatter_lines.extend(f"  - {o}" for o in outputs)
    frontmatter_lines.append("---")

    synopsis = f"/sova:{name} {argument_hint}".strip()

    sections = [
        "\n".join(frontmatter_lines),
        "",
        "## Name",
        f"sova:{name}",
        "",
        "## Synopsis",
        "```",
        synopsis,
        "```",
        "",
        "## Description",
        "",
        description,
        "",
        "## Implementation",
        "",
        body,
        "",
    ]
    if inputs:
        sections += ["## Arguments", ""]
        sections += [f"- `{i}`" for i in inputs]
        sections += [""]
    sections += ["## Examples", "", "```", example, "```", ""]

    return "\n".join(sections).rstrip() + "\n"


def _build_plugin_json(version: str) -> dict[str, object]:
    return {
        "name": PLUGIN_NAME,
        "description": PLUGIN_DESCRIPTION,
        "version": version,
        "author": {"name": PLUGIN_AUTHOR},
    }


def _sync_marketplace_json(plugin_json: dict[str, object]) -> None:
    """Update the sova entry in the root marketplace.json from plugin.json.

    name/description/version are kept in sync with plugin.json (the single source of
    truth for those fields); category/keywords are marketplace-only fields with no
    equivalent in plugin.json and are preserved as-is.
    """
    data = json.loads(_MARKETPLACE_JSON_PATH.read_text(encoding="utf-8"))
    for entry in data.get("plugins", []):
        if entry.get("name") == PLUGIN_NAME:
            entry["description"] = plugin_json["description"]
            entry["version"] = plugin_json["version"]
            break
    _MARKETPLACE_JSON_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def export() -> dict[str, str]:
    """Regenerate the marketplace package and return {relative_path: sha256} for the manifest."""
    version = read_pyproject_version()
    plugin_json = _build_plugin_json(version)
    _PLUGIN_JSON_PATH.write_text(json.dumps(plugin_json, indent=2) + "\n", encoding="utf-8")
    _sync_marketplace_json(plugin_json)

    _PLUGIN_COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
    selected = set(SELECTED_COMMANDS)
    for existing in _PLUGIN_COMMANDS_DIR.glob("*.md"):
        if existing.stem not in selected:
            existing.unlink()

    source_hashes: dict[str, str] = {}
    for name in SELECTED_COMMANDS:
        source_path = _COMMANDS_DIR / f"{name}.md"
        source_hashes[f"commands/{name}.md"] = hashlib.sha256(source_path.read_bytes()).hexdigest()
        rendered = render_command(name)
        (_PLUGIN_COMMANDS_DIR / f"{name}.md").write_text(rendered, encoding="utf-8")

    manifest = {"version": version, "source_hashes": source_hashes}
    _MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return source_hashes


def main() -> None:
    hashes = export()
    print(f"Exported {len(hashes)} commands to {_PLUGIN_COMMANDS_DIR.relative_to(_REPO_ROOT)}")


if __name__ == "__main__":
    main()
