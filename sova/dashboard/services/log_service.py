"""Log service -- read and filter structured log files."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

# Cache: path -> (mtime, parsed_lines)
_log_cache: dict[str, tuple[float, list[dict]]] = {}

_META_KEYS = frozenset({"component", "level", "timestamp", "event", "message"})


def _build_message(entry: dict) -> str:
    """Build a human-readable message from structlog event + context fields."""
    event = entry.get("event", "")
    context = {k: v for k, v in entry.items() if k not in _META_KEYS and v is not None}
    if not context:
        return event
    parts = [f"{k}={v}" for k, v in context.items()]
    return f"{event}  ({', '.join(parts)})"


def _get_log_path(project_dir: Path | None = None) -> Path:
    """Resolve the log file path for a project."""
    if project_dir is None:
        project_dir = Path.cwd()
    return project_dir / ".claude" / "sova.log"


def _parse_log_file(log_path: Path) -> list[dict]:
    """Parse a JSON-per-line log file with mtime-based caching."""
    path_str = str(log_path)

    if log_path.exists():
        mtime = os.path.getmtime(log_path)
        cached = _log_cache.get(path_str)
        if cached and cached[0] == mtime:
            return cached[1]
    else:
        return []

    entries = []
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if "message" not in entry:
                    entry["message"] = _build_message(entry)
                entries.append(entry)
            except json.JSONDecodeError:
                entries.append(
                    {
                        "level": "INFO",
                        "message": line,
                        "component": "unknown",
                        "timestamp": "",
                    }
                )

    _log_cache[path_str] = (mtime, entries)
    return entries


async def get_logs(
    project_dir: Path | None = None,
    *,
    level: str = "",
    component: str = "",
    search: str = "",
    limit: int = 200,
    offset: int = 0,
) -> dict:
    """Get filtered log entries.

    Returns dict with 'entries' (list of log dicts) and 'total' (count before limit).
    """
    log_path = _get_log_path(project_dir)
    all_entries = await asyncio.to_thread(_parse_log_file, log_path)

    filtered = all_entries
    if level:
        level_upper = level.upper()
        filtered = [e for e in filtered if e.get("level", "").upper() == level_upper]
    if component:
        filtered = [e for e in filtered if e.get("component", "") == component]
    if search:
        search_lower = search.lower()
        filtered = [e for e in filtered if search_lower in e.get("message", "").lower()]

    total = len(filtered)

    # Most recent first, then paginate
    filtered = list(reversed(filtered))
    paginated = filtered[offset : offset + limit]

    return {"entries": paginated, "total": total}


async def get_components(project_dir: Path | None = None) -> list[str]:
    """Get distinct component names from the log file."""
    log_path = _get_log_path(project_dir)
    all_entries = await asyncio.to_thread(_parse_log_file, log_path)
    components = sorted({e.get("component", "") for e in all_entries if e.get("component")})
    return components
