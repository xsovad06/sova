"""Handoff file management for the dashboard.

Reads, archives, and clears the file-based handoff that agents write
to `.claude/agent-control/handoff.json`. The dashboard polls this file
to render action panels.

Supports multi-project mode via project context or set_project_dir().
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from sova.config.context import resolve_project_dir
from sova.ipc.handoff import is_schema_valid, prune_unreadable_warnings, read_handoff_json
from sova.utils.files import remove_stale_temp_files

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
    return resolve_project_dir(default_project_dir=_default_project_dir)


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


def get_all_handoffs(
    project_dir: Path | None = None,
    *,
    unreadable_out: list[str] | None = None,
    schema_invalid_out: list[str] | None = None,
) -> list[dict]:
    """Return all active handoff files as dicts.

    When *unreadable_out* is given, the path of every candidate that failed
    to parse is appended to it, so a caller can surface those separately
    (e.g. as a warning on the agents page) without a second file-read pass.
    A candidate that vanished after the listing (archived or cleared mid-poll) is skipped,
    not reported: it was not corrupt, it was handled.

    The dashboard renders any JSON object, legacy shapes included, so such a file is still
    returned. *schema_invalid_out* collects the paths of the returned files that fail
    ``DashboardHandoff`` validation: the supervisor and the auto-handoff read through the
    pipeline reader, which skips them, so the page must not present them as healthy.
    """
    cdir = _control_dir(project_dir)
    if cdir is None:
        return []
    if not cdir.exists():
        prune_unreadable_warnings(cdir, [])
        return []

    candidates = list(cdir.glob("handoff-*.json"))
    legacy = cdir / "handoff.json"
    if legacy.exists():
        candidates.append(legacy)
    prune_unreadable_warnings(cdir, candidates)

    results = []
    for path in candidates:
        data = read_handoff_json(path)
        if data is None:
            if unreadable_out is not None and path.exists():
                unreadable_out.append(str(path))
            continue
        if schema_invalid_out is not None and not is_schema_valid(data):
            schema_invalid_out.append(str(path))
        results.append(data)

    # The dashboard checks only that a file is a JSON object, so created_at may be any JSON
    # value; a str() key keeps one odd file from turning every poll into a TypeError.
    results.sort(key=lambda h: str(h.get("created_at") or ""), reverse=True)
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
    except OSError:
        return None
    hf_str = str(hf)
    cached = _handoff_caches.get(ck)
    if cached and mtime == cached[0] and hf_str == cached[1]:
        return cached[2]
    data = read_handoff_json(hf)
    # Only a successful parse is cached: a read that caught the file mid-write
    # (truncated, same mtime on a coarse-timestamp filesystem) must be retried.
    if data is not None:
        _handoff_caches[ck] = (mtime, hf_str, data)
    return data


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
            ldata = read_handoff_json(legacy)
            if ldata is not None and str(ldata.get("issue") or "").lstrip("#").strip() == issue.lstrip("#").strip():
                files.append(legacy)
    else:
        files = list(cdir.glob("handoff-*.json")) if cdir.exists() else []
        legacy = cdir / "handoff.json"
        if legacy.exists():
            files.append(legacy)

    # Temp files from a handoff write that died before its os.replace() (see write_text_atomic).
    remove_stale_temp_files(cdir, ".handoff*.tmp")

    archive = _archive_dir(project_dir)
    last_data = None

    for hf in files:
        if not hf.exists():
            continue
        data = read_handoff_json(hf)
        if data is not None:
            last_data = data
        # An unreadable file (corrupt or non-UTF-8) is archived byte-for-byte too: clear_handoff()
        # deletes every file this returns after, and that must leave a copy to repair it from.
        meta = data if data is not None else {"source": "unreadable"}
        if archive is not None:
            archive.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            source = meta.get("source", "unknown")
            h_issue = meta.get("issue") or ""
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
            data = read_handoff_json(legacy)
            if data is not None and str(data.get("issue") or "").lstrip("#").strip() == issue.lstrip("#").strip():
                legacy.unlink()
                cleared = True
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
