"""Review comment formatting, prompt construction, and finding parsing.

Extracted from reviewer.py to separate data/formatting concerns from
orchestration (ReviewerRole). Re-exported by reviewer.py for backward
compatibility.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal

from sova.adapters.base import Task
from sova.utils.logging import get_logger
from sova.utils.markdown import extract_section as _extract_section

log = get_logger(component="role.reviewer")

DIFF_CHUNK_SIZE = 100_000  # ~100KB per chunk

_SPEC_SECTIONS = (
    "Solution",
    "Edge Cases",
    "Design Decisions",
    "Scope Boundaries",
    "Implementation Notes",
    "Review Rationale",
    "Address Review Notes",
)


def _extract_spec_sections(raw_content: str) -> dict[str, str]:
    """Extract review-relevant sections from a spec's raw markdown content."""
    sections: dict[str, str] = {}
    for heading in _SPEC_SECTIONS:
        content = _extract_section(raw_content, heading)
        if content:
            sections[heading] = content
    return sections


@dataclass
class ReviewFinding:
    """A single finding from the code review."""

    file: str
    severity: int
    category: str
    description: str
    suggestion: str = ""
    line: int | None = None


@dataclass
class ReviewResult:
    """Aggregated review output."""

    findings: list[ReviewFinding] = field(default_factory=list)
    summary: str = ""
    total_cost: Decimal = Decimal("0")
    post_failed: bool = False

    @property
    def actionable(self) -> list[ReviewFinding]:
        return [f for f in self.findings if f.category != "protected-path"]


def _format_addressed_findings(findings: list[dict] | None) -> str:
    """Format addressed external findings into a prompt section."""
    if not findings:
        return ""

    # Group by source
    by_source: dict[str, list[dict]] = {}
    for f in findings:
        source = f.get("source", "unknown")
        by_source.setdefault(source, []).append(f)

    lines = [
        "## Already Addressed by Static Tools",
        "The following issues were already detected and addressed by external tools "
        "before this review. Focus your review on complementary dimensions that static "
        "tools cannot catch: logic correctness, architecture, edge cases, concurrency, "
        "and design intent.",
        "",
    ]
    for source, items in sorted(by_source.items()):
        lines.append(f"### {source} ({len(items)} finding{'s' if len(items) != 1 else ''})")
        for item in items:
            severity = item.get("severity", "?")
            tool_id = item.get("tool_id", "")
            file_path = item.get("file_path", "unknown")
            msg = item.get("message", "")
            tool_tag = f" [{tool_id}]" if tool_id else ""
            lines.append(f"- [{severity}]{tool_tag} `{file_path}`: {msg}")
        lines.append("")

    return "\n".join(lines)


def _build_review_prompt(
    task: Task,
    diff: str,
    files: list[str],
    spec_sections: dict[str, str] | None = None,
    addressed_findings: list[dict] | None = None,
) -> str:
    """Build the LLM prompt for code review."""
    file_list = "\n".join(f"- {f}" for f in files)

    has_spec = bool(spec_sections)

    spec_block = ""
    if has_spec:
        parts = [f"### {heading}\n{content}" for heading, content in spec_sections.items()]
        spec_block = "\n\n## Spec Context\n" + "\n\n".join(parts)

    addressed_block = _format_addressed_findings(addressed_findings)

    spec_checklist = (
        "\n9. **Spec alignment** (5-8): implementation deviates from spec intent, "
        "scope creep, missing edge cases from spec, design decisions not followed"
        if has_spec
        else ""
    )
    categories = "bug|security|error-handling|testing|api|performance|design|docs"
    if has_spec:
        categories += "|spec_alignment"

    # When spec sections exist, the spec already encodes the issue's intent in a
    # structured form.  Omit the verbose issue body to save tokens -- the title
    # is enough for identification.
    description_block = f"\n**Description**: {task.body}" if not has_spec and task.body else ""

    return f"""You are a senior software engineer performing a thorough code review. \
Your job is to find real issues -- do NOT rubber-stamp the PR. \
Assume the code has bugs until proven otherwise.

## PR Context
**Issue**: {task.title}{description_block}
{spec_block}
{addressed_block}
## Changed Files
{file_list}

## Diff
```
{diff}
```

## Review Checklist
Examine every changed line against each criterion. Score each finding 1-10 (10 = critical bug, 1 = nitpick).

1. **Bugs** (7-10): logic errors, off-by-one, null/None handling, race conditions, incorrect API usage
2. **Security** (6-10): injection, secrets in code, auth bypass, unsafe deserialization, format string attacks
3. **Error handling** (4-7): uncaught exceptions at system boundaries, silent failures, missing validation
4. **Testing gaps** (3-6): untested error paths, missing edge cases, assertions that don't verify behavior
5. **API contracts** (4-7): wrong parameter types, missing required args, incorrect return types
6. **Performance** (3-6): N+1 queries, unbounded loops, unnecessary allocations, import-time side effects
7. **Design** (3-5): hardcoded values that should be configurable, module-level state, tight coupling
8. **Docs** (2-3): stale comments, misleading docstrings{spec_checklist}

## Critical Rules
- You MUST find at least one issue. No PR is perfect. If you think the code is clean, look harder.
- Focus on REAL issues that would cause bugs, security holes, or maintenance problems.
- Report ALL findings regardless of severity. Low-severity findings will still be addressed.
- For each finding, explain WHY it is a problem and provide a CONCRETE fix.
- Be specific: reference exact file paths and line numbers from the diff.

## Output Format
Return ONLY a JSON object (no markdown fences, no extra text, no preamble):
{{
  "findings": [
    {{
      "file": "path/to/file.py",
      "line": 42,
      "severity": 7,
      "category": "{categories}",
      "description": "Concise description of the issue",
      "suggestion": "Specific fix recommendation"
    }}
  ],
  "summary": "2-3 sentence overall assessment. State the most critical issue first."
}}"""


_MAX_COMPACT_SPEC_CHARS = 300


def _compact_spec_ref(spec_sections: dict[str, str] | None) -> dict[str, str] | None:
    """Return a compact version of spec sections for follow-up chunks.

    Avoids duplicating the full spec in every diff chunk prompt. Keeps section
    headings with truncated content so the LLM knows which spec areas exist.
    """
    if not spec_sections:
        return None
    compact: dict[str, str] = {}
    for heading, content in spec_sections.items():
        if len(content) <= _MAX_COMPACT_SPEC_CHARS:
            compact[heading] = content
        else:
            compact[heading] = content[:_MAX_COMPACT_SPEC_CHARS] + "... (see full spec in chunk 1)"
    return compact


def _safe_severity(value: object, default: int = 5) -> int:
    """Convert a severity value to int safely, returning *default* on failure.

    Handles int, float, numeric strings, None, and non-numeric strings
    (e.g. ``"HIGH"``) without raising.
    """
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        log.warning("safe_severity.non_numeric", value=value, default=default)
        return default


def _extract_json(text: str) -> dict | None:
    """Extract the best JSON object from *text* using ``raw_decode``.

    Scans left-to-right through ``{`` positions.  Returns the first valid
    JSON object that contains a ``"findings"`` key, or the first valid parse
    if none has ``"findings"``.  Returns ``None`` when no valid JSON is found.
    """
    decoder = json.JSONDecoder()
    first_valid: dict | None = None

    pos = 0
    while True:
        idx = text.find("{", pos)
        if idx < 0:
            break
        try:
            obj, end_idx = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            pos = idx + 1
            continue
        if isinstance(obj, dict):
            if "findings" in obj:
                return obj
            if first_valid is None:
                first_valid = obj
        pos = end_idx

    return first_valid


def _parse_findings(text: str) -> tuple[list[ReviewFinding], str]:
    """Parse LLM response into findings. Returns (findings, summary)."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = _extract_json(text)
        if data is None:
            log.warning("parse_findings.failed", text_preview=text[:200])
            return [], "Failed to parse review response"

    findings = []
    for item in data.get("findings", []):
        findings.append(
            ReviewFinding(
                file=item.get("file", "unknown"),
                severity=_safe_severity(item.get("severity", 5)),
                category=item.get("category", "other"),
                description=item.get("description", ""),
                suggestion=item.get("suggestion", ""),
                line=item.get("line"),
            )
        )
    return findings, data.get("summary", "")


def _chunk_diff(diff: str, chunk_size: int = DIFF_CHUNK_SIZE) -> list[str]:
    """Split a large diff into chunks at file boundaries."""
    if len(diff) <= chunk_size:
        return [diff]

    chunks: list[str] = []
    current: list[str] = []
    current_size = 0

    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git ") and current_size >= chunk_size:
            chunks.append("".join(current))
            current = []
            current_size = 0
        current.append(line)
        current_size += len(line)

    if current:
        chunks.append("".join(current))

    return chunks if chunks else [diff]


_SEVERITY_CRITICAL = 7
_SEVERITY_HIGH = 5
_SEVERITY_MEDIUM = 3

_VERDICT_TO_LABEL: dict[str, str] = {
    "APPROVE": "sova:approved",
    "REVISE": "sova:revise",
    "BLOCK": "sova:block",
}


def _sova_verdict_label_name(findings: list[ReviewFinding]) -> str:
    """Return the sova:{verdict} label name for the given findings."""
    return _VERDICT_TO_LABEL[_verdict_label(findings)]


def _severity_label(severity: int) -> str:
    """Map a numeric severity (1-10) to a categorical label."""
    if severity >= _SEVERITY_CRITICAL:
        return "CRITICAL"
    if severity >= _SEVERITY_HIGH:
        return "HIGH"
    if severity >= _SEVERITY_MEDIUM:
        return "MEDIUM"
    return "LOW"


def _verdict_label(findings: list[ReviewFinding]) -> str:
    """Determine the review verdict from findings."""
    if not findings:
        return "APPROVE"
    max_sev = max(f.severity for f in findings)
    if max_sev >= _SEVERITY_CRITICAL:
        return "BLOCK"
    return "REVISE"


def _make_protected_path_finding(matched_files: list[str]) -> ReviewFinding:
    """Create a finding for PR files matching protected path patterns.

    ``matched_files`` must contain at least one entry (the caller guards
    with ``if protected:`` before calling).
    """
    if not matched_files:
        raise ValueError("matched_files must not be empty")
    paths_str = ", ".join(sorted(matched_files))
    return ReviewFinding(
        file=matched_files[0],
        severity=1,
        category="protected-path",
        description=f"PR touches protected path(s): {paths_str}. Human approval required.",
    )


def _format_findings_body(findings: list[ReviewFinding], summary: str) -> str:
    """Build the shared review body used by both review API and comment fallback."""
    verdict = _verdict_label(findings)
    lines = [f"<!-- sova-review: {verdict.lower()} -->", "", f"## Review: {verdict}", ""]

    if not findings:
        if summary:
            lines.extend([summary, ""])
        lines.append("No issues found after thorough review.")
        return "\n".join(lines)

    if summary:
        lines.extend([summary, ""])

    lines.append(f"**{len(findings)} findings** (all to be addressed)")
    lines.append("")

    for f in sorted(findings, key=lambda x: x.severity, reverse=True):
        label = _severity_label(f.severity)
        loc = f"`{f.file}:{f.line}`" if f.line else f"`{f.file}`"
        entry = f"- **[{label}]** [{f.category}] {loc}: {f.description}"
        if f.suggestion:
            entry += f" Fix: {f.suggestion}"
        lines.append(entry)

    return "\n".join(lines)


def _format_findings_comment(findings: list[ReviewFinding], summary: str) -> str:
    """Format findings into a GitHub PR comment (fallback path)."""
    return _format_findings_body(findings, summary)


def _format_inline_comment(finding: ReviewFinding) -> str:
    """Format a single finding as an inline PR review comment."""
    label = _severity_label(finding.severity)
    parts = [f"**[{label}] {finding.category}**: {finding.description}"]
    if finding.suggestion:
        parts.append(f"\n**Suggestion**: {finding.suggestion}")
    return "\n".join(parts)


def _format_review_body(
    findings: list[ReviewFinding],
    summary: str,
) -> str:
    """Format the review body for the PR review API (with inline comments)."""
    return _format_findings_body(findings, summary)


def _build_review_comments(
    findings: list[ReviewFinding],
    diff_lines: dict[str, set[int]],
) -> tuple[list[dict], list[ReviewFinding]]:
    """Split findings into inline comments and body-only findings.

    Returns (inline_comments, body_only_findings).
    """
    inline_comments: list[dict] = []
    body_only: list[ReviewFinding] = []

    for f in findings:
        valid_lines = diff_lines.get(f.file, set())
        if f.line and f.line in valid_lines:
            inline_comments.append(
                {
                    "path": f.file,
                    "line": f.line,
                    "side": "RIGHT",
                    "body": _format_inline_comment(f),
                }
            )
        else:
            body_only.append(f)

    return inline_comments, body_only
