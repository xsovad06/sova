"""Agent validation: conflict checks, budget limits, PR merge status, state transitions.

Separated from agent_lifecycle to isolate validation and guard logic.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from sova.adapters.base import Task
from sova.config.loader import load_config
from sova.dashboard.services.agent_pool import ProjectAgents
from sova.db.session import get_session
from sova.supervisor.suitability import Suitability, SuitabilityMode, assess_suitability
from sova.utils.formatting import decimal_to_json
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.config.models import ProjectConfig
    from sova.supervisor.dependency_graph import DependencyGraph

log = get_logger(component="dashboard.control.validation")

# Codes of a start refused by current state: an agent already active on the issue, or too little free memory.
ISSUE_CONFLICT = "issue_conflict"
MEMORY_PRESSURE = "memory_pressure"


async def _check_issue_conflict(issue: str, pa: ProjectAgents, *, force: bool = False) -> dict | None:
    """Check if an agent is already running for this issue (in-memory + DB).

    Returns an error dict if a conflict exists, None if clear.
    When *force* is True, stale (dead-PID) DB runs are marked interrupted
    and live conflicts are skipped so the caller can proceed.
    Must be called inside ``pa._lock``.
    """
    for existing in pa.agents.values():
        if existing.issue == issue:
            if force:
                log.info("issue_conflict.force_skipped", issue=issue, run_id=existing.run_id)
                continue
            return {
                "error": f"Issue #{issue} already has an active agent (run {existing.run_id})",
                "code": ISSUE_CONFLICT,
                "existing_run_id": existing.run_id,
            }

    try:
        from sqlalchemy import select

        from sova.dashboard.services.agent_recovery import _is_process_alive
        from sova.dashboard.services.work_service import _TERMINAL
        from sova.db.models import TaskRun

        in_memory_ids = set(pa.agents.keys())
        async with await get_session(project_dir=pa.project_dir) as session:
            async with session.begin():
                stmt = select(TaskRun).where(
                    TaskRun.issue_number == issue,
                    TaskRun.status.notin_(_TERMINAL),
                    TaskRun.pid.isnot(None),
                )
                result = await session.execute(stmt)
                runs = result.scalars().all()

                for run in runs:
                    if run.id in in_memory_ids:
                        continue
                    if _is_process_alive(run.pid):
                        if force:
                            log.info("issue_conflict.force_skipped_external", issue=issue, run_id=run.id, pid=run.pid)
                            continue
                        msg = f"Issue #{issue} already has an active agent (external run {run.id}, PID {run.pid})"
                        return {"error": msg, "code": ISSUE_CONFLICT, "existing_run_id": run.id}
                    run.status = "interrupted"
                    run.error_message = "Stale run: process no longer alive"
                    run.ended_at = datetime.now(timezone.utc)
                    log.warning("issue_conflict.auto_recovered", run_id=run.id, issue=issue, pid=run.pid)
    except (OSError, RuntimeError, SQLAlchemyError):
        log.warning("issue_conflict_check.db_failed", issue=issue, exc_info=True)

    return None


async def _check_pr_merged_on_failure(pr_number: int | None, project_dir: Path | None) -> bool:
    """Check if a PR was merged on GitHub despite the agent process failing.

    Used by _wait_and_finalize to avoid marking integration runs as "failed"
    when the merge succeeded but post-merge cleanup crashed.
    """
    if pr_number is None:
        return False
    try:
        from sova.git.pr import get_pr_status

        cfg = load_config(project_dir)
        repo = cfg.github_repo
        if not repo:
            return False
        status = await get_pr_status(pr_number, repo=repo, github_user=cfg.github_user)
        return status.state == "MERGED"
    except (RuntimeError, OSError):
        log.debug("check_pr_merged.failed", pr_number=pr_number, exc_info=True)
        return False


async def _check_issue_budget(issue: str, project_dir: Path) -> dict | None:
    """Check if the issue has exceeded its cumulative budget across all runs.

    Returns an error dict if over budget, None if clear.

    Fail-open: if the check itself fails (DB error, config error, etc.),
    returns None (clear to proceed) so budget infrastructure issues don't
    block all agent operations. The failure is logged as a warning.
    """
    try:
        from sova.dashboard.services.lifecycle_service import get_lifecycle_for_issue

        cfg = load_config(project_dir)
        max_budget = cfg.agent.max_issue_budget

        async with await get_session(project_dir=project_dir) as session:
            lifecycle = await get_lifecycle_for_issue(session, issue)
            if lifecycle is None:
                return None

            current = lifecycle.total_cost_usd
            if current >= max_budget:
                return {
                    "code": "budget_exceeded",
                    "error": (
                        f"Issue #{issue} has exceeded the per-issue budget "
                        f"(${lifecycle.total_cost_usd:.2f} / ${max_budget:.2f}). "
                        f"Use --force to bypass."
                    ),
                    "total_cost_usd": decimal_to_json(current),
                    "max_issue_budget": decimal_to_json(max_budget),
                }
    except Exception:  # noqa: BLE001 (budget check spans config, DB and lifecycle lookups)
        log.warning("issue_budget_check.failed", issue=issue, exc_info=True)

    return None


# Roles that classify or review an issue, which is always allowed whatever the issue's suitability.
SUITABILITY_EXEMPT_ROLES = frozenset({"triage", "reviewer"})
# Only these two modes stop a manual launch: interactive and blocked issues are shown, never refused.
_SUITABILITY_BLOCKING_MODES = frozenset({SuitabilityMode.EPIC, SuitabilityMode.HUMAN})
# One deadline for the whole launch-time check (task fetch and classification together). It runs while
# start_agent() holds the project lock, so a stalled tracker must not hold it for long.
_SUITABILITY_CHECK_TIMEOUT_S = 10.0


class RolePinUnavailable(Exception):
    """The tracker state of an issue could not be read, so a role-less override cannot pick a safe role."""


async def classify_issue_suitability(
    issue: str,
    project_dir: Path,
    task: Task | None = None,
    cfg: ProjectConfig | None = None,
    graph: DependencyGraph | None = None,
) -> Suitability | None:
    """Classify *issue* for a manual launch, or return None when the classification could not be read.

    None means unknown, never allowed: the caller decides what an unknown issue does. The local `sova assess`
    record is read only when no tracker label already decides the outcome, as the autonomous
    human-involvement gate does. A caller that already fetched the issue passes it as *task* (and its loaded
    config as *cfg*) to save a second read. Dependencies are considered only when the caller passes a *graph*
    that contains the issue (the launch modal's display; `blocked` never stops a launch, so the launch-time
    check passes none). A graph that lacks the issue is ignored rather than failing closed to `blocked`. This
    function sets no deadline of its own: `classify_for_launch()` bounds the whole read.
    """
    try:
        return await _read_issue_suitability(issue, project_dir, task, cfg, graph)
    except Exception:  # noqa: BLE001 (a tracker or config or DB failure leaves the classification unknown)
        log.warning("issue_suitability_check.failed", issue=issue, exc_info=True)
        return None


def _graph_has_task(task: Task, graph: DependencyGraph | None) -> bool:
    """Whether *graph* holds *task*, so its dependencies can be read from it."""
    if graph is None:
        return False
    try:
        return graph.has_task(int(task.id))
    except (TypeError, ValueError):
        return False


async def _read_issue_suitability(
    issue: str, project_dir: Path, task: Task | None, cfg: ProjectConfig | None, graph: DependencyGraph | None
) -> Suitability:
    from sova.adapters import create_adapter
    from sova.supervisor.labels import local_assessment_applies

    if cfg is None:
        cfg = load_config(project_dir)
    if task is None:
        task = await create_adapter(cfg).get_task(issue)

    local_assessment: str | None = None
    if local_assessment_applies(task.labels):
        local_assessment = await _latest_local_suitability(task.id or issue, issue, project_dir, cfg.github_repo)
    with_dependencies = _graph_has_task(task, graph)
    return assess_suitability(
        task,
        graph if with_dependencies else None,
        local_assessment=local_assessment,
        consider_dependencies=with_dependencies,
    )


async def _latest_local_suitability(canonical: str, typed: str, project_dir: Path, project_slug: str) -> str | None:
    """The latest `sova assess` suitability for the issue, keyed by the tracker's own id first.

    A launch for `007` fetches issue `7`, and the record is stored under `7`; the id as typed is still
    tried when it differs, so a record written under that spelling is not lost.
    """
    from sova.knowledge.assessments import get_latest_assessment

    async with await get_session(project_dir=project_dir) as session:
        for key in dict.fromkeys((canonical, typed)):
            record = await get_latest_assessment(session, issue_number=key, project_slug=project_slug)
            if record is not None:
                return record.suitability
    return None


def canonical_role(role: str | None, cfg: ProjectConfig | None) -> str:
    """The role name the dispatcher would run for an explicit *role*: a configured nickname resolved."""
    from sova.roles.dispatcher import resolve_role_nickname

    return resolve_role_nickname(role or "developer", cfg.roles if cfg is not None else None)


def dispatched_role(role: str | None, task: Task, cfg: ProjectConfig | None) -> str | None:
    """The role a run launches as: *role* resolved, or for no role the one `dispatch()` picks from the state.

    `sova run` with no `--role` routes by tracker state (BACKLOG to triage, IN_REVIEW to reviewer), and a
    state no role handles (HUMAN_ONLY) has none: None, which is never exempt from the suitability check.
    """
    if role:
        return canonical_role(role, cfg)
    from sova.roles.dispatcher import role_for_state

    return role_for_state(task.state)


async def pin_override_role(issue: str, role: str | None, project_dir: Path) -> str | None:
    """The role to launch with when a suitability override has none: the default for a human-only issue.

    `dispatch()` has no role for a HUMAN_ONLY issue and raises, so an override without an explicit role would
    be reported as started and then fail. Any other state keeps routing by state (None is returned).
    Raises `RolePinUnavailable` when the state cannot be read: that is neither "not human-only" nor a role, and
    the caller must refuse the launch and ask for an explicit role instead of spawning one that may fail.
    """
    if role or not issue:
        return role
    try:
        from sova.adapters import create_adapter
        from sova.adapters.base import TaskState

        cfg = load_config(project_dir)
        async with asyncio.timeout(_SUITABILITY_CHECK_TIMEOUT_S):
            state = await create_adapter(cfg).get_state(issue)
    except Exception as exc:  # noqa: BLE001 (any tracker or config failure leaves the state unread)
        log.warning("issue_suitability_check.role_pin_failed", issue=issue, exc_info=True)
        raise RolePinUnavailable(
            f"Cannot read the state of issue #{issue} to choose a role for the override. "
            "Pick an explicit role and launch again."
        ) from exc
    return cfg.roles.default if state == TaskState.HUMAN_ONLY else None


async def classify_for_launch(
    issue: str,
    role: str | None,
    project_dir: Path,
    task: Task | None = None,
    cfg: ProjectConfig | None = None,
    graph: DependencyGraph | None = None,
    timeout_s: float | None = None,
) -> Suitability | None:
    """The suitability of *issue* for a launch as *role*, or None when it is exempt or could not be read.

    A triage or reviewer run is exempt, whether *role* names it, a nickname resolves to it, or (with no *role*)
    `dispatch()` would choose it from the tracker state; the tracker is not read for the classification then.
    The task fetch and the classification share one *timeout_s* deadline (`_SUITABILITY_CHECK_TIMEOUT_S` when
    not given); a timeout reads as unknown (None).
    """
    timeout_s = timeout_s if timeout_s is not None else _SUITABILITY_CHECK_TIMEOUT_S
    try:
        async with asyncio.timeout(timeout_s):
            return await _classify_for_launch(issue, role, project_dir, task, cfg, graph)
    except TimeoutError:
        log.warning("issue_suitability_check.timed_out", issue=issue, timeout_s=timeout_s)
        return None


async def _classify_for_launch(
    issue: str,
    role: str | None,
    project_dir: Path,
    task: Task | None,
    cfg: ProjectConfig | None,
    graph: DependencyGraph | None,
) -> Suitability | None:
    if role:
        if canonical_role(role, cfg) in SUITABILITY_EXEMPT_ROLES:
            return None
    else:
        if task is None:
            task = await _fetch_launch_task(issue, project_dir, cfg)
        if task is None or dispatched_role(role, task, cfg) in SUITABILITY_EXEMPT_ROLES:
            return None
    return await classify_issue_suitability(issue, project_dir, task=task, cfg=cfg, graph=graph)


async def check_issue_suitability(
    issue: str, role: str | None, project_dir: Path, *, task: Task | None = None
) -> dict | None:
    """Refuse a manual launch on an epic or human-only issue.

    Returns an error dict (``code="suitability_blocked"``, with ``mode`` and ``reasons``) when the issue is
    classified `epic` or `human`, None when the launch may proceed. Triage and reviewer runs are never
    blocked: named directly, through a configured nickname, or (with no *role*) chosen by `dispatch()` from
    the tracker state. An issue whose classification or state cannot be read is let through: a tracker outage
    must not make every manual launch impossible, and the failure has already been logged. The whole check
    (task fetch included) is bounded by one `_SUITABILITY_CHECK_TIMEOUT_S` deadline. A caller that already read
    the issue passes it as *task*, so the launch makes one tracker read, not two.
    """
    if not issue:
        return None
    cfg: ProjectConfig | None = None
    try:
        cfg = load_config(project_dir)
    except Exception:  # noqa: BLE001 (an unreadable config leaves nicknames unresolved and classify retries it)
        log.warning("issue_suitability_check.config_failed", issue=issue, exc_info=True)
    suitability = await classify_for_launch(issue, role, project_dir, task=task, cfg=cfg)
    if suitability is None or suitability.mode not in _SUITABILITY_BLOCKING_MODES:
        return None

    reasons = suitability.reasons
    described = ", ".join(f"{r.get('source')}: {r.get('value')}" for r in reasons) or "no reason recorded"
    log.info("issue_suitability_check.blocked", issue=issue, role=role, mode=suitability.mode.value, reasons=reasons)
    return {
        "code": "suitability_blocked",
        "error": f"Issue #{issue} is classified {suitability.mode.value} ({described}) and is not meant for an agent.",
        "mode": suitability.mode.value,
        "reasons": reasons,
    }


async def _fetch_launch_task(issue: str, project_dir: Path, cfg: ProjectConfig | None) -> Task | None:
    """The tracker issue, or None (logged) when it cannot be read; the caller's deadline bounds the read."""
    try:
        from sova.adapters import create_adapter

        return await create_adapter(cfg or load_config(project_dir)).get_task(issue)
    except Exception:  # noqa: BLE001 (a tracker or config failure leaves the launch role unknown)
        log.warning("issue_suitability_check.task_fetch_failed", issue=issue, exc_info=True)
        return None


async def fetch_launch_task(issue: str, project_dir: Path) -> Task | None:
    """The tracker issue a launch is about to act on, or None (logged) when it is unreadable or the read times out.

    Read once per launch under one `_SUITABILITY_CHECK_TIMEOUT_S` deadline and shared by the developer state
    check, the suitability check and the prior-state record, so a stalled tracker costs one deadline.
    """
    try:
        async with asyncio.timeout(_SUITABILITY_CHECK_TIMEOUT_S):
            return await _fetch_launch_task(issue, project_dir, None)
    except TimeoutError:
        log.warning("launch_task_fetch.timeout", issue=issue)
        return None


# Codes of a write refused because another login owns the PR or issue, or gh acts as another account.
OWNED_BY_OTHER = "owned_by_other"
GH_LOGIN_MISMATCH = "gh_login_mismatch"
# One bound for every ownership lookup a start does, so a slow GitHub costs one deadline, never a hung start.
_OWNERSHIP_CHECK_TIMEOUT_S = 15.0


async def _ownership_refusal(
    cfg: ProjectConfig,
    issue: str,
    *,
    pr_number: int | None,
    check_pr: bool,
    task: Task | None,
    compare_assignees: bool,
) -> dict | None:
    """The OWNED_BY_OTHER refusal for the issue or PR a start targets, else None (every lookup fails open, logged)."""
    from sova.core.ownership import check_ownership

    # Also for a PR run: check_ownership() falls back to the issue's assignees when the PR author is unknown,
    # as the board does, and reads them through the adapter only then.
    adapter = None
    if task is None and issue and compare_assignees:
        from sova.adapters import create_adapter

        try:
            adapter = create_adapter(cfg)
        except Exception:  # noqa: BLE001 (fail open: without an adapter the assignees are unknown, logged)
            log.warning("write_ownership.adapter_failed", issue=issue, exc_info=True)
    try:
        async with asyncio.timeout(_OWNERSHIP_CHECK_TIMEOUT_S):
            result = await check_ownership(
                issue or None,
                github_user=cfg.github_user,
                check_pr=bool(pr_number) or check_pr,
                respect_ownership=cfg.supervisor.respect_ownership,
                repo=cfg.github_repo,
                pr_number=pr_number,
                adapter=adapter,
                task_assignees=task.assignees if task is not None else None,
                compare_assignees=compare_assignees,
            )
    except TimeoutError:
        # Fail open: a slow GitHub leaves the owner unknown, the same as any other unreadable owner.
        log.warning("write_ownership.check_timed_out", issue=issue, pr=pr_number, exc_info=True)
        return None
    if result.refused:
        log.info("write_ownership.refused", issue=issue, pr=result.pr_number, owner=result.owner)
        return {"error": result.detail, "code": OWNED_BY_OTHER, "owner": result.owner}
    return None


async def check_write_ownership(
    issue: str,
    project_dir: Path,
    *,
    pr_number: int | None = None,
    check_pr: bool = False,
    task: Task | None = None,
) -> dict | None:
    """The refusal for a write whose PR or issue another login owns, or made through another gh login; else None.

    Runs before any process is spawned by ``start_agent()``, ``start_command()`` and ``_process_auto_handoff()``,
    with the same ``check_ownership()`` the supervisor gate uses. A run on *pr_number* (or with *check_pr*)
    checks the PR author; any other run checks the issue's assignees (*task*'s when the caller read it). The gh
    login check is independent of ``supervisor.respect_ownership``. Every lookup fails open and is logged.
    """
    try:
        cfg = load_config(project_dir)
    except Exception:  # noqa: BLE001 (fail open: an unloadable config leaves the owner unknown, logged)
        log.warning("write_ownership.config_failed", issue=issue, exc_info=True)
        return None

    from sova.core.ownership import assignees_are_logins
    from sova.utils.gh import gh_login_mismatch

    # With neither an issue nor a PR there is no owner to read: only the gh login check below applies.
    refusal = None
    if issue or pr_number:
        refusal = await _ownership_refusal(
            cfg,
            issue,
            pr_number=pr_number,
            check_pr=check_pr,
            task=task,
            compare_assignees=assignees_are_logins(cfg.task_source.type),
        )
    if refusal:
        return refusal

    mismatch = await gh_login_mismatch(cfg.github_user)
    if mismatch:
        log.warning("write_ownership.gh_login_mismatch", issue=issue, github_user=cfg.github_user)
        return {"error": mismatch, "code": GH_LOGIN_MISMATCH}
    return None


STATE_NOT_READY = "state_not_ready"


async def check_developer_state(
    issue: str,
    project_dir: Path,
    *,
    role: str | None = None,
    override_suitability: bool = False,
    task: Task | None = None,
) -> dict | None:
    """Refuse a developer start on an issue its Gate 3 would refuse, naming the agent the issue needs instead.

    A start with no *role* runs whatever `dispatch()` picks from the issue's state (triage for BACKLOG, the
    researcher for TRIAGED, the reviewer for IN_REVIEW), so only one that resolves to the developer, or to no
    role at all, is checked: the others are let through to run their own agent. A caller that already read the
    issue passes it as *task*; otherwise it is read here.

    Called before the spawn, so a start that cannot pass the gate is never launched: a launched one moves the
    issue to in progress and its failure rollback then has a state to undo that the run never earned. Returns
    an error dict (``code=STATE_NOT_READY``, with the issue's ``state`` and the ``next_role`` its state calls
    for, or None when no agent does) when the issue's state is not one ``DeveloperRole`` accepts, None when the
    start may proceed. The acceptance is the role's own (``state_accepted()``), so *override_suitability* lets a
    human-only issue through here exactly as it does at the gate. Fails open like the suitability check: an
    unreadable tracker leaves the gate to the role.
    """
    from sova.adapters.base import TaskState
    from sova.core.context import state_accepted
    from sova.roles.developer import DeveloperRole
    from sova.roles.dispatcher import role_for_state

    if task is None:
        task = await fetch_launch_task(issue, project_dir)
    if task is None:
        return None
    launches_as = dispatched_role(role, task, None)
    if launches_as not in (None, "developer") or state_accepted(
        task.state, DeveloperRole.allowed_input_states, override_suitability=override_suitability
    ):
        return None
    next_role = role_for_state(task.state)
    state = task.state.value.replace("_", " ")
    if task.state is TaskState.TRIAGED:
        hint = "It has no research or spec yet: run the researcher first."
    elif next_role:
        hint = f"Run the {next_role} first."
    else:
        hint = "No agent handles it in this state."
    log.info("developer_state_check.blocked", issue=issue, state=task.state.value, next_role=next_role)
    return {
        "code": STATE_NOT_READY,
        "error": f"Issue #{issue} is {state}, not ready for development. {hint}",
        "state": task.state.value,
        "next_role": next_role,
    }


def suitability_blocked_detail(result: dict) -> dict:
    """The 409 body for a ``suitability_blocked`` start: the clients key on ``code`` and render ``message``."""
    return {
        "code": "suitability_blocked",
        "message": result["error"],
        "mode": result.get("mode"),
        "reasons": result.get("reasons", []),
    }


def check_memory_pressure(project_dir: Path) -> tuple[dict | None, str | None]:
    """Check system memory availability before spawning an agent.

    Returns ``(block_error, warning_message)``:
    - ``(dict, None)`` when memory is below block threshold (spawn must be rejected)
    - ``(None, str)`` when memory is below warn threshold (spawn allowed, show warning)
    - ``(None, None)`` when memory is sufficient or guard is disabled

    Fail-open: if psutil raises, config cannot be loaded, or threshold
    values are invalid, returns ``(None, None)``.
    """
    try:
        cfg = load_config(project_dir)
        guard = cfg.memory_guard
        if not guard.enabled:
            return None, None

        import psutil

        mem = psutil.virtual_memory()
        available_gb = mem.available / (1024**3)

        if available_gb < guard.block_threshold_gb:
            return {
                "code": MEMORY_PRESSURE,
                "error": (
                    f"Insufficient memory: {available_gb:.1f} GB available, "
                    f"{guard.block_threshold_gb:.1f} GB required. "
                    f"Close applications or use --force to bypass."
                ),
                "available_gb": round(available_gb, 2),
                "block_threshold_gb": guard.block_threshold_gb,
            }, None

        if available_gb < guard.warn_threshold_gb:
            return None, (
                f"Low memory: {available_gb:.1f} GB available (warn threshold: {guard.warn_threshold_gb:.1f} GB)"
            )
    except Exception:  # noqa: BLE001 (memory guard is advisory; spawning proceeds if it cannot be evaluated)
        log.warning("memory_guard.check_failed", exc_info=True)

    return None, None


def check_network_connectivity(project_dir: Path) -> tuple[dict | None, str | None]:
    """Check network reachability before spawning an agent.

    Same contract as ``check_memory_pressure``: ``(block_error, warning)``,
    fail-open on any error. Unlike memory pressure there is no warn tier, since
    reachability is binary.

    This is the last-resort check at the spawn choke point, so it covers a
    manual dashboard click as well as a supervisor-executed spawn (the
    supervisor's own gate stops it deciding to spawn in the first place).
    """
    try:
        from sova.supervisor.network_health import get_connectivity_status

        # Tracker first, config second. The tracker read is in-memory while
        # load_config() is a blocking TOML + DB read, and if the network is up
        # there is nothing to block whatever the config says, so the common path
        # does no I/O at all rather than paying a config load on every spawn.
        status = get_connectivity_status()
        if not status.is_down:
            return None, None

        guard = load_config(project_dir).network_guard
        if guard.enabled and guard.block_spawns:
            return {
                "code": "network_unreachable",
                "error": (
                    "No connection to GitHub or the LLM provider "
                    f"(down for {round(status.down_for_seconds)}s). An agent spawned now would "
                    "fail within seconds. Use --force to bypass."
                ),
                "down_seconds": round(status.down_for_seconds),
            }, None
    except Exception:  # noqa: BLE001 (network guard is advisory; spawning proceeds if it cannot be evaluated)
        log.warning("network_guard.check_failed", exc_info=True)

    return None, None


def check_stale_command(project_dir: Path, command: str, run_dir: Path | None = None) -> dict | None:
    """Block a safety-critical command whose installed copy predates its canonical fix (#1251).

    The spawn-time counterpart of the supervisor's ``stale_commands`` gate,
    and the only enforcement point for commands the supervisor never spawns
    (``pr``, ``rearrange-commits``, ``review-full``), which reach a run only
    through a dashboard button or handoff action. Fail-open, like the other
    pre-spawn checks: an install or config that cannot be read must not block
    every command. ``run_dir`` is the worktree the command will run in; see
    ``find_stale_command()`` for why its branch's copy is judged separately.
    """
    try:
        from sova.commands.staleness import find_stale_command

        reason = find_stale_command(project_dir, command, run_dir=run_dir)
    except Exception:  # noqa: BLE001 (stale-command check is advisory when it cannot be evaluated; spawning proceeds)
        log.warning("stale_command.check_failed", command=command, exc_info=True)
        return None
    if reason is None:
        return None
    return {"code": "stale_command", "error": reason}


async def _transition_to_in_progress(issue: str, project_dir: Path) -> None:
    """Move the issue to IN_PROGRESS on the configured tracker.

    The state it had before is not read here: ``start_agent()`` records it on the run, from the read it already
    made, before the spawn (``PRIOR_STATE_KEY``), so a run that fails fast cannot reach its rollback first.
    """
    try:
        from sova.adapters import create_adapter
        from sova.adapters.base import TaskState

        adapter = create_adapter(load_config(project_dir))
        await adapter.transition_state(issue, TaskState.IN_PROGRESS)
        log.info("issue.transitioned", issue=issue, state="in_progress")
    except Exception:  # noqa: BLE001 (config load, adapter construction and tracker call each fail differently)
        log.warning("issue.transition_failed", issue=issue, exc_info=True)
