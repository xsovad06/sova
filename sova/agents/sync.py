"""Syncs canonical skills into the currently-configured runtime's artifact
directory, additively to the always-on Claude Code install path.

``sova install``/``sova setup`` always renders commands/guidelines/skills
into ``.claude/`` regardless of ``agent.runtime``, because that directory is
read by the interactive Claude Code session too, not only by whichever
backend ``agent.runtime`` tells SOVA to spawn for autonomous pipeline agents.
This module only adds the extra mirror a non-Claude adapter needs (today,
Codex's ``.agents/skills/``, which also gets one mechanically-rendered skill
per canonical command via ``RuntimeAdapter.extra_skill_sources()``) and
warns, never deletes, about a previously-configured runtime's directory left
behind after a switch.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from sova.agents.base import RuntimeAdapter
from sova.agents.claude_code import ClaudeCodeAdapter
from sova.agents.registry import ADAPTERS, create_runtime_adapter
from sova.commands.catalog import get_canonical_dir
from sova.commands.distribution import UpdateResult, update_skills
from sova.commands.manifest import MANIFEST_FILENAME, read_manifest
from sova.commands.skill_render import materialize_combined_skill_sources
from sova.config.models import ProjectConfig
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from rich.console import Console

log = get_logger(component="agents.sync")

# Directories a previous version of an adapter used before its skills_dir()
# moved, kept so an upgrade from that version doesn't leave orphaned,
# manifest-tracked content with nothing to detect it: warn_orphaned_runtime_
# artifacts() only scans the *currently registered* adapters' resolved
# directories, so a directory that is no longer any adapter's target is
# otherwise invisible to it. CodexAdapter.skills_dir() moved from
# `.codex/skills` to `.agents/skills` in #1123.
_LEGACY_SKILL_DIRS: dict[str, Path] = {"codex": Path(".codex") / "skills"}


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
    target, ``.agents/skills/``, installs every command-derived entry under
    a ``sova-`` prefix for exactly this reason; see ``CodexAdapter``. A
    standalone skill under ``skills_src_dir`` installs under its own bare
    name instead, relying on ``materialize_combined_skill_sources()``'s
    separate existing-content check rather than the prefix, since it is a
    different artifact class from a command-derived skill; see that
    function's docstring, issue #1136.)

    The adapter's ``extra_skill_sources()`` (Codex's command-derived skills)
    are merged with ``skills_src_dir`` into a scratch directory first, so a
    single ``update_skills()`` call produces one coherent manifest for both
    source kinds. No further ``name_prefix`` is passed to ``update_skills()``
    itself: ``materialize_combined_skill_sources()`` already named every
    entry in the scratch directory exactly as it should land in ``target``.

    ``prune_stale=True`` on that call removes any manifest-tracked entry
    absent from the scratch tree entirely (when unmodified), not just ones
    whose content changed: a prior install's ``sova-<name>`` standalone
    skill (from before issue #1136 removed that prefix for standalone
    skills) would otherwise sit on disk forever alongside the new bare-named
    one, discoverable twice under the runtime's own skill lookup.
    """
    adapter = create_runtime_adapter(cfg.agent.runtime)
    target = adapter.skills_dir(project_dir)
    if target is None or target == ClaudeCodeAdapter().skills_dir(project_dir):
        return None

    extra = adapter.extra_skill_sources(get_canonical_dir())
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        materialize_combined_skill_sources(
            skills_src_dir, extra, scratch, name_prefix=adapter.skill_name_prefix, existing_target_dir=target
        )
        return update_skills(scratch, target, cfg, force=force, prune_stale=True)


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
        if result.removed:
            console.print(
                f"{indent}[dim]Removed {len(result.removed)} stale entr(ies) from a prior naming scheme:[/dim]"
            )
            for name in result.removed:
                console.print(f"{indent}  - {name}")
        if result.conflicts:
            console.print(f"{indent}[yellow]Conflicts ({len(result.conflicts)}):[/yellow]")
            for name in result.conflicts:
                console.print(f"{indent}  ! {name}: locally modified, skipped")
            console.print(f"{indent}[dim]Use --force to overwrite, or manually merge.[/dim]")
    for warning in warnings:
        console.print(f"{indent}[yellow]Warning: {warning}[/yellow]")


def warn_orphaned_runtime_artifacts(project_dir: Path, cfg: ProjectConfig) -> list[str]:
    """Warn about SOVA-managed skill entries from a no-longer-configured runtime.

    Never deletes anything (cleanup is left to the operator), so a runtime
    switch can't silently discard content a human may have edited in place.
    Scoped to the manifest-tracked (``managed: true``) entries specifically,
    never the whole directory: a runtime whose ``skill_name_prefix`` is
    non-empty (e.g. Codex's ``.agents/skills``) shares that directory with
    independently-maintained, hand-authored content under plain names (this
    repo's own ``testing-patterns``, ``database-patterns``, etc.), and
    advising "remove it manually" against the directory as a whole would
    point an operator at deleting content SOVA never installed.

    Silent, rather than merely softened, when that shared directory also
    holds anything SOVA didn't install (confirmed on this very repo: the
    checked-in ``.agents/skills/sova-*`` self-render tree sits beside
    hand-authored skills, and is permanent build output, not a leftover
    mirror from a runtime no longer configured; see
    ``test_no_warning_for_this_repos_own_self_rendered_skills`` and
    ``test_no_warning_when_shared_directory_also_holds_unmanaged_content``,
    which pin exactly this). A directory holding only manifest-tracked
    entries is unambiguous, so that case still warns. ``on_disk`` is
    restricted to directories actually containing a ``SKILL.md``, so an
    unrelated stray subdirectory (an assets folder, a cache dir) can't widen
    that ambiguity check.

    Also reports a previous version of an adapter's skills directory (see
    ``_LEGACY_SKILL_DIRS``) when it still holds a SOVA manifest: that
    directory is no longer any adapter's resolved target at all, so the
    live-registry scan below would otherwise never see it.
    """
    active_name = create_runtime_adapter(cfg.agent.runtime).name
    warnings: list[str] = []
    for runtime_name, adapter_cls in _non_default_adapters(project_dir).items():
        if runtime_name == active_name:
            continue
        adapter = adapter_cls()
        target = adapter.skills_dir(project_dir)
        if target is None:
            continue
        manifest = read_manifest(target)
        if manifest is None:
            continue
        managed = {name for name, entry in manifest.commands.items() if entry.managed}
        if not managed:
            continue
        on_disk = {f"{p.name}/SKILL.md" for p in target.iterdir() if p.is_dir() and (p / "SKILL.md").is_file()}
        if on_disk - managed:
            continue
        warnings.append(
            f"{target} still holds {len(managed)} SOVA-managed skill entries for runtime {runtime_name!r}, "
            f"which is no longer configured (agent.runtime={cfg.agent.runtime!r}); remove those entries "
            f"and {MANIFEST_FILENAME} manually if unneeded, but leave any other content in that directory alone"
        )
    for runtime_name, legacy_rel in _LEGACY_SKILL_DIRS.items():
        if runtime_name == active_name:
            continue
        legacy_dir = project_dir / legacy_rel
        manifest = read_manifest(legacy_dir)
        if manifest is None:
            continue
        managed = {name for name, entry in manifest.commands.items() if entry.managed}
        if not managed:
            continue
        current_target = ADAPTERS[runtime_name]().skills_dir(project_dir) if runtime_name in ADAPTERS else None
        moved_note = f" it has since moved to {current_target}; " if current_target is not None else " "
        warnings.append(
            f"{legacy_dir} still holds {len(managed)} SOVA-managed skill entries from an earlier version of the "
            f"{runtime_name!r} runtime;{moved_note}remove those entries and {MANIFEST_FILENAME} manually if unneeded"
        )
    return warnings
