"""Command discovery and classification.

Scans a directory of markdown command files, parses YAML frontmatter,
and groups commands by category.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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
        user_invocable=fields.get("user-invocable", "false").lower() in ("true", "yes"),
        path=path,
    )


def _parse_yaml_simple(text: str) -> dict[str, str]:
    """Minimal YAML-like key: value parser for frontmatter.

    Only handles simple scalar values (no nesting, no lists).
    """
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip()
    return result
