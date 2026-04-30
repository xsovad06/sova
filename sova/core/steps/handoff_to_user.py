"""Step: Handoff to User -- notify user that the PR is ready for human review."""

from __future__ import annotations

from sova.core.context import ExecutionContext
from sova.core.steps.base import BaseStep, GateCheckResult, StepResult
from sova.ipc.handoff import (
    AgentHandoff,
    DashboardHandoff,
    HandoffAction,
    write_handoff,
    write_handoff_file,
)
from sova.ipc.notifications import notify
from sova.utils.logging import get_logger

log = get_logger(component="step.handoff_to_user")


class HandoffToUserStep(BaseStep):
    name = "handoff_to_user"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.handoff_to_user", issue=ctx.issue_number, pr=ctx.pr_number)

        agent_handoff = AgentHandoff(
            role="developer",
            phase="address_review",
            summary=f"Review findings addressed for PR #{ctx.pr_number}, ready for human review",
            next_action="integrate",
            needs_human=True,
            human_message=f"PR #{ctx.pr_number} has been reviewed and findings addressed. Ready for your review.",
            pr_number=ctx.pr_number,
            branch_name=ctx.branch_name,
        )

        if ctx.task_run_id:
            try:
                await write_handoff(ctx.task_run_id, agent_handoff)
            except Exception:
                log.warning("step.handoff_to_user.db_failed", exc_info=True)

        dashboard_handoff = DashboardHandoff(
            source="developer",
            status="awaiting_action",
            issue=ctx.issue_number,
            pr_number=ctx.pr_number,
            branch=ctx.branch_name,
            summary=f"PR #{ctx.pr_number} reviewed and findings addressed -- ready for human review",
            details={"next_action": "integrate", "cost_usd": str(ctx.cost_usd)},
            next_actions=[
                HandoffAction(
                    id="integrate",
                    label="Integrate PR",
                    description="Rebase, merge, cleanup, and learn",
                    style="approve",
                    mode="claude-command",
                    command=f"/integrate-pr {ctx.pr_number}",
                    args={"issue": ctx.issue_number, "pr": ctx.pr_number},
                ),
                HandoffAction(
                    id="approve",
                    label="Merge Only",
                    description="Squash merge without rebase or learning",
                    style="neutral",
                    mode="claude-command",
                    command=f"/approve-merge {ctx.pr_number}",
                    args={"issue": ctx.issue_number, "pr": ctx.pr_number},
                ),
            ],
        )

        try:
            write_handoff_file(ctx.project_dir, dashboard_handoff)
        except Exception:
            log.warning("step.handoff_to_user.file_failed", exc_info=True)

        notify(
            ctx.config.notification,
            f"SOVA -- #{ctx.issue_number} ready for your review",
            f"PR #{ctx.pr_number} reviewed and findings addressed. Integrate when ready.",
        )

        return StepResult(
            success=True,
            summary=f"PR #{ctx.pr_number} ready for human review",
        )

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        return GateCheckResult(passed=True)

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        return self.name in ctx.completed_steps
