"""Step 2: Assess -- verify the issue is ready for development.

Gate 3: The Developer agent refuses to pick up any issue not in
"Researched" state. This prevents the old failure mode where the agent
blindly started work on underspecified issues.

Also guards against duplicate developer runs when a PR already exists
for the issue (the correct action is address-review, not a fresh run).
"""

from __future__ import annotations

from sova.adapters.base import TaskState
from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.git.pr import find_pr_for_issue
from sova.utils.logging import get_logger

log = get_logger(component="step.assess")

_READY_STATES = frozenset({TaskState.RESEARCHED, TaskState.IN_PROGRESS})


class AssessStep(BaseStep):
    name = "assess"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        task = await ctx.adapter.get_task(ctx.issue_number)
        ctx.task = task
        state = await ctx.adapter.get_state(ctx.issue_number)

        log.info("step.assess", issue=ctx.issue_number, tracker_state=state)

        if state not in _READY_STATES:
            return StepResult(
                success=False,
                summary=f"Issue #{ctx.issue_number} is in {state} state, not ready for development",
                error=f"Issue must be in {', '.join(_READY_STATES)} state (current: {state})",
            )

        if not ctx.pr_number and not ctx.force:
            existing_pr = await find_pr_for_issue(
                ctx.issue_number,
                repo=ctx.repo,
                github_user=ctx.config.github_user,
            )
            if existing_pr:
                log.warning(
                    "step.assess.pr_exists",
                    issue=ctx.issue_number,
                    pr=existing_pr.number,
                )
                return StepResult(
                    success=False,
                    summary=f"PR #{existing_pr.number} already exists for issue #{ctx.issue_number}",
                    error=(
                        f"Open PR #{existing_pr.number} already exists. "
                        "Use address-review pipeline (pass --pr) instead of a fresh developer run. "
                        "Use --force to override."
                    ),
                )

        return StepResult(success=True, summary=f"Issue #{ctx.issue_number} is in {state} state")

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        return GateCheckResult(passed=True)

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        return self.name in ctx.completed_steps or ctx.force or not ctx.has_issue
