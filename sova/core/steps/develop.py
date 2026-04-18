"""Step 4: Develop -- invoke Claude CLI for TDD implementation.

This is the critical step where actual code is written. The gate check
ensures that development actually produced code changes (preventing the
issue #60 failure mode where the agent ran through the entire pipeline
with zero changes).
"""

from __future__ import annotations

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.llm.client import invoke_command
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="step.develop")


class DevelopStep(BaseStep):
    name = "develop"
    max_retries = 1

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.develop", issue=ctx.issue_number, cwd=str(ctx.working_dir))

        try:
            result = await invoke_command(
                "/develop",
                args=ctx.issue_number,
                model=ctx.config.agent.model,
                cwd=ctx.working_dir,
                max_budget_usd=ctx.config.agent.max_budget - ctx.cost_usd,
            )
            ctx.add_cost(result.cost_usd)
            ctx.session_id = result.session_id

            return StepResult(
                success=True,
                summary=f"Development completed ({result.total_tokens} tokens)",
                cost_usd=float(result.cost_usd),
            )
        except RuntimeError as exc:
            return StepResult(success=False, summary="Development failed", error=str(exc))

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Gate: development must produce actual code changes."""
        diff_result = await run("git", "diff", "--stat", "HEAD", cwd=ctx.working_dir)

        if not diff_result.success or not diff_result.stdout.strip():
            return GateCheckResult(
                passed=False,
                reason="Development produced no code changes",
            )
        return GateCheckResult(passed=True)

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        return False
