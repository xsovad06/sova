"""Read task state files and parse task history."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from app import config


def get_active_tasks() -> list[dict]:
    """Read all task-state.json files from worktree directories."""
    worktree_dir = config.WORKTREE_DIR
    if not worktree_dir.exists():
        return []
    tasks = []
    for ticket_dir in worktree_dir.iterdir():
        if not ticket_dir.is_dir():
            continue
        state_file = ticket_dir / "task-state.json"
        if not state_file.exists():
            continue
        try:
            data = json.loads(state_file.read_text())
            # Calculate time in current state
            updated = data.get("updated_at", "")
            if updated:
                try:
                    dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                    delta = datetime.now(timezone.utc) - dt
                    data["time_in_state"] = _format_duration(delta.total_seconds())
                except ValueError:
                    data["time_in_state"] = "unknown"
            tasks.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    return sorted(tasks, key=lambda t: t.get("updated_at", ""), reverse=True)


def get_task_history() -> list[dict]:
    """Parse task-history.md markdown table."""
    history_file = config.TASK_HISTORY_FILE
    if not history_file.exists():
        return []
    content = history_file.read_text()
    # Match markdown table rows (skip header and separator)
    row_re = re.compile(r"^\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|$")
    entries = []
    header_seen = False
    for line in content.splitlines():
        m = row_re.match(line.strip())
        if not m:
            continue
        if not header_seen:
            header_seen = True
            continue
        # Skip separator line
        if m.group(1).startswith("-"):
            continue
        entries.append({
            "date": m.group(1).strip(),
            "ticket": m.group(2).strip(),
            "summary": m.group(3).strip(),
            "outcome": m.group(4).strip(),
        })
    return entries


def get_task_summary() -> dict:
    active = get_active_tasks()
    history = get_task_history()
    in_progress = [t for t in active if t.get("status") == "in_progress"]
    paused = [t for t in active if t.get("status") == "paused"]
    done_count = sum(1 for h in history if "PR" in h.get("outcome", "") or "merged" in h.get("outcome", "").lower())
    failed_count = sum(1 for h in history if "fail" in h.get("outcome", "").lower() or "abort" in h.get("outcome", "").lower())
    return {
        "active_count": len(in_progress) + len(paused),
        "in_progress": len(in_progress),
        "paused": len(paused),
        "completed": done_count,
        "failed": failed_count,
        "total_history": len(history),
    }


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"
    return f"{int(seconds // 86400)}d {int((seconds % 86400) // 3600)}h"
