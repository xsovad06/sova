"""Unified suitability classifier: tracker labels + local assessment + dependency graph.

Single source of truth for whether an issue is fit for autonomous agent work.
`DependencyGraph.get_ready_tasks()`, `queue_maintenance._discover_ready()`, and
`check_human_involvement_gate()` all route through `assess_suitability()` so the
three call sites agree on one classification instead of each re-deriving a
slightly different subset of the exclusion rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from sova.adapters.base import Task, TaskState
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
