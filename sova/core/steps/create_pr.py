"""Step 9: Create PR -- generate a rich description and open a pull request."""

from __future__ import annotations

from sova.adapters.base import TaskState
from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.git import operations as git_ops
from sova.llm.client import invoke
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="step.create_pr")

_PR_BODY_PROMPT = """\
Generate a pull request description for the changes below. Output ONLY the \
markdown body (no fences, no commentary). Use this structure:

## Summary
1-3 bullet points: WHAT changed and WHY.

## Changes
Brief description of each logical change grouped by area.

## Review guidance
What should a reviewer focus on? Any trade-offs or shortcuts?

## Test plan
How were these changes verified?

Closes #{issue_number}

---
Issue #{issue_number}: {issue_title}

{issue_body}

Commits on this branch:
{commit_log}

Files changed:
{diff_stat}
"""


class CreatePRStep(BaseStep):
    name = "create_pr"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.create_pr", issue=ctx.issue_number, branch=ctx.branch_name)

        task_title = ctx.task.title if ctx.task else ctx.branch_name
        title = f"feat(#{ctx.issue_number}): {task_title}"

        body = await self._generate_pr_body(ctx, task_title)

        try:
            pr_info = await git_ops.create_pr(
                title=title,
                body=body,
                base=ctx.base_branch,
                head=ctx.branch_name,
                repo=ctx.repo,
            )
            ctx.pr_number = pr_info.number
            ctx.pr_url = pr_info.url

            try:
                await ctx.adapter.transition_state(ctx.issue_number, TaskState.IN_REVIEW)
            except Exception as exc:
                log.warning("step.create_pr.tracker_update_failed", error=str(exc))

            return StepResult(success=True, summary=f"Created PR #{pr_info.number}")
        except RuntimeError as exc:
            return StepResult(success=False, summary="Failed to create PR", error=str(exc))

    async def _generate_pr_body(self, ctx: ExecutionContext, task_title: str) -> str:
        log_result = await run(
            "git", "log", f"{ctx.base_branch}..HEAD", "--format=%h %s%n%b", "--no-merges",
            cwd=ctx.working_dir,
        )
        diff_result = await run(
            "git", "diff", f"{ctx.base_branch}..HEAD", "--stat",
            cwd=ctx.working_dir,
        )

        issue_body = ctx.task.body if ctx.task else ""
        commit_log = log_result.stdout.strip() if log_result.success else "(unavailable)"
        diff_stat = diff_result.stdout.strip() if diff_result.success else "(unavailable)"

        prompt = _PR_BODY_PROMPT.format(
            issue_number=ctx.issue_number,
            issue_title=task_title,
            issue_body=issue_body or "(no description)",
            commit_log=commit_log,
            diff_stat=diff_stat,
        )

        try:
            result = await invoke(prompt, model="sonnet", cwd=ctx.working_dir, timeout=120)
            ctx.add_cost(result.cost_usd)
            body = result.text
            if f"#{ctx.issue_number}" not in body:
                body += f"\n\nCloses #{ctx.issue_number}"
            return body
        except RuntimeError:
            log.warning("step.create_pr.body_generation_failed", fallback="minimal")
            return f"Closes #{ctx.issue_number}"

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Gate: PR number must have been extracted."""
        if ctx.pr_number is not None:
            return GateCheckResult(passed=True)
        return GateCheckResult(passed=False, reason="No PR number after create_pr step")

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        # Skip if PR already exists (resumed run)
        return ctx.pr_number is not None
