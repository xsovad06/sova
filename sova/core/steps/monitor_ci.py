"""Step 8: Monitor CI -- poll CI checks until they complete."""

from __future__ import annotations

import asyncio

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.git.operations import get_ci_checks
from sova.utils.logging import get_logger

log = get_logger(component="step.monitor_ci")


class MonitorCIStep(BaseStep):
    name = "monitor_ci"
    max_retries = 0

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        if ctx.pr_number is None:
            return StepResult(success=False, summary="No PR to monitor", error="pr_number is None")

        poll_interval = ctx.config.ci.poll_interval
        max_wait = ctx.config.ci.max_wait
        elapsed = 0

        log.info("step.monitor_ci", pr=ctx.pr_number, max_wait=max_wait)

        while elapsed < max_wait:
            checks = await get_ci_checks(ctx.pr_number, repo=ctx.repo)

            if not checks:
                log.debug("step.monitor_ci.no_checks", elapsed=elapsed)
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
                continue

            all_completed = all(c.is_completed for c in checks)
            if all_completed:
                failed = [c for c in checks if not c.is_passed]
                if failed:
                    names = ", ".join(c.name for c in failed)
                    return StepResult(success=False, summary=f"CI failed: {names}", error=f"Failed checks: {names}")
                return StepResult(success=True, summary=f"All {len(checks)} CI checks passed")

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        return StepResult(success=False, summary="CI monitoring timed out", error=f"Timed out after {max_wait}s")

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        return GateCheckResult(passed=True)

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        return False
