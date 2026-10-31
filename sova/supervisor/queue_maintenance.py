"""Deterministic queue maintenance for the supervisor daemon.

Runs once per poll cycle (after quota sync, before progression) to:
1. Remove done/deleted issues from the queue
2. Discover ready issues (RESEARCHED state) and append them
3. Persist the updated queue to SOVA project settings (stored in the project database)

The LLM planner may further prune or reorder after this step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from sova.adapters.base import Task, TaskState
from sova.adapters.priority import Priority
from sova.knowledge.assessments import get_latest_assessments
from sova.supervisor.dependency_graph import DependencyGraph, build_dependency_graph
from sova.supervisor.gates.connectivity import check_connectivity_gate
from sova.supervisor.gates.rate_limit import check_github_rate_limit_gate
from sova.supervisor.labels import local_assessment_applies
from sova.supervisor.suitability import AGENT_ELIGIBLE_MODES, assess_suitability
from sova.utils.log_dedup import log_once
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from sova.adapters.base import TaskAdapter
    from sova.config.models import NetworkGuardConfig, SupervisorConfig

log = get_logger(component="supervisor.queue_maintenance")

_DONE_STATES = frozenset({TaskState.DONE, TaskState.HUMAN_ONLY})
_READY_STATES = frozenset({TaskState.RESEARCHED})
_AGENT_IN_FLIGHT_STATES = frozenset({TaskState.IN_PROGRESS, TaskState.IN_REVIEW, TaskState.ON_QA})


@dataclass(frozen=True, slots=True)
class QueueMaintenanceResult:
    """Summary of a single maintenance pass."""

    previous: tuple[int, ...]
    current: tuple[int, ...]
    removed: tuple[int, ...] = field(default_factory=tuple)
    added: tuple[int, ...] = field(default_factory=tuple)
    changed: bool = False


async def maintain_queue(
    adapter: TaskAdapter,
    config: SupervisorConfig,
    project_dir: Path,
    network_guard: NetworkGuardConfig | None = None,
    *,
    session_factory: async_sessionmaker | None = None,
    project_slug: str = "",
) -> QueueMaintenanceResult:
    """Run deterministic queue maintenance: prune done, discover ready, persist.

    Skips the entire pass and returns unchanged if GitHub API calls fail
    (rate limiting, network errors). Never writes a partial queue.

    Includes an empty-list guard: if the adapter returns [] but the queue
    is non-empty, the queue is returned unchanged (prevents data loss when
    the API succeeds but returns an empty result due to query filter issues
    or transient backend problems). Rate limit and network errors are caught
    separately via the AdapterError exception path above.

    Runs the same rate-limit and connectivity gates `ProgressionEngine.evaluate_all()`
    checks before its own `build_dependency_graph()` call, in the same order
    (connectivity first: with the network down every API call below fails, so
    checking it first costs one in-memory read rather than a wasted round
    trip once rate-limit state is also stale). Both run before any adapter
    call. `network_guard` is optional (omitted by most tests, logged at
    debug when absent) since an unset guard never blocks.

    `session_factory`/`project_slug`, when given, resolve each RESEARCHED
    candidate's local `task_assessments` fallback (see
    `check_human_involvement_gate()`), the same way that gate receives them,
    rather than deriving a factory from `project_dir` here: a project_dir
    passed in tests rarely points at a real initialized database. Only
    resolved for ids where `local_assessment_applies()` says a label hasn't
    already decided the verdict (the same predicate the gate uses), and in a
    single batched query (`get_latest_assessments()`) rather than one per id.

    Re-checks suitability for already-queued entries too, not just new
    candidates (see `_prune_unsuitable`): an entry that drifts unsuitable
    after being queued (a label added, a local record written) must not sit
    there forever, re-blocked by the gate on every poll cycle while occupying
    a slot `_discover_ready` can no longer fill. A dependency-blocked entry is
    deliberately exempt: that is `check_dependency_gate`'s job to withhold and
    report, not this re-check's job to evict.
    """
    previous = tuple(config.task_queue)

    if network_guard is not None:
        connectivity_block = check_connectivity_gate(network_guard)
        if connectivity_block is not None:
            log.info("queue_maintenance.skipped_network_unreachable", detail=connectivity_block.detail)
            return QueueMaintenanceResult(previous=previous, current=previous)
    else:
        log.debug("queue_maintenance.network_guard_not_configured")

    rate_limit_block = check_github_rate_limit_gate(adapter.github_user)
    if rate_limit_block is not None:
        log.info("queue_maintenance.skipped_rate_limited", detail=rate_limit_block.detail)
        return QueueMaintenanceResult(previous=previous, current=previous)

    try:
        tasks = await adapter.list_tasks()
    except Exception:  # noqa: BLE001 (adapter raises AdapterError/ValueError too; stay fail-open)
        log.warning("queue_maintenance.list_tasks_failed", exc_info=True)
        return QueueMaintenanceResult(previous=previous, current=previous)

    if not tasks and previous:
        log.warning(
            "queue_maintenance.empty_list_guard",
            message="Adapter returned empty list but queue is non-empty; skipping maintenance to prevent data loss",
            queue_size=len(previous),
        )
        return QueueMaintenanceResult(previous=previous, current=previous)

    task_map: dict[int, TaskState] = {}
    task_objects: dict[int, Task] = {}
    for task in tasks:
        try:
            issue_id = int(task.id)
            task_map[issue_id] = task.state
            task_objects[issue_id] = task
        except (ValueError, TypeError):
            continue

    pruned = _prune_done(list(previous), task_map)
    pruned_set = set(pruned)
    removed_done = tuple(i for i in previous if i not in pruned_set)

    candidate_ids = _ready_candidate_ids(pruned, task_map)
    remaining = _remaining_capacity(config.max_queue_size, len(pruned))

    # Already-queued issues `_prune_unsuitable` will re-check (every state
    # except `_AGENT_IN_FLIGHT_STATES`, mirroring its own scope): these must be
    # re-checked too, not just newly discovered candidates, so capacity freed
    # by pruning an entry that drifted unsuitable is available this same cycle
    # rather than the next one. Not scoped to RESEARCHED: an epic or
    # interactive-recommended entry typically sits in BACKLOG/TRIAGED, where
    # it would otherwise never be re-checked.
    prune_recheck_ids = [i for i in pruned if task_map.get(i) not in _AGENT_IN_FLIGHT_STATES]

    # The dependency graph has to come from `build_dependency_graph()`, not from
    # `tasks` above: `adapter.list_tasks()` defaults to open issues only, so a
    # dependency that is closed *because it is done* would be absent and
    # `assess_suitability` would fail closed on it, permanently withholding every
    # issue that has ever listed a dependency. `build_dependency_graph()` fetches
    # `state="all"` and resolves out-of-page references individually, and is cached
    # per repo for 180s, so the supervisor's own later build in this same poll
    # cycle reuses this one rather than paying for a second fetch.
    #
    # Built lazily: skipped outright when dependency gating is disabled
    # (`supervisor.respect_dependencies=False`; this is the exclusive-filter
    # counterpart to that switch gating `check_dependency_gate`, so it must
    # honour the same config or the switch stops doing what it says), or when
    # there is no discovery candidate to classify against it. `_prune_unsuitable`
    # never considers dependencies (see its own docstring), so a re-check-only
    # pass never needs the graph.
    graph: DependencyGraph | None = None
    if config.respect_dependencies and remaining != 0 and candidate_ids:
        try:
            graph = await build_dependency_graph(adapter)
        except Exception:  # noqa: BLE001 (fail-open: graph build spans adapter and GitHub API calls)
            log.warning("queue_maintenance.graph_build_failed", exc_info=True)
            graph = None

    # Local assessments are resolved for the union of discovery candidates
    # (only when there is capacity to add them, including capacity a prune
    # could free this same cycle) and already-queued entries `_prune_unsuitable`
    # will re-check (always, since pruning is not capacity-gated), and only for
    # ids whose labels don't already decide the verdict (`local_assessment_applies`):
    # a candidate carrying a pipeline-state label like `agent:researched` can
    # never have its local record applied, so querying for it is wasted work.
    discovery_ids = candidate_ids if (remaining != 0 or prune_recheck_ids) else []
    assessment_ids = sorted(
        {
            issue_id
            for issue_id in {*discovery_ids, *prune_recheck_ids}
            if issue_id in task_objects and local_assessment_applies(task_objects[issue_id].labels)
        }
    )
    local_assessments = await _resolve_local_assessments(session_factory, project_slug, assessment_ids)

    suitable_queue = _prune_unsuitable(pruned, task_map, task_objects, graph, local_assessments)
    suitable_set = set(suitable_queue)
    pruned_unsuitable = tuple(i for i in pruned if i not in suitable_set)
    removed = removed_done + pruned_unsuitable

    added_issues = _discover_ready(
        suitable_queue, task_map, config.max_queue_size, task_objects, graph, local_assessments
    )

    current = suitable_queue + list(added_issues)
    changed = tuple(current) != previous

    if changed:
        from sova.config.db_loader import save_task_queue

        try:
            await save_task_queue(project_dir, current)
            config.task_queue = current
            log.info(
                "queue_maintenance.updated",
                removed=list(removed),
                added=list(added_issues),
                queue_size=len(current),
            )
        except Exception:  # noqa: BLE001 (queue is reverted in memory on any persistence failure)
            log.warning("queue_maintenance.persist_failed_reverting", exc_info=True)
            current = list(previous)
            changed = False
    else:
        log.debug("queue_maintenance.no_changes", queue_size=len(current))

    return QueueMaintenanceResult(
        previous=previous,
        current=tuple(current),
        removed=removed,
        added=added_issues,
        changed=changed,
    )


def _prune_done(queue: list[int], task_map: dict[int, TaskState]) -> list[int]:
    """Remove issues that are done or no longer exist on the tracker."""
    result: list[int] = []
    for issue_id in queue:
        state = task_map.get(issue_id)
        if state is None:
            log.info("queue_maintenance.pruned_missing", issue=issue_id)
            continue
        if state in _DONE_STATES:
            log.info("queue_maintenance.pruned_done", issue=issue_id, state=state.value)
            continue
        result.append(issue_id)
    return result


def _remaining_capacity(max_queue_size: int, current_queue_len: int) -> int | None:
    """Slots left in the queue, or None when `max_queue_size` is unlimited (<=0)."""
    if max_queue_size > 0:
        return max(0, max_queue_size - current_queue_len)
    return None


def _ready_candidate_ids(queue: list[int], task_map: dict[int, TaskState]) -> list[int]:
    """RESEARCHED issue ids outside `queue`.

    Used (alongside already-queued RESEARCHED entries) to decide whether
    building the dependency graph, and resolving local suitability records,
    are worth their cost at all.
    """
    existing = set(queue)
    return [issue_id for issue_id, state in task_map.items() if state in _READY_STATES and issue_id not in existing]


async def _resolve_local_assessments(
    session_factory: async_sessionmaker | None,
    project_slug: str,
    candidate_ids: list[int],
) -> dict[int, str]:
    """Fetch the latest local `task_assessments` record for each id in `candidate_ids`, if any.

    Without this, the queue auto-fill disagreed with `check_human_involvement_gate()`:
    an issue with a local `human_only`/`interactive_recommended` record (set via
    `sova assess set`, used for tickets that never receive a tracker label) would
    be classified `unclassified` here, get queued, and then be blocked forever by
    the gate, permanently occupying a queue slot. `maintain_queue()` only ever
    passes ids where `local_assessment_applies()` is True, so this never fetches
    a record no caller can use.

    One batched query (`get_latest_assessments()`) rather than one per id: this
    runs on every supervisor poll cycle (every ~2 minutes), and a backlog of
    tens of RESEARCHED issues previously meant tens of round trips to fetch at
    most a handful of rows.

    No `session_factory` (passed by the caller, mirroring the gate's own
    parameter rather than deriving one from `project_dir` here) means no local
    fallback data is available, same as the gate's own "no factory" case.
    Fails open on a store read error too, matching the gate's own
    local-record fallback: an unreadable local store must not withhold an
    issue that was never gated on it.
    """
    if not candidate_ids or session_factory is None:
        return {}
    try:
        async with session_factory() as session:
            records = await get_latest_assessments(
                session, issue_numbers=[str(issue_id) for issue_id in candidate_ids], project_slug=project_slug
            )
        return {int(issue_number): record.suitability for issue_number, record in records.items()}
    except Exception:  # noqa: BLE001 (fail open on local-store read failure, matching the gate)
        log.warning("queue_maintenance.local_assessment_lookup_failed", exc_info=True)
        return {}


def _prune_unsuitable(
    queue: list[int],
    task_map: dict[int, TaskState],
    task_objects: dict[int, Task],
    graph: DependencyGraph | None,
    local_assessments: dict[int, str],
) -> list[int]:
    """Remove already-queued issues `assess_suitability` would no longer admit.

    Scoped to skip issues in `_AGENT_IN_FLIGHT_STATES`: an issue an agent is
    already developing must never be pulled out of the queue just because its
    suitability drifted mid-flight. Every other state (including BACKLOG and
    TRIAGED, where an epic or interactive-recommended entry typically sits,
    since epics rarely reach RESEARCHED) is re-checked, or an issue that gains
    `type: epic`/`agent:interactive-recommended`/`agent:on-hold` or a local human_only/interactive
    record after being queued would sit there forever, re-blocked by the
    human-involvement gate on every poll cycle while still occupying a
    `max_queue_size` slot `_discover_ready` can no longer fill.

    `task_objects`/`graph`/`local_assessments` missing data for a given id
    fails open exactly like `_discover_ready`: no task object means no
    suitability data at all, so the id is kept.
    """
    result: list[int] = []
    for issue_id in queue:
        if task_map.get(issue_id) in _AGENT_IN_FLIGHT_STATES:
            result.append(issue_id)
            continue
        task = task_objects.get(issue_id)
        if task is None:
            result.append(issue_id)
            continue
        local_assessment = local_assessments.get(issue_id)
        if local_assessment is not None and not local_assessment_applies(task.labels):
            local_assessment = None
        # Dependency blocking of an already-queued entry is check_dependency_gate's job;
        # pruning it here would discard a deliberately queued, dependency-waiting issue.
        suitability = assess_suitability(task, local_assessment=local_assessment, consider_dependencies=False)
        if suitability.mode not in AGENT_ELIGIBLE_MODES:
            log_once(
                log,
                "queue_maintenance.pruned_unsuitable",
                key=f"{issue_id}:{suitability.mode.value}",
                level="info",
                issue=issue_id,
                mode=suitability.mode.value,
                reasons=suitability.reasons,
                blocked_by=suitability.blocked_by,
            )
            continue
        result.append(issue_id)
    return result


def _discover_ready(
    current_queue: list[int],
    task_map: dict[int, TaskState],
    max_queue_size: int,
    task_objects: dict[int, Task] | None = None,
    graph: DependencyGraph | None = None,
    local_assessments: dict[int, str] | None = None,
) -> tuple[int, ...]:
    """Find issues in RESEARCHED state not already in the queue.

    Sorts by priority (blocker > critical > high > medium > low > undefined),
    then by issue number (lowest first) as a tie-breaker.

    Issues classified `epic`, `human`, `interactive`, or `blocked` by
    `assess_suitability` are excluded; only `autonomous`/`unclassified` issues
    qualify. Every exclusion is logged (deduped per issue/mode) with its
    mode/reasons so a dropped issue is never silently missing from the queue.
    Without `task_objects`, no suitability data is available at all, so an
    issue is included on state alone (unchanged from prior behavior). Without
    `graph` (its build failed, or was skipped because
    `supervisor.respect_dependencies` is False), or for an issue the graph
    does not contain, labels are still honoured but dependencies are not: a
    missing graph must not fail closed and silently stop the queue filling.
    `local_assessments`, when given, maps issue id to its latest local
    `task_assessments` record, consulted only when no suitability label is
    present (mirrors `check_human_involvement_gate()`'s own fallback) and
    discarded per issue when `local_assessment_applies(task.labels)` is False
    (a pipeline-state label like `agent:researched` means a human has moved
    the issue past the point where a stale local record should still apply,
    exactly as it does for the gate): this check runs here too, not only in
    the caller, so a precomputed map containing an inapplicable record for a
    given issue is still handled correctly.

    An unresolvable dependency reference (the graph contains the issue but
    not its blocker) fails closed here exactly as it does in
    `DependencyGraph.get_ready_tasks()`, so the two consumers agree on every
    issue rather than the queue silently re-admitting one the graph API still
    reports as `blocked`.
    """
    existing = set(current_queue)
    capacity = _remaining_capacity(max_queue_size, len(current_queue))

    if capacity == 0:
        return ()

    local_assessments = local_assessments or {}
    candidates: list[int] = []
    for issue_id, state in task_map.items():
        if state not in _READY_STATES or issue_id in existing:
            continue
        task = task_objects.get(issue_id) if task_objects else None
        if task is not None:
            local_assessment = local_assessments.get(issue_id)
            if local_assessment is not None and not local_assessment_applies(task.labels):
                local_assessment = None
            has_graph_data = graph is not None and graph.has_task(issue_id)
            suitability = assess_suitability(
                task,
                graph,
                local_assessment=local_assessment,
                consider_dependencies=has_graph_data,
            )
            if suitability.mode not in AGENT_ELIGIBLE_MODES:
                log_once(
                    log,
                    "queue_maintenance.skipped_unsuitable",
                    key=f"{issue_id}:{suitability.mode.value}",
                    level="info",
                    issue=issue_id,
                    mode=suitability.mode.value,
                    reasons=suitability.reasons,
                    blocked_by=suitability.blocked_by,
                )
                continue
        candidates.append(issue_id)

    def _sort_key(issue_id: int) -> tuple[int, int]:
        if task_objects and issue_id in task_objects:
            rank = task_objects[issue_id].priority.rank
        else:
            rank = Priority.UNDEFINED.rank
        return (rank, issue_id)

    candidates.sort(key=_sort_key)

    if capacity is not None:
        candidates = candidates[:capacity]

    return tuple(candidates)


async def apply_planner_queue_changes(
    config: SupervisorConfig,
    project_dir: Path,
    *,
    removals: list[int] | None = None,
    reorder: list[int] | None = None,
) -> list[int]:
    """Apply LLM planner queue suggestions (prune/reorder only, never add).

    Returns the updated queue. Persists to DB if changed.
    """
    current = list(config.task_queue)
    changed = False

    if removals:
        before_len = len(current)
        remove_set = set(removals)
        current = [i for i in current if i not in remove_set]
        if len(current) != before_len:
            changed = True
            removed = remove_set & set(config.task_queue)
            log.info("queue_maintenance.planner_removed", removed=sorted(removed))

    if reorder:
        remove_set = set(removals) if removals else set()
        normalized = [i for i in reorder if i not in remove_set]
        if len(normalized) != len(set(normalized)):
            log.warning("queue_maintenance.planner_reorder_has_duplicates", reorder=reorder)
            normalized = list(dict.fromkeys(normalized))
        reorder_set = set(normalized)
        current_set = set(current)
        if reorder_set == current_set:
            current = normalized
            changed = True
            log.info("queue_maintenance.planner_reordered", new_order=current)
        elif reorder_set <= current_set:
            reordered_subset = [i for i in normalized if i in current_set]
            remainder = [i for i in current if i not in reorder_set]
            current = reordered_subset + remainder
            changed = True
            log.info("queue_maintenance.planner_partial_reorder", new_order=current)

    if changed:
        from sova.config.db_loader import save_task_queue

        try:
            await save_task_queue(project_dir, current)
            config.task_queue = current
        except Exception:  # noqa: BLE001 (queue is reverted in memory on any persistence failure)
            log.warning("queue_maintenance.planner_persist_failed", exc_info=True)
            return list(config.task_queue)

    return current
