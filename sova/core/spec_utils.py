"""Spec file utilities: find, read, and parse spec files.

Core-layer functions for spec file operations. Dashboard-level mutations
(approve, reject, write_answers) remain in ``sova.dashboard.services.spec_service``.
"""

from __future__ import annotations

import hashlib
import re
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from sova.config.context import get_project_dir
from sova.utils.logging import get_logger
from sova.utils.markdown import _strip_fenced_blocks
from sova.utils.markdown import extract_section as _extract_section

if TYPE_CHECKING:
    from sova.adapters.base import Task

log = get_logger(component="core.spec_utils")

# Sections SOVA appends to an issue body after the spec was written (triage,
# research). Excluded from the issue-revision hash so those appends don't
# make an otherwise-unchanged spec look stale. All three are always appended
# at the end of the body (never interleaved), so ``_strip_appended_sections``
# strips by position (first match to EOF) rather than per-heading, which
# would otherwise stop at the first ``## `` subheading an LLM-authored
# Research section happens to contain and leave residue after it.
_APPENDED_SECTION_HEADINGS = ("Triage Assessment", "Research")
_APPENDED_SECTION_RE = re.compile(
    r"^## (?:" + "|".join(re.escape(h) for h in _APPENDED_SECTION_HEADINGS) + r")\s*$",
    re.MULTILINE,
)

_ISSUE_REVISION_HASH_LEN = 12
# ``[ \t]*`` (not ``\s*``) keeps the capture on the marker's own line: ``\s``
# includes ``\n``, which let a value on a following line (e.g. a blank line
# then prose) be captured as the hash.
_ISSUE_REVISION_MARKER_RE = re.compile(r"\*\*Issue revision\*\*:[ \t]*(\S*)")
_HEX_RE = re.compile(rf"[0-9a-fA-F]{{{_ISSUE_REVISION_HASH_LEN}}}")
# MULTILINE + anchored to the start of a line: a genuine ATX title is "# "
# (single hash, space), never "## " (a section heading), so this never
# mis-fires on a ``## `` boundary. Searched against a fence-masked copy of
# the spec (see ``spec_is_current``) so a "# " line inside a fenced example
# block is never mistaken for the real title.
_SPEC_TITLE_RE = re.compile(r"^# (.+)$", re.MULTILINE)

# Prefix the spec template puts in front of the issue title in a spec's
# ``# Spec: <title>`` heading.
_SPEC_TITLE_PREFIX = "Spec: "


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


def _unwrap_original_description(body: str) -> str:
    """Undo the ``/spec`` command's optional Step 8 write-back, which restructures the
    whole issue body as ``## Original Description`` + ``---`` + ``## Implementation Spec``.

    That rewrite is purely cosmetic (the original requirement text is preserved
    verbatim inside ``## Original Description``), so without this the hash would
    change the moment a spec is written back, invalidating the hash it was meant
    to protect. Returns the unwrapped original content when the heading is
    present (which also drops ``## Implementation Spec`` and the ``---``
    separator, since both fall after it and ``extract_section`` stops at the
    next ``## ``), or *body* unchanged otherwise.
    """
    original = _extract_section(body, "Original Description")
    if not original:
        return body
    # The template's ``---`` separator sits between the two halves, so
    # extract_section includes it as trailing content of this section.
    return re.sub(r"\n*-{3,}\s*$", "", original).strip()


def _strip_appended_sections(body: str) -> str:
    """Remove SOVA-appended sections (see ``_APPENDED_SECTION_HEADINGS``) from an issue body,
    and return the remainder with surrounding whitespace trimmed.

    Shared by the writer (``_stamp_issue_revision`` in ``spec.py``, which hashes
    an issue's title/body when a spec is written) and the checker
    (``spec_is_current``, which recomputes the same hash from the live issue),
    so neither can drift on what counts as "the issue's own content".
    """
    body = _unwrap_original_description(body)
    masked = _strip_fenced_blocks(body)
    match = _APPENDED_SECTION_RE.search(masked)
    if match:
        body = body[: match.start()]
    return body.strip()


def _frontmatter_span(text: str) -> str:
    """Return the masked, frontmatter-bounded slice of *text* used for stamp placement/parsing.

    Masks fenced code blocks (so a structural marker quoted inside an example
    is invisible) and stops at the first ``## `` heading (frontmatter ends
    there). Shared by ``spec_is_current``/``check_spec_freshness`` (marker
    search) and ``_stamp_issue_revision`` in ``spec.py`` (insertion point), so
    the writer can never place the stamp somewhere the checker doesn't look.
    Length-preserving relative to *text* up to the returned slice's length, so
    an offset found in the result maps 1:1 onto *text* itself.
    """
    masked = _strip_fenced_blocks(text)
    heading_boundary = re.search(r"^## ", masked, re.MULTILINE)
    frontmatter_end = heading_boundary.start() if heading_boundary else len(masked)
    return masked[:frontmatter_end]


def compute_issue_revision_hash(title: str, body: str) -> str:
    """Return a short fingerprint of an issue's title and body.

    Truncated to 12 hex chars: this is a staleness fingerprint, not a
    security boundary, and a short hash keeps the spec header line
    human-scannable. The title is trimmed exactly like the legacy title
    comparison in ``spec_is_current`` does, so the two paths cannot disagree
    over whether a title differing only by surrounding whitespace counts as
    a change.
    """
    stripped_title = (title or "").strip()
    stripped_body = _strip_appended_sections(body or "")
    digest = hashlib.sha256(f"{stripped_title}\n\n{stripped_body}".encode()).hexdigest()
    return digest[:_ISSUE_REVISION_HASH_LEN]


class SpecFreshness(Enum):
    """How trustworthy a spec is against an issue's current state.

    ``CURRENT``: safe to use as-is. ``STALE``: an issue-revision hash mismatch,
    meaning the issue provably changed after the spec was written. ``UNMATCHED``:
    a legacy spec with no hash marker whose title no longer matches the issue,
    so it cannot even be confirmed to be this issue's spec. ``UNVERIFIABLE``: no
    task was available to compare against (e.g. a tracker outage). Each is
    reported and handled distinctly by consumers rather than being collapsed
    into one generic "stale" bucket: see ``spec_freshness_caveat``.
    """

    CURRENT = "current"
    STALE = "stale"
    UNMATCHED = "unmatched"
    UNVERIFIABLE = "unverifiable"


def check_spec_freshness(spec_text: str, task: Task | None) -> SpecFreshness:
    """Classify how trustworthy *spec_text* is against *task*'s current title/body.

    A spec written after this check shipped carries a ``**Issue revision**:
    <hash>`` line. A mismatched hash means the issue changed since the spec
    was written (``STALE``); an unparseable/corrupted value (e.g. hand-edited)
    is treated the same way rather than raising, since a byte-mismatch is the
    expected "not current" outcome either way. Either case is decided here
    directly, without falling through to the legacy title comparison below: a
    spec new enough to carry the marker is never compared by title.

    A legacy spec with no such marker at all falls back to comparing its
    ``# Spec: <title>`` heading against the issue's current title (trimmed,
    exact match), so an unchanged legacy spec stays usable without forcing
    every pre-existing spec to be regenerated. A mismatch there is
    ``UNMATCHED`` rather than ``STALE``: there is no hash proving the issue
    changed, only a title that no longer lines up, so the spec may simply not
    be this issue's. A legacy spec with no title heading either (so there is
    nothing to compare) is treated as ``CURRENT``: there is no signal to call
    it stale on.

    ``task is None`` (no issue available to compare against, e.g. a tracker
    outage) is ``UNVERIFIABLE``, decided before any text parsing.

    Both the marker search and the title search run against a fence-masked
    copy of *spec_text* (see ``_strip_fenced_blocks``), and the marker search
    is further restricted to the spec's frontmatter via ``_frontmatter_span``
    (the text before the first ``## `` heading, which is where
    ``_stamp_issue_revision`` always writes it). Otherwise a spec whose body
    quotes the marker or a ``# `` heading inside a fenced example (this
    module's own docs do) would have that quoted text read as the spec's real
    stamp or title.
    """
    if task is None:
        return SpecFreshness.UNVERIFIABLE

    masked = _strip_fenced_blocks(spec_text)
    frontmatter = _frontmatter_span(spec_text)

    marker_match = _ISSUE_REVISION_MARKER_RE.search(frontmatter)
    if marker_match:
        raw_value = marker_match.group(1)
        if not _HEX_RE.fullmatch(raw_value):
            return SpecFreshness.STALE
        current_hash = compute_issue_revision_hash(task.title, task.body or "")
        return SpecFreshness.CURRENT if raw_value.lower() == current_hash else SpecFreshness.STALE

    title_match = _SPEC_TITLE_RE.search(masked)
    if not title_match:
        return SpecFreshness.CURRENT
    spec_title = title_match.group(1).strip()
    spec_title = spec_title.removeprefix(_SPEC_TITLE_PREFIX)
    return SpecFreshness.CURRENT if spec_title == (task.title or "").strip() else SpecFreshness.UNMATCHED


def spec_is_current(spec_text: str, task: Task) -> bool:
    """Return True if *spec_text* still reflects *task*'s current title/body.

    Thin bool wrapper over ``check_spec_freshness`` for callers that only need
    a yes/no answer.
    """
    return check_spec_freshness(spec_text, task) is SpecFreshness.CURRENT


_FRESHNESS_CAVEATS: dict[SpecFreshness, str] = {
    SpecFreshness.STALE: (
        "This spec may be stale: the issue changed after it was written (issue-revision hash "
        "mismatch). Treat it as background only; the issue's own current body and criteria win "
        "on any conflict."
    ),
    SpecFreshness.UNMATCHED: (
        "This spec could not be confirmed to match the current issue (legacy spec with no "
        "issue-revision marker, and its title no longer matches). Treat it as background only; "
        "the issue's own current body and criteria win on any conflict."
    ),
    SpecFreshness.UNVERIFIABLE: (
        "This spec's freshness could not be verified (the issue could not be fetched). Treat it "
        "as background only; the issue's own current body and criteria win on any conflict."
    ),
}


def spec_freshness_caveat(freshness: SpecFreshness) -> str:
    """Return an operator/reviewer-facing caveat for a non-current *freshness*, or "" when current."""
    return _FRESHNESS_CAVEATS.get(freshness, "")


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

    # Extract title from first heading: avoid regex backtracking (S5852)
    title = f"Spec for #{issue_number}"
    title_match = re.search(r"^# (.+)", text)
    if title_match:
        raw = title_match.group(1).strip()
        title = raw.removeprefix(_SPEC_TITLE_PREFIX)
    result["title"] = title

    # Extract open questions
    result["open_questions"] = _extract_open_questions(text)
    result["has_open_questions"] = len(result["open_questions"]) > 0

    return result


def _find_answer_marker(text: str) -> int | None:
    """Return the index of a whitespace-preceded ``A:`` in ``text``, or ``None``.

    Requires at least two characters before ``A:`` (one for the question
    text, one of whitespace separating it), matching the original
    ``\\s+A:`` regex: a lone whitespace char immediately before ``A:`` with
    nothing before it (e.g. ``"Q: A: answer"``) has no room left for a
    non-empty question-text group, so the old regex failed to match at all
    and left the line unsplit.
    """
    start = 0
    while True:
        idx = text.find("A:", start)
        if idx == -1:
            return None
        if idx >= 2 and text[idx - 1].isspace():
            return idx
        start = idx + 1


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
        # Detect Q: ... A: ... format written by write_answers(). A long run
        # of spaces with no " A:" marker made both a negative-lookahead scan
        # and a backtracking "\s+A:" regex super-linear (each rescans the
        # whole remaining run from every position in the run). A direct
        # str.find() for the literal "A:" is unambiguous here and O(n).
        answer = ""
        if cleaned.startswith("Q:"):
            rest = cleaned[2:]
            sep = _find_answer_marker(rest)
            if sep is not None:
                cleaned = rest[:sep].strip()
                answer = rest[sep + 2 :].strip()
            else:
                cleaned = rest.strip()
        questions.append({"id": question_id, "text": cleaned, "answer": answer})
        question_id += 1

    return questions
