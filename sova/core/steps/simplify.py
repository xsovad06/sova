"""Step 5: Simplify -- code quality pass via Claude CLI."""

from __future__ import annotations

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.llm.client import invoke_command
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="step.simplify")


class SimplifyStep(BaseStep):
    name = "simplify"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.simplify", cwd=str(ctx.working_dir))

        try:
            result = await invoke_command(
                "/simplify",
                model=ctx.config.agent.model,
                cwd=ctx.working_dir,
                max_budget_usd=ctx.config.agent.max_budget - ctx.cost_usd,
            )
            ctx.add_cost(result.cost_usd)
            return StepResult(success=True, summary="Simplification pass completed", cost_usd=float(result.cost_usd))
        except RuntimeError as exc:
            return StepResult(success=False, summary="Simplification failed", error=str(exc))

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Gate: changes must still exist after simplification."""
        diff_result = await run("git", "diff", "--stat", "HEAD", cwd=ctx.working_dir)
        # Allow both staged and unstaged changes
        staged = await run("git", "diff", "--cached", "--stat", cwd=ctx.working_dir)
        has_changes = bool(
            (diff_result.success and diff_result.stdout.strip()) or (staged.success and staged.stdout.strip())
        )
        # Also check commits ahead of base
        log_result = await run("git", "log", f"{ctx.config.base_branch}..HEAD", "--oneline", cwd=ctx.working_dir)
        has_commits = bool(log_result.success and log_result.stdout.strip())

        if has_changes or has_commits:
            return GateCheckResult(passed=True)
        return GateCheckResult(passed=False, reason="All changes were reverted during simplification")

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        return self.name in ctx.completed_steps
