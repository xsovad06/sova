"""Command discovery and classification.

Scans a directory of markdown command files, parses YAML frontmatter,
and groups commands by category.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from sova.utils.logging import get_logger

log = get_logger(component="commands.catalog")

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class CommandEntry:
    """A single command file with parsed metadata."""

    name: str
    description: str
    category: str
    user_invocable: bool
    path: Path
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)


def discover(commands_dir: Path) -> list[CommandEntry]:
    """Find all valid command .md files in a directory.

    Parses YAML frontmatter for name, description, category, and user-invocable.
    Skips files without valid frontmatter.
    """
    entries: list[CommandEntry] = []

    if not commands_dir.is_dir():
        log.warning("commands.dir_not_found", path=str(commands_dir))
        return entries

    for path in sorted(commands_dir.glob("*.md")):
        entry = _parse_command_file(path)
        if entry is not None:
            entries.append(entry)

    return entries


def get_canonical_dir() -> Path:
    """Return the path to the canonical commands directory in the SOVA repo."""
    return Path(__file__).resolve().parent.parent.parent / "commands"


def classify(commands: list[CommandEntry]) -> dict[str, list[CommandEntry]]:
    """Group commands by their category."""
    groups: dict[str, list[CommandEntry]] = {}
    for cmd in commands:
        groups.setdefault(cmd.category, []).append(cmd)
    return groups


def _parse_command_file(path: Path) -> CommandEntry | None:
    """Parse a command markdown file and extract frontmatter metadata."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        log.warning("commands.read_error", path=str(path))
        return None

    match = _FRONTMATTER_RE.match(content)
    if not match:
        log.debug("commands.no_frontmatter", path=str(path))
        return None

    frontmatter = match.group(1)
    fields = _parse_yaml_simple(frontmatter)

    name = fields.get("name", "")
    if not name:
        log.debug("commands.no_name", path=str(path))
        return None

    return CommandEntry(
        name=name,
        description=fields.get("description", ""),
        category=fields.get("category", "core"),
        user_invocable=str(fields.get("user-invocable", "false")).lower() in ("true", "yes"),
        path=path,
        inputs=fields.get("inputs", []) if isinstance(fields.get("inputs"), list) else [],
        outputs=fields.get("outputs", []) if isinstance(fields.get("outputs"), list) else [],
    )


def _parse_yaml_simple(text: str) -> dict[str, str | list[str]]:
    """Minimal YAML-like key: value parser for frontmatter.

    Handles scalar values and simple YAML lists (lines starting with ``- ``).
    """
    result: dict[str, str | list[str]] = {}
    current_list_key: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # List item: "  - value"
        if stripped.startswith("- ") and current_list_key is not None:
            val = stripped[2:].strip()
            lst = result[current_list_key]
            if isinstance(lst, list):
                lst.append(val)
            continue
        # Scalar or list key
        if ":" in stripped:
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip()
            if value:
                result[key] = value
                current_list_key = None
            else:
                # Empty value -- next lines may be list items
                result[key] = []
                current_list_key = key
        else:
            current_list_key = None
    return result
