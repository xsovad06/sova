"""Step: Address review -- fix review findings from the Reviewer agent."""

from __future__ import annotations

from pathlib import Path

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.ipc.handoff import read_handoff, read_handoff_file
from sova.llm.client import invoke_command
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="step.address_review")


def _load_review_findings(project_dir: Path) -> list[dict]:
    """Load review findings from the reviewer's handoff file."""
    handoff = read_handoff_file(project_dir)
    if handoff is None:
        return []
    return handoff.details.get("findings", [])


async def _load_review_findings_from_db(task_run_id: int | None) -> list[dict]:
    """Load review findings from a specific run's handoff in DB."""
    if task_run_id is None:
        return []
    try:
        handoff = await read_handoff(task_run_id)
        if handoff and handoff.pending_findings:
            return handoff.pending_findings
    except Exception:
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
                    TaskRun.status.in_(["done", "failed", "interrupted"]),
                    TaskRun.handoff_json.isnot(None),
                )
                .order_by(TaskRun.started_at.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            run = result.scalar_one_or_none()
            if run and run.handoff_json:
                findings = run.handoff_json.get("pending_findings", [])
                if findings:
                    log.info("address_review.findings_from_reviewer", run_id=run.id, count=len(findings))
                    return findings
    except Exception:
        log.debug("address_review.issue_findings_failed", exc_info=True)
    return []


def _format_findings_prompt(findings: list[dict]) -> str:
    """Format findings into a prompt for the LLM to address."""
    lines = [
        "Address ALL of the following code review findings. For each finding:",
        "- DEFAULT: Fix the issue in the code.",
        "- EXCEPTION: If a finding is a false positive, not applicable in context,",
        "  or requires a human decision, state the reason instead of fixing.",
        "  Do NOT skip findings without justification.\n",
    ]
    for i, f in enumerate(findings, 1):
        loc = f.get("file", "unknown")
        if f.get("line"):
            loc += f":{f['line']}"
        lines.append(f"{i}. [{f.get('severity', '?')}/10] [{f.get('category', 'other')}] `{loc}`")
        lines.append(f"   {f.get('description', '')}")
        if f.get("suggestion"):
            lines.append(f"   Fix: {f['suggestion']}")
        lines.append("")
    lines.append("After fixing all issues, make sure all tests still pass.")
    return "\n".join(lines)


class AddressReviewStep(BaseStep):
    name = "address_review"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.address_review", pr=ctx.pr_number)

        # Load findings: file -> resumed run -> most recent reviewer for this issue
        findings = _load_review_findings(ctx.project_dir)
        if not findings:
            findings = await _load_review_findings_from_db(ctx.resume_run_id)
        if not findings:
            findings = await _load_review_findings_by_issue(ctx.issue_number)

        if not findings:
            log.info("step.address_review.no_findings")
            return StepResult(success=True, summary="No review findings to address")

        log.info("step.address_review.findings_loaded", count=len(findings))

        prompt = _format_findings_prompt(findings)
        try:
            result = await invoke_command(
                prompt,
                model=ctx.config.agent.model,
                cwd=ctx.working_dir,
                max_budget_usd=ctx.config.agent.max_budget - ctx.cost_usd,
            )
            ctx.add_cost(result.cost_usd)
            return StepResult(
                success=True,
                summary=f"Addressed {len(findings)} review findings",
                cost_usd=result.cost_usd,
            )
        except RuntimeError as exc:
            return StepResult(success=False, summary="Failed to address review findings", error=str(exc))

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Gate: addressing review should produce changes."""
        diff_result = await run("git", "diff", "--stat", "HEAD", cwd=ctx.working_dir)
        staged = await run("git", "diff", "--cached", "--stat", cwd=ctx.working_dir)
        has_changes = bool(
            (diff_result.success and diff_result.stdout.strip()) or (staged.success and staged.stdout.strip())
        )
        log_result = await run("git", "log", f"{ctx.base_branch}..HEAD", "--oneline", cwd=ctx.working_dir)
        has_commits = bool(log_result.success and log_result.stdout.strip())

        if has_changes or has_commits:
            return GateCheckResult(passed=True)
        return GateCheckResult(passed=False, reason="No changes after addressing review findings")

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        return self.name in ctx.completed_steps
