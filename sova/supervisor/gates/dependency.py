"""Dependency gate: blocks tasks whose non-epic dependencies are not DONE."""

from __future__ import annotations

from sova.supervisor.dependency_graph import DependencyGraph
from sova.supervisor.gates import BlockReason
from sova.supervisor.suitability import unsatisfied_dependencies
from sova.utils.logging import get_logger

log = get_logger(component="supervisor.gates.dependency")


def check_dependency_gate(issue: int, graph: DependencyGraph) -> BlockReason | None:
    """Check if all non-epic dependencies are DONE (no I/O: reads in-memory graph only).

    Epic dependencies are skipped: epics are tracking containers, not real
    blockers. Delegates the walk to `unsatisfied_dependencies()`
    (`sova/supervisor/suitability.py`), the same one `assess_suitability()`
    uses, so the epic-skip and missing-reference rules cannot drift between
    this gate and the classifier.

    `unsatisfied_dependencies()` is three-valued: `None` means dependency data
    is unavailable (the issue is not in `graph`), `[]` means satisfied, and a
    non-empty list means blocked. `assess_suitability()` fails CLOSED on
    `None`; this gate deliberately fails OPEN on it instead (unchanged from
    before this module existed), since a graph that does not contain `issue`
    at all is this gate's caller's problem to have built, not a reason to
    withhold a spawn. The two cases are handled separately, rather than both
    falling into one `if not blocked_by` check, only so the fail-open case is
    logged and not silently indistinguishable from "satisfied".
    """
    blocked_by = unsatisfied_dependencies(issue, graph)
    if blocked_by is None:
        log.debug("dependency_gate.data_unavailable", issue=issue)
        return None
    if not blocked_by:
        return None

    dep_id = blocked_by[0]
    dep_task = graph.get_task(dep_id)
    if dep_task is None:
        return BlockReason(
            gate="dependency",
            detail=f"Dependency #{dep_id} not found (missing reference)",
        )
    return BlockReason(
        gate="dependency",
        detail=f"Blocked by #{dep_id} (state: {dep_task.state.value})",
    )
