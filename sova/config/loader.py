"""Configuration loading for SOVA.

Supports:
- sova.toml (primary, TOML format)
- Environment variable overrides (SOVA_ prefix)

"""

from __future__ import annotations

import os
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
    "testing",
    "output",
    "monitoring",
    "knowledge",
    "integration_gates",
    "integration",
    "rtk",
    "coderabbit_quota",
    "pr_monitor",
    "supervisor",
    "memory_guard",
    "watchdog",
    "telemetry",
    "fleet",
    "awareness",
    "oversight",
    "dependabot",
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

    # Migrate deprecated [resources] section into [memory_guard]
    if "resources" in data and isinstance(data["resources"], dict):
        guard = result.setdefault("memory_guard", {})
        if isinstance(guard, dict):
            key_map = {
                "memory_block_threshold_gb": "block_threshold_gb",
                "memory_warn_threshold_gb": "warn_threshold_gb",
            }
            for old_key, new_key in key_map.items():
                if old_key in data["resources"] and new_key not in guard:
                    guard[new_key] = data["resources"][old_key]

    # For the telemetry section, allow SOVA_TELEMETRY_* env vars to override TOML values.
    # Pydantic Settings treats init kwargs as highest priority, so TOML values passed via
    # ProjectConfig(telemetry=...) would otherwise silently win over env vars.
    if "telemetry" in result and isinstance(result["telemetry"], dict):
        tel = dict(result["telemetry"])
        for field, env_var in [
            ("hub_url", "SOVA_TELEMETRY_HUB_URL"),
            ("hub_token", "SOVA_TELEMETRY_HUB_TOKEN"),
            ("machine_id", "SOVA_TELEMETRY_MACHINE_ID"),
        ]:
            env_val = os.environ.get(env_var)
            if env_val is not None:
                tel[field] = env_val
        result["telemetry"] = tel

    # Root-level keys that don't belong to a [section] map to ProjectConfig fields.
    # Filter to known fields to avoid validation errors from typos or custom keys.
    known_fields = ProjectConfig.model_fields
    for key, value in data.items():
        if key not in result and key not in _NESTED_SECTIONS and key != "project":
            if key in known_fields:
                result[key] = value

    return result


def _migrate_deprecated_keys(flat: dict[str, Any]) -> None:
    """Migrate deprecated config keys to their replacements."""
    commit = flat.get("commit")
    if isinstance(commit, dict) and "no_ai_coauthor" in commit:
        old_val = commit.pop("no_ai_coauthor")
        if "ai_coauthor" not in commit:
            commit["ai_coauthor"] = not old_val
