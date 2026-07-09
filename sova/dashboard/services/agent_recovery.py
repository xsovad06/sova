"""Stale run detection, PID liveness checks, and interrupted run management."""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from cachetools import TTLCache

from sova.utils.logging import get_logger

log = get_logger(component="dashboard.control.recovery")

_SENTINEL_NO_PR = -1  # cached "no PR exists" marker (distinct from None = not cached)

_SYNTHESIS_TTL_SECONDS = 60
# TTL cache for synthesized PR actions: (issue, pr) -> actions list or None
_synthesis_cache: TTLCache[tuple[str, int], list[dict] | None] = TTLCache(maxsize=256, ttl=_SYNTHESIS_TTL_SECONDS)
# Issue-level cache to avoid find_pr_for_issue shell call on cache hit
_issue_pr_cache: TTLCache[str, int | None] = TTLCache(maxsize=256, ttl=_SYNTHESIS_TTL_SECONDS)
# Reentrant lock protecting both caches from concurrent thread access
_cache_lock = threading.RLock()

_MERGE_ROLES = frozenset({"integrate-pr", "approve-merge"})


def _check_issue_cache(issue_number: str) -> tuple[bool, int | None, list[dict] | None]:
    """Check issue-level and synthesis caches.

    Returns (fully_resolved, pr_number_or_None, result).
    - fully_resolved=True, pr=None, result=None: no PR exists (sentinel cached)
    - fully_resolved=True, pr=N, result=actions: synthesis cache hit
    - fully_resolved=False, pr=N, result=None: PR known but synthesis not cached
    - fully_resolved=False, pr=None, result=None: issue cache miss entirely
    """
    with _cache_lock:
        try:
            cached_pr = _issue_pr_cache[issue_number]
        except KeyError:
            return False, None, None
        if cached_pr == _SENTINEL_NO_PR:
            return True, None, None
        if cached_pr is not None:
            try:
                synth_result = _synthesis_cache[(issue_number, cached_pr)]
                return True, cached_pr, synth_result
            except KeyError:
                pass
            # PR number is known from cache; synthesis cache miss means we skip find_pr shell call
            return False, cached_pr, None
        return False, None, None


def _deduplicate_reviews(reviews: list) -> dict:
    """Keep latest review per reviewer, using timestamp comparison."""
    from sova.adapters.base import PRReview  # noqa: F811

    latest_by_reviewer: dict[str, PRReview] = {}
    for review in reviews:
        existing = latest_by_reviewer.get(review.reviewer)
        if existing is None:
            latest_by_reviewer[review.reviewer] = review
            continue
        try:
            new_ts = datetime.fromisoformat(review.submitted_at.replace("Z", "+00:00"))
            old_ts = datetime.fromisoformat(existing.submitted_at.replace("Z", "+00:00"))
            if new_ts > old_ts:
                latest_by_reviewer[review.reviewer] = review
        except (ValueError, AttributeError):
            if review.submitted_at > existing.submitted_at:
                latest_by_reviewer[review.reviewer] = review
    return latest_by_reviewer


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


async def recover_stale_runs(project_dir: Path | None = None) -> list[dict]:
    """Detect and mark stale non-terminal TaskRuns on dashboard startup."""
    try:
        from sqlalchemy import select

        from sova.dashboard.services.work_service import _TERMINAL
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        interrupted = []

        _SKIP_RECOVERY = _TERMINAL | {"paused"}

        async with await get_session(project_dir=project_dir) as session:
            async with session.begin():
                stmt = select(TaskRun).where(TaskRun.status.notin_(_SKIP_RECOVERY))
                result = await session.execute(stmt)
                stale_runs = result.scalars().all()

                for run in stale_runs:
                    if run.pid and _is_process_alive(run.pid):
                        log.info("recovery.still_alive", run_id=run.id, pid=run.pid)
                        continue

                    was_status = run.status
                    final_status = "interrupted"

                    # Check if agent completed and wrote a handoff before dying
                    try:
                        from sova.dashboard.services import handoff_service

                        hf = handoff_service.get_handoff(project_dir, issue=run.issue_number)
                        if hf and hf.get("status") == "awaiting_action":
                            hf_time_str = hf.get("created_at")
                            run_start = run.started_at or datetime.min.replace(tzinfo=timezone.utc)
                            if run_start.tzinfo is None:
                                run_start = run_start.replace(tzinfo=timezone.utc)
                            if hf_time_str:
                                hf_dt = datetime.fromisoformat(hf_time_str.replace("Z", "+00:00"))
                                if hf_dt.tzinfo is None:
                                    hf_dt = hf_dt.replace(tzinfo=timezone.utc)
                            else:
                                hf_dt = None
                            if hf_dt is not None and hf_dt >= run_start:
                                final_status = "done"
                                cost = hf.get("details", {}).get("cost_usd")
                                if cost is not None:
                                    from decimal import Decimal

                                    run.total_cost_usd = Decimal(str(cost))
                                run.error_message = None
                                log.info(
                                    "recovery.completed_with_handoff",
                                    run_id=run.id,
                                    issue=run.issue_number,
                                )
                    except Exception:
                        log.debug("recovery.handoff_check_failed", run_id=run.id, exc_info=True)

                    if run.pr_number and run.role:
                        cmd_name = run.role.removeprefix("command:").removeprefix("/").split()[0]
                        if cmd_name in _MERGE_ROLES:
                            try:
                                from sova.dashboard.services.agent_lifecycle import _check_pr_merged_on_failure

                                if await _check_pr_merged_on_failure(run.pr_number, project_dir):
                                    final_status = "done"
                                    run.error_message = (
                                        f"Agent process died but PR #{run.pr_number} was merged successfully"
                                    )
                                    log.info(
                                        "recovery.merge_succeeded_despite_crash",
                                        run_id=run.id,
                                        pr=run.pr_number,
                                    )
                            except Exception:
                                log.debug("recovery.merge_check_failed", run_id=run.id, exc_info=True)

                    run.status = final_status
                    if final_status == "interrupted":
                        run.error_message = f"Stale run recovered on startup (was {was_status!r})"
                    run.ended_at = datetime.now(timezone.utc)
                    if final_status == "interrupted":
                        interrupted.append(
                            {
                                "run_id": run.id,
                                "issue": run.issue_number,
                                "role": run.role,
                                "pid": run.pid,
                            }
                        )
                    log.warning(
                        "recovery.stale_run",
                        run_id=run.id,
                        issue=run.issue_number,
                        pid=run.pid,
                        was_status=was_status,
                        final_status=final_status,
                    )

        if interrupted:
            log.info("recovery.complete", interrupted_count=len(interrupted))
        return interrupted
    except Exception:
        log.warning("recovery.failed", exc_info=True)
        return []


async def get_interrupted_runs(limit: int = 5) -> list[dict]:
    """Get recently interrupted task runs."""
    from sova.dashboard.services import run_service
    from sova.db.session import get_session

    try:
        async with await get_session() as session:
            async with session.begin():
                return await run_service.list_runs(session, status="interrupted", limit=limit)
    except Exception:
        log.debug("interrupted_runs.query_failed", exc_info=True)
        return []


async def dismiss_interrupted_runs() -> int:
    """Mark all interrupted runs as failed. Returns count of dismissed runs."""
    from sqlalchemy import update

    from sova.db.models import TaskRun
    from sova.db.session import get_session

    try:
        async with await get_session() as session:
            async with session.begin():
                stmt = (
                    update(TaskRun)
                    .where(TaskRun.status == "interrupted")
                    .values(status="failed", error_message="Dismissed by user")
                )
                result = await session.execute(stmt)
                return result.rowcount
    except Exception:
        log.debug("dismiss_interrupted.failed", exc_info=True)
        return 0


async def get_sova_review_verdict(issue_number: str) -> dict:
    """Query the DB for the most recent SOVA reviewer verdict on an issue.

    Returns adapter-agnostic review state from SOVA's own TaskRun records,
    independent of any platform-specific review mechanism (GitHub reviews, etc.).

    When handoff_json is present, the verdict is derived from it (authoritative).
    When a review run completed successfully but has no handoff_json (e.g.
    command:review-pr which posts to GitHub but doesn't write handoff), the
    verdict defaults to "revise" since a review that found nothing would have
    been an approval.
    """
    from sqlalchemy import func, select

    from sova.db.models import TaskRun
    from sova.db.session import get_session

    no_review: dict = {
        "has_sova_review": False,
        "verdict": None,
        "finding_count": 0,
        "reviewed_at": None,
    }

    try:
        async with await get_session() as session:
            # Include non-done runs: a reviewer that posted findings but crashed
            # on a post-review side-effect ("failed") or was killed during
            # cleanup ("interrupted") still produced a valid verdict.
            stmt = (
                select(TaskRun)
                .where(
                    TaskRun.issue_number == issue_number.lstrip("#").strip(),
                    TaskRun.role.in_(["reviewer", "command:review-pr"]),
                    TaskRun.status.in_(["done", "failed", "interrupted"]),
                    TaskRun.handoff_json.isnot(None),
                )
                .order_by(func.coalesce(TaskRun.ended_at, TaskRun.started_at).desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            run = result.scalar_one_or_none()

            if not run:
                return no_review

            handoff = run.handoff_json
            if handoff is None:
                return {
                    "has_sova_review": True,
                    "verdict": "revise",
                    "finding_count": 0,
                    "reviewed_at": ts.isoformat() if (ts := run.ended_at or run.started_at) else None,
                }

            next_action = handoff.get("next_action", "")
            findings = handoff.get("pending_findings", [])

            if next_action == "approve":
                verdict = "approve"
            elif findings:
                max_sev = max((f.get("severity", 0) for f in findings), default=0)
                verdict = "block" if max_sev >= 7 else "revise"
            else:
                verdict = "approve"

            return {
                "has_sova_review": True,
                "verdict": verdict,
                "finding_count": len(findings),
                "reviewed_at": ts.isoformat() if (ts := run.ended_at or run.started_at) else None,
            }
    except Exception:
        log.debug("sova_review_verdict.query_failed", issue=issue_number, exc_info=True)
        return no_review


def _summarize_ci_checks(checks: list | None) -> str:
    """Summarize CI check results into a single status string."""
    from sova.git.operations import CheckConclusion, CheckStatus

    if checks is None:
        return "unknown"
    if not checks:
        return "none"
    if all(c.status == CheckStatus.COMPLETED and c.conclusion == CheckConclusion.SUCCESS for c in checks):
        return "passed"
    if any(c.status == CheckStatus.COMPLETED and c.conclusion == CheckConclusion.FAILURE for c in checks):
        return "failed"
    if any(c.status == CheckStatus.IN_PROGRESS for c in checks):
        return "pending"
    return "passed"


def _load_repo_config() -> tuple[str, str] | None:
    """Load project config and return (repo, gh_user), or None if unavailable."""
    from sova.config.loader import load_config
    from sova.dashboard.project_context import get_project_dir

    project_dir = get_project_dir()
    if not project_dir:
        return None
    try:
        cfg = load_config(project_dir)
    except Exception:
        return None
    if not cfg.github_repo:
        return None
    return cfg.github_repo, cfg.github_user


async def get_pr_status_for_issue(issue_number: str) -> dict:
    """Get PR status for an issue -- approval state, CI, mergeability."""
    from sova.git.operations import find_pr_for_issue, get_ci_checks, get_pr_status

    repo_cfg = _load_repo_config()
    if not repo_cfg:
        return {"has_pr": False}
    repo, gh_user = repo_cfg

    pr_info = await find_pr_for_issue(issue_number, repo=repo, github_user=gh_user)
    if not pr_info:
        return {"has_pr": False}

    try:
        status = await get_pr_status(pr_info.number, repo=repo, github_user=gh_user)
    except Exception:
        log.debug("pr_status.fetch_failed", issue=issue_number, exc_info=True)
        return {"has_pr": True, "pr_number": pr_info.number, "error": "Failed to fetch PR status"}

    ci_summary = "unknown"
    try:
        checks = await get_ci_checks(pr_info.number, repo=repo, github_user=gh_user)
        ci_summary = _summarize_ci_checks(checks)
    except Exception:
        log.debug("ci_checks.fetch_failed", pr=pr_info.number, exc_info=True)

    sova_review = await get_sova_review_verdict(issue_number)

    return {
        "has_pr": True,
        "pr_number": status.number,
        "state": status.state,
        "review_decision": status.review_decision,
        "mergeable": status.mergeable,
        "ci_status": ci_summary,
        "title": status.title,
        "url": status.url,
        "is_approved": status.is_approved,
        "is_mergeable": status.is_mergeable,
        "sova_review": sova_review,
    }


async def _has_active_run(issue_number: str) -> bool:
    """Check if there are non-terminal TaskRuns for this issue."""
    from sqlalchemy import select

    from sova.dashboard.services.work_service import _TERMINAL
    from sova.db.models import TaskRun
    from sova.db.session import get_session

    async with await get_session() as session:
        async with session.begin():
            stmt = (
                select(TaskRun.id)
                .where(
                    TaskRun.issue_number == issue_number,
                    TaskRun.status.notin_(_TERMINAL),
                )
                .limit(1)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none() is not None


def _interpret_reviews(latest_by_reviewer: dict) -> tuple[bool, int, int]:
    """Interpret review states, excluding dismissed and bot reviews.

    Returns (has_changes_requested, approvals, human_review_count).
    """
    has_changes_requested = False
    approvals = 0
    human_review_count = 0

    for review in latest_by_reviewer.values():
        if review.state == "DISMISSED" or review.is_bot:
            continue
        human_review_count += 1
        if review.state == "CHANGES_REQUESTED":
            has_changes_requested = True
        elif review.state == "APPROVED":
            approvals += 1

    return has_changes_requested, approvals, human_review_count


def _build_address_review_action(issue_number: str, pr_number: int) -> list[dict]:
    """Build the 'Address Review' action list."""
    return [
        {
            "id": "address_review",
            "label": "Address Review",
            "description": "Address review findings from PR reviewers",
            "style": "approve",
            "mode": "agent",
            "command": "",
            "args": {"issue": issue_number, "pr": pr_number, "role": "developer"},
            "auto_execute": False,
        },
    ]


def _build_integrate_actions(issue_number: str, pr_number: int) -> list[dict]:
    """Build the 'Integrate PR' action list."""
    return [
        {
            "id": "integrate",
            "label": "Integrate PR",
            "description": "All reviews approved -- rebase, merge, cleanup, and learn",
            "style": "approve",
            "mode": "claude-command",
            "command": f"/integrate-pr {pr_number}",
            "args": {"issue": issue_number, "pr": pr_number},
            "auto_execute": False,
        },
    ]


async def _fetch_and_interpret_reviews(issue_number: str, pr_number: int, cache_key: tuple) -> list[dict] | None:
    """Fetch reviews from adapter, deduplicate, interpret, and build actions."""
    from sova.adapters import create_adapter
    from sova.config.loader import load_config
    from sova.dashboard.project_context import get_project_dir

    cfg = load_config(get_project_dir())
    try:
        adapter = create_adapter(cfg)
        reviews = await adapter.get_pr_reviews(pr_number)
    except Exception:
        log.debug("synthesize.fetch_reviews_failed", issue=issue_number, exc_info=True)
        with _cache_lock:
            _synthesis_cache[cache_key] = None
        return None

    if not reviews:
        with _cache_lock:
            _synthesis_cache[cache_key] = None
        return None

    latest_by_reviewer = _deduplicate_reviews(reviews)
    has_changes_requested, approvals, human_review_count = _interpret_reviews(latest_by_reviewer)

    actions: list[dict] | None = None
    if has_changes_requested:
        actions = _build_address_review_action(issue_number, pr_number)
    elif approvals > 0 and approvals == human_review_count:
        actions = _build_integrate_actions(issue_number, pr_number)

    with _cache_lock:
        _synthesis_cache[cache_key] = actions
    return actions


async def synthesize_pr_actions(issue_number: str) -> list[dict] | None:
    """Synthesize HandoffAction-shaped dicts from PR review state.

    Returns None if no PR exists or no actionable review state found.
    Called only when no handoff file exists and no agent is running for the issue.
    """
    from sova.git.operations import find_pr_for_issue

    issue_number = issue_number.lstrip("#").strip()

    repo_cfg = _load_repo_config()
    if not repo_cfg:
        return None
    repo, gh_user = repo_cfg

    fully_resolved, cached_pr, cached_result = _check_issue_cache(issue_number)
    if fully_resolved:
        return cached_result

    # Use cached PR number if available, otherwise call find_pr_for_issue
    if cached_pr is not None:
        pr_number = cached_pr
    else:
        pr_info = await find_pr_for_issue(issue_number, repo=repo, github_user=gh_user)
        if not pr_info:
            with _cache_lock:
                _issue_pr_cache[issue_number] = _SENTINEL_NO_PR
            return None
        pr_number = pr_info.number
        with _cache_lock:
            _issue_pr_cache[issue_number] = pr_number

    cache_key = (issue_number, pr_number)
    with _cache_lock:
        try:
            return _synthesis_cache[cache_key]
        except KeyError:
            pass

    try:
        if await _has_active_run(issue_number):
            with _cache_lock:
                _synthesis_cache[cache_key] = None
            return None
    except Exception:
        log.debug("synthesize.active_run_check_failed", issue=issue_number, exc_info=True)

    return await _fetch_and_interpret_reviews(issue_number, pr_number, cache_key)


def invalidate_synthesis_cache(issue: str, pr: int) -> None:
    """Invalidate synthesized PR actions cache for an issue/PR pair."""
    with _cache_lock:
        _synthesis_cache.pop((issue, pr), None)
        _issue_pr_cache.pop(issue, None)


async def get_synthesized_handoff() -> dict | None:
    """Build a handoff-shaped dict from PR review state for recent done runs.

    Queries recent completed developer/review runs that have a PR, then
    synthesizes actionable handoff buttons from the PR's review state.
    """
    from datetime import timedelta

    from sqlalchemy import func, select

    from sova.db.models import TaskRun
    from sova.db.session import get_session

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        async with await get_session() as session:
            async with session.begin():
                stmt = (
                    select(TaskRun)
                    .where(
                        TaskRun.status == "done",
                        TaskRun.role.in_(["developer", "command:review-pr"]),
                        TaskRun.pr_number.isnot(None),
                        func.coalesce(TaskRun.ended_at, TaskRun.started_at) >= cutoff,
                    )
                    .order_by(func.coalesce(TaskRun.ended_at, TaskRun.started_at).desc())
                    .limit(2)
                )
                result = await session.execute(stmt)
                runs = result.scalars().all()

        for run in runs:
            if not run.issue_number or run.pr_number is None:
                continue
            actions = await synthesize_pr_actions(run.issue_number)
            if actions:
                return {
                    "source": "pr-review-state",
                    "status": "awaiting_action",
                    "issue": run.issue_number,
                    "pr_number": run.pr_number,
                    "summary": "Actions synthesized from PR review state",
                    "next_actions": actions,
                }
    except Exception:
        log.debug("synthesized_handoff.failed", exc_info=True)

    return None
