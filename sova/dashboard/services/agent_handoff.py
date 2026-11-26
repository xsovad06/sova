"""Auto-handoff orchestration after agent completion."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from sova.config.loader import load_config
from sova.dashboard.services.agent_validation import check_memory_pressure
from sova.dashboard.services.feed_service import FeedEventSeverity, emit_safe
from sova.db.session import get_session
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from sova.dashboard.services.agent_pool import AgentState, ProjectAgents
    from sova.ipc.handoff import DashboardHandoff, HandoffAction

log = get_logger(component="dashboard.control.handoff")

# Leads the breaker's reason when the cycle count could not be read: the alert must say "unverifiable", not
# "exhausted", since the resolver holds that PR for the user instead of routing it to Integrate.
_BUDGET_UNVERIFIABLE_PREFIX = "Address-review budget unverifiable"


async def _check_address_review_circuit_breaker(
    issue: str, pr_number: int | None, role: str | None, project_dir: Path
) -> str | None:
    """Check if the address-review circuit breaker should block auto-execution.

    Returns a reason string if blocked, None if clear. A blocked PR no longer
    needs a manual-only handoff written: resolve_next_action()'s
    review_budget_exhausted rule already resolves an over-budget PR to
    PR_REVIEW_EXHAUSTED with an Integrate action the dashboard renders
    directly (an unreadable history resolves to a user hold instead, never
    Integrate), so the caller only has to stop auto-spawning the next cycle.
    The verdict cache is invalidated before resolving, not after, so this
    call's own ``address_cycles`` reflects this run's just-completed cycle
    (already posted as a ``sova-addressed`` marker on GitHub by this point)
    rather than a pre-cycle cached count.

    Fails closed when the count could not be read at all: the cycle count now
    lives entirely in GitHub's review history, so an unreachable GitHub makes
    it unknown rather than zero, and authorizing one more cycle on an unknown
    count is how a capped loop silently runs uncapped. ``/address-pr`` step 17
    takes the same position for the same decision, and the blocked state is
    recoverable by a human click rather than a dead end.
    """
    if role != "developer" or pr_number is None:
        return None

    from sova.dashboard.services.work_verdict import (
        TRUNCATED_KEY,
        UNRESOLVED_KEY,
        invalidate_verdict,
        resolve_verdict_for_pr,
    )

    cfg = load_config(project_dir)
    max_cycles = cfg.pipeline.max_address_review_cycles
    if max_cycles <= 0:
        return None

    invalidate_verdict(project_dir, pr_number)
    verdict = await resolve_verdict_for_pr(issue, pr_number=pr_number, project_dir=project_dir, config=cfg)

    if verdict.get(TRUNCATED_KEY):
        # More reviews than the history window holds: permanent, and far past any budget, so report it as
        # exhausted (the resolver routes it to Integrate) rather than as a transient outage to retry.
        return (
            f"Address-review budget exhausted: PR #{pr_number} on issue #{issue} has more reviews than the "
            f"history window holds, so the completed-cycle count cannot be read (max allowed: {max_cycles}). "
            f"Routed to Integrate for a manual decision."
        )

    if verdict.get(UNRESOLVED_KEY):
        return (
            f"{_BUDGET_UNVERIFIABLE_PREFIX}: GitHub did not return the review "
            f"history for PR #{pr_number} on issue #{issue}, so the completed-cycle "
            f"count is unknown (max allowed: {max_cycles}). Held for the user: retry the "
            f"review once GitHub is reachable, rather than risking an uncapped review loop."
        )

    count = verdict.get("address_cycles", 0)
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


def _notify_budget_exhausted(agent: AgentState, issue: str, reason: str, *, headline: str) -> None:
    """Announce an exhausted (or unverifiable) address-review budget on every configured channel.

    Routed through notify() rather than send_desktop_notification() so the
    operator's notification config (desktop toggle, Slack, email, webhook) is
    honoured and delivery stays fire-and-forget: this runs on the agent-exit
    path, which must not block on a notifier subprocess or be derailed by one
    failing before clear_handoff() runs.
    """
    try:
        from sova.ipc.notifications import NotificationKind, notify

        notify(
            load_config(agent.project_dir).notification,
            "SOVA",
            f"{agent.project_dir.name} | {reason}",
            kind=NotificationKind.NEEDS_HUMAN,
            subtitle=f"{headline.capitalize()} {_issue_label(issue)}",
            group=f"sova-{issue}" if issue else "sova",
            issue=issue or None,
            pr_number=agent.pr_number,
            run_id=agent.run_id,
            project_dir=agent.project_dir,
        )
    except Exception:  # noqa: BLE001 (best-effort: config load and the OS notifier both fail in many ways)
        log.debug("notify.failed", run_id=agent.run_id, exc_info=True)


async def _persist_completing_agent_handoff(run_id: int, handoff: "DashboardHandoff", project_dir: "Path") -> None:
    """Persist handoff details to the completing agent's TaskRun.handoff_json.

    Called from _process_auto_handoff before the file is cleared. This backstops
    write_handoff() in the subprocess, which may write to the wrong DB when the
    subprocess CWD is a linked worktree rather than the project root. Persisting
    here (dashboard context with the correct project_dir) ensures every other
    consumer of TaskRun.handoff_json (address-review, rebase, the watchdog) can
    always find it.

    Only writes if handoff_json is not already set (subprocess may have written it
    correctly when the CWD worktree fix is in place).
    """
    try:
        from sova.db.models import TaskRun

        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                task_run = await session.get(TaskRun, run_id)
                if task_run is not None and not task_run.handoff_json and handoff.details:
                    task_run.handoff_json = handoff.details
                    log.info("auto_handoff.handoff_json_persisted", run_id=run_id, source=handoff.source)
    except (OSError, RuntimeError, SQLAlchemyError):
        log.warning("auto_handoff.handoff_json_persist_failed", run_id=run_id, exc_info=True)


def _slug_for_project(project_dir: Path | None, pool: ProjectAgents | None = None) -> str | None:
    """The slug of the agent pool the finished agent ran in, so its follow-up lands in that same pool.

    Resolved from the finished agent rather than the ambient contextvar: a finalize running for an adopted run has
    no request context, and two projects can share an issue number. The owning *pool* (``_wait_and_finalize()``
    holds it) wins, since two pools can share a directory (``__default__`` and the primary project's registered
    slug on a hub) and the follow-up must keep the finished run's slot limit and UI scope. Without it, a live pool
    for the directory, then ``find_slug_for_path()``. ``None`` (no ``project_dir``, or an unregistered directory
    with no pool) keeps ``start_agent()``'s default resolution.
    """
    from sova.config.registry import find_slug_for_path
    from sova.dashboard.services.agent_pool import list_all_pools

    pools = list_all_pools()
    if pool is not None:
        owner = next((slug for slug, pa in pools.items() if pa is pool), None)
        if owner is not None:
            return owner
    if project_dir is None:
        return None
    resolved = project_dir.resolve()
    for slug, pa in pools.items():
        if pa.project_dir.resolve() == resolved:
            return slug
    return find_slug_for_path(resolved)


def _clear_read_handoff(agent: AgentState, handoff: DashboardHandoff, data: dict) -> None:
    """Clear the handoff the auto-handoff read: its issue's file, or the issue-less file of a PR-only handoff.

    Never an empty key: a PR-only run has no issue, and clearing by it must not wipe other issues' handoffs.
    *data* is the file content that was read, so a replacement written since then stays pending.
    """
    from sova.dashboard.services import handoff_service

    issue = str(handoff.issue or agent.issue or "").lstrip("#").strip()
    if issue:
        handoff_service.clear_handoff(agent.project_dir, issue=issue, expected=data)
    else:
        handoff_service.clear_issueless_handoff(agent.project_dir, expected=data)


async def _start_follow_up(
    action: HandoffAction, target_issue: str, target_role: str | None, pr_num: int | None, slug: str | None
) -> object:
    """Start the follow-up of an auto-execute *action*; the caller has already cleared the handoff.

    Returns the start's result, or ``None`` when the action names nothing that can be started.
    """
    from sova.dashboard.services import agent_lifecycle, handoff_service

    if action.mode == "agent":
        result = await agent_lifecycle.start_agent(
            target_issue, role=target_role, pr_number=pr_num, slug=slug, _skip_handoff_clear=True
        )
        log.info("auto_handoff.agent_started", result=result, slug=slug)
        return result
    if action.mode == "claude-command":
        cmd = handoff_service.command_name(action.command)
        if cmd:
            result = await agent_lifecycle.start_command(cmd, action.args, slug=slug, _skip_handoff_clear=True)
            if handoff_service.start_succeeded(result):
                log.info("auto_handoff.command_started", result=result)
            return result
        return None
    log.warning("auto_handoff.unsupported_mode", mode=action.mode)
    return None


def _read_actionable_handoff(agent: AgentState) -> tuple[DashboardHandoff, dict] | None:
    """Return the handoff *agent* owns when it awaits action and names the agent's issue, else ``None``."""
    from sova.ipc.handoff import read_owned_handoff

    owned = read_owned_handoff(agent.project_dir, agent.issue, pr_number=agent.pr_number)
    if owned is None:
        return None
    handoff, data = owned
    if handoff.status != "awaiting_action":
        return None

    h_issue = str(handoff.issue).lstrip("#").strip() if handoff.issue else ""
    a_issue = str(agent.issue).lstrip("#").strip() if agent.issue else ""
    if h_issue and a_issue and h_issue != a_issue:
        log.info(
            "auto_handoff.issue_mismatch",
            run_id=agent.run_id,
            agent_issue=agent.issue,
            handoff_issue=handoff.issue,
        )
        return None
    return handoff, data


def _agent_action_target(
    action: HandoffAction, handoff: DashboardHandoff, agent: AgentState
) -> tuple[str, str | None, int | None]:
    """Return the ``(issue, role, pr_number)`` an agent-mode *action* targets, falling back to *handoff*."""
    args = action.args or {}
    raw_pr = args.get("pr") or handoff.pr_number
    target_role = args.get("role")
    target_issue = str(args.get("issue", handoff.issue)).lstrip("#").strip()
    try:
        pr_num = int(raw_pr) if raw_pr is not None else None
    except (ValueError, TypeError):
        log.warning("auto_handoff.invalid_pr_number", raw_pr=raw_pr, run_id=agent.run_id)
        pr_num = None
    return target_issue, target_role, pr_num


async def _review_already_done(action: HandoffAction, agent: AgentState, target_issue: str, pr_num: int | None) -> bool:
    """Return True when a review *action* is stale because a reviewer already ran for this PR.

    The developer may have written a "please review" handoff after the reviewer
    already completed (timing race). An "addressed" verdict is the one exception:
    it means an address cycle completed after that review, so the handoff is a
    re-review request for the new head, not a stale duplicate.
    """
    if action.id not in {"review", "review_pr"} or pr_num is None:
        return False

    from sova.dashboard.services.work_verdict import resolve_verdict_for_pr

    verdict = await resolve_verdict_for_pr(target_issue, pr_number=pr_num, project_dir=agent.project_dir)
    if verdict.get("has_sova_review") and verdict.get("verdict") != "addressed":
        log.info(
            "auto_handoff.review_already_done",
            run_id=agent.run_id,
            issue=target_issue,
            pr_number=pr_num,
        )
        return True
    return False


async def _address_review_budget_blocked(
    agent: AgentState, target_issue: str, target_role: str | None, pr_num: int | None
) -> bool:
    """Return True (after alerting) when the address-review circuit breaker blocks this spawn.

    No manual-only handoff is needed: resolve_next_action() already resolves an
    over-budget PR to PR_REVIEW_EXHAUSTED with Integrate, and an unreadable
    history to a user hold, so clearing the stale handoff is enough for the
    dashboard to pick up the right next action on its next poll.
    """
    reason = await _check_address_review_circuit_breaker(target_issue, pr_num, target_role, agent.project_dir)
    if not reason:
        return False
    log.warning(
        "auto_handoff.circuit_breaker",
        run_id=agent.run_id,
        issue=target_issue,
        pr_number=pr_num,
        reason=reason,
    )
    unverifiable = reason.startswith(_BUDGET_UNVERIFIABLE_PREFIX)
    headline = f"address-review budget {'unverifiable' if unverifiable else 'exhausted'}"
    emit_safe(
        f"{_issue_label(target_issue)}: {headline}",
        severity=FeedEventSeverity.warning,
        detail=reason,
        category="handoff",
        metadata={"issue": target_issue, "pr_number": pr_num, "run_id": agent.run_id},
    )
    _notify_budget_exhausted(agent, target_issue, reason, headline=headline)
    return True


def _write_memory_blocked_handoff(
    agent: AgentState, action: HandoffAction, handoff: DashboardHandoff, summary: str
) -> None:
    """Write a manual-only copy of *action* so the user can retry it once memory frees up."""
    from sova.ipc.handoff import DashboardHandoff, HandoffAction, write_handoff_file

    blocked_handoff = DashboardHandoff(
        source="memory_guard",
        status="awaiting_action",
        issue=handoff.issue,
        pr_number=handoff.pr_number,
        branch=handoff.branch,
        summary=summary,
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
    write_handoff_file(agent.project_dir, blocked_handoff)


def _memory_pressure_blocked(agent: AgentState, action: HandoffAction, handoff: DashboardHandoff, data: dict) -> bool:
    """Return True when memory pressure blocks *action* (all modes), swapping the handoff for a manual-only one."""
    mem_block, _mem_warn = check_memory_pressure(agent.project_dir)
    if not mem_block:
        return False
    log.warning(
        "auto_handoff.memory_blocked",
        run_id=agent.run_id,
        issue=handoff.issue,
        error=mem_block.get("error", ""),
    )
    _clear_read_handoff(agent, handoff, data)
    summary = mem_block.get("error", "Memory pressure blocked auto-handoff")
    _write_memory_blocked_handoff(agent, action, handoff, summary)
    return True


async def _run_auto_handoff(agent: AgentState, pool: ProjectAgents | None) -> None:
    """Body of ``_process_auto_handoff()``, which wraps it so no failure escapes into finalization."""
    from sova.dashboard.services.handoff_service import start_succeeded

    actionable = _read_actionable_handoff(agent)
    if actionable is None:
        return
    handoff, data = actionable

    # Persist handoff details to the completing agent's TaskRun before clearing
    # the file. Done after the mismatch guard to avoid writing a mismatched
    # issue's verdict into the completing run's handoff_json. This backstops
    # write_handoff() in the subprocess, which may write to the wrong DB when
    # the subprocess CWD is a linked worktree rather than the project root.
    if agent.run_id is not None and handoff.details:
        await _persist_completing_agent_handoff(agent.run_id, handoff, agent.project_dir)

    # Only the first auto action executes.
    action = next((a for a in handoff.next_actions if a.auto_execute), None)
    if action is None:
        return

    target_issue, target_role, pr_num = "", None, None
    if action.mode == "agent":
        target_issue, target_role, pr_num = _agent_action_target(action, handoff, agent)
        if await _review_already_done(action, agent, target_issue, pr_num) or (
            await _address_review_budget_blocked(agent, target_issue, target_role, pr_num)
        ):
            _clear_read_handoff(agent, handoff, data)
            return

    if _memory_pressure_blocked(agent, action, handoff, data):
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

    _clear_read_handoff(agent, handoff, data)
    slug = _slug_for_project(agent.project_dir, pool)
    result = await _start_follow_up(action, target_issue, target_role, pr_num, slug)
    if result is not None and not start_succeeded(result):
        log.warning("auto_handoff.start_refused", run_id=agent.run_id, action_id=action.id, result=result)


async def _process_auto_handoff(agent: AgentState, pool: ProjectAgents | None = None) -> None:
    """Check for auto-executable handoff actions after an agent completes.

    Reads the handoff file and auto-triggers the first action marked
    with auto_execute=True. This enables role chaining (e.g., Developer
    hands off to Reviewer automatically after CI passes). The handoff is
    cleared before the follow-up starts: the trigger file must be gone
    before the follow-up run reads state, and a pending auto action must
    not show as a button a user can click into a second start. A refused
    start therefore leaves no pending handoff (the accepted trade-off of
    this path, which nobody retries by hand).

    An issue-less (PR-only) agent reads only the handoff that names no issue
    and carries its own PR, never the newest other issue's file or another
    PR's handoff (``read_owned_handoff()``). *pool* is the pool the agent ran
    in; the follow-up starts in it (``_slug_for_project()``).
    """
    try:
        await _run_auto_handoff(agent, pool)
    except Exception:  # noqa: BLE001 (auto-handoff is best-effort; a failure must not break finalization)
        log.warning("auto_handoff.failed", run_id=agent.run_id, exc_info=True)
