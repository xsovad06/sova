"""Handoff protocol for inter-agent context passing.

Agents are ephemeral: they spawn, work, write a handoff, and die.
The handoff carries enough context for a fresh agent to pick up
without re-reading the entire codebase.

Two handoff types:
- AgentHandoff: DB-backed, used by orchestrator/scheduler for history.
- DashboardHandoff: file-based, used by dashboard for actionable UI panels.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError

from sova.db.models import TaskRun
from sova.db.session import get_session
from sova.utils.dedup_log import (
    FileVersion,
    file_version,
    forget,
    forget_unless_unchanged,
    prune_directory,
    warn_once_per_version,
)
from sova.utils.files import write_text_atomic
from sova.utils.logging import get_logger

log = get_logger(component="ipc.handoff")


# ---------------------------------------------------------------------------
# Action model (used by dashboard handoff panels)
# ---------------------------------------------------------------------------


class HandoffAction(BaseModel):
    """A single action button rendered in the dashboard handoff panel."""

    id: str
    label: str
    description: str = ""
    style: Literal["approve", "neutral", "danger"] = "neutral"
    mode: Literal["agent", "claude-command", "shell", "dashboard-only"] = "claude-command"
    command: str = ""
    args: dict = Field(default_factory=dict)
    auto_execute: bool = False


# ---------------------------------------------------------------------------
# Dashboard handoff (file-based, for actionable UI)
# ---------------------------------------------------------------------------


class DashboardHandoff(BaseModel):
    """Handoff written to disk for dashboard action panels.

    This is the file-based handoff that the dashboard polls and renders
    as action buttons. Separate from the DB-backed AgentHandoff which
    stores history for the orchestrator.
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    source: str
    status: Literal["awaiting_action", "completed", "failed"]
    issue: str = ""
    pr_number: int | None = None
    branch: str = ""
    summary: str
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    details: dict = Field(default_factory=dict)
    next_actions: list[HandoffAction] = Field(default_factory=list)


_SAFE_ISSUE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def handoff_filename(issue: str | None) -> str:
    """Return the handoff filename for an issue, or the legacy name."""
    if issue:
        safe = issue.lstrip("#").strip()
        if safe and _SAFE_ISSUE_RE.fullmatch(safe):
            return f"handoff-{safe}.json"
        if safe:
            log.warning("handoff_file.invalid_issue", issue=issue)
    return "handoff.json"


def _control_dir(project_dir: Path) -> Path:
    return project_dir / ".claude" / "agent-control"


def write_handoff_file(project_dir: Path, handoff: DashboardHandoff) -> Path:
    """Write a DashboardHandoff to a per-issue file in agent-control."""
    cdir = _control_dir(project_dir)
    cdir.mkdir(parents=True, exist_ok=True)
    filename = handoff_filename(handoff.issue)
    if filename == "handoff.json":
        log.warning("handoff_file.no_issue", source=handoff.source)
    path = cdir / filename
    # Atomic, not write_text(): the dashboard polls these files every few seconds, and a read
    # that caught a truncated file would report a healthy handoff as unreadable.
    write_text_atomic(path, json.dumps(handoff.model_dump(), indent=2))
    log.info("handoff_file.written", path=str(path), source=handoff.source, issue=handoff.issue)
    return path


def read_handoff_file(project_dir: Path, issue: str | None = None) -> DashboardHandoff | None:
    """Read a DashboardHandoff for a specific issue, or the most recent one.

    With issue: reads handoff-{issue}.json directly.
    Without issue: finds the most recently modified per-issue file,
    falling back to the legacy handoff.json.
    """
    cdir = _control_dir(project_dir)
    if not cdir.exists():
        return None

    if issue:
        path = cdir / handoff_filename(issue)
        if not path.exists():
            legacy = cdir / "handoff.json"
            if legacy.exists():
                parsed = _parse_handoff(legacy)
                if parsed and parsed.issue == issue:
                    return parsed
            return None
        return _parse_handoff(path)

    candidates = list(cdir.glob("handoff-*.json"))
    legacy = cdir / "handoff.json"
    if legacy.exists():
        candidates.append(legacy)
    if not candidates:
        return None

    # A candidate can be cleared between the glob and here (the auto-handoff clears a handoff before it
    # spawns the next agent), so a vanished file drops out instead of raising from stat().
    versions = [(version[0], p) for p in candidates if (version := file_version(p)) is not None]
    if not versions:
        return None
    newest = max(versions, key=lambda item: item[0])[1]
    return _parse_handoff(newest)


class ClosingSummaryError(ValueError):
    """The closing summary could not be recorded (a bad argument, or a file that belongs to another issue or PR)."""


# The details key naming the command that wrote the closing summary. A command's summary is the run's result, so
# the run history prefers it over per-run counters an earlier pipeline run left in the same merged file.
SUMMARY_SOURCE_KEY = "summary_source"
COMMAND_SOURCE_PREFIX = "command:"


def _closing_handoff_path(cdir: Path, issue: str, pr_number: int) -> Path:
    """The file a command's closing summary goes to: the issue's file, else one keyed by the PR.

    A PR with no issue (a Dependabot merge) gets its own ``handoff-pr-<n>.json``: the shared legacy file would hold
    whichever PR wrote it last, and every later no-issue PR would be refused for it.
    """
    return cdir / (handoff_filename(issue) if issue else f"handoff-pr-{pr_number}.json")


def record_closing_summary(
    project_dir: Path,
    *,
    source: str,
    issue: str,
    pr_number: int,
    summary: str,
    merged: bool | None = None,
) -> Path:
    """Merge a command's closing one-line ``summary`` into its handoff file, creating one when there is none.

    The run history shows ``details.summary`` as the run's result, and ``details.merged`` (when given) says whether
    an Integrate run merged. The merge keeps everything else the file holds (``next_actions``, ``status``, the
    reviewer's findings), so a next action already there survives. A per-issue file may start from the legacy
    ``handoff.json`` when that is this issue's; a PR with no issue starts from the legacy file only when it is
    this PR's. Raises ``ClosingSummaryError`` for a bad argument or a file that belongs to another issue or PR.
    """
    issue = issue.lstrip("#").strip()
    if issue and not _SAFE_ISSUE_RE.fullmatch(issue):
        raise ClosingSummaryError(f"Closing summary not recorded: {issue!r} is not an issue number or key")
    if pr_number <= 0:
        raise ClosingSummaryError(f"Closing summary not recorded: {pr_number} is not a PR number")
    summary = " ".join(summary.split())
    if not summary:
        raise ClosingSummaryError("Closing summary not recorded: the summary is empty")

    cdir = _control_dir(project_dir)
    path = _closing_handoff_path(cdir, issue, pr_number)
    data = _read_json_object(path)
    if data is None:
        # A handoff written before the keyed file existed: keep its next actions when it is this run's.
        legacy = _read_json_object(cdir / "handoff.json")
        if legacy is not None and str(legacy.get("issue") or "") == issue:
            if issue or legacy.get("pr_number") in (None, pr_number):
                data = legacy
    if data is None:
        data = {"source": source, "status": "completed", "issue": issue, "pr_number": pr_number}
    if str(data.get("issue") or "") != issue:
        raise ClosingSummaryError("Closing summary not recorded: the handoff file belongs to another issue")
    if data.get("pr_number") not in (None, pr_number) and not issue:
        raise ClosingSummaryError("Closing summary not recorded: the handoff file belongs to another PR")

    earlier = data.get("details")
    details = {**(earlier if isinstance(earlier, dict) else {}), "summary": summary, SUMMARY_SOURCE_KEY: source}
    if merged is not None:
        details["merged"] = merged
    data["summary"] = summary
    data["details"] = details
    cdir.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps(data, indent=2))
    log.info("handoff_file.closing_summary", path=str(path), source=source, issue=issue, pr_number=pr_number)
    return path


def read_all_handoff_files(project_dir: Path) -> list[DashboardHandoff]:
    """Read all active handoff files (per-issue + legacy)."""
    cdir = _control_dir(project_dir)
    if not cdir.exists():
        return []

    candidates = list(cdir.glob("handoff-*.json"))
    legacy = cdir / "handoff.json"
    if legacy.exists():
        candidates.append(legacy)

    prune_unreadable_warnings(cdir, candidates)

    handoffs = []
    for path in candidates:
        h = _parse_handoff(path)
        if h is not None:
            handoffs.append(h)

    handoffs.sort(key=lambda h: h.created_at, reverse=True)
    return handoffs


# resolved path -> (mtime, size) of the unreadable version already warned about
_warned_unreadable: dict[str, FileVersion] = {}


def _note_unreadable(path: Path, **fields: Any) -> None:
    """Log that *path* cannot be used as a handoff: a warning once per version, debug on repeats.

    Handoffs are read on recurring paths (the dashboard's poll, the supervisor, every
    agent exit), so one corrupt file would otherwise log a warning on every cycle. A
    new write is a new version; see ``sova.utils.dedup_log``.
    """
    warn_once_per_version(log, "handoff_file.unreadable", path, _warned_unreadable, **fields)


def _short_error(exc: Exception) -> str:
    """Describe *exc* in one short line without the file's content.

    A traceback is not logged for an unreadable handoff: a ``json.loads`` frame holds the whole file
    text and a ``ValidationError`` carries the offending input values, and handoff summaries can embed
    issue or PR text and command output. A validation error is reduced to its field locations and messages.
    """
    if isinstance(exc, ValidationError):
        detail = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
            for err in exc.errors(include_url=False, include_input=False, include_context=False)
        )
    else:
        detail = str(exc)
    return f"{type(exc).__name__}: {detail}"[:200]


def _read_json_object(path: Path) -> dict[str, Any] | None:
    """Parse *path* as a JSON object, or log it as unreadable and return None.

    A file that is valid JSON but not an object (``[]``, ``"x"``, ``1``, ``null``) is
    unreadable too: every consumer indexes the result by key, so returning it would
    turn one corrupt file into an ``AttributeError`` for the whole poll.

    A file that vanished between a directory listing and this read is not unreadable: the
    auto-handoff clears a handoff before it spawns the next agent, so that race is normal.
    It returns None without a warning, and callers that list unreadable paths check
    ``path.exists()`` before reporting one.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        _note_unreadable(path, error=_short_error(exc))
        return None
    if not isinstance(data, dict):
        _note_unreadable(path, reason=f"top-level JSON value is {type(data).__name__}, not an object")
        return None
    return data


def read_handoff_json(path: Path) -> dict[str, Any] | None:
    """Read a handoff file as a plain dict, or return None (and log once per version) when unreadable.

    This is the dashboard's reader and checks only that the file is a JSON object. It
    deliberately does not validate ``DashboardHandoff``: the dashboard renders legacy shapes
    too (an action keyed by ``action`` rather than ``id``, a file with no ``summary``).
    ``_parse_handoff()`` is the pipeline's reader and also checks the schema, so a file this
    function accepts can still be one the pipeline has warned about. A success here therefore
    drops a warned-version entry only once the file has changed since it was recorded;
    clearing the entry of the unchanged version would let the pipeline's next read warn again,
    and in the combined server process the two readers alternate on every poll.
    """
    data = _read_json_object(path)
    if data is not None:
        forget_unless_unchanged(path, _warned_unreadable)
    return data


def prune_unreadable_warnings(control_dir: Path, live: list[Path]) -> None:
    """Drop warned-version entries for files in *control_dir* that are not in *live*.

    An entry otherwise leaves only when its own path is later read successfully, so a
    corrupt handoff that was archived, cleared or deleted by hand would keep its entry
    for the life of the process.
    """
    prune_directory(control_dir, live, _warned_unreadable)


def _parse_handoff(path: Path) -> DashboardHandoff | None:
    data = _read_json_object(path)
    if data is None:
        return None
    try:
        handoff = DashboardHandoff.model_validate(data)
    except ValidationError as exc:
        _note_unreadable(path, error=_short_error(exc))
        return None
    forget(path, _warned_unreadable)
    return handoff


def is_schema_valid(data: dict[str, Any]) -> bool:
    """Whether *data* validates as a ``DashboardHandoff``, which is what the pipeline reader requires.

    The dashboard renders any JSON object (legacy shapes included) and uses this to report the ones
    the pipeline will skip, so that disagreement is visible instead of silent.
    """
    try:
        DashboardHandoff.model_validate(data)
    except ValidationError:
        return False
    return True


# ---------------------------------------------------------------------------
# Agent handoff (DB-backed, for orchestrator history)
# ---------------------------------------------------------------------------


class AgentHandoff(BaseModel):
    """Context passed between agent spawns."""

    # Who wrote this
    role: str
    phase: str

    # What happened
    summary: str
    key_decisions: list[str] = Field(default_factory=list)
    files_changed: list[str] = Field(default_factory=list)
    tests_added: list[str] = Field(default_factory=list)

    # What's next
    next_action: str
    pending_findings: list[dict] = Field(default_factory=list)
    addressed_findings: list[dict] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    needs_human: bool = False
    human_message: str | None = None

    # Structured metadata (finding summaries, severity breakdowns, etc.)
    metadata: dict = Field(default_factory=dict)

    # References
    pr_number: int | None = None
    branch_name: str
    commit_shas: list[str] = Field(default_factory=list)


async def write_handoff(task_run_id: int, handoff: AgentHandoff) -> None:
    """Persist a handoff to the TaskRun record."""
    async with await get_session() as session:
        task_run = await session.get(TaskRun, task_run_id)
        if task_run:
            task_run.handoff_json = handoff.model_dump()
            await session.commit()
            log.info("handoff.written", run_id=task_run_id, role=handoff.role, next_action=handoff.next_action)


async def read_handoff(task_run_id: int) -> AgentHandoff | None:
    """Read the most recent handoff from a TaskRun record."""
    async with await get_session() as session:
        task_run = await session.get(TaskRun, task_run_id)
        if not task_run or not task_run.handoff_json:
            return None
        return AgentHandoff.model_validate(task_run.handoff_json)
