"""Agent permission management for .claude/settings.json."""

from __future__ import annotations

import json
import logging
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

_REQUIRED_PERMISSIONS = ["Bash(*)", "Read(*)", "Edit(*)", "Write(*)"]
_SETTINGS_FILENAME = "settings.json"
_ADDED_MARKER_FILENAME = ".sova-added-permissions.json"


class PermissionRemovalResult(str, Enum):
    """Outcome of an attempted removal of SOVA-added permission entries.

    A bare bool would make a settings.json read/write failure indistinguishable
    from "nothing recorded as added", so a caller checking only truthiness could
    report uninstall cleanup as complete when the file was never touched.
    """

    CHANGED = "changed"
    UNCHANGED = "unchanged"
    ERROR = "error"


def inject_agent_permissions(claude_dir: Path) -> bool:
    """Add agent permission entries to .claude/settings.json.

    Uses read-modify-write with dedup to avoid destroying user config. Also
    records exactly which entries were newly added (not already present) in
    a sidecar marker file, so remove_agent_permissions can later remove only
    the entries SOVA itself introduced and leave an identical entry that
    predates SOVA untouched.
    Returns True if permissions were added, False if skipped (already present or error).
    """
    settings_path = claude_dir / _SETTINGS_FILENAME

    try:
        try:
            data = json.loads(settings_path.read_text())
        except FileNotFoundError:
            data = {}
        except json.JSONDecodeError:
            logger.warning("Malformed .claude/settings.json, skipping agent permission injection")
            return False

        if not isinstance(data, dict):
            logger.warning("Malformed .claude/settings.json, skipping agent permission injection")
            return False

        permissions = data.setdefault("permissions", {})
        if not isinstance(permissions, dict):
            logger.warning("Unexpected type for permissions in settings.json, skipping agent permission injection")
            return False

        allow = permissions.setdefault("allow", [])
        if not isinstance(allow, list):
            logger.warning(
                "Unexpected type for permissions.allow in settings.json, skipping agent permission injection"
            )
            return False

        try:
            existing_set = set(allow)
        except TypeError:
            logger.warning("permissions.allow contains non-hashable items, skipping agent permission injection")
            return False
        required_set = set(_REQUIRED_PERMISSIONS)
        if required_set.issubset(existing_set):
            logger.debug("Agent permissions already present in settings.json")
            return False

        added = [perm for perm in _REQUIRED_PERMISSIONS if perm not in existing_set]
        allow.extend(added)

        settings_path.write_text(json.dumps(data, indent=2) + "\n")
        _record_added_permissions(claude_dir, added)
        return True
    except OSError as exc:
        logger.warning("Failed to inject agent permissions: %s", exc, exc_info=True)
        return False


def _record_added_permissions(claude_dir: Path, added: list[str]) -> None:
    """Persist exactly which permission entries this install added."""
    marker_path = claude_dir / _ADDED_MARKER_FILENAME
    try:
        combined = sorted(set(_read_added_permissions(marker_path)) | set(added))
        marker_path.write_text(json.dumps({"added": combined}, indent=2) + "\n")
    except OSError:
        logger.warning("Failed to record which agent permissions were added", exc_info=True)


def _read_added_permissions(marker_path: Path) -> list[str]:
    try:
        data = json.loads(marker_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    added = data.get("added", [])
    if not isinstance(added, list):
        return []
    return [perm for perm in added if isinstance(perm, str)]


def _unlink_marker(marker_path: Path) -> None:
    try:
        marker_path.unlink(missing_ok=True)
    except OSError:
        logger.debug("Failed to remove the agent permissions marker file", exc_info=True)


def check_agent_permissions(claude_dir: Path) -> tuple[bool, list[str]]:
    """Check if .claude/settings.json has all required agent permissions.

    Returns (has_all, missing) where:
    - has_all: True if all required permissions are present
    - missing: list of missing permission entries
    """
    settings_path = claude_dir / _SETTINGS_FILENAME

    try:
        try:
            data = json.loads(settings_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return (False, _REQUIRED_PERMISSIONS.copy())

        if not isinstance(data, dict):
            return (False, _REQUIRED_PERMISSIONS.copy())

        permissions = data.get("permissions", {})
        if not isinstance(permissions, dict):
            return (False, _REQUIRED_PERMISSIONS.copy())

        allow = permissions.get("allow", [])
        if not isinstance(allow, list):
            return (False, _REQUIRED_PERMISSIONS.copy())

        try:
            existing_set = set(allow)
        except TypeError:
            logger.warning("permissions.allow contains non-hashable items")
            return (False, _REQUIRED_PERMISSIONS.copy())
        required_set = set(_REQUIRED_PERMISSIONS)
        missing = sorted(required_set - existing_set)

        return (len(missing) == 0, missing)
    except OSError:
        return (False, _REQUIRED_PERMISSIONS.copy())


def remove_agent_permissions(claude_dir: Path) -> PermissionRemovalResult:
    """Remove the agent permission entries SOVA itself added to .claude/settings.json.

    Only entries recorded by inject_agent_permissions's marker file are removed,
    so a Bash(*)/Read(*)/Edit(*)/Write(*) entry that predates SOVA, or that
    some other tool added, survives uninstall untouched. Without that record
    (no marker file, e.g. nothing was ever injected) this is a no-op rather
    than a guess at what to remove.

    This is deliberately a permanent no-op for any project installed before
    the marker file existed: inject_agent_permissions short-circuits without
    writing a marker whenever the four permissions are already present, which
    is exactly the state of every pre-existing install, and re-running
    install adds nothing new to record either. There is no retroactive
    migration, so uninstall on such a project returns UNCHANGED and leaves
    the permissions in settings.json, with no separate message to the user.
    """
    marker_path = claude_dir / _ADDED_MARKER_FILENAME
    added = _read_added_permissions(marker_path)
    if not added:
        return PermissionRemovalResult.UNCHANGED

    settings_path = claude_dir / _SETTINGS_FILENAME

    try:
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            _unlink_marker(marker_path)
            return PermissionRemovalResult.UNCHANGED

        if not isinstance(data, dict):
            return PermissionRemovalResult.UNCHANGED

        permissions = data.get("permissions", {})
        if not isinstance(permissions, dict):
            return PermissionRemovalResult.UNCHANGED

        allow = permissions.get("allow", [])
        if not isinstance(allow, list):
            return PermissionRemovalResult.UNCHANGED

        original_len = len(allow)
        try:
            added_set = set(added)
            allow[:] = [p for p in allow if p not in added_set]
        except TypeError:
            logger.warning("permissions.allow contains non-hashable items, skipping agent permission removal")
            return PermissionRemovalResult.ERROR

        if len(allow) == original_len:
            _unlink_marker(marker_path)
            return PermissionRemovalResult.UNCHANGED

        # Clean up empty structures
        if not allow:
            del permissions["allow"]
        if not permissions:
            del data["permissions"]

        settings_path.write_text(json.dumps(data, indent=2) + "\n")
    except OSError as exc:
        logger.warning("Failed to remove agent permissions: %s", exc, exc_info=True)
        return PermissionRemovalResult.ERROR

    _unlink_marker(marker_path)
    return PermissionRemovalResult.CHANGED
