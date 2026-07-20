"""Auto-handoff orchestration after agent completion."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sova.dashboard.services.agent_validation import check_memory_pressure
from sova.dashboard.services.feed_service import emit_safe
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from sova.dashboard.services.agent_pool import AgentState
    from sova.ipc.handoff import DashboardHandoff

log = get_logger(component="dashboard.control.handoff")


async def _count_address_review_runs(issue: str, pr_number: int, project_dir: Path) -> int:
    """Count completed address-review runs for the given issue+PR.

    Only counts runs that actually executed the address-review pipeline
    (identified by having an ``address_review`` StepExecution).  The
    initial developer run also acquires ``pr_number`` mid-pipeline via
    ``_sync_task_run_context()`` after CreatePRStep, so filtering on
    ``pr_number`` alone would include it and trigger the breaker one
    cycle too early.

    NOTE: This relies on ``StepExecution.step_name == "address_review"``
    matching the name used by ``AddressReviewStep`` in
    ``sova.core.steps.address_review``.  If that step is renamed, this
    query must be updated to match.
    """
    from sqlalchemy import func, select

    from sova.core.state import TASK_RUN_TERMINAL
    from sova.db.models import StepExecution, TaskRun
    from sova.db.session import get_session

    async with await get_session(project_dir=project_dir) as session:
        stmt = (
            select(func.count(TaskRun.id.distinct()))
            .join(StepExecution, StepExecution.task_run_id == TaskRun.id)
            .where(
                StepExecution.step_name == "address_review",
                TaskRun.issue_number == issue,
                TaskRun.role == "developer",
                TaskRun.pr_number == pr_number,
                TaskRun.status.in_(TASK_RUN_TERMINAL),
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


async def _persist_completing_agent_handoff(run_id: int, handoff: "DashboardHandoff", project_dir: "Path") -> None:
    """Persist handoff details to the completing agent's TaskRun.handoff_json.

    Called from _process_auto_handoff before the file is cleared. This backstops
    write_handoff() in the subprocess, which may write to the wrong DB when the
    subprocess CWD is a linked worktree rather than the project root. Persisting
    here (dashboard context with the correct project_dir) ensures
    get_sova_review_verdict() can always find the real verdict.

    Only writes if handoff_json is not already set (subprocess may have written it
    correctly when the CWD worktree fix is in place).
    """
    try:
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                task_run = await session.get(TaskRun, run_id)
                if task_run is not None and not task_run.handoff_json and handoff.details:
                    task_run.handoff_json = handoff.details
                    log.info("auto_handoff.handoff_json_persisted", run_id=run_id, source=handoff.source)
    except Exception:
        log.warning("auto_handoff.handoff_json_persist_failed", run_id=run_id, exc_info=True)


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

        # Persist handoff details to the completing agent's TaskRun before clearing
        # the file. Done after the mismatch guard to avoid writing a mismatched
        # issue's verdict into the completing run's handoff_json. This backstops
        # write_handoff() in the subprocess, which may write to the wrong DB when
        # the subprocess CWD is a linked worktree rather than the project root.
        if agent.run_id is not None and handoff.details:
            await _persist_completing_agent_handoff(agent.run_id, handoff, agent.project_dir)

        for action in handoff.next_actions:
            if not action.auto_execute:
                continue

            # Extract args once for agent-mode actions
            if action.mode == "agent":
                args = action.args or {}
                raw_pr = args.get("pr") or handoff.pr_number
                target_role = args.get("role")
                target_issue = str(args.get("issue", handoff.issue)).lstrip("#").strip()
                pr_num = int(raw_pr) if raw_pr is not None else None

                # Check circuit breaker for address-review spawns
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
                        issue=target_issue,
                        pr_number=pr_num,
                        branch=handoff.branch,
                        summary=reason,
                        next_actions=[
                            HandoffAction(
                                id="address_review",
                                label="Address Review (manual)",
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

            # Memory pressure gate (all action modes)
            mem_block, _mem_warn = check_memory_pressure(agent.project_dir)
            if mem_block:
                log.warning(
                    "auto_handoff.memory_blocked",
                    run_id=agent.run_id,
                    issue=handoff.issue,
                    error=mem_block.get("error", ""),
                )
                from sova.ipc.handoff import DashboardHandoff, HandoffAction, write_handoff_file

                blocked_handoff = DashboardHandoff(
                    source="memory_guard",
                    status="awaiting_action",
                    issue=handoff.issue,
                    pr_number=handoff.pr_number,
                    branch=handoff.branch,
                    summary=mem_block.get("error", "Memory pressure blocked auto-handoff"),
                    next_actions=[
                        HandoffAction(
                            id=action.id,
                            label=f"{action.label} (manual)",
                            mode=action.mode,
                            command=action.command,
                            args=action.args,
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

            issue_label = f"#{handoff.issue}" if handoff.issue else "Agent"
            emit_safe(
                f"{issue_label}: auto-handoff to {action.id}",
                category="handoff",
                metadata={"action_id": action.id, "issue": handoff.issue, "run_id": agent.run_id},
            )

            handoff_service.clear_handoff(agent.project_dir, issue=agent.issue)

            if action.mode == "agent":
                result = await agent_lifecycle.start_agent(
                    target_issue,
                    role=target_role,
                    pr_number=pr_num,
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
