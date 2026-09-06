"""Reviewer role -- review PRs and provide feedback.

Reads IN_REVIEW issues with linked PRs, reviews the code changes
via LLM, and posts scored review findings on the PR. Writes a handoff
with actionable findings for the Developer to address.

Data classes, formatting functions, prompt construction, and parsing
logic live in ``_review_comments.py``. This module re-exports them for
backward compatibility and contains the ``ReviewerRole`` orchestration
class.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from sova.adapters.base import Task, TaskState
from sova.core.context import ExecutionContext
from sova.core.schema_validation import ValidationError, validate_step_output
from sova.core.schemas import get_review_schema
from sova.core.spec_utils import find_spec_file
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

if TYPE_CHECKING:
    from sova.llm.models import LLMResult

from sova.roles._review_comments import (
    _MAX_COMPACT_SPEC_CHARS,
    _SPEC_SECTIONS,
    _VERDICT_TO_LABEL,
    DIFF_CHUNK_SIZE,
    ReviewFinding,
    ReviewResult,
    _build_review_comments,
    _build_review_prompt,
    _chunk_diff,
    _compact_spec_ref,
    _extract_json,
    _extract_spec_sections,
    _format_addressed_findings,
    _format_findings_body,
    _format_findings_comment,
    _format_inline_comment,
    _format_review_body,
    _make_protected_path_finding,
    _parse_findings,
    _safe_severity,
    _severity_label,
    _sova_verdict_label_name,
    _verdict_label,
)
from sova.roles._review_format import _SEVERITY_HIGH
from sova.roles.base import AgentRole, RoleResult, TaskAssessment
from sova.utils.logging import get_logger

# Re-export everything from _review_comments for backward compatibility.
# Do NOT remove these imports; external modules depend on them.
__all__ = [
    "DIFF_CHUNK_SIZE",
    "ReviewFinding",
    "ReviewResult",
    "ReviewerRole",
    "_build_review_comments",
    "_build_review_prompt",
    "_chunk_diff",
    "_compact_spec_ref",
    "_extract_json",
    "_extract_spec_sections",
    "_format_addressed_findings",
    "_format_findings_body",
    "_format_findings_comment",
    "_format_inline_comment",
    "_format_review_body",
    "_make_protected_path_finding",
    "_MAX_COMPACT_SPEC_CHARS",
    "_parse_findings",
    "_safe_severity",
    "_severity_label",
    "_sova_verdict_label_name",
    "_SPEC_SECTIONS",
    "_verdict_label",
    "_VERDICT_TO_LABEL",
]

log = get_logger(component="role.reviewer")


def _build_finding_summary(review: ReviewResult) -> dict:
    """Build a severity-bucketed summary of review findings for handoff metadata."""
    all_findings = review.findings
    actionable = review.actionable
    by_severity: dict[str, int] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for f in actionable:
        by_severity[_severity_label(f.severity).lower()] += 1
    return {
        "total": len(all_findings),
        "actionable": len(actionable),
        "by_severity": by_severity,
    }


def _check_protected_paths(files: list[str], protected_paths: list[str]) -> list[str]:
    """Return *files* whose path starts with any entry in *protected_paths*.

    Performs case-sensitive prefix matching. Pattern ``.github/`` matches
    ``.github/workflows`` but not ``.GitHub/`` or ``github/``.
    """
    if not protected_paths:
        return []
    return [f for f in files if any(f.startswith(p) for p in protected_paths)]


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

        try:
            diff = await get_pr_diff(ctx.pr_number, repo=ctx.repo, github_user=ctx.config.github_user)
            files = await get_pr_files(ctx.pr_number, repo=ctx.repo, github_user=ctx.config.github_user)
        except Exception as exc:
            return RoleResult(
                success=False,
                summary=f"Failed to fetch PR #{ctx.pr_number} diff",
                error=str(exc),
            )

        addressed = await self._load_addressed_findings(ctx)
        if addressed:
            log.info("reviewer.addressed_findings_loaded", count=len(addressed))

        review = await self._run_review(ctx, task, diff, files, addressed_findings=addressed)

        protected = _check_protected_paths(files, ctx.config.review.protected_paths)
        if protected:
            log.info("reviewer.protected_paths_hit", paths=protected)
            review.findings.append(_make_protected_path_finding(protected))

        self._append_review_rationale(ctx, review)

        post_succeeded = await self._post_review(ctx, review, diff)
        if not post_succeeded:
            review.post_failed = True

        if not review.post_failed:
            await self._write_verdict_label(ctx, review)

        await self._write_handoff(ctx, review)

        await self._extract_review_memories(ctx, task, review)

        total_count = len(review.findings)
        log.info("reviewer.done", issue=ctx.issue_number, findings=total_count)

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

        return None

    async def _load_addressed_findings(self, ctx: ExecutionContext) -> list[dict]:
        """Load addressed external findings from the developer's handoff.

        Three sources (tried in order):
        1. File-based handoff for this issue
        2. Resume run's DB handoff (when spawned with --resume)
        3. Recent developer TaskRuns for this issue (DB fallback, loops
           through up to 10 runs to skip address-review cycles that don't
           populate addressed_findings)
        """
        try:
            handoff = read_handoff_file(ctx.project_dir, issue=ctx.issue_number or None)
            if handoff and handoff.details.get("addressed_findings"):
                return handoff.details["addressed_findings"]
        except Exception:
            log.debug("reviewer.addressed_findings_file_failed", exc_info=True)

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
        """No-op: automatic memory extraction is disabled.

        Use ``/extract-knowledge`` for human-reviewed knowledge capture.
        """

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

    async def _post_review(self, ctx: ExecutionContext, review: ReviewResult, diff: str) -> bool:
        """Post review findings as inline PR review comments, with fallback.

        Returns True if the review was posted successfully via any method,
        False if all posting attempts failed. Never raises: callers must check
        the return value and set ``review.post_failed`` accordingly so the
        handoff can signal the failure without triggering spurious address-review
        pipeline cycles.
        """
        diff_lines = parse_diff_lines(diff)
        inline_comments, body_only = _build_review_comments(review.findings, diff_lines)

        body = _format_review_body(review.findings, review.summary)

        try:
            await ctx.adapter.post_pr_review(
                ctx.pr_number,
                body=body,
                event="COMMENT",
                comments=inline_comments,
            )
            log.info("reviewer.posted_review", inline=len(inline_comments), body_only=len(body_only))
            return True
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
                    return True
                except Exception:
                    log.warning("reviewer.body_only_review_failed", exc_info=True)
            else:
                log.warning("reviewer.review_api_failed", exc_info=True)

        fallback = _format_findings_comment(review.findings, review.summary)
        try:
            await ctx.adapter.post_pr_comment(ctx.pr_number, fallback)
            log.info("reviewer.posted_comment_fallback", finding_count=len(review.findings))
            return True
        except Exception:
            log.warning(
                "reviewer.all_posting_attempts_failed",
                pr=ctx.pr_number,
                issue=ctx.issue_number,
                exc_info=True,
            )
            return False

    async def _run_review(
        self,
        ctx: ExecutionContext,
        task: Task,
        diff: str,
        files: list[str],
        addressed_findings: list[dict] | None = None,
    ) -> ReviewResult:
        """Send diff to LLM for review, chunking if too large."""
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
        """Delegate to the dimension review panel."""
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
                chunk_spec = spec_sections if i == 0 else _compact_spec_ref(spec_sections)
                chunk_addressed = addressed_findings if i == 0 else None
                prompt = _build_review_prompt(
                    task,
                    chunk,
                    files,
                    spec_sections=chunk_spec,
                    addressed_findings=chunk_addressed,
                )
                chunk_budget = ctx.config.agent.max_budget / len(chunks)

                llm_result = await invoke(
                    prompt,
                    model="sonnet",
                    cwd=ctx.working_dir,
                    max_budget_usd=chunk_budget,
                )
                ctx.add_cost(llm_result.cost_usd)
                result.total_cost += llm_result.cost_usd

                chunk_spent = llm_result.cost_usd
                last_retry_text = llm_result.text

                async def retry_review(retry_prompt: str) -> LLMResult:
                    nonlocal chunk_spent, last_retry_text
                    remaining = chunk_budget - chunk_spent
                    if remaining <= Decimal("0"):
                        raise RuntimeError("Chunk review budget exhausted, cannot retry")
                    max_retry_budget = max(Decimal("0"), remaining)
                    retry_result = await invoke(
                        retry_prompt,
                        model="sonnet",
                        cwd=ctx.working_dir,
                        max_budget_usd=max_retry_budget,
                    )
                    chunk_spent += retry_result.cost_usd
                    last_retry_text = retry_result.text
                    return retry_result

                # Validate output with retry on failure
                try:
                    data, retry_cost = await validate_step_output(
                        raw_text=llm_result.text,
                        schema=get_review_schema(),
                        llm_invoke=retry_review,
                        original_prompt=prompt,
                        max_retries=2,
                    )
                    ctx.add_cost(retry_cost)
                    result.total_cost += retry_cost

                    # Convert validated dict to ReviewFinding objects
                    findings = [
                        ReviewFinding(
                            file=item.get("file", "unknown"),
                            severity=_safe_severity(item.get("severity", 5)),
                            category=item.get("category", "other"),
                            description=item.get("description", ""),
                            suggestion=item.get("suggestion", ""),
                            line=item.get("line"),
                        )
                        for item in data.get("findings", [])
                    ]
                    summary = data.get("summary", "")
                except ValidationError as ve:
                    log.warning("reviewer.validation_failed", chunk=i + 1, error=str(ve), exc_info=True)
                    ctx.add_cost(ve.retry_cost)
                    result.total_cost += ve.retry_cost
                    findings, summary = _parse_findings(last_retry_text)

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

            significant = [f for f in review.findings if f.severity >= _SEVERITY_HIGH]
            if not significant:
                return

            lines: list[str] = []
            for f in sorted(significant, key=lambda x: x.severity, reverse=True):
                loc = f"{f.file}:{f.line}" if f.line else f.file
                label = _severity_label(f.severity)
                lines.append(f"- [{label}] [{f.category}] `{loc}`: {f.description}")
                if f.suggestion:
                    lines.append(f"  Fix: {f.suggestion}")

            project_dir = ctx.working_dir or ctx.project_dir
            append_spec_section(ctx.issue_number, SECTION_REVIEW_RATIONALE, "\n".join(lines), project_dir)
        except Exception:
            log.warning("reviewer.review_rationale_failed", exc_info=True)

    async def _write_verdict_label(self, ctx: ExecutionContext, review: ReviewResult) -> None:
        """Write a sova:{verdict} label to the issue for cross-machine visibility.

        Removes any existing sova:* verdict labels first (idempotent), then adds
        the current verdict. Non-fatal: if the label write fails, the DB/marker
        fallback path in _fetch_sova_verdicts() remains functional.

        Uses actionable findings (excludes protected-path) so that
        protected-path-only reviews write ``sova:approved`` instead of
        ``sova:revise``, preventing spurious address-review routing.
        """
        label = _sova_verdict_label_name(review.actionable)
        issue = ctx.issue_number
        if not issue:
            return
        try:
            for old_label in set(_VERDICT_TO_LABEL.values()) - {label}:
                await ctx.adapter.remove_label(issue, old_label)
            await ctx.adapter.add_label(issue, label)
            log.info("reviewer.verdict_label_written", issue=issue, label=label)
        except Exception:
            log.warning("reviewer.verdict_label_failed", issue=issue, label=label, exc_info=True)

    async def _write_handoff(self, ctx: ExecutionContext, review: ReviewResult) -> None:
        """Write both DB-backed and file-based handoffs.

        When ``review.post_failed`` is True (all posting attempts failed), the
        handoff uses ``next_action="review_post_failed"`` and surfaces a manual
        Re-run Review action with ``auto_execute=False``. This prevents
        ``_process_auto_handoff()`` from spawning a spurious address-review cycle
        and prevents ``get_sova_review_verdict()`` from defaulting to "revise".
        """
        actionable = review.actionable

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

        finding_summary = _build_finding_summary(review)

        if review.post_failed:
            next_action = "review_post_failed"
            post_failed_summary = (
                "Review completed but could not be posted to GitHub. Re-run the reviewer to retry posting."
            )
            agent_handoff = AgentHandoff(
                role="reviewer",
                phase="review",
                summary=post_failed_summary,
                key_decisions=[],
                next_action=next_action,
                pending_findings=findings_data,
                metadata={"finding_summary": finding_summary},
                pr_number=ctx.pr_number,
                branch_name=ctx.branch_name,
            )
            rerun_action = HandoffAction(
                id="rerun_review",
                label="Re-run Review",
                description="Retry posting the review to GitHub",
                style="neutral",
                mode="agent",
                command="",
                args={"issue": ctx.issue_number, "pr": ctx.pr_number, "role": "reviewer"},
                auto_execute=False,
            )
            dashboard_handoff = DashboardHandoff(
                source="reviewer",
                status="awaiting_action",
                issue=ctx.issue_number,
                pr_number=ctx.pr_number,
                branch=ctx.branch_name,
                summary=post_failed_summary,
                details={
                    "next_action": next_action,
                    "cost_usd": str(review.total_cost),
                    "pending_findings": findings_data,
                },
                next_actions=[rerun_action],
            )
            log.warning(
                "reviewer.post_failed_handoff_written",
                pr=ctx.pr_number,
                issue=ctx.issue_number,
                finding_count=len(actionable),
            )
        else:
            has_protected_only = not actionable and review.findings
            if actionable:
                next_action = "address_review"
            elif has_protected_only:
                next_action = "needs_human_review"
            else:
                next_action = "approve"

            agent_handoff = AgentHandoff(
                role="reviewer",
                phase="review",
                summary=review.summary,
                key_decisions=[],
                next_action=next_action,
                pending_findings=findings_data,
                metadata={"finding_summary": finding_summary},
                pr_number=ctx.pr_number,
                branch_name=ctx.branch_name,
            )

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
            elif has_protected_only:
                actions.append(
                    HandoffAction(
                        id="integrate",
                        label="Integrate PR",
                        description="Code review passed. Protected paths touched: human approval required.",
                        style="approve",
                        mode="claude-command",
                        command=f"/integrate-pr {ctx.pr_number}",
                        args={"issue": ctx.issue_number, "pr": ctx.pr_number},
                        auto_execute=False,
                    ),
                )
            else:
                actions.append(
                    HandoffAction(
                        id="integrate",
                        label="Integrate PR",
                        description="No actionable findings: rebase, merge, cleanup, and learn",
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
                    "pending_findings": findings_data,
                },
                next_actions=actions,
            )

        if ctx.task_run_id:
            try:
                await write_handoff(ctx.task_run_id, agent_handoff)
            except Exception:
                log.warning("reviewer.handoff_db_failed", exc_info=True)

        try:
            write_handoff_file(ctx.project_dir, dashboard_handoff)
        except Exception:
            log.warning("reviewer.handoff_file_failed", exc_info=True)
