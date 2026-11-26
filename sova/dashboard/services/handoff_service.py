"""Handoff file management for the dashboard.

Reads, archives, and clears the file-based handoff that agents write
to `.claude/agent-control/handoff.json`. The dashboard polls this file
to render action panels.

Supports multi-project mode via project context or set_project_dir().
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from sova.config.context import get_project_dir, resolve_project_dir
from sova.dashboard.services.agent_recovery import get_synthesized_handoff, invalidate_synthesis_cache
from sova.dashboard.services.snapshot_service import get_snapshot, invalidate_snapshot, refresh_snapshot
from sova.ipc.handoff import is_schema_valid, prune_unreadable_warnings, read_handoff_json
from sova.utils.files import remove_stale_temp_files
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


def _normalize_issue(issue: object) -> str:
    return str(issue or "").lstrip("#").strip()


def _legacy_matches(legacy: Path, issue: str) -> bool:
    """Whether the legacy shared ``handoff.json`` holds the handoff of *issue* (an empty one included)."""
    data = read_handoff_json(legacy)
    return data is not None and _normalize_issue(data.get("issue")) == issue


def _issue_files(cdir: Path, issue: str) -> list[Path]:
    """The files that hold *issue*'s handoff: its own file and a legacy file naming it."""
    from sova.ipc.handoff import handoff_filename

    files = []
    fname = handoff_filename(issue)
    if fname != "handoff.json":
        files.append(cdir / fname)
    legacy = cdir / "handoff.json"
    if legacy.exists() and _legacy_matches(legacy, issue):
        files.append(legacy)
    return files


def _all_files(cdir: Path) -> list[Path]:
    files = list(cdir.glob("handoff-*.json")) if cdir.exists() else []
    legacy = cdir / "handoff.json"
    if legacy.exists():
        files.append(legacy)
    return files


def _copy_to_archive(hf: Path, meta: dict, archive: Path) -> None:
    """Copy one handoff file into *archive* under a name built from its time, source, issue and file stem."""
    archive.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    source = meta.get("source", "unknown")
    h_issue = meta.get("issue") or ""
    suffix = f"_{h_issue}" if h_issue else ""
    shutil.copy2(hf, archive / f"{ts}_{source}{suffix}_{hf.stem}.json")


def _prune_archive(archive: Path, keep: int = 50) -> None:
    """Delete all but the newest *keep* archived handoffs (names start with their timestamp)."""
    if not archive.exists():
        return
    archives = sorted(archive.glob("*.json"), key=lambda p: p.name)
    for old in archives[:-keep]:
        old.unlink(missing_ok=True)


def _archive_files(cdir: Path, files: list[Path], project_dir: Path | None) -> dict | None:
    """Copy *files* into the archive, keeping its newest 50 entries. Returns the last archived data."""
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
        # An unreadable file (corrupt or non-UTF-8) is archived byte-for-byte too: the clear functions
        # delete every file they pass here, and that must leave a copy to repair it from.
        if archive is not None:
            _copy_to_archive(hf, data if data is not None else {"source": "unreadable"}, archive)

    if archive is not None:
        _prune_archive(archive)

    return last_data


def archive_handoff(project_dir: Path | None = None, issue: str | None = None) -> dict | None:
    """Copy one issue's handoff file(s) to the archive directory. Returns the last archived data.

    Like ``clear_handoff()``, an empty or ``None`` issue (or one that is only ``#`` and whitespace) archives
    nothing and returns ``None``; ``archive_all_handoffs()`` is the one way to archive every file.
    """
    key = _normalize_issue(issue)
    if not key:
        return None
    cdir = _control_dir(project_dir)
    if cdir is None:
        return None
    return _archive_files(cdir, _issue_files(cdir, key), project_dir)


def archive_all_handoffs(project_dir: Path | None = None) -> dict | None:
    """Copy every handoff file of the project to the archive directory. Returns the last archived data."""
    cdir = _control_dir(project_dir)
    if cdir is None:
        return None
    return _archive_files(cdir, _all_files(cdir), project_dir)


def _remove_files(files: list[Path]) -> bool:
    cleared = False
    for hf in files:
        if hf.exists():
            hf.unlink()
            cleared = True
    return cleared


def _same_version(data: dict | None, expected: dict) -> bool:
    """Whether the on-disk *data* is the handoff version *expected* was read from.

    Every key the file holds must carry the same value in *expected*. Keys only *expected* has are ignored: a
    reader may have annotated its copy (``age_seconds`` and the like) after the read, and that must not make the
    version look replaced. A replacement written since the read differs in a key of its own (``created_at`` at
    least), so it still fails the match.
    """
    return data is not None and all(k in expected and expected[k] == v for k, v in data.items())


def _holding(files: list[Path], expected: dict | None) -> list[Path]:
    """The *files* that still hold the *expected* handoff, or all of them when no version is expected.

    A caller that read a handoff, then awaited a start, passes what it read: a file rewritten in the meantime
    holds a newer handoff, which stays pending instead of being archived and removed unrun.
    """
    if expected is None:
        return files
    held = [hf for hf in files if _same_version(read_handoff_json(hf), expected)]
    if files and not held:
        log.info("handoff.clear_skipped_replaced", files=[hf.name for hf in files], issue=expected.get("issue"))
    return held


def clear_handoff(project_dir: Path | None = None, issue: str | None = None, *, expected: dict | None = None) -> bool:
    """Archive and remove the handoff of one issue; returns whether a file was removed.

    An empty or ``None`` issue (or one that is only ``#`` and whitespace) clears nothing: a caller whose
    issue did not resolve must not wipe every other issue's pending handoff. ``clear_all_handoffs()`` is the
    one way to clear everything. With *expected*, only a file whose contents still equal it is cleared.
    """
    key = _normalize_issue(issue)
    if not key:
        log.debug("handoff.clear_skipped_empty_issue", issue=issue)
        return False

    cdir = _control_dir(project_dir)
    if cdir is None:
        return False

    files = _holding(_issue_files(cdir, key), expected)
    _archive_files(cdir, files, project_dir)
    cleared = _remove_files(files)
    for ck in {_cache_key(project_dir, key), _cache_key(project_dir, issue), _cache_key(project_dir, None)}:
        _handoff_caches[ck] = (0.0, "", None)
    return cleared


def clear_issueless_handoff(project_dir: Path | None = None, *, expected: dict | None = None) -> bool:
    """Archive and remove the handoff that names no issue (a PR-only one), leaving every issue's handoff in place.

    Such a handoff can only live in the legacy shared ``handoff.json`` (``handoff_filename(None)``), so that file
    is removed, and only while it still names no issue (and, with *expected*, still holds that handoff).
    """
    cdir = _control_dir(project_dir)
    if cdir is None:
        return False

    legacy = cdir / "handoff.json"
    files = _holding([legacy] if legacy.exists() and _legacy_matches(legacy, "") else [], expected)
    _archive_files(cdir, files, project_dir)
    cleared = _remove_files(files)
    _handoff_caches[_cache_key(project_dir, None)] = (0.0, "", None)
    return cleared


def clear_all_handoffs(project_dir: Path | None = None) -> bool:
    """Archive and remove every handoff file of the project. Only the explicit ``POST /handoff/clear`` calls this."""
    cdir = _control_dir(project_dir)
    if cdir is None:
        return False

    files = _all_files(cdir)
    _archive_files(cdir, files, project_dir)
    cleared = _remove_files(files)
    prefix = str(project_dir or _resolve_project_dir()) + ":"
    for k in [k for k in _handoff_caches if k.startswith(prefix)]:
        _handoff_caches[k] = (0.0, "", None)
    _handoff_caches[_cache_key(project_dir, None)] = (0.0, "", None)
    return cleared


def command_name(raw: str | None) -> str:
    """The catalog name in a handoff action's command: ``"/integrate-pr 42"`` becomes ``"integrate-pr"``.

    A handoff stores the slash form a person would type; start_command() takes only the bare
    name (the PR and issue travel in ``args``) and refuses anything outside the catalog. A missing
    command (None, empty, or not a string) yields "", which every caller treats as nothing to run.
    """
    if not isinstance(raw, str):
        return ""
    parts = raw.strip().lstrip("/").split()
    return parts[0] if parts else ""


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
        # An explicitly empty issue (a reviewer handoff on a PR with no linked
        # issue) stays empty, as on the auto-execute path: start_agent() then
        # works from pr_number instead of taking the PR number as an issue.
        if "issue" in args or "ticket" in args:
            ticket = str(args.get("issue") or args.get("ticket") or "")
        else:
            ticket = str(args.get("pr", ""))
        return {"type": "agent", "issue": ticket, "role": args.get("role"), "pr_number": args.get("pr")}

    if mode == "claude-command":
        return {"type": "claude-command", "command": command_name(command), "args": args}

    if mode == "shell":
        return {"type": "shell", "command": command}

    return {"type": "unknown", "error": f"Unknown mode: {mode}"}


class ActionOutcome(StrEnum):
    """How ``execute_action()`` ended; the route maps each one to an HTTP status."""

    STARTED = "started"
    NOT_FOUND = "not_found"
    REFUSED = "refused"
    INVALID_INPUT = "invalid_input"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


@dataclass(frozen=True)
class ActionResult:
    """Result of ``execute_action()``: ``payload`` is the start's response, ``detail`` the reason it did not start."""

    outcome: ActionOutcome
    payload: dict = field(default_factory=dict)
    detail: str = ""


def _matches_action(action: dict, action_id: str) -> bool:
    return action_id in {action.get("id"), action.get("action"), action.get("command"), action.get("label")}


def _find_action(handoff: dict, action_id: str) -> dict | None:
    return next((a for a in handoff.get("next_actions", []) if _matches_action(a, action_id)), None)


def start_succeeded(result: object) -> bool:
    """Whether a ``start_agent()``/``start_command()`` result is a started run rather than an error dict."""
    return not (isinstance(result, dict) and ("error" in result or result.get("status") == "error"))


def _refusal_codes() -> frozenset[str]:
    """Codes of a start refused by a conflict with current state, which a later retry of the same action can pass."""
    from sova.dashboard.services.agent_lifecycle import NO_FREE_SLOT
    from sova.dashboard.services.agent_validation import ISSUE_CONFLICT, MEMORY_PRESSURE, STATE_NOT_READY

    return frozenset(
        {
            NO_FREE_SLOT,
            ISSUE_CONFLICT,
            MEMORY_PRESSURE,
            STATE_NOT_READY,
            "network_unreachable",
            "budget_exceeded",
            "suitability_blocked",
            "stale_command",
        }
    )


def _failure_outcome(result: dict) -> ActionResult:
    """The outcome of an unsuccessful start: INVALID_INPUT for a command or argument start_command() rejects, REFUSED
    for a known conflict code, FAILED for anything else.

    REFUSED (HTTP 409) tells the client to retry later, so it is opt-in: an uncoded or unknown error (a broken run
    record or spawn, a worktree that could not be resolved, an unsupported model) would fail again the same way.
    INVALID_INPUT (HTTP 400) is the same refusal ``POST /agents/command`` answers with 400 for the same input.
    """
    from sova.dashboard.services.agent_lifecycle import COMMAND_INPUT_ERRORS

    code = result.get("code")
    if code in COMMAND_INPUT_ERRORS:
        outcome = ActionOutcome.INVALID_INPUT
    else:
        outcome = ActionOutcome.REFUSED if code in _refusal_codes() else ActionOutcome.FAILED
    return ActionResult(outcome, detail=result.get("detail") or result.get("error") or "Action failed")


async def find_awaiting_approval_run(issue: str) -> int | None:
    """Find the most recent awaiting_approval TaskRun for an issue."""
    from sqlalchemy import select

    from sova.core.state import TaskStatus
    from sova.db.models import TaskRun
    from sova.db.session import get_session

    async with await get_session() as session, session.begin():
        stmt = (
            select(TaskRun.id)
            .where(
                TaskRun.issue_number == issue,
                TaskRun.status == TaskStatus.AWAITING_APPROVAL,
            )
            .order_by(TaskRun.started_at.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def _execute_spec_action(action_id: str, issue: str) -> ActionResult | None:
    """Approve or reject the spec of an awaiting_approval run of *issue*; ``None`` when there is no such run."""
    from sova.dashboard.services import control_service

    run_id = await find_awaiting_approval_run(issue)
    if run_id is None:
        return None
    if action_id == "approve-spec":
        result = await control_service.resume_from_approval(run_id)
    else:
        result = await control_service.reject_spec(run_id)
    if result.get("error") == "not_found":
        return ActionResult(ActionOutcome.NOT_FOUND, detail=result["detail"])
    if result.get("error") == "conflict":
        return ActionResult(ActionOutcome.REFUSED, detail=result["detail"])
    if "error" in result:
        return ActionResult(ActionOutcome.FAILED, detail=result.get("detail", result["error"]))
    result["action"] = "Approve Spec" if action_id == "approve-spec" else "Reject"
    return ActionResult(ActionOutcome.STARTED, payload=result)


def _clear_executed_handoff(handoff: dict) -> None:
    """Clear the handoff an action was started from, after the start succeeded.

    A file-backed handoff is cleared by its own file: its issue's, or the issue-less ``handoff.json`` of a
    PR-only handoff, so no other issue's pending handoff is touched, and only while the file still holds it. A
    synthesized one has no file, and only its synthesis cache is dropped. A failed clear is logged, not raised:
    the run is already underway.
    """
    issue = _normalize_issue(handoff.get("issue"))
    try:
        if handoff.get("source") == "pr-review-state":
            pr = handoff.get("pr_number")
            if issue and pr is not None:
                invalidate_synthesis_cache(issue, pr)
        elif issue:
            clear_handoff(issue=issue, expected=handoff)
        else:
            clear_issueless_handoff(expected=handoff)
    except Exception:  # noqa: BLE001 (best effort; the action already started so a failed clear must not fail it)
        log.warning("handoff.clear_after_start_failed", issue=issue, exc_info=True)
    invalidate_handoff_snapshot()


def _parse_ref(issue: str | None) -> tuple[str, str] | None:
    """Split an ``execute_action()`` ref into ``(issue, pr)``; ``None`` for a ``pr:`` ref with a malformed number."""
    norm_issue = _normalize_issue(issue)
    if not norm_issue.startswith("pr:"):
        return norm_issue, ""
    norm_pr = norm_issue.removeprefix("pr:")
    # An empty or malformed number would filter nothing and run the first handoff with this action.
    if not (norm_pr.isascii() and norm_pr.isdigit()):
        return None
    return "", norm_pr


def _ref_selects(h: dict, norm_issue: str, norm_pr: str) -> bool:
    h_issue = _normalize_issue(h.get("issue"))
    h_pr = str(h.get("pr_number") or "")
    return (not norm_issue or h_issue == norm_issue) and (not norm_pr or h_pr == norm_pr)


async def _select_action(action_id: str, norm_issue: str, norm_pr: str) -> tuple[dict, dict] | None:
    """The first ``(handoff, action)`` the ref selects: file-backed handoffs first, then the synthesized one."""
    for h in get_all_handoffs():
        if _ref_selects(h, norm_issue, norm_pr) and (match := _find_action(h, action_id)):
            return h, match
    synthesized = await get_synthesized_handoff()
    if synthesized and _ref_selects(synthesized, norm_issue, norm_pr):
        if match := _find_action(synthesized, action_id):
            return synthesized, match
    return None


async def _start_action(action: dict) -> object:
    """Start *action* without clearing its handoff; an ``ActionResult`` for a mode the dashboard cannot run."""
    from sova.dashboard.services import control_service

    # The start functions would clear the handoff before their spawn, which can still fail: the caller clears it after.
    exec_params = build_action_command(action)
    if exec_params["type"] == "agent":
        return await control_service.start_agent(
            exec_params.get("issue") or "",
            role=exec_params.get("role"),
            pr_number=exec_params.get("pr_number"),
            _skip_handoff_clear=True,
        )
    if exec_params["type"] == "claude-command":
        return await control_service.start_command(
            exec_params["command"], exec_params.get("args", {}), _skip_handoff_clear=True
        )
    if exec_params["type"] == "shell":
        return ActionResult(ActionOutcome.UNSUPPORTED, detail="Shell mode not yet supported in SOVA dashboard")
    return ActionResult(ActionOutcome.UNSUPPORTED, detail=f"Unknown execution type: {exec_params['type']}")


# Handoffs whose action is being started right now, keyed by project and handoff (see _in_flight_key()).
_in_flight: set[tuple[str, str]] = set()


def _in_flight_key(handoff: dict) -> tuple[str, str]:
    """The project and identity of *handoff*: its issue, or ``pr:N`` for a PR-only one."""
    issue = _normalize_issue(handoff.get("issue"))
    return str(_resolve_project_dir()), issue or f"pr:{handoff.get('pr_number') or ''}"


async def execute_action(action_id: str, issue: str | None = None) -> ActionResult:
    """Execute one action of a pending handoff and clear that handoff only once the start succeeded.

    ``issue`` names the handoff's issue, or ``pr:N`` for the handoff that carries PR N. The two never mix: an
    issue ref matches by issue only, and ``pr:N`` by PR only, so it also finds an issue-backed handoff for that PR
    (the PR is the same work). A ``pr:`` ref whose number is not a plain integer matches nothing.

    File-backed handoffs are searched first, then the synthesized (PR state) one, then the spec actions of an
    awaiting_approval run. A refused or failed start leaves the handoff pending, so the same action can be retried:
    REFUSED is a conflict with current state (no free slot, a start of the same handoff already in flight),
    INVALID_INPUT a command name or argument outside what start_command() accepts, FAILED anything else (a broken
    run record or spawn, an uncoded error).
    """
    not_found = ActionResult(ActionOutcome.NOT_FOUND, detail=f"Action '{action_id}' not found in any handoff")
    ref = _parse_ref(issue)
    if ref is None:
        return not_found
    norm_issue, norm_pr = ref

    selected = await _select_action(action_id, norm_issue, norm_pr)
    if selected is None:
        if norm_issue and action_id in {"approve-spec", "reject-spec"}:
            spec_result = await _execute_spec_action(action_id, norm_issue)
            if spec_result is not None:
                return spec_result
        return not_found
    handoff, action = selected

    # The handoff stays listed until the start succeeded, so a second request (another tab, the phone page, the
    # MCP tool) would find it during the start and run it again. Claiming it here, with no await since the
    # selection, makes the second request a refusal instead.
    key = _in_flight_key(handoff)
    if key in _in_flight:
        log.info("handoff.action_in_flight", action_id=action_id, issue=handoff.get("issue"))
        return ActionResult(ActionOutcome.REFUSED, detail="An action of this handoff is already being started")
    _in_flight.add(key)
    try:
        result = await _start_action(action)
        if isinstance(result, ActionResult):
            return result
        if not start_succeeded(result):
            log.info("handoff.action_refused", action_id=action_id, issue=handoff.get("issue"))
            return _failure_outcome(result)

        _clear_executed_handoff(handoff)
    finally:
        _in_flight.discard(key)
    result["action"] = action.get("label", action_id)
    return ActionResult(ActionOutcome.STARTED, payload=result)


async def read_handoff(issue: str | None) -> dict:
    """Read active handoff files, optionally filtered by issue, as ``GET /handoff`` returns them.

    ``unreadable_handoffs`` lists the files that could not be parsed. ``schema_invalid_handoffs``
    lists the files that parse as JSON objects, and are still returned in ``handoffs`` because the
    dashboard renders legacy shapes, but fail ``DashboardHandoff`` validation: the supervisor and the
    auto-handoff skip them, so they must not look healthy. Without ``issue`` both cover the whole
    project; with ``issue`` they are narrowed to that issue's own file and the legacy shared
    ``handoff.json``, so another issue's corrupt file is not reported as this one's.
    """
    unreadable: list[str] = []
    schema_invalid: list[str] = []
    all_handoffs = get_all_handoffs(unreadable_out=unreadable, schema_invalid_out=schema_invalid)

    if issue:
        # Built inline rather than via handoff_filename(): this route is polled every few seconds
        # and that helper warns on an unsafe issue string, which here is client-controlled input.
        own_files = {f"handoff-{issue.lstrip('#').strip()}.json", "handoff.json"}
        unreadable = [path for path in unreadable if Path(path).name in own_files]
        schema_invalid = [path for path in schema_invalid if Path(path).name in own_files]
        filtered = [h for h in all_handoffs if str(h.get("issue", "")) == issue]
    else:
        filtered = all_handoffs

    problems = {"unreadable_handoffs": unreadable, "schema_invalid_handoffs": schema_invalid}

    if filtered:
        return {"has_handoff": True, "handoff": filtered[0], "handoffs": filtered, **problems}
    if issue:
        return {"has_handoff": False, "handoffs": [], **problems}

    synthesized = await get_synthesized_handoff()
    if synthesized is not None:
        return {"has_handoff": True, "handoff": synthesized, "handoffs": [synthesized], **problems}
    return {"has_handoff": False, "handoffs": [], **problems}


def _snapshot_project_dir() -> Path | None:
    return get_project_dir()


async def _build_project_handoff() -> dict:
    return await read_handoff(None)


async def get_handoff_snapshot() -> dict:
    """Return the project-wide handoff from the last build; a stale one is rebuilt in the background.

    The synthesized handoff can wait on GitHub, so the project-wide read never builds on the request path.
    """
    return (await get_snapshot("handoff", _snapshot_project_dir(), _build_project_handoff)).payload


async def build_handoff_snapshot() -> dict:
    """Rebuild the project-wide handoff snapshot now and return it; raises when the rebuild failed.

    The status socket's producer calls this, so the snapshot ``GET /handoff`` answers from stays warm. A failed
    rebuild raises rather than returning the previous payload: the producer then reports ``handoff_unavailable``
    instead of passing old data off as current.
    """
    project_dir = _snapshot_project_dir()
    await refresh_snapshot("handoff", project_dir, _build_project_handoff)
    snapshot = await get_snapshot("handoff", project_dir, _build_project_handoff)
    if snapshot.failed_at is not None:
        raise RuntimeError(f"handoff rebuild failed: {snapshot.last_error}")
    return snapshot.payload


def invalidate_handoff_snapshot() -> None:
    """Drop the project-wide snapshot after a handoff write, so the next read is built fresh."""
    invalidate_snapshot("handoff", _snapshot_project_dir())
