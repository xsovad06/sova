"""Parse costs.jsonl and aggregate cost data."""

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import config

# mtime-based cache to avoid re-parsing on every request
_cache: tuple[float, list[dict]] = (0.0, [])


def _parse_lines(path: Path) -> list[dict]:
    global _cache
    if not path.exists():
        return []
    mtime = path.stat().st_mtime
    if mtime == _cache[0]:
        return _cache[1]
    entries = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    _cache = (mtime, entries)
    return entries


def get_all() -> list[dict]:
    return _parse_lines(config.COSTS_FILE)


def get_today_total() -> float:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return sum(
        e.get("cost_usd", 0)
        for e in _parse_lines(config.COSTS_FILE)
        if e.get("timestamp", "").startswith(today)
    )


def get_rolling_total(days: int = 7) -> float:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return sum(
        e.get("cost_usd", 0)
        for e in _parse_lines(config.COSTS_FILE)
        if e.get("timestamp", "") >= cutoff
    )


def get_daily_totals(days: int = 14) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    by_day: dict[str, float] = defaultdict(float)
    for e in _parse_lines(config.COSTS_FILE):
        ts = e.get("timestamp", "")
        if not ts:
            continue
        day = ts[:10]
        try:
            if datetime.fromisoformat(ts.replace("Z", "+00:00")) >= cutoff:
                by_day[day] += e.get("cost_usd", 0)
        except ValueError:
            continue
    return [{"date": d, "cost_usd": round(v, 4)} for d, v in sorted(by_day.items())]


def get_by_ticket() -> list[dict]:
    by_ticket: dict[str, dict] = defaultdict(lambda: {"cost_usd": 0, "sessions": 0, "tokens_in": 0, "tokens_out": 0})
    for e in _parse_lines(config.COSTS_FILE):
        key = e.get("jira_key", "unknown")
        by_ticket[key]["cost_usd"] += e.get("cost_usd", 0)
        by_ticket[key]["sessions"] += 1
        by_ticket[key]["tokens_in"] += e.get("input_tokens", 0)
        by_ticket[key]["tokens_out"] += e.get("output_tokens", 0)
    return [
        {"ticket": k, **{kk: round(vv, 4) if isinstance(vv, float) else vv for kk, vv in v.items()}}
        for k, v in sorted(by_ticket.items(), key=lambda x: x[1]["cost_usd"], reverse=True)
    ]


def get_by_phase() -> list[dict]:
    by_phase: dict[str, dict] = defaultdict(lambda: {"cost_usd": 0, "count": 0})
    for e in _parse_lines(config.COSTS_FILE):
        phase = e.get("phase", "unknown")
        by_phase[phase]["cost_usd"] += e.get("cost_usd", 0)
        by_phase[phase]["count"] += 1
    return [
        {"phase": k, "cost_usd": round(v["cost_usd"], 4), "count": v["count"]}
        for k, v in sorted(by_phase.items(), key=lambda x: x[1]["cost_usd"], reverse=True)
    ]


def get_summary() -> dict:
    entries = _parse_lines(config.COSTS_FILE)
    total = sum(e.get("cost_usd", 0) for e in entries)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_total = sum(e.get("cost_usd", 0) for e in entries if e.get("timestamp", "").startswith(today))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    rolling = sum(e.get("cost_usd", 0) for e in entries if e.get("timestamp", "") >= cutoff)
    return {
        "total_cost_usd": round(total, 4),
        "total_sessions": len(entries),
        "today_cost_usd": round(today_total, 4),
        "rolling_7d_usd": round(rolling, 4),
    }
