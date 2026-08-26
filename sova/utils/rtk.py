"""RTK (context compression) integration -- settings.json hook management."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

_RTK_COMMAND = "rtk hook claude"
_RTK_HOOK = {
    "type": "command",
    "command": _RTK_COMMAND,
}


def is_rtk_available() -> bool:
    """Check if the RTK binary is installed and executable."""
    return shutil.which("rtk") is not None


def inject_rtk_hook(claude_dir: Path) -> bool:
    """Inject the RTK PreToolUse hook into .claude/settings.json.

    Uses read-modify-write with dedup to avoid destroying user config.
    Upgrades legacy ``"rtk"`` entries to ``"rtk hook claude"`` in-place.
    Returns True if the hook was added or upgraded, False if already current or error.
    """
    settings_path = claude_dir / "settings.json"

    try:
        try:
            data = json.loads(settings_path.read_text())
        except FileNotFoundError:
            data = {}
        except json.JSONDecodeError:
            logger.warning("Malformed .claude/settings.json -- skipping RTK hook injection")
            return False

        if not isinstance(data, dict):
            logger.warning("Malformed .claude/settings.json -- skipping RTK hook injection")
            return False

        hooks = data.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            logger.warning("Unexpected type for hooks in settings.json -- skipping RTK hook injection")
            return False

        pre_tool_use = hooks.setdefault("PreToolUse", [])
        if not isinstance(pre_tool_use, list):
            logger.warning("Unexpected type for PreToolUse in settings.json -- skipping RTK hook injection")
            return False

        upgraded = _upgrade_legacy_entries(pre_tool_use)
        if upgraded:
            settings_path.write_text(json.dumps(data, indent=2) + "\n")
            return True

        if any(_is_rtk_entry(h) for h in pre_tool_use):
            logger.debug("RTK hook already present in settings.json")
            return False

        pre_tool_use.append(_RTK_HOOK)
        settings_path.write_text(json.dumps(data, indent=2) + "\n")
        return True
    except OSError as exc:
        logger.warning("Failed to inject RTK hook: %s", exc, exc_info=True)
        return False


def remove_rtk_hook(claude_dir: Path) -> bool:
    """Remove the RTK PreToolUse hook from .claude/settings.json.

    Returns True if a hook was removed, False if not found or error.
    """
    settings_path = claude_dir / "settings.json"

    try:
        try:
            data = json.loads(settings_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return False

        if not isinstance(data, dict):
            return False

        hooks = data.get("hooks", {})
        if not isinstance(hooks, dict):
            return False

        pre_tool_use = hooks.get("PreToolUse", [])
        if not isinstance(pre_tool_use, list):
            return False

        original_len = len(pre_tool_use)
        pre_tool_use[:] = [h for h in pre_tool_use if not _is_rtk_entry(h) and not _is_legacy_rtk_entry(h)]

        if len(pre_tool_use) == original_len:
            return False

        # Clean up empty structures
        if not pre_tool_use:
            del hooks["PreToolUse"]
        if not hooks:
            del data["hooks"]

        settings_path.write_text(json.dumps(data, indent=2) + "\n")
        return True
    except OSError as exc:
        logger.warning("Failed to remove RTK hook: %s", exc, exc_info=True)
        return False


def _is_rtk_entry(hook: dict) -> bool:
    """Check if a hook dict is a current-format RTK entry.

    Matches the flat format (``{"command": "rtk hook claude"}``) and
    the nested matcher format (``{"matcher": "Bash", "hooks": [{"command": "rtk hook claude"}]}``).
    """
    if not isinstance(hook, dict):
        return False
    cmd = hook.get("command")
    if cmd == _RTK_COMMAND:
        return True
    nested = hook.get("hooks")
    if isinstance(nested, list):
        return any(isinstance(h, dict) and h.get("command") == _RTK_COMMAND for h in nested)
    return False


def _is_legacy_rtk_entry(hook: dict) -> bool:
    """Check if a hook dict uses the legacy bare ``"rtk"`` command."""
    if not isinstance(hook, dict):
        return False
    cmd = hook.get("command")
    if cmd == "rtk":
        return True
    nested = hook.get("hooks")
    if isinstance(nested, list):
        return any(isinstance(h, dict) and h.get("command") == "rtk" for h in nested)
    return False


def _upgrade_legacy_entries(entries: list) -> bool:
    """Replace legacy ``"rtk"`` entries with ``"rtk hook claude"`` in-place.

    Returns True if any entries were upgraded.
    """
    upgraded = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("command") == "rtk":
            entry["command"] = _RTK_COMMAND
            upgraded = True
        nested = entry.get("hooks")
        if isinstance(nested, list):
            for h in nested:
                if isinstance(h, dict) and h.get("command") == "rtk":
                    h["command"] = _RTK_COMMAND
                    upgraded = True
    return upgraded
