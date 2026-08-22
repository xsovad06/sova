"""Spec service -- read, approve, reject, and manage spec files.

Read-only spec operations (find, read, parse, extract) are defined in
``sova.core.spec_utils`` and re-exported here for backward compatibility.
Dashboard-level mutations (approve, reject, write_answers) remain in this module.
"""

from __future__ import annotations

import re
from pathlib import Path

from sova.core.spec_utils import (
    _extract_open_questions,
    _parse_spec,
    _specs_dir,
    find_spec_file,
    read_spec,
)
from sova.utils.logging import get_logger

# Re-export core functions for backward compatibility.
__all__ = [
    "_extract_open_questions",
    "_parse_spec",
    "_specs_dir",
    "approve_spec",
    "find_spec_file",
    "list_all_specs",
    "list_pending_specs",
    "read_spec",
    "reject_spec",
    "write_answers",
]

log = get_logger(component="dashboard.service.spec")


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

    text = path.read_text()

    # Guard: only approve draft specs
    status_match = re.search(r"\*\*Status\*\*:\s*(\w+)", text, re.IGNORECASE)
    current_status = status_match.group(1).lower() if status_match else "draft"
    if current_status != "draft":
        return {"error": f"Spec is already '{current_status}', cannot approve"}

    # Update status to approved (count=1: only replace the first/frontmatter occurrence)
    text = re.sub(r"\*\*Status\*\*:\s*\w+", "**Status**: approved", text, count=1)
    path.write_text(text)

    log.info("spec.approved", issue=issue_number, path=str(path))
    return _parse_spec(text, path, issue_number)


def reject_spec(issue_number: str, project_dir: Path | None = None) -> dict:
    """Mark a spec as rejected."""
    path = find_spec_file(issue_number, project_dir)
    if path is None:
        return {"error": f"No spec file found for issue #{issue_number}"}

    text = path.read_text()

    # Guard: only reject draft specs (consistent with approve_spec)
    status_match = re.search(r"\*\*Status\*\*:\s*(\w+)", text, re.IGNORECASE)
    current_status = status_match.group(1).lower() if status_match else "draft"
    if current_status not in ("draft", "rejected"):
        return {"error": f"Spec is '{current_status}', cannot reject"}

    text = re.sub(r"\*\*Status\*\*:\s*\w+", "**Status**: rejected", text, count=1)
    path.write_text(text)

    log.info("spec.rejected", issue=issue_number, path=str(path))
    return {"status": "rejected", "issue_number": issue_number}


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
            text = f.read_text()
            parsed = _parse_spec(text, f, issue_match.group(1))
        except Exception:
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

    text = path.read_text()
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

    path.write_text(text)
