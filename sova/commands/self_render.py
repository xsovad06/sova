"""Render the canonical ``commands/`` templates into this repo's ``.claude/commands/``.

This repository is both the canonical source of the distributable slash commands
and a sync target for them. ``commands/*.md`` carry ``{{ var }}`` placeholders
that :func:`~sova.commands.templates.render_command` substitutes at install
time, while ``.claude/commands/`` holds the rendered copy Claude Code actually
loads. Claude Code has no templating of its own, so an unrendered
``{{ check_cmd }}`` reaches the agent verbatim instead of ``make check``.

Because the rendered tree is checked in, it can drift from canonical in either
direction. A later ``sova commands sync`` then overwrites that drift and leaves
the primary checkout dirty; ``ensure_claude_artifacts()`` mirrors the dirt into
every worktree, where ``RearrangeCommitsStep``'s gate reads it as the agent's own
uncommitted work and pauses the run. ``render_self()`` is the single
regeneration path, shared by ``make commands-render`` and the drift guard in
``tests/test_command_render_drift.py``, so the two trees cannot diverge silently.

The variable values are pinned in :data:`SELF_VARIABLES` rather than read from
``load_config()`` on purpose: the rendered tree is a checked-in build artifact
and must be reproducible from the repository alone. ``load_config()`` resolves
``check_cmd`` from ``.claude/sova.db``, which does not exist in CI, so the same
canonical template would render as ``make check`` locally and as the
``lint_cmd && test_cmd`` fallback on a clean checkout.
"""

from __future__ import annotations

import re
from pathlib import Path

from sova.commands.distribution import InstallResult, install_commands
from sova.config.models import ProjectConfig
from sova.utils.logging import get_logger

log = get_logger(component="commands.self_render")

# Template variable values for this repository. Every ``{{ var }}`` placeholder
# used anywhere in ``commands/`` must have a value here, or
# ``test_every_used_placeholder_is_pinned`` fails rather than letting an
# unsubstituted placeholder ship to agents. A pinned value no command uses yet is
# harmless: ``build_variables()`` passes the whole set to ``render_command()``,
# which only substitutes placeholders it actually finds.
SELF_VARIABLES: dict[str, str] = {
    "check_cmd": "make check",
    "test_cmd": "make test",
    "lint_cmd": "make lint",
    "format_cmd": "make format",
    "github_repo": "xsovad06/sova",
    # Pinned independently of github_repo: the real repo slug is lowercase
    # ("sova"), but the brand name used in prose is "SOVA".
    "project_name": "SOVA",
}

# Matches the same placeholder shape render_command() substitutes: a bare word
# only, so documentation examples such as ``{{ icon("x", "w-5") }}`` in
# design.md are correctly left alone.
PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

CANONICAL_SUBDIR = "commands"
RENDERED_SUBDIR = ".claude/commands"


def repo_root() -> Path:
    """Return the SOVA repository root (the parent of the ``sova`` package)."""
    return Path(__file__).resolve().parent.parent.parent


def self_config() -> ProjectConfig:
    """Build the pinned ProjectConfig used to render this repo's own commands."""
    return ProjectConfig(**SELF_VARIABLES)


def used_placeholders(directory: Path, pattern: str = "*.md") -> set[str]:
    """Return every placeholder name appearing in templates matching *pattern* under *directory*.

    ``pattern`` defaults to the flat ``commands/*.md`` shape; callers scanning
    the standalone ``skills/*/SKILL.md`` tree pass ``"*/SKILL.md"``.
    """
    names: set[str] = set()
    for path in sorted(directory.glob(pattern)):
        names.update(PLACEHOLDER_RE.findall(path.read_text(encoding="utf-8")))
    return names


def render_self(root: Path | None = None, target_dir: Path | None = None) -> InstallResult:
    """Render canonical commands into ``target_dir`` (default: this repo's rendered tree).

    Delegates to :func:`install_commands` so the regeneration exercises the same
    code path a real ``sova install`` takes. That recreates ``.sova-manifest.json``
    from scratch, listing exactly the canonical commands; project-only commands
    living alongside them are left on disk and stay absent from the manifest,
    which is what marks them unmanaged and keeps a later sync from touching them.
    """
    base = root if root is not None else repo_root()
    canonical = base / CANONICAL_SUBDIR
    target = target_dir if target_dir is not None else base / RENDERED_SUBDIR

    result = install_commands(canonical, target, self_config())
    log.info("commands.self_rendered", target=str(target), installed=result.installed)
    return result


def main() -> None:
    """Entry point for ``make commands-render``."""
    root = repo_root()
    result = render_self(root)
    print(f"Rendered {result.installed} commands into {RENDERED_SUBDIR}/")


if __name__ == "__main__":
    main()
