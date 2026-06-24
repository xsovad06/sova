"""Step: Handoff to User -- notify user that the PR is ready for human review."""

from __future__ import annotations

from sova.core.context import ExecutionContext
from sova.core.steps._handoff_helpers import write_step_handoff
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.ipc.handoff import HandoffAction
from sova.utils.logging import get_logger

log = get_logger(component="step.handoff_to_user")


class HandoffToUserStep(BaseStep):
    name = "handoff_to_user"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        label = ctx.display_label
        log.info("step.handoff_to_user", label=label, pr=ctx.pr_number)

        return await write_step_handoff(
            ctx,
            role="developer",
            phase="address_review",
            summary=f"PR #{ctx.pr_number} reviewed and findings addressed -- ready for human review",
            agent_summary=f"Review findings addressed for PR #{ctx.pr_number}, ready for human review",
            next_action="integrate",
            needs_human=True,
            human_message=f"PR #{ctx.pr_number} has been reviewed and findings addressed. Ready for your review.",
            actions=[
                HandoffAction(
                    id="integrate",
                    label="Integrate PR",
                    description="Rebase, merge, cleanup, and learn",
                    style="approve",
                    mode="claude-command",
                    command=f"/integrate-pr {ctx.pr_number}",
                    args={"issue": ctx.issue_number, "pr": ctx.pr_number},
                ),
            ],
            notification_message=f"PR #{ctx.pr_number} reviewed, ready for integration",
            notification_subtitle=f"Reviewer finished {label}",
            result_summary=f"PR #{ctx.pr_number} ready for human review",
        )

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        return GateCheckResult(passed=True)

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        return self.name in ctx.completed_steps
