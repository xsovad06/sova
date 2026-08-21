"""Spec file utilities -- find, read, and parse spec files.

Core-layer functions for spec file operations. Dashboard-level mutations
(approve, reject, write_answers) remain in ``sova.dashboard.services.spec_service``.
"""

from __future__ import annotations

import re
from pathlib import Path

from sova.config.context import get_project_dir
from sova.utils.logging import get_logger
from sova.utils.markdown import extract_section as _extract_section

log = get_logger(component="core.spec_utils")


def _specs_dir(project_dir: Path | None = None) -> Path | None:
    """Return the .claude/specs directory for the project, or None if unavailable."""
    d = project_dir or get_project_dir()
    if d is None:
        return None
    return d / ".claude" / "specs"


def find_spec_file(issue_number: str, project_dir: Path | None = None) -> Path | None:
    """Find the spec file for an issue.

    Matches both ``{issue_number}-slug.md`` (GitHub) and
    ``{PROJECT}-{issue_number}-slug.md`` (Jira key prefix).
    """
    specs = _specs_dir(project_dir)
    if specs is None or not specs.exists():
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

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        log.warning("spec.read_failed", path=str(path), exc_info=True)
        return None
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
        answer = ""
        if cleaned.startswith("Q:"):
            a_idx = cleaned.find(" A: ")
            if a_idx != -1:
                answer = cleaned[a_idx + 4 :].strip()
                cleaned = cleaned[2:a_idx].strip()
            else:
                cleaned = cleaned[2:].strip()
        questions.append({"id": question_id, "text": cleaned, "answer": answer})
        question_id += 1

    return questions
