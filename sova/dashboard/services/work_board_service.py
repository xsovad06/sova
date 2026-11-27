"""Work board service: every issue in exactly one column, assembled from the sources the dashboard already has.

``build_board()`` composes the work items (``get_work_items()``), the "Up next" ranking
(``load_ranking_graph()`` + ``rank_graph_entries()``), the supervisor queue, the active runs and the
dependency graph. The stage of each issue comes from ``sova.work.state.work_stage()``, the one place the stage
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

from sova.adapters.base import Task, TaskState
from sova.config.db_loader import load_hidden_issues, load_task_queue, remove_from_task_queue
from sova.config.loader import load_config
from sova.dashboard.services.snapshot_service import (
    Snapshot,
    get_snapshot,
    invalidate_snapshot,
    is_building,
    peek_snapshot,
    request_refresh,
    wait_for_refresh,
)
from sova.dashboard.services.work_item_service import (
    _parse_issue_number,
    board_exclusions,
    build_work_items,
    get_work_items,
    graph_age_meta,
    load_ranking_graph,
    normalize_work_items_for_digest,
    rank_graph_entries,
)
from sova.db.session import get_session
from sova.git.pr import list_merged_prs_since
from sova.supervisor.dependency_graph import GRAPH_CACHE_TTL
from sova.supervisor.labels import is_epic
from sova.supervisor.queue_maintenance import prune_closed
from sova.supervisor.suitability import assess_suitability, unblock_count, unsatisfied_dependencies
from sova.utils.logging import get_logger
from sova.work.state import (
    YOURS_MODES,
    WorkItemState,
    WorkStage,
    stage_color,
    stage_label,
    start_action,
    state_color,
    state_label,
    work_stage,
)

if TYPE_CHECKING:
    from sova.supervisor.dependency_graph import DependencyGraph

log = get_logger(component="dashboard.work_board")

UP_NEXT_LIMIT = 5

# A queue lookup asks the tracker once per issue, and `gh` alone defaults to 300 s: one stalled call must not hold
# the board, so a lookup that is not answered in this long leaves its issue unknown (the queue entry stays).
_QUEUE_LOOKUP_TIMEOUT = 5.0

# One `gh pr list` per window, not per board refresh: the board reloads on every live-update event.
_MERGED_CACHE_TTL = 60.0
# How long after a merge the cached dependency graph may still show the issue open: its cache TTL, plus a margin
# for the build time (the cache entry is stamped when a build finishes, but the data reflects its start).
_GRAPH_LAG_SECONDS = GRAPH_CACHE_TTL + 60.0
_merged_cache: dict[tuple[str, date], tuple[float, list[dict]]] = {}
# Invalidations per resolved project: a merged-PR fetch caches its answer only when no invalidation ran while it was
# in flight, so a fetch that started before a merge cannot store the pre-merge list for another TTL.
_merged_generations: dict[str, int] = {}
_done_assignees_cache: dict[tuple[str, str], tuple[float, list[str]]] = {}

# Board snapshots (#1534): GET /work/board answers from the last build of its (project, milestone, owner) key. A key
# asked for within this long is rebuilt when its sources change; an older one waits for its next request.
_BOARD_RECENT_SECONDS = 600.0
_board_requests: dict[tuple[str, str, str], float] = {}  # (resolved project, milestone, owner) -> last request
# Keys kept per project: milestone is a free-form query value, so the least recently requested key (and its
# snapshot) is dropped past this many rather than letting arbitrary values grow the maps and their rebuilds.
_BOARD_MAX_KEYS_PER_PROJECT = 8
# How long a writer (hide, queue, priority) waits for the board rebuild its write requested. The write is already
# durable, and the writer's tab still hears ``work_items_changed`` when a slower rebuild lands, so a slow or hung
# tracker never holds the request for a whole build.
WRITER_WAIT_SECONDS = 5.0


def reset_board_cache() -> None:
    """Forget the cached merged-PR lists and the requested board keys. Test-only, like ``reset_graph_load_state()``."""
    _merged_cache.clear()
    _merged_generations.clear()
    _done_assignees_cache.clear()
    _board_requests.clear()


def _merged_generation_key(project_dir: Path | None) -> str:
    return str(project_dir.resolve()) if project_dir is not None else ""


def invalidate_merged_cache(project_dir: Path) -> None:
    """Drop a project's cached merged-PR list, so the next board build sees a merge that just happened.

    Also counts the invalidation, so a fetch already in flight (which may predate the merge) is not cached.
    """
    generation_key = _merged_generation_key(project_dir)
    _merged_generations[generation_key] = _merged_generations.get(generation_key, 0) + 1
    prefix = f"{project_dir}:"
    resolved_prefix = f"{project_dir.resolve()}:"
    for key in [k for k in _merged_cache if k[0].startswith((prefix, resolved_prefix))]:
        _merged_cache.pop(key, None)


def _board_kind(milestone: str, owner: str) -> str:
    return f"board:{owner}:{milestone}"


def _resolve_project(project_dir: Path | None) -> Path:
    """The project a board is built for: the request's, else the default one (``build_board()``'s own rule)."""
    from sova.config.context import resolve_project_dir
    from sova.dashboard.services.agent_pool import get_default_project_dir

    return resolve_project_dir(project_dir, default_project_dir=get_default_project_dir()).resolve()


def normalize_board_for_digest(payload: dict) -> dict:
    """A board payload without its clock-driven fields (ages, elapsed times), for change detection only."""
    out = normalize_work_items_for_digest(payload)

    def strip(value: object) -> object:
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k != "elapsed_formatted"}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    return strip(out)  # type: ignore[return-value]  # a dict in, so a dict out


async def get_board_snapshot(project_dir: Path | None, milestone: str = "", *, owner: str = "all") -> Snapshot:
    """The last board built for this key, at once; only the first request for a key waits for the build.

    A snapshot older than the default snapshot TTL starts a background rebuild, and a rebuild that changes the
    payload sends ``work_items_changed`` through the snapshot change listener.
    """
    project_dir = _resolve_project(project_dir)
    _board_requests[(str(project_dir), milestone, owner)] = time.monotonic()
    _evict_excess_keys(project_dir)
    return await get_snapshot(
        _board_kind(milestone, owner),
        project_dir,
        lambda: build_board(project_dir, milestone, owner=owner),
        normalize=normalize_board_for_digest,
    )


def _evict_excess_keys(project_dir: Path) -> None:
    """Drop this project's least recently requested board keys (and their snapshots) past the per-project cap."""
    target = str(project_dir)
    keys = sorted((k for k in _board_requests if k[0] == target), key=_board_requests.__getitem__)
    for project, milestone, owner in keys[: max(0, len(keys) - _BOARD_MAX_KEYS_PER_PROJECT)]:
        _board_requests.pop((project, milestone, owner), None)
        invalidate_snapshot(_board_kind(milestone, owner), project_dir)


def keep_board_keys_alive(project_dir: Path | None) -> None:
    """Mark this project's requested board keys as just requested, while a visible Work tab shows one of them.

    An open Work tab requests the board only when told it changed, so with nothing changing its key would lapse
    after ``_BOARD_RECENT_SECONDS`` and the next run transition or probe change would rebuild nothing and tell no
    socket. The status socket's producer calls this every tick a visible Work tab is connected.
    """
    target = str(_resolve_project(project_dir))
    now = time.monotonic()
    for key in [k for k in _board_requests if k[0] == target]:
        _board_requests[key] = now


def board_response(snapshot: Snapshot) -> dict:
    """A board snapshot as served now: its running times advanced by the snapshot's age, plus its ``snapshot`` meta.

    The page's running timers start from ``elapsed_seconds``, which would otherwise lag by the age of the build;
    ``elapsed_formatted`` is recomputed from the advanced seconds so the two never disagree.
    """
    from sova.dashboard.services.work_service import _format_seconds

    age = int(time.monotonic() - snapshot.built_at)
    payload = dict(snapshot.payload)

    def advanced(entry: object) -> object:
        if not isinstance(entry, dict) or not isinstance(entry.get("elapsed_seconds"), int):
            return entry
        elapsed = entry["elapsed_seconds"] + age
        out = {**entry, "elapsed_seconds": elapsed}
        if "elapsed_formatted" in entry:
            out["elapsed_formatted"] = _format_seconds(elapsed)
        return out

    for key in ("running", "other_runs"):
        entries = payload.get(key)
        if age and isinstance(entries, list):
            payload[key] = [advanced(entry) for entry in entries]
    return {**payload, "snapshot": snapshot.meta()}


async def board_rebuilds_settled(project_dir: Path | None) -> None:
    """Wait for this project's board rebuilds in flight, for a write the board must show at once (hide, queue).

    The writer calls ``refresh_board_snapshots()`` (or a service that does) and then this, outside any lock, so its
    reload right after the request returns is answered from a board that read the write. Every other open Work tab
    keeps answering from the previous board meanwhile, and hears ``work_items_changed`` once the rebuilt board
    differs: no tab but the writer's waits on the tracker. The wait is bounded by ``WRITER_WAIT_SECONDS``: past it
    the writer reloads the previous board, and its ``work_items_changed`` brings the rebuilt one when it lands.
    """
    project_dir = _resolve_project(project_dir)
    target = str(project_dir)
    kinds = [_board_kind(milestone, owner) for project, milestone, owner in _board_requests if project == target]
    if not kinds:
        return
    try:
        # wait_for_refresh() never cancels the shared build, so giving up here leaves every rebuild running.
        await asyncio.wait_for(
            asyncio.gather(*(wait_for_refresh(kind, project_dir) for kind in kinds)), timeout=WRITER_WAIT_SECONDS
        )
    except TimeoutError:
        log.info("work_board.writer_wait_timed_out", project_dir=target, timeout=WRITER_WAIT_SECONDS)


def refresh_work_snapshots(project_dir: Path) -> bool:
    """Rebuild this project's work-items snapshot and its recent board snapshots in the background.

    Called after the sources they read were invalidated. A snapshot that was never built and is not building is
    left to its first request; a first build in flight runs once more, since it may have read the old sources. A
    change reaches the sockets as ``work_items_changed``. Returns whether any rebuild was requested, so a caller
    whose change no snapshot will report can tell the sockets itself.
    """
    requested = False
    if peek_snapshot("work_items", project_dir) is not None or is_building("work_items", project_dir):
        request_refresh(
            "work_items", project_dir, lambda: build_work_items(project_dir), normalize=normalize_work_items_for_digest
        )
        requested = True
    return refresh_board_snapshots(project_dir) or requested


def refresh_board_snapshots(project_dir: Path | None) -> bool:
    """Rebuild, in the background, every board snapshot of this project that was asked for recently.

    The previous snapshots keep answering until each rebuild lands, and a rebuilt board that differs sends
    ``work_items_changed``. Also the refresh after a write the board must show (hide, queue, priority), whose
    writer then awaits ``board_rebuilds_settled()``. Returns whether any rebuild was requested.
    """
    project_dir = _resolve_project(project_dir)
    target = str(project_dir)
    cutoff = time.monotonic() - _BOARD_RECENT_SECONDS
    requested = False
    lapsed: list[tuple[str, str, str]] = []
    for (project, milestone, owner), requested_at in _board_requests.items():
        if project != target:
            continue
        kind = _board_kind(milestone, owner)
        if requested_at < cutoff:
            lapsed.append((project, milestone, owner))
            continue
        if peek_snapshot(kind, project_dir) is None and not is_building(kind, project_dir):
            continue  # never built, or dropped: the next request builds it
        # A first build in flight is refreshed too: it may have read the sources before they changed.
        request_refresh(
            kind,
            project_dir,
            lambda m=milestone, o=owner: build_board(project_dir, m, owner=o),
            normalize=normalize_board_for_digest,
        )
        requested = True
    for project, milestone, owner in lapsed:
        # Dropped with its key: this function finds a snapshot only through a key, so one left behind would answer
        # the next request from before a hide or queue write that could no longer refresh it.
        _board_requests.pop((project, milestone, owner), None)
        invalidate_snapshot(_board_kind(milestone, owner), project_dir)
    return requested


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
        "start": start_action(state, number),
    }


def _item_mode(item: dict) -> str | None:
    return (item.get("suitability") or {}).get("mode")


def _item_run_status(item: dict) -> str | None:
    return (item.get("last_run") or {}).get("status")


def _item_entry(item: dict, stage: WorkStage) -> dict:
    entry = _issue_entry(
        _parse_issue_number(item.get("issue_number")),
        title=item.get("title", ""),
        url=item.get("url", ""),
        stage=stage,
        state=item.get("state", ""),
        priority=item.get("issue_priority", ""),
    )
    entry.update(_item_extras(item))
    return entry


def _item_extras(item: dict) -> dict:
    """What a work item adds to its entry: agent fit, the issue's creation time, its open PR, last run and actions."""
    return {
        "mode": _item_mode(item),
        "created_at": item.get("created_at") or None,
        "pr": _pr_summary(item),
        "last_run": _last_run_summary(item),
        "actions": _item_actions(item),
        "assignees": item.get("assignees") or [],
        "jira_key": item.get("jira_key") or "",
    }


def _entry_is_mine(entry: dict, github_user: str, jira_display_name: str) -> bool:
    assignees = {name.casefold() for name in entry.get("assignees") or []}
    pr = entry.get("pr") or {}
    pr_assignees = {name.casefold() for name in pr.get("assignees") or entry.get("pr_assignees") or []}
    github = github_user.casefold()
    jira = jira_display_name.casefold()
    if entry.get("jira_key") and jira and jira in assignees:
        return True
    return bool(
        github
        and (
            (not entry.get("jira_key") and github in assignees)
            or github in pr_assignees
            or github == (pr.get("author") or entry.get("pr_author") or "").casefold()
        )
    )


# The handlers the Work page can run. A work-item action with another handler is left out, never shown dead.
_RUNNABLE_HANDLERS = frozenset({"start_agent", "run_command", "trigger_rebase"})
_ACTION_KEYS = ("id", "label", "style", "handler", "handler_args", "merge_blockers")


def _item_actions(item: dict) -> list[dict]:
    """The work item's own actions for its state (sova.work.state.get_actions), primary first, as the drawer lists
    them.

    Each keeps ``id, label, style, handler, handler_args`` and, on an Integrate action, ``merge_blockers`` (the
    unmet merge conditions the page confirms before running it).
    """
    actions = [item.get("primary_action"), *(item.get("secondary_actions") or [])]
    return [
        {key: action[key] for key in _ACTION_KEYS if key in action}
        for action in actions
        if isinstance(action, dict) and action.get("handler") in _RUNNABLE_HANDLERS
    ]


def _pr_summary(item: dict) -> dict | None:
    """The linked PR as the card and drawer show it, or None when the issue has no open PR."""
    pr = item.get("pr_details") or {}
    number = pr.get("number") or item.get("pr_number")
    if not number:
        return None
    return {
        "number": number,
        "url": pr.get("url", ""),
        "state": pr.get("computed_state", ""),
        "state_label": pr.get("state_label", ""),
        "state_color": pr.get("state_color", ""),
        "ci_state": pr.get("ci_status", ""),
        "mergeable": pr.get("mergeable", ""),
        "merge_state": pr.get("merge_state", ""),
        "is_draft": bool(pr.get("is_draft")),
        "threads_resolved": pr.get("thread_resolved", 0),
        "threads_total": pr.get("thread_total", 0),
        "additions": pr.get("additions", 0),
        "deletions": pr.get("deletions", 0),
        "changed_files": pr.get("changed_files", 0),
        "author": pr.get("author", ""),
        "assignees": pr.get("assignees") or [],
    }


def _last_run_summary(item: dict) -> dict | None:
    """The issue's latest run (id, role, status, when it ended), or None when it never ran."""
    run = item.get("last_run") or {}
    if not run.get("id"):
        return None
    return {
        "run_id": run["id"],
        "role": run.get("role", ""),
        "status": run.get("status", ""),
        "ended_at": run.get("ended_at"),
    }


def _fallback_entry(number: int, graph: DependencyGraph | None, stage: WorkStage) -> dict:
    """An entry for an issue the work items lack (closed, deleted or outside the open list), never raising."""
    task = graph.get_task(number) if graph is not None else None
    if task is None:
        return _issue_entry(number, title="", url="", stage=stage, state="", priority="")
    entry = _issue_entry(
        number, title=task.title, url=task.url, stage=stage, state=task.state.value, priority=task.priority.value
    )
    entry.update(assignees=list(task.assignees), jira_key=task.metadata.get("key", ""))
    return entry


def _reason_for_needs(state: str) -> tuple[str, str]:
    """(kind, reason) naming why an item with this state needs the user; a failed run is the remaining case."""
    if state == WorkItemState.PR_READY_TO_MERGE:
        return "merge", "Ready to merge"
    if state == WorkItemState.PR_REVIEW_EXHAUSTED:
        return "review_budget", "Review budget exhausted"
    if state == WorkItemState.SPEC_REVIEW:
        return "spec", "Spec awaiting approval"
    if state == WorkItemState.PR_MERGED_ISSUE_OPEN:
        return "stale_label", "PR no longer open, issue still labeled in review"
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


def _session_command(item: dict) -> str:
    """The command that opens an interactive session on the issue: the full workflow, with a person in it."""
    return f'claude "/develop-full {item.get("issue_number", "")}"'


def _running_details(item: dict, runs: dict[str, dict]) -> dict:
    agent = item.get("running_agent") or {}
    issue = _parse_issue_number(item.get("issue_number"))
    run = runs.get(f"issue:{issue}") if issue is not None else None
    run = run or runs.get(f"run:{agent.get('run_id')}") or {}
    same_agent = bool(run and agent.get("run_id") == run.get("id", agent.get("run_id")))
    role = (agent.get("role") if same_agent else None) or run.get("role")
    if not run:
        role = agent.get("role")
    return {
        "run_id": run.get("id") or agent.get("run_id"),
        "role": role,
        "step_name": run.get("current_step"),
        "step_index": run.get("step_index"),
        "total_steps": run.get("total_steps"),
        "ci_state": (item.get("pr_details") or {}).get("ci_status", ""),
        "elapsed_seconds": agent.get("elapsed_seconds") if same_agent else run.get("elapsed_seconds"),
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

    try:
        async with await get_session(project_dir=project_dir) as session, session.begin():
            runs = await get_active_work(session, project_dir)
    except Exception:  # noqa: BLE001 (the board still renders without per-run details)
        log.warning("work_board.active_runs_failed", exc_info=True)
        return None
    indexed: dict[str, dict] = {}
    for run in reversed(runs):  # get_active_work() is newest first, so the newest is written last
        indexed[f"run:{run['id']}"] = run
        issue = _parse_issue_number(run.get("issue_number"))
        if issue is not None:
            indexed[f"issue:{issue}"] = run
    return indexed


def _other_runs(runs: dict[str, dict] | None, running_entries: list[dict]) -> list[dict]:
    """Running runs no Running card shows (a planner run, or one on an issue the work items leave out), so each
    still has a View log and a Stop somewhere on the page. Oldest first, like the column."""
    on_cards = {entry.get("run_id") for entry in running_entries}
    seen: set[int] = set()
    others = []
    for run in (runs or {}).values():
        run_id = run.get("id")
        if run_id is None or run_id in seen or run_id in on_cards or run.get("status") != "running":
            continue
        seen.add(run_id)
        others.append(
            {
                "run_id": run_id,
                "issue_number": run.get("issue_number") or None,
                "pr_number": run.get("pr_number"),
                "role": run.get("role"),
                "pipeline_variant": run.get("pipeline_variant"),
                "run_label": run.get("run_label") or "",
                "step_name": run.get("current_step"),
                "elapsed_seconds": run.get("elapsed_seconds"),
                "started_at": run.get("started_at"),
            }
        )
    return sorted(others, key=lambda r: r["started_at"] or "")


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
    """PRs merged since local midnight, newest first, one entry per issue a PR links; ``None`` when unavailable.

    A PR that links no issue has an entry with no number. Every merge is listed, so ``_settle_done_today()``
    decides per issue which entry stays. Each entry carries a private ``_pr_url``, and ``_closes`` (the PR's
    closing references name the issue), that function removes. A PR merged at exactly midnight belongs to the
    new day. The merged list is cached for ``_MERGED_CACHE_TTL`` seconds, so a failure is never cached as an
    empty day.
    """
    from sova.work.pr_state import closing_references, extract_all_linked_issues, extract_jira_key

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
            generation_key = _merged_generation_key(project_dir)
            generation = _merged_generations.get(generation_key, 0)
            merged = await list_merged_prs_since(repo=cfg.github_repo, since=since_day, github_user=cfg.github_user)
            for stale in [key for key in _merged_cache if key[1] != since_day]:
                del _merged_cache[stale]  # an earlier day is never asked for again
            if _merged_generations.get(generation_key, 0) == generation:
                # Not cached when a merge invalidated the list during the fetch: this answer may predate it.
                _merged_cache[cache_key] = (time.monotonic(), merged)
    except Exception:  # noqa: BLE001 (done today is optional context; reported as unavailable)
        log.warning("work_board.merged_prs_failed", exc_info=True)
        return None

    entries: list[tuple[datetime, dict]] = []
    task_source = getattr(cfg, "task_source", None)
    for pr in merged:
        issue_numbers = extract_all_linked_issues(pr) or [None]
        jira_key = ""
        if task_source is not None and task_source.type == "jira" and task_source.jira_project_key:
            jira_key = extract_jira_key(pr).upper()
            prefix = f"{task_source.jira_project_key.upper()}-"
            if not jira_key.startswith(prefix):
                continue
            issue_numbers = [int(jira_key.removeprefix(prefix))]
        merged_at = _parse_merged_at(pr.get("mergedAt"))
        if merged_at is None or merged_at < midnight:
            continue
        closes = set() if jira_key else set(closing_references(pr))
        for number in issue_numbers:
            entry = _issue_entry(
                number,
                title=pr.get("title", ""),
                url=(
                    f"{task_source.jira_base_url.rstrip('/')}/browse/{jira_key}"
                    if jira_key and task_source.jira_base_url
                    else pr.get("url", "")
                ),
                stage=WorkStage.DONE,
                state=WorkItemState.MERGED.value,
                priority="",
            )
            author = (pr.get("author") or {}).get("login", "")
            assignees = [a["login"] for a in pr.get("assignees") or [] if a.get("login")]
            entry.update(
                {
                    "pr_number": pr.get("number"),
                    "merged_at": merged_at.isoformat(),
                    "pr_author": author,
                    "pr_assignees": assignees,
                    "_pr_url": pr.get("url", ""),
                    "_closes": number in closes,
                }
            )
            if jira_key:
                entry["jira_key"] = jira_key
            entries.append((merged_at, entry))
    entries.sort(key=lambda pair: pair[0], reverse=True)
    return [entry for _at, entry in entries]


def _settle_done_today(
    done_today: list[dict],
    items: list[dict],
    graph: DependencyGraph,
    open_prs: dict[int, set[int]] | None = None,
    now: datetime | None = None,
    running_issues: set[int] | None = None,
) -> list[dict]:
    """Keep an issue in Done only when a merged PR finished it; one entry per issue, newest merge.

    A PR that only does part of an issue ("Part of #N") merges while the issue stays open, often with its next
    PR already open. Such an issue keeps its own column and the merged PR stays here as an entry with no number,
    like a PR that links no issue; a PR demoted for several issues keeps one entry, and none when the same PR
    also keeps a numbered entry for an issue it finished. An issue a run in *running_issues* owns has no
    numbered entry (decided before that check, so a PR's merge never vanishes with it).

    The checks run in this order. A work item whose PR is not one merged today is an open PR. An issue the
    graph lists in a state the board does not put in ``done`` (``work_stage()``: on_qa counts as done) is
    finished, whatever else links it. Any open PR in *open_prs* (every issue a PR links, from
    ``board_exclusions()``) that is not one merged today then keeps the issue open (the PR list lags a merge by
    up to its cache TTL, and that stale case is the one Done wins). Within ``_GRAPH_LAG_SECONDS`` of *now* the
    cached graph (about ``GRAPH_CACHE_TTL``, plus the build time) can still show the issue open, so there a
    merge counts as finishing it when GitHub's closing references name the issue, or when it is the work item's
    own PR (the item lags the merge the same way, and a Jira issue has no closing references). Past the window
    the graph is trusted again, so an issue reopened after a closing merge, or one the keyword never closed,
    keeps its own column. Otherwise the graph state decides. An issue the graph lacks (opened after the graph
    was cached) is judged by its work item's state the same way.
    """
    merged_prs, closed_by_merge, recent_prs = _merged_today_index(done_today, now)
    items_by_issue = _items_by_issue(items)
    seen: set[tuple[str, int | None]] = set()
    settled: list[dict] = []
    for entry in done_today:
        number = entry["number"]
        pr_url = entry.pop("_pr_url", "")
        entry.pop("_closes", None)
        if number is not None and _merge_left_open(
            number,
            merged_prs[number],
            (open_prs or {}).get(number, set()),
            items_by_issue.get(number),
            graph,
            closed_by_merge,
            recent_prs[number],
        ):
            log.debug("work_board.done_demoted", issue=number, pr=entry.get("pr_number"))
            entry.update({"number": None, "url": pr_url})
            entry.pop("jira_key", None)
            number = None
        key = ("issue", number) if number is not None else ("pr", entry.get("pr_number"))
        if key in seen and key[1] is not None:
            continue
        seen.add(key)
        settled.append(entry)
    settled = [entry for entry in settled if entry["number"] not in (running_issues or ())]
    finished_prs = {entry["pr_number"] for entry in settled if entry["number"] is not None}
    return [entry for entry in settled if entry["number"] is not None or entry.get("pr_number") not in finished_prs]


def _merged_today_index(
    done_today: list[dict], now: datetime | None
) -> tuple[defaultdict[int, set[int]], set[int], defaultdict[int, set[int]]]:
    """Each issue's PRs merged today, the issues a closing merge within the graph lag of *now* closes, and each
    issue's PRs merged within that lag."""
    merged_prs: defaultdict[int, set[int]] = defaultdict(set)
    recent_prs: defaultdict[int, set[int]] = defaultdict(set)
    closed_by_merge: set[int] = set()
    for entry in done_today:
        number = entry["number"]
        if number is None:
            continue
        recent = _within_graph_lag(entry.get("merged_at"), now)
        if entry.get("pr_number") is not None:
            merged_prs[number].add(entry["pr_number"])
            if recent:
                recent_prs[number].add(entry["pr_number"])
        if entry.get("_closes") and recent:
            closed_by_merge.add(number)
    return merged_prs, closed_by_merge, recent_prs


def _within_graph_lag(merged_at: str | None, now: datetime | None) -> bool:
    """Whether a merge at *merged_at* is recent enough that the cached dependency graph may not show it yet."""
    if now is None:
        return False
    merged = _parse_merged_at(merged_at)
    return merged is not None and (now - merged).total_seconds() <= _GRAPH_LAG_SECONDS


def _items_by_issue(items: list[dict]) -> dict[int, dict]:
    """Each issue's work item."""
    by_issue: dict[int, dict] = {}
    for item in items:
        number = _parse_issue_number(item.get("issue_number"))
        if number is not None:
            by_issue[number] = item
    return by_issue


# A work item in one of these states carries a PR that is no longer open (merged earlier, or closed unmerged),
# so its number says nothing about whether another PR is open.
_ITEM_PR_NOT_OPEN_STATES = frozenset({WorkItemState.PR_MERGED_ISSUE_OPEN, WorkItemState.MERGED, WorkItemState.DONE})


def _merge_left_open(
    number: int,
    merged: set[int],
    open_prs: set[int],
    item: dict | None,
    graph: DependencyGraph,
    closed_by_merge: set[int],
    recent_merged: set[int],
) -> bool:
    """Whether issue *number* is still open after today's merges: another PR is open, or its state says so.

    The item's own PR counts as open only when its state says the PR is open, and an item whose PR is gone
    (merged earlier, or closed unmerged) falls through to the graph and closing-reference checks.
    """
    item_pr = (item.get("pr_details") or {}).get("number") if item else None
    if item_pr is not None and item_pr not in merged and item.get("state") not in _ITEM_PR_NOT_OPEN_STATES:
        return True
    task = graph.get_task(number)
    graph_stage = work_stage(task.state.value) if task is not None else None
    if graph_stage is WorkStage.DONE:
        return False  # no column holds a finished issue, so an open PR that only links it second cannot keep it
    if open_prs - merged:
        return True
    if number in closed_by_merge or (item_pr is not None and item_pr in recent_merged):
        return False  # the graph lags this merge
    if task is not None:
        return True
    if item is None or item_pr in merged:
        return False  # no evidence, or the item still shows a merged PR as open (stale, so Done wins)
    return work_stage(item.get("state", "")) is not WorkStage.DONE


async def _enrich_done_assignees(project_dir: Path | None, graph: DependencyGraph, entries: list[dict]) -> bool:
    """Attach Jira assignees to merged issues. Return false when any lookup failed."""
    missing = []
    for entry in entries:
        if not entry.get("jira_key"):
            continue
        task = graph.get_task(entry["number"])
        if task is not None:
            entry["assignees"] = list(task.assignees)
        else:
            cache_key = (str(project_dir), entry["jira_key"])
            cached = _done_assignees_cache.get(cache_key)
            if cached is not None and time.monotonic() - cached[0] < _MERGED_CACHE_TTL:
                entry["assignees"] = list(cached[1])
            else:
                missing.append(entry)
    if not missing:
        return True
    from sova.adapters import close_adapter, create_adapter

    try:
        adapter = create_adapter(await asyncio.to_thread(load_config, project_dir))
    except Exception:  # noqa: BLE001 (merged entries still show without optional assignees)
        log.warning("work_board.done_assignees_adapter_failed", exc_info=True)
        return False
    try:
        results = await asyncio.gather(
            *(adapter.get_task(entry["jira_key"]) for entry in missing), return_exceptions=True
        )
    finally:
        await close_adapter(adapter)
    complete = True
    for entry, result in zip(missing, results, strict=True):
        if isinstance(result, Task):
            entry["assignees"] = list(result.assignees)
            if len(_done_assignees_cache) >= 256:
                _done_assignees_cache.clear()
            _done_assignees_cache[(str(project_dir), entry["jira_key"])] = (time.monotonic(), entry["assignees"])
        else:
            log.warning("work_board.done_assignees_lookup_failed", issue=entry["jira_key"], error=repr(result))
            complete = False
    return complete


# Queue pruning ---------------------------------------------------------------


async def _queued_states(project_dir: Path | None, queue: list[int], graph: DependencyGraph) -> dict[int, TaskState]:
    """The tracker state of each queued issue there is evidence for: the graph's open state, else a lookup.

    The graph may be the previous one, answered while a rebuild runs, so its DONE is not proof: an issue closed
    and then reopened still reads DONE there, and pruning it would delete a live queue entry. A DONE issue, like
    one the graph lacks, is therefore confirmed against the tracker, and a lookup that fails leaves its issue
    out, so an outage or a stalled lookup (bounded by ``_QUEUE_LOOKUP_TIMEOUT``) reads as "unknown" and never as
    "closed". A graph state other than DONE is used as is:
    it only keeps the entry, which a stale graph cannot make wrong in a destructive way.
    """
    states: dict[int, TaskState] = {}
    unknown: list[int] = []
    for issue in queue:
        task = graph.get_task(issue)
        if task is not None and task.state != TaskState.DONE:
            states[issue] = task.state
        else:
            unknown.append(issue)
    if not unknown:
        return states

    from sova.adapters import close_adapter, create_adapter

    try:
        adapter = create_adapter(await asyncio.to_thread(load_config, project_dir))
    except Exception:  # noqa: BLE001 (no adapter means no evidence and so keeps the entries)
        log.warning("work_board.queue_lookup_adapter_failed", exc_info=True)
        return states
    try:

        async def lookup(issue: int) -> TaskState:
            # Bound each lookup, not the gather: cancelling the gather would discard lookups that already answered.
            async with asyncio.timeout(_QUEUE_LOOKUP_TIMEOUT):
                return await adapter.get_state(str(issue))

        results = await asyncio.gather(*(lookup(i) for i in unknown), return_exceptions=True)
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


# Relations -------------------------------------------------------------------


def _relations(number: int | None, graph: DependencyGraph, open_nodes: set[int]) -> dict:
    """What an issue waits for and what waits for it (open, non-epic issues, ascending), and its milestone.

    An issue the graph lacks (a merged PR's closed issue, a hidden issue gone from the tracker) has no
    relations rather than an error.
    """
    task = graph.get_task(number) if number is not None else None
    if task is None:
        return {"depends_on": [], "dependents": [], "milestone": ""}
    return {
        "depends_on": sorted(unsatisfied_dependencies(number, graph) or []),
        "dependents": sorted(graph.get_dependents(number) & open_nodes),
        "milestone": task.milestone,
    }


def _milestones(graph: DependencyGraph, *, running: set[int], open_prs: set[int]) -> list[str]:
    """The milestones of the issues ranked for Up next, for the milestone filter's choices."""
    return sorted(
        {entry["milestone"] for entry in rank_graph_entries(graph, running=running, open_prs=open_prs)} - {""}
    )


# Columns ---------------------------------------------------------------------

# Where each stage's entries sit in the payload, and which column shows them: "Your own work" is a collapsed
# group inside Needs you, and blocked issues have no column (they show in the chains).
_STAGE_LAYOUT: dict[WorkStage, tuple[str, WorkStage | None]] = {
    WorkStage.NEXT: ("up_next", WorkStage.NEXT),
    WorkStage.QUEUED: ("queued", WorkStage.QUEUED),
    WorkStage.RUNNING: ("running", WorkStage.RUNNING),
    WorkStage.REVIEW: ("review", WorkStage.REVIEW),
    WorkStage.NEEDS: ("needs", WorkStage.NEEDS),
    WorkStage.YOURS: ("yours", WorkStage.NEEDS),
    WorkStage.BLOCKED: ("blocked", None),
    WorkStage.DONE: ("done_today", WorkStage.DONE),
}

_STAGE_HELP: dict[WorkStage, str] = {
    WorkStage.NEXT: "Ranked issues ready for an agent. Queue one to let the supervisor start it later.",
    WorkStage.QUEUED: "Issues in the saved supervisor queue. It starts them in order when enabled and a slot is free.",
    WorkStage.RUNNING: "Active runs, including rebases, and issues marked in progress.",
    WorkStage.REVIEW: (
        "Open PRs in draft, CI, review, or changes requested, plus issues labeled in review. "
        "Review threads show findings."
    ),
    WorkStage.NEEDS: "A decision or retry needs you, including merge-ready PRs and failed runs.",
    WorkStage.YOURS: "Issues marked for human or interactive work.",
    WorkStage.BLOCKED: "Issues waiting on another issue.",
    WorkStage.DONE: "Issues with a PR merged today.",
}


def _columns() -> list[dict]:
    """Every stage in board order, with its label, color token, payload key and the column that shows it.

    A stage whose ``column`` is another stage is a collapsed group inside that column; ``None`` means the
    board has no column for it. The page draws from this list and keeps no stage table of its own.
    """
    return [
        {
            "stage": stage.value,
            "label": stage_label(stage),
            "color": stage_color(stage),
            "description": _STAGE_HELP[stage],
            "entries": _STAGE_LAYOUT[stage][0],
            "column": column.value if (column := _STAGE_LAYOUT[stage][1]) is not None else None,
        }
        for stage in WorkStage
    ]


# Board -----------------------------------------------------------------------


def _up_next(
    graph: DependencyGraph,
    *,
    running: set[int],
    open_prs: set[int],
    hidden: set[int],
    queued: set[int],
    stage_of: dict[int, WorkStage],
    owner: str = "all",
    github_user: str = "",
    jira_display_name: str = "",
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
        if owner == "mine":
            task = graph.get_task(number)
            if task is None or not _entry_is_mine(
                {"assignees": task.assignees, "jira_key": task.metadata.get("key", "")},
                github_user,
                jira_display_name,
            ):
                continue
        entries.append(entry)
    shown = []
    for entry in entries[:UP_NEXT_LIMIT]:
        number = entry["id"]
        task = graph.get_task(number)
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
                "assignees": list(task.assignees) if task is not None else [],
                "jira_key": task.metadata.get("key", "") if task is not None else "",
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
        last_run = item.get("last_run") or {}
        return {
            "kind": kind,
            "reason": reason,
            "pr_number": item.get("pr_number"),
            "run_id": last_run.get("id"),
            "role": last_run.get("role"),
        }
    if stage is WorkStage.YOURS:
        details = _reason_for_yours(item)
        if _item_mode(item) == "interactive":
            details["session_command"] = _session_command(item)
        return details
    if stage is WorkStage.BLOCKED:
        return {"blocked_by": (item.get("suitability") or {}).get("blocked_by", [])}
    return {}


def _classify_items(items: list[dict], queue_set: set[int], runs: dict[str, dict]) -> _Classified:
    """Put each work item in its one stage (``work_stage()``) and build its column entry."""
    board = _Classified()
    for item in items:
        number = _parse_issue_number(item.get("issue_number"))
        active = runs.get(f"issue:{number}") if number is not None else None
        stage = work_stage(
            item.get("state", ""),
            _item_mode(item),
            _item_run_status(item),
            queued=number in queue_set,
            active_run=bool(active and active.get("status") == "running"),
        )
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
        active = runs.get(f"issue:{issue}")
        stage = work_stage(
            state, None, None, queued=True, active_run=bool(active and active.get("status") == "running")
        )
        entry = _fallback_entry(issue, graph, stage)
        entry.update(_stage_details({"issue_number": issue, "state": state}, stage, runs))
        board.stage_of[issue] = stage
        board.entries[issue] = entry
        if stage is not WorkStage.DONE:
            board.columns[stage].append(entry)


def _apply_done_precedence(board: _Classified, done_today: list[dict] | None, runs: dict[str, dict]) -> None:
    """Take an issue merged today out of other columns unless a current run owns it.

    The work items are cached and can lag a merge (still ``pr_ready_to_merge``), so ``done_today``, built from
    the merged PRs, is the fresher source for these issues.
    """
    merged = {entry["number"] for entry in done_today or [] if entry["number"] is not None}
    merged -= {
        issue
        for issue in merged
        if board.stage_of.get(issue) is WorkStage.RUNNING
        and (run := runs.get(f"issue:{issue}")) is not None
        and run.get("status") == "running"
    }
    if not merged:
        return
    for stage, entries in board.columns.items():
        if stage is not WorkStage.DONE:
            entries[:] = [entry for entry in entries if entry["number"] not in merged]
    for issue in merged:
        if issue in board.stage_of:
            board.stage_of[issue] = WorkStage.DONE


SEARCH_LIMIT = 20

# Suitability modes of an issue no agent is meant to start: the board offers it no start action either.
_NO_START_MODES = frozenset({"epic", "human"})


def _search_matches(number: int, title: str, query: str, words: list[str]) -> int | None:
    """How well an issue matches: 0 for its exact number, 1 for a number it starts with, 2 for a title holding
    every word; None when it does not match."""
    if query.isdigit():
        if str(number) == query:
            return 0
        return 1 if str(number).startswith(query) else None
    lowered = title.lower()
    return 2 if all(word in lowered for word in words) else None


async def search_issues(project_dir: Path | None, query: str, limit: int = SEARCH_LIMIT) -> dict:
    """Open issues matching *query*, on the board or not: ``{"results": [entry, ...], "more": int}``.

    A number (``1418`` or ``#1418``) matches that issue first, then the issues whose number starts with it; words
    match the issues whose title holds every one, ignoring case. Newest first within each kind of match. Each entry
    has the board entry's fields except ``stage`` (``start`` included, so the drawer can offer the run the issue's
    state calls for, and ``None`` for an epic or human issue, as on the board), plus its relations and
    milestone. Reads the dependency graph the board uses, which lists every open issue, so a search makes no
    tracker call of its own; a graph failure raises, like the board's.
    """
    q = query.strip().lstrip("#").strip()
    if not q:
        return {"results": [], "more": 0}
    words = q.lower().split()
    graph = await load_ranking_graph(project_dir)
    scored: list[tuple[int, int, Task]] = []
    for number in graph.nodes:
        task = graph.get_task(number)
        if task is None or task.state is TaskState.DONE:
            continue
        score = _search_matches(number, task.title, q, words)
        if score is not None:
            scored.append((score, number, task))
    scored.sort(key=lambda match: (match[0], -match[1]))
    open_nodes = _open_chain_nodes(graph)
    results = []
    for _, number, task in scored[:limit]:
        state = task.state.value
        entry = _issue_entry(
            number,
            title=task.title,
            url=task.url,
            stage=work_stage(state),
            state=state,
            priority=task.priority.value,
        )
        # The state alone cannot place an issue the way the board does (queue, hidden list, suitability mode and
        # run status all count), so a search result carries no stage: the board's own columns are the authority.
        entry.pop("stage", None)
        entry["jira_key"] = task.metadata.get("key", "")
        if assess_suitability(task, graph).mode.value in _NO_START_MODES:
            entry["start"] = None
        entry["actions"] = [entry["start"]] if entry["start"] else []
        entry.update(_relations(number, graph, open_nodes))
        results.append(entry)
    return {"results": results, "more": max(0, len(scored) - limit)}


async def build_board(
    project_dir: Path | None, milestone: str = "", *, owner: str = "all", now: datetime | None = None
) -> dict:
    """The Work board: ``{stages, columns, agents, api_health, milestones, up_next, more_ready, queued, queue_order,
    running, other_runs, review, needs, yours, blocked, done_today, chains, other_blocked, hidden, unavailable}``.

    Every entry carries ``depends_on`` and ``dependents`` (open, non-epic issue numbers) and its ``milestone``;
    work-item entries add ``mode``, ``pr`` and ``last_run``. Running and needs entries carry the ``run_id`` their
    buttons act on.

    ``queue_order`` is the whole saved queue (closed issues pruned), including a queued issue that sits in another
    column, so a card's Move up and Move down read its place in the order the move acts on.

    Every issue is in one column (``work_stage()``). ``milestone`` limits the ranking behind ``up_next`` only.
    A dependency graph failure raises, since an empty board must not look like an idle project; the sources that
    only enrich the board fail open, and the ones that did are named in ``unavailable``.
    """
    from sova.config.context import resolve_project_dir
    from sova.dashboard.services.agent_pool import get_default_project_dir

    project_dir = resolve_project_dir(project_dir, default_project_dir=get_default_project_dir())
    now = now or datetime.now(timezone.utc)
    # The graph first: with no previous graph (a cold start, or a priority change that forgot it) the build is
    # awaited without the work-items poll's short wait, so the items that follow classify from this graph rather
    # than from none. With one, it answers at once and the rebuild runs in the background.
    graph = await load_ranking_graph(project_dir)
    ranking_graph = await load_ranking_graph(project_dir, milestone) if milestone else graph
    items_data, (running, open_prs, prs_by_issue), queue, hidden_list, runs, done_today = await asyncio.gather(
        get_work_items(project_dir),
        board_exclusions(project_dir),
        load_task_queue(project_dir),
        load_hidden_issues(project_dir),
        _active_runs(project_dir),
        _done_today(project_dir, now),
    )
    queue, prune_failed = await _prune_closed_queue(project_dir, queue, graph)
    queue_set = set(queue)
    hidden = set(hidden_list)
    running = running | {
        number
        for run in (runs or {}).values()
        if run.get("status") == "running"
        if (number := _parse_issue_number(run.get("issue_number"))) is not None
    }
    done_assignees_failed = False
    if done_today is not None:
        # A merge that does not finish its issue keeps its entry (no number) even while a run owns the issue.
        run_owned = {
            number
            for key, run in (runs or {}).items()
            if key.startswith("issue:") and run.get("status") == "running"
            if (number := _parse_issue_number(key.removeprefix("issue:"))) is not None
        }
        done_today = _settle_done_today(done_today, items_data["items"], graph, prs_by_issue, now, run_owned)
        done_assignees_failed = not await _enrich_done_assignees(project_dir, graph, done_today)

    board = _classify_items(items_data["items"], queue_set, runs or {})
    _classify_queue_fallbacks(board, queue, graph, runs or {})
    _apply_done_precedence(board, done_today, runs or {})
    columns, stage_of = board.columns, board.stage_of

    queued = [board.entries[issue] for issue in queue if stage_of.get(issue) is WorkStage.QUEUED]
    up_next, more_ready = _up_next(
        ranking_graph,
        running=running,
        open_prs=open_prs,
        hidden=hidden,
        queued=queue_set,
        stage_of=stage_of,
        owner=owner,
        github_user=items_data.get("github_user") or "",
        jira_display_name=items_data.get("jira_display_name") or "",
    )
    for entry in up_next:  # the ranking knows no runs or PRs: take them from the issue's work item
        item = board.by_number.get(entry["number"])
        if item is not None:
            entry.update(_item_extras(item))
        task = graph.get_task(entry["number"])
        if task is not None:
            entry.update(assignees=list(task.assignees), jira_key=task.metadata.get("key", ""))
    hidden_entries = [
        _item_entry(board.by_number[n], stage_of[n])
        if n in board.by_number
        else _fallback_entry(n, graph, WorkStage.NEXT)
        for n in hidden_list
    ]
    other_runs = _other_runs(runs, columns[WorkStage.RUNNING])
    for run in other_runs:
        issue = _parse_issue_number(run.get("issue_number"))
        task = graph.get_task(issue) if issue is not None else None
        if task is not None:
            run.update(assignees=list(task.assignees), jira_key=task.metadata.get("key", ""))

    on_board_entries = [
        *up_next,
        *queued,
        *columns[WorkStage.RUNNING],
        *columns[WorkStage.REVIEW],
        *columns[WorkStage.NEEDS],
        *columns[WorkStage.YOURS],
        *columns[WorkStage.BLOCKED],
        *(done_today or []),
        *({"number": _parse_issue_number(run.get("issue_number")), **run} for run in other_runs),
    ]
    for entry in [*on_board_entries, *other_runs, *hidden_entries]:
        entry["mine"] = _entry_is_mine(
            entry, items_data.get("github_user") or "", items_data.get("jira_display_name") or ""
        )
    if owner == "mine":
        on_board_entries = [
            entry
            for entry in on_board_entries
            if _entry_is_mine(entry, items_data.get("github_user") or "", items_data.get("jira_display_name") or "")
        ]
    on_board = {entry["number"] for entry in on_board_entries if entry["number"] is not None}
    chains, other_blocked = _chains(graph, on_board, stage_of)
    open_nodes = _open_chain_nodes(graph)
    for entry in [
        *up_next,
        *(e for entries in columns.values() for e in entries),
        *(done_today or []),
        *hidden_entries,
    ]:
        entry.update(_relations(entry["number"], graph, open_nodes))

    return {
        "stages": [stage.value for stage in WorkStage],
        "columns": _columns(),
        "agents": {"running": items_data.get("agents_running"), "limit": items_data.get("max_concurrent")},
        "api_health": items_data.get("api_health") or {"status": "ok"},
        # Age of the graph the board ranked from and whether its refresh failed, so the client can say it is old.
        "graph": graph_age_meta(project_dir),
        "github_user": items_data.get("github_user") or "",
        "jira_display_name": items_data.get("jira_display_name") or "",
        "milestones": _milestones(graph, running=running, open_prs=open_prs),
        "up_next": up_next,
        "more_ready": more_ready,
        "queued": queued,
        "queue_order": queue,
        "running": columns[WorkStage.RUNNING],
        "other_runs": other_runs,
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
                ("open_prs", prs_by_issue is None),
                ("done_assignees", done_assignees_failed),
                ("active_runs", runs is None),
                ("queue_prune", prune_failed),
            )
            if failed
        ],
    }
