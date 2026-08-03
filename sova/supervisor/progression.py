"""Dependency-aware task progression engine.

Deterministic state machine that evaluates active tasks and produces
ProgressionDecision objects based on observable state (issue labels,
dependency graph, CodeRabbit quota, agent slots, budget). No LLM calls.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from sova.adapters.base import TaskAdapter, TaskState
from sova.config.loader import load_config
from sova.config.models import ProjectConfig, SupervisorConfig
from sova.core.state import TASK_RUN_TERMINAL
from sova.dashboard.services.agent_handoff import _count_address_review_runs
from sova.dashboard.services.agent_recovery import _is_process_alive, get_sova_review_verdict
from sova.dashboard.services.agent_validation import _check_issue_budget
from sova.db.models import TaskRun
from sova.git.pr import find_pr_for_issue
from sova.supervisor.dependency_graph import DependencyGraph, build_dependency_graph
from sova.supervisor.file_overlap import (
    BranchFileSet,
    check_file_overlap,
    get_active_branch_file_sets,
    predict_candidate_files,
)
from sova.utils.logging import get_logger

try:
    import psutil

    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False

log = get_logger(component="supervisor.progression")

# awaiting_approval is intentionally excluded: a completed researcher whose spec is pending
# human review must still block re-spawn even though the agent process has exited.
_ALREADY_RUNNING_TERMINAL = TASK_RUN_TERMINAL - {"awaiting_approval"}

_NOT_COMPUTED = object()  # sentinel: distinguish "not precomputed" from "checked, no block"


class ProgressionAction(StrEnum):
    SPAWN_RESEARCHER = "spawn_researcher"
    SPAWN_DEVELOPER = "spawn_developer"
    SPAWN_INTEGRATE = "spawn_integrate"
    SPAWN_ADDRESS_REVIEW = "spawn_address_review"
    WAIT = "wait"
    BLOCKED = "blocked"
    SPAWN_REBASE = "spawn_rebase"
    CHECKPOINT_NEEDED = "checkpoint_needed"


@dataclass(frozen=True, slots=True)
class BlockReason:
    gate: str
    detail: str


@dataclass(frozen=True, slots=True)
class ProgressionDecision:
    issue_number: int
    action: ProgressionAction
    role: str | None = None
    reason: str = ""
    blocked_by: tuple[BlockReason, ...] = field(default_factory=tuple)
    pr_number: int | None = None


# Map ProgressionAction to the role string used by start_agent()
_ACTION_TO_ROLE: dict[ProgressionAction, str] = {
    ProgressionAction.SPAWN_RESEARCHER: "researcher",
    ProgressionAction.SPAWN_DEVELOPER: "developer",
    ProgressionAction.SPAWN_INTEGRATE: "command:integrate-pr",
    ProgressionAction.SPAWN_ADDRESS_REVIEW: "developer",
}

# Actions that should trigger issue assignment on spawn (development work, not post-work).
_ASSIGN_ACTIONS = frozenset({ProgressionAction.SPAWN_RESEARCHER, ProgressionAction.SPAWN_DEVELOPER})

# Actions that do not trigger agent spawning (used by daemon to filter before approval/execution).
NON_ACTIONABLE_ACTIONS = frozenset(
    {
        ProgressionAction.WAIT,
        ProgressionAction.BLOCKED,
        ProgressionAction.CHECKPOINT_NEEDED,
    }
)


class TaskProgressionEngine:
    """Evaluate active tasks and produce deterministic progression decisions."""

    def __init__(
        self,
        config: SupervisorConfig,
        adapter: TaskAdapter,
        project_dir: Path,
        session_factory: async_sessionmaker,
    ) -> None:
        self._config = config
        self._adapter = adapter
        self._project_dir = project_dir
        self._session_factory = session_factory
        self._last_graph: DependencyGraph | None = None

    async def evaluate_all(self) -> list[ProgressionDecision]:
        """Scan all active tasks, return next action for each."""
        # Check rate limit before making any API calls (graph build calls list_tasks)
        global_rate_limit = self._check_github_rate_limit_gate()
        if global_rate_limit is not None:
            log.info("evaluate_all.skipped_rate_limited")
            return []

        try:
            graph = await build_dependency_graph(self._adapter)
        except Exception:
            log.warning("evaluate_all.graph_build_failed", exc_info=True)
            return []

        self._last_graph = graph

        # Load config once for the whole evaluation cycle
        cfg = load_config(self._project_dir)

        # Pre-compute global gates once, then decrement as slots/quota are consumed
        global_memory = self._check_memory_pressure_gate(cfg)
        global_quota = await self._check_quota_gate(ProgressionAction.SPAWN_DEVELOPER, cfg=cfg)

        # Pre-fetch merge conflict state for all open PRs (fail-open)
        try:
            from sova.dashboard.services.pr_service import get_pr_mergeability_map

            precomputed_conflicts = await get_pr_mergeability_map()
        except Exception:
            log.debug("evaluate_all.mergeability_fetch_failed", exc_info=True)
            precomputed_conflicts = {}

        # Pre-fetch active branch file sets for file overlap gate (fail-open)
        precomputed_file_sets: list[BranchFileSet] | None = None
        if self._config.file_overlap_gate:
            try:
                precomputed_file_sets = await get_active_branch_file_sets(
                    self._session_factory,
                    self._project_dir,
                )
            except Exception:
                log.debug("evaluate_all.file_overlap_fetch_failed", exc_info=True)

        # Compute alive count once: used for both the slot gate and remaining capacity
        alive_count = await self._get_alive_count()
        global_slots: BlockReason | None = None
        if alive_count >= cfg.max_parallel_agents:
            global_slots = BlockReason(
                gate="slots",
                detail=f"All agent slots occupied ({alive_count}/{cfg.max_parallel_agents})",
            )
        remaining_slots = cfg.max_parallel_agents - alive_count
        remaining_quota = not bool(global_quota)  # True if quota is available

        # When a task queue is configured, evaluate only queued issues (in order),
        # skipping blocked items without removing them. Read from self._config
        # (SupervisorConfig) which the daemon creates fresh each poll cycle.
        task_queue = self._config.task_queue
        if task_queue:
            node_set = set(graph.nodes)
            task_ids: list[int] = []
            skipped: list[int] = []
            for qid in task_queue:
                (task_ids if qid in node_set else skipped).append(qid)
            if skipped:
                log.warning("evaluate_all.queue_items_not_in_graph", skipped=skipped)
        else:
            task_ids = list(graph.nodes)

        tasks = [graph.get_task(nid) for nid in task_ids]
        decisions: list[ProgressionDecision] = []
        for task in tasks:
            if task is None:
                continue
            issue = int(task.id)

            # Re-check rate limit each iteration: if a GitHub API call during
            # a prior task's evaluation triggered the tracker, stop making
            # further API calls. The precomputed None from line 119 is stale
            # once the tracker transitions mid-loop.
            mid_loop_rate_limit = self._check_github_rate_limit_gate()
            if mid_loop_rate_limit is not None:
                log.info("evaluate_all.mid_loop_rate_limited", issue=issue)
                break

            # Recompute slot blocker based on remaining capacity
            effective_slots = global_slots
            if effective_slots is None and remaining_slots <= 0:
                effective_slots = BlockReason(
                    gate="slots",
                    detail="All agent slots would be occupied (batch capacity exhausted)",
                )

            # Recompute quota blocker based on remaining capacity
            effective_quota = global_quota
            if effective_quota is None and not remaining_quota:
                effective_quota = BlockReason(
                    gate="quota",
                    detail="CodeRabbit quota would be exhausted (batch capacity exhausted)",
                )

            decision = await self._evaluate_single(
                issue,
                task.state,
                graph,
                precomputed_memory=global_memory,
                precomputed_rate_limit=global_rate_limit,
                precomputed_quota=effective_quota,
                precomputed_slots=effective_slots,
                precomputed_conflicts=precomputed_conflicts,
                precomputed_file_sets=precomputed_file_sets,
                task_labels=task.labels,
                task_body=task.body,
                task_assignees=task.assignees,
            )
            decisions.append(decision)

            # Decrement capacity for actionable decisions
            if decision.action not in NON_ACTIONABLE_ACTIONS and decision.action != ProgressionAction.SPAWN_REBASE:
                remaining_slots -= 1
                if decision.action == ProgressionAction.SPAWN_DEVELOPER:
                    remaining_quota = False

        return decisions

    async def evaluate_task(self, issue_number: int) -> ProgressionDecision:
        """Evaluate a single task's readiness for progression."""
        # Check rate limit before any GitHub API calls (get_state, build_dependency_graph, etc.)
        rate_limit_block = self._check_github_rate_limit_gate()
        if rate_limit_block is not None:
            return ProgressionDecision(
                issue_number=issue_number,
                action=ProgressionAction.BLOCKED,
                reason="GitHub API rate limited",
                blocked_by=(rate_limit_block,),
            )

        try:
            state = await self._adapter.get_state(str(issue_number))
        except Exception:
            log.warning("evaluate_task.get_state_failed", issue=issue_number, exc_info=True)
            return ProgressionDecision(
                issue_number=issue_number,
                action=ProgressionAction.BLOCKED,
                reason="Failed to fetch task state from tracker",
                blocked_by=(BlockReason(gate="adapter", detail="get_state() call failed"),),
            )

        # Short-circuit: skip graph build if no transition is possible or automation is disabled
        candidate = self._determine_transition(state)
        if candidate is None:
            return ProgressionDecision(
                issue_number=issue_number,
                action=ProgressionAction.WAIT,
                reason=f"No transition available from state '{state.value}'",
            )
        if candidate == ProgressionAction.CHECKPOINT_NEEDED:
            return ProgressionDecision(
                issue_number=issue_number,
                action=ProgressionAction.CHECKPOINT_NEEDED,
                reason=f"Automation disabled for state '{state.value}': requires human approval",
            )

        try:
            graph = await build_dependency_graph(self._adapter)
        except Exception:
            log.warning("evaluate_task.graph_build_failed", issue=issue_number, exc_info=True)
            return ProgressionDecision(
                issue_number=issue_number,
                action=ProgressionAction.BLOCKED,
                reason="Failed to build dependency graph",
                blocked_by=(BlockReason(gate="adapter", detail="build_dependency_graph() failed"),),
            )

        try:
            from sova.dashboard.services.pr_service import get_pr_mergeability_map

            precomputed_conflicts = await get_pr_mergeability_map()
        except Exception:
            log.debug("evaluate_task.mergeability_fetch_failed", issue=issue_number, exc_info=True)
            precomputed_conflicts = {}

        # Fetch task info and file sets for file overlap gate
        precomputed_file_sets: list[BranchFileSet] | None = None
        task_labels: list[str] = []
        task_body: str = ""
        task_assignees: list[str] = []
        task = graph.get_task(issue_number)
        if task is not None:
            task_labels = task.labels
            task_body = task.body
            task_assignees = task.assignees

        if self._config.file_overlap_gate and task is not None:
            try:
                precomputed_file_sets = await get_active_branch_file_sets(
                    self._session_factory,
                    self._project_dir,
                    exclude_issue=str(issue_number),
                )
            except Exception:
                log.debug("evaluate_task.file_overlap_fetch_failed", issue=issue_number, exc_info=True)

        return await self._evaluate_single(
            issue_number,
            state,
            graph,
            precomputed_conflicts=precomputed_conflicts,
            precomputed_file_sets=precomputed_file_sets,
            task_labels=task_labels,
            task_body=task_body,
            task_assignees=task_assignees,
        )

    async def execute_decision(self, decision: ProgressionDecision) -> dict:
        """Spawn agent based on decision. Returns start_agent result or error dict."""
        if decision.action in {ProgressionAction.WAIT, ProgressionAction.BLOCKED, ProgressionAction.CHECKPOINT_NEEDED}:
            return {"skipped": True, "action": decision.action.value, "reason": decision.reason}

        if decision.action == ProgressionAction.SPAWN_REBASE:
            from sova.supervisor.rebase import attempt_auto_rebase

            return await attempt_auto_rebase(
                decision.issue_number,
                self._project_dir,
                self._session_factory,
            )

        from sova.config.registry import find_slug_for_path
        from sova.dashboard.services.agent_lifecycle import start_agent

        role = decision.role or _ACTION_TO_ROLE.get(decision.action)
        if role is None:
            return {"error": f"No role mapping for action {decision.action}"}

        kwargs: dict = {"issue": str(decision.issue_number), "role": role}

        # Pass the correct project slug so start_agent writes to this project's DB
        # (not the default project). Without this, multi-project supervisors spawn
        # agents in the wrong project context and the circuit breaker never sees the
        # failures.
        if self._project_dir is not None:
            slug = find_slug_for_path(self._project_dir)
            if slug is None:
                return {"error": f"Project path is not registered: {self._project_dir}"}
            kwargs["slug"] = slug

        # Actions that operate on an existing PR need the PR number
        if decision.action in (ProgressionAction.SPAWN_INTEGRATE, ProgressionAction.SPAWN_ADDRESS_REVIEW):
            pr_number = decision.pr_number or await self._find_pr_for_issue(decision.issue_number)
            if pr_number is None:
                return {"error": f"No open PR found for issue #{decision.issue_number}"}
            kwargs["pr_number"] = pr_number

        # Claim unassigned issues before spawning (distributed ownership).
        # The ownership gate already verified this issue is either unassigned or assigned
        # to us, so we attempt the assign unconditionally (idempotent on GitHub).
        # Only assign for development actions (researcher, developer), not for post-work
        # actions (integrate) where the work is already complete.
        if self._config.respect_ownership and decision.action in _ASSIGN_ACTIONS and role:
            try:
                claimed = await self._adapter.assign(str(decision.issue_number), role)
                if claimed:
                    log.info(
                        "ownership.claimed",
                        issue=decision.issue_number,
                        user=self._adapter.github_user,
                        role=role,
                    )
                else:
                    log.warning(
                        "ownership.claim_failed",
                        issue=decision.issue_number,
                        user=self._adapter.github_user,
                    )
            except Exception:
                log.warning("ownership.claim_failed", issue=decision.issue_number, exc_info=True)

        log.info(
            "progression.execute",
            issue=decision.issue_number,
            action=decision.action.value,
            role=role,
        )
        return await start_agent(**kwargs)

    async def execute_decisions(self, decisions: list[ProgressionDecision]) -> list[dict]:
        """Execute all actionable decisions (filters out WAIT/BLOCKED/CHECKPOINT_NEEDED).

        Respects ``max_spawns_per_cycle`` to prevent burst API usage.
        """
        actionable = [d for d in decisions if d.action not in NON_ACTIONABLE_ACTIONS]
        cap = self._config.max_spawns_per_cycle
        if len(actionable) > cap:
            log.info(
                "execute_decisions.capped",
                total=len(actionable),
                cap=cap,
                deferred=len(actionable) - cap,
            )
            actionable = actionable[:cap]
        results: list[dict] = []
        for decision in actionable:
            result = await self.execute_decision(decision)
            results.append(result)
        return results

    def _all_children_done(self, children: list[int], graph: DependencyGraph) -> bool:
        """Check if all child issues are in DONE state."""
        for child_id in children:
            child_task = graph.get_task(child_id)
            if child_task is None or child_task.state != TaskState.DONE:
                return False
        return True

    async def _try_close_epic(self, node_id: int, title: str, children: list[int]) -> dict:
        """Attempt to close an epic and return result dict."""
        try:
            await self._adapter.transition_state(str(node_id), TaskState.DONE)
            log.info(
                "auto_close_epics.closed",
                issue=node_id,
                title=title,
                children=sorted(children),
            )
            return {"issue": node_id, "title": title, "closed": True}
        except Exception:
            log.warning("auto_close_epics.transition_failed", issue=node_id, exc_info=True)
            return {"issue": node_id, "title": title, "closed": False}

    async def auto_close_epics(self, graph: DependencyGraph | None = None) -> list[dict]:
        """Auto-close epic issues when all child issues are DONE.

        Returns a list of closed epic info dicts: {issue, title, closed}.
        Fail-open: returns [] on graph build failure.

        Epics are tracking containers (type: epic label) that should be closed
        when all their children (issues that list the epic in Dependencies) are done.
        Epics with no children are never auto-closed (manual tracking container).
        Manually closed epics are not reopened if children become un-done.

        When ``graph`` is provided (e.g. from a prior ``evaluate_all`` call),
        it is reused to avoid a redundant ``list_tasks`` API call.
        """
        from sova.supervisor.dependency_graph import is_epic

        if graph is None:
            graph = self._last_graph
        if graph is None:
            if self._check_github_rate_limit_gate() is not None:
                log.info("auto_close_epics.skipped_rate_limited")
                return []
            try:
                graph = await build_dependency_graph(self._adapter)
            except Exception:
                log.warning("auto_close_epics.graph_build_failed", exc_info=True)
                return []

        results: list[dict] = []
        for node_id in graph.nodes:
            task = graph.get_task(node_id)
            if task is None or not is_epic(task.labels):
                continue
            if task.state == TaskState.DONE:
                continue

            children = graph.get_dependents(node_id)
            if not children:
                continue

            if self._all_children_done(children, graph):
                result = await self._try_close_epic(node_id, task.title, children)
                results.append(result)

        return results

    # -- Internal evaluation logic ------------------------------------------------

    async def _evaluate_single(
        self,
        issue_number: int,
        state: TaskState,
        graph: DependencyGraph,
        *,
        precomputed_memory: BlockReason | None | object = _NOT_COMPUTED,
        precomputed_rate_limit: BlockReason | object | None = _NOT_COMPUTED,
        precomputed_quota: BlockReason | None | object = _NOT_COMPUTED,
        precomputed_slots: BlockReason | None | object = _NOT_COMPUTED,
        precomputed_conflicts: dict[int, str] | None = None,
        precomputed_file_sets: list[BranchFileSet] | None = None,
        task_labels: list[str] | None = None,
        task_body: str = "",
        task_assignees: list[str] | None = None,
    ) -> ProgressionDecision:
        """Evaluate a single task against all gates."""
        candidate = self._determine_transition(state)
        if candidate is None:
            return ProgressionDecision(
                issue_number=issue_number,
                action=ProgressionAction.WAIT,
                reason=f"No transition available from state '{state.value}'",
            )

        # Short-circuit: CHECKPOINT_NEEDED means automation is disabled -- no gates to run
        if candidate == ProgressionAction.CHECKPOINT_NEEDED:
            return ProgressionDecision(
                issue_number=issue_number,
                action=ProgressionAction.CHECKPOINT_NEEDED,
                reason=f"Automation disabled for state '{state.value}': requires human approval",
            )

        # Refine IN_REVIEW: check SOVA verdict to decide between address-review and integrate.
        # _determine_transition returns SPAWN_INTEGRATE as a placeholder for IN_REVIEW when
        # either auto flag is enabled; we refine it here based on the actual PR verdict.
        refined_pr: int | None = None
        if state == TaskState.IN_REVIEW and candidate == ProgressionAction.SPAWN_INTEGRATE:
            candidate, refined_pr = await self._refine_in_review_action(issue_number)
            if candidate == ProgressionAction.WAIT:
                return ProgressionDecision(
                    issue_number=issue_number,
                    action=ProgressionAction.WAIT,
                    reason="No PR found for IN_REVIEW issue",
                )
            if candidate == ProgressionAction.CHECKPOINT_NEEDED:
                return ProgressionDecision(
                    issue_number=issue_number,
                    action=ProgressionAction.CHECKPOINT_NEEDED,
                    reason=f"Automation disabled for state '{state.value}': requires human approval",
                )

        blockers, discovered_pr = await self._collect_gate_blockers(
            issue_number,
            candidate,
            graph,
            precomputed_memory=precomputed_memory,
            precomputed_rate_limit=precomputed_rate_limit,
            precomputed_quota=precomputed_quota,
            precomputed_slots=precomputed_slots,
            precomputed_conflicts=precomputed_conflicts,
            precomputed_file_sets=precomputed_file_sets,
            task_labels=task_labels,
            task_body=task_body,
            task_assignees=task_assignees,
            refined_pr=refined_pr,
        )

        if blockers:
            all_conflict = all(b.gate == "conflict" for b in blockers)
            if all_conflict and self._config.auto_rebase:
                return ProgressionDecision(
                    issue_number=issue_number,
                    action=ProgressionAction.SPAWN_REBASE,
                    reason="PR has merge conflicts, attempting auto-rebase",
                )

            reasons = "; ".join(b.detail for b in blockers)
            return ProgressionDecision(
                issue_number=issue_number,
                action=ProgressionAction.BLOCKED,
                role=_ACTION_TO_ROLE.get(candidate),
                reason=f"Blocked: {reasons}",
                blocked_by=tuple(blockers),
            )

        final_pr = refined_pr or discovered_pr
        return ProgressionDecision(
            issue_number=issue_number,
            action=candidate,
            role=_ACTION_TO_ROLE.get(candidate),
            reason=f"Ready to {candidate.value}",
            pr_number=final_pr,
        )

    async def _collect_gate_blockers(
        self,
        issue_number: int,
        candidate: ProgressionAction,
        graph: DependencyGraph,
        *,
        precomputed_memory: BlockReason | None | object = _NOT_COMPUTED,
        precomputed_rate_limit: BlockReason | object | None = _NOT_COMPUTED,
        precomputed_quota: BlockReason | None | object = _NOT_COMPUTED,
        precomputed_slots: BlockReason | None | object = _NOT_COMPUTED,
        precomputed_conflicts: dict[int, str] | None = None,
        precomputed_file_sets: list[BranchFileSet] | None = None,
        task_labels: list[str] | None = None,
        task_body: str = "",
        task_assignees: list[str] | None = None,
        refined_pr: int | None = None,
    ) -> tuple[list[BlockReason], int | None]:
        """Run all gate checks and return (active_blockers, discovered_pr_number)."""
        blockers: list[BlockReason] = []

        # Rate limit gate first: if GitHub API is throttled, skip gates that make API calls
        # (ownership calls find_pr_for_issue, repeated_failures/budget query DB only but
        # circuit_breaker calls _count_address_review_runs which is DB-only too).
        if precomputed_rate_limit is _NOT_COMPUTED:
            rate_limit_block = self._check_github_rate_limit_gate()
        else:
            rate_limit_block = precomputed_rate_limit
        if rate_limit_block:
            blockers.append(rate_limit_block)
            return blockers, None

        # Dependency gate is sync (reads in-memory graph only)
        if self._config.respect_dependencies:
            dep_block = self._check_dependency_gate(issue_number, graph)
            if dep_block:
                blockers.append(dep_block)

        # Run async per-task gates concurrently (ownership gate returns a tuple)
        running_result, budget_result, ownership_result = await asyncio.gather(
            self._check_already_running(issue_number),
            self._check_budget_gate(issue_number),
            self._check_ownership_gate(issue_number, candidate, task_assignees=task_assignees),
        )
        ownership_block, discovered_pr = ownership_result

        simple_results: list[BlockReason | None] = [running_result, budget_result, ownership_block]
        if candidate == ProgressionAction.SPAWN_RESEARCHER:
            simple_results.append(await self._check_repeated_failures_gate(issue_number))
        if candidate == ProgressionAction.SPAWN_ADDRESS_REVIEW:
            cb_pr = refined_pr or discovered_pr
            cb_block = await self._check_address_review_circuit_breaker_gate(issue_number, pr_number=cb_pr)
            simple_results.append(cb_block)
        blockers.extend(r for r in simple_results if r is not None)

        # Merge conflict gate (only blocks integrate actions)
        if precomputed_conflicts is not None and candidate == ProgressionAction.SPAWN_INTEGRATE:
            conflict_block = self._check_merge_conflict_gate(issue_number, precomputed_conflicts)
            if conflict_block:
                blockers.append(conflict_block)

        # File overlap gate (per-task, uses precomputed file sets)
        if self._config.file_overlap_gate and precomputed_file_sets is not None:
            overlap_block = self._check_file_overlap_gate(
                issue_number,
                precomputed_file_sets,
                task_labels or [],
                task_body,
            )
            if overlap_block:
                blockers.append(overlap_block)

        # Add remaining precomputed global gates (rate limit already resolved above)
        global_blocks = await self._resolve_global_gates(
            candidate,
            precomputed_quota=precomputed_quota,
            precomputed_slots=precomputed_slots,
            precomputed_memory=precomputed_memory,
            precomputed_rate_limit=rate_limit_block,  # already resolved, pass through
        )
        blockers.extend(global_blocks)

        return blockers, discovered_pr

    async def _resolve_global_gates(
        self,
        candidate: ProgressionAction,
        *,
        precomputed_quota: BlockReason | None | object,
        precomputed_slots: BlockReason | None | object,
        precomputed_memory: BlockReason | None | object,
        precomputed_rate_limit: BlockReason | object | None = _NOT_COMPUTED,
    ) -> list[BlockReason]:
        """Resolve precomputed-or-on-demand global gates and return active blockers."""
        blocks: list[BlockReason] = []

        # GitHub API rate limit gate (runs first: if API is down, other gates may produce bad data)
        if precomputed_rate_limit is _NOT_COMPUTED:
            rate_limit_block = self._check_github_rate_limit_gate()
        else:
            rate_limit_block = precomputed_rate_limit
        if rate_limit_block:
            blocks.append(rate_limit_block)

        # Quota gate only applies to actions that produce PRs (SPAWN_DEVELOPER)
        if precomputed_quota is _NOT_COMPUTED:
            quota_block = await self._check_quota_gate(candidate)
        elif candidate == ProgressionAction.SPAWN_DEVELOPER:
            quota_block = precomputed_quota
        else:
            quota_block = None
        if quota_block:
            blocks.append(quota_block)

        if precomputed_slots is _NOT_COMPUTED:
            slot_block = await self._check_slot_gate()
        else:
            slot_block = precomputed_slots
        if slot_block:
            blocks.append(slot_block)

        if precomputed_memory is _NOT_COMPUTED:
            memory_block = self._check_memory_pressure_gate()
        else:
            memory_block = precomputed_memory
        if memory_block:
            blocks.append(memory_block)

        return blocks

    def _determine_transition(self, state: TaskState) -> ProgressionAction | None:
        """Map current state to candidate action based on config flags.

        Returns the action to take, CHECKPOINT_NEEDED when a transition is possible but
        the relevant auto flag is disabled (needs human approval), or None when no
        transition exists from this state.
        """
        if state == TaskState.TRIAGED:
            return (
                ProgressionAction.SPAWN_RESEARCHER
                if self._config.auto_research
                else ProgressionAction.CHECKPOINT_NEEDED
            )
        if state == TaskState.RESEARCHED:
            return (
                ProgressionAction.SPAWN_DEVELOPER if self._config.auto_develop else ProgressionAction.CHECKPOINT_NEEDED
            )
        if state == TaskState.IN_REVIEW:
            # IN_REVIEW has two possible actions: address-review (if SOVA verdict is revise/block)
            # or integrate (if PR is approved). The actual action is refined in _refine_in_review_action
            # which checks the SOVA verdict (requires I/O). Return SPAWN_INTEGRATE as a placeholder
            # if either auto flag is enabled; CHECKPOINT_NEEDED if both are disabled.
            if self._config.auto_integrate or self._config.auto_address_review:
                return ProgressionAction.SPAWN_INTEGRATE
            return ProgressionAction.CHECKPOINT_NEEDED
        # BACKLOG: triage is manual per spec
        # NEEDS_SPEC: human approves spec externally
        # IN_PROGRESS: handled by existing role chaining
        # DONE, HUMAN_ONLY: no action
        return None

    def _check_dependency_gate(self, issue: int, graph: DependencyGraph) -> BlockReason | None:
        """Check if all dependencies are DONE (no I/O: reads in-memory graph only)."""
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
            if dep_task.state != TaskState.DONE:
                return BlockReason(
                    gate="dependency",
                    detail=f"Blocked by #{dep_id} (state: {dep_task.state.value})",
                )

        return None

    async def _check_quota_gate(
        self, action: ProgressionAction, *, cfg: ProjectConfig | None = None
    ) -> BlockReason | None:
        """Check CodeRabbit quota headroom for actions that produce PRs."""
        if action != ProgressionAction.SPAWN_DEVELOPER:
            return None

        try:
            from sova.supervisor.coderabbit_quota import get_quota_status

            if cfg is None:
                cfg = load_config(self._project_dir)
            if not cfg.coderabbit_quota.enabled:
                return None

            async with self._session_factory() as session:
                status = await get_quota_status(session, cfg.coderabbit_quota)
                if not status.can_create_pr:
                    wait_msg = ""
                    if status.next_available_minutes is not None:
                        wait_msg = f" (available in {status.next_available_minutes:.0f}m)"
                    return BlockReason(
                        gate="quota",
                        detail=f"CodeRabbit quota exhausted{wait_msg}",
                    )
        except Exception:
            log.debug("quota_gate.check_failed", exc_info=True)

        return None

    async def _get_alive_count(self) -> int:
        """Count active agent reservations: alive processes plus pending (PID-less) runs."""
        try:
            async with self._session_factory() as session:
                stmt = select(TaskRun).where(TaskRun.status.notin_(TASK_RUN_TERMINAL))
                result = await session.execute(stmt)
                active_runs = result.scalars().all()
                count = 0
                for run in active_runs:
                    if run.pid is None:
                        count += 1  # pending: PID not yet assigned, count as active reservation
                    elif _is_process_alive(run.pid):
                        count += 1
                return count
        except Exception:
            log.debug("get_alive_count.failed", exc_info=True)
            return 0

    async def _check_slot_gate(self, *, cfg: ProjectConfig | None = None) -> BlockReason | None:
        """Check agent slot availability against max_concurrent."""
        try:
            alive_count = await self._get_alive_count()
            if cfg is None:
                cfg = load_config(self._project_dir)
            max_concurrent = cfg.max_parallel_agents
            if alive_count >= max_concurrent:
                return BlockReason(
                    gate="slots",
                    detail=f"All agent slots occupied ({alive_count}/{max_concurrent})",
                )
        except Exception:
            log.debug("slot_gate.check_failed", exc_info=True)

        return None

    def _check_github_rate_limit_gate(self) -> BlockReason | None:
        """Check if GitHub API rate limit is exhausted. Fail-open."""
        try:
            from sova.supervisor.github_quota import get_github_quota_tracker

            tracker = get_github_quota_tracker(self._adapter.github_user)
            if tracker.should_skip():
                status = tracker.get_status()
                return BlockReason(
                    gate="rate_limit",
                    detail=f"GitHub API rate limited (cooldown: {status.cooldown_remaining_seconds:.0f}s remaining)",
                )
        except Exception:
            log.debug("rate_limit_gate.check_failed", exc_info=True)
        return None

    def _check_memory_pressure_gate(self, cfg: ProjectConfig | None = None) -> BlockReason | None:
        """Check system memory pressure. Fail-open if psutil is unavailable."""
        if not _PSUTIL_AVAILABLE:
            return None

        try:
            if cfg is None:
                cfg = load_config(self._project_dir)
            mem = psutil.virtual_memory()
            available_gb = mem.available / (1024**3)
            block_threshold = cfg.resources.memory_block_threshold_gb
            warn_threshold = cfg.resources.memory_warn_threshold_gb

            if available_gb < block_threshold:
                return BlockReason(
                    gate="memory",
                    detail=(
                        f"System memory pressure: {available_gb:.2f} GB available < {block_threshold:.2f} GB threshold"
                    ),
                )
            if available_gb < warn_threshold:
                log.warning(
                    "memory_pressure.warn",
                    available_gb=round(available_gb, 2),
                    warn_threshold_gb=warn_threshold,
                )
        except Exception:
            log.debug("memory_gate.check_failed", exc_info=True)

        return None

    async def _check_budget_gate(self, issue: int) -> BlockReason | None:
        """Check per-issue budget limit."""
        try:
            error = await _check_issue_budget(str(issue), self._project_dir)
            if error:
                return BlockReason(gate="budget", detail=error["error"])
        except Exception:
            log.debug("budget_gate.check_failed", issue=issue, exc_info=True)

        return None

    async def _check_already_running(self, issue: int) -> BlockReason | None:
        """Check if an agent is already running or being started for this issue.

        awaiting_approval runs (spec pending human review) block unconditionally: the researcher
        completed its work and the issue should not be re-researched even though the process exited.
        """
        try:
            async with self._session_factory() as session:
                stmt = select(TaskRun).where(
                    TaskRun.issue_number == str(issue),
                    TaskRun.status.notin_(_ALREADY_RUNNING_TERMINAL),
                )
                result = await session.execute(stmt)
                runs = result.scalars().all()
                for run in runs:
                    if run.status == "awaiting_approval":
                        return BlockReason(
                            gate="already_running",
                            detail=f"Spec awaiting human approval for #{issue} (run {run.id})",
                        )
                    if run.pid is None:
                        return BlockReason(
                            gate="already_running",
                            detail=f"Agent being started for #{issue} (run {run.id}, PID not yet assigned)",
                        )
                    if _is_process_alive(run.pid):
                        return BlockReason(
                            gate="already_running",
                            detail=f"Agent already running for #{issue} (run {run.id}, PID {run.pid})",
                        )
        except Exception:
            log.debug("already_running.check_failed", issue=issue, exc_info=True)

        return None

    async def _check_repeated_failures_gate(self, issue: int) -> BlockReason | None:
        """Block researcher spawn after too many failures since the last success. Fail-open."""
        max_failures = self._config.max_researcher_failures
        if max_failures == 0:
            return None
        try:
            async with self._session_factory() as session:
                # Only count failures after the most recent successful researcher run so
                # that issues which were successfully researched and re-triaged can be
                # researched again without being blocked by stale failure history.
                last_success_subq = (
                    select(func.coalesce(func.max(TaskRun.id), 0))
                    .where(
                        TaskRun.issue_number == str(issue),
                        TaskRun.role == "researcher",
                        TaskRun.status == "done",
                    )
                    .scalar_subquery()
                )
                stmt = (
                    select(func.count())
                    .select_from(TaskRun)
                    .where(
                        TaskRun.issue_number == str(issue),
                        TaskRun.role == "researcher",
                        TaskRun.status == "failed",
                        TaskRun.id > last_success_subq,
                    )
                )
                result = await session.execute(stmt)
                count = result.scalar_one_or_none() or 0
                if count >= max_failures:
                    return BlockReason(
                        gate="repeated_failure",
                        detail=(
                            f"Researcher has failed {count} times for #{issue}; "
                            f"human review required (threshold: {max_failures})"
                        ),
                    )
        except Exception:
            log.debug("repeated_failures.check_failed", issue=issue, exc_info=True)

        return None

    def _check_merge_conflict_gate(self, issue_number: int, mergeability_map: dict[int, str]) -> BlockReason | None:
        """Check if the PR for this issue has merge conflicts. Fail-open."""
        status = mergeability_map.get(issue_number)
        if status == "CONFLICTING":
            return BlockReason(
                gate="conflict",
                detail=f"PR for #{issue_number} has merge conflicts with base branch",
            )
        return None

    def _check_file_overlap_gate(
        self,
        issue_number: int,
        active_file_sets: list[BranchFileSet],
        labels: list[str],
        body: str,
    ) -> BlockReason | None:
        """Check if candidate task's predicted files overlap with in-flight branches."""
        try:
            candidate_files = predict_candidate_files(labels, body)
            if not candidate_files:
                return None

            filtered = [fs for fs in active_file_sets if fs.issue_number != str(issue_number)]
            overlaps = check_file_overlap(candidate_files, filtered, threshold=self._config.file_overlap_threshold)
            if not overlaps:
                return None

            details = []
            for o in overlaps:
                sample = sorted(o.overlapping_files)[:3]
                files_str = ", ".join(sample)
                if len(o.overlapping_files) > 3:
                    files_str += f" (+{len(o.overlapping_files) - 3} more)"
                issue_ref = f"#{o.conflicting_issue}" if o.conflicting_issue else o.conflicting_branch
                details.append(f"overlaps with {issue_ref} on {files_str}")

            return BlockReason(
                gate="file_overlap",
                detail="; ".join(details),
            )
        except Exception:
            log.debug("file_overlap_gate.check_failed", issue=issue_number, exc_info=True)
            return None

    async def _check_ownership_gate(
        self,
        issue: int,
        candidate: ProgressionAction,
        *,
        task_assignees: list[str] | None = None,
    ) -> tuple[BlockReason | None, int | None]:
        """Check if issue/PR is owned by the configured github_user.

        For development actions (SPAWN_DEVELOPER, SPAWN_RESEARCHER), checks issue assignee.
        For review actions (SPAWN_INTEGRATE), checks PR author instead (handles teammate takeover).
        Fail-open on API errors (log warning and proceed).

        ``task_assignees`` avoids a redundant API call when the caller already has the task data.

        Returns (block_reason, discovered_pr_number). The PR number is populated when
        the gate checks a PR (SPAWN_INTEGRATE) and can be reused by execute_decision
        to avoid a duplicate API call.
        """
        if not self._config.respect_ownership:
            return None, None

        if not self._adapter.github_user:
            log.warning("ownership_gate.github_user_not_configured", issue=issue)
            return None, None

        try:
            # For review/integrate phases, check PR author (PR ownership is authoritative post-development)
            if candidate == ProgressionAction.SPAWN_INTEGRATE:
                pr_result, pr_number = await self._check_pr_ownership(issue)
                if pr_result is not _NOT_COMPUTED:
                    return pr_result, pr_number

            # Development phase or fallback: check issue assignee
            block = await self._check_issue_ownership(issue, task_assignees)
            return block, None

        except Exception:
            log.warning("ownership_gate.check_failed", issue=issue, exc_info=True)
            return None, None  # Fail-open

    async def _check_pr_ownership(self, issue: int) -> tuple[BlockReason | None | object, int | None]:
        """Check PR author ownership.

        Returns (gate_result, pr_number) where gate_result is:
        BlockReason if blocked, None if authorized,
        _NOT_COMPUTED if should fall through.
        """
        if not self._adapter.repo:
            return _NOT_COMPUTED, None

        pr = await find_pr_for_issue(str(issue), repo=self._adapter.repo, github_user=self._adapter.github_user)
        if not pr or not pr.author_login:
            return _NOT_COMPUTED, pr.number if pr else None

        # PR found with author: this is decisive
        if pr.author_login != self._adapter.github_user:
            return BlockReason(
                gate="ownership",
                detail=f"PR #{pr.number} is owned by {pr.author_login} (not {self._adapter.github_user})",
            ), pr.number
        return None, pr.number  # Authorized

    async def _check_issue_ownership(self, issue: int, task_assignees: list[str] | None) -> BlockReason | None:
        """Check issue assignee ownership. Returns None if unassigned or assigned to self."""
        assignees = task_assignees if task_assignees is not None else []
        if task_assignees is None:
            # Fallback: fetch from API (single-task evaluation without precomputed data)
            task = await self._adapter.get_task(str(issue))
            assignees = task.assignees

        if not assignees or self._adapter.github_user in assignees:
            return None

        # Assigned to someone else
        assignee_str = ", ".join(assignees)
        return BlockReason(
            gate="ownership",
            detail=f"Issue #{issue} is assigned to {assignee_str} (not {self._adapter.github_user})",
        )

    async def _find_pr_for_issue(self, issue: int) -> int | None:
        """Find an open PR linked to this issue."""
        try:
            if not self._adapter.repo:
                return None
            pr = await find_pr_for_issue(str(issue), repo=self._adapter.repo, github_user=self._adapter.github_user)
            return pr.number if pr else None
        except Exception:
            log.debug("find_pr.failed", issue=issue, exc_info=True)
            return None

    async def _refine_in_review_action(self, issue: int) -> tuple[ProgressionAction, int | None]:
        """Refine the IN_REVIEW placeholder into a specific action based on SOVA verdict.

        Returns (action, pr_number). Checks the SOVA review verdict to decide
        between SPAWN_ADDRESS_REVIEW (verdict is revise/block) and SPAWN_INTEGRATE
        (verdict is approve or no review exists).
        """
        pr_number = await self._find_pr_for_issue(issue)
        if pr_number is None:
            return ProgressionAction.WAIT, None

        try:
            verdict_data = await get_sova_review_verdict(str(issue), pr_number=pr_number, project_dir=self._project_dir)
        except Exception:
            log.debug("refine_in_review.verdict_failed", issue=issue, exc_info=True)
            if self._config.auto_integrate:
                return ProgressionAction.SPAWN_INTEGRATE, pr_number
            return ProgressionAction.CHECKPOINT_NEEDED, pr_number

        verdict = verdict_data.get("verdict")
        has_review = verdict_data.get("has_sova_review", False)

        if has_review and verdict in ("revise", "block"):
            if self._config.auto_address_review:
                return ProgressionAction.SPAWN_ADDRESS_REVIEW, pr_number
            return ProgressionAction.CHECKPOINT_NEEDED, pr_number

        if self._config.auto_integrate:
            return ProgressionAction.SPAWN_INTEGRATE, pr_number
        return ProgressionAction.CHECKPOINT_NEEDED, pr_number

    async def _check_address_review_circuit_breaker_gate(
        self, issue: int, *, pr_number: int | None
    ) -> BlockReason | None:
        """Block SPAWN_ADDRESS_REVIEW when the address-review cycle limit is reached."""
        if pr_number is None:
            return None

        try:
            cfg = load_config(self._project_dir)
            max_cycles = cfg.pipeline.max_address_review_cycles
            if max_cycles <= 0:
                return None

            count = await _count_address_review_runs(str(issue), pr_number, self._project_dir)
            if count >= max_cycles:
                return BlockReason(
                    gate="circuit_breaker",
                    detail=f"Address-review circuit breaker: {count}/{max_cycles} cycles completed for PR #{pr_number}",
                )
        except Exception:
            log.debug("circuit_breaker_gate.check_failed", issue=issue, exc_info=True)

        return None
