"""Shared review body formatting.

Single source of truth for review markdown output, severity labels,
and verdict logic. Used by both ReviewerRole (_review_comments.py)
and the /review-pr command (via format_from_json for CLI access).
"""

from __future__ import annotations

import json

from sova.utils.review_markers import SHA_RE

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


def verdict_from_severities(
    severities: list[int],
    *,
    revise_at: int = _SEVERITY_MEDIUM,
    block_at: int = _SEVERITY_CRITICAL,
) -> str:
    """Determine the review verdict from a list of severity ints.

    A finding below ``revise_at`` is advisory and does not affect the verdict.
    Returns ``APPROVE``, ``REVISE``, or ``BLOCK``.
    """
    if not severities:
        return "APPROVE"
    max_sev = max(clamp_severity(s) for s in severities)
    if max_sev >= block_at:
        return "BLOCK"
    if max_sev >= revise_at:
        return "REVISE"
    return "APPROVE"


def verdict_from_findings(
    findings: list[dict],
    *,
    revise_at: int = _SEVERITY_MEDIUM,
    block_at: int = _SEVERITY_CRITICAL,
) -> str:
    """Determine the review verdict from a list of finding dicts.

    Each dict must have a ``severity`` key (int). Returns ``APPROVE``,
    ``REVISE``, or ``BLOCK``.
    """
    return verdict_from_severities([f.get("severity", 5) for f in findings], revise_at=revise_at, block_at=block_at)


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
    sha: str | None = None,
    *,
    revise_at: int = _SEVERITY_MEDIUM,
    block_at: int = _SEVERITY_CRITICAL,
) -> str:
    """Format a complete review body in markdown.

    Args:
        findings: List of finding dicts with keys: file, line, severity,
            category, description, suggestion.
        summary: Overall review summary text.
        positives: Positive observations. Section omitted when empty/None.
        sha: Full SHA of the reviewed PR head commit. When known, embedded in
            the marker so the verdict can be anchored to the reviewed commit.
        revise_at: Severity at or above which a finding blocks the verdict
            and is listed under ``### Findings``. Findings below this are
            advisory: still recorded, listed under
            ``### Advisory (not blocking)`` instead.
        block_at: Severity at or above which the verdict is ``BLOCK`` rather
            than ``REVISE``.
    """
    verdict = verdict_from_findings(findings, revise_at=revise_at, block_at=block_at)
    sha_suffix = f" sha={sha}" if sha else ""
    lines = [f"<!-- sova-review: {verdict.lower()}{sha_suffix} -->", "", f"## Review: {verdict}", ""]

    effective_summary = (summary or "").strip() or "Review of changes."
    lines.extend([effective_summary, ""])

    blocking = [f for f in findings if clamp_severity(f.get("severity", 5)) >= revise_at]
    advisory = [f for f in findings if clamp_severity(f.get("severity", 5)) < revise_at]

    lines.append("### Findings")
    lines.append("")
    if not blocking:
        no_findings_text = "No issues found after thorough review."
        no_blocking_text = "No blocking issues found after thorough review."
        lines.append(no_blocking_text if findings else no_findings_text)
    else:
        count_label = "finding" if len(blocking) == 1 else "findings"
        lines.append(f"**{len(blocking)} {count_label}**")
        lines.append("")

        sorted_blocking = sorted(
            blocking,
            key=lambda x: clamp_severity(x.get("severity", 5)),
            reverse=True,
        )

        lines.extend(_format_finding_line(f) for f in sorted_blocking)

    if advisory:
        lines.append("")
        lines.append("### Advisory (not blocking)")
        lines.append("")
        count_label = "finding" if len(advisory) == 1 else "findings"
        lines.append(f"**{len(advisory)} {count_label}** (recorded for a later fix round, does not block approval)")
        lines.append("")

        sorted_advisory = sorted(
            advisory,
            key=lambda x: clamp_severity(x.get("severity", 5)),
            reverse=True,
        )

        lines.extend(_format_finding_line(f) for f in sorted_advisory)

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


def normalize_sha(value: object) -> str | None:
    """Normalize a review's commit anchor, or None when it is unusable.

    Lowercased so the anchor compares equal to the lowercase head sha GitHub
    reports; a case mismatch would read as a stale verdict. An unknown or
    malformed anchor leaves the marker unanchored (fresh, never stale) rather
    than embedding a value the dashboard cannot parse.
    """
    if isinstance(value, str) and SHA_RE.fullmatch(value):
        return value.lower()
    return None


def format_from_data(
    data: dict,
    *,
    revise_at: int = _SEVERITY_MEDIUM,
    block_at: int = _SEVERITY_CRITICAL,
) -> str:
    """Format an already-parsed review payload as a markdown review body.

    Shared by format_from_json() and by build_review_payload_from_json(), which
    needs the parsed findings anyway to place inline comments, so the body and
    the comments are always built from the same parse of the same data.
    """
    return format_review_body(
        data.get("findings", []),
        data.get("summary", ""),
        data.get("positives"),
        sha=normalize_sha(data.get("sha")),
        revise_at=revise_at,
        block_at=block_at,
    )


def format_from_json(
    json_text: str,
    *,
    revise_at: int = _SEVERITY_MEDIUM,
    block_at: int = _SEVERITY_CRITICAL,
) -> str:
    """Parse JSON review data and format as markdown review body.

    For CLI use from the /review-pr command::

        python3 -c "import sys; from sova.roles._review_format import format_from_json; \\
            print(format_from_json(sys.stdin.read()))" < /tmp/review.json
    """
    return format_from_data(json.loads(json_text), revise_at=revise_at, block_at=block_at)
