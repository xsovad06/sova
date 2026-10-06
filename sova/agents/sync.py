"""Syncs canonical skills into the currently-configured runtime's artifact
directory, additively to the always-on Claude Code install path.

``sova install``/``sova setup`` always renders commands/guidelines/skills
into ``.claude/`` regardless of ``agent.runtime``, because that directory is
read by the interactive Claude Code session too, not only by whichever
backend ``agent.runtime`` tells SOVA to spawn for autonomous pipeline agents.
This module only adds the extra mirror a non-Claude adapter needs (today,
Codex's ``.codex/skills/``) and warns, never deletes, about a
previously-configured runtime's directory left behind after a switch.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sova.agents.base import RuntimeAdapter
from sova.agents.claude_code import ClaudeCodeAdapter
from sova.agents.registry import ADAPTERS, create_runtime_adapter
from sova.commands.distribution import UpdateResult, update_skills
from sova.commands.manifest import MANIFEST_FILENAME
from sova.config.models import ProjectConfig
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from rich.console import Console

log = get_logger(component="agents.sync")


def _non_default_adapters(project_dir: Path) -> dict[str, type[RuntimeAdapter]]:
    """Adapters whose skills directory isn't the always-installed Claude Code one.

    Computed from the live ``ADAPTERS`` registry on every call (rather than
    once at import time) so a runtime registered after this module is first
    imported is still covered, and compared by resolved directory (rather
    than by subclassing ``ClaudeCodeAdapter``) so a future adapter that
    shares Claude Code's directory by subclassing it is still excluded.
    """
    claude_target = ClaudeCodeAdapter().skills_dir(project_dir)
    return {name: cls for name, cls in ADAPTERS.items() if cls().skills_dir(project_dir) != claude_target}


def sync_runtime_skills(
    skills_src_dir: Path, project_dir: Path, cfg: ProjectConfig, *, force: bool = False
) -> UpdateResult | None:
    """Install or update skills in the configured runtime's own directory.

    Returns ``None`` when the configured runtime's skills directory is the
    same one the caller's existing ``.claude/skills/`` install step already
    maintains, or when the runtime has no skills directory of its own.

    Always goes through ``update_skills()``, which is conflict-aware even on
    a "cold" target with no manifest yet: a destination directory could in
    principle already hold hand-authored content that must be reported as a
    conflict rather than silently overwritten on the first sync. (Codex's
    target, ``.codex/skills/``, is deliberately a directory no pre-existing
    source tree in this repo could already occupy; see ``CodexAdapter``.)
    """
    adapter = create_runtime_adapter(cfg.agent.runtime)
    target = adapter.skills_dir(project_dir)
    if target is None or target == ClaudeCodeAdapter().skills_dir(project_dir):
        return None

    return update_skills(skills_src_dir, target, cfg, force=force)


def report_runtime_skills_sync(
    console: Console, result: UpdateResult | None, cfg: ProjectConfig, warnings: list[str], *, indent: str = ""
) -> None:
    """Print a runtime-skills sync result and any orphan warnings, one way, everywhere.

    Shared by both the CLI's install path (``project.py``) and its
    standalone sync commands (``commands.py``) so the two surfaces can't
    independently drift on how they report conflicts: a prior version
    printed only a bare updated-count and silently dropped
    ``result.conflicts``, reporting success even when a locally-modified
    runtime skill was left untouched.
    """
    if result is not None:
        console.print(f"{indent}[green]Skills synced for runtime {cfg.agent.runtime!r}: {result.updated}[/green]")
        if result.conflicts:
            console.print(f"{indent}[yellow]Conflicts ({len(result.conflicts)}):[/yellow]")
            for name in result.conflicts:
                console.print(f"{indent}  ! {name}: locally modified, skipped")
            console.print(f"{indent}[dim]Use --force to overwrite, or manually merge.[/dim]")
    for warning in warnings:
        console.print(f"{indent}[yellow]Warning: {warning}[/yellow]")


def warn_orphaned_runtime_artifacts(project_dir: Path, cfg: ProjectConfig) -> list[str]:
    """Warn about SOVA-managed skill directories from a no-longer-configured runtime.

    Never deletes anything (cleanup is left to the operator), so a
    runtime switch can't silently discard content a human may have edited
    in place.
    """
    active_name = create_runtime_adapter(cfg.agent.runtime).name
    warnings: list[str] = []
    for runtime_name, adapter_cls in _non_default_adapters(project_dir).items():
        if runtime_name == active_name:
            continue
        target = adapter_cls().skills_dir(project_dir)
        if target is None:
            continue
        if (target / MANIFEST_FILENAME).is_file():
            warnings.append(
                f"{target} still holds SOVA-managed skills for runtime {runtime_name!r}, which is no "
                f"longer configured (agent.runtime={cfg.agent.runtime!r}); remove it manually if unneeded"
            )
    return warnings
