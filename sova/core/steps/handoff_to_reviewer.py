"""Step: Handoff to Reviewer: write handoff for the Reviewer role to pick up.

Terminal step of both the developer pipeline (first review of a new PR) and
the address-review pipeline (re-review of the head that carries the fixes).
The re-review closes the loop autonomously: the sova_reviewed integration gate
needs an approving verdict on the current head, and an address cycle alone
only ever yields "addressed". The Reviewer -> Developer direction is bounded
by pipeline.max_address_review_cycles in _process_auto_handoff().
"""

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
        rereview = ctx.pipeline_variant == "address_review"
        log.info(
            "step.handoff_to_reviewer",
            label=label,
            pr=ctx.pr_number,
            confidence=ctx.confidence_score,
            rereview=rereview,
        )

        auto = ctx.config.pipeline.auto_handoff
        review_description = (
            f"Spawn Reviewer agent to re-review PR #{ctx.pr_number} after the address cycle"
            if rereview
            else f"Spawn Reviewer agent to review PR #{ctx.pr_number}"
        )
        actions: list[HandoffAction] = []

        confidence = ctx.config.confidence
        score = ctx.confidence_score
        if confidence.gate_enabled and score is not None:
            note = ""
            if score < confidence.critical_threshold:
                auto = False
                note = ": critical risk, human input needed"
            elif score < confidence.review_threshold:
                auto = False
                note = ": below review threshold, human input needed"
            elif score >= confidence.auto_merge_threshold:
                # The human picks between review and the fast path, so the
                # review action must not stay on auto: _process_auto_handoff()
                # spawns it and clears the handoff file immediately, and the
                # integrate action below would never reach the dashboard.
                auto = False
                note = ": high-confidence change, review or integrate"
                actions.append(
                    HandoffAction(
                        id="integrate",
                        label="Integrate PR",
                        description=(
                            f"Confidence score {score}/100: auto-merge candidate. Skips review if you trust the score."
                        ),
                        style="approve",
                        mode="claude-command",
                        command=f"/integrate-pr {ctx.pr_number}",
                        args={"issue": ctx.issue_number, "pr": ctx.pr_number},
                        auto_execute=False,
                    ),
                )
            review_description += f" (confidence score {score}/100{note})"

        actions.append(
            HandoffAction(
                id="review",
                label="Review PR",
                description=review_description,
                style="approve",
                mode="agent",
                command="",
                args={"issue": ctx.issue_number, "pr": ctx.pr_number, "role": "reviewer"},
                auto_execute=auto,
            ),
        )

        if rereview:
            addressed = len(ctx.addressed_review_findings)
            phase = "address_review"
            summary = f"PR #{ctx.pr_number} findings addressed ({addressed}), handing to Reviewer for re-review"
            agent_summary = f"Review findings addressed for {label}, PR #{ctx.pr_number} ready for re-review"
            notification_message = f"PR #{ctx.pr_number} findings addressed, handing to Reviewer"
            notification_subtitle = f"Address review finished {label}"
            result_summary = f"Handed off to Reviewer for re-review (PR #{ctx.pr_number})"
        else:
            phase = "develop"
            summary = f"PR #{ctx.pr_number} ready for review (CI passed)"
            agent_summary = f"Development complete for {label}, PR #{ctx.pr_number} created with passing CI"
            notification_message = f"PR #{ctx.pr_number} passed CI, handing to Reviewer"
            notification_subtitle = f"Developer finished {label}"
            result_summary = f"Handed off to Reviewer (PR #{ctx.pr_number})"

        return await write_step_handoff(
            ctx,
            role="developer",
            phase=phase,
            summary=summary,
            agent_summary=agent_summary,
            next_action="review",
            actions=actions,
            notification_message=notification_message,
            notification_subtitle=notification_subtitle,
            result_summary=result_summary,
        )

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        return GateCheckResult(passed=True)

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        return self.name in ctx.completed_steps
