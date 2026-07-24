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
from sova.dashboard.services.agent_recovery import _is_process_alive
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


# Map ProgressionAction to the role string used by start_agent()
_ACTION_TO_ROLE: dict[ProgressionAction, str] = {
    ProgressionAction.SPAWN_RESEARCHER: "researcher",
    ProgressionAction.SPAWN_DEVELOPER: "developer",
    ProgressionAction.SPAWN_INTEGRATE: "command:integrate-pr",
}


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

    async def evaluate_all(self) -> list[ProgressionDecision]:
        """Scan all active tasks, return next action for each."""
        try:
            graph = await build_dependency_graph(self._adapter)
        except Exception:
            log.warning("evaluate_all.graph_build_failed", exc_info=True)
            return []

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

        tasks = [graph.get_task(nid) for nid in graph.nodes]
        decisions: list[ProgressionDecision] = []
        for task in tasks:
            if task is None:
                continue
            issue = int(task.id)

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
                precomputed_quota=effective_quota,
                precomputed_slots=effective_slots,
                precomputed_conflicts=precomputed_conflicts,
                precomputed_file_sets=precomputed_file_sets,
                task_labels=task.labels,
                task_body=task.body,
            )
            decisions.append(decision)

            # Decrement capacity for actionable decisions
            non_actionable = {ProgressionAction.WAIT, ProgressionAction.BLOCKED, ProgressionAction.CHECKPOINT_NEEDED}
            if decision.action not in non_actionable and decision.action != ProgressionAction.SPAWN_REBASE:
                remaining_slots -= 1
                if decision.action == ProgressionAction.SPAWN_DEVELOPER:
                    remaining_quota = False

        return decisions

    async def evaluate_task(self, issue_number: int) -> ProgressionDecision:
        """Evaluate a single task's readiness for progression."""
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
        task = graph.get_task(issue_number)
        if task is not None:
            task_labels = task.labels
            task_body = task.body

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

        from sova.dashboard.services.agent_lifecycle import start_agent

        role = decision.role or _ACTION_TO_ROLE.get(decision.action)
        if role is None:
            return {"error": f"No role mapping for action {decision.action}"}

        kwargs: dict = {"issue": str(decision.issue_number), "role": role}

        # For integrate-pr, we need the PR number
        if decision.action == ProgressionAction.SPAWN_INTEGRATE:
            pr_number = await self._find_pr_for_issue(decision.issue_number)
            if pr_number is None:
                return {"error": f"No open PR found for issue #{decision.issue_number}"}
            kwargs["pr_number"] = pr_number

        log.info(
            "progression.execute",
            issue=decision.issue_number,
            action=decision.action.value,
            role=role,
        )
        return await start_agent(**kwargs)

    async def execute_decisions(self, decisions: list[ProgressionDecision]) -> list[dict]:
        """Execute all actionable decisions (filters out WAIT/BLOCKED/CHECKPOINT_NEEDED)."""
        _non_actionable = {ProgressionAction.WAIT, ProgressionAction.BLOCKED, ProgressionAction.CHECKPOINT_NEEDED}
        actionable = [d for d in decisions if d.action not in _non_actionable]
        results: list[dict] = []
        for decision in actionable:
            result = await self.execute_decision(decision)
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
        precomputed_quota: BlockReason | None | object = _NOT_COMPUTED,
        precomputed_slots: BlockReason | None | object = _NOT_COMPUTED,
        precomputed_conflicts: dict[int, str] | None = None,
        precomputed_file_sets: list[BranchFileSet] | None = None,
        task_labels: list[str] | None = None,
        task_body: str = "",
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

        blockers = await self._collect_gate_blockers(
            issue_number,
            candidate,
            graph,
            precomputed_memory=precomputed_memory,
            precomputed_quota=precomputed_quota,
            precomputed_slots=precomputed_slots,
            precomputed_conflicts=precomputed_conflicts,
            precomputed_file_sets=precomputed_file_sets,
            task_labels=task_labels,
            task_body=task_body,
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

        return ProgressionDecision(
            issue_number=issue_number,
            action=candidate,
            role=_ACTION_TO_ROLE.get(candidate),
            reason=f"Ready to {candidate.value}",
        )

    async def _collect_gate_blockers(
        self,
        issue_number: int,
        candidate: ProgressionAction,
        graph: DependencyGraph,
        *,
        precomputed_memory: BlockReason | None | object = _NOT_COMPUTED,
        precomputed_quota: BlockReason | None | object = _NOT_COMPUTED,
        precomputed_slots: BlockReason | None | object = _NOT_COMPUTED,
        precomputed_conflicts: dict[int, str] | None = None,
        precomputed_file_sets: list[BranchFileSet] | None = None,
        task_labels: list[str] | None = None,
        task_body: str = "",
    ) -> list[BlockReason]:
        """Run all gate checks and return active blockers."""
        blockers: list[BlockReason] = []

        # Dependency gate is sync (reads in-memory graph only)
        if self._config.respect_dependencies:
            dep_block = self._check_dependency_gate(issue_number, graph)
            if dep_block:
                blockers.append(dep_block)

        # Run async per-task gates concurrently
        gates = [
            self._check_already_running(issue_number),
            self._check_budget_gate(issue_number),
        ]
        if candidate == ProgressionAction.SPAWN_RESEARCHER:
            gates.append(self._check_repeated_failures_gate(issue_number))
        gate_results = await asyncio.gather(*gates)
        blockers.extend(r for r in gate_results if r is not None)

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

        # Add precomputed global gates (or compute on demand for single-task eval)
        global_blocks = await self._resolve_global_gates(
            candidate,
            precomputed_quota=precomputed_quota,
            precomputed_slots=precomputed_slots,
            precomputed_memory=precomputed_memory,
        )
        blockers.extend(global_blocks)

        return blockers

    async def _resolve_global_gates(
        self,
        candidate: ProgressionAction,
        *,
        precomputed_quota: BlockReason | None | object,
        precomputed_slots: BlockReason | None | object,
        precomputed_memory: BlockReason | None | object,
    ) -> list[BlockReason]:
        """Resolve precomputed-or-on-demand global gates and return active blockers."""
        blocks: list[BlockReason] = []

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
            # auto_address_review is reserved for future supervisor-level address-review control.
            # Intra-pipeline chaining (develop -> review -> address) is handled by the handoff
            # system, not the progression engine. The supervisor only handles the inter-role
            # transition: IN_REVIEW -> integrate (once the PR is approved by a human).
            return (
                ProgressionAction.SPAWN_INTEGRATE
                if self._config.auto_integrate
                else ProgressionAction.CHECKPOINT_NEEDED
            )
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

    async def _find_pr_for_issue(self, issue: int) -> int | None:
        """Find an open PR linked to this issue."""
        try:
            cfg = load_config(self._project_dir)
            if not cfg.github_repo:
                return None
            pr = await find_pr_for_issue(str(issue), repo=cfg.github_repo, github_user=cfg.github_user)
            return pr.number if pr else None
        except Exception:
            log.debug("find_pr.failed", issue=issue, exc_info=True)
            return None
