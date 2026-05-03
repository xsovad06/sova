"""Step: Handoff to Reviewer -- write handoff for the Reviewer role to pick up."""

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

log = get_logger(component="step.handoff_to_reviewer")


class HandoffToReviewerStep(BaseStep):
    name = "handoff_to_reviewer"

    async def execute(self, ctx: ExecutionContext) -> StepResult:
        log.info("step.handoff_to_reviewer", issue=ctx.issue_number, pr=ctx.pr_number)

        agent_handoff = AgentHandoff(
            role="developer",
            phase="develop",
            summary=f"Development complete for issue #{ctx.issue_number}, PR #{ctx.pr_number} created with passing CI",
            next_action="review",
            pr_number=ctx.pr_number,
            branch_name=ctx.branch_name,
        )

        if ctx.task_run_id:
            try:
                await write_handoff(ctx.task_run_id, agent_handoff)
            except Exception:
                log.warning("step.handoff_to_reviewer.db_failed", exc_info=True)

        dashboard_handoff = DashboardHandoff(
            source="developer",
            status="awaiting_action",
            issue=ctx.issue_number,
            pr_number=ctx.pr_number,
            branch=ctx.branch_name,
            summary=f"PR #{ctx.pr_number} ready for review (CI passed)",
            details={"next_action": "review", "cost_usd": str(ctx.cost_usd)},
            next_actions=[
                HandoffAction(
                    id="review",
                    label="Review PR",
                    description=f"Spawn Reviewer agent to review PR #{ctx.pr_number}",
                    style="approve",
                    mode="agent",
                    command="",
                    args={"issue": ctx.issue_number, "pr": ctx.pr_number, "role": "reviewer"},
                    auto_execute=True,
                ),
            ],
        )

        try:
            write_handoff_file(ctx.project_dir, dashboard_handoff)
        except Exception:
            log.warning("step.handoff_to_reviewer.file_failed", exc_info=True)

        project_name = ctx.project_dir.name
        notify(
            ctx.config.notification,
            "SOVA",
            f"{project_name} | PR #{ctx.pr_number} passed CI, handing to Reviewer",
            subtitle=f"Developer finished #{ctx.issue_number}",
            group=f"sova-{ctx.issue_number}",
        )

        return StepResult(
            success=True,
            summary=f"Handed off to Reviewer (PR #{ctx.pr_number})",
        )

    async def validate_output(self, ctx: ExecutionContext) -> GateCheckResult:
        return GateCheckResult(passed=True)

    async def can_skip(self, ctx: ExecutionContext) -> bool:
        return self.name in ctx.completed_steps
