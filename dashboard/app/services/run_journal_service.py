"""Run journal -- persistent audit trail for agent and command runs.

Creates a JSON record the instant a process starts, updates it during execution,
and finalizes it on completion or crash detection. Records survive dashboard
restarts and agent crashes, providing a complete history of all runs.

Storage: .claude/agent-control/runs/<run_id>.json
Active:  .claude/agent-control/runs/current.json (copy of active run record)
"""

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from app import config

MAX_ARCHIVED_RUNS = 50


def _runs_dir() -> Path:
    return config.DATA_DIR / "agent-control" / "runs"


def _current_link() -> Path:
    return _runs_dir() / "current.json"


def _generate_run_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{ts}-{secrets.token_hex(3)}"


def _run_file(run_id: str) -> Path:
    return _runs_dir() / f"{run_id}.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pid_is_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _write_record(record: dict) -> None:
    """Write a run record to disk."""
    runs = _runs_dir()
    runs.mkdir(parents=True, exist_ok=True)
    path = _run_file(record["run_id"])
    path.write_text(json.dumps(record, indent=2))


def _set_current(record: dict) -> None:
    """Write current.json as a copy of the active run record."""
    runs = _runs_dir()
    runs.mkdir(parents=True, exist_ok=True)
    _current_link().write_text(json.dumps(record, indent=2))


def _clear_current() -> None:
    """Remove the current.json pointer."""
    link = _current_link()
    if link.exists():
        link.unlink()


def _prune_old_runs() -> None:
    """Keep only the most recent MAX_ARCHIVED_RUNS run files."""
    runs = _runs_dir()
    if not runs.exists():
        return
    files = sorted(
        (f for f in runs.glob("*.json") if f.name != "current.json"),
        key=lambda p: p.name,
    )
    for old in files[:-MAX_ARCHIVED_RUNS]:
        old.unlink(missing_ok=True)


# -- Public API --


def start_run(
    pid: int,
    mode: str,
    command: str,
    ticket: str | None = None,
    started_by: str = "dashboard",
) -> dict:
    """Create a new run record. Called immediately after process spawn."""
    run_id = _generate_run_id()
    record = {
        "run_id": run_id,
        "started_at": _now_iso(),
        "started_by": started_by,
        "pid": pid,
        "mode": mode,
        "command": command,
        "ticket": ticket,
        "status": "running",
        "updated_at": _now_iso(),
        "current_step": None,
        "ended_at": None,
        "exit_code": None,
        "outcome": None,
        "error": None,
        "output_tail": [],
    }
    _write_record(record)
    _set_current(record)
    _prune_old_runs()
    return record


def finalize_run(run_id: str, exit_code: int | None, output_tail: list[str] | None = None) -> dict | None:
    """Mark a run as completed or crashed. Called when process exits.

    Idempotent: returns early if the run is already finalized.
    """
    record = read_run(run_id)
    if not record or record["status"] != "running":
        return record

    record["status"] = "completed" if exit_code == 0 else "crashed"
    record["exit_code"] = exit_code
    record["ended_at"] = _now_iso()
    record["updated_at"] = _now_iso()
    if output_tail:
        record["output_tail"] = output_tail[-20:]

    _write_record(record)
    _clear_current()
    return record


def read_run(run_id: str) -> dict | None:
    """Read a specific run record."""
    path = _run_file(run_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def get_current_run() -> dict | None:
    """Read the current (active) run record."""
    link = _current_link()
    if not link.exists():
        return None
    try:
        return json.loads(link.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def recover_orphaned_runs() -> dict | None:
    """Check for orphaned runs on startup and mark them as abandoned.

    Called once when the dashboard starts. If current.json points to a run
    whose PID is no longer alive, the run is marked as abandoned.

    Returns the abandoned record if one was found, else None.
    """
    current = get_current_run()
    if current is None:
        return None

    pid = current.get("pid")
    if pid and _pid_is_alive(pid):
        # Process is still alive -- leave it alone
        return None

    run_id = current["run_id"]
    record = read_run(run_id)
    if not record:
        _clear_current()
        return None

    record["status"] = "abandoned"
    record["ended_at"] = _now_iso()
    record["updated_at"] = _now_iso()
    record["error"] = "Dashboard restarted; agent process not found"

    _write_record(record)
    _clear_current()
    return record


def list_runs(limit: int = 20) -> list[dict]:
    """List recent run records, newest first."""
    runs = _runs_dir()
    if not runs.exists():
        return []

    files = sorted(
        (f for f in runs.glob("*.json") if f.name != "current.json"),
        key=lambda p: p.name,
        reverse=True,
    )

    result = []
    for path in files[:limit]:
        try:
            result.append(json.loads(path.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    return result
