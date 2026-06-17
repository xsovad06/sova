"""Step: Handoff to Reviewer -- write handoff for the Reviewer role to pick up."""

from __future__ import annotations

from sova.core.context import ExecutionContext
from sova.core.steps._handoff_helpers import write_step_handoff
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.ipc.handoff import HandoffAction
from sova.utils.logging import get_logger

log = get_logger(component="step.handoff_to_reviewer")


class HandoffToReviewerStep(BaseStep):
    name = "handoff_to_reviewer"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        label = ctx.display_label
        log.info("step.handoff_to_reviewer", label=label, pr=ctx.pr_number)

        auto = ctx.config.pipeline.auto_handoff

        return await write_step_handoff(
            ctx,
            role="developer",
            phase="develop",
            summary=f"PR #{ctx.pr_number} ready for review (CI passed)",
            agent_summary=(f"Development complete for {label}, PR #{ctx.pr_number} created with passing CI"),
            next_action="review",
            actions=[
                HandoffAction(
                    id="review",
                    label="Review PR",
                    description=f"Spawn Reviewer agent to review PR #{ctx.pr_number}",
                    style="approve",
                    mode="agent",
                    command="",
                    args={"issue": ctx.issue_number, "pr": ctx.pr_number, "role": "reviewer"},
                    auto_execute=auto,
                ),
            ],
            notification_message=f"PR #{ctx.pr_number} passed CI, handing to Reviewer",
            notification_subtitle=f"Developer finished {label}",
            result_summary=f"Handed off to Reviewer (PR #{ctx.pr_number})",
        )

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        return GateCheckResult(passed=True)

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        return self.name in ctx.completed_steps
