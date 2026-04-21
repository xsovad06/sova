"""Step 6: Push -- push branch to remote after validating."""

from __future__ import annotations

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.git import operations as git_ops
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="step.push")


class PushStep(BaseStep):
    name = "push"
    max_retries = 1

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.push", branch=ctx.branch_name, cwd=str(ctx.working_dir))

        try:
            await git_ops.push(
                ctx.branch_name,
                set_upstream=True,
                cwd=ctx.working_dir,
            )
            return StepResult(success=True, summary=f"Pushed {ctx.branch_name}")
        except RuntimeError as exc:
            return StepResult(success=False, summary="Push failed", error=str(exc))

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        """Gate: branch must have commits ahead of base."""
        result = await run("git", "rev-list", "--count", f"{ctx.base_branch}..HEAD", cwd=ctx.working_dir)
        if not result.success:
            return GateCheckResult(passed=False, reason="Failed to count commits ahead of base")

        try:
            count = int(result.stdout.strip() or "0")
        except ValueError:
            return GateCheckResult(passed=False, reason=f"Unexpected rev-list output: {result.stdout[:100]}")
        if count == 0:
            return GateCheckResult(passed=False, reason="No commits ahead of base branch")
        return GateCheckResult(passed=True)
