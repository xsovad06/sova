"""Handoff state management -- read, write, and archive agent handoff files."""

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app import config

# mtime-based cache
_handoff_cache: tuple[float, dict | None] = (0.0, None)


def _handoff_file() -> Path:
    return config.DATA_DIR / "agent-control" / "handoff.json"


def _archive_dir() -> Path:
    return config.DATA_DIR / "agent-control" / "handoff-archive"


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


def _list_archives(reverse: bool = False) -> list[Path]:
    """List archived handoff files sorted by filename (which contains timestamp)."""
    archive = _archive_dir()
    if not archive.exists():
        return []
    return sorted(archive.glob("*.json"), key=lambda p: p.name, reverse=reverse)


def archive_handoff() -> dict | None:
    """Move the current handoff to the archive directory. Returns the archived handoff."""
    hf = _handoff_file()
    if not hf.exists():
        return None

    handoff = get_handoff()
    if not handoff:
        return None

    archive = _archive_dir()
    archive.mkdir(parents=True, exist_ok=True)

    # Archive with timestamp-based filename
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    source = handoff.get("source", "unknown")
    archive_name = f"{ts}_{source}.json"
    archive_path = archive / archive_name

    shutil.copy2(hf, archive_path)

    # Prune old archives (keep last 50)
    for old in _list_archives()[:-50]:
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


def write_handoff(handoff: dict) -> dict:
    """Write a new handoff file. Archives the previous one if it exists."""
    archive_handoff()

    control_dir = config.DATA_DIR / "agent-control"
    control_dir.mkdir(parents=True, exist_ok=True)

    # Ensure required fields
    if "id" not in handoff:
        handoff["id"] = str(uuid4())
    if "created_at" not in handoff:
        handoff["created_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    hf = _handoff_file()
    hf.write_text(json.dumps(handoff, indent=2))

    global _handoff_cache
    _handoff_cache = (hf.stat().st_mtime, handoff)

    return handoff


def get_archive(limit: int = 20) -> list[dict]:
    """Get recent archived handoffs."""
    result = []
    for path in _list_archives(reverse=True)[:limit]:
        try:
            data = json.loads(path.read_text())
            data["_archive_file"] = path.name
            result.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    return result


def build_action_command(action: dict) -> dict:
    """Build the execution parameters for a handoff action.

    Returns a dict with the fields needed by process_service to start
    the appropriate agent or command.
    """
    mode = action.get("mode", "")
    command = action.get("command", "")
    args = action.get("args", {})

    if mode == "agent":
        # Map to pak-agent.sh mode
        agent_mode = command  # e.g., "handle-pr"
        ticket = args.get("ticket") or str(args.get("pr", ""))
        return {"type": "agent", "mode": agent_mode, "ticket": ticket}

    elif mode == "claude-command":
        # Run a Claude Code command file
        return {"type": "claude-command", "command": command, "args": args}

    elif mode == "shell":
        # Raw shell command
        return {"type": "shell", "command": command, "args": args}

    return {"type": "unknown", "error": f"Unknown mode: {mode}"}
