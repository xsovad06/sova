"""Step: Address review -- fix review findings from the Reviewer agent."""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.ipc.handoff import read_handoff, read_handoff_file
from sova.llm.client import invoke_command
from sova.utils.logging import get_logger
from sova.utils.review_markers import (
    INLINE_COMMENT_STUB,
    SOVA_ADDRESSED_MARKER_RE,
    SOVA_VERDICT_MARKER_RE,
)
from sova.utils.shell import run

log = get_logger(component="step.address_review")


def _load_review_findings(project_dir: Path, issue: str = "") -> list[dict]:
    """Load review findings from the reviewer's handoff file."""
    handoff = read_handoff_file(project_dir, issue=issue or None)
    if handoff is None:
        return []
    # "pending_findings" is the canonical key written by ReviewerRole.
    # "findings" is kept as a legacy fallback for older handoff files.
    return handoff.details.get("pending_findings") or handoff.details.get("findings", [])


async def _load_review_findings_from_db(task_run_id: int | None) -> list[dict]:
    """Load review findings from a specific run's handoff in DB."""
    if task_run_id is None:
        return []
    try:
        handoff = await read_handoff(task_run_id)
        if handoff and handoff.pending_findings:
            return handoff.pending_findings
    except (OSError, RuntimeError, SQLAlchemyError):
        log.debug("address_review.db_findings_failed", exc_info=True)
    return []


async def _load_review_findings_by_issue(issue_number: str) -> list[dict]:
    """Load findings from the most recent reviewer run for this issue."""
    issue = issue_number.lstrip("#").strip()
    if not issue:
        return []

    from sqlalchemy import select

    from sova.db.models import TaskRun
    from sova.db.session import get_session

    try:
        async with await get_session() as session:
            stmt = (
                select(TaskRun)
                .where(
                    TaskRun.issue_number == issue,
                    TaskRun.role.in_(["reviewer", "command:review-pr"]),
                    TaskRun.status.in_(["done", "failed", "interrupted", "stopped"]),
                    TaskRun.handoff_json.isnot(None),
                )
                .order_by(TaskRun.started_at.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            run_record = result.scalar_one_or_none()
            if run_record and run_record.handoff_json:
                findings = run_record.handoff_json.get("pending_findings", [])
                if findings:
                    log.info("address_review.findings_from_reviewer", run_id=run_record.id, count=len(findings))
                    return findings
    except (OSError, RuntimeError, SQLAlchemyError):
        log.debug("address_review.issue_findings_failed", exc_info=True)
    return []


# Matches a finding line emitted by sova.roles._review_format._format_finding_line():
#   - **[HIGH 7/10]** [bug] `path/to/file.py:42`: Description text Fix: suggestion
# or, for a file-level finding with no line number:
#   - **[HIGH 7/10]** [bug] `path/to/file.py`: Description text
_FINDING_LINE_RE = re.compile(
    r"^-\s+\*\*\[\w+\s+(?P<severity>\d+)/10\]\*\*\s*"
    r"\[(?P<category>[^\]]*)\]\s*`(?P<location>[^`]*)`:\s*(?P<rest>.*)$"
)
_SECTION_HEADING_RE = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
_FIX_SPLIT_RE = re.compile(r"\s+Fix:\s+")


_LINE_RANGE_RE = re.compile(r"^\d+\s*-\s*\d+$")


def _split_location(location: str) -> tuple[str, int | None]:
    """Split a ``file`` or ``file:line`` location into (file, line).

    A file path can itself contain a colon (e.g. a Windows-style path), so a
    trailing segment is only stripped when it is a line number: all digits, or
    a ``10-15`` range (which a hand-written review may use and which has no
    single line to anchor to, so the file is kept and the line left unknown).
    Anything else leaves the whole string as the file path unchanged.
    """
    file_path, sep, maybe_line = location.rpartition(":")
    if sep and maybe_line.isdigit():
        return file_path, int(maybe_line)
    if sep and _LINE_RANGE_RE.match(maybe_line):
        return file_path, None
    return location, None


def _extract_markdown_sections(body: str) -> dict[str, list[str]]:
    """Split a markdown body into ``### Heading`` -> list of body-text sections.

    A heading can repeat (e.g. a human-authored summary field that embeds its
    own ``### Findings`` heading above the real one): accumulating into a list
    per heading means a duplicate adds findings rather than silently shadowing
    the genuine section.
    """
    headings = list(_SECTION_HEADING_RE.finditer(body))
    sections: dict[str, list[str]] = {}
    for i, m in enumerate(headings):
        start = m.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(body)
        sections.setdefault(m.group(1), []).append(body[start:end])
    return sections


def _parse_finding_lines(section_text: str) -> list[dict]:
    """Parse every ``- **[LABEL N/10]** ...`` entry out of one section's text."""
    findings: list[dict] = []
    for line in section_text.splitlines():
        m = _FINDING_LINE_RE.match(line.strip())
        if not m:
            continue
        file_path, line_num = _split_location(m.group("location"))
        parts = _FIX_SPLIT_RE.split(m.group("rest"), maxsplit=1)
        description = parts[0]
        suggestion = parts[1] if len(parts) > 1 else ""
        findings.append(
            {
                "file": file_path,
                "line": line_num,
                "severity": int(m.group("severity")),
                "category": m.group("category").strip().lower(),
                "description": description.strip(),
                "suggestion": suggestion.strip(),
                "source": "github-review",
            }
        )
    return findings


_LEGACY_SEVERITY_MAP = {"CRITICAL": 10, "HIGH": 9, "MEDIUM": 6, "LOW": 3}

# Matches the pre-markdown review shape still found on older PRs and in
# hand-written human reviews that follow SOVA's original documented layout:
# a bracketed severity, a category, a dash-dash separator, a description,
# then optional "Location:", "Problem:", and "Suggestion:" fields.
_LEGACY_HEADER_RE = re.compile(r"\*{0,2}\[(\w+)\]\s*([\w][\w ]*?)\s*--\s*(.*?)\*{0,2}\s*$", re.MULTILINE)
_LEGACY_BLOCK_SPLIT_RE = re.compile(r"\n(?=\*{0,2}\[(?:CRITICAL|HIGH|MEDIUM|LOW)\])")
_LEGACY_LOCATION_RE = re.compile(r"Location:\s*(\S+?)(?:\n|$)")
_LEGACY_PROBLEM_RE = re.compile(r"Problem:\s*(.*?)(?:\nSuggestion:|\Z)", re.DOTALL)
_LEGACY_SUGGESTION_RE = re.compile(r"Suggestion:\s*(.*?)(?:\n\[|```|\Z)", re.DOTALL)


def _parse_legacy_review_body(body: str) -> list[dict]:
    """Parse the pre-markdown finding shape out of a review body.

    SOVA no longer emits this layout, but review bodies posted before the
    markdown format landed are still live on open PRs, and a human reviewer
    may write it by hand. Parsing it is strictly better than degrading the
    whole body to one opaque finding.
    """
    findings: list[dict] = []
    for block in _LEGACY_BLOCK_SPLIT_RE.split(body):
        m = _LEGACY_HEADER_RE.match(block.strip())
        if not m:
            continue

        file_path = ""
        line_num = None
        loc_m = _LEGACY_LOCATION_RE.search(block)
        if loc_m:
            file_path, line_num = _split_location(loc_m.group(1))

        problem_m = _LEGACY_PROBLEM_RE.search(block)
        suggestion_m = _LEGACY_SUGGESTION_RE.search(block)
        findings.append(
            {
                "file": file_path,
                "line": line_num,
                "severity": _LEGACY_SEVERITY_MAP.get(m.group(1).upper(), 5),
                "category": m.group(2).lower(),
                "description": problem_m.group(1).strip() if problem_m else m.group(3),
                "suggestion": suggestion_m.group(1).strip() if suggestion_m else "",
                "source": "github-review",
            }
        )
    return findings


def _parse_review_body(body: str) -> list[dict]:
    """Parse structured findings from a single review body.

    Expects the ``### Findings`` / ``### Advisory (not blocking)`` markdown
    format emitted by ``sova.roles._review_format.format_review_body()``
    (used for both SOVA's own re-review and the ``/review-pr`` command).

    When no such section is present, the older pre-markdown layout is tried
    next (see ``_parse_legacy_review_body``), and only a body that matches
    neither degrades to a single finding carrying the whole text (e.g. a
    free-text human review comment).

    A section that parses to zero findings is trusted as genuinely empty
    (e.g. "No issues found after thorough review.") only when the body
    carries SOVA's own ``<!-- sova-review: -->`` marker, proving the
    formatter wrote it. A human who happens to write a ``### Findings``
    heading above prose bullets still falls through to the whole-body
    fallback rather than having their entire review silently dropped.
    """
    sections = _extract_markdown_sections(body)
    structured_headings = [h for h in sections if h == "Findings" or h.startswith("Advisory")]

    findings: list[dict] = []
    for heading in structured_headings:
        for section_text in sections[heading]:
            findings.extend(_parse_finding_lines(section_text))
    if findings:
        return findings

    if structured_headings and SOVA_VERDICT_MARKER_RE.search(body):
        return []

    legacy = _parse_legacy_review_body(body)
    if legacy:
        return legacy
    if len(body) > 50:
        return [
            {
                "file": "",
                "line": None,
                "severity": 7,
                "category": "review",
                "description": body[:3000],
                "suggestion": "",
                "source": "github-review",
            }
        ]
    return []


# Matches the inline-comment shape sova.roles._review_comments._format_inline_comment()
# emits: "**[HIGH] bug**: description" then an optional "**Suggestion**: ..." block.
_INLINE_COMMENT_HEAD_RE = re.compile(r"^\*\*\[\w+\]\s*[^*]*\*\*:\s*", re.DOTALL)
_INLINE_SUGGESTION_RE = re.compile(r"\n+\*\*Suggestion\*\*:\s*", re.DOTALL)


def _parse_inline_comment_body(text: str) -> tuple[str, str]:
    """Split an inline review comment into (description, suggestion).

    Understands SOVA's own inline shape and degrades to "all description" for
    a hand-written comment, so a human's inline note is still usable.
    """
    parts = _INLINE_SUGGESTION_RE.split(text, maxsplit=1)
    description = _INLINE_COMMENT_HEAD_RE.sub("", parts[0].strip(), count=1).strip()
    suggestion = parts[1].strip() if len(parts) > 1 else ""
    return description, suggestion


def _parse_paginated_gh_json(stdout: str) -> list:
    """Parse ``gh api --paginate`` stdout into a single flat list.

    Per ``gh api --help``, each page is a separate JSON array, concatenated
    with no separator (``[...][...]``), which ``json.loads`` rejects outright
    once there is more than one page (default page size 30). ``JSONDecoder.raw_decode``
    reads one JSON value at a time and reports where it stopped, so looping it
    over the remaining text recovers every page without needing ``--slurp``
    (not available on all installed `gh` versions).
    """
    import json as _json

    decoder = _json.JSONDecoder()
    pages: list = []
    text = stdout.strip()
    idx = 0
    length = len(text)
    while idx < length:
        try:
            obj, end = decoder.raw_decode(text, idx)
        except _json.JSONDecodeError:
            log.warning("address_review.paginated_json_truncated", offset=idx, exc_info=True)
            break
        pages.append(obj)
        idx = end
        while idx < length and text[idx].isspace():
            idx += 1

    flat: list = []
    for page in pages:
        if isinstance(page, list):
            flat.extend(page)
        else:
            flat.append(page)
    return flat


async def _load_inline_review_comments(ctx: ExecutionContext) -> dict[tuple[str, int], list[tuple[str, str, bool]]]:
    """Fetch non-bot inline PR review comments, keyed by ``(path, line)``.

    A review body collapses any finding that already has its own inline
    comment down to ``INLINE_COMMENT_STUB``, so the body alone no longer
    carries that finding's text. This is the other half of that trade: the
    full text is read back out of the inline comments it was moved into.

    Each location maps to a *list* of matches rather than the last one seen:
    two distinct findings can land on the same ``(path, line)``, and a human's
    own top-level comment on that line is a third candidate. Picking one
    silently would risk attributing the wrong text to a finding. The third
    tuple element flags whether the raw body matches SOVA's own inline-comment
    shape (``_INLINE_COMMENT_HEAD_RE``), so a same-location human note never
    outranks SOVA's own comment when both exist.
    """
    try:
        from sova.utils.gh import resolve_gh_env

        env = await resolve_gh_env(ctx.config.github_user)
        result = await run(
            "gh",
            "api",
            f"repos/{ctx.repo}/pulls/{ctx.pr_number}/comments",
            "--paginate",
            cwd=ctx.working_dir,
            env=env,
        )
        if not result.success or not result.stdout.strip():
            return {}

        raw = _parse_paginated_gh_json(result.stdout)

        by_location: dict[tuple[str, int], list[tuple[str, str, bool]]] = {}
        for c in raw:
            if not isinstance(c, dict):
                continue
            if (c.get("user") or {}).get("type", "") == "Bot" or c.get("in_reply_to_id") is not None:
                continue
            path = c.get("path") or ""
            # "line" is null once the comment's line falls out of the current
            # diff; "original_line" still pins it to the commit it was made on.
            line = c.get("line")
            if line is None:
                line = c.get("original_line")
            body = (c.get("body") or "").strip()
            if not path or not isinstance(line, int) or isinstance(line, bool) or not body:
                continue
            is_sova_shaped = bool(_INLINE_COMMENT_HEAD_RE.match(body))
            description, suggestion = _parse_inline_comment_body(body)
            by_location.setdefault((path, line), []).append((description, suggestion, is_sova_shaped))
        return by_location
    except Exception:  # noqa: BLE001 (best-effort enrichment; the body text still stands in)
        log.warning("address_review.inline_comment_fetch_failed", exc_info=True)
        return {}


async def _hydrate_collapsed_findings(ctx: ExecutionContext, findings: list[dict]) -> None:
    """Replace every ``INLINE_COMMENT_STUB`` description with its inline comment text.

    Mutates *findings* in place. A stub with no matching inline comment (the
    comment was deleted, or its line no longer resolves) keeps the stub: it at
    least tells the addressing agent where to look, which is strictly better
    than an empty description.
    """
    stubs = [f for f in findings if f.get("description") == INLINE_COMMENT_STUB]
    if not stubs:
        return

    inline = await _load_inline_review_comments(ctx)
    if not inline:
        log.warning("address_review.collapsed_findings_unhydrated", count=len(stubs))
        return

    hydrated = 0
    for f in stubs:
        matches = inline.get((f.get("file") or "", f.get("line")))
        if not matches:
            continue
        # Prefer comments shaped like SOVA's own inline comment: a same-location
        # human note (which never matches the shape) must not outrank it.
        sova_shaped = [m for m in matches if m[2]]
        candidates = sova_shaped or matches
        if len(candidates) > 1:
            # Ambiguous: more than one comment landed on this exact (file, line),
            # so there is no safe way to tell which one belongs to this finding.
            # Keep the stub rather than risk attributing the wrong text to it.
            log.warning(
                "address_review.ambiguous_inline_location",
                file=f.get("file"),
                line=f.get("line"),
                candidates=len(candidates),
            )
            continue
        description, suggestion, _ = candidates[0]
        if not description:
            continue
        f["description"] = description
        if suggestion:
            f["suggestion"] = suggestion
        hydrated += 1
    log.info("address_review.hydrated_collapsed_findings", hydrated=hydrated, total=len(stubs))


async def _load_findings_from_github_reviews(ctx: ExecutionContext) -> list[dict]:
    """Fetch review findings from GitHub PR review bodies.

    Calls ``gh api`` directly (bypassing the adapter) so this works for
    both GitHub-Issues and JIRA task sources -- PRs always live on GitHub.
    """
    if not ctx.pr_number:
        return []
    try:
        from sova.utils.gh import resolve_gh_env

        env = await resolve_gh_env(ctx.config.github_user)
        result = await run(
            "gh",
            "api",
            f"repos/{ctx.repo}/pulls/{ctx.pr_number}/reviews",
            "--paginate",
            cwd=ctx.working_dir,
            env=env,
        )
        if not result.success or not result.stdout.strip():
            return []

        raw_reviews = _parse_paginated_gh_json(result.stdout)

        findings: list[dict] = []
        for r in raw_reviews:
            if not isinstance(r, dict):
                continue
            state = r.get("state", "")
            body = r.get("body", "") or ""
            is_bot = (r.get("user") or {}).get("type", "") == "Bot"
            if state == "DISMISSED" or is_bot or not body.strip():
                continue
            # An address cycle's own summary is posted as a COMMENT review; it
            # reports fixes, it does not request any, so it is never a finding.
            if SOVA_ADDRESSED_MARKER_RE.search(body):
                continue
            findings.extend(_parse_review_body(body))

        # Findings whose body entry was collapsed to a stub live in the inline
        # comments instead; pull their real text back in before returning.
        await _hydrate_collapsed_findings(ctx, findings)

        if findings:
            log.info("address_review.github_review_findings", count=len(findings))
        return findings
    except Exception:  # noqa: BLE001 (one of four finding sources; failure falls through to the others)
        log.warning("address_review.github_review_fetch_failed", exc_info=True)
        return []


async def _load_coderabbit_findings(ctx: ExecutionContext) -> tuple[list[dict], list[str]]:
    """Fetch unresolved CodeRabbit findings from the PR.

    Returns (findings_as_dicts, thread_ids).
    """
    if not ctx.pr_number:
        return [], []
    try:
        from sova.adapters.external_reviews import _fetch_coderabbit_threads

        cr_result = await _fetch_coderabbit_threads(
            ctx.repo,
            ctx.pr_number,
            github_user=ctx.config.github_user,
        )
        findings = [
            {
                "file": f.file_path,
                "line": f.line,
                "severity": 6,
                "category": "external-review",
                "description": f.message,
                "suggestion": "",
                "source": "coderabbit",
            }
            for f in cr_result.findings
        ]
        if findings:
            log.info("address_review.coderabbit_findings", count=len(findings))
        return findings, cr_result.thread_ids
    except Exception:  # noqa: BLE001 (one of four finding sources; failure falls through to the others)
        log.warning("address_review.coderabbit_fetch_failed", exc_info=True)
        return [], []


def _load_spec_for_context(ctx: ExecutionContext) -> str:
    """Load spec decision context for the address-review prompt."""
    try:
        from sova.core.steps._spec_helpers import REVIEW_CONTEXT_SECTIONS, read_spec_sections

        return read_spec_sections(ctx.issue_number, ctx.project_dir, REVIEW_CONTEXT_SECTIONS)
    except (OSError, ValueError):
        log.debug("address_review.spec_context_failed", exc_info=True)
        return ""


def _format_findings_prompt(
    findings: list[dict],
    *,
    spec_context: str = "",
    pending_docs_path: Path | None = None,
) -> str:
    """Format findings into a prompt for the LLM to address.

    ``pending_docs_path`` points at a queue of documentation and knowledge that
    a previous integrate run found missing and deliberately did not push. It is
    drained here, on a branch that is about to be pushed anyway, because a
    documentation push onto an otherwise-ready PR costs a full CI cycle and
    delays the merge by the length of the suite.
    """
    lines = []
    if spec_context:
        lines.extend(
            [
                "## Decision Context (from spec)",
                "Use this context to understand WHY previous agents made specific choices.",
                spec_context,
                "",
            ]
        )
    if findings:
        lines.extend(
            [
                "Address ALL of the following code review findings. For each finding:",
                "- DEFAULT: Fix the issue in the code.",
                "- EXCEPTION: If a finding is a false positive, not applicable in context,",
                "  or requires a human decision, state the reason instead of fixing.",
                "  Do NOT skip findings without justification.\n",
            ]
        )
        for i, f in enumerate(findings, 1):
            loc = f.get("file", "unknown")
            if f.get("line"):
                loc += f":{f['line']}"
            source_tag = f" [from {f['source']}]" if f.get("source") else ""
            lines.append(f"{i}. [{f.get('severity', '?')}/10] [{f.get('category', 'other')}] `{loc}`{source_tag}")
            lines.append(f"   {f.get('description', '')}")
            if f.get("suggestion"):
                lines.append(f"   Fix: {f['suggestion']}")
            lines.append("")
        lines.append("After fixing all issues, make sure all tests still pass.")
        lines.append("")
        lines.extend(
            [
                "Then fold in any documentation work, so it rides this branch's existing",
                "push rather than forcing a separate CI cycle at integration time:",
                "- Update any project documentation the changes here made stale.",
            ]
        )
    else:
        # No review findings, but a pending-docs queue is non-empty (the only
        # reason this function is called with an empty findings list). Fold in
        # the queue on its own, so it still rides whatever push comes next
        # instead of sitting deferred indefinitely.
        lines.extend(
            [
                "There are no code review findings to address. Fold in the",
                "following documentation work instead, so it rides this branch's",
                "existing push rather than forcing a separate CI cycle later:",
            ]
        )
    if pending_docs_path is not None:
        lines.extend(
            [
                f"- Apply every entry in {pending_docs_path} to its destination file,",
                "  then delete that queue file. It holds knowledge a previous run",
                "  deferred rather than pushing.",
            ]
        )
    lines.extend(
        [
            "",
            "IMPORTANT: Do NOT commit your changes. Fix the code, run tests, then stop.",
            "Leave all changes staged or unstaged. A commit reorganization step runs",
            "immediately after this to fold your fixes cleanly into the existing commits.",
        ]
    )
    return "\n".join(lines)


class AddressReviewStep(BaseStep):
    name = "address_review"
    TASK_TYPE = "address_review"

    def __init__(self) -> None:
        super().__init__()
        self._head_before_llm: str | None = None
        self._had_findings: bool = False

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.address_review", pr=ctx.pr_number)

        # Capture HEAD before LLM invocation for gate check
        head_result = await run("git", "rev-parse", "HEAD", cwd=ctx.working_dir)
        if head_result.success:
            self._head_before_llm = head_result.stdout.strip()

        # Load findings: file -> resumed run -> most recent reviewer for this issue
        findings = _load_review_findings(ctx.project_dir, issue=ctx.issue_number)
        if not findings:
            findings = await _load_review_findings_from_db(ctx.resume_run_id)
        if not findings:
            findings = await _load_review_findings_by_issue(ctx.issue_number)

        # Fetch findings from GitHub PR review bodies (covers reviews not
        # posted via Sova's own handoff mechanism).
        if not findings:
            findings = await _load_findings_from_github_reviews(ctx)

        # Also fetch CodeRabbit findings from the PR
        cr_findings, _thread_ids = await _load_coderabbit_findings(ctx)
        if cr_findings:
            findings.extend(cr_findings)

        # A pending-docs queue is checked even with zero findings: /integrate-pr
        # defers documentation here specifically so it rides the next push, and
        # a clean review (no findings) must not leave it stranded indefinitely.
        pending_docs = ctx.project_dir / ".claude" / "agent-control" / "pending-docs.md"
        try:
            has_pending_docs = pending_docs.exists() and pending_docs.read_text().strip() != ""
        except (OSError, UnicodeDecodeError):
            # An unreadable queue file must not crash the step; fall back to
            # the pre-existing safe behavior of treating it as absent.
            log.warning("step.address_review.pending_docs_read_failed", exc_info=True)
            has_pending_docs = False

        if not findings and not has_pending_docs:
            log.info("step.address_review.no_findings")
            return StepResult(success=True, summary="No review findings to address")

        log.info(
            "step.address_review.findings_loaded",
            count=len(findings),
            has_pending_docs=has_pending_docs,
        )

        spec_context = _load_spec_for_context(ctx)
        if spec_context:
            log.info(
                "step.address_review.spec_compression",
                spec_context_chars=len(spec_context),
                findings_count=len(findings),
            )
        prompt = _format_findings_prompt(
            findings,
            spec_context=spec_context,
            pending_docs_path=pending_docs if has_pending_docs else None,
        )
        try:
            result = await invoke_command(
                prompt,
                model=ctx.resolved_model or ctx.config.agent.model,
                fallback_model=ctx.get_cli_fallback_model(),
                task_type=ctx.routing_task_type(self.TASK_TYPE),
                cwd=ctx.working_dir,
                max_budget_usd=ctx.config.agent.max_budget - ctx.cost_usd,
                timeout=ctx.config.agent.step_timeout,
            )
            ctx.add_usage(result)
            self._had_findings = bool(findings)
            # Carried to ResolveExternalReviewsStep, which posts the address
            # summary on the PR once the fixes are pushed and CI is green.
            ctx.addressed_review_findings = list(findings)
            if findings:
                summary = f"Addressed {len(findings)} review findings"
            else:
                summary = "No review findings; drained pending documentation queue"
            return StepResult(
                success=True,
                summary=summary,
                cost_usd=result.cost_usd,
            )
        except RuntimeError as exc:
            return StepResult(success=False, summary="Failed to address review findings", error=str(exc))

    async def _clear_stale_verdict_label(self, ctx: ExecutionContext) -> None:
        """Remove a stale sova:revise/sova:block label after findings are addressed.

        Non-fatal: a label API failure must not fail the step. Never writes a
        replacement label; only ReviewerRole._write_verdict_label() may assert
        approval. Imported lazily to avoid a circular import (sova.roles imports
        sova.core.steps at package init time).
        """
        issue = ctx.issue_number
        if not issue or ctx.adapter is None:
            return
        from sova.roles.reviewer import _VERDICT_TO_LABEL

        for label in (_VERDICT_TO_LABEL["REVISE"], _VERDICT_TO_LABEL["BLOCK"]):
            try:
                await ctx.adapter.remove_label(issue, label)
            except Exception:  # noqa: BLE001 (adapter raises AdapterError/ValueError too; stay fail-open)
                log.warning("address_review.verdict_label_clear_failed", issue=issue, label=label, exc_info=True)

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Gate: the LLM must have produced new changes, commits, or confirmed prior fixes.

        Three passing conditions:
        1. Uncommitted changes exist (LLM modified files)
        2. HEAD moved (LLM committed directly)
        3. Branch is already ahead of base (findings were fixed in prior runs)
        """
        diff_result = await run("git", "diff", "--stat", "HEAD", cwd=ctx.working_dir)
        staged = await run("git", "diff", "--cached", "--stat", cwd=ctx.working_dir)
        has_uncommitted = bool(
            (diff_result.success and diff_result.stdout.strip()) or (staged.success and staged.stdout.strip())
        )

        head_result = await run("git", "rev-parse", "HEAD", cwd=ctx.working_dir)
        head_after = head_result.stdout.strip() if head_result.success else ""
        head_moved = self._head_before_llm is not None and head_after != self._head_before_llm

        if has_uncommitted or head_moved:
            if self._had_findings:
                await self._clear_stale_verdict_label(ctx)
            return GateCheckResult(passed=True)

        log_result = await run("git", "log", f"{ctx.base_branch}..HEAD", "--oneline", cwd=ctx.working_dir)
        has_prior_commits = bool(log_result.success and log_result.stdout.strip())
        if has_prior_commits:
            # NOT treated as evidence for label clearing: on the address-review
            # pipeline the branch is a PR branch, so base..HEAD is populated by
            # the feature's own pre-existing commits before this run ever
            # starts. That makes this condition trivially true regardless of
            # whether this specific cycle fixed anything, so it cannot be used
            # to justify clearing a stale sova:revise/sova:block label.
            log.info("step.address_review.findings_already_fixed")
            return GateCheckResult(passed=True)

        return GateCheckResult(passed=False, reason="No changes after addressing review findings")

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        return self.name in ctx.completed_steps
