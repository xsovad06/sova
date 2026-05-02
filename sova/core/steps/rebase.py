"""Step: Rebase -- rebase feature branch onto latest base, resolving conflicts via LLM."""

from __future__ import annotations

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.git.operations import rebase_with_conflict_resolution
from sova.utils.logging import get_logger

log = get_logger(component="step.rebase")


class RebaseStep(BaseStep):
    name = "rebase"
    max_retries = 0

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.rebase", branch=ctx.branch_name, base=ctx.base_branch)

        try:
            result, cost = await rebase_with_conflict_resolution(
                ctx.base_branch,
                cwd=ctx.working_dir,
                model=ctx.config.agent.model,
                max_budget_usd=ctx.config.agent.max_budget - ctx.cost_usd,
            )
        except RuntimeError as exc:
            return StepResult(success=False, summary="Rebase failed", error=str(exc))

        ctx.add_cost(cost)

        if result.success:
            summary = f"Rebased onto {ctx.base_branch}"
            if result.conflicts_resolved:
                summary += f" ({result.conflicts_resolved} conflicts resolved)"
            return StepResult(success=True, summary=summary, cost_usd=float(cost))

        return StepResult(success=False, summary="Rebase failed", error=result.error)

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Gate: no rebase-in-progress markers should remain."""
        rebase_dir = ctx.working_dir / ".git" / "rebase-merge"
        rebase_apply = ctx.working_dir / ".git" / "rebase-apply"
        if rebase_dir.exists() or rebase_apply.exists():
            return GateCheckResult(passed=False, reason="Rebase still in progress")
        return GateCheckResult(passed=True)

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        return self.name in ctx.completed_steps
