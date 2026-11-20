"""Spec service: read, approve, reject, and manage spec files.

Read-only spec operations (find, read, parse) live in ``sova.core.spec_utils``
and are re-exported here for backward compatibility.  Dashboard-level mutations
(approve, reject, write_answers) remain in this module.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path

from sova.config.context import get_project_dir
from sova.config.loader import load_config
from sova.core.spec_utils import (
    SpecDrift,
    SpecDriftResult,
    _extract_open_questions,
    _parse_spec,
    _specs_dir,
    check_spec_code_drift,
    find_spec_file,
    read_spec,
)
from sova.core.steps._spec_helpers import SECTION_IMPLEMENTATION_NOTES
from sova.db.session import get_session
from sova.utils.logging import get_logger
from sova.utils.markdown import extract_section

log = get_logger(component="dashboard.service.spec")

# Re-export core functions for backward compatibility.
__all__ = [
    "_extract_open_questions",
    "_parse_spec",
    "_specs_dir",
    "find_spec_file",
    "read_spec",
    "approve_spec",
    "reject_spec",
    "list_pending_specs",
    "list_all_specs",
    "write_answers",
    "spec_drift",
]


def approve_spec(
    issue_number: str,
    project_dir: Path | None = None,
) -> dict:
    """Approve a spec.

    Returns the updated spec dict or {"error": ...}.
    """
    path = find_spec_file(issue_number, project_dir)
    if path is None:
        return {"error": f"No spec file found for issue #{issue_number}"}

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("spec.approve.read_failed", issue=issue_number, exc_info=True)
        return {"error": f"Failed to read spec file: {exc}"}

    # Guard: only approve draft specs
    status_match = re.search(r"\*\*Status\*\*:\s*(\w+)", text, re.IGNORECASE)
    current_status = status_match.group(1).lower() if status_match else "draft"
    if current_status != "draft":
        return {"error": f"Spec is already '{current_status}', cannot approve"}

    # Update status to approved (count=1: only replace the first/frontmatter occurrence)
    text = re.sub(r"\*\*Status\*\*:\s*\w+", "**Status**: approved", text, count=1)

    try:
        path.write_text(text, encoding="utf-8")
    except (OSError, UnicodeEncodeError) as exc:
        log.warning("spec.approve.write_failed", issue=issue_number, exc_info=True)
        return {"error": f"Failed to write spec file: {exc}"}

    log.info("spec.approved", issue=issue_number, path=str(path))
    return _parse_spec(text, path, issue_number)


def reject_spec(issue_number: str, project_dir: Path | None = None) -> dict:
    """Mark a spec as rejected."""
    path = find_spec_file(issue_number, project_dir)
    if path is None:
        return {"error": f"No spec file found for issue #{issue_number}"}

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("spec.reject.read_failed", issue=issue_number, exc_info=True)
        return {"error": f"Failed to read spec file: {exc}"}

    # Guard: only reject draft specs (consistent with approve_spec)
    status_match = re.search(r"\*\*Status\*\*:\s*(\w+)", text, re.IGNORECASE)
    current_status = status_match.group(1).lower() if status_match else "draft"
    if current_status not in ("draft", "rejected"):
        return {"error": f"Spec is '{current_status}', cannot reject"}

    text = re.sub(r"\*\*Status\*\*:\s*\w+", "**Status**: rejected", text, count=1)

    try:
        path.write_text(text, encoding="utf-8")
    except (OSError, UnicodeEncodeError) as exc:
        log.warning("spec.reject.write_failed", issue=issue_number, exc_info=True)
        return {"error": f"Failed to write spec file: {exc}"}

    log.info("spec.rejected", issue=issue_number, path=str(path))
    return {"status": "rejected", "issue_number": issue_number}


def _drift_for(raw_content: str, repo_dir: Path) -> SpecDriftResult:
    """Load the project's base branch and run the drift check (blocking: config read plus git)."""
    return check_spec_code_drift(raw_content, repo_dir, load_config(repo_dir).base_branch)


_DRIFT_STATUSES = frozenset({"draft", "approved"})


def _spec_written_at(issue_number: str, project_dir: Path) -> datetime | None:
    """When the spec file for *issue_number* was last written (UTC), or None when it cannot be read."""
    try:
        path = find_spec_file(issue_number, project_dir)
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) if path is not None else None
    except OSError:
        return None


async def _development_started(issue_number: str, project_dir: Path) -> bool | None:
    """True when a developer run for *issue_number* has opened a PR or is still active; None when unknown.

    The spec has no "implemented" status and ``## Implementation Notes`` is optional (skipped without a task,
    non-fatal on a failed write, absent from specs written before it existed), so neither can say that the
    spec's own change has landed. A developer ``TaskRun`` can: once it has a PR, the files the spec names are
    changed by that PR on the base branch, and a drift warning would always fire and mean nothing. An active
    developer run is also past the point where a re-research makes sense (``/revise`` refuses it anyway).
    Only a PR run started after the spec file was last written counts: a run that predates a re-research (for
    example a PR closed unmerged) consumed an older spec, so it must not hide the new spec's drift. An unreadable
    spec timestamp applies no such cut-off. An active run counts whatever its age (``/revise`` refuses it).
    A failed lookup is ``None``, never ``False``, so the caller does not read it as "no development yet".
    """
    from sqlalchemy import and_, or_, select
    from sqlalchemy.exc import SQLAlchemyError

    from sova.core.state import TASK_RUN_TERMINAL
    from sova.db.models import TaskRun

    spec_written_at = _spec_written_at(issue_number, project_dir)
    opened_pr = TaskRun.pr_number.isnot(None)
    if spec_written_at is not None:
        opened_pr = and_(opened_pr, TaskRun.started_at >= spec_written_at)
    try:
        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                stmt = (
                    select(TaskRun.id)
                    .where(
                        TaskRun.issue_number == issue_number,
                        TaskRun.role == "developer",
                        or_(opened_pr, TaskRun.status.notin_(TASK_RUN_TERMINAL)),
                    )
                    .limit(1)
                )
                return (await session.execute(stmt)).first() is not None
    except (OSError, RuntimeError, SQLAlchemyError):
        log.warning("spec.drift_run_lookup_failed", issue=issue_number, exc_info=True)
        return None


async def _spec_still_guides_work(raw_content: str, status: str, issue_number: str | None, project_dir: Path) -> bool:
    """True when the spec can still steer development, so a drift warning is meaningful.

    A rejected spec cannot. Neither can one whose own change has landed or is in flight: it has Implementation
    Notes, or a developer run for the issue has a PR or is active (``_development_started``). When the run
    lookup fails the answer is unknown, and an unknown is not checked (never a warning on a guess).
    """
    if status not in _DRIFT_STATUSES or extract_section(raw_content, SECTION_IMPLEMENTATION_NOTES):
        return False
    if issue_number is None:
        return True
    return await _development_started(issue_number, project_dir) is False


async def spec_drift(
    raw_content: str,
    status: str = "draft",
    project_dir: Path | None = None,
    issue_number: str | None = None,
) -> dict:
    """Report whether files *raw_content* names changed on the base branch after the spec was written.

    Only a spec that can still guide work (see ``_spec_still_guides_work``) is checked; a rejected or already
    implemented spec is ``unverifiable`` with no files. Runs against the project's primary checkout in a worker
    thread, with local refs only (never a fetch), so it cannot stall the event loop or the request. Any failure
    is ``unverifiable``, never ``current``.
    """
    result = SpecDriftResult(SpecDrift.UNVERIFIABLE)
    try:
        repo_dir = project_dir or get_project_dir()
        if repo_dir is not None and await _spec_still_guides_work(raw_content, status, issue_number, repo_dir):
            result = await asyncio.to_thread(_drift_for, raw_content, repo_dir)
    except Exception:  # noqa: BLE001 (config load and thread dispatch fail in unrelated ways; the check is advisory)
        log.warning("spec.drift_unavailable", exc_info=True)
    return {"status": result.status.value, "files": list(result.files)}


def _iter_all_specs(project_dir: Path | None = None) -> list[dict]:
    """Parse all spec files in the specs directory into structured dicts."""
    specs = _specs_dir(project_dir)
    if specs is None or not specs.exists():
        return []

    results = []
    for f in sorted(specs.iterdir()):
        if f.suffix != ".md":
            continue
        issue_match = re.match(r"(\d+)-", f.name) or re.match(r"[A-Z][A-Z0-9_]*-(\d+)-", f.name)
        if not issue_match:
            continue
        try:
            text = f.read_text(encoding="utf-8")
            parsed = _parse_spec(text, f, issue_match.group(1))
        except (OSError, ValueError):
            log.warning("spec.parse_failed", file=str(f), exc_info=True)
            continue
        results.append(parsed)
    return results


def list_pending_specs(project_dir: Path | None = None) -> list[dict]:
    """List all draft specs awaiting approval."""
    return [s for s in _iter_all_specs(project_dir) if s["status"] == "draft"]


_STATUS_SORT_ORDER = {"draft": 0, "approved": 1, "rejected": 2}


def list_all_specs(project_dir: Path | None = None) -> list[dict]:
    """List all specs (draft, approved, rejected) sorted by status then created date descending."""
    results = _iter_all_specs(project_dir)
    # Two-pass sort: first by created desc, then by status asc.
    # Python's sort is stable (guaranteed by language spec), so the created
    # ordering is preserved within each status group.
    results.sort(key=lambda s: s.get("created") or "", reverse=True)
    results.sort(key=lambda s: _STATUS_SORT_ORDER.get(s["status"], 99))
    return results


def write_answers(
    issue_number: str,
    answers: dict[str, str],
    project_dir: Path | None = None,
) -> None:
    """Write user-provided answers into the spec's Open Questions section.

    Each answer replaces the original question line with 'Q: ... A: ...' format.
    """
    path = find_spec_file(issue_number, project_dir)
    if path is None or not answers:
        return

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        log.warning("spec.write_answers.read_failed", issue=issue_number, exc_info=True)
        return

    questions = _extract_open_questions(text)
    if not questions:
        return

    for q in questions:
        answer = answers.get(str(q["id"]), "")
        if answer:
            if q["answer"]:
                # Re-answering: replace the complete Q/A entry to avoid corruption
                old_entry = f"Q: {q['text']} A: {q['answer']}"
                new_entry = f"Q: {q['text']} A: {answer}"
                text = text.replace(old_entry, new_entry)
            else:
                # First-time answer: replace just the question text
                text = text.replace(q["text"], f"Q: {q['text']} A: {answer}")

    try:
        path.write_text(text, encoding="utf-8")
    except (OSError, UnicodeEncodeError):
        log.warning("spec.write_answers.write_failed", issue=issue_number, exc_info=True)
