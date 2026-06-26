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

# Per-project mtime-based cache: cache_key -> (mtime, path, data)
_handoff_caches: dict[str, tuple[float, str, dict | None]] = {}


def set_project_dir(path: Path) -> None:
    """Set the default project directory (called once during app startup)."""
    global _default_project_dir
    _default_project_dir = path


def _resolve_project_dir() -> Path:
    """Resolve the active project directory."""
    ctx_dir = get_project_dir()
    if ctx_dir is not None:
        return ctx_dir
    return _default_project_dir or Path.cwd()


def _control_dir(project_dir: Path | None = None) -> Path | None:
    d = project_dir or _resolve_project_dir()
    if d is None:
        return None
    return d / ".claude" / "agent-control"


def _archive_dir(project_dir: Path | None = None) -> Path | None:
    d = project_dir or _resolve_project_dir()
    if d is None:
        return None
    return d / ".claude" / "agent-control" / "handoff-archive"


def _cache_key(project_dir: Path | None, issue: str | None) -> str:
    d = project_dir or _resolve_project_dir()
    return f"{d}:{issue or 'all'}"


def get_handoff(project_dir: Path | None = None, issue: str | None = None) -> dict | None:
    """Read a handoff file with mtime-based caching.

    With issue: reads handoff-{issue}.json specifically.
    Without: returns the most recently modified handoff file.
    """
    from sova.ipc.handoff import handoff_filename

    cdir = _control_dir(project_dir)
    if cdir is None:
        return None

    if issue:
        hf = cdir / handoff_filename(issue)
        return _read_cached(hf, project_dir, issue)

    candidates = list(cdir.glob("handoff-*.json")) if cdir.exists() else []
    legacy = cdir / "handoff.json"
    if legacy.exists():
        candidates.append(legacy)
    if not candidates:
        return None

    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    ck = _cache_key(project_dir, None)
    return _read_cached_path(newest, ck)


def get_all_handoffs(project_dir: Path | None = None) -> list[dict]:
    """Return all active handoff files as dicts."""
    cdir = _control_dir(project_dir)
    if cdir is None or not cdir.exists():
        return []

    candidates = list(cdir.glob("handoff-*.json"))
    legacy = cdir / "handoff.json"
    if legacy.exists():
        candidates.append(legacy)

    results = []
    for path in candidates:
        try:
            data = json.loads(path.read_text())
            results.append(data)
        except (json.JSONDecodeError, OSError):
            continue

    results.sort(key=lambda h: h.get("created_at", ""), reverse=True)
    return results


def _read_cached(hf: Path, project_dir: Path | None, issue: str | None) -> dict | None:
    ck = _cache_key(project_dir, issue)
    return _read_cached_path(hf, ck)


def _read_cached_path(hf: Path, ck: str) -> dict | None:
    if not hf.exists():
        _handoff_caches[ck] = (0.0, "", None)
        return None
    try:
        mtime = hf.stat().st_mtime
        hf_str = str(hf)
        cached = _handoff_caches.get(ck)
        if cached and mtime == cached[0] and hf_str == cached[1]:
            return cached[2]
        data = json.loads(hf.read_text())
        _handoff_caches[ck] = (mtime, hf_str, data)
        return data
    except (json.JSONDecodeError, OSError):
        return None


def archive_handoff(project_dir: Path | None = None, issue: str | None = None) -> dict | None:
    """Move handoff file(s) to the archive directory. Returns the last archived data."""
    from sova.ipc.handoff import handoff_filename

    cdir = _control_dir(project_dir)
    if cdir is None:
        return None

    if issue:
        fname = handoff_filename(issue)
        files = []
        if fname != "handoff.json":
            files.append(cdir / fname)
        legacy = cdir / "handoff.json"
        if legacy.exists():
            try:
                ldata = json.loads(legacy.read_text())
                if str(ldata.get("issue") or "").lstrip("#").strip() == issue.lstrip("#").strip():
                    files.append(legacy)
            except (json.JSONDecodeError, OSError):
                pass
    else:
        files = list(cdir.glob("handoff-*.json")) if cdir.exists() else []
        legacy = cdir / "handoff.json"
        if legacy.exists():
            files.append(legacy)

    archive = _archive_dir(project_dir)
    last_data = None

    for hf in files:
        if not hf.exists():
            continue
        try:
            data = json.loads(hf.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        last_data = data
        if archive is not None:
            archive.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            source = data.get("source", "unknown")
            h_issue = data.get("issue") or ""
            suffix = f"_{h_issue}" if h_issue else ""
            stem = hf.stem
            archive_name = f"{ts}_{source}{suffix}_{stem}.json"
            shutil.copy2(hf, archive / archive_name)

    if archive is not None and archive.exists():
        archives = sorted(archive.glob("*.json"), key=lambda p: p.name)
        for old in archives[:-50]:
            old.unlink(missing_ok=True)

    return last_data


def clear_handoff(project_dir: Path | None = None, issue: str | None = None) -> bool:
    """Archive and remove handoff file(s).

    With issue: clears only that issue's handoff.
    Without: clears all handoff files.
    """
    from sova.ipc.handoff import handoff_filename

    archive_handoff(project_dir, issue=issue)

    cdir = _control_dir(project_dir)
    if cdir is None:
        return False

    cleared = False
    if issue:
        fname = handoff_filename(issue)
        if fname != "handoff.json":
            hf = cdir / fname
            if hf.exists():
                hf.unlink()
                cleared = True
        legacy = cdir / "handoff.json"
        if legacy.exists():
            try:
                data = json.loads(legacy.read_text())
                if str(data.get("issue") or "").lstrip("#").strip() == issue.lstrip("#").strip():
                    legacy.unlink()
                    cleared = True
            except (json.JSONDecodeError, OSError):
                pass
        ck = _cache_key(project_dir, issue)
        _handoff_caches[ck] = (0.0, "", None)
    else:
        for hf in list(cdir.glob("handoff-*.json")) + [cdir / "handoff.json"]:
            if hf.exists():
                hf.unlink()
                cleared = True
        prefix = str(project_dir or _resolve_project_dir()) + ":"
        keys_to_clear = [k for k in _handoff_caches if k.startswith(prefix)]
        for k in keys_to_clear:
            _handoff_caches[k] = (0.0, "", None)

    _handoff_caches[_cache_key(project_dir, None)] = (0.0, "", None)
    return cleared


def build_action_command(action: dict) -> dict:
    """Map a handoff action to execution parameters.

    Returns a dict with fields needed by control_service to start
    the appropriate agent or command.
    """
    mode = action.get("mode", "")
    command = action.get("command", "")
    args = action.get("args", {})

    if not mode:
        cmd_name = command or action.get("action", "") or action.get("id", "")
        if cmd_name:
            mode = "claude-command"
            command = command or cmd_name

    if mode == "agent":
        ticket = args.get("issue") or args.get("ticket") or str(args.get("pr", ""))
        return {"type": "agent", "issue": ticket, "role": args.get("role"), "pr_number": args.get("pr")}

    if mode == "claude-command":
        return {"type": "claude-command", "command": command, "args": args}

    if mode == "shell":
        return {"type": "shell", "command": command}

    return {"type": "unknown", "error": f"Unknown mode: {mode}"}
