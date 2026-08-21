"""Shared helpers for spec provenance threading across pipeline steps."""

from __future__ import annotations

import re
from pathlib import Path

from sova.core.spec_utils import find_spec_file
from sova.utils.logging import get_logger
from sova.utils.markdown import extract_section

log = get_logger(component="step.spec_helpers")

# Section heading constants -- single source of truth for spec provenance threading
SECTION_OPEN_QUESTIONS = "Open Questions"
SECTION_SOLUTION = "Solution"
SECTION_EDGE_CASES = "Edge Cases"
SECTION_DESIGN_DECISIONS = "Design Decisions"
SECTION_SCOPE_BOUNDARIES = "Scope Boundaries"
SECTION_IMPLEMENTATION_NOTES = "Implementation Notes"
SECTION_REVIEW_RATIONALE = "Review Rationale"
SECTION_ADDRESS_REVIEW_NOTES = "Address Review Notes"

# Common heading tuples used by pipeline steps
SPEC_PLAN_SECTIONS = (SECTION_SOLUTION, SECTION_DESIGN_DECISIONS)
DEVELOP_SECTIONS = (
    SECTION_SOLUTION,
    SECTION_EDGE_CASES,
    SECTION_DESIGN_DECISIONS,
    SECTION_SCOPE_BOUNDARIES,
    SECTION_OPEN_QUESTIONS,
)
REVIEW_CONTEXT_SECTIONS = (SECTION_DESIGN_DECISIONS, SECTION_IMPLEMENTATION_NOTES, SECTION_REVIEW_RATIONALE)
MEMORY_EXTRACTION_SECTIONS = (*REVIEW_CONTEXT_SECTIONS, SECTION_ADDRESS_REVIEW_NOTES)


def extract_sections_from_text(text: str, headings: tuple[str, ...]) -> str:
    """Extract and concatenate sections from pre-loaded spec text.

    Like ``read_spec_sections`` but operates on an already-loaded string,
    avoiding a redundant file read when the caller has the text in hand.
    """
    parts: list[str] = []
    for heading in headings:
        content = extract_section(text, heading)
        if content:
            parts.append(f"## {heading}\n{content}")
    return "\n\n".join(parts)


def append_spec_section(
    issue_number: str,
    section_heading: str,
    content: str,
    project_dir: Path,
) -> bool:
    """Append a named section to the spec file. Returns True if written.

    If the section already exists, replaces its content.
    If no spec file exists, returns False (non-fatal).
    """
    path = find_spec_file(issue_number, project_dir)
    if path is None:
        log.debug("spec_helpers.no_spec", issue=issue_number)
        return False

    text = path.read_text()

    existing = extract_section(text, section_heading)

    if existing:
        text = _replace_section(text, section_heading, content)
    else:
        text = text.rstrip() + f"\n\n## {section_heading}\n" + content + "\n"

    path.write_text(text)
    log.info("spec_helpers.appended", issue=issue_number, section=section_heading)
    return True


def read_spec_sections(
    issue_number: str,
    project_dir: Path,
    headings: tuple[str, ...],
) -> str:
    """Read and concatenate specified sections from the spec file.

    Returns empty string if no spec or no matching sections found.
    """
    path = find_spec_file(issue_number, project_dir)
    if path is None:
        return ""

    text = path.read_text()
    parts: list[str] = []
    for heading in headings:
        content = extract_section(text, heading)
        if content:
            parts.append(f"## {heading}\n{content}")

    return "\n\n".join(parts)


def _replace_section(text: str, heading: str, new_content: str) -> str:
    """Replace the content of an existing ## section in markdown text."""
    from sova.utils.markdown import _strip_fenced_blocks

    masked = _strip_fenced_blocks(text)

    pattern = rf"(^## {re.escape(heading)}\s*$)"
    match = re.search(pattern, masked, re.MULTILINE)
    if not match:
        return text

    section_start = match.end()

    # Find the next ## heading using masked text (skips fenced blocks)
    next_heading = re.search(r"^## ", masked[section_start:], re.MULTILINE)
    if next_heading:
        section_end = section_start + next_heading.start()
    else:
        section_end = len(text)

    return text[:section_start] + "\n" + new_content + "\n\n" + text[section_end:]
