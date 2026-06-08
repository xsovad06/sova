"""Step 11: Wait for external reviews -- poll until configured tools complete."""

from __future__ import annotations

import asyncio

from sova.adapters.external_reviews import get_check_statuses
from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.utils.logging import get_logger

log = get_logger(component="step.wait_external_reviews")


class WaitForExternalReviewsStep(BaseStep):
    name = "wait_for_external_reviews"
    max_retries = 0

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        if ctx.pr_number is None:
            return StepResult(success=False, summary="No PR to monitor", error="pr_number is None")

        ext = ctx.config.external_reviews
        tools = ext.tools
        poll_interval = ext.poll_interval
        max_wait = ext.timeout * 60
        elapsed = 0

        log.info(
            "step.wait_external_reviews.start",
            pr=ctx.pr_number,
            tools=tools,
            timeout_min=ext.timeout,
        )

        statuses = []
        while elapsed < max_wait:
            statuses = await get_check_statuses(
                ctx.pr_number,
                repo=ctx.repo,
                tools=tools,
                github_user=ctx.config.github_user,
            )

            all_completed = all(s.completed for s in statuses)
            if all_completed:
                names = ", ".join(s.name for s in statuses)
                log.info("step.wait_external_reviews.all_completed", checks=names)
                return StepResult(
                    success=True,
                    summary=f"External reviews completed: {names}",
                )

            pending = [s.name for s in statuses if not s.completed]
            log.debug(
                "step.wait_external_reviews.waiting",
                pending=pending,
                elapsed=elapsed,
            )
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        pending = [s.name for s in statuses if not s.completed]
        log.warning(
            "step.wait_external_reviews.timeout",
            pending=pending,
            elapsed=elapsed,
        )
        return StepResult(
            success=True,
            summary=f"External review wait timed out after {ext.timeout}min, proceeding",
        )

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        # Wait step does not modify code -- gate passes if PR exists
        if ctx.pr_number is not None:
            return GateCheckResult(passed=True)
        return GateCheckResult(passed=False, reason="No PR number after wait step")

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        if self.name in ctx.completed_steps:
            return True
        ext = ctx.config.external_reviews
        return not ext.enabled or not ext.tools
