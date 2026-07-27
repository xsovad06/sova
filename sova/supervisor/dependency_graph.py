"""Dependency graph engine -- parse issue dependencies, build DAG, check readiness."""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

from sova.adapters.base import Task, TaskAdapter, TaskState
from sova.utils.logging import get_logger
from sova.utils.markdown import extract_section

log = get_logger(component="supervisor.dependency_graph")

_DEP_PATTERN = re.compile(r"#(\d+)")

# States that should be excluded from "ready to work on" -- either already
# being worked on, already completed, or explicitly rejected/blocked.
_EXCLUDED_FROM_READY: frozenset[TaskState] = frozenset(
    {TaskState.DONE, TaskState.IN_PROGRESS, TaskState.IN_REVIEW, TaskState.HUMAN_ONLY}
)

# Actions a user or supervisor can take for each issue state, surfaced in the graph drawer.
_STATE_ACTIONS: dict[TaskState, list[dict]] = {
    TaskState.BACKLOG: [],
    TaskState.TRIAGED: [{"id": "researcher", "label": "Run Researcher", "role": "researcher"}],
    TaskState.RESEARCHED: [{"id": "developer", "label": "Run Developer", "role": "developer"}],
    TaskState.IN_PROGRESS: [],
    TaskState.IN_REVIEW: [
        {"id": "integrate-pr", "label": "Integrate PR", "role": "integrate-pr"},
        {"id": "address-pr", "label": "Address PR", "role": "address-pr"},
    ],
    TaskState.DONE: [],
    TaskState.HUMAN_ONLY: [],
}

_BODY_EXCERPT_LEN = 100


def _get_spec_meta(issue_id: int, project_dir: Path | None = None) -> dict | None:
    """Return spec metadata for an issue, or None if no spec file exists.

    Reads status, complexity, and open-question count from the spec file at
    ``.claude/specs/{issue_id}-*.md``.  Lazy-imports spec_service to avoid a
    hard dependency on the dashboard layer at module load time.
    """
    from sova.dashboard.services import spec_service  # lazy import -- dashboard layer

    spec = spec_service.read_spec(str(issue_id), project_dir)
    if spec is None:
        return None
    return {
        "url": f"/spec/{issue_id}",
        "status": spec.get("status", "draft"),
        "complexity": spec.get("complexity", "unknown"),
        "open_questions": len(spec.get("open_questions", [])),
    }


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
        """Validate the DAG: detect cycles and missing references.

        Always performs both checks for completeness -- callers get the full
        picture in one call.  Not short-circuited on missing_refs because
        cycle information is independently valuable.
        """
        all_ids = set(self._tasks.keys())

        # Find missing references (deps pointing outside the task set)
        missing: set[int] = set()
        for deps in self._deps.values():
            missing |= deps - all_ids

        # Cycle detection via Kahn's algorithm (only on known nodes)
        in_degree = self._build_in_degree(all_ids)
        cycle_members = sorted(all_ids - set(self._topo_sort(all_ids, in_degree)))

        return ValidationResult(
            valid=len(cycle_members) == 0 and len(missing) == 0,
            cycle_members=cycle_members,
            missing_refs=sorted(missing),
        )

    def get_ready_tasks(self) -> list[int]:
        """Issues whose dependencies are all DONE (ready to work on).

        Missing deps are treated as blocking (fail-closed).
        Issues already in progress, in review, done, or human-only are excluded.
        """
        all_ids = set(self._tasks.keys())
        ready: list[int] = []

        for tid, task in sorted(self._tasks.items()):
            if task.state in _EXCLUDED_FROM_READY:
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
        visited = self._collect_transitive_deps(issue)
        sub_in_degree = self._build_in_degree(visited)
        return self._topo_sort(visited, sub_in_degree)

    def get_parallel_groups(self) -> list[ParallelGroup]:
        """Group tasks into tiers that can execute in parallel.

        Tier 0 has no dependencies, tier 1 depends only on tier 0, etc.
        """
        all_ids = set(self._tasks.keys())
        in_degree = self._build_in_degree(all_ids)

        remaining = set(all_ids)
        groups: list[ParallelGroup] = []
        tier = 0

        while remaining:
            tier_nodes = sorted(tid for tid in remaining if in_degree[tid] == 0)
            if not tier_nodes:
                break
            groups.append(ParallelGroup(tier=tier, task_ids=tier_nodes))
            remaining -= set(tier_nodes)
            for nid in tier_nodes:
                for dependent in self._rdeps.get(nid, set()):
                    if dependent in remaining:
                        in_degree[dependent] -= 1
            tier += 1

        return groups

    # -- Internal helpers ------------------------------------------------------

    def _build_in_degree(self, node_ids: set[int]) -> dict[int, int]:
        """Build in-degree map for a subset of nodes."""
        in_degree = dict.fromkeys(node_ids, 0)
        for tid in node_ids:
            for dep in self._deps.get(tid, set()):
                if dep in node_ids:
                    in_degree[tid] += 1
        return in_degree

    def _topo_sort(self, node_ids: set[int], in_degree: dict[int, int]) -> list[int]:
        """Kahn's algorithm topological sort over a node subset."""
        queue: deque[int] = deque(sorted(nid for nid, deg in in_degree.items() if deg == 0))
        result: list[int] = []
        while queue:
            nid = queue.popleft()
            result.append(nid)
            for dependent in self._rdeps.get(nid, set()):
                if dependent in node_ids:
                    in_degree[dependent] -= 1
                    if in_degree[dependent] == 0:
                        queue.append(dependent)
        return result

    def _collect_transitive_deps(self, issue: int) -> set[int]:
        """BFS to collect all transitive dependencies including the issue itself."""
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
        return visited

    def to_dict(
        self,
        *,
        pr_map: dict[int, dict] | None = None,
        pr_state_actions: dict[str, list[dict]] | None = None,
        agent_map: dict[int, dict] | None = None,
        handoff_map: dict[int, dict] | None = None,
        last_run_map: dict[int, dict] | None = None,
        project_dir: Path | None = None,
    ) -> dict:
        """Serialize the graph for API responses.

        *pr_map* maps issue IDs to PR info dicts (pr_number, pr_url, pr_state,
        pr_state_label).  When provided, nodes are enriched with PR fields and
        IN_REVIEW nodes get PR-state-aware available_actions via *pr_state_actions*.

        *agent_map* maps issue IDs to running agent info (run_id, role, status, elapsed_seconds).
        *handoff_map* maps issue IDs to pending handoff info (next_action).
        *last_run_map* maps issue IDs to last terminal run info (run_id, status).
        *project_dir* is forwarded to ``_get_spec_meta`` so RESEARCHED nodes can
        be enriched with spec metadata and spec-aware action buttons.  When None,
        the spec service falls back to the contextvar-based project directory.

        Recomputes validate(), get_ready_tasks(), and get_parallel_groups()
        on every call.  Avoid calling repeatedly on the same graph instance;
        cache the result if multiple reads are needed.
        """
        effective_pr_map = pr_map or {}
        effective_agent_map = agent_map or {}
        effective_handoff_map = handoff_map or {}
        effective_last_run_map = last_run_map or {}
        nodes = []
        for tid, task in sorted(self._tasks.items()):
            body = task.body or ""
            # Skip section headers (#...) and list/dep items (-...) so the excerpt
            # shows the human-readable description, not dependency boilerplate.
            excerpt_lines = [
                ln.strip()
                for ln in body.splitlines()
                if ln.strip() and not ln.strip().startswith("#") and not ln.strip().startswith("-")
            ]
            excerpt = " ".join(excerpt_lines)[:_BODY_EXCERPT_LEN].strip()

            actions = list(_STATE_ACTIONS.get(task.state, []))
            pr_info = effective_pr_map.get(tid)

            # Override actions for IN_REVIEW nodes when PR state is known
            if pr_info and task.state == TaskState.IN_REVIEW and pr_state_actions:
                pr_state = pr_info.get("pr_state", "")
                if pr_state in pr_state_actions:
                    actions = list(pr_state_actions[pr_state])

            # Enrich RESEARCHED nodes with spec metadata and spec-aware actions
            spec_meta: dict | None = None
            if task.state == TaskState.RESEARCHED:
                try:
                    spec_meta = _get_spec_meta(tid, project_dir)
                except Exception:
                    log.warning("dependency_graph.spec_meta_failed", issue=tid, exc_info=True)
                if spec_meta:
                    actions = [
                        {"id": "view-spec", "label": "View Spec", "type": "link", "url": f"/spec/{tid}"},
                        {
                            "id": "approve-spec",
                            "label": "Approve & Develop",
                            "type": "api",
                            "url": f"/spec/{tid}/approve",
                        },
                        {
                            "id": "revise-spec",
                            "label": "Revise Spec",
                            "type": "api",
                            "url": f"/spec/{tid}/revise",
                        },
                    ]

            node: dict = {
                "id": tid,
                "title": task.title,
                "state": task.state.value,
                "milestone": task.milestone,
                "dependencies": sorted(self._deps.get(tid, set())),
                "body_excerpt": excerpt,
                "available_actions": actions,
            }
            if pr_info:
                node["pr_number"] = pr_info.get("pr_number")
                node["pr_url"] = pr_info.get("pr_url", "")
                node["pr_state"] = pr_info.get("pr_state", "")
                node["pr_state_label"] = pr_info.get("pr_state_label", "")
            agent_info = effective_agent_map.get(tid)
            if agent_info:
                node["agent_running"] = True
                node["agent_run_id"] = agent_info["run_id"]
                node["agent_role"] = agent_info["role"]
                node["agent_status"] = agent_info["status"]
                node["agent_elapsed_seconds"] = agent_info.get("elapsed_seconds", 0)
            handoff_info = effective_handoff_map.get(tid)
            if handoff_info:
                node["handoff_pending"] = True
                node["handoff_action"] = handoff_info.get("next_action", "")
            last_run_info = effective_last_run_map.get(tid)
            if last_run_info:
                node["last_run_status"] = last_run_info["status"]
                node["last_run_id"] = last_run_info["run_id"]
            if spec_meta:
                node["spec_meta"] = spec_meta
            nodes.append(node)

        validation = self.validate()
        ready = self.get_ready_tasks()
        groups = self.get_parallel_groups()

        return {
            "nodes": nodes,
            "edges": [{"from": e[0], "to": e[1]} for e in self.edges if e[1] in self._tasks],
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

    # Warn if there are multiple ## Dependencies sections
    dep_heading_count = len(re.findall(r"^## dependencies\s*$", body, re.MULTILINE | re.IGNORECASE))
    if dep_heading_count > 1:
        log.warning("Multiple '## Dependencies' sections found; using the first one")

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

    Fetches all issues (open + closed) so that:
    - open issues are always included
    - closed issues within a milestone that still has open issues are included
      (shows completed sub-tasks of an in-progress feature)
    - milestones where every issue is closed are excluded (feature is done)

    When *milestone* is provided, only tasks in that milestone are fetched.
    Dependencies outside the filtered set are fetched individually.
    """
    from sova.adapters.base import TaskFilters

    # Fetch up to 500 issues so the graph is not silently truncated on larger
    # repos.  gh CLI handles pagination automatically when --limit > 100.
    filters = TaskFilters(milestone=milestone, state="all", limit=500)
    tasks = await adapter.list_tasks(filters)

    # Milestones that have at least one open (non-done) issue
    active_milestones: set[str] = {t.milestone for t in tasks if t.milestone and t.state != TaskState.DONE}

    # Keep open issues + closed issues within active milestones
    tasks = [t for t in tasks if t.state != TaskState.DONE or (t.milestone and t.milestone in active_milestones)]

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
