"""Command distribution: install, update, diff, and list operations.

Handles the full lifecycle of deploying canonical SOVA commands into target
projects, including template adaptation, manifest tracking, merge conflict
detection, and incremental updates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sova.commands.catalog import discover
from sova.commands.manifest import (
    create_manifest,
    file_hash,
    read_manifest,
    update_manifest,
)
from sova.commands.templates import build_variables, render_command
from sova.config.models import ProjectConfig
from sova.utils.logging import get_logger

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


@dataclass
class DiffResult:
    """Result of a diff_commands() operation."""

    changed: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


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


def _install_files(
    source_files: list[tuple[str, Path]],
    target_dir: Path,
    variables: dict[str, str],
) -> InstallResult:
    """Render and install source files into a target directory with manifest tracking."""
    result = InstallResult()
    hashes: dict[str, str] = {}

    target_dir.mkdir(parents=True, exist_ok=True)

    for filename, source_path in source_files:
        content = source_path.read_text(encoding="utf-8")
        rendered = render_command(content, variables)

        (target_dir / filename).write_text(rendered, encoding="utf-8")
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
) -> UpdateResult:
    """Incrementally update installed files with conflict detection."""
    manifest = read_manifest(target_dir)
    result = UpdateResult()

    if manifest is None:
        install_result = _install_files(source_files, target_dir, variables)
        result.updated = install_result.installed
        return result

    for filename, source_path in source_files:
        content = source_path.read_text(encoding="utf-8")
        rendered = render_command(content, variables)
        new_hash = file_hash(rendered)

        target_path = target_dir / filename
        manifest_entry = manifest.commands.get(filename)

        if manifest_entry is None:
            target_path.write_text(rendered, encoding="utf-8")
            update_manifest(target_dir, filename, new_hash)
            result.updated += 1
            continue

        if manifest_entry.hash == new_hash:
            result.skipped += 1
            continue

        if target_path.exists() and not force:
            installed_hash = file_hash(target_path.read_text(encoding="utf-8"))
            if installed_hash != manifest_entry.hash:
                result.conflicts.append(filename)
                continue

        target_path.write_text(rendered, encoding="utf-8")
        update_manifest(target_dir, filename, new_hash)
        result.updated += 1

    return result


def install_commands(
    canonical_dir: Path,
    target_dir: Path,
    cfg: ProjectConfig,
    *,
    include_autonomous: bool = True,
) -> InstallResult:
    """Install canonical commands into a target project directory."""
    commands = discover(canonical_dir)
    files = [(cmd.path.name, cmd.path) for cmd in commands if include_autonomous or cmd.category != "autonomous"]
    skipped = len(commands) - len(files)

    result = _install_files(files, target_dir, build_variables(cfg))
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
) -> UpdateResult:
    """Update installed commands incrementally."""
    commands = discover(canonical_dir)
    files = [(cmd.path.name, cmd.path) for cmd in commands if include_autonomous or cmd.category != "autonomous"]
    skipped = len(commands) - len(files)

    result = _update_files(files, target_dir, build_variables(cfg), force=force)
    result.skipped += skipped
    return result


def _diff_files(
    source_files: list[tuple[str, Path]],
    target_dir: Path,
    variables: dict[str, str],
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

        content = source_path.read_text(encoding="utf-8")
        rendered = render_command(content, variables)
        new_hash = file_hash(rendered)

        entry = manifest.commands.get(filename)
        if entry is None:
            result.new.append(filename)
        elif entry.hash != new_hash:
            result.changed.append(filename)

    for filename, entry in manifest.commands.items():
        if entry.managed and filename not in canonical_names:
            result.removed.append(filename)

    return result


def diff_commands(
    canonical_dir: Path,
    target_dir: Path,
    cfg: ProjectConfig,
) -> DiffResult:
    """Show what changed between canonical source and installed commands."""
    commands = discover(canonical_dir)
    files = [(cmd.path.name, cmd.path) for cmd in commands]
    return _diff_files(files, target_dir, build_variables(cfg))


def diff_guidelines(
    guidelines_dir: Path,
    target_dir: Path,
    cfg: ProjectConfig,
) -> DiffResult:
    """Show what changed between canonical guidelines and installed ones."""
    files = _collect_guidelines(guidelines_dir)
    return _diff_files(files, target_dir, build_variables(cfg))


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
) -> UpdateResult:
    """Update installed guidelines incrementally."""
    files = _collect_guidelines(guidelines_dir)
    if not files:
        if read_manifest(target_dir) is None:
            return UpdateResult()
        return UpdateResult()

    return _update_files(files, target_dir, build_variables(cfg), force=force)


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
