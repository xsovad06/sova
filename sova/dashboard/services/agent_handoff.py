"""Auto-handoff orchestration after agent completion."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from sova.dashboard.services.agent_pool import AgentState

log = get_logger(component="dashboard.control.handoff")


async def _count_address_review_runs(issue: str, pr_number: int, project_dir: Path) -> int:
    """Count completed address-review runs for the given issue+PR.

    Address-review runs are developer runs with a pr_number set (developer
    runs without pr_number are initial development, not address-review).
    """
    from sqlalchemy import func, select

    from sova.db.models import TaskRun
    from sova.db.session import get_session

    terminal = {"done", "failed", "rejected", "interrupted", "paused"}

    async with await get_session(project_dir=project_dir) as session:
        stmt = (
            select(func.count())
            .select_from(TaskRun)
            .where(
                TaskRun.issue_number == issue,
                TaskRun.role == "developer",
                TaskRun.pr_number == pr_number,
                TaskRun.status.in_(terminal),
            )
        )
        result = await session.execute(stmt)
        return result.scalar_one()


async def _check_address_review_circuit_breaker(
    issue: str, pr_number: int | None, role: str | None, project_dir: Path
) -> str | None:
    """Check if the address-review circuit breaker should block auto-execution.

    Returns a reason string if blocked, None if clear.
    """
    if role != "developer" or pr_number is None:
        return None

    from sova.config.loader import load_config

    cfg = load_config(project_dir)
    max_cycles = cfg.pipeline.max_address_review_cycles
    if max_cycles <= 0:
        return None

    count = await _count_address_review_runs(issue, pr_number, project_dir)
    if count >= max_cycles:
        return (
            f"Circuit breaker: {count} address-review cycles completed for "
            f"PR #{pr_number} on issue #{issue}. "
            f"Max allowed: {max_cycles}. Manual action required."
        )

    return None


async def _process_auto_handoff(agent: AgentState) -> None:
    """Check for auto-executable handoff actions after an agent completes.

    Reads the handoff file and auto-triggers the first action marked
    with auto_execute=True. This enables role chaining (e.g., Developer
    hands off to Reviewer automatically after CI passes).
    """
    try:
        from sova.dashboard.services import agent_lifecycle, handoff_service
        from sova.ipc.handoff import read_handoff_file

        handoff = read_handoff_file(agent.project_dir, issue=agent.issue)
        if handoff is None or handoff.status != "awaiting_action":
            return

        h_issue = str(handoff.issue).lstrip("#").strip() if handoff.issue else ""
        a_issue = str(agent.issue).lstrip("#").strip() if agent.issue else ""
        if h_issue and a_issue and h_issue != a_issue:
            log.info(
                "auto_handoff.issue_mismatch",
                run_id=agent.run_id,
                agent_issue=agent.issue,
                handoff_issue=handoff.issue,
            )
            return

        for action in handoff.next_actions:
            if not action.auto_execute:
                continue

            # Check circuit breaker for address-review spawns
            if action.mode == "agent":
                args = action.args or {}
                raw_pr = args.get("pr") or handoff.pr_number
                target_role = args.get("role")
                target_issue = str(args.get("issue", handoff.issue))
                pr_num = int(raw_pr) if raw_pr is not None else None

                reason = await _check_address_review_circuit_breaker(
                    target_issue, pr_num, target_role, agent.project_dir
                )
                if reason:
                    log.warning(
                        "auto_handoff.circuit_breaker",
                        run_id=agent.run_id,
                        issue=target_issue,
                        pr_number=pr_num,
                        reason=reason,
                    )
                    # Write a manual-only handoff so the dashboard shows the blocked state
                    from sova.ipc.handoff import DashboardHandoff, HandoffAction, write_handoff_file

                    blocked_handoff = DashboardHandoff(
                        source="circuit_breaker",
                        status="awaiting_action",
                        issue=handoff.issue,
                        pr_number=handoff.pr_number,
                        branch=handoff.branch,
                        summary=reason,
                        next_actions=[
                            HandoffAction(
                                id="address_review",
                                label="Address Review (force)",
                                mode="agent",
                                args=args,
                                auto_execute=False,
                            ),
                            HandoffAction(
                                id="integrate",
                                label="Integrate PR",
                                mode="claude-command",
                                command=f"/integrate-pr {pr_num}" if pr_num else "/integrate-pr",
                                auto_execute=False,
                            ),
                        ],
                    )
                    handoff_service.clear_handoff(agent.project_dir, issue=agent.issue)
                    write_handoff_file(agent.project_dir, blocked_handoff)
                    return

            log.info(
                "auto_handoff.executing",
                run_id=agent.run_id,
                action_id=action.id,
                mode=action.mode,
                issue=handoff.issue,
            )

            handoff_service.clear_handoff(agent.project_dir, issue=agent.issue)

            if action.mode == "agent":
                args = action.args or {}
                raw_pr = args.get("pr") or handoff.pr_number
                result = await agent_lifecycle.start_agent(
                    str(args.get("issue", handoff.issue)),
                    role=args.get("role"),
                    pr_number=int(raw_pr) if raw_pr is not None else None,
                    slug=None,
                )
                log.info("auto_handoff.agent_started", result=result)
            elif action.mode == "claude-command":
                cmd = action.command.lstrip("/").split()[0] if action.command else ""
                if cmd:
                    result = await agent_lifecycle.start_command(cmd, action.args, slug=None)
                    log.info("auto_handoff.command_started", result=result)
            else:
                log.warning("auto_handoff.unsupported_mode", mode=action.mode)

            return  # only execute the first auto action

    except Exception:
        log.warning("auto_handoff.failed", run_id=agent.run_id, exc_info=True)
