"""Spec service -- read, approve, reject, and manage spec files."""

from __future__ import annotations

import re
from pathlib import Path

from sova.config.context import get_project_dir
from sova.utils.logging import get_logger
from sova.utils.markdown import extract_section as _extract_section

log = get_logger(component="dashboard.service.spec")


def _specs_dir(project_dir: Path | None = None) -> Path:
    """Return the .claude/specs directory for the project."""
    d = project_dir or get_project_dir()
    return d / ".claude" / "specs"


def find_spec_file(issue_number: str, project_dir: Path | None = None) -> Path | None:
    """Find the spec file for an issue.

    Matches both ``{issue_number}-slug.md`` (GitHub) and
    ``{PROJECT}-{issue_number}-slug.md`` (Jira key prefix).
    """
    specs = _specs_dir(project_dir)
    if not specs.exists():
        return None
    for f in specs.iterdir():
        if f.suffix != ".md":
            continue
        if f.name.startswith(f"{issue_number}-"):
            return f
        parts = f.stem.split("-", 2)
        if len(parts) >= 2 and parts[1] == issue_number and re.match(r"[A-Z][A-Z0-9_]*$", parts[0]):
            return f
    return None


def read_spec(issue_number: str, project_dir: Path | None = None) -> dict | None:
    """Read and parse a spec file into a structured dict.

    Returns None if no spec file exists for the issue.
    """
    path = find_spec_file(issue_number, project_dir)
    if path is None:
        return None

    text = path.read_text()
    return _parse_spec(text, path, issue_number)


def _parse_spec(text: str, path: Path, issue_number: str) -> dict:
    """Parse spec markdown into structured data."""
    result: dict = {
        "issue_number": issue_number,
        "file_path": str(path),
        "file_name": path.name,
        "raw_content": text,
    }

    # Extract frontmatter fields
    status_match = re.search(r"\*\*Status\*\*:\s*(\w+)", text, re.IGNORECASE)
    result["status"] = status_match.group(1).lower() if status_match else "draft"

    complexity_match = re.search(r"\*\*Complexity\*\*:\s*(\w+)", text, re.IGNORECASE)
    result["complexity"] = complexity_match.group(1).lower() if complexity_match else "unknown"

    created_match = re.search(r"\*\*Created\*\*:\s*([\d-]+)", text)
    result["created"] = created_match.group(1) if created_match else ""

    # Extract title from first heading -- avoid regex backtracking (S5852)
    title = f"Spec for #{issue_number}"
    title_match = re.search(r"^# (.+)", text)
    if title_match:
        raw = title_match.group(1).strip()
        title = raw.removeprefix("Spec: ") if raw.startswith("Spec: ") else raw
    result["title"] = title

    # Extract open questions
    result["open_questions"] = _extract_open_questions(text)
    result["has_open_questions"] = len(result["open_questions"]) > 0

    return result


# _extract_section is imported from sova.utils.markdown above


def _extract_open_questions(text: str) -> list[dict]:
    """Extract open questions from the spec."""
    content = _extract_section(text, "Open Questions")
    normalized = content.strip().lower() if content else ""
    if not normalized or normalized.startswith("(omit") or normalized.startswith("none"):
        return []

    questions = []
    question_id = 0
    for line in content.split("\n"):
        line = line.strip()
        if not line or line.startswith("("):
            continue
        # Strip list markers
        cleaned = re.sub(r"^[-*\d.]+\s*", "", line)
        if not cleaned:
            continue
        # Detect Q: ... A: ... format written by write_answers(); non-greedy first
        # group ensures we split on the FIRST " A: " occurrence, so answers
        # containing " A: " are preserved.
        answer = ""
        if cleaned.startswith("Q:"):
            qa_match = re.match(r"^Q:\s*(.+?)\s+A:\s*(.+)$", cleaned)
            if qa_match:
                cleaned = qa_match.group(1).strip()
                answer = qa_match.group(2).strip()
            else:
                cleaned = cleaned[2:].strip()
        questions.append({"id": question_id, "text": cleaned, "answer": answer})
        question_id += 1

    return questions


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
    if not specs.exists():
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
