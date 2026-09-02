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

from sqlalchemy.ext.asyncio import async_sessionmaker

from sova.adapters.base import TaskAdapter, TaskState
from sova.config.loader import load_config
from sova.config.models import SupervisorConfig
from sova.core.state import TASK_RUN_TERMINAL
from sova.dashboard.services.agent_recovery import get_sova_review_verdict
from sova.git.pr import PRInfo
from sova.supervisor.dependency_graph import (
    DependencyGraph,
    build_dependency_graph,
    invalidate_graph_cache,
    is_epic,
)
from sova.supervisor.file_overlap import (
    BranchFileSet,
    get_active_branch_file_sets,
)
from sova.supervisor.gates import BlockReason
from sova.supervisor.gates.already_running import check_already_running
from sova.supervisor.gates.budget import check_budget_gate
from sova.supervisor.gates.ci_budget import check_ci_budget_gate
from sova.supervisor.gates.circuit_breaker import check_address_review_circuit_breaker_gate
from sova.supervisor.gates.dependency import check_dependency_gate
from sova.supervisor.gates.file_conflict import check_file_overlap_gate
from sova.supervisor.gates.human_involvement import check_human_involvement_gate
from sova.supervisor.gates.memory_pressure import check_memory_pressure_gate
from sova.supervisor.gates.merge_conflict import check_merge_conflict_gate
from sova.supervisor.gates.ownership import check_ownership_gate
from sova.supervisor.gates.quota import check_quota_gate
from sova.supervisor.gates.rate_limit import check_github_rate_limit_gate
from sova.supervisor.gates.repeated_failure import check_repeated_failures_gate
from sova.supervisor.gates.review_completed import check_review_completed_gate
from sova.supervisor.gates.slots import check_slot_gate, get_alive_count
from sova.supervisor.planner import PlanResult
from sova.utils.logging import get_logger

log = get_logger(component="supervisor.progression")

_NOT_COMPUTED = object()  # sentinel: distinguish "not precomputed" from "checked, no block"


class ProgressionAction(StrEnum):
    SPAWN_TRIAGE = "spawn_triage"
    SPAWN_RESEARCHER = "spawn_researcher"
    SPAWN_DEVELOPER = "spawn_developer"
    SPAWN_INTEGRATE = "spawn_integrate"
    SPAWN_ADDRESS_REVIEW = "spawn_address_review"
    WAIT = "wait"
    BLOCKED = "blocked"
    SPAWN_REBASE = "spawn_rebase"
    CHECKPOINT_NEEDED = "checkpoint_needed"
    RESET_STALE_STATE = "reset_stale_state"


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
    ProgressionAction.SPAWN_TRIAGE: "triage",
    ProgressionAction.SPAWN_RESEARCHER: "researcher",
    ProgressionAction.SPAWN_DEVELOPER: "developer",
    ProgressionAction.SPAWN_INTEGRATE: "command:integrate-pr",
    ProgressionAction.SPAWN_ADDRESS_REVIEW: "developer",
}

# Actions that should trigger issue assignment on spawn (development work, not post-work).
_ASSIGN_ACTIONS = frozenset(
    {
        ProgressionAction.SPAWN_TRIAGE,
        ProgressionAction.SPAWN_RESEARCHER,
        ProgressionAction.SPAWN_DEVELOPER,
    }
)

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
        self._repo_cache_key: str = getattr(adapter, "repo", "") or getattr(adapter, "project_key", "") or ""

    async def _fetch_mergeability_map(self) -> dict:
        """Fetch merge conflict state for all open PRs (fail-open)."""
        try:
            from sova.dashboard.services.pr_service import get_pr_mergeability_map

            return await get_pr_mergeability_map()
        except Exception:
            log.debug("evaluate_all.mergeability_fetch_failed", exc_info=True)
            return {}

    async def _fetch_file_overlap_sets(self) -> list[BranchFileSet] | None:
        """Fetch active branch file sets for the file overlap gate (fail-open)."""
        if not self._config.file_overlap_gate:
            return None
        try:
            return await get_active_branch_file_sets(
                self._session_factory,
                self._project_dir,
            )
        except Exception:
            log.debug("evaluate_all.file_overlap_fetch_failed", exc_info=True)
            return None

    def _resolve_task_ids(self, graph: DependencyGraph) -> list[int]:
        """Resolve which task IDs to evaluate, respecting task_queue order.

        Empty queue returns nothing: the queue is an exclusive filter.
        """
        task_queue = self._config.task_queue
        if not task_queue:
            return []

        node_set = set(graph.nodes)
        task_ids: list[int] = []
        skipped: list[int] = []
        for qid in task_queue:
            (task_ids if qid in node_set else skipped).append(qid)
        if skipped:
            log.warning("evaluate_all.queue_items_not_in_graph", skipped=skipped)
        return task_ids

    @staticmethod
    def _effective_gate(
        global_blocker: BlockReason | None,
        exhausted: bool,
        gate: str,
        detail_prefix: str,
    ) -> BlockReason | None:
        """Return global blocker, or a batch-exhaustion blocker if capacity is spent."""
        if global_blocker is not None:
            return global_blocker
        if exhausted:
            return BlockReason(gate=gate, detail=f"{detail_prefix} ({gate} capacity exhausted)")
        return None

    @staticmethod
    def _update_remaining_capacity(
        decision: ProgressionDecision,
        remaining_slots: int,
        remaining_quota: bool,
    ) -> tuple[int, bool]:
        """Decrement capacity counters for actionable decisions."""
        if decision.action in NON_ACTIONABLE_ACTIONS or decision.action in (
            ProgressionAction.SPAWN_REBASE,
            ProgressionAction.RESET_STALE_STATE,
        ):
            return remaining_slots, remaining_quota
        remaining_slots -= 1
        if decision.action == ProgressionAction.SPAWN_DEVELOPER:
            remaining_quota = False
        return remaining_slots, remaining_quota

    async def evaluate_all(self, *, plan: PlanResult | None = None) -> list[ProgressionDecision]:
        """Scan all active tasks, return next action for each.

        When *plan* is provided, actionable decisions whose ``(action, issue)``
        pair is not in the plan's approved list are converted to WAIT.
        Deterministic gates still hard-block regardless of the plan.
        """
        global_rate_limit = check_github_rate_limit_gate(self._adapter.github_user)
        if global_rate_limit is not None:
            log.info("evaluate_all.skipped_rate_limited")
            return []

        try:
            graph = await build_dependency_graph(self._adapter)
        except Exception:
            log.warning("evaluate_all.graph_build_failed", exc_info=True)
            return []

        self._last_graph = graph
        cfg = load_config(self._project_dir)

        global_memory = check_memory_pressure_gate(cfg.memory_guard)
        global_quota = await check_quota_gate(
            is_developer=True, quota_config=cfg.coderabbit_quota, session_factory=self._session_factory
        )
        global_ci_budget = await check_ci_budget_gate(
            is_developer=True,
            github_user=self._adapter.github_user,
            github_repo=cfg.github_repo,
            ci_block_minutes=cfg.supervisor.ci_block_minutes,
        )
        precomputed_conflicts = await self._fetch_mergeability_map()
        precomputed_file_sets = await self._fetch_file_overlap_sets()

        alive_count = await get_alive_count(self._session_factory)
        global_slots: BlockReason | None = None
        if alive_count >= cfg.max_parallel_agents:
            global_slots = BlockReason(
                gate="slots",
                detail=f"All agent slots occupied ({alive_count}/{cfg.max_parallel_agents})",
            )
        remaining_slots = cfg.max_parallel_agents - alive_count
        remaining_quota = not bool(global_quota)

        task_ids = self._resolve_task_ids(graph)
        tasks = [graph.get_task(nid) for nid in task_ids]
        decisions: list[ProgressionDecision] = []
        for task in tasks:
            if task is None:
                continue

            mid_loop_rate_limit = check_github_rate_limit_gate(self._adapter.github_user)
            if mid_loop_rate_limit is not None:
                log.info("evaluate_all.mid_loop_rate_limited", issue=int(task.id))
                break

            effective_slots = self._effective_gate(
                global_slots,
                remaining_slots <= 0,
                "slots",
                "All agent slots would be occupied",
            )
            effective_quota = self._effective_gate(
                global_quota,
                not remaining_quota,
                "quota",
                "CodeRabbit quota would be exhausted",
            )

            decision = await self._evaluate_single(
                int(task.id),
                task.state,
                graph,
                precomputed_memory=global_memory,
                precomputed_rate_limit=global_rate_limit,
                precomputed_quota=effective_quota,
                precomputed_slots=effective_slots,
                precomputed_ci_budget=global_ci_budget,
                precomputed_conflicts=precomputed_conflicts,
                precomputed_file_sets=precomputed_file_sets,
                task_labels=task.labels,
                task_body=task.body,
                task_assignees=task.assignees,
            )
            decisions.append(decision)

            remaining_slots, remaining_quota = self._update_remaining_capacity(
                decision,
                remaining_slots,
                remaining_quota,
            )

        if plan is not None:
            approved_set = {(a.action, a.issue) for a in plan.actions}
            filtered: list[ProgressionDecision] = []
            for d in decisions:
                if d.action in NON_ACTIONABLE_ACTIONS:
                    filtered.append(d)
                elif (d.action.value, d.issue_number) in approved_set:
                    filtered.append(d)
                else:
                    log.info(
                        "progression.plan_filtered",
                        issue=d.issue_number,
                        action=d.action.value,
                        detail="not in LLM plan; skipped",
                    )
                    filtered.append(
                        ProgressionDecision(
                            issue_number=d.issue_number,
                            action=ProgressionAction.WAIT,
                            reason=f"not in LLM plan (deterministic: {d.action.value})",
                            blocked_by=d.blocked_by,
                            pr_number=d.pr_number,
                        )
                    )
            decisions = filtered

        return decisions

    async def evaluate_task(self, issue_number: int) -> ProgressionDecision:
        """Evaluate a single task's readiness for progression."""
        rate_limit_block = check_github_rate_limit_gate(self._adapter.github_user)
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

        precomputed_conflicts = await self._fetch_mergeability_map()

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

        if decision.action == ProgressionAction.RESET_STALE_STATE:
            return await self._execute_stale_reset(decision.issue_number)

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

        if self._project_dir is not None:
            slug = find_slug_for_path(self._project_dir)
            if slug is None:
                return {"error": f"Project path is not registered: {self._project_dir}"}
            kwargs["slug"] = slug

        if decision.action in (ProgressionAction.SPAWN_INTEGRATE, ProgressionAction.SPAWN_ADDRESS_REVIEW):
            pr_info = await self._find_pr_for_issue(decision.issue_number) if not decision.pr_number else None
            pr_number = decision.pr_number or (pr_info.number if pr_info else None)
            if pr_number is None:
                return {"error": f"No open PR found for issue #{decision.issue_number}"}
            kwargs["pr_number"] = pr_number

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
        result = await start_agent(**kwargs)
        if "error" not in result:
            invalidate_graph_cache(self._repo_cache_key)
        return result

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

    async def _execute_stale_reset(self, issue_number: int) -> dict:
        """Execute a RESET_STALE_STATE decision: roll back the issue to the appropriate prior state."""
        from sqlalchemy import select

        from sova.dashboard.services.feed_service import FeedEventSeverity, emit_safe
        from sova.db.models import TaskRun

        try:
            async with self._session_factory() as session:
                stmt = (
                    select(TaskRun)
                    .where(
                        TaskRun.issue_number == str(issue_number),
                        TaskRun.status.in_(TASK_RUN_TERMINAL),
                    )
                    .order_by(TaskRun.id.desc())
                    .limit(1)
                )
                result = await session.execute(stmt)
                last_run = result.scalar_one_or_none()

            if last_run is not None:
                from sova.dashboard.services.agent_recovery import rollback_issue_state

                await rollback_issue_state(last_run.id, self._project_dir)
                target_desc = f"run {last_run.id} (role: {last_run.role or 'unknown'})"
            else:
                alive_block = await check_already_running(issue_number, self._session_factory)
                if alive_block:
                    log.info("stale_reset.skipped_agent_alive", issue=issue_number, detail=alive_block.detail)
                    return {"skipped": True, "issue": issue_number, "reason": alive_block.detail}
                await self._adapter.transition_state(str(issue_number), TaskState.TRIAGED)
                target_desc = "TRIAGED (no prior run)"

            invalidate_graph_cache(self._repo_cache_key)
            emit_safe(
                f"Stale IN_PROGRESS reset: #{issue_number}",
                severity=FeedEventSeverity.warning,
                detail=f"No agent alive, rolled back via {target_desc}",
                category="supervisor",
                metadata={"issue": issue_number},
            )
            log.info("stale_reset.completed", issue=issue_number, via=target_desc)
            return {"reset": True, "issue": issue_number}
        except Exception:
            log.warning("stale_reset.failed", issue=issue_number, exc_info=True)
            return {"error": f"Failed to reset stale state for #{issue_number}"}

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
            invalidate_graph_cache(self._repo_cache_key)
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

        if graph is None:
            graph = self._last_graph
        if graph is None:
            if check_github_rate_limit_gate(self._adapter.github_user) is not None:
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
        precomputed_ci_budget: BlockReason | None | object = _NOT_COMPUTED,
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

        if candidate == ProgressionAction.CHECKPOINT_NEEDED:
            return ProgressionDecision(
                issue_number=issue_number,
                action=ProgressionAction.CHECKPOINT_NEEDED,
                reason=f"Automation disabled for state '{state.value}': requires human approval",
            )

        if candidate == ProgressionAction.RESET_STALE_STATE:
            alive_block = await check_already_running(issue_number, self._session_factory)
            if alive_block:
                return ProgressionDecision(
                    issue_number=issue_number,
                    action=ProgressionAction.WAIT,
                    reason=f"Agent still active for IN_PROGRESS #{issue_number}: {alive_block.detail}",
                )
            if precomputed_rate_limit is _NOT_COMPUTED:
                rate_limit_block = check_github_rate_limit_gate(self._adapter.github_user)
            else:
                rate_limit_block = precomputed_rate_limit
            if rate_limit_block:
                log.info(
                    "evaluate_single.blocked",
                    issue=issue_number,
                    candidate=candidate.value,
                    gates=["rate_limit"],
                )
                return ProgressionDecision(
                    issue_number=issue_number,
                    action=ProgressionAction.BLOCKED,
                    reason=f"Rate limited: {rate_limit_block.detail}",
                    blocked_by=(rate_limit_block,),
                )
            log.info(
                "evaluate_single.ready",
                issue=issue_number,
                action=ProgressionAction.RESET_STALE_STATE.value,
                pr_number=None,
            )
            return ProgressionDecision(
                issue_number=issue_number,
                action=ProgressionAction.RESET_STALE_STATE,
                reason=f"Stale IN_PROGRESS: no agent running for #{issue_number}",
            )

        refined_pr_info: PRInfo | None = None
        if state == TaskState.IN_REVIEW and candidate == ProgressionAction.SPAWN_INTEGRATE:
            candidate, refined_pr_info = await self._refine_in_review_action(issue_number)
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
            precomputed_ci_budget=precomputed_ci_budget,
            precomputed_conflicts=precomputed_conflicts,
            precomputed_file_sets=precomputed_file_sets,
            task_labels=task_labels,
            task_body=task_body,
            task_assignees=task_assignees,
            refined_pr_info=refined_pr_info,
        )

        if blockers:
            all_conflict = all(b.gate == "conflict" for b in blockers)
            if all_conflict and self._config.auto_rebase:
                log.info(
                    "evaluate_single.ready",
                    issue=issue_number,
                    action=ProgressionAction.SPAWN_REBASE.value,
                    pr_number=None,
                )
                return ProgressionDecision(
                    issue_number=issue_number,
                    action=ProgressionAction.SPAWN_REBASE,
                    reason="PR has merge conflicts, attempting auto-rebase",
                )

            reasons = "; ".join(b.detail for b in blockers)
            log.info(
                "evaluate_single.blocked",
                issue=issue_number,
                candidate=candidate.value,
                gates=[b.gate for b in blockers],
            )
            return ProgressionDecision(
                issue_number=issue_number,
                action=ProgressionAction.BLOCKED,
                role=_ACTION_TO_ROLE.get(candidate),
                reason=f"Blocked: {reasons}",
                blocked_by=tuple(blockers),
            )

        refined_pr_num = refined_pr_info.number if refined_pr_info else None
        final_pr = refined_pr_num or discovered_pr
        log.info(
            "evaluate_single.ready",
            issue=issue_number,
            action=candidate.value,
            pr_number=final_pr,
        )
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
        precomputed_ci_budget: BlockReason | None | object = _NOT_COMPUTED,
        precomputed_conflicts: dict[int, str] | None = None,
        precomputed_file_sets: list[BranchFileSet] | None = None,
        task_labels: list[str] | None = None,
        task_body: str = "",
        task_assignees: list[str] | None = None,
        refined_pr_info: PRInfo | None = None,
    ) -> tuple[list[BlockReason], int | None]:
        """Run all gate checks and return (active_blockers, discovered_pr_number)."""
        blockers: list[BlockReason] = []

        if precomputed_rate_limit is _NOT_COMPUTED:
            rate_limit_block = check_github_rate_limit_gate(self._adapter.github_user)
        else:
            rate_limit_block = precomputed_rate_limit
        if rate_limit_block:
            blockers.append(rate_limit_block)
            return blockers, None

        if self._config.respect_dependencies:
            dep_block = check_dependency_gate(issue_number, graph)
            if dep_block:
                blockers.append(dep_block)

        effective_labels = task_labels
        if effective_labels is None and graph is not None:
            task_node = graph.get_task(issue_number)
            effective_labels = task_node.labels if task_node else []
        human_block = check_human_involvement_gate(issue_number, effective_labels or [])
        if human_block:
            blockers.append(human_block)

        running_result, budget_result, ownership_result = await asyncio.gather(
            check_already_running(issue_number, self._session_factory),
            check_budget_gate(issue_number, self._project_dir),
            check_ownership_gate(
                issue_number,
                is_integrate=(candidate == ProgressionAction.SPAWN_INTEGRATE),
                respect_ownership=self._config.respect_ownership,
                github_user=self._adapter.github_user,
                repo=getattr(self._adapter, "repo", ""),
                adapter=self._adapter,
                task_assignees=task_assignees,
                known_pr_info=refined_pr_info,
            ),
        )
        ownership_block, discovered_pr = ownership_result

        simple_results: list[BlockReason | None] = [running_result, budget_result, ownership_block]
        if candidate == ProgressionAction.SPAWN_RESEARCHER:
            simple_results.append(
                await check_repeated_failures_gate(
                    issue_number, self._config.max_researcher_failures, self._session_factory
                )
            )
        if candidate == ProgressionAction.SPAWN_DEVELOPER:
            simple_results.append(
                await check_repeated_failures_gate(
                    issue_number, self._config.max_developer_failures, self._session_factory, role="developer"
                )
            )
        if candidate == ProgressionAction.SPAWN_ADDRESS_REVIEW:
            cb_pr = (refined_pr_info.number if refined_pr_info else None) or discovered_pr
            cfg = load_config(self._project_dir)
            cb_block = await check_address_review_circuit_breaker_gate(
                issue_number,
                pr_number=cb_pr,
                max_cycles=cfg.pipeline.max_address_review_cycles,
                project_dir=self._project_dir,
            )
            simple_results.append(cb_block)
        blockers.extend(r for r in simple_results if r is not None)

        if candidate == ProgressionAction.SPAWN_INTEGRATE:
            if precomputed_conflicts is not None:
                conflict_block = check_merge_conflict_gate(issue_number, precomputed_conflicts)
                if conflict_block:
                    blockers.append(conflict_block)

            gate_pr_number = (refined_pr_info.number if refined_pr_info else None) or discovered_pr
            enriched_pr = await self._fetch_enriched_pr(gate_pr_number) if gate_pr_number else None
            review_block = await check_review_completed_gate(
                issue_number,
                labels=effective_labels or [],
                pr_number=gate_pr_number,
                project_dir=self._project_dir,
                pr_data=enriched_pr,
            )
            if review_block:
                blockers.append(review_block)

        if self._config.file_overlap_gate and precomputed_file_sets is not None:
            overlap_block = check_file_overlap_gate(
                issue_number,
                precomputed_file_sets,
                task_labels or [],
                task_body,
                self._config.file_overlap_threshold,
            )
            if overlap_block:
                blockers.append(overlap_block)

        global_blocks = await self._resolve_global_gates(
            candidate,
            precomputed_quota=precomputed_quota,
            precomputed_slots=precomputed_slots,
            precomputed_memory=precomputed_memory,
            precomputed_rate_limit=rate_limit_block,
            precomputed_ci_budget=precomputed_ci_budget,
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
        precomputed_ci_budget: BlockReason | None | object = _NOT_COMPUTED,
    ) -> list[BlockReason]:
        """Resolve precomputed-or-on-demand global gates and return active blockers."""
        blocks: list[BlockReason] = []

        if precomputed_rate_limit is _NOT_COMPUTED:
            rate_limit_block = check_github_rate_limit_gate(self._adapter.github_user)
        else:
            rate_limit_block = precomputed_rate_limit
        if rate_limit_block:
            blocks.append(rate_limit_block)

        needs_config = any(
            v is _NOT_COMPUTED
            for v in (precomputed_quota, precomputed_ci_budget, precomputed_slots, precomputed_memory)
        )
        cfg = load_config(self._project_dir) if needs_config else None

        is_developer = candidate == ProgressionAction.SPAWN_DEVELOPER
        if precomputed_quota is _NOT_COMPUTED:
            quota_block = await check_quota_gate(
                is_developer=is_developer,
                quota_config=cfg.coderabbit_quota,
                session_factory=self._session_factory,
            )
        elif is_developer:
            quota_block = precomputed_quota
        else:
            quota_block = None
        if quota_block:
            blocks.append(quota_block)

        if precomputed_ci_budget is _NOT_COMPUTED:
            ci_budget_block = await check_ci_budget_gate(
                is_developer=is_developer,
                github_user=self._adapter.github_user,
                github_repo=cfg.github_repo,
                ci_block_minutes=cfg.supervisor.ci_block_minutes,
            )
        elif is_developer:
            ci_budget_block = precomputed_ci_budget
        else:
            ci_budget_block = None
        if ci_budget_block:
            blocks.append(ci_budget_block)

        if precomputed_slots is _NOT_COMPUTED:
            slot_block = await check_slot_gate(self._session_factory, cfg.max_parallel_agents)
        else:
            slot_block = precomputed_slots
        if slot_block:
            blocks.append(slot_block)

        if precomputed_memory is _NOT_COMPUTED:
            memory_block = check_memory_pressure_gate(cfg.memory_guard)
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
            if self._config.auto_integrate or self._config.auto_address_review:
                return ProgressionAction.SPAWN_INTEGRATE
            return ProgressionAction.CHECKPOINT_NEEDED
        if state == TaskState.BACKLOG:
            return ProgressionAction.SPAWN_TRIAGE if self._config.auto_triage else ProgressionAction.CHECKPOINT_NEEDED
        if state == TaskState.IN_PROGRESS:
            return ProgressionAction.RESET_STALE_STATE
        return None

    async def _fetch_enriched_pr(self, pr_number: int) -> dict | None:
        """Fetch enriched PR data from the PR service cache for gate checks."""
        try:
            from sova.dashboard.services.pr_service import list_open_prs_with_state

            prs = await list_open_prs_with_state(self._project_dir)
            for pr in prs:
                if pr.get("number") == pr_number:
                    return pr
        except Exception:
            log.debug("fetch_enriched_pr.failed", pr=pr_number, exc_info=True)
        return None

    async def _find_pr_for_issue(self, issue: int) -> PRInfo | None:
        """Find an open PR linked to this issue."""
        try:
            from sova.git.pr import find_pr_for_issue

            if not self._adapter.repo:
                return None
            return await find_pr_for_issue(str(issue), repo=self._adapter.repo, github_user=self._adapter.github_user)
        except Exception:
            log.debug("find_pr.failed", issue=issue, exc_info=True)
            return None

    async def _refine_in_review_action(self, issue: int) -> tuple[ProgressionAction, PRInfo | None]:
        """Refine the IN_REVIEW placeholder into a specific action based on SOVA verdict.

        Returns (action, pr_info). Only integrates when a SOVA review explicitly
        approved. Revise/block triggers address-review. All other cases (no review,
        post_failed, exception) return CHECKPOINT_NEEDED.
        """
        pr_info = await self._find_pr_for_issue(issue)
        if pr_info is None:
            return ProgressionAction.WAIT, None

        try:
            verdict_data = await get_sova_review_verdict(
                str(issue), pr_number=pr_info.number, project_dir=self._project_dir
            )
        except Exception:
            log.debug("refine_in_review.verdict_failed", issue=issue, exc_info=True)
            return ProgressionAction.CHECKPOINT_NEEDED, pr_info

        verdict = verdict_data.get("verdict")
        has_review = verdict_data.get("has_sova_review", False)

        if has_review and verdict in ("revise", "block"):
            if self._config.auto_address_review:
                return ProgressionAction.SPAWN_ADDRESS_REVIEW, pr_info
            return ProgressionAction.CHECKPOINT_NEEDED, pr_info

        if has_review and verdict == "approve" and self._config.auto_integrate:
            return ProgressionAction.SPAWN_INTEGRATE, pr_info
        return ProgressionAction.CHECKPOINT_NEEDED, pr_info
