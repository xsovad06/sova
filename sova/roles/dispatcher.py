"""Role dispatcher: routes tasks to the appropriate agent role.

The dispatcher determines which role should handle a task based on its
current tracker state or an explicit role name. It enforces the mandatory
pipeline order: Triage -> Research -> Develop.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.exc import SQLAlchemyError

from sova.adapters.base import TaskState
from sova.config.models import RolesConfig
from sova.core.context import ExecutionContext
from sova.core.state import TASK_RUN_TERMINAL, TaskStatus
from sova.db.models import TaskRun
from sova.db.session import get_session
from sova.roles.base import AgentRole, RoleResult
from sova.roles.developer import DeveloperRole
from sova.roles.planner import PlannerRole
from sova.roles.researcher import ResearcherRole
from sova.roles.reviewer import ReviewerRole
from sova.roles.triage import TriageRole
from sova.utils.logging import get_logger

log = get_logger(component="dispatcher")

# Registry of all built-in roles
_ROLES: dict[str, type[AgentRole]] = {
    "triage": TriageRole,
    "researcher": ResearcherRole,
    "developer": DeveloperRole,
    "reviewer": ReviewerRole,
    "planner": PlannerRole,
}

BUILTIN_ROLE_NAMES: frozenset[str] = frozenset(_ROLES.keys())

# Maps tracker states to the role that should handle them
_STATE_TO_ROLE: dict[TaskState, str] = {
    TaskState.BACKLOG: "triage",
    TaskState.TRIAGED: "researcher",
    TaskState.RESEARCHED: "developer",
    TaskState.IN_PROGRESS: "developer",
    TaskState.IN_REVIEW: "reviewer",
}


def _resolve_nickname(name: str, config: RolesConfig | None) -> str:
    """Resolve a role nickname to its canonical name."""
    if config and name in config.nicknames:
        return config.nicknames[name]
    return name


def resolve_role_nickname(name: str, config: RolesConfig | None) -> str:
    """The canonical role name for *name*, a configured nickname resolved (the public form of the lookup)."""
    return _resolve_nickname(name, config)


def role_for_state(state: TaskState) -> str | None:
    """The name of the role that handles an issue in *state*, or None when no role does (e.g. HUMAN_ONLY, DONE)."""
    return _STATE_TO_ROLE.get(state)


def get_role(name: str, *, config: RolesConfig | None = None) -> AgentRole:
    """Get a role instance by name, resolving nicknames if configured.

    Raises ValueError if the role name is not found.
    """
    name = _resolve_nickname(name, config)

    role_cls = _ROLES.get(name)
    if role_cls is None:
        available = ", ".join(sorted(_ROLES.keys()))
        raise ValueError(f"Unknown role: {name!r}. Available: {available}")

    return role_cls()


def resolve_role_for_state(state: TaskState) -> AgentRole:
    """Determine which role should handle an issue in the given state.

    Raises ValueError if no role handles the given state (e.g., DONE).
    """
    role_name = _STATE_TO_ROLE.get(state)
    if role_name is None:
        raise ValueError(f"No role handles issues in {state!r} state")

    return get_role(role_name)


def list_roles() -> list[AgentRole]:
    """Return instances of all registered roles."""
    return [cls() for cls in _ROLES.values()]


async def get_role_async(name: str, *, config: RolesConfig | None = None) -> AgentRole:
    """Get a role by name, falling back to DB lookup for custom roles.

    Raises ValueError if the role name is not found in built-ins or DB.
    """
    name = _resolve_nickname(name, config)

    role_cls = _ROLES.get(name)
    if role_cls is not None:
        return role_cls()

    # Fall back to DB lookup for custom roles
    from sqlalchemy import select

    from sova.db.models import WorkflowDefinition
    from sova.db.session import get_session
    from sova.roles.custom import CustomRole

    try:
        async with await get_session() as session:
            async with session.begin():
                stmt = select(WorkflowDefinition).where(WorkflowDefinition.name == name)
                result = await session.execute(stmt)
                definition = result.scalar_one_or_none()
                if definition is not None:
                    return CustomRole(definition)
    except Exception:  # noqa: BLE001 (logged then re-raised; the caller decides how to handle it)
        log.warning("dispatcher.custom_lookup_failed", name=name, exc_info=True)
        raise

    available = ", ".join(sorted(_ROLES.keys()))
    raise ValueError(f"Unknown role: {name!r}. Available built-in: {available}")


async def dispatch(
    ctx: ExecutionContext,
    *,
    role_name: str | None = None,
    config: RolesConfig | None = None,
) -> tuple[AgentRole, RoleResult]:
    """Dispatch a task to the appropriate role and execute it.

    If role_name is provided, uses that role explicitly.
    Otherwise, auto-selects the role based on the task's tracker state.

    Returns the role used and its execution result.
    """
    if role_name:
        role = await get_role_async(role_name, config=config)
    elif ctx.has_issue:
        # Auto-select based on tracker state
        state = await ctx.adapter.get_state(ctx.issue_number)
        role = resolve_role_for_state(state)
    else:
        raise ValueError("Issue-less runs require an explicit --role")

    log.info("dispatch", issue=ctx.issue_number or "", label=ctx.display_label, role=role.name)
    ctx.role = role.name
    created = None
    cost_before = ctx.cost_usd
    if ctx.task_run_id is None and not role.creates_own_task_run:
        created = await _create_shielded(ctx)
    try:
        result = await role.execute(ctx)
    except Exception as exc:
        # A cancellation (SIGTERM) is not caught: the row stays running for the liveness sweep to classify,
        # as for a pipeline run, rather than being recorded as a failure.
        if created is not None:
            await _finalize_shielded(ctx, created, TaskStatus.FAILED, cost_before, error=str(exc) or type(exc).__name__)
        raise
    if created is not None:
        if result.success:
            status = TaskStatus.DONE
        elif result.awaiting_approval:
            status = TaskStatus.AWAITING_APPROVAL
        else:
            status = TaskStatus.FAILED
        await _finalize_shielded(ctx, created, status, cost_before, error=None if result.success else result.error)
    return role, result


async def _create_shielded(ctx: ExecutionContext) -> int | None:
    """Run ``_create_task_run`` to completion even when SIGTERM cancels the awaiting task.

    A cancellation landing mid-write would otherwise abandon the insert, or commit it without
    ``ctx.task_run_id`` set, orphaning a running row the shutdown handler cannot finalize. The write is
    shielded and, on cancellation, awaited (so ``ctx.task_run_id`` is assigned) before re-raising.
    """
    write = asyncio.ensure_future(_create_task_run(ctx))
    try:
        return await asyncio.shield(write)
    except asyncio.CancelledError:
        await write
        raise


async def _create_task_run(ctx: ExecutionContext) -> int | None:
    """Record a TaskRun for a role that runs outside WorkflowEngine, and point *ctx* at it.

    A terminal ``sova run`` (or ``sova watch``) of the reviewer, triage or planner role used to leave no
    row at all, so its model, tokens and cost were only in the log (issue #1403). A failed write is
    logged and the role runs untracked, as before.
    """
    try:
        async with await get_session(ctx.project_dir) as session, session.begin():
            task_run = TaskRun(
                issue_number=ctx.issue_number.lstrip("#") or None,
                run_label=ctx.run_label,
                role=ctx.role,
                status=TaskStatus.RUNNING.value,
                pr_number=ctx.pr_number,
                resumed_from_id=ctx.resume_run_id,
                pid=os.getpid(),
            )
            session.add(task_run)
            await session.flush()
            run_id = task_run.id
    except (OSError, RuntimeError, SQLAlchemyError):
        log.warning("dispatch.task_run_create_failed", role=ctx.role, exc_info=True)
        return None
    ctx.task_run_id = run_id
    log.info("dispatch.task_run_created", run_id=run_id, role=ctx.role, label=ctx.display_label)
    return run_id


async def _finalize_shielded(
    ctx: ExecutionContext, run_id: int, status: TaskStatus, cost_before: Decimal, *, error: str | None
) -> None:
    """Run ``_finalize_task_run`` to completion even when SIGTERM cancels the awaiting task.

    A cancellation landing mid-write would otherwise skip the status and ``ended_at`` update and leave a
    finished review for the liveness sweep to classify as interrupted. The write is shielded and, on
    cancellation, awaited before the cancellation is re-raised.
    """
    write = asyncio.ensure_future(_finalize_task_run(ctx, run_id, status, cost_before, error=error))
    try:
        await asyncio.shield(write)
    except asyncio.CancelledError:
        await write
        raise


async def _finalize_task_run(
    ctx: ExecutionContext, run_id: int, status: TaskStatus, cost_before: Decimal, *, error: str | None
) -> None:
    """Close the TaskRun ``_create_task_run`` opened, with the spend the role added to *ctx*.

    Recorded here rather than left to the CLI: ``sova watch`` and ``sova parallel`` never call
    ``_persist_run_cost()``, and the per-issue budget gate sums ``total_cost_usd``. Only raises the
    recorded value, so the CLI writing the same figure afterwards is a no-op.
    """
    try:
        async with await get_session(ctx.project_dir) as session, session.begin():
            task_run = await session.get(TaskRun, run_id)
            if task_run is None:
                return
            # Another path (the dashboard finalizer, the liveness sweep) may have closed the row already:
            # keep its terminal status and end time, and still record the cost below.
            if task_run.status not in TASK_RUN_TERMINAL:
                task_run.status = status.value
                task_run.current_step = None
                task_run.ended_at = datetime.now(timezone.utc)
            run_cost = ctx.cost_usd - cost_before
            if run_cost > (task_run.total_cost_usd or Decimal("0")):
                task_run.total_cost_usd = run_cost
            if ctx.pr_number is not None:
                task_run.pr_number = ctx.pr_number
            if error:
                task_run.error_message = error
    except (OSError, RuntimeError, SQLAlchemyError):
        log.warning("dispatch.task_run_finalize_failed", run_id=run_id, status=status.value, exc_info=True)
