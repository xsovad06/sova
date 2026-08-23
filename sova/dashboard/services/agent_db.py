"""Agent DB persistence -- TaskRun/CostRecord CRUD for dashboard-spawned agents.

Separated from agent_lifecycle to keep DB logic focused and testable.
"""

from __future__ import annotations

import re
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


async def _update_task_run_output_path(run_id: int, output_path: str, project_dir: Path) -> None:
    """Store the output file path on a TaskRun for reconnection after restart."""
    try:
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                task_run = await session.get(TaskRun, run_id)
                if task_run:
                    task_run.output_file_path = output_path
    except Exception:
        log.warning("task_run.output_path_update_failed", run_id=run_id, exc_info=True)


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


async def _handle_terminal_status(task_run: object, file_handoff: dict | None, session: object, run_id: int) -> None:
    """Handle already-terminal TaskRun status (apply handoff and finalize steps)."""
    log.info("task_run.already_terminal", run_id=run_id, status=task_run.status)
    # Apply file handoff even for already-terminal runs. WorkflowEngine
    # finalizes status in the subprocess before exiting, but write_handoff()
    # there may target the wrong DB when CWD is a linked worktree. Persisting
    # here (dashboard context, correct project_dir) ensures
    # get_sova_review_verdict() can always find the real verdict.
    if not task_run.handoff_json:
        _apply_file_handoff(task_run, file_handoff, run_id)
    await _finalize_orphaned_steps(session, run_id)


async def _record_cost(task_run: object, run_id: int, cost: Decimal, agent: AgentState, session: object) -> None:
    """Record cost for a completed task run."""
    from sova.db.models import CostRecord

    if agent.last_result_cost and agent.last_result_cost > 0:
        cost_record = CostRecord(
            task_run_id=run_id,
            phase="agent",
            issue=task_run.issue_number or "",
            model="claude",
            cost_usd=cost,
        )
        session.add(cost_record)


async def _dequeue_pr_entry(session: object, run_id: int) -> None:
    """Cancel pending PR queue entry on terminal transition."""
    try:
        from sova.supervisor.pr_throttle import dequeue as pr_dequeue

        await pr_dequeue(session, task_run_id=run_id)
    except Exception:
        log.debug("task_run.pr_dequeue_failed", run_id=run_id, exc_info=True)


async def _finalize_task_run(run_id: int, *, exit_code: int, agent: AgentState) -> bool:
    """Update the TaskRun with final status and cost.

    Status is only updated if not already terminal (the WorkflowEngine may
    have set it first). Cost is always updated from the stream output since
    it includes Claude Code's own overhead.

    Returns True if the status was actually transitioned (not already terminal).
    """
    try:
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        status = "done" if exit_code == 0 else "failed"
        cost = Decimal(str(agent.last_result_cost)) if agent.last_result_cost else Decimal("0")
        file_handoff = _read_file_handoff(agent.project_dir, issue=agent.issue)

        async with await get_session(project_dir=agent.project_dir) as session:
            async with session.begin():
                task_run = await session.get(TaskRun, run_id)
                if task_run is None:
                    return False

                if cost > 0:
                    task_run.total_cost_usd = cost

                if task_run.status in _TERMINAL_STATUSES:
                    await _handle_terminal_status(task_run, file_handoff, session, run_id)
                    return False

                task_run.status = status
                task_run.ended_at = datetime.now(timezone.utc)
                if exit_code != 0:
                    task_run.error_message = f"Process exited with code {exit_code}"

                await _record_cost(task_run, run_id, cost, agent, session)

                if not task_run.handoff_json:
                    _apply_file_handoff(task_run, file_handoff, run_id)

                await _dequeue_pr_entry(session, run_id)
                await _finalize_orphaned_steps(session, run_id)

        log.info("task_run.finalized", run_id=run_id, status=status, cost=float(cost))
        _emit_finalize_event(run_id, status=status, exit_code=exit_code, agent=agent, cost=cost)

        if status in ("failed", "interrupted"):
            from sova.dashboard.services.agent_recovery import rollback_issue_state

            try:
                await rollback_issue_state(run_id, agent.project_dir)
            except Exception:
                log.debug("task_run.rollback_on_finalize_failed", run_id=run_id, exc_info=True)

        return True
    except Exception:
        log.warning("task_run.finalize_failed", exc_info=True)
        return False


async def _finalize_orphaned_steps(session: object, run_id: int) -> None:
    """Mark any StepExecution records still in 'running' as 'interrupted'.

    Called inside the _finalize_task_run transaction so the step status
    is consistent with the terminal TaskRun status. Without this, the
    dashboard step progress bar shows "running" for a step in a "done"
    or "failed" run.
    """
    try:
        from sqlalchemy import update

        from sova.db.models import StepExecution

        result = await session.execute(
            update(StepExecution)
            .where(StepExecution.task_run_id == run_id)
            .where(StepExecution.status == "running")
            .values(status="interrupted", ended_at=datetime.now(timezone.utc))
        )
        if result.rowcount:
            log.info("task_run.orphaned_steps_finalized", run_id=run_id, count=result.rowcount)
    except Exception:
        log.debug("task_run.orphaned_steps_finalize_failed", run_id=run_id, exc_info=True)


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

    Fail-closed: requires at least one tier to positively confirm the push.
    Tiers checked in order (short-circuits on first positive):
      0. Output lines exist (agent produced meaningful output)
      1. PR headRefOid changed (SHA comparison)
      2. Branch has no unpushed commits (git fetch + rev-list)
      3. Output text contains push keywords (requires 2+ distinct matches)
    """
    if agent.pr_number is None:
        return "address-pr run has no associated PR number"

    # Tier 0: output must exist (agent actually ran)
    lines = await _fetch_output_lines(run_id, agent.project_dir)
    if lines is None:
        return "address-pr produced no output (agent never ran meaningfully)"

    # Tier 1: SHA comparison (most reliable)
    sha_pushed = await _check_pr_pushed_via_sha(agent)
    if sha_pushed is True:
        return None

    # Tier 2: branch ref check
    pushed = await _check_pr_branch_pushed(agent)
    if pushed is True:
        return None
    if pushed is False:
        return "address-pr completed without pushing changes"

    # Tier 3: text scan (require 2+ distinct keyword matches to reduce false positives)
    lowered = [line.lower() for line in lines]
    matched_keywords = {kw for kw in _PUSH_KEYWORDS if any(kw in line for line in lowered)}
    if len(matched_keywords) >= 2:
        return None

    return "address-pr completed without pushing changes"


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


_PUSH_KEYWORDS = ("git push", "force-with-lease", "force-push", "pushed to", "pushed commit")

_SOVA_REVIEW_MARKER_RE = re.compile(r"<!--\s*sova-review:\s*(approve|revise|block)\s*-->", re.IGNORECASE)

_VERDICT_TO_NEXT_ACTION = {
    "approve": "approve",
    "revise": "address_review",
    "block": "address_review",
}


def _extract_review_verdict_marker(lines: list[str]) -> str | None:
    """Extract verdict from <!-- sova-review: X --> marker in output lines.

    Scans lines in reverse so the last marker wins (per spec).
    """
    for line in reversed(lines):
        matches = _SOVA_REVIEW_MARKER_RE.findall(line)
        if matches:
            return matches[-1].lower()
    return None


async def _persist_review_verdict(run_id: int, verdict: str, project_dir: Path | None) -> None:
    """Write structured handoff_json to a review-pr TaskRun."""
    from sova.db.models import TaskRun
    from sova.db.session import get_session

    next_action = _VERDICT_TO_NEXT_ACTION.get(verdict, "address_review")
    handoff_data = {
        "next_action": next_action,
        "pending_findings": [],
    }

    try:
        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                task_run = await session.get(TaskRun, run_id)
                if task_run is None:
                    return
                if task_run.handoff_json is not None:
                    log.debug("review_pr.handoff_already_set", run_id=run_id)
                    return
                task_run.handoff_json = handoff_data
                log.info("review_pr.verdict_persisted", run_id=run_id, verdict=verdict, next_action=next_action)
    except Exception:
        log.warning("review_pr.persist_verdict_failed", run_id=run_id, exc_info=True)
        raise


async def _validate_review_pr(run_id: int, agent: AgentState) -> str | None:
    """Check that review-pr posted a review, then persist the verdict as handoff_json."""
    if agent.pr_number is None:
        return "review-pr run has no associated PR number"

    lines = await _fetch_output_lines(run_id, agent.project_dir)
    if lines is None:
        return "review-pr has no recorded output"

    has_post_evidence = any(
        "review posted" in line.lower()
        or "pullrequestreview" in line.lower()
        or ("pulls/" in line.lower() and "/reviews" in line.lower())
        for line in lines
    )

    if not has_post_evidence:
        return "review-pr completed without posting a review on GitHub"

    verdict = _extract_review_verdict_marker(lines)
    if not verdict:
        from sova.dashboard.services.agent_recovery import _parse_verdict_from_output

        verdict = _parse_verdict_from_output(lines)
    if not verdict:
        verdict = "revise"
    await _persist_review_verdict(run_id, verdict, agent.project_dir)

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


def _collect_bypass_diagnostics(
    started_at: object | None,
    worktree_path: str | None,
    project_dir: object | None,
) -> list[str]:
    """Collect non-empty diagnostic fields for bypass messages."""
    parts: list[str] = []
    if started_at:
        parts.append(f"started_at={started_at}")
    if worktree_path:
        parts.append(f"worktree={worktree_path}")
    if project_dir:
        parts.append(f"project_dir={project_dir}")
    return parts


def _build_bypass_message(
    role: str,
    pr_number: int | None,
    prompt: str | None,
    run_id: int,
    *,
    diagnostics: list[str] | None = None,
) -> str:
    """Build the error message for a pipeline bypass detection."""
    msg = (
        f"Pipeline bypassed: {role} agent completed without "
        f"executing workflow steps (current_step still 'agent', "
        f"0 step executions)"
    )
    if role == "developer" and pr_number is None:
        msg += " and no PR was created"
    if diagnostics:
        msg += f" [{', '.join(diagnostics)}]"
    if prompt:
        log.warning(
            "validate_pipeline.bypass_diagnostic",
            run_id=run_id,
            role=role,
            prompt_sent=prompt[:500],
        )
    return msg


async def _check_incomplete_pr(run_id: int, session: object) -> str | None:
    """Check if a developer run committed code but never produced a PR.

    Two cases:
    1. push/create_pr step completed but pr_number is still None (existing).
    2. commit step completed but push/create_pr never ran: pipeline stopped
       mid-flight after committing code.
    """
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
    if "commit" in done_names:
        return "Pipeline incomplete: developer agent committed code but never pushed or created a PR"
    return None


async def _check_interrupted_step(run_id: int, session: object) -> str | None:
    """Check if a pipeline step was interrupted (died mid-execution).

    After _finalize_orphaned_steps runs, steps that were still 'running' when
    the process exited are marked 'interrupted'. If the last step in the
    pipeline is interrupted and no subsequent steps ran, the pipeline crashed
    silently during that step.
    """
    from sqlalchemy import select

    from sova.db.models import StepExecution

    stmt = (
        select(StepExecution.step_name, StepExecution.status)
        .where(StepExecution.task_run_id == run_id)
        .order_by(StepExecution.id.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    row = result.first()
    if row and row[1] == "interrupted":
        return f"Pipeline incomplete: '{row[0]}' step was interrupted (process exited mid-step)"
    return None


async def _check_pipeline_steps(
    run_id: int,
    role: str,
    prompt: str | None,
    project_dir: str | None,
    session: object,
) -> str | None:
    """Check pipeline execution within an active DB session."""
    from sqlalchemy import func, select

    from sova.db.models import StepExecution, TaskRun

    task_run = await session.get(TaskRun, run_id)
    if task_run is None:
        return None

    sentinel_active = task_run.current_step == "agent"

    stmt = select(func.count()).where(StepExecution.task_run_id == run_id)
    result = await session.execute(stmt)
    step_count = result.scalar() or 0

    if sentinel_active and step_count == 0:
        diag = _collect_bypass_diagnostics(task_run.started_at, task_run.worktree_path, project_dir)
        return _build_bypass_message(role, task_run.pr_number, prompt, run_id, diagnostics=diag)

    if role == "developer" and step_count > 0 and task_run.pr_number is None:
        incomplete = await _check_incomplete_pr(run_id, session)
        if incomplete:
            return incomplete

    return await _check_interrupted_step(run_id, session)


async def _validate_pipeline_outcome(run_id: int, agent: AgentState) -> str | None:
    """Validate that a pipeline-based role actually executed its workflow steps.

    Returns an error message if the pipeline was bypassed, None if OK or
    not a pipeline role.
    """
    if not agent.role or agent.role not in _PIPELINE_ROLES:
        return None

    try:
        from sova.db.session import get_session

        async with await get_session(project_dir=agent.project_dir) as session:
            async with session.begin():
                return await _check_pipeline_steps(
                    run_id,
                    agent.role,
                    agent.prompt,
                    agent.project_dir,
                    session,
                )
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

        from sova.dashboard.services.agent_recovery import rollback_issue_state

        try:
            await rollback_issue_state(run_id, project_dir)
        except Exception:
            log.debug("task_run.rollback_on_downgrade_failed", run_id=run_id, exc_info=True)
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
