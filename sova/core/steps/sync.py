"""Step 1: Sync -- pull latest changes on the base branch."""

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
            return StepResult(success=True, summary=f"Synced {base}")
        except RuntimeError as exc:
            return StepResult(success=False, summary="Failed to sync", error=str(exc))

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        return GateCheckResult(passed=True)

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        return self.name in ctx.completed_steps
