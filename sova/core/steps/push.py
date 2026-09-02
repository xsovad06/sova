"""Step 6: Push -- push branch to remote after validating."""

from __future__ import annotations

from sova.core.context import BUDGET_STOP_RETRY_THRESHOLD, ExecutionContext
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

        force = ctx.pr_number is not None
        # ValidateStep may give up fixing pre-push hook failures once budget
        # is nearly exhausted (see BUDGET_STOP_RETRY_THRESHOLD) without ever
        # disabling the hook. Without matching that threshold here, this
        # push would hit the same still-failing local hook and abort,
        # silently defeating ValidateStep's graceful degradation.
        no_verify = ctx.budget_remaining_fraction < BUDGET_STOP_RETRY_THRESHOLD
        if no_verify:
            log.warning("step.push.budget_skip_hooks", fraction=ctx.budget_remaining_fraction)
        try:
            await git_ops.push(
                ctx.branch_name,
                force=force,
                set_upstream=True,
                cwd=ctx.working_dir,
                no_verify=no_verify,
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
