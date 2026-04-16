"""Parse agent.log — structured log entries."""

import re

from app import config

# Format: [2026-04-15T10:42:08Z] [INFO] [preflight] Message text
LOG_RE = re.compile(
    r"\[(\d{4}-\d{2}-\d{2}T[\d:]+Z)\]\s+"
    r"\[(\w+)\]\s+"
    r"\[([\w.-]+)\]\s+"
    r"(.*)"
)

# mtime-based cache
_cache: tuple[float, list[dict]] = (0.0, [])


def _parse_all() -> list[dict]:
    global _cache
    log_file = config.LOG_FILE
    if not log_file.exists():
        return []
    mtime = log_file.stat().st_mtime
    if mtime == _cache[0]:
        return _cache[1]
    entries = []
    for line in log_file.read_text().splitlines():
        m = LOG_RE.match(line.strip())
        if m:
            entries.append(
                {
                    "timestamp": m.group(1),
                    "level": m.group(2),
                    "component": m.group(3),
                    "message": m.group(4),
                }
            )
    _cache = (mtime, entries)
    return entries


def get_logs(
    level: str | None = None,
    component: str | None = None,
    since: str | None = None,
    search: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    entries = _parse_all()

    if level:
        entries = [e for e in entries if e["level"] == level.upper()]
    if component:
        entries = [e for e in entries if e["component"] == component]
    if since:
        entries = [e for e in entries if e["timestamp"] >= since]
    if search:
        q = search.lower()
        entries = [e for e in entries if q in e["message"].lower()]

    # Return newest first
    entries.reverse()
    return entries[offset : offset + limit]


def get_recent(n: int = 10) -> list[dict]:
    entries = _parse_all()
    return list(reversed(entries[-n:]))


def get_components() -> list[str]:
    return sorted({e["component"] for e in _parse_all()})


def get_counts() -> dict:
    entries = _parse_all()
    counts = {"INFO": 0, "WARN": 0, "ERROR": 0}
    for e in entries:
        lvl = e["level"]
        counts[lvl] = counts.get(lvl, 0) + 1
    return {"total": len(entries), "by_level": counts}
