"""Dependency graph engine -- parse issue dependencies, build DAG, check readiness."""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field

from sova.adapters.base import Task, TaskAdapter, TaskState
from sova.utils.logging import get_logger
from sova.utils.markdown import extract_section

log = get_logger(component="supervisor.dependency_graph")

_DEP_PATTERN = re.compile(r"#(\d+)")


@dataclass
class ValidationResult:
    """Result of DAG validation."""

    valid: bool
    cycle_members: list[int] = field(default_factory=list)
    missing_refs: list[int] = field(default_factory=list)


@dataclass
class ParallelGroup:
    """A group of tasks that can execute in parallel (same topological tier)."""

    tier: int
    task_ids: list[int] = field(default_factory=list)


class DependencyGraph:
    """DAG of issue dependencies with readiness checks and traversal.

    Built on-the-fly from issue bodies via the adapter layer.  No DB tables --
    the graph is ephemeral and rebuilt per API call.
    """

    def __init__(self, tasks: list[Task]) -> None:
        self._tasks: dict[int, Task] = {int(t.id): t for t in tasks}
        # adjacency: edges[A] = {B, C} means A depends on B and C
        self._deps: dict[int, set[int]] = defaultdict(set)
        # reverse: rdeps[B] = {A} means A depends on B
        self._rdeps: dict[int, set[int]] = defaultdict(set)

        for task in tasks:
            tid = int(task.id)
            deps = parse_dependencies(task.body, exclude_self=tid)
            self._deps[tid] = deps
            for dep in deps:
                self._rdeps[dep].add(tid)

    # -- Public API -----------------------------------------------------------

    @property
    def nodes(self) -> list[int]:
        """All issue IDs in the graph."""
        return sorted(self._tasks.keys())

    @property
    def edges(self) -> list[tuple[int, int]]:
        """All dependency edges as (dependent, dependency) tuples."""
        result: list[tuple[int, int]] = []
        for tid, deps in sorted(self._deps.items()):
            for dep in sorted(deps):
                result.append((tid, dep))
        return result

    def get_task(self, issue: int) -> Task | None:
        """Get task by ID, or None if not in the graph."""
        return self._tasks.get(issue)

    def has_task(self, issue: int) -> bool:
        """Check if an issue exists in the graph."""
        return issue in self._tasks

    def get_dependencies(self, issue: int) -> set[int]:
        """Direct dependencies of an issue."""
        return set(self._deps.get(issue, set()))

    def get_dependents(self, issue: int) -> set[int]:
        """Issues that directly depend on the given issue."""
        return set(self._rdeps.get(issue, set()))

    def validate(self) -> ValidationResult:
        """Validate the DAG: detect cycles and missing references."""
        all_ids = set(self._tasks.keys())

        # Find missing references (deps pointing outside the task set)
        missing: set[int] = set()
        for deps in self._deps.values():
            missing |= deps - all_ids

        # Cycle detection via Kahn's algorithm (only on known nodes)
        in_degree: dict[int, int] = {tid: 0 for tid in all_ids}
        for tid in all_ids:
            for dep in self._deps.get(tid, set()):
                if dep in all_ids:
                    in_degree[tid] += 1  # tid depends on dep -> incoming edge to tid

        queue: deque[int] = deque(tid for tid, deg in in_degree.items() if deg == 0)
        visited: list[int] = []

        while queue:
            nid = queue.popleft()
            visited.append(nid)
            for dependent in self._rdeps.get(nid, set()):
                if dependent in all_ids:
                    in_degree[dependent] -= 1
                    if in_degree[dependent] == 0:
                        queue.append(dependent)

        cycle_members = sorted(all_ids - set(visited))

        return ValidationResult(
            valid=len(cycle_members) == 0 and len(missing) == 0,
            cycle_members=cycle_members,
            missing_refs=sorted(missing),
        )

    def get_ready_tasks(self) -> list[int]:
        """Issues whose dependencies are all DONE (ready to work on).

        Missing deps are treated as blocking (fail-closed).
        Issues already DONE are excluded.
        """
        all_ids = set(self._tasks.keys())
        ready: list[int] = []

        for tid, task in sorted(self._tasks.items()):
            if task.state == TaskState.DONE:
                continue
            deps = self._deps.get(tid, set())
            if not deps:
                ready.append(tid)
                continue
            # All deps must be known AND done
            all_satisfied = True
            for dep in deps:
                if dep not in all_ids:
                    all_satisfied = False
                    break
                dep_task = self._tasks[dep]
                if dep_task.state != TaskState.DONE:
                    all_satisfied = False
                    break
            if all_satisfied:
                ready.append(tid)

        return ready

    def get_chain(self, issue: int) -> list[int]:
        """Transitive dependency chain for an issue (topological order).

        Returns all transitive dependencies ordered so that a dependency
        always appears before its dependents.  The issue itself is last.
        """
        # BFS to collect all transitive deps
        visited: set[int] = set()
        to_visit: deque[int] = deque([issue])
        while to_visit:
            nid = to_visit.popleft()
            if nid in visited:
                continue
            visited.add(nid)
            for dep in self._deps.get(nid, set()):
                if dep in self._tasks:
                    to_visit.append(dep)

        # Topological sort of the subgraph
        sub_in_degree: dict[int, int] = {nid: 0 for nid in visited}
        for nid in visited:
            for dep in self._deps.get(nid, set()):
                if dep in visited:
                    sub_in_degree[nid] += 1

        queue: deque[int] = deque(sorted(nid for nid, deg in sub_in_degree.items() if deg == 0))
        result: list[int] = []
        while queue:
            nid = queue.popleft()
            result.append(nid)
            for dependent in self._rdeps.get(nid, set()):
                if dependent in visited:
                    sub_in_degree[dependent] -= 1
                    if sub_in_degree[dependent] == 0:
                        queue.append(dependent)

        return result

    def get_parallel_groups(self) -> list[ParallelGroup]:
        """Group tasks into tiers that can execute in parallel.

        Tier 0 has no dependencies, tier 1 depends only on tier 0, etc.
        """
        all_ids = set(self._tasks.keys())

        # Build in-degree considering only known nodes
        in_degree: dict[int, int] = {tid: 0 for tid in all_ids}
        for tid in all_ids:
            for dep in self._deps.get(tid, set()):
                if dep in all_ids:
                    in_degree[tid] += 1

        remaining = set(all_ids)
        groups: list[ParallelGroup] = []
        tier = 0

        while remaining:
            # Nodes with zero in-degree in remaining set
            tier_nodes = sorted(tid for tid in remaining if in_degree[tid] == 0)
            if not tier_nodes:
                # Remaining nodes are in a cycle -- stop
                break
            groups.append(ParallelGroup(tier=tier, task_ids=tier_nodes))
            remaining -= set(tier_nodes)
            # Reduce in-degree for dependents
            for nid in tier_nodes:
                for dependent in self._rdeps.get(nid, set()):
                    if dependent in remaining:
                        in_degree[dependent] -= 1
            tier += 1

        return groups

    def to_dict(self) -> dict:
        """Serialize the graph for API responses."""
        nodes = []
        for tid, task in sorted(self._tasks.items()):
            nodes.append(
                {
                    "id": tid,
                    "title": task.title,
                    "state": task.state.value,
                    "milestone": task.milestone,
                    "dependencies": sorted(self._deps.get(tid, set())),
                }
            )

        validation = self.validate()
        ready = self.get_ready_tasks()
        groups = self.get_parallel_groups()

        return {
            "nodes": nodes,
            "edges": [{"from": e[0], "to": e[1]} for e in self.edges],
            "ready": ready,
            "parallel_groups": [{"tier": g.tier, "task_ids": g.task_ids} for g in groups],
            "validation": {
                "valid": validation.valid,
                "cycle_members": validation.cycle_members,
                "missing_refs": validation.missing_refs,
            },
        }


# -- Module-level helpers -----------------------------------------------------


def parse_dependencies(body: str, *, exclude_self: int | None = None) -> set[int]:
    """Extract issue references from the first ``## Dependencies`` section."""
    if not body:
        return set()

    # Case-insensitive heading search: find the actual heading text, then extract
    heading_match = re.search(r"^## (dependencies)\s*$", body, re.MULTILINE | re.IGNORECASE)
    section = extract_section(body, heading_match.group(1)) if heading_match else ""
    if not section:
        return set()

    deps: set[int] = set()
    for ref in _DEP_PATTERN.findall(section):
        dep_id = int(ref)
        if exclude_self is not None and dep_id == exclude_self:
            continue
        deps.add(dep_id)

    return deps


async def build_dependency_graph(
    adapter: TaskAdapter,
    *,
    milestone: str = "",
) -> DependencyGraph:
    """Build a dependency graph from the adapter's task list.

    When *milestone* is provided, only tasks in that milestone are fetched.
    Dependencies outside the filtered set are fetched individually.
    """
    from sova.adapters.base import TaskFilters

    filters = TaskFilters(milestone=milestone) if milestone else None
    tasks = await adapter.list_tasks(filters)

    # Collect all referenced deps and fetch missing ones
    all_dep_ids: set[int] = set()
    task_map: dict[int, Task] = {}
    for task in tasks:
        tid = int(task.id)
        task_map[tid] = task
        deps = parse_dependencies(task.body, exclude_self=tid)
        all_dep_ids |= deps

    missing_ids = all_dep_ids - set(task_map.keys())
    if missing_ids:

        async def _fetch(mid: int) -> Task | None:
            try:
                return await adapter.get_task(str(mid))
            except Exception:
                log.debug("Could not fetch dependency #%d", mid, exc_info=True)
                return None

        results = await asyncio.gather(*[_fetch(mid) for mid in missing_ids])
        for dep_task in results:
            if dep_task is not None:
                task_map[int(dep_task.id)] = dep_task

    return DependencyGraph(list(task_map.values()))
