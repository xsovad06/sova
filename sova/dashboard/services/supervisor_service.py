"""Supervisor dashboard service: decision logs, the approval plan, the task queue and the CI budget payload."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from sova.config.loader import load_config
from sova.dashboard.services import daemon_registry
from sova.db.models import SupervisorDecision
from sova.db.session import get_session
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.config.models import ProjectConfig
    from sova.supervisor.progression import ProgressionDecision

log = get_logger(component="dashboard.service.supervisor")

# In-memory plan store. Ephemeral: cleared on server restart. The daemon
# rebuilds the plan on the next poll cycle so nothing is permanently lost.
# Project-keyed to isolate plans in multi-project mode.
_pending_plan: dict[str, list[ProgressionDecision]] = {}
_plan_reasoning: dict[str, str | None] = {}
_plan_deferred: dict[str, list[dict]] = {}

# In-memory planner health counters, keyed by project slug (same key space as
# ``_pending_plan``). Deliberately not persisted: this is a diagnostic
# counter, not authoritative state, and is rebuilt from scratch on the next
# poll cycle after a restart.
_planner_health: dict[str, dict[str, int | str | None]] = {}


def resolve_project_slug(github_repo: str, project_dir: Path | None = None) -> str:
    """Derive a stable plan-store key from config.

    Uses ``github_repo`` when set.  Falls back to the project directory name
    so two projects without a repo slug never collide on the same key.
    """
    if github_repo:
        return github_repo
    if project_dir is not None:
        return f"local:{project_dir.name}"
    return ""


def get_pending_plan(project_slug: str) -> list[ProgressionDecision]:
    """Return the current pending approval plan for a project (may be empty)."""
    return list(_pending_plan.get(project_slug, []))


def get_plan_reasoning(project_slug: str) -> str | None:
    """Return the LLM planner's reasoning text for a project, if any."""
    return _plan_reasoning.get(project_slug)


def get_plan_deferred(project_slug: str) -> list[dict]:
    """Return the LLM planner's deferred action list for a project, if any."""
    return list(_plan_deferred.get(project_slug, []))


def set_pending_plan(
    decisions: list[ProgressionDecision],
    *,
    project_slug: str,
    reasoning: str | None = None,
    deferred: list[dict] | None = None,
) -> None:
    """Replace the pending plan for a project with a new set of decisions."""
    global _pending_plan, _plan_reasoning, _plan_deferred  # noqa: PLW0603
    _pending_plan[project_slug] = list(decisions)
    _plan_reasoning[project_slug] = reasoning
    _plan_deferred[project_slug] = list(deferred) if deferred else []


def restore_plan_items(
    decisions: list[ProgressionDecision],
    *,
    project_slug: str,
    reasoning: str | None = None,
    deferred: list[dict] | None = None,
) -> None:
    """Put decisions taken out of the plan back, without overwriting what was written since.

    The supervisor daemon or another request can write the plan while an approval awaits its engine, so the plan
    is merged, never replaced: a decision whose issue is already in the current plan is skipped, and the
    reasoning and deferred list are restored only when the plan is empty (nothing newer to keep).
    """
    global _pending_plan, _plan_reasoning, _plan_deferred  # noqa: PLW0603
    current = _pending_plan.get(project_slug, [])
    present = {d.issue_number for d in current}
    _pending_plan[project_slug] = [d for d in decisions if d.issue_number not in present] + current
    if not current:
        _plan_reasoning[project_slug] = reasoning
        _plan_deferred[project_slug] = list(deferred) if deferred else []


def remove_plan_items(issue_numbers: set[int], project_slug: str) -> list[ProgressionDecision]:
    """Remove and return decisions for the given issue numbers from a project's plan.

    Decisions not in *issue_numbers* remain in the plan.
    """
    global _pending_plan
    plan = _pending_plan.get(project_slug, [])
    if not plan:
        return []
    removed = [d for d in plan if d.issue_number in issue_numbers]
    if removed:
        _pending_plan[project_slug] = [d for d in plan if d.issue_number not in issue_numbers]
    return removed


async def get_recent_decisions(
    project_dir: Path,
    *,
    project_slug: str | None = None,
    limit: int = 100,
    component: str | None = None,
    event_type: str | None = None,
) -> list[dict]:
    """Return recent supervisor decisions, newest first."""
    async with await get_session(project_dir=project_dir) as session:
        stmt = select(SupervisorDecision)
        if project_slug:
            stmt = stmt.where(SupervisorDecision.project_slug == project_slug)
        if component:
            stmt = stmt.where(SupervisorDecision.component == component)
        if event_type:
            stmt = stmt.where(SupervisorDecision.event_type == event_type)
        stmt = stmt.order_by(SupervisorDecision.created_at.desc()).limit(limit)
        result = await session.execute(stmt)
        rows = result.scalars().all()

    return [
        {
            "id": row.id,
            "component": row.component,
            "event_type": row.event_type,
            "issue_number": row.issue_number,
            "action": row.action,
            "detail": row.detail,
            "metadata": row.metadata_json,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


async def get_decision_counts(project_dir: Path, *, project_slug: str | None = None) -> dict:
    """Return component-level decision counts for the status panel."""
    async with await get_session(project_dir=project_dir) as session:
        stmt = select(SupervisorDecision.component, func.count(SupervisorDecision.id))
        if project_slug:
            stmt = stmt.where(SupervisorDecision.project_slug == project_slug)
        stmt = stmt.group_by(SupervisorDecision.component)
        result = await session.execute(stmt)
        return dict(result.all())


def _empty_planner_health() -> dict:
    return {
        "success_count": 0,
        "failure_count": 0,
        "last_success_at": None,
        "last_failure_at": None,
    }


def record_planner_outcome(project_slug: str, event_type: str) -> None:
    """Record a planner success/failure outcome in memory for the dashboard health widget.

    Ephemeral, like ``_pending_plan``: rebuilt from scratch on daemon restart
    since this is a diagnostic counter, not authoritative state.
    """
    health = _planner_health.setdefault(project_slug, _empty_planner_health())
    now = datetime.now(timezone.utc).isoformat()
    if event_type == "success":
        health["success_count"] = int(health["success_count"]) + 1
        health["last_success_at"] = now
    elif event_type == "failure":
        health["failure_count"] = int(health["failure_count"]) + 1
        health["last_failure_at"] = now


def get_planner_health(project_slug: str | None = None) -> dict:
    """Return planner success/failure counts and last-seen timestamps for the dashboard health widget.

    In-memory only, keyed by project slug. When *project_slug* is None,
    aggregates across every project tracked in this process (mirrors the
    previous DB behaviour of applying no ``WHERE`` filter).
    """
    if project_slug is not None:
        health = _planner_health.get(project_slug)
        return dict(health) if health is not None else _empty_planner_health()

    if not _planner_health:
        return _empty_planner_health()

    success_at = [h["last_success_at"] for h in _planner_health.values() if h["last_success_at"]]
    failure_at = [h["last_failure_at"] for h in _planner_health.values() if h["last_failure_at"]]
    return {
        "success_count": sum(int(h["success_count"]) for h in _planner_health.values()),
        "failure_count": sum(int(h["failure_count"]) for h in _planner_health.values()),
        "last_success_at": max(success_at) if success_at else None,
        "last_failure_at": max(failure_at) if failure_at else None,
    }


class PlanSetupError(Exception):
    """The engine for a plan's decisions could not be built (config, adapter or database), so none ran."""


def _decision_result(decision: ProgressionDecision, result: dict) -> dict:
    """One decision's outcome in the shape every caller reads: ``{ok, issue_number, ...}``.

    The engine reports a failure as an ``error`` key or as ``status: "failed"``, and its text is not all
    author-written (an auto-rebase failure carries the exception text). A failed result is therefore reduced to
    the fixed message below (the engine's own text is logged), so nothing the engine printed reaches a client.
    A result that did not fail passes through with ``ok: True``.
    """
    failed = bool(result.get("error")) or result.get("status") == "failed"
    if failed:
        log.warning(
            "plan.execute_decision_failed",
            issue_number=decision.issue_number,
            action=decision.action.value,
            engine_error=str(result.get("error", ""))[:200],
        )
        return {"ok": False, "issue_number": decision.issue_number, "error": "Decision failed"}
    return {**result, "ok": True, "issue_number": decision.issue_number}


async def execute_plan_decisions(decisions: list[ProgressionDecision], project_dir: Path) -> list[dict]:
    """Execute a list of ProgressionDecision objects via the engine.

    Isolated into its own function so tests can patch it without touching the entire engine lifecycle. Every
    result is ``{"ok": bool, "issue_number": int, ...}`` (see ``_decision_result()``): a failed decision has the
    fixed ``error`` "Decision failed", a raised exception is logged and replaced by the same message, so a caller
    never infers failure from a key being present and no engine text reaches a response. Raises
    ``PlanSetupError`` before any decision runs when the engine cannot be built.
    """
    from sqlalchemy.exc import SQLAlchemyError

    from sova.adapters import create_adapter
    from sova.adapters.base import AdapterError
    from sova.db.session import get_session_factory
    from sova.supervisor.progression import TaskProgressionEngine

    try:
        cfg = await asyncio.to_thread(load_config, project_dir)
        adapter = create_adapter(cfg)
        session_factory = await get_session_factory(project_dir)
        engine = TaskProgressionEngine(
            config=cfg,
            adapter=adapter,
            project_dir=project_dir,
            session_factory=session_factory,
        )
    except (ValueError, KeyError, OSError, RuntimeError, AdapterError, SQLAlchemyError) as exc:
        log.warning("plan.engine_setup_failed", exc_info=True)
        raise PlanSetupError("Failed to prepare the plan's decisions") from exc
    results: list[dict] = []
    for decision in decisions:
        try:
            result = await engine.execute_decision(decision)
        except Exception:  # noqa: BLE001 (one failed decision must not abort the rest of the plan)
            log.warning("plan.execute_decision_failed", issue_number=decision.issue_number, exc_info=True)
            results.append({"ok": False, "issue_number": decision.issue_number, "error": "Decision failed"})
        else:
            results.append(_decision_result(decision, result))
    return results


# Task queue. The callers (the router) hold ``task_queue_lock`` across a read-modify-write.


class QueuePersistError(Exception):
    """The task queue could not be saved (a database error)."""


class QueueConflictError(QueuePersistError):
    """The queue changed since the edit read it: the client can reload and retry, so this is not a server fault."""


def deduplicate(items: list[int]) -> list[int]:
    """Deduplicate preserving first occurrence order."""
    return list(dict.fromkeys(items))


async def persist_queue(project_dir: Path | None, queue: list[int], *, based_on: list[int] | None = None) -> list[int]:
    """Persist task_queue via the shared DB helper and return what was saved.

    ``based_on`` (the queue the edit was computed from) rebases a single-entry edit onto the stored queue, so
    a write another process saved in between (the CLI's post-merge prune, the daemon) is kept. Raises
    ``QueuePersistError`` on a DB-layer failure and its subclass ``QueueConflictError`` on a concurrent write.
    """
    from sqlalchemy.exc import SQLAlchemyError

    from sova.config.db_loader import TaskQueueConflictError, save_task_queue

    try:
        return await save_task_queue(project_dir, queue, based_on=based_on)
    except TaskQueueConflictError as exc:
        log.info("supervisor.queue.persist_conflict")
        raise QueueConflictError("The queue changed; reload and retry") from exc
    except SQLAlchemyError as exc:
        log.error("supervisor.queue.persist_failed", exc_info=True)
        raise QueuePersistError("Failed to persist queue to database") from exc


async def read_queue(project_dir: Path | None) -> list[int]:
    """Read the current task queue from DB (async)."""
    from sova.config.db_loader import load_task_queue

    return await load_task_queue(project_dir)


async def notify_daemon_queue_change(project_dir: Path | None) -> None:
    """Refresh a running daemon's config snapshot after a queue edit.

    Non-fatal: no registered daemon (supervisor disabled, or the dashboard
    running without the scheduler) is a no-op. Does not wake the daemon
    early (``wake=False``): ``_poll_once()`` already reloads config fresh
    every cycle, so the edit lands on the next natural cycle rather than
    triggering a burst of premature polls on rapid successive edits (e.g.
    drag-to-reorder).
    """
    daemon = daemon_registry.get_daemon(project_dir)
    if daemon is None:
        return
    try:
        cfg = await asyncio.to_thread(load_config, project_dir)
        daemon.reload_config(cfg, wake=False)
    except Exception:  # noqa: BLE001 (best-effort daemon notify; queue mutation must not fail on it)
        log.debug("supervisor.queue.daemon_notify_failed", exc_info=True)


# Per-process memo of the projects whose sova.toml queue was already considered for migration.
_toml_migrated: set[str] = set()


def reset_toml_migrated() -> None:
    """Forget which projects were already migrated (for tests)."""
    _toml_migrated.clear()


async def maybe_migrate_queue_from_toml(project_dir: Path | None) -> None:
    """One-time migration: copy task_queue from sova.toml to DB if DB is empty.

    Must be called under the task queue lock to prevent races with concurrent writes.
    """
    cache_key = str(project_dir) if project_dir else "__default__"
    if cache_key in _toml_migrated:
        return

    db_queue = await read_queue(project_dir)
    if db_queue:
        _toml_migrated.add(cache_key)
        return

    try:
        cfg = await asyncio.to_thread(load_config, project_dir)
        toml_queue = cfg.supervisor.task_queue
        if toml_queue:
            log.info("supervisor.queue.toml_migration", count=len(toml_queue))
            await persist_queue(project_dir, toml_queue)
        _toml_migrated.add(cache_key)
    except Exception:  # noqa: BLE001 (one-time migration is best-effort; the queue works without it)
        log.debug("supervisor.queue.toml_migration_skipped", exc_info=True)


# CI budget

CI_BUDGET_ZERO = {
    "total": 0,
    "used": 0,
    "remaining": 0,
    "pct_used": 0.0,
    "warn": False,
    "block": False,
    "cached": False,
}


async def get_ci_budget_payload(cfg: ProjectConfig) -> dict:
    """Current GitHub Actions CI minutes usage and threshold status; the zero state when it cannot be read."""
    from sova.supervisor.ci_budget import _UNLIMITED_SENTINEL, get_ci_budget_tracker

    if not cfg.github_repo:
        return dict(CI_BUDGET_ZERO)

    try:
        tracker = get_ci_budget_tracker(cfg.github_user)
        cached = tracker.is_cached(cfg.github_repo)
        budget = await tracker.get_budget(cfg.github_repo, cfg.github_user)
    except Exception:  # noqa: BLE001 (budget display falls back to zero state on any failure)
        log.warning("ci_budget.endpoint_failed", exc_info=True)
        return dict(CI_BUDGET_ZERO)

    if budget.total == 0 and budget.remaining == 0:
        return dict(CI_BUDGET_ZERO)

    warn = budget.remaining < cfg.supervisor.ci_warn_minutes and budget.remaining < _UNLIMITED_SENTINEL
    block = budget.remaining < cfg.supervisor.ci_block_minutes and budget.remaining < _UNLIMITED_SENTINEL
    return {
        "total": budget.total,
        "used": budget.used,
        "remaining": budget.remaining,
        "pct_used": budget.pct_used,
        "warn": warn,
        "block": block,
        "cached": cached,
    }
