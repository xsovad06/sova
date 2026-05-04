"""Shared logic for handoff steps (to reviewer, to user)."""

from __future__ import annotations

from sova.core.context import ExecutionContext
from sova.core.steps.base import StepResult
from sova.ipc.handoff import (
    AgentHandoff,
    DashboardHandoff,
    HandoffAction,
    write_handoff,
    write_handoff_file,
)
from sova.ipc.notifications import notify
from sova.utils.logging import get_logger

log = get_logger(component="step.handoff")


async def write_step_handoff(
    ctx: ExecutionContext,
    *,
    role: str,
    phase: str,
    summary: str,
    next_action: str,
    actions: list[HandoffAction],
    notification_message: str,
    notification_subtitle: str,
    result_summary: str,
    agent_summary: str | None = None,
    needs_human: bool = False,
    human_message: str | None = None,
) -> StepResult:
    """Write both DB and file handoffs, send notification, return StepResult."""
    agent_handoff = AgentHandoff(
        role=role,
        phase=phase,
        summary=agent_summary or summary,
        next_action=next_action,
        needs_human=needs_human,
        human_message=human_message or "",
        pr_number=ctx.pr_number,
        branch_name=ctx.branch_name,
    )

    if ctx.task_run_id:
        try:
            await write_handoff(ctx.task_run_id, agent_handoff)
        except Exception:
            log.warning("step.handoff.db_failed", exc_info=True)

    dashboard_handoff = DashboardHandoff(
        source=role,
        status="awaiting_action",
        issue=ctx.issue_number,
        pr_number=ctx.pr_number,
        branch=ctx.branch_name,
        summary=summary,
        details={"next_action": next_action, "cost_usd": str(ctx.cost_usd)},
        next_actions=actions,
    )

    try:
        write_handoff_file(ctx.project_dir, dashboard_handoff)
    except Exception:
        log.warning("step.handoff.file_failed", exc_info=True)

    project_name = ctx.project_dir.name
    notify(
        ctx.config.notification,
        "SOVA",
        f"{project_name} | {notification_message}",
        subtitle=notification_subtitle,
        group=f"sova-{ctx.issue_number}",
    )

    return StepResult(success=True, summary=result_summary)
