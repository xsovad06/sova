"""Template rendering for command files.

Replaces ``{{ var }}`` placeholders in command content with project-specific
values from SOVA config / ProjectConfig using regex substitution.
"""

from __future__ import annotations

import re

from sova.config.models import ProjectConfig

_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_DOUBLE_DASH_RE = re.compile(r" -{2} ")


def split_fenced_lines(text: str) -> list[tuple[bool, str]]:
    """Pair each line of ``text`` with whether it is inside (or delimiting) a fenced code block.

    The one place fence state is tracked, shared by every renderer that must treat code
    differently from prose: a shell flag, path, or placeholder inside a fence needs a
    code-shaped rewrite (or none at all), while the same token in prose needs an
    explanatory one. Fence delimiter lines themselves report ``True`` so no prose rule
    is ever applied to them.

    Follows CommonMark's own fence-matching rule rather than toggling on any
    fence-looking line: a closing fence must use the same marker character
    (backtick vs. tilde) and be at least as long as the opening one. Without
    that, a ``~~~`` block containing a literal ```` ``` ```` line (or a
    4-backtick fence wrapping a nested 3-backtick example) would close early,
    leaving the rest of the block misclassified as prose.
    """
    in_fence = False
    fence_char = ""
    fence_len = 0
    paired: list[tuple[bool, str]] = []
    for line in text.split("\n"):
        match = _FENCE_RE.match(line)
        if match:
            marker = match.group(1)
            if not in_fence:
                in_fence = True
                fence_char = marker[0]
                fence_len = len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len:
                in_fence = False
            paired.append((True, line))
            continue
        paired.append((in_fence, line))
    return paired


def dedash_prose(text: str) -> str:
    """Replace space-dash-dash-space prose separators (AGENTS.md forbids them, see invariants/no-double-dash.sh).

    Fenced code blocks are left untouched, mirroring the invariant's own exemption for them: a
    real shell ``--`` flag or example inside a fence must not be rewritten. Shared by every
    renderer that derives distributable text from canonical ``commands/*.md`` bodies (today:
    ``sova.commands.marketplace_export``, ``sova.commands.skill_render``), since those bodies
    predate the invariant and still use `` -- `` themselves.
    """
    return "\n".join(line if fenced else _DOUBLE_DASH_RE.sub(": ", line) for fenced, line in split_fenced_lines(text))


def workflow_reference_re(name: str) -> re.Pattern[str]:
    """Match the provider-neutral ``name`` workflow cross-reference syntax.

    Canonical commands reference each other as a backticked bare command name
    immediately followed by the literal word "workflow" (e.g. "the `test`
    workflow"), rather than Claude's `/test` slash syntax. Each render target
    maps a match to its own invocation form: ``sova.commands.distribution``
    renders it back to Claude's `/test` slash syntax, while
    ``sova.commands.skill_render`` renders it to a Codex skill reference.
    """
    return re.compile(rf"`{re.escape(name)}`\s+workflow\b")


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
        # Claude Code substitutes this literal token with the user's actual
        # invocation arguments at runtime; Codex has no equivalent mechanism
        # and renders {{ arguments }} to descriptive prose instead (see
        # sova.commands.skill_render's own substitution, applied before this
        # value ever gets a chance to fill it in).
        "arguments": "$ARGUMENTS",
    }

    # Scopes: derived from commit config or default
    variables["scopes"] = _derive_scopes(cfg)

    return variables


def _derive_project_name(cfg: ProjectConfig) -> str:
    """Derive a human-readable project name from config.

    ``cfg.project_name``, when set, wins outright: it exists precisely for a
    case like this repo's own, where the real repo slug ("sova") and the
    brand name used in prose ("SOVA") differ, so neither has to be faked to
    produce the other.
    """
    if cfg.project_name:
        return cfg.project_name
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
