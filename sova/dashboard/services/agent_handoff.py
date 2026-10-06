"""Auto-handoff orchestration after agent completion."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from sova.dashboard.services.agent_validation import check_memory_pressure
from sova.dashboard.services.feed_service import FeedEventSeverity, emit_safe
from sova.supervisor.gates.utils import count_address_review_runs
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from sova.dashboard.services.agent_pool import AgentState
    from sova.ipc.handoff import DashboardHandoff

log = get_logger(component="dashboard.control.handoff")


async def _check_address_review_circuit_breaker(
    issue: str, pr_number: int | None, role: str | None, project_dir: Path
) -> str | None:
    """Check if the address-review circuit breaker should block auto-execution.

    Returns a reason string if blocked, None if clear. A blocked PR no longer
    needs a manual-only handoff written: resolve_next_action()'s
    review_budget_exhausted rule already resolves an over-budget PR to
    PR_REVIEW_EXHAUSTED with an Integrate action the dashboard renders
    directly, so the caller only has to stop auto-spawning the next cycle.
    The verdict cache is invalidated unconditionally once the cycle count is
    computed, not only when the budget turns out to be exhausted, so a verdict
    served from cache always reflects this run's just-completed cycle rather
    than waiting out the cache TTL.
    """
    if role != "developer" or pr_number is None:
        return None

    from sova.config.loader import load_config

    cfg = load_config(project_dir)
    max_cycles = cfg.pipeline.max_address_review_cycles
    if max_cycles <= 0:
        return None

    count = await count_address_review_runs(issue, pr_number, project_dir)

    from sova.dashboard.services.work_verdict import invalidate_verdict

    invalidate_verdict(project_dir, pr_number)

    if count >= max_cycles:
        return (
            f"Address-review budget exhausted: {count} cycles completed for "
            f"PR #{pr_number} on issue #{issue} (max allowed: {max_cycles}). "
            f"Routed to Integrate for a manual decision."
        )

    return None


def _issue_label(issue: str) -> str:
    """Render an issue number for a notification subtitle or feed title."""
    return f"#{issue}" if issue else "Agent"


def _notify_budget_exhausted(agent: AgentState, issue: str, reason: str) -> None:
    """Announce an exhausted address-review budget on every configured channel.

    Routed through notify() rather than send_desktop_notification() so the
    operator's notification config (desktop toggle, Slack, email, webhook) is
    honoured and delivery stays fire-and-forget: this runs on the agent-exit
    path, which must not block on a notifier subprocess or be derailed by one
    failing before clear_handoff() runs.
    """
    try:
        from sova.config.loader import load_config
        from sova.ipc.notifications import notify

        notify(
            load_config(agent.project_dir).notification,
            "SOVA",
            f"{agent.project_dir.name} | {reason}",
            subtitle=f"Address-review budget exhausted {_issue_label(issue)}",
            group=f"sova-{issue}" if issue else "sova",
        )
    except Exception:  # noqa: BLE001 (best-effort: config load and the OS notifier both fail in many ways)
        log.debug("notify.failed", run_id=agent.run_id, exc_info=True)


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
    except (OSError, RuntimeError, SQLAlchemyError):
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
                try:
                    pr_num = int(raw_pr) if raw_pr is not None else None
                except (ValueError, TypeError):
                    log.warning("auto_handoff.invalid_pr_number", raw_pr=raw_pr, run_id=agent.run_id)
                    pr_num = None

                # Skip spawning if a reviewer already ran for this PR: the
                # developer may have written a "please review" handoff after the
                # reviewer already completed (timing race), making the handoff stale.
                # An "addressed" verdict is the one exception: it means an address
                # cycle completed after that review, so the handoff is a re-review
                # request for the new head, not a stale duplicate.
                if action.id in {"review", "review_pr"} and pr_num is not None:
                    from sova.dashboard.services.agent_recovery import get_sova_review_verdict

                    verdict = await get_sova_review_verdict(
                        target_issue, pr_number=pr_num, project_dir=agent.project_dir
                    )
                    if verdict.get("has_sova_review") and verdict.get("verdict") != "addressed":
                        log.info(
                            "auto_handoff.review_already_done",
                            run_id=agent.run_id,
                            issue=target_issue,
                            pr_number=pr_num,
                        )
                        handoff_service.clear_handoff(agent.project_dir, issue=agent.issue)
                        return

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
                    # No manual-only handoff needed: resolve_next_action()'s
                    # review_budget_exhausted rule already resolves this PR to
                    # PR_REVIEW_EXHAUSTED with an Integrate action, so clearing
                    # the stale handoff is enough for the dashboard to pick up
                    # the right next action on its next poll.
                    emit_safe(
                        f"{_issue_label(target_issue)}: address-review budget exhausted",
                        severity=FeedEventSeverity.warning,
                        detail=reason,
                        category="handoff",
                        metadata={"issue": target_issue, "pr_number": pr_num, "run_id": agent.run_id},
                    )
                    _notify_budget_exhausted(agent, target_issue, reason)
                    handoff_service.clear_handoff(agent.project_dir, issue=agent.issue)
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

    except Exception:  # noqa: BLE001 (auto-handoff is best-effort; a failure must not break finalization)
        log.warning("auto_handoff.failed", run_id=agent.run_id, exc_info=True)
