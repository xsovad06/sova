"""Configuration loading for SOVA.

Supports:
- sova.toml (primary, TOML format)
- Database overrides (DB wins over TOML)
- Environment variable overrides (SOVA_ prefix, highest priority)

"""

from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)


def load_config(project_dir: Path | None = None) -> ProjectConfig:
    """Load project configuration with priority: env > DB > TOML > defaults."""
    if project_dir is None:
        project_dir = Path.cwd()

    project_dir = project_dir.resolve()

    toml_path = project_dir / "sova.toml"
    toml_flat: dict[str, Any] = {}
    if toml_path.exists():
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
        toml_flat = _flatten_toml(data)
        _migrate_deprecated_keys(toml_flat)

    from sova.config.db_loader import _try_load_from_db

    db_overrides = _try_load_from_db(project_dir)
    if db_overrides:
        merged = _deep_merge(toml_flat, db_overrides)
    else:
        merged = toml_flat

    _apply_env_overrides(merged)

    return ProjectConfig(**merged) if merged else ProjectConfig()


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
    "mcp",
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
    "a2a",
    "conflict_resolution",
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

    # Root-level keys that don't belong to a [section] map to ProjectConfig fields.
    # Filter to known fields to avoid validation errors from typos or custom keys.
    known_fields = ProjectConfig.model_fields
    for key, value in data.items():
        if key not in result and key not in _NESTED_SECTIONS and key != "project":
            if key in known_fields:
                result[key] = value

    return result


def _deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge overrides into base. Lists are replaced wholesale, not merged."""
    result = dict(base)
    for key, override_val in overrides.items():
        if key in result and isinstance(result[key], dict) and isinstance(override_val, dict):
            result[key] = _deep_merge(result[key], override_val)
        else:
            result[key] = override_val
    return result


def _apply_env_overrides(merged: dict[str, Any]) -> None:
    """Apply env var overrides so env vars beat init kwargs in Pydantic Settings.

    Must run AFTER all merges (TOML + DB) so env vars have highest priority.
    Handles both top-level SOVA_ vars and nested SOVA_SECTION_ vars.
    """
    prefix = "SOVA_"
    known_fields = ProjectConfig.model_fields
    for env_key, env_val in os.environ.items():
        if not env_key.startswith(prefix):
            continue
        field_name = env_key[len(prefix) :].lower()
        if field_name in known_fields:
            merged[field_name] = env_val

    _apply_nested_env_overrides(
        merged,
        "telemetry",
        "SOVA_TELEMETRY_",
        [
            ("hub_url", "SOVA_TELEMETRY_HUB_URL"),
            ("hub_token", "SOVA_TELEMETRY_HUB_TOKEN"),
            ("machine_id", "SOVA_TELEMETRY_MACHINE_ID"),
        ],
    )


def _apply_nested_env_overrides(
    merged: dict[str, Any],
    section: str,
    _prefix: str,
    field_env_pairs: list[tuple[str, str]],
) -> None:
    """Apply env var overrides for a nested config section, creating it if needed."""
    existing = merged.get(section)
    if section in merged and not isinstance(existing, dict):
        return
    tel = dict(existing) if isinstance(existing, dict) else {}
    changed = False
    for field, env_var in field_env_pairs:
        env_val = os.environ.get(env_var)
        if env_val is not None:
            tel[field] = env_val
            changed = True
    if changed:
        merged[section] = tel


def _migrate_deprecated_keys(flat: dict[str, Any]) -> None:
    """Migrate deprecated config keys to their replacements."""
    commit = flat.get("commit")
    if isinstance(commit, dict) and "no_ai_coauthor" in commit:
        old_val = commit.pop("no_ai_coauthor")
        if "ai_coauthor" not in commit:
            commit["ai_coauthor"] = not old_val

    spec = flat.get("spec")
    if isinstance(spec, dict) and "auto_approve_threshold" in spec:
        old_val = spec.pop("auto_approve_threshold")
        if "auto_approve_simple" not in spec:
            spec["auto_approve_simple"] = old_val not in ("never", "none")
