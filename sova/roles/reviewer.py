"""Reviewer role -- review PRs and provide feedback.

Reads IN_REVIEW issues with linked PRs, reviews the code changes
via LLM, and posts scored review findings on the PR. Writes a handoff
with actionable findings for the Developer to address.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.exc import SQLAlchemyError

from sova.adapters.base import Task, TaskState
from sova.core.context import ExecutionContext
from sova.dashboard.services.spec_service import find_spec_file
from sova.db.models import TaskRun
from sova.db.session import get_session
from sova.git.diff import parse_diff_lines
from sova.git.operations import find_pr_for_issue, get_pr_branch, get_pr_diff, get_pr_files
from sova.ipc.handoff import (
    AgentHandoff,
    DashboardHandoff,
    HandoffAction,
    read_handoff_file,
    write_handoff,
    write_handoff_file,
)
from sova.llm.client import invoke
from sova.roles.base import AgentRole, RoleResult, TaskAssessment
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

    @property
    def actionable(self) -> list[ReviewFinding]:
        return list(self.findings)


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
Return ONLY a JSON object (no markdown fences, no extra text):
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


def _format_findings_comment(findings: list[ReviewFinding], summary: str, pr_number: int) -> str:
    """Format findings into a GitHub PR comment matching /review-full style."""
    lines = [f"## Code Review for PR #{pr_number}", ""]

    actionable = list(findings)

    if not findings:
        if summary:
            lines.extend([summary, ""])
        lines.append("No issues found after thorough review.")
        lines.extend(["", "---", "**Assessment**: LGTM -- no issues found, ready to merge"])
        return "\n".join(lines)

    # Summary block
    if summary:
        lines.extend([summary, ""])

    lines.append(f"**{len(findings)} findings** (all to be addressed)")
    lines.append("")

    # Scored findings table
    lines.append("| Sev | Category | File | Finding |")
    lines.append("|-----|----------|------|---------|")
    for f in sorted(findings, key=lambda x: x.severity, reverse=True):
        loc = f"`{f.file}:{f.line}`" if f.line else f"`{f.file}`"
        lines.append(f"| {f.severity}/10 | {f.category} | {loc} | {f.description} |")

    # Detailed actionable findings with fix suggestions
    if actionable:
        lines.extend(["", "### Findings requiring action", ""])
        for i, f in enumerate(sorted(actionable, key=lambda x: x.severity, reverse=True), 1):
            loc = f"{f.file}:{f.line}" if f.line else f.file
            lines.append(f"**{i}. [{f.severity}/10] [{f.category}] `{loc}`**")
            lines.append("")
            lines.append(f"  {f.description}")
            if f.suggestion:
                lines.append("")
                lines.append(f"  **Fix**: {f.suggestion}")
            lines.append("")

    # Assessment
    max_sev = max(f.severity for f in findings) if findings else 0
    if max_sev >= 7:
        assessment = "BLOCK -- critical issues must be fixed before merge"
    elif actionable:
        assessment = "REVISE -- actionable findings should be addressed"
    else:
        assessment = "LGTM -- only minor observations, ready to merge"
    lines.extend(["---", f"**Assessment**: {assessment}"])

    return "\n".join(lines)


def _format_inline_comment(finding: ReviewFinding) -> str:
    """Format a single finding as an inline PR review comment."""
    parts = [f"**[{finding.severity}/10] {finding.category}**: {finding.description}"]
    if finding.suggestion:
        parts.append(f"\n**Suggestion**: {finding.suggestion}")
    return "\n".join(parts)


def _format_review_body(
    findings: list[ReviewFinding],
    summary: str,
    pr_number: int,
    body_only: list[ReviewFinding],
) -> str:
    """Format the review body with summary table and any non-inline findings."""
    lines = [f"## Code Review for PR #{pr_number}", ""]

    if not findings:
        if summary:
            lines.extend([summary, ""])
        lines.append("No issues found after thorough review.")
        lines.extend(["", "---", "**Assessment**: LGTM -- no issues found, ready to merge"])
        return "\n".join(lines)

    if summary:
        lines.extend([summary, ""])

    lines.append(f"**{len(findings)} findings** ({len(findings) - len(body_only)} inline, {len(body_only)} in summary)")
    lines.append("")

    lines.append("| Sev | Category | File | Finding |")
    lines.append("|-----|----------|------|---------|")
    for f in sorted(findings, key=lambda x: x.severity, reverse=True):
        loc = f"`{f.file}:{f.line}`" if f.line else f"`{f.file}`"
        lines.append(f"| {f.severity}/10 | {f.category} | {loc} | {f.description} |")

    if body_only:
        lines.extend(["", "### Findings not on changed lines", ""])
        for i, f in enumerate(sorted(body_only, key=lambda x: x.severity, reverse=True), 1):
            loc = f"{f.file}:{f.line}" if f.line else f.file
            lines.append(f"**{i}. [{f.severity}/10] [{f.category}] `{loc}`**")
            lines.append("")
            lines.append(f"  {f.description}")
            if f.suggestion:
                lines.append("")
                lines.append(f"  **Fix**: {f.suggestion}")
            lines.append("")

    max_sev = max(f.severity for f in findings)
    if max_sev >= 7:
        assessment = "BLOCK -- critical issues must be fixed before merge"
    else:
        assessment = "REVISE -- actionable findings should be addressed"
    lines.extend(["---", f"**Assessment**: {assessment}"])

    return "\n".join(lines)


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


class ReviewerRole(AgentRole):
    name = "reviewer"
    description = "Review PRs and provide feedback"
    allowed_input_states = frozenset({TaskState.IN_REVIEW})
    output_state = TaskState.IN_REVIEW

    async def assess_task(self, task: Task) -> TaskAssessment:
        return TaskAssessment(
            suitability="ready",
            confidence=0.7,
            reasoning="Task has a linked PR ready for review.",
            estimated_complexity="moderate",
            suggested_role="reviewer",
        )

    async def execute(self, ctx: ExecutionContext) -> RoleResult:
        try:
            return await self._execute(ctx)
        finally:
            # Clear the "agent" sentinel on ALL exit paths (success, failure,
            # exception) so the DB record never shows a perpetual "agent" step.
            if ctx.task_run_id:
                await self._clear_current_step(ctx)

    async def _execute(self, ctx: ExecutionContext) -> RoleResult:
        task = await ctx.adapter.get_task(ctx.issue_number)

        if not self.validate_preconditions(task, force=ctx.force):
            return RoleResult(
                success=False,
                summary=f"Issue #{ctx.issue_number} not in valid state for review",
                error=f"Precondition failed: issue is in {task.state}, "
                f"expected one of {', '.join(self.allowed_input_states)}",
            )

        if error_msg := await self._discover_pr(ctx):
            return RoleResult(success=False, summary=error_msg, error=error_msg)

        log.info("reviewer.start", issue=ctx.issue_number, pr=ctx.pr_number, branch=ctx.branch_name)

        # Fetch PR diff and file list
        try:
            diff = await get_pr_diff(ctx.pr_number, repo=ctx.repo, github_user=ctx.config.github_user)
            files = await get_pr_files(ctx.pr_number, repo=ctx.repo, github_user=ctx.config.github_user)
        except Exception as exc:
            return RoleResult(
                success=False,
                summary=f"Failed to fetch PR #{ctx.pr_number} diff",
                error=str(exc),
            )

        # Load addressed external findings from developer handoff
        addressed = await self._load_addressed_findings(ctx)
        if addressed:
            log.info("reviewer.addressed_findings_loaded", count=len(addressed))

        # Run LLM review (chunked if needed)
        review = await self._run_review(ctx, task, diff, files, addressed_findings=addressed)

        # Append review rationale to spec (non-fatal provenance threading)
        self._append_review_rationale(ctx, review)

        # Post review with inline comments on specific lines
        await self._post_review(ctx, review, diff)

        # Write handoff
        await self._write_handoff(ctx, review)

        # Extract learnings from this review
        await self._extract_review_memories(ctx, task, review)

        total_count = len(review.findings)
        log.info("reviewer.done", issue=ctx.issue_number, findings=total_count)

        # IN_REVIEW is correct for all outcomes: architecture doc says "Issue
        # state ownership is human -- agents never auto-move issues to DONE.
        # Issues stay IN_REVIEW until the human merges." Even a clean review
        # (0 findings) requires human approval before integration.
        return RoleResult(
            success=True,
            summary=f"Reviewed PR #{ctx.pr_number}: {total_count} findings",
            output_state=TaskState.IN_REVIEW,
            findings=[f.description for f in review.findings],
        )

    async def _discover_pr(self, ctx: ExecutionContext) -> str | None:
        """Discover PR number and branch for the issue.

        Returns an error message string if discovery fails, or ``None`` on
        success (PR number and branch populated on *ctx*).
        """
        if not ctx.pr_number:
            log.info("reviewer.discovering_pr", issue=ctx.issue_number)
            pr_info = await find_pr_for_issue(
                ctx.issue_number,
                repo=ctx.repo,
                github_user=ctx.config.github_user,
            )
            if pr_info:
                ctx.pr_number = pr_info.number
                ctx.pr_url = pr_info.url
                if pr_info.branch and not ctx.branch_name:
                    ctx.branch_name = pr_info.branch
                log.info("reviewer.pr_discovered", pr=pr_info.number, branch=ctx.branch_name)
            else:
                return f"Issue #{ctx.issue_number} has no linked PR"

        if ctx.pr_number and not ctx.branch_name:
            try:
                ctx.branch_name = await get_pr_branch(
                    ctx.pr_number,
                    repo=ctx.repo,
                    github_user=ctx.config.github_user,
                )
            except Exception:
                log.warning("reviewer.branch_discovery_failed", exc_info=True)

        return None  # success -- ctx is populated

    async def _load_addressed_findings(self, ctx: ExecutionContext) -> list[dict]:
        """Load addressed external findings from the developer's handoff.

        Three sources (tried in order):
        1. File-based handoff for this issue
        2. Resume run's DB handoff (when spawned with --resume)
        3. Recent developer TaskRuns for this issue (DB fallback, loops
           through up to 10 runs to skip address-review cycles that don't
           populate addressed_findings)
        """
        # Source 1: file handoff
        try:
            handoff = read_handoff_file(ctx.project_dir, issue=ctx.issue_number or None)
            if handoff and handoff.details.get("addressed_findings"):
                return handoff.details["addressed_findings"]
        except Exception:
            log.debug("reviewer.addressed_findings_file_failed", exc_info=True)

        # Source 2: resume_run_id DB handoff
        if ctx.resume_run_id:
            try:
                from sova.ipc.handoff import read_handoff as read_db_handoff

                db_handoff = await read_db_handoff(ctx.resume_run_id)
                if db_handoff and db_handoff.addressed_findings:
                    log.info(
                        "reviewer.addressed_findings_from_resume",
                        run_id=ctx.resume_run_id,
                        count=len(db_handoff.addressed_findings),
                    )
                    return db_handoff.addressed_findings
            except Exception:
                log.debug("reviewer.addressed_findings_resume_failed", exc_info=True)

        # Source 3: DB fallback -- loop through recent developer runs
        issue = (ctx.issue_number or "").lstrip("#").strip()
        if not issue:
            return []
        try:
            from sqlalchemy import select

            async with await get_session() as session:
                stmt = (
                    select(TaskRun)
                    .where(
                        TaskRun.issue_number == issue,
                        TaskRun.role == "developer",
                        TaskRun.status.in_(["done", "failed", "interrupted"]),
                        TaskRun.handoff_json.isnot(None),
                    )
                    .order_by(TaskRun.started_at.desc())
                    .limit(10)
                )
                result = await session.execute(stmt)
                for run_record in result.scalars():
                    if run_record.handoff_json:
                        findings = run_record.handoff_json.get("addressed_findings", [])
                        if findings:
                            log.info("reviewer.addressed_findings_from_db", run_id=run_record.id, count=len(findings))
                            return findings
        except Exception:
            log.debug("reviewer.addressed_findings_db_failed", exc_info=True)

        return []

    async def _extract_review_memories(self, ctx: ExecutionContext, task: Task, review: ReviewResult) -> None:
        """Extract learnings from this review into memory (non-fatal)."""
        try:
            from sova.knowledge.extraction import extract_memories

            await extract_memories(
                role="reviewer",
                issue_number=ctx.issue_number,
                repo=ctx.repo,
                task_title=task.title,
                files_changed=[],
                step_summaries=[f"review: {len(review.findings)} findings"],
                review_findings=[
                    {
                        "file": f.file,
                        "line": f.line,
                        "severity": f.severity,
                        "category": f.category,
                        "description": f.description,
                        "suggestion": f.suggestion,
                    }
                    for f in review.findings
                ],
                cwd=ctx.working_dir,
            )
        except Exception:
            log.warning("reviewer.extract_memory_failed", exc_info=True)

    async def _clear_current_step(self, ctx: ExecutionContext) -> None:
        """Clear the current_step sentinel on the TaskRun.

        ReviewerRole bypasses WorkflowEngine so _adopt_task_run() is never
        called. This leaves current_step="agent" permanently. Clear it here
        so the DB record accurately reflects that the reviewer has finished.
        """
        try:
            async with await get_session() as session:
                async with session.begin():
                    task_run = await session.get(TaskRun, ctx.task_run_id)
                    if task_run:
                        task_run.current_step = None
        except (OSError, SQLAlchemyError):
            log.warning("reviewer.clear_step_failed", exc_info=True)

    async def _post_review(self, ctx: ExecutionContext, review: ReviewResult, diff: str) -> None:
        """Post review findings as inline PR review comments, with fallback."""
        diff_lines = parse_diff_lines(diff)
        inline_comments, body_only = _build_review_comments(review.findings, diff_lines)

        body = _format_review_body(review.findings, review.summary, ctx.pr_number, body_only)

        try:
            await ctx.adapter.post_pr_review(
                ctx.pr_number,
                body=body,
                event="COMMENT",
                comments=inline_comments,
            )
            log.info("reviewer.posted_review", inline=len(inline_comments), body_only=len(body_only))
            return
        except Exception:
            if inline_comments:
                log.warning("reviewer.inline_review_failed", exc_info=True)
                try:
                    await ctx.adapter.post_pr_review(
                        ctx.pr_number,
                        body=body,
                        event="COMMENT",
                        comments=[],
                    )
                    log.info("reviewer.posted_review_body_only", finding_count=len(review.findings))
                    return
                except Exception:
                    log.warning("reviewer.body_only_review_failed", exc_info=True)
            else:
                log.warning("reviewer.review_api_failed", exc_info=True)
        fallback = _format_findings_comment(review.findings, review.summary, ctx.pr_number)
        await ctx.adapter.post_pr_comment(ctx.pr_number, fallback)

    async def _run_review(
        self,
        ctx: ExecutionContext,
        task: Task,
        diff: str,
        files: list[str],
        addressed_findings: list[dict] | None = None,
    ) -> ReviewResult:
        """Send diff to LLM for review, chunking if too large."""
        # Load spec for intent-anchored review (spec-mediated context compression)
        spec_sections = self._load_spec_sections(ctx)

        if spec_sections:
            spec_chars = sum(len(v) for v in spec_sections.values())
            body_chars = len(task.body) if task.body else 0
            log.info(
                "reviewer.spec_compression",
                spec_chars=spec_chars,
                body_chars_omitted=body_chars,
                sections=list(spec_sections.keys()),
            )

        # Panel review: parallel focused dimension reviewers
        if ctx.config.review.panel.enabled:
            return await self._run_panel_review(ctx, task, diff, files, spec_sections, addressed_findings)

        return await self._run_single_review(
            ctx,
            task,
            diff,
            files,
            spec_sections,
            addressed_findings=addressed_findings,
        )

    async def _run_panel_review(
        self,
        ctx: ExecutionContext,
        task: Task,
        diff: str,
        files: list[str],
        spec_sections: dict[str, str] | None,
        addressed_findings: list[dict] | None = None,
    ) -> ReviewResult:
        """Delegate to parallel panel review."""
        from sova.roles.panel_review import run_panel_review

        budget_remaining = ctx.config.agent.max_budget - ctx.cost_usd
        log.info("reviewer.panel_mode", dimensions=ctx.config.review.panel.dimensions)

        result = await run_panel_review(
            task=task,
            diff=diff,
            files=files,
            panel_config=ctx.config.review.panel,
            spec_sections=spec_sections,
            cwd=ctx.working_dir,
            budget_remaining=budget_remaining,
            addressed_findings=addressed_findings,
        )
        ctx.add_cost(result.total_cost)
        return result

    async def _run_single_review(
        self,
        ctx: ExecutionContext,
        task: Task,
        diff: str,
        files: list[str],
        spec_sections: dict[str, str] | None,
        addressed_findings: list[dict] | None = None,
    ) -> ReviewResult:
        """Original single-reviewer path."""
        chunks = _chunk_diff(diff)
        result = ReviewResult()

        for i, chunk in enumerate(chunks):
            try:
                # Send full spec only for first chunk; subsequent chunks get a compact reference
                chunk_spec = spec_sections if i == 0 else _compact_spec_ref(spec_sections)
                # Include addressed findings only in first chunk prompt
                chunk_addressed = addressed_findings if i == 0 else None
                prompt = _build_review_prompt(
                    task,
                    chunk,
                    files,
                    spec_sections=chunk_spec,
                    addressed_findings=chunk_addressed,
                )
                llm_result = await invoke(
                    prompt,
                    model="sonnet",
                    cwd=ctx.working_dir,
                )
                ctx.add_cost(llm_result.cost_usd)
                result.total_cost += llm_result.cost_usd

                findings, summary = _parse_findings(llm_result.text)
                result.findings.extend(findings)
                if i == 0 or not result.summary:
                    result.summary = summary

            except Exception:
                log.warning("reviewer.llm_failed", chunk=i + 1, total=len(chunks), exc_info=True)
                if not result.findings:
                    result.summary = "LLM review unavailable -- manual review recommended"

        return result

    def _load_spec_sections(self, ctx: ExecutionContext) -> dict[str, str] | None:
        """Load spec sections for intent-anchored review. Returns None if no spec exists."""
        issue = ctx.issue_number
        if not issue or not str(issue).isdigit():
            return None
        try:
            # Prefer working_dir (worktree) so specs on the PR branch are found
            spec_dir = ctx.working_dir or ctx.project_dir
            path = find_spec_file(str(issue), spec_dir)
            if path is None:
                log.debug("reviewer.no_spec", issue=issue)
                return None
            raw = path.read_text()
            sections = _extract_spec_sections(raw)
            if sections:
                log.info("reviewer.spec_loaded", issue=issue, sections=list(sections.keys()))
            return sections or None
        except Exception:
            log.warning("reviewer.spec_load_failed", issue=issue, exc_info=True)
            return None

    def _append_review_rationale(self, ctx: ExecutionContext, review: ReviewResult) -> None:
        """Append review rationale to spec for findings with severity >= 5."""
        try:
            from sova.core.steps._spec_helpers import SECTION_REVIEW_RATIONALE, append_spec_section

            significant = [f for f in review.findings if f.severity >= 5]
            if not significant:
                return

            lines: list[str] = []
            for f in sorted(significant, key=lambda x: x.severity, reverse=True):
                loc = f"{f.file}:{f.line}" if f.line else f.file
                lines.append(f"- [{f.severity}/10] [{f.category}] `{loc}`: {f.description}")
                if f.suggestion:
                    lines.append(f"  Fix: {f.suggestion}")

            project_dir = ctx.working_dir or ctx.project_dir
            append_spec_section(ctx.issue_number, SECTION_REVIEW_RATIONALE, "\n".join(lines), project_dir)
        except Exception:
            log.warning("reviewer.review_rationale_failed", exc_info=True)

    async def _write_handoff(self, ctx: ExecutionContext, review: ReviewResult) -> None:
        """Write both DB-backed and file-based handoffs."""
        actionable = review.actionable
        next_action = "address_review" if actionable else "approve"

        findings_data = [
            {
                "file": f.file,
                "line": f.line,
                "severity": f.severity,
                "category": f.category,
                "description": f.description,
                "suggestion": f.suggestion,
            }
            for f in actionable
        ]

        agent_handoff = AgentHandoff(
            role="reviewer",
            phase="review",
            summary=review.summary,
            key_decisions=[],
            next_action=next_action,
            pending_findings=findings_data,
            pr_number=ctx.pr_number,
            branch_name=ctx.branch_name,
        )

        if ctx.task_run_id:
            try:
                await write_handoff(ctx.task_run_id, agent_handoff)
            except Exception:
                log.warning("reviewer.handoff_db_failed", exc_info=True)

        # Dashboard handoff
        actions: list[HandoffAction] = []

        if actionable:
            auto = ctx.config.pipeline.auto_address_review
            actions.append(
                HandoffAction(
                    id="address_review",
                    label="Address Review",
                    description=f"Fix {len(actionable)} actionable findings",
                    style="approve",
                    mode="agent",
                    command="",
                    args={"issue": ctx.issue_number, "pr": ctx.pr_number, "role": "developer"},
                    auto_execute=auto,
                ),
            )
        else:
            actions.append(
                HandoffAction(
                    id="integrate",
                    label="Integrate PR",
                    description="No actionable findings -- rebase, merge, cleanup, and learn",
                    style="approve",
                    mode="claude-command",
                    command=f"/integrate-pr {ctx.pr_number}",
                    args={"issue": ctx.issue_number, "pr": ctx.pr_number},
                ),
            )

        dashboard_handoff = DashboardHandoff(
            source="reviewer",
            status="awaiting_action",
            issue=ctx.issue_number,
            pr_number=ctx.pr_number,
            branch=ctx.branch_name,
            summary=f"{len(review.findings)} findings (all to be addressed)",
            details={
                "next_action": next_action,
                "cost_usd": str(review.total_cost),
                "findings": findings_data,
            },
            next_actions=actions,
        )

        try:
            write_handoff_file(ctx.project_dir, dashboard_handoff)
        except Exception:
            log.warning("reviewer.handoff_file_failed", exc_info=True)
