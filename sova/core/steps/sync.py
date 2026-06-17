"""Step 1: Sync -- pull latest changes on the base branch.

Also fetches the task from the tracker so ctx.task is populated for
downstream steps (commit message, PR title) even when --force skips
the assess step.
"""

from __future__ import annotations

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.git import operations as git_ops
from sova.utils.logging import get_logger

log = get_logger(component="step.sync")


class SyncStep(BaseStep):
    name = "sync"
    max_retries = 1

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        base = ctx.config.base_branch
        log.info("step.sync", base_branch=base)
        try:
            await git_ops.sync_branch(base, cwd=ctx.project_dir)
        except RuntimeError as exc:
            return StepResult(success=False, summary="Failed to sync", error=str(exc))

        if ctx.task is None and ctx.has_issue:
            try:
                ctx.task = await ctx.adapter.get_task(ctx.issue_number)
            except Exception:
                log.warning("step.sync.task_fetch_failed", issue=ctx.issue_number, exc_info=True)

        return StepResult(success=True, summary=f"Synced {base}")

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        return GateCheckResult(passed=True)
