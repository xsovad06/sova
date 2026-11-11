"""Machine-readable markers SOVA embeds in GitHub pull request reviews.

Leaf module (no intra-project imports) so the pipeline steps under
``sova/core/`` and the dashboard's verdict resolver can share one definition
without pulling ``sova.roles`` into an import cycle.

Two markers exist:

* ``<!-- sova-review: {verdict} sha={sha} -->`` opens every SOVA review body
  (formatted by ``sova.roles._review_format``). The dashboard reads the
  verdict and the reviewed commit from it.
* ``<!-- sova-addressed: sha={sha} -->`` opens the summary an address cycle
  posts once it has pushed its fixes. It is posted as a COMMENT-state PR
  review, not an issue comment, so it arrives in the same review history
  (with a server timestamp) that the verdict scan already reads: a review body
  newer than the verdict carrying this marker means the verdict has been
  addressed. Both the autonomous address-review pipeline
  (``ResolveReviewThreadsStep``) and the ``/address-pr`` command emit it, so
  the two address paths are tracked the same way.
"""

from __future__ import annotations

import re

SOVA_ADDRESSED_MARKER_RE = re.compile(
    r"<!--\s*sova-addressed(?:\s*:\s*sha=([0-9a-f]{7,40}))?\s*-->",
    re.IGNORECASE,
)

SOVA_VERDICT_MARKER_RE = re.compile(
    r"<!--\s*sova-review:\s*(approve|revise|block)(?:\s+sha=([0-9a-f]{7,40}))?\s*-->",
    re.IGNORECASE,
)

# A git object id: 7 to 40 hex digits. Shared with sova.roles._review_format so
# both markers accept exactly the same anchor.
SHA_RE = re.compile(r"[0-9a-f]{7,40}", re.IGNORECASE)

# Placeholder a review body carries instead of a finding's full text when that
# finding already has its own inline PR review comment. Lives here, in the leaf
# module, because both sides of the boundary need the exact same string:
# sova.roles._review_format writes it, and sova.core.steps.address_review
# recognizes it as "the real text is in the inline comment, go fetch it".
INLINE_COMMENT_STUB = "see inline comment above."

# Category of the synthetic finding that caps the verdict when part of the diff was never reviewed. It is a
# result-level condition, not an LLM finding: never an inline comment, an actionable finding, or a finding the
# address-review step tries to fix.
INCOMPLETE_REVIEW_CATEGORY = "incomplete-review"

# Second marker a review body carries, right after ``sova-review``, when its only blocking finding is that
# incomplete-review cap. The verdict stays ``revise`` (never ``approve``), but the verdict resolver reads this
# marker to route the PR to a manual re-review instead of an address-review cycle, which cannot fix an
# unreviewed chunk. A separate marker rather than a token inside ``sova-review``, so the verdict marker's shape
# (and every reader of it) is unchanged.
SOVA_INCOMPLETE_REVIEW_MARKER = "<!-- sova-review-incomplete -->"
SOVA_INCOMPLETE_REVIEW_MARKER_RE = re.compile(r"<!--\s*sova-review-incomplete\s*-->", re.IGNORECASE)

_FINDING_TEXT_MAX = 140


def parse_verdict_marker(body: str) -> tuple[str, str | None] | None:
    """Read (verdict, reviewed sha) from a ``sova-review`` marker, or None if absent.

    The marker is the sole source of a SOVA verdict: a review body without one
    (a human review, or a pre-marker SOVA review) is not recognizable and must
    be skipped by the caller rather than guessed at.
    """
    m = SOVA_VERDICT_MARKER_RE.search(body)
    if not m:
        return None
    sha = m.group(2)
    return m.group(1).lower(), (sha.lower() if sha else None)


def addressed_marker(sha: str | None = None) -> str:
    """Return the ``sova-addressed`` marker, anchored to ``sha`` when one is known."""
    if sha and SHA_RE.fullmatch(sha):
        return f"<!-- sova-addressed: sha={sha.lower()} -->"
    return "<!-- sova-addressed -->"


def _cell(text: str) -> str:
    """Make ``text`` safe for a single markdown table cell."""
    flat = " ".join(str(text).split())
    if len(flat) > _FINDING_TEXT_MAX:
        flat = flat[: _FINDING_TEXT_MAX - 3].rstrip() + "..."
    return flat.replace("|", "\\|")


def _finding_cell(finding: dict) -> str:
    location = str(finding.get("file") or "").strip()
    line = finding.get("line")
    if location and line:
        location = f"{location}:{line}"
    description = _cell(finding.get("description") or "")
    severity = finding.get("severity")
    prefix = f"[{severity}/10] " if isinstance(severity, int) and not isinstance(severity, bool) else ""
    if location:
        return f"{prefix}`{_cell(location)}`: {description}"
    return f"{prefix}{description}"


def format_address_summary(
    findings: list[dict], *, head_sha: str | None, round_no: int = 1, pushed: bool = True
) -> str:
    """Format the summary body an address cycle posts after pushing its fixes.

    Mirrors the ``## Address Review: Round N`` table the ``/address-pr``
    command writes by hand, with the marker on the first line so the dashboard
    can find it in the PR's review list.

    ``pushed=False`` renders a cycle whose fixes did not reach the branch: the marker still opens the body
    (it is what counts the cycle against the address budget) but no finding is listed as addressed.
    """
    short = head_sha[:7] if head_sha else None
    action = f"Addressed in {short}." if short else "Addressed."
    lines = [addressed_marker(head_sha), "", f"## Address Review: Round {round_no}", ""]
    if not pushed:
        lines.append("No changes were pushed in this cycle; no finding was addressed and no thread was resolved.")
        return "\n".join(lines)
    if not findings:
        lines.append("No review findings were pending; the push carried deferred documentation only.")
        return "\n".join(lines)
    lines.extend(["| # | Finding | Action |", "|---|---------|--------|"])
    lines.extend(f"| {i} | {_finding_cell(f)} | {action} |" for i, f in enumerate(findings, start=1))
    return "\n".join(lines)
