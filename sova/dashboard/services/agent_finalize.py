"""Agent finalization: wait-and-finalize, crash recovery, merge queue checks.

Handles what happens when an agent process exits: DB finalization, outcome
validation, merge queue monitoring, crash recovery cleanup, and auto-handoff.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from sova.dashboard.services.agent_context import (
    _resolve_issue_from_pr,
)
from sova.dashboard.services.agent_db import (
    _downgrade_to_failed,
    _finalize_task_run,
    _validate_command_outcome,
    _validate_pipeline_outcome,
)
from sova.dashboard.services.agent_pool import (
    AgentState,
    CompletedAgent,
    ProjectAgents,
)
from sova.dashboard.services.agent_validation import (
    _check_pr_merged_on_failure,
)
from sova.git.merge import delete_remote_branch, handle_post_merge_state
from sova.git.pr import get_pr_branch
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.control")

_MERGE_ROLES = frozenset({"integrate-pr", "approve-merge"})

_DB_TERMINAL_POLL_INTERVAL = 30.0


async def _crash_recovery_cleanup(agent: AgentState) -> None:
    """Clean up branch and issue state when merge succeeded but agent crashed."""
    from sova.adapters import create_adapter
    from sova.config.loader import load_config

    if not agent.pr_number:
        log.warning("finalize.crash_recovery_no_pr", issue=agent.issue)
        return

    cfg = load_config(agent.project_dir)
    repo = cfg.github_repo
    github_user = cfg.github_user

    if not repo:
        log.warning("finalize.crash_recovery_no_github_repo", pr=agent.pr_number)
        return

    branch_name = await get_pr_branch(
        agent.pr_number,
        repo=repo,
        github_user=github_user,
    )

    if branch_name:
        deleted = await delete_remote_branch(
            branch_name,
            repo=repo,
            github_user=github_user,
        )
        if deleted:
            log.info(
                "finalize.crash_recovery_branch_deleted",
                pr=agent.pr_number,
                branch=branch_name,
            )
    else:
        log.warning(
            "finalize.crash_recovery_branch_lookup_failed",
            pr=agent.pr_number,
        )

    issue_number = agent.issue or await _resolve_issue_from_pr(
        agent.pr_number,
        agent.project_dir,
    )

    if not issue_number:
        log.warning(
            "finalize.crash_recovery_no_issue_number",
            pr=agent.pr_number,
            had_agent_issue=bool(agent.issue),
        )
        return

    adapter = create_adapter(cfg)
    await handle_post_merge_state(
        issue_number,
        post_merge_state=cfg.integration.post_merge_state,
        repo=repo,
        github_user=github_user,
        adapter=adapter,
    )
    log.info(
        "finalize.crash_recovery_state_transitioned",
        pr=agent.pr_number,
        issue=issue_number,
        state=cfg.integration.post_merge_state,
    )


async def _check_merge_queue_on_failure(
    agent: AgentState,
    run_id: int | None,
    status: str,
    exit_code: int,
) -> tuple[str, int]:
    """Check if the PR is in a merge queue after a merge-role agent fails.

    If the PR is queued, creates a MergeQueueEntry for the background
    monitor and marks the run as "done" (the agent's work succeeded;
    the merge is tracked). Returns the updated (status, exit_code).
    """
    try:
        from sova.config.loader import load_config
        from sova.dashboard.services.merge_queue_monitor import create_merge_queue_entry
        from sova.git.merge import get_merge_queue_status

        cfg = load_config(agent.project_dir)
        repo = cfg.github_repo
        if not repo:
            return status, exit_code

        queue_status = await get_merge_queue_status(
            agent.pr_number,
            repo=repo,
            github_user=cfg.github_user,
        )
        if not queue_status.in_queue:
            return status, exit_code

        branch = ""
        try:
            from sova.utils.gh import resolve_gh_env
            from sova.utils.shell import run

            env = await resolve_gh_env(cfg.github_user)
            result = await run(
                "gh",
                "pr",
                "view",
                str(agent.pr_number),
                "--repo",
                repo,
                "--json",
                "headRefName",
                "--jq",
                ".headRefName",
                env=env,
            )
            if result.success and result.stdout.strip():
                branch = result.stdout.strip()
        except Exception:
            pass

        await create_merge_queue_entry(
            pr_number=agent.pr_number,
            repo=repo,
            project_dir=agent.project_dir or Path.cwd(),
            issue_number=agent.issue,
            task_run_id=run_id,
            github_user=cfg.github_user,
            branch_name=branch,
        )

        from sova.dashboard.services.feed_service import FeedEventSeverity, emit_safe

        emit_safe(
            f"PR #{agent.pr_number} enqueued in merge queue (monitored)",
            severity=FeedEventSeverity.info,
            detail=f"Agent exited but merge queue monitor is tracking PR #{agent.pr_number}",
            category="merge_queue",
            metadata={"pr_number": agent.pr_number, "repo": repo},
        )

        log.info(
            "finalize.pr_in_merge_queue",
            run_id=run_id,
            pr=agent.pr_number,
            queue_state=queue_status.state,
        )
        return "done", 0
    except Exception:
        log.debug("finalize.merge_queue_check_failed", run_id=run_id, exc_info=True)
        return status, exit_code


async def _is_run_terminal_in_db(run_id: int, project_dir: Path | None) -> bool:
    """Check if a TaskRun has reached terminal status in the database.

    Safety net: detects when the subprocess has finalized the DB record
    but the process is still alive (e.g., cleanup hang, stuck I/O).
    """
    from sova.dashboard.services.agent_db import _TERMINAL_STATUSES
    from sova.db.models import TaskRun
    from sova.db.session import get_session

    try:
        async with await get_session(project_dir=project_dir) as session:
            task_run = await session.get(TaskRun, run_id)
            if task_run is None:
                return True
            return task_run.status in _TERMINAL_STATUSES
    except Exception:
        log.debug("finalize.terminal_check_failed", run_id=run_id, exc_info=True)
        return False


async def _check_merge_queue_marker_file(agent: AgentState, run_id: int | None) -> None:
    """Check for a merge-queue.json marker written by the agent command.

    When an agent writes .claude/agent-control/merge-queue.json after
    enqueuing a PR, create a MergeQueueEntry proactively so the monitor
    tracks it even if the agent exits cleanly.
    """
    project_dir = agent.project_dir
    if project_dir is None:
        return

    control_dir = Path(project_dir) / ".claude" / "agent-control"
    if not control_dir.exists():
        return

    # Support both per-PR naming (merge-queue-{N}.json) and legacy shared file
    marker_paths = list(control_dir.glob("merge-queue-*.json"))
    legacy_path = control_dir / "merge-queue.json"
    if legacy_path.exists():
        marker_paths.append(legacy_path)

    if not marker_paths:
        return

    for marker_path in marker_paths:
        try:
            data = json.loads(marker_path.read_text())
            pr_number = data.get("pr_number")
            repo = data.get("repo", "")
            issue_number = data.get("issue_number")
            branch_name = data.get("branch_name", "")

            if not pr_number or not repo:
                marker_path.unlink(missing_ok=True)
                continue

            from sova.dashboard.services.merge_queue_monitor import create_merge_queue_entry

            await create_merge_queue_entry(
                pr_number=int(pr_number),
                repo=repo,
                project_dir=project_dir,
                issue_number=str(issue_number) if issue_number else None,
                task_run_id=run_id,
                github_user=data.get("github_user", ""),
                branch_name=branch_name,
            )

            from sova.dashboard.services.feed_service import FeedEventSeverity, emit_safe

            emit_safe(
                f"PR #{pr_number} enqueued in merge queue (monitored)",
                severity=FeedEventSeverity.info,
                category="merge_queue",
                metadata={"pr_number": pr_number, "repo": repo},
            )

            log.info("finalize.merge_queue_marker_processed", pr=pr_number, repo=repo)

            marker_path.unlink(missing_ok=True)
        except Exception:
            log.debug("finalize.merge_queue_marker_failed", path=str(marker_path), exc_info=True)


async def _wait_with_terminal_check(agent: AgentState) -> int:
    """Wait for process exit, killing the process if its DB record becomes terminal.

    For direct-spawn pipeline runs, this is a safety net: if the process
    hangs after finalizing the DB record, it gets killed. For Claude-based
    spawns (reviewer, commands), it also catches the case where sova run
    finalizes but the outer claude -p wrapper is stuck.
    """
    while True:
        try:
            return await asyncio.wait_for(agent.process.wait(), timeout=_DB_TERMINAL_POLL_INTERVAL)
        except TimeoutError:
            pass

        if await _is_run_terminal_in_db(agent.run_id, agent.project_dir):
            log.warning(
                "finalize.db_terminal_process_alive",
                run_id=agent.run_id,
                pid=agent.process.pid,
            )
            try:
                await agent.process.stop(timeout=5.0)
            except Exception:
                try:
                    agent.process._proc.kill()
                except OSError:
                    pass
                else:
                    try:
                        await asyncio.wait_for(agent.process._proc.wait(), timeout=3.0)
                    except (TimeoutError, asyncio.CancelledError):
                        pass
            rc = agent.process.returncode
            return rc if rc is not None else -1


async def _wait_and_finalize(pa: ProjectAgents, agent: AgentState) -> None:
    """Wait for the process to exit, then finalize the DB record."""
    from sova.dashboard.services.agent_handoff import _process_auto_handoff
    from sova.dashboard.services.agent_resource import _finalize_resource_monitoring

    if agent.process is None:
        return

    try:
        exit_code = await _wait_with_terminal_check(agent)
    except asyncio.CancelledError:
        if agent.process is not None and agent.process.is_running:
            try:
                agent.process._proc.terminate()
            except OSError:
                pass
            else:
                try:
                    await asyncio.wait_for(agent.process._proc.wait(), timeout=2.0)
                except (TimeoutError, asyncio.CancelledError):
                    try:
                        agent.process._proc.kill()
                    except OSError:
                        pass
        raise
    run_id = agent.run_id

    status = "done" if exit_code == 0 else "failed"

    if exit_code != 0 and agent.pr_number is not None:
        cmd_name = agent.role.removeprefix("command:").removeprefix("/").split()[0]
        if cmd_name in _MERGE_ROLES:
            if await _check_pr_merged_on_failure(agent.pr_number, agent.project_dir):
                log.info(
                    "finalize.merge_succeeded_despite_crash",
                    run_id=run_id,
                    pr=agent.pr_number,
                    exit_code=exit_code,
                )
                status = "done"
                exit_code = 0

                try:
                    await _crash_recovery_cleanup(agent)
                except Exception:
                    log.warning(
                        "finalize.crash_recovery_cleanup_failed",
                        pr=agent.pr_number,
                        exc_info=True,
                    )
            else:
                status, exit_code = await _check_merge_queue_on_failure(
                    agent,
                    run_id,
                    status,
                    exit_code,
                )

    if exit_code == 0 and agent.pr_number is not None:
        await _check_merge_queue_marker_file(agent, run_id)

    cost = agent.last_result_cost or 0.0

    if agent.output_writer:
        try:
            await agent.output_writer.close()
        except Exception:
            log.warning("output_writer.close_failed", run_id=run_id, exc_info=True)

    await _finalize_resource_monitoring(agent)

    # Finalize the DB record BEFORE removing from pa.agents so the
    # liveness sweep (which skips managed run_ids) cannot race us and
    # stamp the run "interrupted" in the window between pop and finalize.
    status_changed = await _finalize_task_run(run_id, exit_code=exit_code, agent=agent)

    # Validate that command runs actually produced expected outcomes.
    # Downgrades "done" to "failed" if the command exited cleanly but
    # didn't actually perform its core work (e.g., address-pr without
    # pushing commits, review-pr without posting a review).
    if exit_code == 0 and run_id:
        failure_reason = await _validate_command_outcome(run_id, agent)
        if not failure_reason:
            failure_reason = await _validate_pipeline_outcome(run_id, agent)
        if failure_reason:
            await _downgrade_to_failed(run_id, failure_reason, agent.project_dir)
            status = "failed"
            exit_code = 1
            log.warning("agent.outcome_validation_failed", run_id=run_id, reason=failure_reason)

    if status_changed:
        try:
            from sova.dashboard.routers.agents import _ws_manager

            await _ws_manager.broadcast_event("graph_invalidated", agent.project_dir)
        except Exception:
            log.debug("ws.graph_invalidated_failed", run_id=run_id, exc_info=True)

    async with pa._lock:
        pa.agents.pop(run_id, None)
        pa.recently_completed.append(
            CompletedAgent(
                run_id=run_id,
                issue=agent.issue,
                role=agent.role,
                status=status,
                cost=cost,
            )
        )

    # Finalize lifecycle phase (only for issue-based runs)
    if agent.issue:
        from sova.dashboard.services.agent_approval import _finalize_lifecycle_phase

        await _finalize_lifecycle_phase(run_id, exit_code, agent.last_result_cost or 0.0, agent.project_dir)

    try:
        from sova.config.loader import load_config
        from sova.ipc.notifications import notify

        cfg = load_config(agent.project_dir)
        role_label = agent.role.split(":")[-1].replace("-", " ").title()
        project_name = agent.project_dir.name
        issue_label = f"#{agent.issue}" if agent.issue else role_label
        group_key = f"sova-{agent.issue or agent.role or 'run'}"
        if exit_code != 0:
            notify(
                cfg.notification,
                "SOVA",
                f"{project_name} | Exit code {exit_code}",
                subtitle=f"{role_label} failed {issue_label}",
                group=group_key,
            )
        else:
            msg = project_name
            if cost:
                msg += f" | ${cost:.4f}"
            notify(
                cfg.notification,
                "SOVA",
                msg,
                subtitle=f"{role_label} finished {issue_label}",
                group=group_key,
            )
    except Exception:
        log.debug("notify.failed", run_id=run_id, exc_info=True)

    log.info("agent.completed", run_id=run_id, issue=agent.issue, status=status, cost=cost)

    # Schedule non-blocking telemetry push if hub is configured
    try:
        from sova.config.loader import load_config
        from sova.dashboard.services.agent_resource import _background_tasks

        tel_cfg = load_config(agent.project_dir)
        if tel_cfg.telemetry.hub_url and run_id:
            from sova.dashboard.services.telemetry_push import push_telemetry

            t = asyncio.create_task(push_telemetry(run_id, agent.project_dir, tel_cfg))
            _background_tasks.add(t)
            t.add_done_callback(_background_tasks.discard)
    except Exception:
        log.debug("telemetry.schedule_failed", run_id=run_id, exc_info=True)

    if exit_code == 0:
        await _process_auto_handoff(agent)
