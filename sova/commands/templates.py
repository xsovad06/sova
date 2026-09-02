"""Template rendering for command files.

Replaces ``{{ var }}`` placeholders in command content with project-specific
values from SOVA config / ProjectConfig using regex substitution.
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


def reverse_render(content: str, variables: dict[str, str]) -> str:
    """Reverse template rendering by replacing known values with placeholders.

    Replaces exact variable values in content with their ``{{ var_name }}``
    placeholders. Processes longer values first to avoid partial replacements.

    Uses a two-pass approach with sentinel markers to prevent placeholder
    corruption when a shorter variable value is a substring of a previously
    inserted placeholder name (e.g., ``project_name="repo"`` corrupting
    ``{{ github_repo }}``).
    """
    if not variables:
        return content

    # Two-pass approach to prevent placeholder corruption when a shorter
    # variable value is a substring of a longer variable's placeholder name
    # (e.g., project_name="repo" corrupting "{{ github_repo }}").
    #
    # Pass 1: split content on already-inserted sentinels so subsequent
    # replacements only touch unprotected segments.
    _SENTINEL_L = "\x00\x01"
    _SENTINEL_R = "\x00\x02"

    # Start with the full content as a single unprotected segment.
    segments: list[str] = [content]

    for key, value in sorted(variables.items(), key=lambda kv: len(kv[1]), reverse=True):
        if not value:
            continue
        placeholder = f"{_SENTINEL_L} {key} {_SENTINEL_R}"
        new_segments: list[str] = []
        for seg in segments:
            if _SENTINEL_L in seg:
                # Already-protected segment: pass through unchanged.
                new_segments.append(seg)
            else:
                # Unprotected segment: replace and interleave with placeholder.
                parts = seg.split(value)
                for i, part in enumerate(parts):
                    new_segments.append(part)
                    if i < len(parts) - 1:
                        new_segments.append(placeholder)
        segments = new_segments

    # Pass 2: join and replace sentinels with actual Jinja2 delimiters.
    result = "".join(segments)
    return result.replace(_SENTINEL_L, "{{").replace(_SENTINEL_R, "}}")


def build_variables(cfg: ProjectConfig) -> dict[str, str]:
    """Extract template variables from a ProjectConfig.

    Returns a dict of variable names to their values, suitable for
    passing to render_command().
    """
    variables: dict[str, str] = {
        "test_cmd": cfg.test_cmd,
        "lint_cmd": cfg.lint_cmd,
        "format_cmd": cfg.format_cmd,
        "check_cmd": cfg.check_cmd or f"{cfg.lint_cmd} && {cfg.test_cmd}",
        "base_branch": cfg.base_branch,
        "github_repo": cfg.github_repo,
        "github_user": cfg.github_user,
        "project_name": _derive_project_name(cfg),
    }

    # Scopes: derived from commit config or default
    variables["scopes"] = _derive_scopes(cfg)

    return variables


def _derive_project_name(cfg: ProjectConfig) -> str:
    """Derive a human-readable project name from config."""
    repo = (cfg.github_repo or "").strip().strip("/")
    if "/" in repo:
        return repo.rsplit("/", 1)[-1] or "project"
    return repo or "project"


def _derive_scopes(cfg: ProjectConfig) -> str:
    """Derive commit scopes from project config.

    If the project has a persona_map configured, use that to hint at scopes.
    Otherwise, provide a generic default.
    """
    if cfg.persona_map:
        return cfg.persona_map
    return "core, tests, docs, config"
