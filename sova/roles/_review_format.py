"""Shared review body formatting.

Single source of truth for review markdown output, severity labels,
and verdict logic. Used by both ReviewerRole (_review_comments.py)
and the /review-pr command (via format_from_json for CLI access).
"""

from __future__ import annotations

import json

_SEVERITY_CRITICAL = 7
_SEVERITY_HIGH = 5
_SEVERITY_MEDIUM = 3


def clamp_severity(severity: int) -> int:
    """Clamp severity to the 1-10 range."""
    return max(1, min(10, severity))


def severity_label(severity: int) -> str:
    """Map a numeric severity (1-10) to a categorical label."""
    clamped = clamp_severity(severity)
    if clamped >= _SEVERITY_CRITICAL:
        return "CRITICAL"
    if clamped >= _SEVERITY_HIGH:
        return "HIGH"
    if clamped >= _SEVERITY_MEDIUM:
        return "MEDIUM"
    return "LOW"


def verdict_from_severities(severities: list[int]) -> str:
    """Determine the review verdict from a list of severity ints.

    Returns ``APPROVE``, ``REVISE``, or ``BLOCK``.
    """
    if not severities:
        return "APPROVE"
    max_sev = max(clamp_severity(s) for s in severities)
    if max_sev >= _SEVERITY_CRITICAL:
        return "BLOCK"
    return "REVISE"


def verdict_from_findings(findings: list[dict]) -> str:
    """Determine the review verdict from a list of finding dicts.

    Each dict must have a ``severity`` key (int). Returns ``APPROVE``,
    ``REVISE``, or ``BLOCK``.
    """
    return verdict_from_severities([f.get("severity", 5) for f in findings])


def _verdict_action(verdict: str) -> str:
    if verdict == "APPROVE":
        return "Approved"
    if verdict == "BLOCK":
        return "Block"
    return "Request changes"


def _verdict_rationale(verdict: str, findings: list[dict]) -> str:
    if verdict == "APPROVE" or not findings:
        return "no issues found"
    top = max(findings, key=lambda f: clamp_severity(f.get("severity", 5)))
    desc = top.get("description", "issue found")
    return desc.rstrip(".!?")


def _format_finding_line(f: dict) -> str:
    """Format a single finding dict as a markdown list entry."""
    sev = clamp_severity(f.get("severity", 5))
    label = severity_label(sev)
    file_path = f.get("file") or "unknown"
    line_num = f.get("line")
    loc = f"`{file_path}:{line_num}`" if line_num is not None else f"`{file_path}`"
    cat = f.get("category", "other")
    desc = f.get("description") or "Issue detected"
    suggestion = f.get("suggestion", "")

    entry = f"- **[{label} {sev}/10]** [{cat}] {loc}: {desc}"
    if suggestion:
        entry += f" Fix: {suggestion}"
    return entry


def format_review_body(
    findings: list[dict],
    summary: str = "",
    positives: list[str] | None = None,
) -> str:
    """Format a complete review body in markdown.

    Args:
        findings: List of finding dicts with keys: file, line, severity,
            category, description, suggestion.
        summary: Overall review summary text.
        positives: Positive observations. Section omitted when empty/None.
    """
    verdict = verdict_from_findings(findings)
    lines = [f"<!-- sova-review: {verdict.lower()} -->", "", f"## Review: {verdict}", ""]

    effective_summary = (summary or "").strip() or "Review of changes."
    lines.extend([effective_summary, ""])

    lines.append("### Findings")
    lines.append("")
    if not findings:
        lines.append("No issues found after thorough review.")
    else:
        count_label = "finding" if len(findings) == 1 else "findings"
        lines.append(f"**{len(findings)} {count_label}** (all to be addressed)")
        lines.append("")

        sorted_findings = sorted(
            findings,
            key=lambda x: clamp_severity(x.get("severity", 5)),
            reverse=True,
        )

        lines.extend(_format_finding_line(f) for f in sorted_findings)

    if positives:
        lines.append("")
        lines.append("### What's Done Well")
        lines.extend(f"- {p}" for p in positives)

    lines.append("")
    lines.append("### Verdict")
    action = _verdict_action(verdict)
    rationale = _verdict_rationale(verdict, findings)
    lines.append(f"**{action}**: {rationale}.")

    return "\n".join(lines)


def format_from_json(json_text: str) -> str:
    """Parse JSON review data and format as markdown review body.

    For CLI use from the /review-pr command::

        python3 -c "import sys; from sova.roles._review_format import format_from_json; \\
            print(format_from_json(sys.stdin.read()))" < /tmp/review.json
    """
    data = json.loads(json_text)
    return format_review_body(
        data.get("findings", []),
        data.get("summary", ""),
        data.get("positives"),
    )
