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


def install_commands(
    canonical_dir: Path,
    target_dir: Path,
    cfg: ProjectConfig,
    *,
    include_autonomous: bool = True,
) -> InstallResult:
    """Install canonical commands into a target project directory.

    Renders template variables, writes files, creates manifest.
    Preserves any existing project-local commands.
    """
    commands = discover(canonical_dir)
    variables = build_variables(cfg)
    result = InstallResult()
    hashes: dict[str, str] = {}

    target_dir.mkdir(parents=True, exist_ok=True)

    for cmd in commands:
        if not include_autonomous and cmd.category == "autonomous":
            result.skipped += 1
            continue

        content = cmd.path.read_text(encoding="utf-8")
        rendered = render_command(content, variables)
        target_path = target_dir / cmd.path.name

        target_path.write_text(rendered, encoding="utf-8")
        hashes[cmd.path.name] = file_hash(rendered)
        result.installed += 1

    create_manifest(target_dir, hashes)
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
    """Update installed commands incrementally.

    Only writes commands whose canonical source has changed.
    Detects conflicts where the user has modified a managed command.
    """
    commands = discover(canonical_dir)
    variables = build_variables(cfg)
    manifest = read_manifest(target_dir)
    result = UpdateResult()

    if manifest is None:
        # No manifest = first install
        install_result = install_commands(canonical_dir, target_dir, cfg, include_autonomous=include_autonomous)
        result.updated = install_result.installed
        return result

    for cmd in commands:
        if not include_autonomous and cmd.category == "autonomous":
            result.skipped += 1
            continue

        filename = cmd.path.name
        content = cmd.path.read_text(encoding="utf-8")
        rendered = render_command(content, variables)
        new_hash = file_hash(rendered)

        target_path = target_dir / filename
        manifest_entry = manifest.commands.get(filename)

        if manifest_entry is None:
            # New command not previously installed
            target_path.write_text(rendered, encoding="utf-8")
            update_manifest(target_dir, filename, new_hash)
            result.updated += 1
            continue

        if manifest_entry.hash == new_hash:
            # Source unchanged
            result.skipped += 1
            continue

        # Source changed -- check if user also modified the installed file
        if target_path.exists() and not force:
            installed_hash = file_hash(target_path.read_text(encoding="utf-8"))
            if installed_hash != manifest_entry.hash:
                # User modified this file AND source changed = conflict
                result.conflicts.append(filename)
                continue

        # Safe to update
        target_path.write_text(rendered, encoding="utf-8")
        update_manifest(target_dir, filename, new_hash)
        result.updated += 1

    return result


def diff_commands(
    canonical_dir: Path,
    target_dir: Path,
    cfg: ProjectConfig,
) -> DiffResult:
    """Show what changed between canonical source and installed commands.

    Returns lists of changed, new, and removed command filenames.
    """
    commands = discover(canonical_dir)
    variables = build_variables(cfg)
    manifest = read_manifest(target_dir)
    result = DiffResult()

    if manifest is None:
        # Everything is new
        result.new = [cmd.path.name for cmd in commands]
        return result

    canonical_names = set()
    for cmd in commands:
        filename = cmd.path.name
        canonical_names.add(filename)

        content = cmd.path.read_text(encoding="utf-8")
        rendered = render_command(content, variables)
        new_hash = file_hash(rendered)

        entry = manifest.commands.get(filename)
        if entry is None:
            result.new.append(filename)
        elif entry.hash != new_hash:
            result.changed.append(filename)

    # Check for removed commands (in manifest but not in canonical)
    for filename, entry in manifest.commands.items():
        if entry.managed and filename not in canonical_names:
            result.removed.append(filename)

    return result


def install_guidelines(
    guidelines_dir: Path,
    target_dir: Path,
    cfg: ProjectConfig,
) -> InstallResult:
    """Install guideline templates into a target project's rules directory.

    Renders template variables, writes files, creates a separate manifest
    in the target directory for conflict-aware updates.
    """
    variables = build_variables(cfg)
    result = InstallResult()
    hashes: dict[str, str] = {}

    target_dir.mkdir(parents=True, exist_ok=True)

    if not guidelines_dir.is_dir():
        return result

    for path in sorted(guidelines_dir.glob("*.md")):
        content = path.read_text(encoding="utf-8")
        rendered = render_command(content, variables)
        target_path = target_dir / path.name

        target_path.write_text(rendered, encoding="utf-8")
        hashes[path.name] = file_hash(rendered)
        result.installed += 1

    create_manifest(target_dir, hashes)
    log.info("guidelines.installed", count=result.installed)

    return result


def update_guidelines(
    guidelines_dir: Path,
    target_dir: Path,
    cfg: ProjectConfig,
    *,
    force: bool = False,
) -> UpdateResult:
    """Update installed guidelines incrementally.

    Only writes guidelines whose canonical source has changed.
    Detects conflicts where the user has modified a managed guideline.
    """
    variables = build_variables(cfg)
    manifest = read_manifest(target_dir)
    result = UpdateResult()

    if manifest is None:
        install_result = install_guidelines(guidelines_dir, target_dir, cfg)
        result.updated = install_result.installed
        return result

    if not guidelines_dir.is_dir():
        return result

    for path in sorted(guidelines_dir.glob("*.md")):
        filename = path.name
        content = path.read_text(encoding="utf-8")
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
