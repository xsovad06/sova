"""Unified suitability classifier: tracker labels + local assessment + dependency graph.

Single source of truth for whether an issue is fit for autonomous agent work.
`DependencyGraph.get_ready_tasks()`, `queue_maintenance._discover_ready()`, and
`check_human_involvement_gate()` all route through `assess_suitability()` so the
three call sites agree on one classification instead of each re-deriving a
slightly different subset of the exclusion rules.

`rank_next_tasks()` orders the autonomous issues among them for the dashboard's
"Up next" list, and `rank_and_count_next_tasks()` also counts the ones still
waiting for a classification; both draw on the same candidate pool.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Collection, Iterable, Iterator
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING

from sova.adapters.base import Task, TaskState
from sova.adapters.milestone import UNPHASED, extract_phase_order
from sova.knowledge.assessments import SUITABILITY_SHORTHANDS
from sova.supervisor.labels import is_epic, is_human_only, is_interactive_recommended, is_on_hold, is_ready

if TYPE_CHECKING:
    from sova.supervisor.dependency_graph import DependencyGraph

# The three stored `task_assessments.suitability` values, pinned to their sole
# writer (`sova/knowledge/assessments.py`, written by `sova assess set`/
# `/find-task`) rather than duplicated as bare string literals: a rename on
# that side would otherwise leave this module silently matching nothing, and
# because every unmatched value falls through to `UNCLASSIFIED`, the failure
# mode is fail-open (a local `human_only` record would stop blocking).
_LOCAL_READY = SUITABILITY_SHORTHANDS["ready"]
_LOCAL_INTERACTIVE = SUITABILITY_SHORTHANDS["interactive"]
_LOCAL_HUMAN_ONLY = SUITABILITY_SHORTHANDS["human-only"]


class SuitabilityMode(StrEnum):
    """How an issue may (or may not) be picked up for autonomous agent work."""

    EPIC = "epic"
    HUMAN = "human"
    BLOCKED = "blocked"
    INTERACTIVE = "interactive"
    AUTONOMOUS = "autonomous"
    UNCLASSIFIED = "unclassified"


# The two modes an issue may be scheduled for autonomous work in: either it is
# explicitly cleared (`autonomous`) or nothing says otherwise (`unclassified`).
# Every other mode (`epic`/`human`/`interactive`/`blocked`) withholds it.
AGENT_ELIGIBLE_MODES: frozenset[SuitabilityMode] = frozenset({SuitabilityMode.AUTONOMOUS, SuitabilityMode.UNCLASSIFIED})


@dataclass(frozen=True, slots=True)
class Suitability:
    """Result of classifying an issue's fitness for autonomous agent work."""

    mode: SuitabilityMode
    reasons: list[dict] = field(default_factory=list)
    blocked_by: list[int] = field(default_factory=list)


def unsatisfied_dependencies(issue: int, graph: DependencyGraph | None) -> list[int] | None:
    """Return open non-epic dependency ids for `issue`, or None if dependency data is unavailable.

    Epic dependencies are skipped: epics are tracking containers, not real
    blockers. A dependency with no known task (missing reference) is treated
    as blocking, matching the existing fail-closed `DependencyGraph` semantics.
    The single implementation of this walk, shared by `assess_suitability()`
    and `check_dependency_gate()` (`sova/supervisor/gates/dependency.py`), so
    the epic-skip and missing-reference rules cannot drift between the two.
    """
    if graph is None or not graph.has_task(issue):
        return None
    blocked_by: list[int] = []
    for dep in sorted(graph.get_dependencies(issue)):
        dep_task = graph.get_task(dep)
        if dep_task is None:
            blocked_by.append(dep)
            continue
        if is_epic(dep_task.labels):
            continue
        if dep_task.state != TaskState.DONE:
            blocked_by.append(dep)
    return blocked_by


def _unsatisfied_dependencies(task: Task, graph: DependencyGraph | None) -> list[int] | None:
    """Resolve `task`'s numeric id and delegate to `unsatisfied_dependencies()`."""
    try:
        tid = int(task.id)
    # A non-numeric tracker id (Jira) can never be a `DependencyGraph` node,
    # so dependency data is unavailable rather than empty.
    except (TypeError, ValueError):
        return None
    return unsatisfied_dependencies(tid, graph)


def assess_suitability(
    task: Task,
    graph: DependencyGraph | None = None,
    local_assessment: str | None = None,
    *,
    consider_dependencies: bool = True,
) -> Suitability:
    """Classify a task's fitness for autonomous agent work.

    Precedence: epic > human > blocked > interactive > autonomous > unclassified.

    `blocked` has two independent causes: an `agent:on-hold` label (a lead's
    hold or the user's own unanswered question, which outranks `agent:ready`
    and `agent:interactive-recommended` and is read even with
    `consider_dependencies=False`) and an open dependency. Only the dependency
    cause is live: it is derived from the graph, so it clears when the blocker
    closes, whereas the label stays until a human removes it.

    A tracker label always wins over a conflicting `local_assessment` for
    `agent:ready`/`agent:human-only`/`type: epic`: `agent:ready` clears a
    conflicting local `human_only`/`interactive_recommended` record, and a
    local `ready` record never overrides one of those three blocking labels.
    The one exception is `agent:interactive-recommended`: since the
    human-only check (label or local record) runs before the
    interactive-recommended label is read, a local `human_only` record still
    outranks that label and reports `mode="human"` with the local record as
    the source, even though the label itself would also block. Both modes
    block, so eligibility is unaffected either way; only the reported source
    differs.

    `consider_dependencies=False` (the human-involvement gate's case: its
    dependency counterpart is `check_dependency_gate`, and it has no graph of
    its own) skips the dependency branch entirely rather than needing a
    graph at all, so `graph` is irrelevant and never allocated or walked.
    With `consider_dependencies=True` (the default), `graph=None`, or a graph
    that doesn't contain `task`, means dependency data was supposed to
    resolve and didn't, and fails closed to `blocked` rather than
    `autonomous`.
    """
    labels = task.labels

    if is_epic(labels):
        return Suitability(mode=SuitabilityMode.EPIC, reasons=[{"source": "label", "value": "type: epic"}])

    ready_label = is_ready(labels)

    if is_human_only(labels):
        return Suitability(mode=SuitabilityMode.HUMAN, reasons=[{"source": "label", "value": "agent:human-only"}])
    if not ready_label and local_assessment == _LOCAL_HUMAN_ONLY:
        return Suitability(mode=SuitabilityMode.HUMAN, reasons=[{"source": "local_assessment", "value": "human_only"}])

    if is_on_hold(labels):
        return Suitability(mode=SuitabilityMode.BLOCKED, reasons=[{"source": "label", "value": "agent:on-hold"}])

    if consider_dependencies:
        blocked_by = _unsatisfied_dependencies(task, graph)
        if blocked_by is None:
            return Suitability(mode=SuitabilityMode.BLOCKED, reasons=[{"source": "graph", "value": "unavailable"}])
        if blocked_by:
            return Suitability(
                mode=SuitabilityMode.BLOCKED,
                reasons=[{"source": "dependency", "value": dep} for dep in blocked_by],
                blocked_by=blocked_by,
            )

    if is_interactive_recommended(labels):
        return Suitability(
            mode=SuitabilityMode.INTERACTIVE,
            reasons=[{"source": "label", "value": "agent:interactive-recommended"}],
        )
    if not ready_label and local_assessment == _LOCAL_INTERACTIVE:
        return Suitability(
            mode=SuitabilityMode.INTERACTIVE,
            reasons=[{"source": "local_assessment", "value": "interactive_recommended"}],
        )

    if ready_label:
        return Suitability(mode=SuitabilityMode.AUTONOMOUS, reasons=[{"source": "label", "value": "agent:ready"}])
    if local_assessment == _LOCAL_READY:
        return Suitability(
            mode=SuitabilityMode.AUTONOMOUS,
            reasons=[{"source": "local_assessment", "value": "ready"}],
        )

    return Suitability(mode=SuitabilityMode.UNCLASSIFIED)


def suitability_payload(suitability: Suitability) -> dict:
    """The API shape of a classification: exactly `mode`, `reasons` and `blocked_by`.

    There is deliberately no schedulability flag: `mode` reflects labels and
    dependencies alone and ignores the task's state, as `DependencyGraph.to_dict()`
    documents for the graph node this mirrors.
    """
    return asdict(suitability)


# The two tracker states an issue can be picked up from: triaged (no spec yet) or
# researched (spec written). Earlier states still need triage or a spec, and later
# ones already have an agent or a PR.
_NEXT_TASK_STATES: frozenset[TaskState] = frozenset({TaskState.TRIAGED, TaskState.RESEARCHED})


@dataclass(frozen=True, slots=True)
class RankedTask:
    """One entry of the ranked "Up next" list.

    `rank` is the 1-based position in the full ordering, so a limited result
    still reports each entry's true place. `why` is the machine-readable
    explanation: `priority:<level>`, `unblocks:<n>` (only when n > 0),
    `phase:<n>` (only when the milestone parses) and `state:<state>`, in that order.
    """

    task: Task
    rank: int
    unblocks: int
    phase: int
    mode: SuitabilityMode
    why: list[str]


def _candidates(
    tasks: Iterable[Task],
    graph: DependencyGraph,
    running: Collection[int],
    open_prs: Collection[int],
) -> Iterator[tuple[int, Task, Suitability]]:
    """Yield `(issue id, task, suitability)` for every issue that could be picked up next.

    The one candidate pool behind `rank_next_tasks()` and `rank_and_count_next_tasks()`,
    so the ranking and the unclassified count cannot disagree about it: triaged or
    researched, no running agent, no open PR, classified exactly as the graph node
    is (no local assessment record). An id that does not parse as an issue number is
    skipped because it can never be a graph node.
    """
    for task in tasks:
        try:
            issue = int(task.id)
        except (TypeError, ValueError):
            continue
        if task.state not in _NEXT_TASK_STATES or issue in running or issue in open_prs:
            continue
        yield issue, task, assess_suitability(task, graph)


def unblock_count(issue: int, graph: DependencyGraph) -> int:
    """Count the open, non-epic issues that transitively depend on `issue`.

    Mirrors `unsatisfied_dependencies()`: an epic is a tracking container and a
    DONE issue has nothing left to unblock, so neither is counted and the walk does
    not continue through them. The visited set makes a dependency cycle terminate
    and keeps `issue` itself out of its own count.
    """
    seen = {issue}
    pending = deque([issue])
    count = 0
    while pending:
        for dependent in graph.get_dependents(pending.popleft()):
            if dependent in seen:
                continue
            seen.add(dependent)
            dependent_task = graph.get_task(dependent)
            if dependent_task is None or dependent_task.state == TaskState.DONE or is_epic(dependent_task.labels):
                continue
            count += 1
            pending.append(dependent)
    return count


def _why(task: Task, unblocks: int, phase: int) -> list[str]:
    why = [f"priority:{task.priority.value}"]
    if unblocks > 0:
        why.append(f"unblocks:{unblocks}")
    # Phase 0 is a real phase, so presence is an identity check, never truthiness.
    if phase != UNPHASED:
        why.append(f"phase:{phase}")
    why.append(f"state:{task.state.value}")
    return why


def _rank_candidates(
    candidates: Iterable[tuple[int, Task, Suitability]],
    graph: DependencyGraph,
    limit: int | None,
) -> list[RankedTask]:
    unranked = []
    for issue, task, suitability in candidates:
        if suitability.mode != SuitabilityMode.AUTONOMOUS:
            continue
        unblocks = unblock_count(issue, graph)
        phase = extract_phase_order(task.milestone)
        unranked.append(
            RankedTask(
                task=task,
                rank=0,
                unblocks=unblocks,
                phase=phase,
                mode=suitability.mode,
                why=_why(task, unblocks, phase),
            )
        )
    unranked.sort(key=lambda r: (r.task.priority.rank, -r.unblocks, r.phase, int(r.task.id)))
    ranked = [replace(r, rank=position) for position, r in enumerate(unranked, start=1)]
    return ranked if limit is None else ranked[:limit]


def _count_unclassified(candidates: Iterable[tuple[int, Task, Suitability]]) -> int:
    return sum(1 for _issue, _task, suitability in candidates if suitability.mode == SuitabilityMode.UNCLASSIFIED)


def rank_next_tasks(
    tasks: Iterable[Task],
    graph: DependencyGraph,
    *,
    running: Collection[int],
    open_prs: Collection[int],
    limit: int | None = None,
) -> list[RankedTask]:
    """Order the issues an agent should pick up next. Pure and LLM-free.

    Keeps `autonomous` issues in `_NEXT_TASK_STATES` with no running agent and no
    open PR (`unclassified` is deliberately left out). Orders them by priority rank,
    then most transitive dependents unblocked, then milestone phase (`UNPHASED`
    last), then issue number. The issue number is unique, so the order is total and
    does not depend on the input order.

    `limit=None` is unbounded; `0` is an empty result, not "no limit". A negative
    `limit` is the caller's bug and is not validated here.
    """
    return _rank_candidates(_candidates(tasks, graph, running, open_prs), graph, limit)


def rank_and_count_next_tasks(
    tasks: Iterable[Task],
    graph: DependencyGraph,
    *,
    running: Collection[int],
    open_prs: Collection[int],
    limit: int | None = None,
) -> tuple[list[RankedTask], int]:
    """`rank_next_tasks()` plus the count of candidates that no label has classified yet.

    Each candidate is classified once, so the ranking and the count come from one
    `assess_suitability()` pass. An unclassified candidate joins the ranking as soon
    as it gains `agent:ready`; backlog and needs-spec issues are outside the pool, so
    they are not counted. `limit` truncates the ranking only, never the count.
    """
    candidates = list(_candidates(tasks, graph, running, open_prs))
    return _rank_candidates(candidates, graph, limit), _count_unclassified(candidates)
