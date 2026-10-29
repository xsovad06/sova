"""RTK (context compression) integration: settings.json hook management."""

from __future__ import annotations

import json
import shutil
from enum import Enum
from pathlib import Path

from sova.utils.logging import get_logger

logger = get_logger(component="utils.rtk")
# No warning in this module passes ``exc_info=True``: settings.json can hold an ``env`` block with credentials, and
# these frames hold its raw text (``json.loads``) or parsed content (``data``), which a renderer that shows frame
# locals (rich's, outside the fallback configured in ``sova/utils/logging.py``) would print. A
# ``UnicodeDecodeError`` also carries the whole undecodable file in ``.object``. ``str(exc)`` names the cause
# (the OS error, or the byte and position) without those values.

_RTK_COMMAND = "rtk hook claude"
_LEGACY_RTK_COMMAND = "rtk"
_RTK_MATCHER = "Bash"


class HookRemovalResult(str, Enum):
    """Outcome of an attempted removal of the RTK hook from settings.json.

    A bare bool would make a read/write failure indistinguishable from "no hook was there", so a
    caller checking only truthiness could report an uninstall as clean while the hook stays in the
    file. Mirrors ``PermissionRemovalResult`` in ``sova/utils/permissions.py``.
    """

    REMOVED = "removed"
    NOT_FOUND = "not_found"
    ERROR = "error"


def _rtk_entry() -> dict:
    """Return a new RTK PreToolUse entry in the matcher format Claude Code requires.

    Claude Code rejects the whole settings.json, permissions included, when a
    PreToolUse entry has no ``hooks`` list, so the command is never written as
    a bare top-level entry.
    """
    return {"matcher": _RTK_MATCHER, "hooks": [{"type": "command", "command": _RTK_COMMAND}]}


def is_rtk_available() -> bool:
    """Check if the RTK binary is installed and executable."""
    return shutil.which("rtk") is not None


def inject_rtk_hook(claude_dir: Path) -> bool:
    """Inject the RTK PreToolUse hook into .claude/settings.json.

    Uses read-modify-write with dedup to avoid destroying user config.
    Rewrites flat RTK entries into the matcher format in place and upgrades
    legacy ``"rtk"`` entries to ``"rtk hook claude"``; other entries are left
    untouched. Returns True if the hook was added, rewritten or upgraded,
    False if already current or error.
    """
    settings_path = claude_dir / "settings.json"

    try:
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("rtk.settings_malformed", path=str(settings_path))
            return False

        if not isinstance(data, dict):
            logger.warning("rtk.settings_malformed", path=str(settings_path))
            return False

        hooks = data.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            logger.warning("rtk.hooks_type_unexpected", path=str(settings_path))
            return False

        pre_tool_use = hooks.setdefault("PreToolUse", [])
        if not isinstance(pre_tool_use, list):
            logger.warning("rtk.pre_tool_use_type_unexpected", path=str(settings_path))
            return False

        changed = _nest_flat_entries(pre_tool_use)
        changed = _upgrade_legacy_entries(pre_tool_use) or changed
        if not any(_is_rtk_entry(h) for h in pre_tool_use):
            pre_tool_use.append(_rtk_entry())
            changed = True

        if not changed:
            logger.debug("rtk.hook_already_present", path=str(settings_path))
            return False

        settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return True
    except OSError as exc:
        logger.warning("rtk.inject_failed", path=str(settings_path), error=str(exc))
        return False


def remove_rtk_hook(claude_dir: Path) -> HookRemovalResult:
    """Remove the RTK PreToolUse hook from .claude/settings.json.

    Returns REMOVED if a hook was removed, NOT_FOUND if settings.json is absent, malformed or has no
    RTK entry, and ERROR if settings.json could not be read as UTF-8 or the edited file could not be
    written: the hook may well still be in it, and reporting NOT_FOUND would let an uninstall claim
    there was nothing to remove.
    """
    settings_path = claude_dir / "settings.json"

    try:
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return HookRemovalResult.NOT_FOUND
        except UnicodeDecodeError as exc:
            logger.warning("rtk.remove_unreadable", path=str(settings_path), error=str(exc))
            return HookRemovalResult.ERROR

        if not isinstance(data, dict):
            return HookRemovalResult.NOT_FOUND

        hooks = data.get("hooks", {})
        if not isinstance(hooks, dict):
            return HookRemovalResult.NOT_FOUND

        pre_tool_use = hooks.get("PreToolUse", [])
        if not isinstance(pre_tool_use, list):
            return HookRemovalResult.NOT_FOUND

        original_len = len(pre_tool_use)
        pre_tool_use[:] = [h for h in pre_tool_use if not _is_rtk_entry(h) and not _is_legacy_rtk_entry(h)]

        if len(pre_tool_use) == original_len:
            return HookRemovalResult.NOT_FOUND

        # Clean up empty structures
        if not pre_tool_use:
            del hooks["PreToolUse"]
        if not hooks:
            del data["hooks"]

        settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return HookRemovalResult.REMOVED
    except OSError as exc:
        logger.warning("rtk.remove_failed", path=str(settings_path), error=str(exc))
        return HookRemovalResult.ERROR


def _is_flat_rtk_entry(hook: dict) -> bool:
    """Check if a hook dict is an RTK command written as a bare top-level entry."""
    if not isinstance(hook, dict) or "hooks" in hook:
        return False
    return hook.get("command") in (_RTK_COMMAND, _LEGACY_RTK_COMMAND)


def _nest_flat_entries(entries: list) -> bool:
    """Rewrite flat RTK entries into the matcher format, keeping exactly one RTK entry.

    The first flat entry is wrapped in place, keeping any keys the user added
    (``timeout``, their own ``matcher``), unless a nested RTK entry already
    exists; every other flat RTK entry is dropped, since a duplicate would run
    the hook twice. Returns True if any entry was rewritten or dropped.
    """
    flat_indexes = [index for index, entry in enumerate(entries) if _is_flat_rtk_entry(entry)]
    if not flat_indexes:
        return False

    has_nested = any(
        not _is_flat_rtk_entry(entry) and (_is_rtk_entry(entry) or _is_legacy_rtk_entry(entry)) for entry in entries
    )
    first = flat_indexes[0]
    if not has_nested:
        flat = entries[first]
        hook = {key: value for key, value in flat.items() if key != "matcher"}
        hook.setdefault("type", "command")
        hook["command"] = _RTK_COMMAND
        entries[first] = {"matcher": flat.get("matcher", _RTK_MATCHER), "hooks": [hook]}
        flat_indexes = flat_indexes[1:]

    for index in reversed(flat_indexes):
        del entries[index]
    return True


def _is_rtk_entry(hook: dict) -> bool:
    """Check if a hook dict is an RTK entry running the current ``rtk hook claude`` command.

    Matches the nested matcher format (``{"matcher": "Bash", "hooks": [{"command": "rtk hook claude"}]}``)
    and the flat format (``{"command": "rtk hook claude"}``), which Claude Code rejects and which is
    recognized only so it can be rewritten or removed.
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
    if cmd == _LEGACY_RTK_COMMAND:
        return True
    nested = hook.get("hooks")
    if isinstance(nested, list):
        return any(isinstance(h, dict) and h.get("command") == _LEGACY_RTK_COMMAND for h in nested)
    return False


def _upgrade_legacy_entries(entries: list) -> bool:
    """Replace legacy ``"rtk"`` commands inside nested entries with ``"rtk hook claude"`` in-place.

    Flat legacy entries are handled by :func:`_nest_flat_entries`, which runs first.
    Returns True if any entries were upgraded.
    """
    upgraded = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        nested = entry.get("hooks")
        if isinstance(nested, list):
            for h in nested:
                if isinstance(h, dict) and h.get("command") == _LEGACY_RTK_COMMAND:
                    h["command"] = _RTK_COMMAND
                    upgraded = True
    return upgraded
