"""Research lookahead gate: blocks a researcher spawn while enough work already waits ahead of development."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from sova.adapters.base import Task, TaskAdapter, TaskState
from sova.config.models import ProjectConfig
from sova.core.state import TASK_RUN_TERMINAL
from sova.db.models import TaskRun
from sova.supervisor.dependency_graph import DependencyGraph
from sova.supervisor.gates import BlockReason
from sova.supervisor.gates.dependency import check_dependency_gate
from sova.supervisor.gates.human_involvement import check_human_involvement_gate
from sova.supervisor.gates.ownership import check_ownership_gate
from sova.supervisor.gates.repeated_failure import check_repeated_failures_gate
from sova.utils.logging import get_logger
from sova.utils.process import is_process_alive

log = get_logger(component="supervisor.gates.research_lookahead")

RESEARCHED = "researched"
RESEARCHING = "researching"


@dataclass(frozen=True, slots=True)
class LookaheadHolder:
    """A queued issue that occupies one place in the research lookahead budget."""

    issue: int
    kind: str  # RESEARCHED or RESEARCHING


async def _issues_with_live_researcher(issues: list[int], session_factory: async_sessionmaker) -> set[int]:
    """Issues in ``issues`` with a non-terminal researcher run that is PID-less or has an alive PID."""
    async with session_factory() as session:
        stmt = select(TaskRun).where(
            TaskRun.role == "researcher",
            TaskRun.issue_number.in_([str(i) for i in issues]),
            TaskRun.status.notin_(TASK_RUN_TERMINAL),
        )
        runs = (await session.execute(stmt)).scalars().all()
    return {int(run.issue_number) for run in runs if run.pid is None or is_process_alive(run.pid)}


async def _is_held(
    task: Task,
    graph: DependencyGraph,
    config: ProjectConfig,
    session_factory: async_sessionmaker,
    adapter: TaskAdapter,
) -> bool:
    """True when the dependency, human-involvement, ownership or developer failure gate would hold this issue."""
    issue = int(task.id)
    sup = config.supervisor
    if sup.respect_dependencies and check_dependency_gate(issue, graph) is not None:
        return True
    human_block = await check_human_involvement_gate(
        issue,
        task.labels,
        session_factory=session_factory,
        project_slug=config.github_repo,
        candidate="spawn_developer",
    )
    if human_block is not None:
        return True
    # Fails open like the engine's own check: an unevaluable owner leaves the issue counted.
    ownership_block, _ = await check_ownership_gate(
        issue,
        is_integrate=False,
        respect_ownership=sup.respect_ownership,
        github_user=adapter.github_user,
        repo="",
        adapter=adapter,
        task_assignees=task.assignees,
    )
    if ownership_block is not None:
        return True
    failure_block = await check_repeated_failures_gate(
        issue, sup.max_developer_failures, session_factory, role="developer"
    )
    return failure_block is not None


async def collect_lookahead_holders(
    graph: DependencyGraph,
    queue: list[int],
    config: ProjectConfig,
    session_factory: async_sessionmaker,
    adapter: TaskAdapter,
) -> list[LookaheadHolder] | None:
    """List the queued issues sitting ahead of development, in queue order, each issue once.

    An issue holds a place when it is RESEARCHED, or TRIAGED with a live researcher run, unless a
    gate would hold it back from development anyway (otherwise it would hold the budget forever).

    Returns ``None`` when the holders cannot be determined, so callers pick their own fail-open
    policy instead of reading a failure as an empty queue.
    """
    try:
        queue = list(dict.fromkeys(queue))  # the queue accepts duplicates; count each issue once
        triaged = [i for i in queue if (t := graph.get_task(i)) is not None and t.state == TaskState.TRIAGED]
        researching = await _issues_with_live_researcher(triaged, session_factory) if triaged else set()

        candidates: list[tuple[LookaheadHolder, Task]] = []
        for issue in queue:
            task = graph.get_task(issue)
            if task is None:
                continue
            if task.state == TaskState.RESEARCHED:
                kind = RESEARCHED
            elif task.state == TaskState.TRIAGED and issue in researching:
                kind = RESEARCHING
            else:
                continue
            candidates.append((LookaheadHolder(issue=issue, kind=kind), task))

        held = await asyncio.gather(
            *(_is_held(task, graph, config, session_factory, adapter) for _, task in candidates)
        )
        return [holder for (holder, _), is_held in zip(candidates, held, strict=True) if not is_held]
    except Exception:  # noqa: BLE001 (fail-open; an unevaluable lookahead count must not block progression)
        log.warning("research_lookahead.collect_failed", exc_info=True)
        return None


def check_research_lookahead_gate(holders: list[LookaheadHolder] | None, limit: int) -> BlockReason | None:
    """Block once ``limit`` queued issues already wait ahead of development.

    ``limit`` 0 disables the gate. Fails open on ``None`` holders (count unavailable).
    """
    if limit <= 0 or holders is None or len(holders) < limit:
        return None
    named = ", ".join(f"#{h.issue} {h.kind}" for h in holders)
    return BlockReason(
        gate="research_lookahead",
        detail=f"Research lookahead full ({len(holders)}/{limit} issues ahead of development: {named})",
    )
