"""Command distribution: install, update, diff, and list operations.

Handles the full lifecycle of deploying canonical SOVA commands into target
projects, including template adaptation, manifest tracking, merge conflict
detection, and incremental updates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from sova.commands.catalog import CommandEntry, discover
from sova.commands.manifest import (
    Manifest,
    ManifestEntry,
    create_manifest,
    file_hash,
    read_manifest,
    write_manifest,
)
from sova.commands.templates import build_variables, render_command, split_fenced_lines, workflow_reference_re
from sova.config.models import ProjectConfig
from sova.utils.files import read_text_or_none
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.agents.base import RuntimeAdapter

log = get_logger(component="commands.distribution")


@dataclass
class InstallResult:
    """Result of an install_commands() operation."""

    installed: int = 0
    skipped: int = 0


@dataclass
class UpdateResult:
    """Result of an update_commands() operation."""

    updated: int = 0
    skipped: int = 0
    conflicts: list[str] = field(default_factory=list)
    # Only ever populated when the caller opts into ``prune_stale`` (see
    # ``_update_files()``): a manifest-tracked path whose source entry is
    # gone entirely, removed because its on-disk content still matched the
    # last-installed hash.
    removed: list[str] = field(default_factory=list)


@dataclass
class DiffResult:
    """Result of a diff_commands() operation."""

    changed: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    # Rendered canonical content for every entry in `changed`/`new`, keyed by
    # filename. Populated as a byproduct of the hash comparison this function
    # already performs, so callers that need the content for display (e.g. the
    # settings review modal) don't have to read and render the file a second
    # time.
    rendered: dict[str, str] = field(default_factory=dict)


@dataclass
class DriftEntry:
    """A single file with local modifications detected by reverse diff."""

    filename: str
    canonical_content: str
    local_content: str
    upstream_also_changed: bool = False
    canonical_removed: bool = False


@dataclass
class ReverseDiffResult:
    """Result of a reverse diff (drift detection) operation."""

    modified: list[DriftEntry] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    unmanaged: list[str] = field(default_factory=list)


@dataclass
class ListEntry:
    """A command in the listing."""

    filename: str
    managed: bool
    name: str = ""
    description: str = ""


@dataclass
class ListResult:
    """Result of a list_commands() operation."""

    managed: list[ListEntry] = field(default_factory=list)
    local: list[ListEntry] = field(default_factory=list)


def _render_workflow_references(content: str, workflow_names: list[str] | None) -> str:
    """Rewrite a `name` workflow cross-reference back to Claude's `/name` slash syntax.

    ``workflow_names`` is ``None`` for every non-command target (guidelines,
    skills): only the commands_dir() render path needs this, since Claude's
    actual slash-command mechanics belong there, not baked into a generic
    helper every caller pays for. Scoped to prose lines the same way the
    Codex-side equivalent is (``sova.commands.skill_render``), so a reference
    inside a shell fence is left untouched.
    """
    if not workflow_names:
        return content
    lines: list[str] = []
    for fenced, line in split_fenced_lines(content):
        if not fenced:
            for name in workflow_names:
                line = workflow_reference_re(name).sub(f"`/{name}` workflow", line)
        lines.append(line)
    return "\n".join(lines)


def _write_rendered(target_dir: Path, target_path: Path, rendered: str) -> None:
    """Write *rendered* into *target_dir* at *target_path*, never through a symlink.

    ``Path.write_text`` follows symlinks, so an installed entry that is a symlink
    sent the render to the link's target instead of into the target directory.
    Projects do symlink commands to share them (see ``_copy2_skip_identical`` in
    ``sova/git/worktree.py``, which documents a command "kept in sync across
    projects via a symlink into ``~/.claude/commands``"), and those shared files
    were silently overwritten by any install or sync, with the symlink left in
    place pointing at corrupted content and the manifest recording success.

    Unlinking first guarantees the write lands inside the target directory. The
    entry becomes a regular file, so the sharing is undone rather than honoured;
    that is logged, since the alternative is destroying a file the caller never
    named. Issue #1094.
    """
    if target_path.is_symlink():
        try:
            resolved = target_path.resolve()
        except OSError:
            resolved = target_path
        log.warning("commands.symlink_replaced", path=str(target_path), pointed_at=str(resolved))
        target_path.unlink()

    # Enforce the containment this function exists to guarantee, rather than
    # leaving it as an assumption about the callers. Checked after the unlink so
    # the literal destination is validated, not a symlink's target. A filename
    # here is always a single path component from a directory listing, so a
    # violation means a collector was changed to emit a traversal; that is a
    # programming error and must fail loudly rather than skip the file and leave
    # the manifest recording a hash for something never written. Mirrors the
    # is_relative_to guard in _copy_worktree_files (sova/git/worktree.py).
    base = target_dir.resolve()
    destination = target_path.resolve()
    if destination != base and not destination.is_relative_to(base):
        log.error("commands.write.path_traversal", path=str(target_path), base=str(base))
        raise ValueError(f"refusing to write outside {base}: {target_path}")

    # Write through the validated path rather than the original argument, so the
    # check above and the use below are the same variable.
    #
    # NOSONAR: S2083 flags this as path traversal because `rendered` (the command
    # body) is tainted, not the destination. Sonar's own flow names `rendered` as
    # the malicious value, and it reaches this call as file *content*, never as a
    # path segment. The destination is a single path component from a directory
    # listing, joined to an operator-chosen target directory and checked against
    # it by is_relative_to() immediately above, so writing tainted payload bytes
    # to it cannot traverse paths. Same reasoning and same rule as the marker in
    # sova/utils/mcp_config.py.
    destination.write_text(rendered, encoding="utf-8")  # NOSONAR


def _install_files(
    source_files: list[tuple[str, Path]],
    target_dir: Path,
    variables: dict[str, str],
    *,
    workflow_names: list[str] | None = None,
) -> InstallResult:
    """Render and install source files into a target directory with manifest tracking."""
    result = InstallResult()
    hashes: dict[str, str] = {}

    target_dir.mkdir(parents=True, exist_ok=True)

    for filename, source_path in source_files:
        content = _render_workflow_references(source_path.read_text(encoding="utf-8"), workflow_names)
        rendered = render_command(content, variables)

        target_path = target_dir / filename
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _write_rendered(target_dir, target_path, rendered)
        hashes[filename] = file_hash(rendered)
        result.installed += 1

    create_manifest(target_dir, hashes)
    return result


def _update_files(
    source_files: list[tuple[str, Path]],
    target_dir: Path,
    variables: dict[str, str],
    *,
    force: bool = False,
    filenames: list[str] | None = None,
    workflow_names: list[str] | None = None,
    prune_stale: bool = False,
) -> UpdateResult:
    """Incrementally update installed files with conflict detection.

    ``filenames``, when not ``None``, restricts the update to that explicit
    subset of ``source_files`` (an empty list means "update nothing").

    ``prune_stale``, only honored when ``filenames`` is ``None`` (a
    caller-restricted subset is never the full canonical set, so it must
    never be read as "everything else is gone"), additionally removes any
    manifest entry absent from ``source_files`` entirely. This covers a
    source renamed or retired out from under an existing install (e.g. a
    standalone skill losing its ``sova-`` prefix, issue #1136): without it,
    the old installed path survives forever alongside the new one, since the
    main loop above only ever touches filenames it was actually given. Only
    a ``managed`` entry whose on-disk content still matches the recorded
    hash is removed; a locally-modified managed entry is left in place and
    reported via ``result.conflicts`` instead (same list the main loop above
    already uses for "needs a human"), and an unmanaged entry is never
    touched at all, so project-owned or already-edited content can't be
    silently deleted.
    """
    if filenames is not None:
        allowed = set(filenames)
        source_files = [(filename, path) for filename, path in source_files if filename in allowed]
        if not source_files:
            return UpdateResult()

    manifest = read_manifest(target_dir)
    working = manifest if manifest is not None else Manifest()
    manifest_dirty = False
    result = UpdateResult()

    for filename, source_path in source_files:
        content = _render_workflow_references(source_path.read_text(encoding="utf-8"), workflow_names)
        rendered = render_command(content, variables)
        new_hash = file_hash(rendered)

        target_path = target_dir / filename
        manifest_entry = working.commands.get(filename)

        if manifest_entry is None:
            # No manifest entry means SOVA never installed this filename here,
            # which includes the "no manifest at all yet" case (a target
            # directory that pre-dates SOVA managing it, e.g. a hand-authored
            # tree a runtime adapter mirrors into, or whose manifest was
            # deleted/corrupted after a prior install).
            if target_path.is_file():
                local_text = read_text_or_none(target_path)
                installed_hash = file_hash(local_text) if local_text is not None else None
                if installed_hash == new_hash:
                    # Content already matches canonical: this is recovery after a
                    # missing manifest entry, not unmanaged content. Adopt it as
                    # managed rather than reporting a conflict that --force would
                    # be needed to clear, since --force here would also overwrite
                    # the genuinely divergent, unmanaged case just below.
                    working.commands[filename] = ManifestEntry(hash=new_hash, managed=True)
                    manifest_dirty = True
                    result.skipped += 1
                    continue
                # Genuinely divergent (or unreadable) pre-existing content is
                # therefore unmanaged content, not a stale install, and must not
                # be silently overwritten; only a genuinely missing path is safe
                # to write without asking.
                if not force:
                    result.conflicts.append(filename)
                    continue
            target_path.parent.mkdir(parents=True, exist_ok=True)
            _write_rendered(target_dir, target_path, rendered)
            working.commands[filename] = ManifestEntry(hash=new_hash, managed=True)
            manifest_dirty = True
            result.updated += 1
            continue

        if not force and manifest_entry.hash == new_hash:
            # Canonical hasn't changed since the manifest was last written, and this
            # isn't a forced (explicitly selected) sync: nothing to update, and
            # nothing to repair (the manifest already reflects canonical). Checked
            # before touching the local file at all, so a 'Sync All' (force=False)
            # only reads/hashes local files whose canonical source actually changed,
            # matching the pre-repair fast path. A forced sync (the review-modal's
            # selective restore of a locally-modified or locally-deleted file) still
            # needs to check local drift even when canonical is unchanged, since
            # force means "restore this file" regardless of whether canonical moved.
            result.skipped += 1
            continue

        file_exists = target_path.is_file()
        local_text = read_text_or_none(target_path)
        installed_hash = file_hash(local_text) if local_text is not None else None

        if installed_hash == new_hash:
            # Local file already matches the new canonical content (e.g. the user
            # applied the upstream change by hand, or there's no local drift for
            # this forced sync to restore): nothing to write, but repair the stale
            # manifest hash so a future non-force sync doesn't derive a false
            # conflict from it.
            if manifest_entry.hash != new_hash:
                working.commands[filename] = ManifestEntry(hash=new_hash, managed=True)
                manifest_dirty = True
            result.skipped += 1
            continue

        if not force:
            # An existing file that can't be verified against the manifest (unreadable
            # encoding, permission error) must be treated as a conflict, not silently
            # overwritten: only a genuinely missing file falls through to a clean write.
            if file_exists and (installed_hash is None or installed_hash != manifest_entry.hash):
                result.conflicts.append(filename)
                continue

        target_path.parent.mkdir(parents=True, exist_ok=True)
        _write_rendered(target_dir, target_path, rendered)
        working.commands[filename] = ManifestEntry(hash=new_hash, managed=True)
        manifest_dirty = True
        result.updated += 1

    if prune_stale and filenames is None:
        canonical_names = {filename for filename, _ in source_files}
        for filename in sorted(working.commands):
            if filename in canonical_names:
                continue
            entry = working.commands[filename]
            if not entry.managed:
                continue
            target_path = target_dir / filename
            if not target_path.is_file():
                # Already gone (manually deleted, or never written); just drop
                # the now-meaningless manifest entry.
                del working.commands[filename]
                manifest_dirty = True
                result.removed.append(filename)
                continue
            local_text = read_text_or_none(target_path)
            local_hash = file_hash(local_text) if local_text is not None else None
            if local_hash != entry.hash:
                result.conflicts.append(filename)
                continue
            try:
                target_path.unlink()
            except OSError:
                log.warning("commands.prune.unlink_failed", filename=filename)
                result.conflicts.append(filename)
                continue
            parent = target_path.parent
            if parent != target_dir:
                try:
                    parent.rmdir()
                except OSError:
                    pass
            del working.commands[filename]
            manifest_dirty = True
            result.removed.append(filename)

    if manifest_dirty:
        write_manifest(target_dir, working)

    return result


def _runtime_filter(adapter: RuntimeAdapter | None) -> Callable[[CommandEntry], bool]:
    """Build the per-command predicate for an optional target runtime.

    ``adapter=None`` (every call site that doesn't yet care about runtime
    restriction) keeps every command, matching pre-existing behavior exactly.
    """
    if adapter is None:
        return lambda _cmd: True
    return adapter.supports_command


def install_commands(
    canonical_dir: Path,
    target_dir: Path,
    cfg: ProjectConfig,
    *,
    include_autonomous: bool = True,
    adapter: RuntimeAdapter | None = None,
) -> InstallResult:
    """Install canonical commands into a target project directory.

    ``adapter``, when given, additionally restricts installation to commands
    whose frontmatter ``runtimes`` list (if any) names that adapter's runtime.
    """
    commands = discover(canonical_dir)
    supports = _runtime_filter(adapter)
    files = [
        (cmd.path.name, cmd.path)
        for cmd in commands
        if (include_autonomous or cmd.category != "autonomous") and supports(cmd)
    ]
    skipped = len(commands) - len(files)

    result = _install_files(files, target_dir, build_variables(cfg), workflow_names=[cmd.name for cmd in commands])
    result.skipped = skipped
    log.info("commands.installed", count=result.installed, skipped=result.skipped)
    return result


def update_commands(
    canonical_dir: Path,
    target_dir: Path,
    cfg: ProjectConfig,
    *,
    include_autonomous: bool = True,
    force: bool = False,
    filenames: list[str] | None = None,
    adapter: RuntimeAdapter | None = None,
) -> UpdateResult:
    """Update installed commands incrementally.

    ``filenames``, when not ``None``, restricts the update to that explicit
    subset of discovered commands (an empty list means "update nothing").
    ``adapter``, when given, additionally restricts the update to commands
    whose frontmatter ``runtimes`` list (if any) names that adapter's runtime.
    """
    commands = discover(canonical_dir)
    supports = _runtime_filter(adapter)
    files = [
        (cmd.path.name, cmd.path)
        for cmd in commands
        if (include_autonomous or cmd.category != "autonomous") and supports(cmd)
    ]
    skipped = len(commands) - len(files)

    result = _update_files(
        files,
        target_dir,
        build_variables(cfg),
        force=force,
        filenames=filenames,
        workflow_names=[cmd.name for cmd in commands],
    )
    result.skipped += skipped
    return result


def _diff_files(
    source_files: list[tuple[str, Path]],
    target_dir: Path,
    variables: dict[str, str],
    *,
    workflow_names: list[str] | None = None,
) -> DiffResult:
    """Compare source files against installed manifest to find changes."""
    manifest = read_manifest(target_dir)
    result = DiffResult()

    if not source_files:
        return result

    if manifest is None:
        result.new = [filename for filename, _ in source_files]
        return result

    canonical_names = set()
    for filename, source_path in source_files:
        canonical_names.add(filename)

        content = read_text_or_none(source_path)
        if content is None:
            # An unreadable canonical file (corrupted install, bad checkout, a
            # non-UTF-8 edit) must not abort the whole diff. Skip it: it's
            # already in canonical_names, so it won't be misreported as
            # "removed" either. It simply doesn't appear as changed/new until
            # it becomes readable again.
            log.warning("commands.diff.unreadable_canonical_file", filename=filename)
            continue

        content = _render_workflow_references(content, workflow_names)
        rendered = render_command(content, variables)
        new_hash = file_hash(rendered)

        entry = manifest.commands.get(filename)
        if entry is None:
            result.new.append(filename)
            result.rendered[filename] = rendered
        elif entry.hash != new_hash:
            result.changed.append(filename)
            result.rendered[filename] = rendered

    for filename, entry in manifest.commands.items():
        if entry.managed and filename not in canonical_names:
            result.removed.append(filename)

    return result


def _reverse_diff_files(
    source_files: list[tuple[str, Path]],
    target_dir: Path,
    variables: dict[str, str],
    *,
    workflow_names: list[str] | None = None,
) -> ReverseDiffResult:
    """Compare installed files against canonical source to find local modifications.

    The reverse of _diff_files(): finds local changes that could be back-ported
    to the canonical source rather than upstream changes not yet installed.
    """
    manifest = read_manifest(target_dir)
    result = ReverseDiffResult()

    if manifest is None:
        return result

    canonical_lookup: dict[str, Path] = dict(source_files)

    for filename, entry in manifest.commands.items():
        if not entry.managed:
            continue

        target_path = target_dir / filename

        if not target_path.is_file():
            result.deleted.append(filename)
            continue

        local_content = read_text_or_none(target_path)
        if local_content is None:
            log.warning("commands.reverse_diff.unreadable_local_file", filename=filename)
            continue
        local_hash = file_hash(local_content)

        if local_hash == entry.hash:
            continue

        canonical_path = canonical_lookup.get(filename)
        canonical_removed = canonical_path is None or not canonical_path.is_file()
        canonical_content = ""
        upstream_also_changed = True
        if not canonical_removed:
            raw_canonical = read_text_or_none(canonical_path)
            if raw_canonical is None:
                # An unreadable canonical file degrades the same way a removed
                # one does: there's nothing to diff it against, so treat it as
                # "removed" rather than letting the exception abort the whole
                # reverse diff.
                log.warning("commands.reverse_diff.unreadable_canonical_file", filename=filename)
                canonical_removed = True
            else:
                raw_canonical = _render_workflow_references(raw_canonical, workflow_names)
                canonical_content = render_command(raw_canonical, variables)
                canonical_hash = file_hash(canonical_content)
                upstream_also_changed = canonical_hash != entry.hash

        result.modified.append(
            DriftEntry(
                filename=filename,
                canonical_content=canonical_content,
                local_content=local_content,
                upstream_also_changed=upstream_also_changed,
                canonical_removed=canonical_removed,
            )
        )

    if target_dir.is_dir():
        managed_names = {name for name, e in manifest.commands.items() if e.managed}
        for path in sorted(target_dir.glob("*.md")):
            if path.name not in managed_names:
                result.unmanaged.append(path.name)
        for path in sorted(target_dir.glob("*/SKILL.md")):
            rel_key = f"{path.parent.name}/SKILL.md"
            if rel_key not in managed_names:
                result.unmanaged.append(rel_key)

    return result


def diff_commands(
    canonical_dir: Path,
    target_dir: Path,
    cfg: ProjectConfig,
    *,
    adapter: RuntimeAdapter | None = None,
) -> DiffResult:
    """Show what changed between canonical source and installed commands.

    ``adapter``, when given, restricts the comparison to commands whose
    frontmatter ``runtimes`` list (if any) names that adapter's runtime,
    matching the same restriction ``update_commands()``/``install_commands()``
    apply. Without it, a command installed only for a different runtime (e.g.
    Codex-only) would be reported as "new" against a Claude Code target it
    was never meant to reach.
    """
    commands = discover(canonical_dir)
    supports = _runtime_filter(adapter)
    files = [(cmd.path.name, cmd.path) for cmd in commands if supports(cmd)]
    return _diff_files(files, target_dir, build_variables(cfg), workflow_names=[cmd.name for cmd in commands])


def reverse_diff_commands(
    canonical_dir: Path,
    target_dir: Path,
    cfg: ProjectConfig,
    *,
    adapter: RuntimeAdapter | None = None,
) -> ReverseDiffResult:
    """Show local modifications to installed commands that could be back-ported.

    ``adapter`` has the same meaning as in :func:`diff_commands`.
    """
    commands = discover(canonical_dir)
    supports = _runtime_filter(adapter)
    files = [(cmd.path.name, cmd.path) for cmd in commands if supports(cmd)]
    return _reverse_diff_files(files, target_dir, build_variables(cfg), workflow_names=[cmd.name for cmd in commands])


def diff_guidelines(
    guidelines_dir: Path,
    target_dir: Path,
    cfg: ProjectConfig,
) -> DiffResult:
    """Show what changed between canonical guidelines and installed ones."""
    files = _collect_guidelines(guidelines_dir)
    return _diff_files(files, target_dir, build_variables(cfg))


def reverse_diff_guidelines(
    guidelines_dir: Path,
    target_dir: Path,
    cfg: ProjectConfig,
) -> ReverseDiffResult:
    """Show local modifications to installed guidelines that could be back-ported."""
    files = _collect_guidelines(guidelines_dir)
    return _reverse_diff_files(files, target_dir, build_variables(cfg))


def _collect_guidelines(guidelines_dir: Path) -> list[tuple[str, Path]]:
    """Collect markdown files from a guidelines directory."""
    if not guidelines_dir.is_dir():
        return []
    return [(p.name, p) for p in sorted(guidelines_dir.glob("*.md"))]


def install_guidelines(
    guidelines_dir: Path,
    target_dir: Path,
    cfg: ProjectConfig,
) -> InstallResult:
    """Install guideline templates into a target project's rules directory."""
    files = _collect_guidelines(guidelines_dir)
    if not files:
        return InstallResult()

    result = _install_files(files, target_dir, build_variables(cfg))
    log.info("guidelines.installed", count=result.installed)
    return result


def update_guidelines(
    guidelines_dir: Path,
    target_dir: Path,
    cfg: ProjectConfig,
    *,
    force: bool = False,
    filenames: list[str] | None = None,
) -> UpdateResult:
    """Update installed guidelines incrementally.

    ``filenames``, when not ``None``, restricts the update to that explicit
    subset of collected guidelines (an empty list means "update nothing").
    """
    files = _collect_guidelines(guidelines_dir)
    if not files:
        return UpdateResult()

    return _update_files(files, target_dir, build_variables(cfg), force=force, filenames=filenames)


def _collect_skills(skills_dir: Path, *, name_prefix: str = "") -> list[tuple[str, Path]]:
    """Collect SKILL.md files from subdirectories of a skills directory.

    ``name_prefix`` is applied to the installed directory name, not the
    source directory name, so a runtime that needs a collision-safe target
    (e.g. Codex's ``.agents/skills/``, which pre-existing hand-authored
    content under plain names already occupies) can install the same
    canonical source tree under ``sova-<name>`` without a second,
    prefixed copy of that source tree.
    """
    if not skills_dir.is_dir():
        return []
    result: list[tuple[str, Path]] = []
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_file = skill_dir / "SKILL.md"
        if skill_file.is_file():
            rel_key = f"{name_prefix}{skill_dir.name}/SKILL.md"
            result.append((rel_key, skill_file))
    return result


def install_skills(
    skills_dir: Path,
    target_dir: Path,
    cfg: ProjectConfig,
    *,
    name_prefix: str = "",
) -> InstallResult:
    """Install skill templates into a target project's skills directory."""
    files = _collect_skills(skills_dir, name_prefix=name_prefix)
    if not files:
        return InstallResult()
    result = _install_files(files, target_dir, build_variables(cfg))
    log.info("skills.installed", count=result.installed)
    return result


def update_skills(
    skills_dir: Path,
    target_dir: Path,
    cfg: ProjectConfig,
    *,
    force: bool = False,
    name_prefix: str = "",
    prune_stale: bool = False,
) -> UpdateResult:
    """Update installed skills incrementally.

    ``prune_stale`` is for a caller syncing the *complete* set of skills that
    should exist at ``target_dir`` (see ``_update_files()``): passing it when
    ``skills_dir`` was itself filtered (e.g. by runtime support) would read
    the filtered-out names as "retired" and delete them, so it defaults to
    off and only ``sova.agents.sync.sync_runtime_skills()`` opts in today.
    """
    files = _collect_skills(skills_dir, name_prefix=name_prefix)
    if not files:
        return UpdateResult()
    return _update_files(files, target_dir, build_variables(cfg), force=force, prune_stale=prune_stale)


def diff_skills(skills_dir: Path, target_dir: Path, cfg: ProjectConfig) -> DiffResult:
    """Show what changed between canonical skills and installed ones.

    No ``name_prefix`` parameter: unlike ``install_skills()``/``update_skills()``,
    nothing calls this against a prefixed runtime mirror (e.g. Codex's
    ``.agents/skills/``) today, so there is no caller to thread it through to.
    Add one back only alongside an actual caller resolving that mirror's
    ``skills_dir()`` and ``skill_name_prefix``.
    """
    files = _collect_skills(skills_dir)
    return _diff_files(files, target_dir, build_variables(cfg))


def reverse_diff_skills(skills_dir: Path, target_dir: Path, cfg: ProjectConfig) -> ReverseDiffResult:
    """Show local modifications to installed skills that could be back-ported.

    See ``diff_skills()`` for why this has no ``name_prefix`` parameter.
    """
    files = _collect_skills(skills_dir)
    return _reverse_diff_files(files, target_dir, build_variables(cfg))


def list_commands(target_dir: Path) -> ListResult:
    """List all commands in a target directory, grouped by managed vs local."""
    manifest = read_manifest(target_dir)
    result = ListResult()

    if not target_dir.is_dir():
        return result

    managed_names = set()
    if manifest is not None:
        managed_names = {name for name, entry in manifest.commands.items() if entry.managed}

    for path in sorted(target_dir.glob("*.md")):
        filename = path.name
        if filename in managed_names:
            result.managed.append(ListEntry(filename=filename, managed=True))
        else:
            result.local.append(ListEntry(filename=filename, managed=False))

    return result
