"""Work item service: unified state model for the agents dashboard.

Merges four state sources (GitHub labels, PR status, handoff files, running agents)
into a single computed state per issue/PR. The task browser renders from this state
instead of computing actions independently.

Priority cascade: running agent > PR state (SOVA-verdict adjusted) > GitHub label.

Split into focused modules:
- work_state.py:  state enum, constants, compute_work_item_state, actions, sort
- work_verdict.py: verdict cache, verdict fetching, GitHub review parsing
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from sova.adapters.base import Task
from sova.adapters.priority import Priority
from sova.config.loader import load_config
from sova.dashboard.services.pr_service import confirm_open_pr_numbers, known_open_pr_numbers
from sova.dashboard.services.work_state import (
    _AWAITING_APPROVAL as _AWAITING_APPROVAL,
)
from sova.dashboard.services.work_state import (
    _LABEL_STATE_MAP as _LABEL_STATE_MAP,
)
from sova.dashboard.services.work_state import (
    _SPEC_ACTION_IDS as _SPEC_ACTION_IDS,
)
from sova.dashboard.services.work_state import (
    _STATE_SORT_ORDER as _STATE_SORT_ORDER,
)
from sova.dashboard.services.work_state import (
    PRFacts as PRFacts,
)
from sova.dashboard.services.work_state import (
    Resolution as Resolution,
)
from sova.dashboard.services.work_state import (  # re-export facade
    WorkItemState as WorkItemState,
)
from sova.dashboard.services.work_state import (
    _build_action as _build_action,
)
from sova.dashboard.services.work_state import (
    _get_actions as _get_actions,
)
from sova.dashboard.services.work_state import (
    _sort_items as _sort_items,
)
from sova.dashboard.services.work_state import (
    compute_work_item_resolution as compute_work_item_resolution,
)
from sova.dashboard.services.work_state import (
    compute_work_item_state as compute_work_item_state,
)
from sova.dashboard.services.work_state import (
    describe_reason_chain as describe_reason_chain,
)
from sova.dashboard.services.work_state import (
    resolve_next_action as resolve_next_action,
)
from sova.dashboard.services.work_state import run_action
from sova.dashboard.services.work_state import (
    state_color as state_color,
)
from sova.dashboard.services.work_state import (
    state_label as state_label,
)
from sova.dashboard.services.work_verdict import (  # re-export facade
    _fetch_sova_verdicts as _fetch_sova_verdicts,
)
from sova.dashboard.services.work_verdict import (
    _sova_verdict_cache as _sova_verdict_cache,
)
from sova.dashboard.services.work_verdict import (
    clear_verdict_cache as clear_verdict_cache,
)
from sova.git.pr import PROpenState, pr_open_state_from_list
from sova.supervisor.suitability import (
    RankedTask,
    assess_suitability,
    rank_and_count_next_tasks,
    rank_next_tasks,
    suitability_payload,
)
from sova.utils.log_dedup import warn_once
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.config.models import ProjectConfig
    from sova.dashboard.services.agent_pool import ProjectAgents
    from sova.supervisor.dependency_graph import DependencyGraph

log = get_logger(component="dashboard.work_item")


# Handoff helpers -------------------------------------------------------------

_HANDOFF_STATES = frozenset({WorkItemState.SPEC_REVIEW})


def _extract_handoff_actions(handoff: dict | None, state: WorkItemState) -> list[dict]:
    if handoff and state in _HANDOFF_STATES:
        return handoff.get("next_actions", [])
    return []


def _extract_handoff_summary(handoff: dict | None, state: WorkItemState) -> str:
    if handoff and state in _HANDOFF_STATES:
        return handoff.get("summary", "")
    return ""


def _reason_chain_payload(resolution: Resolution, facts: PRFacts | None) -> list[str]:
    """Render a Resolution's reason_chain to one sentence per rule, for the item payload.

    The last sentence is the rule that matched; the client displays it and shows
    the full chain on hover. Empty when facts is None (label-only item, spec-review
    override: the ladder never ran, so there is no PR-routing decision to explain).
    """
    if facts is None:
        return []
    return describe_reason_chain(resolution.reason_chain, facts)


def _synthesize_spec_actions(issue_number: str) -> list[dict]:
    """Reconstruct spec handoff actions when the handoff file is missing."""
    return [
        {
            "id": "approve-spec",
            "label": "Approve Spec",
            "description": "Accept the spec and proceed to development",
            "style": "approve",
            "mode": "agent",
            "args": {"issue": issue_number, "role": "developer"},
        },
        {
            "id": "reject-spec",
            "label": "Reject",
            "description": "Reject spec and mark issue as needs_spec",
            "style": "danger",
            "mode": "shell",
            "args": {"issue": issue_number},
        },
    ]


# Dependency graph ------------------------------------------------------------


class GraphConfigError(Exception):
    """Raised when project config or adapter creation fails, so no dependency graph can be built."""


async def load_dependency_graph(project_dir: Path | None, milestone: str = "") -> DependencyGraph:
    """Build the project's dependency graph through ``build_dependency_graph()``.

    Never touches the graph cache directly, so its TTL and the agent-spawn and
    epic-close invalidations apply unchanged. A config or adapter failure raises
    ``GraphConfigError``; a failure while fetching issues propagates as-is.
    """
    from sova.adapters import create_adapter
    from sova.supervisor.dependency_graph import build_dependency_graph

    try:
        cfg = load_config(project_dir)
        adapter = create_adapter(cfg)
    except Exception as exc:  # noqa: BLE001 (config load and adapter construction fail in many ways; translated to GraphConfigError)
        raise GraphConfigError(str(exc)) from exc
    return await build_dependency_graph(adapter, milestone=milestone)


# A graph build lists up to 500 issues plus every older open issue and fetches each missing dependency,
# so no caller waits on one while a recent previous graph exists: it answers from that graph and the rebuild runs
# in the background (``_previous_graph_refreshing()``). With no previous graph, the work-items poll waits
# only this long before classifying as blocked, while the build keeps running and warms the next poll.
_GRAPH_WAIT_SECONDS = 3.0
# Cap on the background build itself, so a hung tracker call cannot leave every later poll waiting on it.
_GRAPH_BUILD_TIMEOUT_SECONDS = 120.0
# After a failed build, polls skip the rebuild for this long instead of re-fetching the whole tracker each time.
_GRAPH_FAILURE_BACKOFF_SECONDS = 30.0


@dataclass(frozen=True)
class _KnownGraph:
    """A finished build's graph with what a response needs to say how old it is."""

    graph: DependencyGraph
    built_at: float  # time.monotonic() when the graph was fetched (``DependencyGraph.fetched_at``), not when read
    digest: str  # what a ranking or classification reads, so a rebuild that changed none of it stays quiet
    refresh_failed: bool = False  # the latest rebuild after this one failed, so the graph is older than it should be


_graph_builds: dict[str, asyncio.Task[DependencyGraph]] = {}  # project -> the build in flight, shared by every caller
_graph_failures: dict[str, tuple[float, BaseException]] = {}  # project -> (monotonic time, error) of its last failure
_last_graphs: dict[str, _KnownGraph] = {}  # project -> its last build, kept through any number of failed rebuilds
# Projects whose graph forget_graph() dropped: the next build that lands owes open boards a reload notice, since
# it replaces a graph they showed (or ranked from) and there is no previous digest left to compare it with.
_graph_notice_owed: set[str] = set()
# Builds forget_graph() retired while still running: no longer shared or cached from, but still cancelled at shutdown.
_retired_graph_builds: set[asyncio.Task[DependencyGraph]] = set()
# Change notifications in flight, held by reference so they are not collected mid-send and cancelled at shutdown.
_graph_notify_tasks: set[asyncio.Task[None]] = set()
GraphChangeListener = Callable[[Path], Awaitable[None]]
_graph_change_listener: GraphChangeListener | None = None


def set_graph_change_listener(listener: GraphChangeListener | None) -> None:
    """Register the coroutine called as ``listener(project_dir)`` when a rebuild replaced a graph with a different one.

    This is how the status socket learns to tell open boards to reload: a board answered from the previous
    graph while the rebuild ran, and nothing else would tell it the new graph has landed.
    """
    global _graph_change_listener
    _graph_change_listener = listener


def reset_graph_load_state() -> None:
    """Forget in-flight builds, recorded failures and last graphs. Test-only, like ``log_dedup.reset()``."""
    _graph_builds.clear()
    _graph_failures.clear()
    _last_graphs.clear()
    _graph_notice_owed.clear()
    _retired_graph_builds.clear()
    _graph_notify_tasks.clear()


def _graph_key(project_dir: Path | None) -> str:
    """The key every graph map is stored under: the resolved project, so one project has one build and one graph.

    Callers pass the project differently (the board resolves it, a work-items request passes its own value,
    ``None`` outside multi-project mode). Keyed by that value, one project would run two full builds at once
    and keep two previous graphs that refresh at different times.
    """
    from sova.config.context import resolve_project_dir
    from sova.dashboard.services.agent_pool import get_default_project_dir

    return str(resolve_project_dir(project_dir, default_project_dir=get_default_project_dir()))


def forget_graph(project_dir: Path | None) -> None:
    """Drop the project's shared build, last graph and recorded failure, so the next ranking fetches afresh.

    For a change that makes the graph stale before its cache expires (an issue's priority). A build in
    flight is not cancelled, since other callers still wait on it, but nothing it fetches is kept. It stays
    tracked in ``_retired_graph_builds`` until it finishes, so ``cancel_graph_builds()`` still stops it.
    """
    key = _graph_key(project_dir)
    retired = _graph_builds.pop(key, None)
    if retired is not None and not retired.done():
        _retired_graph_builds.add(retired)
    _graph_failures.pop(key, None)
    if _last_graphs.pop(key, None) is not None:
        _graph_notice_owed.add(key)


def request_graph_rebuild(project_dir: Path | None) -> None:
    """Start a fresh shared build behind the project's previous graph, which keeps answering until it lands.

    For a change seen on the tracker (the GitHub probe found the issue list changed, #1534): unlike
    ``forget_graph()`` no caller waits, but the ranking must not keep the old graph until its own refresh. A build
    already in flight may have read the issues before the change, so it is retired (its result is not kept) and a
    new one starts. A rebuilt graph that differs tells the status socket (``_on_graph_build_done()``), which rebuilds
    the board snapshots. A project with no graph yet is left to its first ranking, and within the failure backoff
    nothing starts. The caller drops the tracker-level cache first (``invalidate_graph_cache()``), or the new
    build would answer from it.
    """
    key = _graph_key(project_dir)
    if key not in _last_graphs or _recent_graph_failure(key) is not None:
        return
    retired = _graph_builds.pop(key, None)
    if retired is not None and not retired.done():
        _retired_graph_builds.add(retired)
    _shared_graph_build(key)


async def cancel_graph_builds() -> None:
    """Cancel every graph build and change notification in flight, retired builds included, at lifespan shutdown."""
    builds = [*_graph_builds.values(), *_retired_graph_builds, *_graph_notify_tasks]
    for build in builds:
        build.cancel()
    if builds:
        await asyncio.gather(*builds, return_exceptions=True)
    _graph_builds.clear()
    _retired_graph_builds.clear()
    _graph_notify_tasks.clear()


async def _build_graph_bounded(project_dir: Path | None) -> DependencyGraph:
    return await asyncio.wait_for(load_dependency_graph(project_dir), timeout=_GRAPH_BUILD_TIMEOUT_SECONDS)


def _graph_digest(graph: DependencyGraph) -> str:
    """A digest of what a board or ranking reads from a graph, independent of listing order.

    Per node: state, priority, labels, milestone (the ranking's phase and the milestone grouping), assignees
    (the Mine filter), title and issue type (cards and epics). Plus the edges (unblock counts, blockers).
    """
    nodes = []
    for number in sorted(graph.nodes):
        task = graph.get_task(number)
        if task is not None:
            nodes.append(
                (
                    number,
                    str(task.state),
                    str(task.priority),
                    sorted(task.labels),
                    task.milestone,
                    sorted(task.assignees),
                    task.title,
                    task.issue_type,
                )
            )
    edges = sorted(tuple(edge) for edge in graph.edges)
    return hashlib.sha256(json.dumps([nodes, edges], default=str).encode("utf-8")).hexdigest()


async def _notify_graph_changed(project_dir: Path) -> None:
    listener = _graph_change_listener
    if listener is None:
        return
    try:
        await listener(project_dir)
    except Exception:  # noqa: BLE001 (a failed notification only delays an open board's reload to its next event)
        log.warning("work_items.graph_change_notify_failed", exc_info=True)


def _schedule_graph_change_notice(key: str) -> None:
    if _graph_change_listener is None:
        return
    task = asyncio.get_running_loop().create_task(_notify_graph_changed(Path(key)))
    _graph_notify_tasks.add(task)
    task.add_done_callback(_graph_notify_tasks.discard)


def _on_graph_build_done(key: str, build: asyncio.Task[DependencyGraph]) -> None:
    """Forget a finished build, and remember its graph or how it failed.

    A failure keeps the previous graph, marked as having a failed refresh: one transient tracker error must not
    turn every card to blocked, and an older graph is still better than none (its age is reported, never hidden).
    The status socket hears of every change a board can show: a rebuild that replaced a different graph, one
    that landed after ``forget_graph()`` dropped the graph, and a refresh that started or stopped failing, so an
    open board (which does not poll) reloads onto the new graph or shows how old its ranking is. Reading the
    exception marks it retrieved, so a build whose waiters all timed out and walked away does not log "Task
    exception was never retrieved".
    """
    is_current = _graph_builds.get(key) is build
    if is_current:
        del _graph_builds[key]
    _retired_graph_builds.discard(build)
    if build.cancelled():
        return
    failure = build.exception()
    if not is_current:
        return  # forget_graph() replaced it: what it fetched predates the change that caused that
    previous = _last_graphs.get(key)
    if failure is not None:
        _graph_failures[key] = (time.monotonic(), failure)
        if previous is not None and not previous.refresh_failed:
            _last_graphs[key] = replace(previous, refresh_failed=True)
            _schedule_graph_change_notice(key)  # the board now has to say its ranking is old
        return
    _graph_failures.pop(key, None)
    graph = build.result()
    owed = key in _graph_notice_owed
    _graph_notice_owed.discard(key)
    if previous is not None and previous.graph is graph:
        # The graph cache answered with the graph already held: same graph, same age, nothing to hash again.
        _last_graphs[key] = replace(previous, refresh_failed=False)
        changed = previous.refresh_failed
    else:
        digest = _graph_digest(graph)
        _last_graphs[key] = _KnownGraph(graph=graph, built_at=graph.fetched_at, digest=digest)
        changed = previous is None or previous.digest != digest or previous.refresh_failed
    if owed or (previous is not None and changed):
        _schedule_graph_change_notice(key)


def _recent_graph_failure(key: str) -> BaseException | None:
    """The error of the project's last build while it is within ``_GRAPH_FAILURE_BACKOFF_SECONDS``."""
    recorded = _graph_failures.get(key)
    if recorded is not None and time.monotonic() - recorded[0] < _GRAPH_FAILURE_BACKOFF_SECONDS:
        return recorded[1]
    return None


def _backoff_error(failure: BaseException) -> GraphConfigError | RuntimeError:
    """A new exception for a caller within the backoff, chained to the recorded ``failure``.

    Raising the recorded object itself would append every caller's frames to its one shared
    ``__traceback__``, so a later log line would print the stacks of unrelated requests.
    ``GraphConfigError`` keeps its type because the routers map it to 503. The chain is set here
    (``__cause__`` is what ``raise ... from`` assigns), so a caller just raises the result.
    """
    error = (
        GraphConfigError(str(failure))
        if isinstance(failure, GraphConfigError)
        else RuntimeError(f"dependency graph build failed recently: {failure}")
    )
    error.__cause__ = failure
    return error


def _shared_graph_build(key: str) -> asyncio.Task[DependencyGraph]:
    """The project's build in flight, started when there is none. ``key`` is ``_graph_key(project_dir)``.

    The one build every caller of the unfiltered graph shares, so a work-items poll and a
    next-tasks request on a cold cache run one full fetch between them. Callers await it
    through ``asyncio.shield()``, so one of them leaving does not cancel it for the rest.
    ``cancel_graph_builds()`` is the only thing that cancels it.
    """
    build = _graph_builds.get(key)
    if build is None:
        build = asyncio.create_task(_build_graph_bounded(Path(key)))
        _graph_builds[key] = build
        build.add_done_callback(partial(_on_graph_build_done, key))
    return build


def _build_hit_its_cap(build: asyncio.Task[DependencyGraph]) -> bool:
    """Whether ``build`` ended on ``_GRAPH_BUILD_TIMEOUT_SECONDS``, as opposed to a caller's wait running out.

    Both reach a waiter as ``TimeoutError`` (the build's through ``asyncio.shield()``).
    """
    return build.done() and not build.cancelled() and isinstance(build.exception(), TimeoutError)


def _previous_graph_refreshing(key: str) -> DependencyGraph | None:
    """The project's previous graph, with the shared rebuild started (or joined) behind it; None without one.

    Callers answer from it at once instead of awaiting the rebuild, so the 180s graph cache expiring never
    makes a request wait on the tracker. The rebuild's graph answers the next call, and a rebuild that changed
    the graph tells the status socket (``_on_graph_build_done()``), so an open board reloads onto it. The
    previous graph answers whatever its age, through failed rebuilds too, and ``graph_age_meta()`` reports how
    old it is; it is gone only after ``forget_graph()``, so a priority change still waits for a fresh ranking.
    Within the failure backoff no rebuild starts, but the previous graph still answers.
    """
    known = _last_graphs.get(key)
    if known is None:
        return None
    if _recent_graph_failure(key) is None:
        _shared_graph_build(key)
    return known.graph


def graph_age_meta(project_dir: Path | None) -> dict | None:
    """How old the graph a board or work-items response may have used is, or None when no graph is held.

    ``graph_age_seconds`` is the age of the graph a caller is answered from while a rebuild runs, and
    ``graph_refresh_failed`` says the latest rebuild failed, so a client can show that its data is older than
    the refresh interval instead of looking current.
    """
    known = _last_graphs.get(_graph_key(project_dir))
    if known is None:
        return None
    return {
        "graph_age_seconds": round(time.monotonic() - known.built_at, 1),
        "graph_refresh_failed": known.refresh_failed,
    }


async def _load_graph_or_none(project_dir: Path | None) -> DependencyGraph | None:
    """Load the dependency graph for work item classification; None when it is unavailable.

    A previous graph answers at once while the rebuild runs in the background
    (``_previous_graph_refreshing()``), even within the failure backoff. Without one, waits at most
    ``_GRAPH_WAIT_SECONDS``. Concurrent callers share one build per project, the build outlives a caller that
    stops waiting (it is shielded), and a failed build is not retried for ``_GRAPH_FAILURE_BACKOFF_SECONDS``.
    A failure, a backoff, or no build finished within the wait all return None, so every queue task
    classifies as blocked rather than the response stalling or the tracker being hit on every poll.
    """
    key = _graph_key(project_dir)
    previous = _previous_graph_refreshing(key)
    if previous is not None:
        return previous
    if _recent_graph_failure(key) is not None:
        return None
    build = _shared_graph_build(key)
    try:
        return await asyncio.wait_for(asyncio.shield(build), timeout=_GRAPH_WAIT_SECONDS)
    except TimeoutError:
        if _build_hit_its_cap(build):
            log.warning("work_items.graph_failed", exc_info=True)
            return None
        warn_once(log, "work_items.graph_timeout", key, ttl_seconds=60.0, wait_seconds=_GRAPH_WAIT_SECONDS)
        return None
    except Exception:  # noqa: BLE001 (work items must keep rendering; each classifies as blocked without a graph)
        log.warning("work_items.graph_failed", exc_info=True)
        return None


async def load_classification_graph(project_dir: Path | None) -> DependencyGraph | None:
    """The dependency graph for classifying one issue, or None when it is unavailable within the short wait.

    The public form of ``_load_graph_or_none()``: the same shared build, wait limit and failure backoff as
    the work-items poll, so a caller outside this module (the launch modal's suitability warning) cannot
    start a second build or stall on a slow tracker.
    """
    return await _load_graph_or_none(project_dir)


async def load_ranking_graph(project_dir: Path | None, milestone: str = "") -> DependencyGraph:
    """The dependency graph the "Up next" ranking reads. A failure raises: it must not look like an empty list.

    Without ``milestone``, a previous graph answers at once while the shared rebuild runs in the
    background (``_previous_graph_refreshing()``). Without one, this awaits the same shared build as
    the work-items poll, with no wait limit of its own (the build is capped by
    ``_GRAPH_BUILD_TIMEOUT_SECONDS``), so concurrent cold-cache callers run one fetch. Within
    ``_GRAPH_FAILURE_BACKOFF_SECONDS`` of a failed build it raises a new error chained to that failure
    instead of fetching again. A milestone-filtered graph is a rare, manual build that bypasses the
    graph cache, so it is built on its own.
    """
    if milestone:
        return await load_dependency_graph(project_dir, milestone)
    key = _graph_key(project_dir)
    previous = _previous_graph_refreshing(key)
    if previous is not None:
        return previous
    failure = _recent_graph_failure(key)
    if failure is not None:
        raise _backoff_error(failure)
    return await asyncio.shield(_shared_graph_build(key))


def _classify_queue_item(task: dict, graph: DependencyGraph | None) -> dict:
    """Classify a queue task from the dependency graph's own snapshot of it.

    The graph's ``Task`` is preferred so labels, state and dependencies come from
    one snapshot with the graph node, even while the queue cache (120s) and the
    graph cache (180s) briefly disagree. Without a graph, or for an issue the graph
    lacks, the classifier fails closed to ``blocked`` rather than guessing ``autonomous``;
    label-driven modes (epic, human, on-hold) still take precedence.

    The classifier reports both gaps as ``{"source": "graph", "value": "unavailable"}``. This
    function, which alone knows whether a graph exists, refines the case of a graph that was
    built but lacks a numeric issue to ``missing``: an issue created since the graph was cached,
    or a closed issue outside the closed-issue window. A reader can then tell a tracker outage
    from a gap in a healthy graph. The build lists every open issue on its own, so an open
    issue is normally a node and ``missing`` clears on the next rebuild.
    """
    issue = str(task["issue"])
    number = _parse_issue_number(issue)
    classified = graph.get_task(number) if graph is not None and number is not None else None
    graph_lacks_issue = classified is None and graph is not None and number is not None
    if classified is None:
        # The parsed number, so "#4" reads as the issue it names rather than as a non-numeric id.
        classified = Task(
            id=str(number) if number is not None else issue,
            title=task.get("title", ""),
            labels=list(task.get("labels", [])),
        )
    payload = suitability_payload(assess_suitability(classified, graph))
    if graph_lacks_issue and payload["reasons"] == [{"source": "graph", "value": "unavailable"}]:
        payload["reasons"] = [{"source": "graph", "value": "missing"}]
    return payload


# Item builders ---------------------------------------------------------------


def _resolve_spec_review_override(
    *,
    state: WorkItemState,
    running: dict | None,
    pr_data: dict | None,
    last_run: dict | None,
    last_run_status: str | None,
    handoff: dict | None,
    issue_num: str,
    pr_number: str | None,
) -> tuple[WorkItemState, dict, list, list]:
    """Override to SPEC_REVIEW when a spec run awaits approval; otherwise resolve normal actions."""
    if running or pr_data is not None or not last_run or last_run_status != _AWAITING_APPROVAL:
        primary, secondary = _get_actions(state, issue_number=issue_num, pr_number=pr_number)
        return state, primary, secondary, []

    primary = _build_action(
        "resume-approval",
        "Approve & Resume",
        "success",
        "resume_from_approval",
        {"run_id": last_run["id"]},
    )
    spec_handoff_actions = _synthesize_spec_actions(issue_num) if not handoff else []
    return WorkItemState.SPEC_REVIEW, primary, [], spec_handoff_actions


def _task_pr_numbers(
    issue_num: str,
    pr_data: dict | None,
    last_run: dict | None,
    open_pr_numbers: Collection[int] | None,
) -> tuple[int | None, int | None]:
    """Return ``(pr_number, closed_pr_number)`` for a queue task.

    A linked PR wins. Otherwise the last run's PR is kept unless ``open_pr_numbers`` proves it is no
    longer open, in which case it becomes ``closed_pr_number`` (else the queue's own ``closed_pr_number``).
    """
    if pr_data:
        return pr_data.get("number"), None
    if not last_run:
        return None, None
    pr_number = last_run.get("pr_number")
    if not pr_number:
        closed = last_run.get("closed_pr_number")
        # The queue result is cached longer than the open list: a PR it recorded as closed may have reopened.
        if closed is not None and pr_open_state_from_list(closed, open_pr_numbers) is PROpenState.OPEN:
            return None, None
        return None, closed
    if pr_open_state_from_list(pr_number, open_pr_numbers) is PROpenState.NOT_OPEN:
        log.debug("work_items.stale_pr_number_dropped", issue=issue_num, pr=pr_number)
        return None, pr_number
    return pr_number, last_run.get("closed_pr_number")


def _build_task_item(
    task: dict,
    pr_data: dict | None,
    running: dict | None,
    handoff: dict | None,
    sova_verdict: dict | None = None,
    *,
    external_reviews_enabled: bool = True,
    max_address_cycles: int = 0,
    suitability: dict | None = None,
    open_pr_numbers: Collection[int] | None = None,
) -> dict:
    """Build a work item from a queue task and its linked PR/handoff/agent.

    ``open_pr_numbers`` is the fetched open-PR list, or ``None`` when that fetch failed. A PR number
    carried over from the last run is dropped only when that list proves the PR is no longer open;
    that PR, or the one the queue already proved closed, is passed on as ``closed_pr_number``.
    """
    issue_num = str(task["issue"])
    last_run = task.get("last_run")
    pr_number, closed_pr_number = _task_pr_numbers(issue_num, pr_data, last_run, open_pr_numbers)

    resolution, facts = compute_work_item_resolution(
        task_state=task.get("state"),
        pr_data=pr_data,
        running_agent=running,
        sova_verdict=sova_verdict if pr_data else None,
        external_reviews_enabled=external_reviews_enabled,
        max_address_cycles=max_address_cycles,
        closed_pr_number=closed_pr_number,
    )
    last_run_status = last_run.get("status") if last_run else None
    state, primary, secondary, spec_handoff_actions = _resolve_spec_review_override(
        state=resolution.state,
        running=running,
        pr_data=pr_data,
        last_run=last_run,
        last_run_status=last_run_status,
        handoff=handoff,
        issue_num=issue_num,
        pr_number=pr_number,
    )
    reason_chain = _reason_chain_payload(resolution, facts)
    if state is WorkItemState.PR_MERGED_ISSUE_OPEN:
        reason_chain = [f"PR #{closed_pr_number} is no longer open, but the issue is still labeled in review"]
    matched_reason = reason_chain[-1] if reason_chain else ""
    _attach_merge_blockers(primary, secondary, resolution, facts)

    return _build_item(
        issue_number=issue_num,
        pr_number=pr_number,
        title=task.get("title", ""),
        url=task.get("url", ""),
        state=state,
        primary_action=primary,
        secondary_actions=secondary,
        handoff_actions=spec_handoff_actions or _extract_handoff_actions(handoff, state),
        handoff_summary=_extract_handoff_summary(handoff, state),
        running_agent=_format_running_agent(running) if running else None,
        pr_details=_format_pr_details(pr_data) if pr_data else None,
        sova_context=_format_sova_context(sova_verdict if pr_data else None),
        reason_chain=reason_chain,
        matched_reason=matched_reason,
        labels=task.get("labels", []),
        priority=task.get("priority", 99),
        priority_label=task.get("priority_label", ""),
        issue_priority=task.get("issue_priority", Priority.UNDEFINED.value),
        last_run=task.get("last_run"),
        created_at=task.get("created_at", ""),
        assignees=task.get("assignees", []),
        jira_key=task.get("jira_key", ""),
        issue_type=task.get("issue_type", ""),
        last_failed=bool(task.get("last_run") and task["last_run"].get("status") == "failed"),
        story_points=task.get("story_points"),
        sprint=task.get("sprint", ""),
        components=task.get("components", []),
        jira_status=task.get("jira_status", ""),
        jira_priority=task.get("jira_priority", ""),
        updated_at=task.get("updated_at", ""),
        suitability=suitability,
    )


def _build_pr_item(
    pr: dict,
    running: dict | None,
    handoff: dict | None,
    issue_num: str | None,
    sova_verdict: dict | None = None,
    *,
    external_reviews_enabled: bool = True,
    max_address_cycles: int = 0,
) -> dict:
    """Build a work item from a standalone or unlinked PR."""
    resolution, facts = compute_work_item_resolution(
        task_state=None,
        pr_data=pr,
        running_agent=running,
        sova_verdict=sova_verdict,
        external_reviews_enabled=external_reviews_enabled,
        max_address_cycles=max_address_cycles,
    )
    state = resolution.state
    reason_chain = _reason_chain_payload(resolution, facts)
    matched_reason = reason_chain[-1] if reason_chain else ""
    primary, secondary = _get_actions(state, issue_number=issue_num, pr_number=pr["number"])
    _attach_merge_blockers(primary, secondary, resolution, facts)

    return _build_item(
        issue_number=issue_num,
        pr_number=pr["number"],
        title=pr.get("title", ""),
        url=pr.get("url", ""),
        state=state,
        primary_action=primary,
        secondary_actions=secondary,
        handoff_actions=_extract_handoff_actions(handoff, state),
        handoff_summary=_extract_handoff_summary(handoff, state),
        running_agent=_format_running_agent(running) if running else None,
        pr_details=_format_pr_details(pr),
        sova_context=_format_sova_context(sova_verdict),
        reason_chain=reason_chain,
        matched_reason=matched_reason,
        labels=pr.get("labels", []),
        priority=-2,
        priority_label="",
        issue_priority=Priority.from_labels(pr.get("labels", [])).value,
        last_run=None,
        created_at="",
        assignees=pr.get("assignees", []),
        jira_key=pr.get("linked_jira_key", ""),
        issue_type="",
        last_failed=False,
        story_points=None,
        sprint="",
        components=[],
        jira_status="",
        jira_priority="",
        updated_at=pr.get("updated_at", ""),
    )


# Standalone PR items ---------------------------------------------------------


def _append_standalone_pr_items(
    items: list[dict],
    prs: list[dict],
    linked_issue_numbers: set[str],
    running_by_issue: dict[str, dict],
    handoffs_by_issue: dict[str, dict],
    verdicts_by_issue: dict[str, dict] | None = None,
    graph: DependencyGraph | None = None,
    jira_base_url: str = "",
    is_jira: bool = False,
    *,
    external_reviews_enabled: bool = True,
    max_address_cycles: int = 0,
) -> None:
    """Add work items for PRs not already covered by a queue task."""
    for pr in prs:
        issue_num = str(pr.get("linked_issue", "")) if pr.get("linked_issue") else None
        if issue_num and issue_num in linked_issue_numbers:
            continue
        if issue_num:
            linked_issue_numbers.add(issue_num)
        pr_key = issue_num if issue_num else f"pr:{pr['number']}"
        running = running_by_issue.get(pr_key)
        handoff = handoffs_by_issue.get(pr_key) if not issue_num else handoffs_by_issue.get(issue_num)
        if verdicts_by_issue is not None:
            if issue_num:
                verdict = verdicts_by_issue.get(issue_num)
            else:
                verdict = verdicts_by_issue.get(f"pr:{pr['number']}")
        else:
            verdict = None
        item = _build_pr_item(
            pr,
            running,
            handoff,
            issue_num,
            sova_verdict=verdict,
            external_reviews_enabled=external_reviews_enabled,
            max_address_cycles=max_address_cycles,
        )
        # A linked issue outside the actionable queue still has its own tracker title and URL.
        task = graph.get_task(int(issue_num)) if graph is not None and issue_num and issue_num.isdigit() else None
        if task is not None and is_jira:
            item.update(
                title=task.title,
                url=task.url,
                assignees=list(task.assignees),
                issue_priority=task.priority.value,
                jira_key=task.metadata.get("key", ""),
            )
        elif item["jira_key"] and jira_base_url:
            item["url"] = f"{jira_base_url.rstrip('/')}/browse/{item['jira_key']}"
        items.append(item)


# Merge blockers -------------------------------------------------------------


def _attach_merge_blockers(
    primary_action: dict | None,
    secondary_actions: list[dict],
    resolution: Resolution,
    facts: PRFacts | None,
) -> None:
    """Attach merge_blockers to every Integrate action for the confirm-before-proceeding UX.

    The "primary Integrate never prompts" guarantee is structural, not incidental to
    which slot _get_actions() happens to put "integrate" in: it is keyed off
    resolution.action_id == "integrate", the exact condition under which
    resolve_next_action() assigns Integrate as the state's one primary action
    (PR_READY_TO_MERGE once merge conditions are satisfied, PR_REVIEW_EXHAUSTED once
    the address-review budget forces a human decision either way). Both
    primary_action and secondary_actions are still scanned when that condition is
    false, so a future state that maps "integrate" into either slot gets the confirm
    by default rather than losing it by default.

    facts is None when no PR data could be resolved at all (e.g. a label-only item
    whose last-run PR reference didn't resolve): attach an explicit blocker rather
    than an empty list, since an empty list claims the PR is known to be mergeable.
    """
    if resolution.action_id == "integrate":
        return
    blockers = list(resolution.merge_blockers)
    if facts is None:
        blockers = ["The PR's state could not be loaded, so whether it can merge is unknown."]
    actions = list(secondary_actions)
    if primary_action is not None:
        actions.append(primary_action)
    for action in actions:
        if action.get("id") == "integrate":
            action["merge_blockers"] = blockers


# Main entry point ------------------------------------------------------------


async def get_work_items(project_dir: Path | None = None) -> dict:
    """Assemble unified work items from all state sources.

    Returns: {items, running_count, slots_available, max_concurrent, agents_running, github_user, jira_display_name,
    api_health}
    api_health: {status: "ok"}, {status: "unreachable", detail, down_seconds},
    or {status: "rate_limited", detail, cooldown_seconds, hits}
    """
    from sova.config.context import resolve_project_dir
    from sova.dashboard.services.agent_pool import _get_project_agents, get_default_project_dir

    project_dir = resolve_project_dir(project_dir, default_project_dir=get_default_project_dir())

    slug = None
    if project_dir:
        slug = project_dir.name

    try:
        cfg = load_config(project_dir)
        external_reviews_enabled = cfg.external_reviews.enabled
        max_address_cycles = cfg.pipeline.max_address_review_cycles
    except Exception:  # noqa: BLE001 (work items still render with default settings when config is unavailable)
        log.warning("work_items.config_load_failed", project_dir=str(project_dir), exc_info=True)
        cfg = None
        external_reviews_enabled = True
        # 0 disables the review_budget_exhausted rule: fail open to the
        # pre-budget-cap routing rather than inventing an exhausted state.
        max_address_cycles = 0

    (queue, fetched_prs, handoffs, agents_data), graph = await asyncio.gather(
        _fetch_all_sources(project_dir=project_dir, slug=slug, cfg=cfg),
        _load_graph_or_none(project_dir),
    )
    last_run_prs = [pr for task in queue if (pr := (task.get("last_run") or {}).get("pr_number"))]
    open_pr_numbers = await confirm_open_pr_numbers(known_open_pr_numbers(fetched_prs, cfg), last_run_prs, cfg)
    prs = fetched_prs if fetched_prs is not None else []
    if cfg and cfg.task_source.type == "jira" and cfg.task_source.jira_project_key:
        prs = _jira_project_prs(prs, cfg.task_source.jira_project_key)

    running_by_issue = _index_running_agents(agents_data)
    handoffs_by_issue = _index_handoffs(handoffs)
    prs_by_issue = _index_prs_by_issue(prs)

    unlinked_prs = [pr for pr in prs if not pr.get("linked_issue")]
    verdicts_by_issue = await _fetch_sova_verdicts(prs_by_issue, unlinked_prs=unlinked_prs, project_dir=project_dir)

    linked_issue_numbers: set[str] = set()

    items: list[dict] = []
    for task in queue:
        issue_num = str(task["issue"])
        pr_data = prs_by_issue.get(issue_num)
        if pr_data:
            linked_issue_numbers.add(issue_num)
        items.append(
            _build_task_item(
                task,
                pr_data,
                running_by_issue.get(issue_num),
                handoffs_by_issue.get(issue_num),
                sova_verdict=verdicts_by_issue.get(issue_num) if pr_data else None,
                external_reviews_enabled=external_reviews_enabled,
                max_address_cycles=max_address_cycles,
                suitability=_classify_queue_item(task, graph),
                open_pr_numbers=open_pr_numbers,
            )
        )

    _append_standalone_pr_items(
        items,
        prs,
        linked_issue_numbers,
        running_by_issue,
        handoffs_by_issue,
        verdicts_by_issue=verdicts_by_issue,
        graph=graph,
        jira_base_url=cfg.task_source.jira_base_url if cfg and cfg.task_source.type == "jira" else "",
        is_jira=bool(cfg and cfg.task_source.type == "jira"),
        external_reviews_enabled=external_reviews_enabled,
        max_address_cycles=max_address_cycles,
    )

    _sort_items(items)

    running_count = sum(1 for i in items if i["state"] == WorkItemState.AGENT_RUNNING)
    max_concurrent, agents_running = _pool_limit_and_running(
        _get_project_agents(slug), running_count, cfg.max_parallel_agents if cfg else 3
    )

    github_user = cfg.github_user if cfg else ""

    api_health: dict[str, object] = {"status": "ok"}
    try:
        from sova.supervisor.github_quota import get_github_quota_status
        from sova.supervisor.network_health import get_connectivity_status

        # Unreachable is checked first: an outage supersedes throttling, and
        # during one the rate-limit cooldown is not what the operator needs to
        # know about. A throttled call reached GitHub; an unreachable one did not.
        net_status = get_connectivity_status()
        gh_status = get_github_quota_status(github_user)
        if net_status.is_down:
            api_health = {
                "status": "unreachable",
                "detail": "No connection to GitHub or the LLM provider. Agent spawning is paused.",
                "down_seconds": round(net_status.down_for_seconds),
            }
        elif gh_status.is_limited:
            api_health = {
                "status": "rate_limited",
                "detail": "GitHub API rate limit exceeded. Data may be stale.",
                "cooldown_seconds": round(gh_status.cooldown_remaining_seconds),
                "hits": gh_status.hits_in_window,
            }
    except Exception:  # noqa: BLE001 (quota banner is optional context)
        log.debug("work_items.quota_status_failed", exc_info=True)

    jira_display_name = cfg.task_source.jira_display_name if cfg else ""

    return {
        "items": items,
        "running_count": running_count,
        "slots_available": max(0, max_concurrent - agents_running),
        "max_concurrent": max_concurrent,
        "agents_running": agents_running,
        "github_user": github_user,
        "jira_display_name": jira_display_name,
        "api_health": api_health,
        # How old the graph the classification came from is, and whether its rebuild failed (None without one).
        "graph": graph_age_meta(project_dir),
    }


async def build_work_items(project_dir: Path | None) -> dict:
    """One full build of the work items: the snapshot service's builder (``get_work_items`` under another name)."""
    return await get_work_items(project_dir)


# Fields that advance with the clock rather than with a change, so they stay out of the change digest: a
# rebuild whose only difference is an age must not tell every client to refetch.
_VOLATILE_ITEM_KEYS = ("elapsed_seconds", "age_seconds", "graph_age_seconds")
_VOLATILE_API_HEALTH_KEYS = ("down_seconds", "cooldown_seconds")


def normalize_work_items_for_digest(payload: dict) -> dict:
    """A copy of a ``get_work_items()`` payload without its clock-driven fields, for change detection only."""

    def strip(value: object) -> object:
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k not in _VOLATILE_ITEM_KEYS}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    out: dict = strip(payload)  # type: ignore[assignment]  # a dict in, so a dict out
    health = out.get("api_health")
    if isinstance(health, dict):
        out["api_health"] = {k: v for k, v in health.items() if k not in _VOLATILE_API_HEALTH_KEYS}
    return out


# Next tasks ------------------------------------------------------------------


def ranked_task_payload(ranked: RankedTask) -> dict:
    """The entry shape shared by ``/agents/next-tasks`` and ``/dependencies/ready``."""
    task = ranked.task
    state = task.state.value
    return {
        "id": int(task.id),
        "title": task.title,
        "state": state,
        "state_label": state_label(state),
        "state_color": state_color(state),
        "milestone": task.milestone,
        "url": task.url,
        "priority": task.priority.value,
        "mode": ranked.mode.value,
        "rank": ranked.rank,
        "why": ranked.why,
    }


def _graph_tasks(graph: DependencyGraph) -> list[Task]:
    """The candidate pool for ranking: the issues the graph was built for.

    A milestone-filtered graph also holds the out-of-milestone dependencies it fetched
    individually (so blockers classify), and those are not part of the requested milestone.
    The graph records what its fetch returned, so no milestone string is compared here: the
    adapter decides what a milestone value matches (a number or a title, in any case).
    """
    return [task for issue in graph.requested_issues if (task := graph.get_task(issue)) is not None]


def rank_graph_entries(
    graph: DependencyGraph,
    *,
    running: Collection[int],
    open_prs: Collection[int],
    limit: int | None = None,
) -> list[dict]:
    """Ranked next-task entries for the issues ``graph`` was built for (``limit=None`` is unbounded).

    For a milestone-filtered graph the pool is that milestone's issues, but ``unblocks`` still
    counts dependents over the graph given, which is that milestone's subgraph, so it can be
    lower than the same issue's count in the full graph.
    """
    ranked = rank_next_tasks(_graph_tasks(graph), graph, running=running, open_prs=open_prs, limit=limit)
    return [ranked_task_payload(r) for r in ranked]


def _parse_issue_number(raw: object) -> int | None:
    """An issue reference such as ``507``, ``"507"`` or ``"#507"`` as an int; None when it is not one."""
    text = "" if raw is None else str(raw).strip().removeprefix("#")
    return int(text) if text.isascii() and text.isdigit() else None


def _running_issues(agents_data: dict) -> set[int]:
    """Every issue a running agent works on, however its issue string is written."""
    numbers = (_parse_issue_number(agent.get("issue")) for agent in agents_data.get("agents", []))
    return {n for n in numbers if n is not None}


def _open_pr_issues(prs: list[dict]) -> set[int]:
    """Every issue an open PR links to, not only its primary ``linked_issue``.

    A PR closing several issues covers all of them, so none may be offered as "Up next".
    """
    issues: set[int] = set()
    for pr in prs:
        issues.update(pr.get("linked_issues") or [])
        if pr.get("linked_issue") is not None:
            issues.add(pr["linked_issue"])
    return issues


def _jira_project_prs(prs: list[dict], project_key: str) -> list[dict]:
    """Keep PRs with this Jira project's full key and use that key as their issue link.

    A shared GitHub repository may contain PRs for several Jira projects. A numeric suffix or an
    unlinked PR cannot identify the project, so neither can become a card or exclude an Up next issue.
    """
    prefix = f"{project_key.upper()}-"
    scoped = []
    for pr in prs:
        key = (pr.get("linked_jira_key") or "").upper()
        if not key.startswith(prefix):
            continue
        suffix = key.removeprefix(prefix)
        if not suffix.isascii() or not suffix.isdigit() or len(suffix) > 100:
            continue
        issue = int(suffix)
        scoped.append({**pr, "linked_issue": issue, "linked_issues": [issue]})
    return scoped


async def next_task_exclusions(project_dir: Path | None) -> tuple[set[int], set[int]]:
    """The issues the ranker must skip: ``(running, open_prs)``, from the explicit project.

    The one source both ``GET /agents/next-tasks`` and ``GET /dependencies/ready`` read, so
    only the shared ranker decides what they return. Both lookups fail open (any error
    degrades to no exclusions and is logged), leaving the ``triaged``/``researched`` state
    filter as the only exclusion.
    """
    running, open_prs, _by_issue = await board_exclusions(project_dir)
    return running, open_prs


async def board_exclusions(project_dir: Path | None) -> tuple[set[int], set[int], dict[int, set[int]] | None]:
    """``next_task_exclusions()`` plus each issue's open PR numbers, from one open-PR fetch.

    The third item counts every issue a PR links (a work item carries only the PR of its primary
    ``linked_issue``), and is ``None`` when the PR source is down, so a lookup that failed never reads as
    "no open PRs". The Work board's Done today settlement reads it; sharing the fetch keeps the exclusion
    set and that map from disagreeing within one board build.
    """
    from sova.config.context import resolve_project_dir
    from sova.dashboard.services.agent_pool import get_default_project_dir

    project_dir = resolve_project_dir(project_dir, default_project_dir=get_default_project_dir())
    slug = project_dir.name
    prs, agents_data = await asyncio.gather(_scoped_open_prs(project_dir), _safe_agents(slug))
    return _running_issues(agents_data), _open_pr_issues(prs or []), _open_prs_by_issue(prs)


def _open_prs_by_issue(prs: list[dict] | None) -> dict[int, set[int]] | None:
    """Each issue's open PR numbers, counting every issue a PR links; ``None`` when *prs* is ``None``."""
    if prs is None:
        return None
    by_issue: dict[int, set[int]] = {}
    for pr in prs:
        if pr.get("number") is None:
            continue
        for issue in _open_pr_issues([pr]):
            by_issue.setdefault(issue, set()).add(pr["number"])
    return by_issue


async def _scoped_open_prs(project_dir: Path) -> list[dict] | None:
    """Open PRs scoped to a Jira project's own keys when the tracker is Jira; ``None`` when unavailable."""
    prs = await _safe_prs(project_dir)
    try:
        cfg = load_config(project_dir)
        if prs is not None and cfg.task_source.type == "jira" and cfg.task_source.jira_project_key:
            prs = _jira_project_prs(prs, cfg.task_source.jira_project_key)
    except Exception:  # noqa: BLE001 (open PR lookups fail open when config is unavailable)
        log.warning("work_items.exclusions_config_failed", exc_info=True)
        return None
    return prs


async def get_next_tasks(project_dir: Path | None = None, *, limit: int | None = None) -> dict:
    """Rank the issues an agent should pick up next: ``{tasks, unclassified_count}``.

    ``unclassified_count`` covers the unclassified issues in the ranker's own pool
    (triaged or researched, no running agent, no open PR), the ones that join
    ``tasks`` once they gain ``agent:ready``. Local assessment records are not read.

    A graph failure raises (``GraphConfigError`` for config or adapter problems):
    an empty list means "nothing to do", so a failure must not look like one. The
    graph is the build the work-items poll shares (see ``load_ranking_graph()``). The
    running-agent and open-PR sources fail open, degrading to state-only filtering.
    """
    graph = await load_ranking_graph(project_dir)
    running, open_prs = await next_task_exclusions(project_dir)
    ranked, unclassified_count = rank_and_count_next_tasks(
        _graph_tasks(graph), graph, running=running, open_prs=open_prs, limit=limit
    )
    return {"tasks": [ranked_task_payload(r) for r in ranked], "unclassified_count": unclassified_count}


# Fetch helpers ---------------------------------------------------------------


async def _safe_prs(project_dir: Path | None) -> list[dict] | None:
    """Open PRs with computed state; an unavailable source degrades to ``None``, not an empty list.

    ``None`` keeps a failed fetch apart from "no open PRs", so a caller never reads an outage as
    every PR having been merged. Callers that only need the PRs treat ``None`` as no PRs.
    """
    from sova.dashboard.services.pr_service import list_open_prs_with_state

    try:
        return await list_open_prs_with_state(project_dir, raise_on_error=True)
    except Exception:  # noqa: BLE001 (aggregate endpoint must not fail if one source is down)
        log.warning("work_items.prs_failed", exc_info=True)
        return None


async def _safe_agents(slug: str | None) -> dict:
    """Running and completed agents; an unavailable source degrades to none."""
    from sova.dashboard.services.agent_lifecycle import get_unified_agents

    try:
        return await get_unified_agents(slug)
    except Exception:  # noqa: BLE001 (aggregate endpoint must not fail if one source is down)
        log.warning("work_items.agents_failed", exc_info=True)
        return {"agents": [], "completed": []}


async def _fetch_all_sources(
    *,
    project_dir: Path | None,
    slug: str | None,
    cfg: "ProjectConfig | None" = None,
) -> tuple[list[dict], list[dict] | None, list[dict], dict]:
    """Fetch queue, PRs (``None`` when unavailable), handoffs, and running agents concurrently."""
    from sova.dashboard.services.handoff_service import get_all_handoffs
    from sova.dashboard.services.queue_service import get_priority_queue

    async def safe_queue() -> list[dict]:
        try:
            return await get_priority_queue(project_dir, open_prs=prs_task, cfg=cfg)
        except Exception:  # noqa: BLE001 (aggregate endpoint must not fail if one source is down)
            log.warning("work_items.queue_failed", exc_info=True)
            return []

    def safe_handoffs() -> list[dict]:
        try:
            return get_all_handoffs(project_dir)
        except Exception:  # noqa: BLE001 (aggregate endpoint must not fail if one source is down)
            log.warning("work_items.handoffs_failed", exc_info=True)
            return []

    # The queue awaits this same fetch to judge last-run PR numbers, so one poll lists open PRs once.
    prs_task = asyncio.create_task(_safe_prs(project_dir))
    queue_task = asyncio.create_task(safe_queue())
    agents_task = asyncio.create_task(_safe_agents(slug))

    handoffs = safe_handoffs()
    queue = await queue_task
    prs = await prs_task
    agents_data = await agents_task

    return queue, prs, handoffs, agents_data


def _index_running_agents(agents_data: dict) -> dict[str, dict]:
    """Index running agents by issue number (or pr:<number> for standalone PRs)."""
    result: dict[str, dict] = {}
    for agent in agents_data.get("agents", []):
        issue = str(agent.get("issue") or "")
        if issue and issue.isdigit():
            result[issue] = agent
        pr_num = agent.get("pr_number")
        if pr_num is not None:
            result[f"pr:{pr_num}"] = agent
    return result


def _index_handoffs(handoffs: list[dict]) -> dict[str, dict]:
    """Index handoffs by issue number (or pr:<number> for standalone PRs), keeping only awaiting_action."""
    result: dict[str, dict] = {}
    for h in handoffs:
        if h.get("status") != "awaiting_action":
            continue
        issue = str(h.get("issue", ""))
        if issue and issue.isdigit():
            result[issue] = h
        pr_num = h.get("pr_number")
        if pr_num:
            result[f"pr:{pr_num}"] = h
    return result


def _pool_limit_and_running(pa: ProjectAgents | None, running_count: int, configured_limit: int = 3) -> tuple[int, int]:
    """Return the agent limit and the pool's running count, the number start_agent() compares with the limit and
    the resource widget shows. Without a pool, the limit is the configured ``max_parallel_agents`` (3 when no config
    loaded) and the count is the items in AGENT_RUNNING; the stepper seeds from this, so it must be the stored value."""
    if pa is None:
        return configured_limit, running_count
    return pa.max_concurrent, len(pa.agents)


def _index_prs_by_issue(prs: list[dict]) -> dict[str, dict]:
    """Index PRs by linked issue number."""
    result: dict[str, dict] = {}
    for pr in prs:
        linked = pr.get("linked_issue")
        if linked is not None:
            result[str(linked)] = pr
    return result


# Formatters ------------------------------------------------------------------


def _format_running_agent(agent: dict) -> dict:
    role = agent.get("role", "")
    return {
        "run_id": agent.get("run_id"),
        "role": role,
        "role_label": run_action(role, agent.get("pipeline_variant")).running,
        "elapsed_seconds": agent.get("elapsed_seconds", 0),
    }


def _format_sova_context(sova_verdict: dict | None) -> dict:
    """Extract the SOVA review context needed by the LLM suggestion service."""
    if not sova_verdict:
        return {"has_sova_review": False, "verdict": None}
    ctx: dict = {
        "has_sova_review": bool(sova_verdict.get("has_sova_review", False)),
        "verdict": sova_verdict.get("verdict"),
    }
    finding_summary = sova_verdict.get("finding_summary")
    if finding_summary is not None:
        ctx["finding_summary"] = finding_summary
    return ctx


def _format_pr_details(pr: dict) -> dict:
    computed = pr.get("computed_state", "")
    return {
        "number": pr.get("number"),
        "computed_state": computed,
        "ci_status": pr.get("ci_status", ""),
        "review_decision": pr.get("review_decision", ""),
        # A PR with no computed state has no label to show: "Unknown" here would read as a real state.
        "state_label": state_label(computed) if computed else "",
        "state_color": state_color(computed) if computed else "",
        "url": pr.get("url", ""),
        "mergeable": pr.get("mergeable", ""),
        "merge_state": pr.get("merge_state", ""),
        "author": pr.get("author", ""),
        "age_seconds": pr.get("age_seconds", 0),
        "is_draft": pr.get("is_draft", False),
        "additions": pr.get("additions", 0),
        "deletions": pr.get("deletions", 0),
        "changed_files": pr.get("changed_files", 0),
        "thread_total": pr.get("thread_total", 0),
        "thread_resolved": pr.get("thread_resolved", 0),
        "review_logins": pr.get("review_logins", []),
        "assignees": pr.get("assignees", []),
        "updated_at": pr.get("updated_at", ""),
        "commit_count": pr.get("commit_count", 0),
    }


def _build_item(**kwargs: object) -> dict:
    state: WorkItemState = kwargs["state"]  # type: ignore[assignment]
    return {
        "issue_number": kwargs["issue_number"],
        "pr_number": kwargs["pr_number"],
        "title": kwargs["title"],
        "url": kwargs["url"],
        "state": state.value,
        "state_label": state_label(state),
        "state_color": state_color(state),
        "primary_action": kwargs["primary_action"],
        "secondary_actions": kwargs["secondary_actions"],
        "handoff_actions": kwargs["handoff_actions"],
        "handoff_summary": kwargs.get("handoff_summary", ""),
        "running_agent": kwargs["running_agent"],
        "pr_details": kwargs["pr_details"],
        "sova_context": kwargs.get("sova_context") or {"has_sova_review": False, "verdict": None},
        "reason_chain": kwargs.get("reason_chain") or [],
        "matched_reason": kwargs.get("matched_reason") or "",
        "labels": kwargs["labels"],
        "priority": kwargs["priority"],
        "priority_label": kwargs["priority_label"],
        "issue_priority": kwargs["issue_priority"],
        "last_run": kwargs["last_run"],
        "created_at": kwargs["created_at"],
        "assignees": kwargs["assignees"],
        "jira_key": kwargs["jira_key"],
        "issue_type": kwargs.get("issue_type", ""),
        "last_failed": kwargs.get("last_failed", False),
        "story_points": kwargs.get("story_points"),
        "sprint": kwargs.get("sprint", ""),
        "components": kwargs.get("components", []),
        "jira_status": kwargs.get("jira_status", ""),
        "jira_priority": kwargs.get("jira_priority", ""),
        "updated_at": kwargs.get("updated_at", ""),
        # None for a standalone PR item: there is no issue to classify.
        "suitability": kwargs.get("suitability"),
    }
