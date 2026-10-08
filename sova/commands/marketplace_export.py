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
from sova.commands.templates import dedash_prose, workflow_reference_re

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
    # {{ arguments }} is the provider-neutral counterpart to Claude's own
    # $ARGUMENTS token (see sova.commands.templates.build_variables()). A
    # standalone marketplace user runs these as real Claude Code slash
    # commands, so the literal $ARGUMENTS token is exactly what they need,
    # same as SOVA's own .claude/commands/ install path.
    (re.compile(r"\{\{\s*arguments\s*\}\}"), "$ARGUMENTS"),
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
    # Commands outside SELECTED_COMMANDS do not ship with the plugin, so an
    # instruction naming one is a dead end for a standalone user. Rewrite each to
    # describe the action instead. `/verify-local` needs no entry: the canonical
    # text already guards it with "If the project has a `/verify-local` command"
    # and "Skip if no `/verify-local` command exists".
    #
    # These match the provider-neutral "`name` workflow" cross-reference syntax
    # (see sova.commands.templates.workflow_reference_re()), after
    # _IN_PLUGIN_WORKFLOW_RE (applied in _apply_substitutions()) has already
    # turned any reference to a bundled command (develop/spec/review/pr/debug/test)
    # into a real `/name` slash reference; only out-of-plugin names reach these
    # patterns still in the neutral form.
    (
        re.compile(r"Run the `find-task` workflow to pick the next issue"),
        "Select the next issue from the project's task source",
    ),
    (
        re.compile(r"Run `/develop` or the `develop-full` workflow with the issue number to implement"),
        "Run `/develop` with the issue number to implement; for the full cycle, continue with "
        "`/test`, `/review`, and `/pr`",
    ),
    (
        re.compile(r"Use the `develop-full` workflow instead for end-to-end \(develop \+ test \+ review \+ PR\)"),
        "For end-to-end work, run `/develop`, `/test`, `/review`, and `/pr` in sequence",
    ),
    (
        re.compile(r"Use the `develop-full` workflow for the complete develop-test-review-pr cycle"),
        "For the complete develop-test-review-pr cycle, run `/develop`, `/test`, `/review`, and `/pr` in sequence",
    ),
    (
        re.compile(
            r"Run the `extract-knowledge` workflow to capture any reusable patterns, gotchas, or lessons "
            r"into the project's knowledge system\."
        ),
        "Document reusable patterns, gotchas, or lessons in an appropriate project knowledge document.",
    ),
    (
        re.compile(r"the `develop-full` workflow \(Phase 2\) or manual pre-push check"),
        "`/develop`, or a manual pre-push check",
    ),
    (
        re.compile(r"Use the `review-pr` workflow instead"),
        "Use the review steps above to inspect the PR diff and changed files instead",
    ),
    (
        re.compile(r"Run the `review-pr` workflow against this PR to review the actual diff that will be merged:"),
        "Review the actual diff that will be merged, as a senior engineer would:",
    ),
    (
        re.compile(r"Execute the `review-pr` workflow's analysis in full \(fetch diff, read files, deep analysis\)"),
        "Fetch the diff, read every changed file, and analyse it deeply",
    ),
    (re.compile(r"Run the `address-pr` workflow to fix the findings:"), "Fix the findings:"),
    (re.compile(r"Status: ready for /integrate-pr"), "Status: ready to merge"),
    (
        re.compile(r"Run `/review` or the `review-full` workflow to catch issues before pushing"),
        "Run `/review` to catch issues before pushing",
    ),
    (
        re.compile(
            r"the `develop-full` workflow -> the `review-full` workflow -> `/pr` -> the `integrate-pr` workflow"
        ),
        "`/develop` -> `/review` -> `/pr`",
    ),
    (
        re.compile(r"Run the `integrate-pr` workflow for merge, cleanup, and knowledge extraction"),
        "merge the PR, delete the branch, and capture anything learned",
    ),
    (
        re.compile(r"Run the `rearrange-commits` workflow"),
        "Reorganize the branch's commits into clean, logical units",
    ),
    (
        re.compile(r"NEVER merge the PR: that happens via the `integrate-pr` workflow or `/approve-merge`"),
        "NEVER merge the PR yourself unless the user explicitly asks",
    ),
    # A standalone plugin user has neither the `sova` CLI nor the importable
    # sova package, so the SOVA-specific config lookups must become generic
    # instructions. Dropped the four `sova.toml` patterns these replace: #1096
    # rewrote that prose, so they matched nothing and silently did nothing.
    (
        re.compile(
            r"Determine the task source by running `sova config` and reading the `task_source` row "
            r"\(configuration lives in `\.claude/sova\.db`, not in a file\)\."
        ),
        "Determine the task source (GitHub, Jira, or other) from the project's own config or conventions.",
    ),
    (
        re.compile(
            r"This key is not in the `sova config` table, so resolve it through the config loader:\n"
            r"    ```bash\n"
            r"    python3 -c \"from pathlib import Path; from sova\.config\.loader import load_config; \\\n"
            r"print\(load_config\(Path\('\.'\)\)\.external_reviews\.coderabbit\.trigger_review\)\"\n"
            r"    ```\n"
            r"    If it prints `True` and the PR"
        ),
        "If the project uses an automated reviewer such as CodeRabbit and the PR",
    ),
]


def read_pyproject_version() -> str:
    """Read `[project].version` from pyproject.toml, avoiding a hardcoded, drift-prone value."""
    data = tomllib.loads(_PYPROJECT_PATH.read_text(encoding="utf-8"))
    return str(data["project"]["version"])


# A bare cross-reference to one of this plugin's own bundled commands (the
# provider-neutral `name` workflow syntax; see
# sova.commands.templates.workflow_reference_re()) becomes a real `/name` slash
# reference, since every SELECTED_COMMANDS entry ships inside this same
# plugin. Applied before _SUBSTITUTIONS, which hand-rewrites references to
# commands this plugin does NOT bundle. A leading "the " is swallowed too, so
# "the `test` workflow" becomes `/test` rather than the redundant "the `/test`".
_IN_PLUGIN_WORKFLOW_RE = [
    (re.compile(rf"(?:the\s+)?{workflow_reference_re(name).pattern}"), f"`/{name}`") for name in SELECTED_COMMANDS
]


def _apply_substitutions(body: str) -> str:
    for pattern, replacement in _IN_PLUGIN_WORKFLOW_RE:
        body = pattern.sub(replacement, body)
    for pattern, replacement in _SUBSTITUTIONS:
        body = pattern.sub(replacement, body)
    return body


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

    description = dedash_prose(_apply_substitutions(str(fields.get("description", ""))))
    category = str(fields.get("category", "core"))
    inputs = _as_str_list(fields.get("inputs"))
    outputs = _as_str_list(fields.get("outputs"))
    argument_hint = _argument_hint(inputs)
    example = _EXAMPLES[name]
    body = dedash_prose(_apply_substitutions(body)).strip("\n")

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
