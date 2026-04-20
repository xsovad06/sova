"""Template rendering for command files.

Replaces ``{{ var }}`` placeholders in command content with project-specific
values from sova.toml / ProjectConfig using regex substitution.
"""

from __future__ import annotations

import re

from sova.config.models import ProjectConfig


def render_command(content: str, variables: dict[str, str]) -> str:
    """Render template variables in command content.

    Replaces ``{{ var_name }}`` patterns with values from the variables dict.
    Unknown variables are left as-is (preserving the original ``{{ ... }}``).
    """
    if not variables:
        return content

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        if key in variables:
            return variables[key]
        return match.group(0)

    return re.sub(r"\{\{\s*(\w+)\s*\}\}", _replace, content)


def build_variables(cfg: ProjectConfig) -> dict[str, str]:
    """Extract template variables from a ProjectConfig.

    Returns a dict of variable names to their values, suitable for
    passing to render_command().
    """
    variables: dict[str, str] = {
        "test_cmd": cfg.test_cmd,
        "lint_cmd": cfg.lint_cmd,
        "format_cmd": cfg.format_cmd,
        "base_branch": cfg.base_branch,
        "github_repo": cfg.github_repo,
        "github_user": cfg.github_user,
    }

    # Scopes: derived from commit config or default
    variables["scopes"] = _derive_scopes(cfg)

    return variables


def _derive_scopes(cfg: ProjectConfig) -> str:
    """Derive commit scopes from project config.

    If the project has a persona_map configured, use that to hint at scopes.
    Otherwise, provide a generic default.
    """
    if cfg.persona_map:
        return cfg.persona_map
    return "core, tests, docs, config"
