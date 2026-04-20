"""Manifest tracking for installed commands.

Manages ``.sova-manifest.json`` in the target project's commands directory,
tracking which commands are managed by SOVA, their content hashes, and
distinguishing them from project-local commands.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from sova.utils.logging import get_logger

log = get_logger(component="commands.manifest")

MANIFEST_FILENAME = ".sova-manifest.json"
MANIFEST_VERSION = 1


@dataclass
class ManifestEntry:
    """A single command entry in the manifest."""

    hash: str
    managed: bool = True


@dataclass
class Manifest:
    """The full manifest tracking installed commands."""

    version: int = MANIFEST_VERSION
    commands: dict[str, ManifestEntry] = field(default_factory=dict)


def create_manifest(target_dir: Path, entries: dict[str, str]) -> None:
    """Create a new manifest file from a dict of filename -> content hash."""
    data = {
        "version": MANIFEST_VERSION,
        "commands": {name: {"hash": h, "managed": True} for name, h in entries.items()},
    }
    manifest_path = target_dir / MANIFEST_FILENAME
    manifest_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    log.info("manifest.created", path=str(manifest_path), count=len(entries))


def read_manifest(target_dir: Path) -> Manifest | None:
    """Read and parse the manifest file. Returns None if not found."""
    manifest_path = target_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return None

    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("manifest.read_error", path=str(manifest_path))
        return None

    manifest = Manifest(version=data.get("version", MANIFEST_VERSION))
    for name, entry_data in data.get("commands", {}).items():
        manifest.commands[name] = ManifestEntry(
            hash=entry_data.get("hash", ""),
            managed=entry_data.get("managed", True),
        )

    return manifest


def update_manifest(target_dir: Path, filename: str, content_hash: str) -> None:
    """Update a single entry in the manifest."""
    manifest = read_manifest(target_dir)
    if manifest is None:
        create_manifest(target_dir, {filename: content_hash})
        return

    manifest.commands[filename] = ManifestEntry(hash=content_hash, managed=True)
    _write_manifest(target_dir, manifest)


def remove_from_manifest(target_dir: Path, filename: str) -> None:
    """Remove a command entry from the manifest."""
    manifest = read_manifest(target_dir)
    if manifest is None:
        return

    manifest.commands.pop(filename, None)
    _write_manifest(target_dir, manifest)


def file_hash(content: str) -> str:
    """Compute a SHA-256 hash of file content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _write_manifest(target_dir: Path, manifest: Manifest) -> None:
    """Write manifest to disk."""
    data = {
        "version": manifest.version,
        "commands": {name: {"hash": e.hash, "managed": e.managed} for name, e in manifest.commands.items()},
    }
    manifest_path = target_dir / MANIFEST_FILENAME
    manifest_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
