"""Spec file utilities: find, read, and parse spec files.

Core-layer functions for spec file operations. Dashboard-level mutations
(approve, reject, write_answers) remain in ``sova.dashboard.services.spec_service``.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
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


# Spec code-drift check: did files the spec names change on the base branch after research?
# Anchored to line start, like the writer's ``_STAMP_LINE_RE`` in ``steps/spec.py``: an inline mention mid-line
# is neither stripped by the writer nor honoured here.
_BASE_COMMIT_MARKER_RE = re.compile(r"^\*\*Base commit\*\*:[ \t]*(\S*)", re.MULTILINE)
_FULL_SHA_RE = re.compile(r"[0-9a-fA-F]{40}")
_BACKTICKED_RE = re.compile(r"`([^`\n]+)`")
# A repo-relative path: path characters only, with at least one letter (so a version string such as ``1.2.3`` is
# not mistaken for a file, while ``Makefile`` and ``LICENSE`` are kept). Words that are not files are dropped
# later by the intersection with the real changed-file list.
_PATH_CHARS_RE = re.compile(r"[\w.@+/-]+")
_LETTER_RE = re.compile(r"[A-Za-z]")
# One or more ``:line``, ``:line-line`` or ``:line:col`` suffixes.
_LINE_SUFFIX_RE = re.compile(r"(?::\d+(?:-\d+)?)+$")
_TRAILING_PUNCTUATION = ",;.:)"
# Wrapping punctuation that prose puts around a path: ``(sova/a.py``, ``"sova/a.py"``, ``[sova/a.py]``.
_LEADING_WRAPPERS = "(\"'["
_TRAILING_WRAPPERS = "\"']"
_STDERR_LOG_CHARS = 200
_DRIFT_GIT_TIMEOUT_SECONDS = 10
_MAX_DRIFT_FILES_SHOWN = 10


class SpecDrift(Enum):
    """Whether the code a spec names changed on the base branch after the spec was written.

    ``UNVERIFIABLE`` covers every case where the answer is unknown (no or malformed stamp, unknown commit,
    missing ``origin/<base>`` ref, git failure), so a broken check never reads as ``CURRENT``.
    """

    CURRENT = "current"
    DRIFTED = "drifted"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class SpecDriftResult:
    status: SpecDrift
    files: tuple[str, ...] = ()


_UNVERIFIABLE_DRIFT = SpecDriftResult(SpecDrift.UNVERIFIABLE)


def _git_stdout(repo_dir: Path, *args: str) -> str | None:
    """Run ``git <args>`` in *repo_dir* and return stdout, or None on any failure or timeout."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_DRIFT_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        log.warning("spec.drift_git_failed", args=args, exc_info=True)
        return None
    if proc.returncode != 0:
        log.info(
            "spec.drift_git_nonzero",
            args=args,
            returncode=proc.returncode,
            stderr=(proc.stderr or "")[:_STDERR_LOG_CHARS],
        )
        return None
    return proc.stdout


def compute_base_commit(repo_dir: Path, base_branch: str) -> str | None:
    """Return the commit of ``origin/<base_branch>`` that *repo_dir*'s HEAD builds on, or None.

    The merge-base (not HEAD) is stamped: a researcher's checkout may carry unrelated local commits, and only
    the part of the history that is on the base branch can be changed by later merges. No fetch is done.
    """
    out = _git_stdout(repo_dir, "merge-base", "HEAD", f"origin/{base_branch}")
    sha = (out or "").strip()
    if not _FULL_SHA_RE.fullmatch(sha):
        return None
    sha = sha.lower()
    tip = (_git_stdout(repo_dir, "rev-parse", f"origin/{base_branch}") or "").strip().lower()
    if tip and tip != sha:
        # Drift is judged against origin/<base>, so a checkout behind it keeps reporting drift after a re-research.
        log.warning("spec.base_commit_behind_origin", base_branch=base_branch, stamped=sha, origin_tip=tip)
    return sha


def _normalize_spec_path(token: str) -> str | None:
    """Turn one backticked word into a repo-relative path, or None when it does not look like one."""
    path = token.lstrip(_LEADING_WRAPPERS).split("::", 1)[0]
    # "()" comes off before a lone ")" so ``func()`` and ``(see a.py)`` both reduce correctly.
    path = path.rstrip(_TRAILING_PUNCTUATION.replace(")", "") + _TRAILING_WRAPPERS).removesuffix("()")
    path = path.rstrip(_TRAILING_PUNCTUATION + _TRAILING_WRAPPERS)
    path = _LINE_SUFFIX_RE.sub("", path).removeprefix("./").rstrip(_TRAILING_PUNCTUATION + _TRAILING_WRAPPERS)
    if not path or path.startswith("/") or not _PATH_CHARS_RE.fullmatch(path) or not _LETTER_RE.search(path):
        return None
    return path


def extract_spec_paths(spec_text: str) -> set[str]:
    """Return the repo-relative paths a spec names in single backticks (fenced blocks are ignored).

    A path ending in ``/`` is kept as a directory prefix. Anything that is not path-shaped is dropped here,
    and a path-shaped token that is not a changed file is dropped later by the intersection.
    """
    paths: set[str] = set()
    for match in _BACKTICKED_RE.finditer(_strip_fenced_blocks(spec_text)):
        for word in match.group(1).split():
            path = _normalize_spec_path(word)
            if path is not None:
                paths.add(path)
    return paths


def check_spec_code_drift(spec_text: str, repo_dir: Path, base_branch: str) -> SpecDriftResult:
    """Compare the spec's ``**Base commit**`` stamp with ``origin/<base_branch>`` and report touched spec paths.

    Local refs only (no fetch), so a stale ``origin/<base_branch>`` makes the warning later, never wrong.
    Blocking git calls: async callers run this in a worker thread. Never raises.
    """
    try:
        match = _BASE_COMMIT_MARKER_RE.search(_frontmatter_span(spec_text))
        if not match or not _FULL_SHA_RE.fullmatch(match.group(1)):
            return _UNVERIFIABLE_DRIFT
        sha = match.group(1).lower()
        # git exits non-zero when either side does not resolve to a commit (unknown stamp, missing ref). The
        # three-dot form diffs from the merge-base, so a ref that is behind the stamp reports nothing, not the
        # stamp's own newer changes.
        # --no-renames: a rename must report its source as well as its destination (a spec naming the old path
        # is out of date), and it keeps the output independent of the user's diff.renames setting.
        diff = _git_stdout(
            repo_dir,
            "diff",
            "--name-only",
            "--no-renames",
            # Paths relative to repo_dir (the cwd), not the repository root: specs name project-relative paths,
            # which differ when the project is a subdirectory of a larger repo. A no-op at the repo root.
            "--relative",
            "-z",
            f"{sha}^{{commit}}...origin/{base_branch}^{{commit}}",
            "--",
        )
        if diff is None:
            return _UNVERIFIABLE_DRIFT
        changed = {name for name in diff.split("\0") if name}
        named = extract_spec_paths(spec_text)
        exact = {p for p in named if not p.endswith("/")}
        prefixes = tuple(p for p in named if p.endswith("/"))
        touched = (changed & exact) | {c for c in changed if prefixes and c.startswith(prefixes)}
        if not touched:
            return SpecDriftResult(SpecDrift.CURRENT)
        return SpecDriftResult(SpecDrift.DRIFTED, tuple(sorted(touched)))
    except Exception:  # noqa: BLE001 (the drift check is advisory and must never break its caller)
        log.warning("spec.drift_check_failed", exc_info=True)
        return _UNVERIFIABLE_DRIFT


def spec_drift_caveat(result: SpecDriftResult) -> str:
    """Return an operator/developer-facing warning for a ``DRIFTED`` *result*, or "" otherwise."""
    if result.status is not SpecDrift.DRIFTED:
        return ""
    shown = result.files[:_MAX_DRIFT_FILES_SHOWN]
    more = len(result.files) - len(shown)
    listing = ", ".join(shown) + (f" (and {more} more)" if more > 0 else "")
    return (
        "This spec may be out of date with the code: files it names changed on the base branch after the "
        f"spec was written ({listing}). Re-read those files before relying on the spec's file-level details; "
        "a human can re-research the issue from the spec page."
    )


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
