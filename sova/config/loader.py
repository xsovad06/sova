"""Configuration loading for SOVA.

Supports:
- sova.toml (primary, TOML format)
- Environment variable overrides (SOVA_ prefix)

Legacy pak-agent.conf support is available via `sova migrate config`.
"""

from __future__ import annotations

import re
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
    return ProjectConfig(**flat)


_NESTED_SECTIONS = (
    "task_source", "agent", "review", "ci", "watch",
    "worktree", "commit", "triage", "roles", "notification",
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
    for key in ("github_repo", "github_user", "base_branch", "test_cmd", "lint_cmd", "format_cmd"):
        if key in data and key not in result:
            result[key] = data[key]

    return result


# -- Legacy config support (used by `sova migrate config`) ---------------------

# Legacy shell config key mapping: BASH_KEY -> (config_path, python_key)
_LEGACY_KEY_MAP: dict[str, tuple[str, str]] = {
    "TASK_SOURCE": ("task_source", "type"),
    "TASK_SOURCE_CONFIG": ("task_source", "config"),
    "GITHUB_REPO": ("", "github_repo"),
    "GITHUB_USER": ("", "github_user"),
    "TEST_CMD": ("", "test_cmd"),
    "LINT_CMD": ("", "lint_cmd"),
    "FORMAT_CMD": ("", "format_cmd"),
    "BASE_BRANCH": ("", "base_branch"),
    "ISSUE_MILESTONE": ("task_source", "milestone"),
    "ISSUE_LABELS": ("task_source", "labels"),
    "PERSONA_MAP": ("", "persona_map"),
    "AGENT_MODEL": ("agent", "model"),
    "MAX_BUDGET": ("agent", "max_budget"),
    "SKIP_MANUAL_TEST": ("agent", "skip_manual_test"),
    "AUTO_APPROVE_FIXES": ("agent", "auto_approve_fixes"),
    "REVIEW_ENABLED": ("review", "enabled"),
    "REVIEW_MAX_ROUNDS": ("review", "max_rounds"),
    "CI_POLL_INTERVAL": ("ci", "poll_interval"),
    "CI_MAX_WAIT": ("ci", "max_wait"),
    "FLAKY_CHECKS": ("ci", "flaky_checks"),
    "WATCH_INTERVAL_ACTIVE": ("watch", "interval_active"),
    "WATCH_INTERVAL_IDLE": ("watch", "interval_idle"),
    "WATCH_AUTO_SELECT_ISSUES": ("watch", "auto_select_issues"),
    "WATCH_VETO_SECONDS": ("watch", "veto_seconds"),
    "WORKTREE_COPY_FILES": ("worktree", "copy_files"),
    "WORKTREE_TTL_DONE_DAYS": ("worktree", "ttl_done_days"),
    "WORKTREE_TTL_PAUSED_DAYS": ("worktree", "ttl_paused_days"),
    "SHARED_KNOWLEDGE_DIR": ("", "shared_knowledge_dir"),
    "INVARIANTS_DIR": ("", "invariants_dir"),
    "MAX_PARALLEL_AGENTS": ("", "max_parallel_agents"),
    "NO_AI_COAUTHOR": ("commit", "no_ai_coauthor"),
    "COMMIT_AUTHOR": ("commit", "author"),
    "COMMIT_FORMAT": ("commit", "format"),
    "PR_TITLE_FORMAT": ("commit", "pr_title_format"),
    "PR_AUTO_LINK_ISSUES": ("commit", "pr_auto_link_issues"),
    "BRANCH_NAMING": ("commit", "branch_naming"),
    "SCANNER_GITHUB_CHECK": ("", "scanner_github_check"),
    "SLACK_CHANNEL": ("", "slack_channel"),
    "JIRA_BASE_URL": ("task_source", "jira_base_url"),
    "JIRA_EMAIL": ("task_source", "jira_email"),
}

# Keys whose shell values are comma-separated lists
_LIST_KEYS = {"FLAKY_CHECKS", "WORKTREE_COPY_FILES"}

# Keys whose shell values are booleans
_BOOL_KEYS = {
    "SKIP_MANUAL_TEST",
    "AUTO_APPROVE_FIXES",
    "REVIEW_ENABLED",
    "WATCH_AUTO_SELECT_ISSUES",
    "NO_AI_COAUTHOR",
    "PR_AUTO_LINK_ISSUES",
    "SCANNER_GITHUB_CHECK",
}


def _parse_shell_config(path: Path) -> dict[str, str]:
    """Parse a shell-sourceable key=value config file.

    Handles:
    - KEY="value" and KEY=value
    - Comments (# ...)
    - Empty lines
    - $HOME expansion
    """
    result: dict[str, str] = {}
    pattern = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*)$")

    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        match = pattern.match(line)
        if match:
            key = match.group(1)
            value = match.group(2).strip('"').strip("'")
            # Expand $HOME
            value = value.replace("$HOME", str(Path.home()))
            result[key] = value

    return result
