"""Detect a safety-critical command whose installed copy predates its canonical source.

A project runs the copy of a command installed under ``.claude/commands/``,
not the canonical template, so a fix to a squash or merge recipe reaches a
project only once someone syncs it there. Nothing surfaced the gap, and the
#1251 incident ran an unfixed squash recipe that silently reverted changes the
base had gained. :func:`find_stale_command` is the single primitive both the
supervisor's ``stale_commands`` gate and ``start_command()``'s pre-spawn check
call, so the two entry points cannot disagree about what "stale" means.
"""

from __future__ import annotations

from pathlib import Path

from sova.agents.claude_code import ClaudeCodeAdapter
from sova.commands.catalog import get_canonical_dir
from sova.commands.distribution import diff_commands
from sova.config.models import ProjectConfig

# Commands that rewrite, push or merge a PR branch, where running an outdated
# recipe can silently lose or revert work.
SAFETY_CRITICAL_COMMANDS = frozenset({"address-pr", "pr", "rearrange-commits", "review-full", "integrate-pr"})


def find_stale_command(
    project_dir: Path,
    command: str,
    cfg: ProjectConfig | None = None,
    *,
    run_dir: Path | None = None,
) -> str | None:
    """Return why ``command``'s installed copy cannot be trusted as current, or None.

    Only a safety-critical command that is actually installed is judged:
    without an installed copy in ``project_dir``, ``_resolve_command_prompt()``
    inlines the recipe from SOVA's own commands instead of sending a slash
    command, so no installed copy executes. An installed copy is stale when its
    manifest hash differs from the rendered canonical (synced from an older
    version), or when it has no manifest entry at all (no record that the
    project ever received the current recipe). A local edit on top of a current
    copy is not stale: the manifest still matches canonical, so the project's
    customization is respected.

    ``run_dir`` is the worktree the command will run in, when that is not
    ``project_dir``. Claude Code resolves the slash command from the worktree,
    and ``ensure_claude_artifacts()`` never overwrites a copy the branch tracks,
    so a project that commits ``.claude/commands/`` runs the branch's own copy,
    which can predate a fix the primary checkout already has. The copy and
    manifest are then read from ``run_dir``, while config still comes from
    ``project_dir``: a worktree carries no ``sova.db`` of its own.

    Raises on an unreadable install or config; callers decide whether that
    fails open.
    """
    if command not in SAFETY_CRITICAL_COMMANDS:
        return None

    adapter = ClaudeCodeAdapter()
    filename = f"{command}.md"
    project_commands = adapter.commands_dir(project_dir)
    if project_commands is None or not (project_commands / filename).is_file():
        return None

    commands_dir = project_commands
    on_branch = False
    if run_dir is not None and run_dir.resolve() != project_dir.resolve():
        on_branch = True
        commands_dir = adapter.commands_dir(run_dir)
    if commands_dir is None or not (commands_dir / filename).is_file():
        return None

    if cfg is None:
        from sova.config.loader import load_config

        cfg = load_config(project_dir)

    diff = diff_commands(get_canonical_dir(), commands_dir, cfg, adapter=adapter)
    if on_branch:
        where = f"The copy of /{command} committed on the branch checked out at {run_dir}"
        fix = "Rebase the branch onto its base branch to pick up the current copy."
    else:
        where = f"Installed /{command}"
        fix = (
            f"Run `sova commands update --project {project_dir}` (add `--force` to replace a locally customized copy)."
        )
    if filename in diff.changed:
        return f"{where} predates the current SOVA version of it. {fix}"
    if filename in diff.new:
        return f"{where} has no sync record, so it cannot be verified as current. {fix}"
    return None
