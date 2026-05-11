"""Reviewer role -- review PRs and provide feedback.

Reads IN_REVIEW issues with linked PRs, reviews the code changes
via LLM, and posts scored review findings on the PR. Writes a handoff
with actionable findings for the Developer to address.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal

from sova.adapters.base import Task, TaskState
from sova.core.context import ExecutionContext
from sova.git.operations import find_pr_for_issue, get_pr_diff, get_pr_files
from sova.ipc.handoff import AgentHandoff, DashboardHandoff, HandoffAction, write_handoff, write_handoff_file
from sova.llm.client import invoke
from sova.roles.base import AgentRole, RoleResult, TaskAssessment
from sova.utils.logging import get_logger

log = get_logger(component="role.reviewer")

DIFF_CHUNK_SIZE = 100_000  # ~100KB per chunk


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


def _build_review_prompt(task: Task, diff: str, files: list[str]) -> str:
    """Build the LLM prompt for code review."""
    file_list = "\n".join(f"- {f}" for f in files)
    return f"""You are a senior software engineer performing a thorough code review. \
Your job is to find real issues -- do NOT rubber-stamp the PR. \
Assume the code has bugs until proven otherwise.

## PR Context
**Issue**: {task.title}
**Description**: {task.body}

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
8. **Docs** (2-3): stale comments, misleading docstrings

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
      "category": "bug|security|error-handling|testing|api|performance|design|docs",
      "description": "Concise description of the issue",
      "suggestion": "Specific fix recommendation"
    }}
  ],
  "summary": "2-3 sentence overall assessment. State the most critical issue first."
}}"""


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
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start:end])
            except json.JSONDecodeError:
                log.warning("parse_findings.failed", text_preview=text[:200], exc_info=True)
                return [], "Failed to parse review response"
        else:
            log.warning("parse_findings.failed", text_preview=text[:200], exc_info=True)
            return [], "Failed to parse review response"

    findings = []
    for item in data.get("findings", []):
        findings.append(
            ReviewFinding(
                file=item.get("file", "unknown"),
                severity=int(item.get("severity", 5)),
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
        task = await ctx.adapter.get_task(ctx.issue_number)

        if not self.validate_preconditions(task, force=ctx.force):
            return RoleResult(
                success=False,
                summary=f"Issue #{ctx.issue_number} not in valid state for review",
                error=f"Precondition failed: issue is in {task.state}, "
                f"expected one of {', '.join(self.allowed_input_states)}",
            )

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
                log.info("reviewer.pr_discovered", pr=pr_info.number)
            else:
                return RoleResult(
                    success=False,
                    summary=f"Issue #{ctx.issue_number} has no linked PR",
                    error="No PR found for this issue. Create a PR first.",
                )

        log.info("reviewer.start", issue=ctx.issue_number, pr=ctx.pr_number)

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

        # Run LLM review (chunked if needed)
        review = await self._run_review(ctx, task, diff, files)

        # Post review comment on the PR, not the issue
        comment = _format_findings_comment(review.findings, review.summary, ctx.pr_number)
        await ctx.adapter.post_pr_comment(ctx.pr_number, comment)

        # Write handoff
        await self._write_handoff(ctx, review)

        # Extract learnings from this review
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

        total_count = len(review.findings)
        log.info("reviewer.done", issue=ctx.issue_number, findings=total_count)

        return RoleResult(
            success=True,
            summary=f"Reviewed PR #{ctx.pr_number}: {total_count} findings",
            output_state=TaskState.IN_REVIEW,
            findings=[f.description for f in review.findings],
        )

    async def _run_review(self, ctx: ExecutionContext, task: Task, diff: str, files: list[str]) -> ReviewResult:
        """Send diff to LLM for review, chunking if too large."""
        chunks = _chunk_diff(diff)
        result = ReviewResult()

        for i, chunk in enumerate(chunks):
            try:
                llm_result = await invoke(
                    _build_review_prompt(task, chunk, files),
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
            actions.append(
                HandoffAction(
                    id="address_review",
                    label="Address Review",
                    description=f"Fix {len(actionable)} actionable findings",
                    style="approve",
                    mode="agent",
                    command="",
                    args={"issue": ctx.issue_number, "pr": ctx.pr_number, "role": "developer"},
                    auto_execute=True,
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
            actions.append(
                HandoffAction(
                    id="approve",
                    label="Merge Only",
                    description="Squash merge without rebase or learning -- skip the full pipeline",
                    style="neutral",
                    mode="claude-command",
                    command=f"/approve-merge {ctx.pr_number}",
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
