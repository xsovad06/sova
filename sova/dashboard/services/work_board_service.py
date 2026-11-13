"""Work board service: every issue in exactly one column, assembled from the sources the dashboard already has.

``build_board()`` composes the work items (``get_work_items()``), the "Up next" ranking
(``load_ranking_graph()`` + ``rank_graph_entries()``), the supervisor queue, the active runs and the
dependency graph. The stage of each issue comes from ``work_state.work_stage()``, the one place the stage
rule lives, so the client keeps no stage table. The board reads and prunes; it never adds a queue entry.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from sova.adapters.base import TaskState
from sova.config.db_loader import load_hidden_issues, load_task_queue, remove_from_task_queue
from sova.dashboard.services.work_item_service import (
    _parse_issue_number,
    get_work_items,
    load_ranking_graph,
    next_task_exclusions,
    rank_graph_entries,
)
from sova.dashboard.services.work_state import (
    YOURS_MODES,
    WorkItemState,
    WorkStage,
    state_color,
    state_label,
    work_stage,
)
from sova.git.pr import list_merged_prs_since
from sova.supervisor.labels import is_epic
from sova.supervisor.queue_maintenance import prune_closed
from sova.supervisor.suitability import unblock_count, unsatisfied_dependencies
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.supervisor.dependency_graph import DependencyGraph

log = get_logger(component="dashboard.work_board")

UP_NEXT_LIMIT = 5

# One `gh pr list` per window, not per board refresh: the board reloads on every live-update event.
_MERGED_CACHE_TTL = 60.0
_merged_cache: dict[tuple[str, date], tuple[float, list[dict]]] = {}


def reset_board_cache() -> None:
    """Forget the cached merged-PR lists. Test-only, like ``reset_graph_load_state()``."""
    _merged_cache.clear()


# Issue entries ---------------------------------------------------------------


def _issue_entry(
    number: int | None,
    *,
    title: str,
    url: str,
    stage: WorkStage,
    state: str,
    priority: str,
) -> dict:
    return {
        "number": number,
        "title": title,
        "url": url,
        "stage": stage.value,
        "state": state,
        "state_label": state_label(state) if state else "",
        "state_color": state_color(state) if state else "",
        "priority": priority,
    }


def _item_mode(item: dict) -> str | None:
    return (item.get("suitability") or {}).get("mode")


def _item_run_status(item: dict) -> str | None:
    return (item.get("last_run") or {}).get("status")


def _item_entry(item: dict, stage: WorkStage) -> dict:
    return _issue_entry(
        _parse_issue_number(item.get("issue_number")),
        title=item.get("title", ""),
        url=item.get("url", ""),
        stage=stage,
        state=item.get("state", ""),
        priority=item.get("issue_priority", ""),
    )


def _fallback_entry(number: int, graph: DependencyGraph | None, stage: WorkStage) -> dict:
    """An entry for an issue the work items lack (closed, deleted or outside the open list), never raising."""
    task = graph.get_task(number) if graph is not None else None
    if task is None:
        return _issue_entry(number, title="", url="", stage=stage, state="", priority="")
    return _issue_entry(
        number, title=task.title, url=task.url, stage=stage, state=task.state.value, priority=task.priority.value
    )


def _reason_for_needs(state: str) -> tuple[str, str]:
    """(kind, reason) naming why an item with this state needs the user; a failed run is the remaining case."""
    if state == WorkItemState.PR_READY_TO_MERGE:
        return "merge", "Ready to merge"
    if state == WorkItemState.PR_REVIEW_EXHAUSTED:
        return "review_budget", "Review budget exhausted"
    if state == WorkItemState.SPEC_REVIEW:
        return "spec", "Spec awaiting approval"
    return "failed_run", "Last run failed"


def _reason_for_yours(item: dict) -> dict:
    """The label or local record that put an issue in the user's own work, as the classifier reported it."""
    mode = _item_mode(item) or ""
    if item.get("state") == WorkItemState.HUMAN_ONLY and mode not in YOURS_MODES:
        return {"reason": "human_only", "reason_source": "state"}
    reasons = (item.get("suitability") or {}).get("reasons") or []
    if reasons:
        first = reasons[0]
        return {"reason": str(first.get("value", mode)), "reason_source": str(first.get("source", ""))}
    return {"reason": mode, "reason_source": "suitability"}


def _running_details(item: dict, runs: dict[str, dict]) -> dict:
    agent = item.get("running_agent") or {}
    run = runs.get(f"run:{agent.get('run_id')}") or runs.get(f"issue:{item.get('issue_number')}") or {}
    return {
        "role": agent.get("role") or run.get("role"),
        "step_name": run.get("current_step"),
        "step_index": run.get("step_index"),
        "total_steps": run.get("total_steps"),
        "ci_state": (item.get("pr_details") or {}).get("ci_status", ""),
        "elapsed_seconds": agent.get("elapsed_seconds", run.get("elapsed_seconds")),
        "cost_usd": run.get("total_cost_usd"),
    }


def _review_details(item: dict) -> dict:
    pr = item.get("pr_details") or {}
    return {
        "pr_number": item.get("pr_number"),
        "ci_state": pr.get("ci_status", ""),
        "threads_resolved": pr.get("thread_resolved", 0),
        "threads_total": pr.get("thread_total", 0),
    }


# Sources ---------------------------------------------------------------------


async def _active_runs(project_dir: Path | None) -> dict[str, dict] | None:
    """Non-terminal runs keyed ``run:<id>`` and ``issue:<number>`` (the newest run wins); ``None`` when unavailable.

    ``{}`` means no run is active, so a failed lookup returns ``None`` instead and the board names it in
    ``unavailable`` rather than showing running issues with no details as if that were all there is.
    """
    from sova.dashboard.services.work_service import get_active_work
    from sova.db.session import get_session

    try:
        async with await get_session(project_dir=project_dir) as session, session.begin():
            runs = await get_active_work(session, project_dir)
    except Exception:  # noqa: BLE001 (the board still renders without per-run details)
        log.warning("work_board.active_runs_failed", exc_info=True)
        return None
    indexed: dict[str, dict] = {}
    for run in reversed(runs):  # get_active_work() is newest first, so the newest is written last
        indexed[f"run:{run['id']}"] = run
        indexed[f"issue:{run['issue_number']}"] = run
    return indexed


def _local_midnight(now: datetime) -> datetime:
    """Midnight of ``now``'s day in the server's local time zone, as an aware datetime."""
    local = now.astimezone()
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def _parse_merged_at(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


async def _done_today(project_dir: Path | None, now: datetime) -> list[dict] | None:
    """Issues merged since local midnight, one entry per issue a merged PR links; ``None`` when unavailable.

    An issue two merged PRs link appears once, with its most recent merge; a PR that links no issue keeps its
    own entry. A PR merged at exactly midnight belongs to the new day. The merged list is cached for
    ``_MERGED_CACHE_TTL`` seconds, so a failure is never cached as an empty day.
    """
    from sova.config.loader import load_config
    from sova.dashboard.services.pr_service import extract_all_linked_issues

    midnight = _local_midnight(now)
    since_day = midnight.astimezone(timezone.utc).date()
    try:
        cfg = await asyncio.to_thread(load_config, project_dir)
        if not cfg.github_repo:
            return []
        cache_key = (f"{project_dir}:{cfg.github_repo}", since_day)
        cached = _merged_cache.get(cache_key)
        if cached is not None and time.monotonic() - cached[0] < _MERGED_CACHE_TTL:
            merged = cached[1]
        else:
            merged = await list_merged_prs_since(repo=cfg.github_repo, since=since_day, github_user=cfg.github_user)
            for stale in [key for key in _merged_cache if key[1] != since_day]:
                del _merged_cache[stale]  # an earlier day is never asked for again
            _merged_cache[cache_key] = (time.monotonic(), merged)
    except Exception:  # noqa: BLE001 (done today is optional context; reported as unavailable)
        log.warning("work_board.merged_prs_failed", exc_info=True)
        return None

    entries: list[tuple[datetime, dict]] = []
    for pr in merged:
        merged_at = _parse_merged_at(pr.get("mergedAt"))
        if merged_at is None or merged_at < midnight:
            continue
        for number in extract_all_linked_issues(pr) or [None]:
            entry = _issue_entry(
                number,
                title=pr.get("title", ""),
                url=pr.get("url", ""),
                stage=WorkStage.DONE,
                state=WorkItemState.MERGED.value,
                priority="",
            )
            entry.update({"pr_number": pr.get("number"), "merged_at": merged_at.isoformat()})
            entries.append((merged_at, entry))
    entries.sort(key=lambda pair: pair[0], reverse=True)
    seen: set[int] = set()
    unique: list[dict] = []
    for _at, entry in entries:
        number = entry["number"]
        if number is not None:
            if number in seen:
                continue  # newest first, so the entry already kept is the most recent merge
            seen.add(number)
        unique.append(entry)
    return unique


# Queue pruning ---------------------------------------------------------------


async def _queued_states(project_dir: Path | None, queue: list[int], graph: DependencyGraph) -> dict[int, TaskState]:
    """The tracker state of each queued issue there is evidence for: the graph's, else a lookup.

    A lookup that fails leaves its issue out, so an outage reads as "unknown" and never as "closed".
    """
    states: dict[int, TaskState] = {}
    unknown: list[int] = []
    for issue in queue:
        task = graph.get_task(issue)
        if task is not None:
            states[issue] = task.state
        else:
            unknown.append(issue)
    if not unknown:
        return states

    from sova.adapters import close_adapter, create_adapter
    from sova.config.loader import load_config

    try:
        adapter = create_adapter(await asyncio.to_thread(load_config, project_dir))
    except Exception:  # noqa: BLE001 (no adapter means no evidence and so keeps the entries)
        log.warning("work_board.queue_lookup_adapter_failed", exc_info=True)
        return states
    try:
        results = await asyncio.gather(*(adapter.get_state(str(i)) for i in unknown), return_exceptions=True)
    finally:
        await close_adapter(adapter)  # on every path, a cancelled board request included
    for issue, result in zip(unknown, results, strict=True):
        if isinstance(result, TaskState):
            states[issue] = result
        else:
            log.warning("work_board.queue_lookup_failed", issue=issue, error=repr(result))
    return states


async def _prune_closed_queue(
    project_dir: Path | None, queue: list[int], graph: DependencyGraph
) -> tuple[list[int], bool]:
    """Drop closed issues from the saved queue: the queue as it now stands, and whether the save failed.

    Only an issue the tracker is known to have closed is removed. Saving happens only when something was
    removed (``remove_from_task_queue()``), and nothing is ever added. When the save fails the returned queue
    still leaves the closed issues out (the board hides them) but the flag is ``True``, so the board names
    the unsaved prune in ``unavailable`` instead of presenting it as done.
    """
    if not queue:
        return queue, False
    states = await _queued_states(project_dir, queue, graph)
    known = [issue for issue in queue if issue in states]
    kept = set(prune_closed(known, states))
    closed = [issue for issue in known if issue not in kept]
    if not closed:
        return queue, False
    save_failed = False
    try:
        await remove_from_task_queue(project_dir, closed)
    except Exception:  # noqa: BLE001 (the board still hides closed issues when the save fails)
        log.warning("work_board.queue_prune_failed", closed=closed, exc_info=True)
        save_failed = True
    closed_set = set(closed)
    return [issue for issue in queue if issue not in closed_set], save_failed


# Chains ----------------------------------------------------------------------


def _open_chain_nodes(graph: DependencyGraph) -> set[int]:
    """Open, non-epic issues: the nodes a chain is made of (an epic is a container, a closed issue is done)."""
    nodes: set[int] = set()
    for issue in graph.nodes:
        task = graph.get_task(issue)
        if task is not None and task.state != TaskState.DONE and not is_epic(task.labels):
            nodes.add(issue)
    return nodes


def _components(nodes: set[int], graph: DependencyGraph) -> list[set[int]]:
    """Weakly connected groups of two or more ``nodes`` joined by a dependency, ascending by lowest issue."""
    seen: set[int] = set()
    groups: list[set[int]] = []
    for start in sorted(nodes):
        if start in seen:
            continue
        group = {start}
        pending = deque([start])
        while pending:
            issue = pending.popleft()
            for other in (graph.get_dependencies(issue) | graph.get_dependents(issue)) & nodes:
                if other not in group:
                    group.add(other)
                    pending.append(other)
        seen |= group
        if len(group) > 1:
            groups.append(group)
    return groups


def _dependency_order(group: set[int], graph: DependencyGraph) -> list[int]:
    """The group's issues with each dependency before its dependents; a cycle's members follow by number."""
    remaining = {issue: graph.get_dependencies(issue) & group for issue in group}
    ordered: list[int] = []
    while remaining:
        ready = sorted(issue for issue, deps in remaining.items() if not deps)
        if not ready:
            ready = sorted(remaining)
        for issue in ready:
            ordered.append(issue)
            del remaining[issue]
        for deps in remaining.values():
            deps.difference_update(ready)
    return ordered


def _chain_issue(issue: int, group: set[int], graph: DependencyGraph, stages: dict[int, WorkStage]) -> dict:
    task = graph.get_task(issue)
    state = task.state.value if task is not None else ""
    return {
        "number": issue,
        "title": task.title if task is not None else "",
        "state": state,
        "state_label": state_label(state) if state else "",
        "state_color": state_color(state) if state else "",
        "priority": task.priority.value if task is not None else "",
        "stage": stages[issue].value if issue in stages else None,
        "depends_on": sorted(graph.get_dependencies(issue) & group),
    }


def _chains(graph: DependencyGraph, on_board: set[int], stages: dict[int, WorkStage]) -> tuple[list[dict], int]:
    """The dependency chains containing an issue on the board, and the count of other blocked issues.

    A chain with no board issue is left out. The count covers open issues still waiting on a dependency
    that are neither on the board nor in a chain shown here.
    """
    nodes = _open_chain_nodes(graph)
    shown: list[list[int]] = []
    shown_issues: set[int] = set()
    for group in _components(nodes, graph):
        if group & on_board:
            shown.append(_dependency_order(group, graph))
            shown_issues |= group
    chains = [{"issues": [_chain_issue(issue, set(order), graph, stages) for issue in order]} for order in shown]
    other_blocked = sum(
        1
        for issue in nodes - on_board - shown_issues
        if unsatisfied_dependencies(issue, graph)  # None (no data) and [] (nothing open) are both not blocked here
    )
    return chains, other_blocked


# Board -----------------------------------------------------------------------


def _up_next(
    graph: DependencyGraph,
    *,
    running: set[int],
    open_prs: set[int],
    hidden: set[int],
    queued: set[int],
    stage_of: dict[int, WorkStage],
) -> tuple[list[dict], int]:
    """The first ``UP_NEXT_LIMIT`` ranked issues still in the ``next`` stage and not hidden, and how many more.

    Hidden issues are filtered after ranking, so the next ranked issue takes a freed slot and the ranking's
    own rules stay as they are. A queued issue is left out even when the work items lack it (so ``stage_of``
    has no stage for it): it is already in the ``queued`` column.
    """
    entries: list[dict] = []
    for entry in rank_graph_entries(graph, running=running, open_prs=open_prs):
        number = entry["id"]
        if number in hidden or number in queued or stage_of.get(number, WorkStage.NEXT) is not WorkStage.NEXT:
            continue
        entries.append(entry)
    shown = []
    for entry in entries[:UP_NEXT_LIMIT]:
        number = entry["id"]
        shown.append(
            {
                **_issue_entry(
                    number,
                    title=entry["title"],
                    url=entry["url"],
                    stage=WorkStage.NEXT,
                    state=entry["state"],
                    priority=entry["priority"],
                ),
                "rank": entry["rank"],
                "milestone": entry["milestone"],
                "unblocks": unblock_count(number, graph),
                "why": entry["why"],
            }
        )
    return shown, max(0, len(entries) - UP_NEXT_LIMIT)


@dataclass
class _Classified:
    """The work items sorted into stages: the item and entry by issue number, and each stage's column."""

    by_number: dict[int, dict] = field(default_factory=dict)
    columns: dict[WorkStage, list[dict]] = field(default_factory=lambda: defaultdict(list))
    stage_of: dict[int, WorkStage] = field(default_factory=dict)
    entries: dict[int, dict] = field(default_factory=dict)


def _stage_details(item: dict, stage: WorkStage, runs: dict[str, dict]) -> dict:
    """The fields a column adds to an item's entry (``running``, ``review``, ``needs``, ``yours``, ``blocked``)."""
    if stage is WorkStage.RUNNING:
        return _running_details(item, runs)
    if stage is WorkStage.REVIEW:
        return _review_details(item)
    if stage is WorkStage.NEEDS:
        kind, reason = _reason_for_needs(item.get("state", ""))
        return {"kind": kind, "reason": reason, "pr_number": item.get("pr_number")}
    if stage is WorkStage.YOURS:
        return _reason_for_yours(item)
    if stage is WorkStage.BLOCKED:
        return {"blocked_by": (item.get("suitability") or {}).get("blocked_by", [])}
    return {}


def _classify_items(items: list[dict], queue_set: set[int], runs: dict[str, dict]) -> _Classified:
    """Put each work item in its one stage (``work_stage()``) and build its column entry."""
    board = _Classified()
    for item in items:
        number = _parse_issue_number(item.get("issue_number"))
        stage = work_stage(item.get("state", ""), _item_mode(item), _item_run_status(item), queued=number in queue_set)
        entry = _item_entry(item, stage)
        entry.update(_stage_details(item, stage, runs))
        if number is not None:
            board.by_number[number] = item
            board.stage_of[number] = stage
            board.entries[number] = entry
        board.columns[stage].append(entry)
    return board


def _classify_queue_fallbacks(
    board: _Classified, queue: list[int], graph: DependencyGraph, runs: dict[str, dict]
) -> None:
    """Give each queued issue the work items lack the stage the graph's state puts it in, and its column.

    The stage comes from ``work_stage()`` like every other issue, so a queued issue the graph shows in progress
    or human-only lands in ``running`` or ``yours`` instead of ``queued``. An issue in the ``done`` stage has no
    column here (``done_today`` is the only place a finished issue is shown).
    """
    for issue in queue:
        if issue in board.stage_of:
            continue
        task = graph.get_task(issue)
        state = task.state.value if task is not None else ""
        stage = work_stage(state, None, None, queued=True)
        entry = _fallback_entry(issue, graph, stage)
        entry.update(_stage_details({"issue_number": issue, "state": state}, stage, runs))
        board.stage_of[issue] = stage
        board.entries[issue] = entry
        if stage is not WorkStage.DONE:
            board.columns[stage].append(entry)


def _apply_done_precedence(board: _Classified, done_today: list[dict] | None) -> None:
    """Take an issue merged today out of every other column: ``done`` is the first stage that applies.

    The work items are cached and can lag a merge (still ``pr_ready_to_merge``), so ``done_today``, built from
    the merged PRs, is the fresher source for these issues.
    """
    merged = {entry["number"] for entry in done_today or [] if entry["number"] is not None}
    if not merged:
        return
    for stage, entries in board.columns.items():
        if stage is not WorkStage.DONE:
            entries[:] = [entry for entry in entries if entry["number"] not in merged]
    for issue in merged:
        if issue in board.stage_of:
            board.stage_of[issue] = WorkStage.DONE


async def build_board(project_dir: Path | None, milestone: str = "", *, now: datetime | None = None) -> dict:
    """The Work board: ``{stages, up_next, more_ready, queued, running, review, needs, yours, blocked, done_today,
    chains, other_blocked, hidden, unavailable}``.

    Every issue is in one column (``work_stage()``). ``milestone`` limits the ranking behind ``up_next`` only.
    A dependency graph failure raises, since an empty board must not look like an idle project; the sources that
    only enrich the board fail open, and the ones that did are named in ``unavailable``.
    """
    now = now or datetime.now(timezone.utc)
    # The graph first: a cold build is awaited without the work-items poll's short wait, so the items that
    # follow classify from this graph rather than from none (a just-changed priority is only in a fresh one).
    graph = await load_ranking_graph(project_dir)
    ranking_graph = await load_ranking_graph(project_dir, milestone) if milestone else graph
    items_data, (running, open_prs), queue, hidden_list, runs, done_today = await asyncio.gather(
        get_work_items(project_dir),
        next_task_exclusions(project_dir),
        load_task_queue(project_dir),
        load_hidden_issues(project_dir),
        _active_runs(project_dir),
        _done_today(project_dir, now),
    )
    queue, prune_failed = await _prune_closed_queue(project_dir, queue, graph)
    queue_set = set(queue)
    hidden = set(hidden_list)

    board = _classify_items(items_data["items"], queue_set, runs or {})
    _classify_queue_fallbacks(board, queue, graph, runs or {})
    _apply_done_precedence(board, done_today)
    columns, stage_of = board.columns, board.stage_of

    queued = [board.entries[issue] for issue in queue if stage_of.get(issue) is WorkStage.QUEUED]
    up_next, more_ready = _up_next(
        ranking_graph, running=running, open_prs=open_prs, hidden=hidden, queued=queue_set, stage_of=stage_of
    )
    hidden_entries = [
        _item_entry(board.by_number[n], stage_of[n])
        if n in board.by_number
        else _fallback_entry(n, graph, WorkStage.NEXT)
        for n in hidden_list
    ]

    on_board = {
        entry["number"]
        for entry in [
            *up_next,
            *queued,
            *columns[WorkStage.RUNNING],
            *columns[WorkStage.REVIEW],
            *columns[WorkStage.NEEDS],
            *columns[WorkStage.YOURS],
            *columns[WorkStage.BLOCKED],
            *(done_today or []),
        ]
        if entry["number"] is not None
    }
    chains, other_blocked = _chains(graph, on_board, stage_of)

    return {
        "stages": [stage.value for stage in WorkStage],
        "up_next": up_next,
        "more_ready": more_ready,
        "queued": queued,
        "running": columns[WorkStage.RUNNING],
        "review": columns[WorkStage.REVIEW],
        "needs": columns[WorkStage.NEEDS],
        "yours": columns[WorkStage.YOURS],
        "blocked": columns[WorkStage.BLOCKED],
        "done_today": done_today or [],
        "chains": chains,
        "other_blocked": other_blocked,
        "hidden": hidden_entries,
        "unavailable": [
            name
            for name, failed in (
                ("done_today", done_today is None),
                ("active_runs", runs is None),
                ("queue_prune", prune_failed),
            )
            if failed
        ],
    }
