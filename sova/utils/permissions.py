"""Agent permission management for .claude/settings.json."""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_REQUIRED_PERMISSIONS = ["Bash(*)", "Read(*)", "Edit(*)", "Write(*)"]
_SETTINGS_FILENAME = "settings.json"


def inject_agent_permissions(claude_dir: Path) -> bool:
    """Add agent permission entries to .claude/settings.json.

    Uses read-modify-write with dedup to avoid destroying user config.
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

        allow.extend(perm for perm in _REQUIRED_PERMISSIONS if perm not in existing_set)

        settings_path.write_text(json.dumps(data, indent=2) + "\n")
        return True
    except OSError as exc:
        logger.warning("Failed to inject agent permissions: %s", exc, exc_info=True)
        return False


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


def remove_agent_permissions(claude_dir: Path) -> bool:
    """Remove agent permission entries from .claude/settings.json.

    Returns True if permissions were removed, False if not found or error.
    """
    settings_path = claude_dir / _SETTINGS_FILENAME

    try:
        try:
            data = json.loads(settings_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return False

        if not isinstance(data, dict):
            return False

        permissions = data.get("permissions", {})
        if not isinstance(permissions, dict):
            return False

        allow = permissions.get("allow", [])
        if not isinstance(allow, list):
            return False

        original_len = len(allow)
        try:
            required_set = set(_REQUIRED_PERMISSIONS)
            allow[:] = [p for p in allow if p not in required_set]
        except TypeError:
            logger.warning("permissions.allow contains non-hashable items, skipping agent permission removal")
            return False

        if len(allow) == original_len:
            return False

        # Clean up empty structures
        if not allow:
            del permissions["allow"]
        if not permissions:
            del data["permissions"]

        settings_path.write_text(json.dumps(data, indent=2) + "\n")
        return True
    except OSError as exc:
        logger.warning("Failed to remove agent permissions: %s", exc, exc_info=True)
        return False
