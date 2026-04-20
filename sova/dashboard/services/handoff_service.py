"""Handoff file management for the dashboard.

Reads, archives, and clears the file-based handoff that agents write
to `.claude/agent-control/handoff.json`. The dashboard polls this file
to render action panels.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from sova.utils.logging import get_logger

log = get_logger(component="dashboard.handoff")

# Module-level state set by set_project_dir()
_project_dir: Path | None = None

# mtime-based cache to avoid re-reading unchanged files
_handoff_cache: tuple[float, dict | None] = (0.0, None)


def set_project_dir(path: Path) -> None:
    """Set the project directory (called once during app startup)."""
    global _project_dir
    _project_dir = path


def _handoff_file() -> Path:
    assert _project_dir is not None, "handoff_service.set_project_dir() not called"
    return _project_dir / ".claude" / "agent-control" / "handoff.json"


def _archive_dir() -> Path:
    assert _project_dir is not None
    return _project_dir / ".claude" / "agent-control" / "handoff-archive"


def get_handoff() -> dict | None:
    """Read the current handoff file with mtime-based caching."""
    global _handoff_cache

    hf = _handoff_file()
    if not hf.exists():
        _handoff_cache = (0.0, None)
        return None

    try:
        mtime = hf.stat().st_mtime
        if mtime == _handoff_cache[0]:
            return _handoff_cache[1]

        data = json.loads(hf.read_text())
        _handoff_cache = (mtime, data)
        return data
    except (json.JSONDecodeError, OSError):
        return None


def archive_handoff() -> dict | None:
    """Move the current handoff to the archive directory. Returns the archived data."""
    hf = _handoff_file()
    if not hf.exists():
        return None

    handoff = get_handoff()
    if not handoff:
        return None

    archive = _archive_dir()
    archive.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    source = handoff.get("source", "unknown")
    archive_name = f"{ts}_{source}.json"
    shutil.copy2(hf, archive / archive_name)

    # Prune old archives (keep last 50)
    archives = sorted(archive.glob("*.json"), key=lambda p: p.name)
    for old in archives[:-50]:
        old.unlink(missing_ok=True)

    return handoff


def clear_handoff() -> bool:
    """Archive and remove the current handoff file."""
    archive_handoff()
    hf = _handoff_file()
    if hf.exists():
        hf.unlink()
        global _handoff_cache
        _handoff_cache = (0.0, None)
        return True
    return False


def build_action_command(action: dict) -> dict:
    """Map a handoff action to execution parameters.

    Returns a dict with fields needed by control_service to start
    the appropriate agent or command.
    """
    mode = action.get("mode", "")
    command = action.get("command", "")
    args = action.get("args", {})

    if mode == "agent":
        ticket = args.get("ticket") or str(args.get("pr", ""))
        return {"type": "agent", "issue": ticket, "role": args.get("role")}

    if mode == "claude-command":
        return {"type": "claude-command", "command": command, "args": args}

    if mode == "shell":
        return {"type": "shell", "command": command}

    return {"type": "unknown", "error": f"Unknown mode: {mode}"}
