"""Handoff file management for the dashboard.

Reads, archives, and clears the file-based handoff that agents write
to `.claude/agent-control/handoff.json`. The dashboard polls this file
to render action panels.

Supports multi-project mode via project context or set_project_dir().
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from sova.dashboard.project_context import get_project_dir
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.handoff")

# Default project dir for single-project mode
_default_project_dir: Path | None = None

# Per-project mtime-based cache: project_dir_str -> (mtime, data)
_handoff_caches: dict[str, tuple[float, dict | None]] = {}


def set_project_dir(path: Path) -> None:
    """Set the default project directory (called once during app startup)."""
    global _default_project_dir
    _default_project_dir = path


def _resolve_project_dir() -> Path | None:
    """Resolve the active project directory."""
    ctx_dir = get_project_dir()
    if ctx_dir is not None:
        return ctx_dir
    return _default_project_dir


def _handoff_file(project_dir: Path | None = None) -> Path | None:
    d = project_dir or _resolve_project_dir()
    if d is None:
        return None
    return d / ".claude" / "agent-control" / "handoff.json"


def _archive_dir(project_dir: Path | None = None) -> Path | None:
    d = project_dir or _resolve_project_dir()
    if d is None:
        return None
    return d / ".claude" / "agent-control" / "handoff-archive"


def get_handoff(project_dir: Path | None = None) -> dict | None:
    """Read the current handoff file with mtime-based caching."""
    hf = _handoff_file(project_dir)
    if hf is None:
        return None
    cache_key = str(hf.parent.parent)

    if not hf.exists():
        _handoff_caches[cache_key] = (0.0, None)
        return None

    try:
        mtime = hf.stat().st_mtime
        cached = _handoff_caches.get(cache_key)
        if cached and mtime == cached[0]:
            return cached[1]

        data = json.loads(hf.read_text())
        _handoff_caches[cache_key] = (mtime, data)
        return data
    except (json.JSONDecodeError, OSError):
        return None


def archive_handoff(project_dir: Path | None = None) -> dict | None:
    """Move the current handoff to the archive directory. Returns the archived data."""
    hf = _handoff_file(project_dir)
    if hf is None or not hf.exists():
        return None

    handoff = get_handoff(project_dir)
    if not handoff:
        return None

    archive = _archive_dir(project_dir)
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


def clear_handoff(project_dir: Path | None = None) -> bool:
    """Archive and remove the current handoff file."""
    archive_handoff(project_dir)
    hf = _handoff_file(project_dir)
    if hf is not None and hf.exists():
        hf.unlink()
        cache_key = str(hf.parent.parent)
        _handoff_caches[cache_key] = (0.0, None)
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
