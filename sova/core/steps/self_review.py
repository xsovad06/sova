"""Step 5b: Self-review -- review own changes before pushing."""

from __future__ import annotations

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.llm.client import invoke_command
from sova.utils.logging import get_logger

log = get_logger(component="step.self_review")


class SelfReviewStep(BaseStep):
    name = "self_review"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.self_review", cwd=str(ctx.working_dir))

        try:
            result = await invoke_command(
                "/review",
                model=ctx.config.agent.model,
                cwd=ctx.working_dir,
                max_budget_usd=ctx.config.agent.max_budget - ctx.cost_usd,
            )
            ctx.add_cost(result.cost_usd)
            return StepResult(success=True, summary="Self-review completed", cost_usd=float(result.cost_usd))
        except RuntimeError as exc:
            return StepResult(success=False, summary="Self-review failed", error=str(exc))

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Gate: commits must still exist after review (review may rearrange commits)."""
        from sova.utils.shell import run

        log_result = await run("git", "log", f"{ctx.base_branch}..HEAD", "--oneline", cwd=ctx.working_dir)
        has_commits = bool(log_result.success and log_result.stdout.strip())
        if has_commits:
            return GateCheckResult(passed=True)

        diff_result = await run("git", "diff", "--stat", "HEAD", cwd=ctx.working_dir)
        staged = await run("git", "diff", "--cached", "--stat", cwd=ctx.working_dir)
        has_changes = bool(
            (diff_result.success and diff_result.stdout.strip()) or (staged.success and staged.stdout.strip())
        )
        if has_changes:
            return GateCheckResult(passed=False, reason="Commits lost during review but uncommitted changes remain")
        return GateCheckResult(passed=False, reason="All commits and changes lost during review")

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        return self.name in ctx.completed_steps or not ctx.config.review.enabled
