"""Agent DB persistence -- TaskRun/CostRecord CRUD for dashboard-spawned agents.

Separated from agent_lifecycle to keep DB logic focused and testable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sova.core.state import TaskStatus
from sova.dashboard.services.agent_pool import AgentState
from sova.dashboard.services.feed_service import FeedEventSeverity, emit_safe
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.agent_db")


async def _create_task_run(
    issue: str | None, role: str, project_dir: Path, *, pid: int | None = None, pr_number: int | None = None
) -> int | None:
    """Create a TaskRun record and return its ID."""
    try:
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                task_run = TaskRun(
                    issue_number=issue or None,
                    role=role,
                    status=TaskStatus.RUNNING.value,
                    current_step="agent",
                    pid=pid,
                    pr_number=pr_number,
                )
                session.add(task_run)
                await session.flush()
                run_id = task_run.id
        log.info("task_run.created", run_id=run_id, issue=issue or "(none)")
        return run_id
    except Exception:
        log.warning("task_run.create_failed", exc_info=True)
        return None


async def _update_task_run_pid(run_id: int, pid: int, project_dir: Path) -> None:
    """Set the PID on an existing TaskRun after process spawn."""
    try:
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                task_run = await session.get(TaskRun, run_id)
                if task_run:
                    task_run.pid = pid
    except Exception:
        log.warning("task_run.pid_update_failed", run_id=run_id, exc_info=True)


async def _finalize_orphaned_run(run_id: int, project_dir: Path) -> None:
    """Mark a TaskRun as failed when the process never started."""
    try:
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                task_run = await session.get(TaskRun, run_id)
                if task_run:
                    task_run.status = "failed"
                    task_run.error_message = "Process spawn failed"
                    task_run.ended_at = datetime.now(timezone.utc)
    except Exception:
        log.warning("task_run.orphan_cleanup_failed", run_id=run_id, exc_info=True)


_TERMINAL_STATUSES = frozenset({"done", "failed", "rejected", "interrupted", "paused", "awaiting_approval"})


def _read_file_handoff(project_dir: Path, issue: str = "") -> dict | None:
    """Read file-based handoff details (sync I/O, call outside async transactions)."""
    try:
        from sova.ipc.handoff import read_handoff_file

        handoff = read_handoff_file(project_dir, issue=issue or None)
        if handoff is None:
            return None
        return {
            "issue": handoff.issue,
            "pr_number": handoff.pr_number,
            "details": handoff.details,
            "source": handoff.source,
        }
    except Exception:
        log.debug("task_run.file_handoff_read_failed", exc_info=True)
        return None


def _apply_file_handoff(task_run: object, file_handoff: dict | None, run_id: int) -> None:
    """Apply file-based handoff to a TaskRun if it matches by issue or PR."""
    if not file_handoff:
        return
    handoff_issue = str(file_handoff["issue"]).lstrip("#").strip() if file_handoff["issue"] else ""
    run_issue = str(task_run.issue_number).lstrip("#").strip() if task_run.issue_number else ""
    issue_match = (handoff_issue and run_issue and handoff_issue == run_issue) or (not handoff_issue and not run_issue)
    pr_match = file_handoff["pr_number"] and file_handoff["pr_number"] == task_run.pr_number
    if issue_match or pr_match:
        task_run.handoff_json = file_handoff["details"]
        log.info("task_run.file_handoff_persisted", run_id=run_id, source=file_handoff["source"])


def _emit_finalize_event(run_id: int, *, status: str, exit_code: int, agent: AgentState, cost: Decimal) -> None:
    """Emit a feed event for task run finalization."""
    issue = agent.issue
    label = f"#{issue}" if issue else "Agent"
    role_label = (agent.role or "agent").capitalize()
    sev = FeedEventSeverity.error if status == "failed" else FeedEventSeverity.success
    emit_safe(
        f"{label} {role_label} {status}",
        severity=sev,
        detail=f"Exit code: {exit_code}" if exit_code != 0 else None,
        category="agent",
        metadata={"run_id": run_id, "issue": issue, "role": agent.role, "cost_usd": str(cost)},
    )


async def _finalize_task_run(run_id: int, *, exit_code: int, agent: AgentState) -> None:
    """Update the TaskRun with final status and cost.

    Status is only updated if not already terminal (the WorkflowEngine may
    have set it first). Cost is always updated from the stream output since
    it includes Claude Code's own overhead.
    """
    try:
        from sova.db.models import CostRecord, TaskRun
        from sova.db.session import get_session

        status = "done" if exit_code == 0 else "failed"
        cost = Decimal(str(agent.last_result_cost)) if agent.last_result_cost else Decimal("0")
        file_handoff = _read_file_handoff(agent.project_dir, issue=agent.issue)

        async with await get_session(project_dir=agent.project_dir) as session:
            async with session.begin():
                task_run = await session.get(TaskRun, run_id)
                if task_run is None:
                    return

                if cost > 0:
                    task_run.total_cost_usd = cost

                if task_run.status in _TERMINAL_STATUSES:
                    log.info("task_run.already_terminal", run_id=run_id, status=task_run.status)
                    # Apply file handoff even for already-terminal runs. WorkflowEngine
                    # finalizes status in the subprocess before exiting, but write_handoff()
                    # there may target the wrong DB when CWD is a linked worktree. Persisting
                    # here (dashboard context, correct project_dir) ensures
                    # get_sova_review_verdict() can always find the real verdict.
                    if not task_run.handoff_json:
                        _apply_file_handoff(task_run, file_handoff, run_id)
                    return

                task_run.status = status
                task_run.ended_at = datetime.now(timezone.utc)
                if exit_code != 0:
                    task_run.error_message = f"Process exited with code {exit_code}"

                if agent.last_result_cost and agent.last_result_cost > 0:
                    cost_record = CostRecord(
                        task_run_id=run_id,
                        phase="agent",
                        issue=task_run.issue_number or "",
                        model="claude",
                        cost_usd=cost,
                    )
                    session.add(cost_record)

                if not task_run.handoff_json:
                    _apply_file_handoff(task_run, file_handoff, run_id)

                # Cancel pending PR queue entry on any terminal transition.
                # A "done" run may still have a PENDING entry if the queue
                # processor hasn't reached it yet; leaving it would cause
                # a duplicate PR creation attempt.
                try:
                    from sova.supervisor.pr_throttle import dequeue as pr_dequeue

                    await pr_dequeue(session, task_run_id=run_id)
                except Exception:
                    log.debug("task_run.pr_dequeue_failed", run_id=run_id, exc_info=True)

        log.info("task_run.finalized", run_id=run_id, status=status, cost=float(cost))
        _emit_finalize_event(run_id, status=status, exit_code=exit_code, agent=agent, cost=cost)
    except Exception:
        log.warning("task_run.finalize_failed", exc_info=True)


async def _fetch_output_lines(run_id: int, project_dir: Path | None) -> list[str] | None:
    """Fetch output lines for a TaskRun. Returns None if no lines exist or on error."""
    from sova.db.models import OutputLine
    from sova.db.session import get_session

    try:
        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                from sqlalchemy import select

                stmt = select(OutputLine.text).where(OutputLine.task_run_id == run_id).order_by(OutputLine.line_number)
                result = await session.execute(stmt)
                rows = [row[0] for row in result.fetchall()]
                return rows or None
    except Exception:
        log.debug("fetch_output_lines.failed", run_id=run_id, exc_info=True)
        return None


async def _fetch_pr_fields(pr_number: int, project_dir: Path, fields: str, jq_expr: str) -> str | None:
    """Fetch PR fields via gh CLI with standard timeout and error handling.

    Returns the stripped stdout on success, None on any error.
    """
    from sova.utils.shell import run as run_shell

    try:
        result = await run_shell(
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--json",
            fields,
            "--jq",
            jq_expr,
            cwd=project_dir,
            timeout=10,
        )
        if result.success and (output := result.stdout.strip()):
            return output
    except Exception:
        log.debug("fetch_pr_fields.failed", pr=pr_number, fields=fields, exc_info=True)
    return None


async def _capture_pr_head_sha(pr_number: int, project_dir: Path) -> str | None:
    """Capture the current headRefOid of a PR before an agent run starts.

    Returns None on any error (API failure, timeout, missing PR) so callers
    can fall through to existing validation logic.
    """
    sha = await _fetch_pr_fields(pr_number, project_dir, "headRefOid", ".headRefOid")
    if sha:
        log.debug("capture_pr_head_sha.ok", pr=pr_number, sha=sha[:12])
    return sha


async def _check_pr_pushed_via_sha(agent: AgentState) -> bool | None:
    """Compare the PR's current headRefOid against the pre-run snapshot.

    Returns:
        True  -- SHA changed (agent pushed) or PR is merged
        None  -- inconclusive (no pre_run_sha, API error, or SHAs match)
        Never returns False; matching SHAs fall through as inconclusive.
    """
    if agent.pre_run_sha is None or not agent.pr_number:
        return None

    output = await _fetch_pr_fields(
        agent.pr_number, agent.project_dir, "headRefOid,state", "[.headRefOid, .state] | @tsv"
    )
    if not output:
        return None

    parts = output.split("\t")
    if len(parts) < 2:
        return None
    current_sha, state = parts[0], parts[1]

    if not current_sha or not state:
        return None

    if state == "MERGED":
        log.info("check_pr_pushed_via_sha.merged", pr=agent.pr_number)
        return True

    if current_sha != agent.pre_run_sha:
        log.info(
            "check_pr_pushed_via_sha.sha_changed",
            pr=agent.pr_number,
            before=agent.pre_run_sha[:12],
            after=current_sha[:12],
        )
        return True

    log.debug("check_pr_pushed_via_sha.unchanged", pr=agent.pr_number)
    return None


async def _validate_address_pr(run_id: int, agent: AgentState) -> str | None:
    """Check that address-pr actually committed and pushed changes.

    Uses git ref comparison (local HEAD vs remote tracking branch) as the
    primary check. Falls back to output text scanning when the git check
    is inconclusive (e.g., worktree already cleaned up).
    """
    if agent.pr_number is None:
        return None

    sha_pushed = await _check_pr_pushed_via_sha(agent)
    if sha_pushed is True:
        return None

    pushed = await _check_pr_branch_pushed(agent)
    if pushed is True:
        return None
    if pushed is False:
        return "address-pr completed without pushing changes"

    lines = await _fetch_output_lines(run_id, agent.project_dir)
    if lines is None:
        return None

    _PUSH_KEYWORDS = ("git push", "force-with-lease", "force-push", "pushed to", "pushed commit")
    has_push_evidence = any(any(kw in line.lower() for kw in _PUSH_KEYWORDS) for line in lines)

    if not has_push_evidence:
        return "address-pr completed without pushing changes"
    return None


async def _check_pr_branch_pushed(agent: AgentState) -> bool | None:
    """Check if the PR branch has unpushed commits. Returns None if inconclusive."""
    from sova.utils.shell import run as run_shell

    if not agent.pr_number:
        return None
    try:
        branch = await _fetch_pr_fields(agent.pr_number, agent.project_dir, "headRefName", ".headRefName")
        if not branch:
            return None

        fetch_result = await run_shell("git", "fetch", "origin", branch, cwd=agent.project_dir, timeout=15)
        if not fetch_result.success:
            return None

        count_result = await run_shell(
            "git",
            "rev-list",
            "--count",
            f"origin/{branch}..{branch}",
            cwd=agent.project_dir,
            timeout=5,
        )
        if count_result.success and count_result.stdout.strip() == "0":
            return True
        if count_result.success and count_result.stdout.strip().isdigit():
            return False
    except Exception:
        log.debug("check_pr_branch_pushed.failed", pr=agent.pr_number, exc_info=True)
    return None


async def _validate_review_pr(run_id: int, agent: AgentState) -> str | None:
    """Check that review-pr actually posted a review on the PR."""
    if agent.pr_number is None:
        return None

    lines = await _fetch_output_lines(run_id, agent.project_dir)
    if lines is None:
        return None

    has_post_evidence = any(
        "review posted" in line.lower()
        or "pullrequestreview" in line.lower()
        or ("pulls/" in line.lower() and "/reviews" in line.lower())
        for line in lines
    )

    if not has_post_evidence:
        return "review-pr completed without posting a review on GitHub"
    return None


_COMMAND_VALIDATORS = {
    "address-pr": _validate_address_pr,
    "review-pr": _validate_review_pr,
}

_PIPELINE_ROLES = frozenset({"developer", "researcher", "planner"})


async def _validate_command_outcome(run_id: int, agent: AgentState) -> str | None:
    """Validate that a command run actually produced its expected outcome.

    For known command types (address-pr, review-pr), checks evidence of
    the expected side effects (commits pushed, review posted, etc.).
    Returns an error message if validation fails, None if OK or unknown command.
    """
    if not agent.role or not agent.role.startswith("command:"):
        return None

    cmd_name = agent.role.removeprefix("command:").removeprefix("/").split()[0]
    validator_fn = _COMMAND_VALIDATORS.get(cmd_name)
    if not validator_fn:
        return None

    try:
        return await validator_fn(run_id, agent)
    except Exception:
        log.debug("validate_command.failed", run_id=run_id, cmd=cmd_name, exc_info=True)
        return None


def _build_bypass_message(role: str, pr_number: int | None, prompt: str | None, run_id: int) -> str:
    """Build the error message for a pipeline bypass detection."""
    msg = (
        f"Pipeline bypassed: {role} agent completed without "
        f"executing workflow steps (current_step still 'agent', "
        f"0 step executions)"
    )
    if role == "developer" and pr_number is None:
        msg += " and no PR was created"
    if prompt:
        log.warning(
            "validate_pipeline.bypass_diagnostic",
            run_id=run_id,
            role=role,
            prompt_sent=prompt[:500],
        )
    return msg


async def _check_incomplete_pr(run_id: int, session: object) -> str | None:
    """Check if a developer run reached push/create_pr but has no pr_number."""
    from sqlalchemy import select

    from sova.core.state import STEP_DONE_STATUSES
    from sova.db.models import StepExecution

    done_steps_stmt = select(StepExecution.step_name).where(
        StepExecution.task_run_id == run_id,
        StepExecution.status.in_(STEP_DONE_STATUSES),
    )
    result = await session.execute(done_steps_stmt)
    done_names = {row[0] for row in result.fetchall()}
    if "create_pr" in done_names or "push" in done_names:
        return "Pipeline incomplete: developer agent reached push/create_pr step but pr_number is still None"
    return None


async def _validate_pipeline_outcome(run_id: int, agent: AgentState) -> str | None:
    """Validate that a pipeline-based role actually executed its workflow steps.

    Returns an error message if the pipeline was bypassed, None if OK or
    not a pipeline role.
    """
    if not agent.role or agent.role not in _PIPELINE_ROLES:
        return None

    try:
        from sqlalchemy import func, select

        from sova.db.models import StepExecution, TaskRun
        from sova.db.session import get_session

        async with await get_session(project_dir=agent.project_dir) as session:
            async with session.begin():
                task_run = await session.get(TaskRun, run_id)
                if task_run is None:
                    return None

                sentinel_active = task_run.current_step == "agent"

                stmt = select(func.count()).where(StepExecution.task_run_id == run_id)
                result = await session.execute(stmt)
                step_count = result.scalar() or 0

                if sentinel_active and step_count == 0:
                    return _build_bypass_message(agent.role, task_run.pr_number, agent.prompt, run_id)

                if agent.role == "developer" and step_count > 0 and task_run.pr_number is None:
                    return await _check_incomplete_pr(run_id, session)

        return None
    except Exception:
        log.debug("validate_pipeline.failed", run_id=run_id, exc_info=True)
        return None


async def _downgrade_to_failed(run_id: int, reason: str, project_dir: Path) -> None:
    """Downgrade a 'done' TaskRun to 'failed' with the given reason."""
    try:
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                task_run = await session.get(TaskRun, run_id)
                if task_run and task_run.status == "done":
                    task_run.status = "failed"
                    task_run.error_message = reason
                    log.warning("task_run.downgraded", run_id=run_id, reason=reason)
    except Exception:
        log.warning("task_run.downgrade_failed", run_id=run_id, exc_info=True)


async def _fetch_run_states(run_ids: list[int]) -> dict[int, dict]:
    """Fetch current_step, status, and cost from the DB for running agents."""
    if not run_ids:
        return {}
    try:
        from sqlalchemy import select

        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session() as session:
            async with session.begin():
                stmt = select(TaskRun).where(TaskRun.id.in_(run_ids))
                result = await session.execute(stmt)
                runs = result.scalars().all()
        return {
            r.id: {
                "current_step": r.current_step or "agent",
                "status": r.status,
                "cost_usd": float(r.total_cost_usd or 0),
                "pr_number": r.pr_number,
            }
            for r in runs
        }
    except Exception:
        log.debug("fetch_run_states.failed", exc_info=True)
        return {}
