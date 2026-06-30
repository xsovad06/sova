"""Configuration loading for SOVA.

Supports:
- sova.toml (primary, TOML format)
- Environment variable overrides (SOVA_ prefix)

"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sova.config.models import (
    ProjectConfig,
)

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]


def load_config(project_dir: Path | None = None) -> ProjectConfig:
    """Load project configuration from TOML file.

    Search order:
    1. sova.toml in project_dir
    2. Default values
    """
    if project_dir is None:
        project_dir = Path.cwd()

    project_dir = project_dir.resolve()

    # Try TOML first
    toml_path = project_dir / "sova.toml"
    if toml_path.exists():
        return _load_from_toml(toml_path)

    # Default config
    return ProjectConfig()


def _load_from_toml(path: Path) -> ProjectConfig:
    """Load configuration from a TOML file."""
    with open(path, "rb") as f:
        data = tomllib.load(f)

    flat = _flatten_toml(data)
    _migrate_deprecated_keys(flat)
    return ProjectConfig(**flat)


_NESTED_SECTIONS = (
    "llm",
    "task_source",
    "agent",
    "review",
    "develop",
    "ci",
    "watch",
    "worktree",
    "commit",
    "triage",
    "roles",
    "pipeline",
    "spec",
    "notification",
    "server",
    "external_reviews",
    "egress",
    "security",
    "dashboard",
    "output",
    "monitoring",
)


def _flatten_toml(data: dict[str, Any]) -> dict[str, Any]:
    """Flatten nested TOML sections into ProjectConfig fields.

    Maps TOML sections like [task_source], [agent], [review] to their
    corresponding nested Pydantic models.
    """
    result: dict[str, Any] = {}

    # Top-level [project] section maps to root fields
    if "project" in data:
        for key, value in data["project"].items():
            result[key] = value

    # Nested sections map to their config models
    for section in _NESTED_SECTIONS:
        if section in data:
            result[section] = data[section]

    # Root-level keys that don't belong to a section
    for key in ("github_repo", "github_user", "base_branch", "test_cmd", "lint_cmd", "format_cmd", "check_cmd"):
        if key in data and key not in result:
            result[key] = data[key]

    return result


def _migrate_deprecated_keys(flat: dict[str, Any]) -> None:
    """Migrate deprecated config keys to their replacements."""
    commit = flat.get("commit")
    if isinstance(commit, dict) and "no_ai_coauthor" in commit:
        old_val = commit.pop("no_ai_coauthor")
        if "ai_coauthor" not in commit:
            commit["ai_coauthor"] = not old_val
