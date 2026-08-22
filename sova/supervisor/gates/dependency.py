"""Dependency gate: blocks tasks whose non-epic dependencies are not DONE."""

from __future__ import annotations

from sova.adapters.base import TaskState
from sova.supervisor.dependency_graph import DependencyGraph, is_epic
from sova.supervisor.gates import BlockReason


def check_dependency_gate(issue: int, graph: DependencyGraph) -> BlockReason | None:
    """Check if all non-epic dependencies are DONE (no I/O: reads in-memory graph only).

    Epic dependencies are skipped: epics are tracking containers,
    not real blockers.
    """
    deps = graph.get_dependencies(issue)
    if not deps:
        return None

    for dep_id in deps:
        dep_task = graph.get_task(dep_id)
        if dep_task is None:
            return BlockReason(
                gate="dependency",
                detail=f"Dependency #{dep_id} not found (missing reference)",
            )
        if is_epic(dep_task.labels):
            continue
        if dep_task.state != TaskState.DONE:
            return BlockReason(
                gate="dependency",
                detail=f"Blocked by #{dep_id} (state: {dep_task.state.value})",
            )

    return None
