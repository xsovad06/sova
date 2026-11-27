"""The activity feed's narration: what happened today, what needs the user, and how past periods went.

A read model over ``task_runs`` and today's ``feed_events``, built per request for one project and time zone. It adds
no state of its own: a group's state and rounds come from the run history (``work_history_service``), and every
sentence from ``feed_narration``. No model is called.

``build_activity()`` returns ``{"generated_at", "tz", "today", "attention", "background", "background_summary",
"periods", "empty_today_text", "labels"}``:

- ``today``: one entry per issue (else PR, else lone run) with activity today, newest first, with today's cost. A
  run still going reads as in progress; otherwise the line follows the group's state. A group that needs the user is
  in ``attention``; it keeps a ``today`` line only for a finished run of another kind earlier today, and that line
  never mentions what needs the user.
- ``attention``: what waits on the user (a failed run, a spec to approve, a review budget that ran out, a run the
  watchdog killed), one per issue, each with the handoff action that answers it when one is known. It is not limited
  to today: a failure, an approval wait or a watchdog kill stays until a newer run settles it (looking back
  ``_ATTENTION_DAYS``, the one explicit expiry: an unsettled need older than that is no longer offered), so it does
  not vanish at midnight. A group in it keeps a ``today`` line only for a finished run of another kind, worded as
  what that run did, never as where the group stands. An identity names the kind, the group and the run (or PR) it is
  about, so a new failure is a new line. A run row left "running" by a crash for longer than ``_STALE_RUN_AFTER`` is
  not in progress, so it neither reads as working nor hides what needs the user.
- ``background``: today's events that are not about one issue, counted by kind.
- ``labels``, ``background_summary`` and ``empty_today_text``: the section headings, the fold heading and the quiet-day
  line, so the client words nothing itself. A period's label and sentence come from ``feed_narration`` too.
- ``periods``: yesterday, earlier this week, the last weeks and the months before, from ``task_runs`` (so months
  older than the feed events' retention still have lines). Day and week edges are local midnights, so a daylight
  saving change never shifts a count by an hour.
"""

from __future__ import annotations

import sys
import time
from datetime import date, datetime, timedelta, timezone, tzinfo
from datetime import time as day_time
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sova.core.state import TASK_RUN_TERMINAL
from sova.dashboard.services import feed_narration as narrate
from sova.dashboard.services.snapshot_service import peek_snapshot
from sova.dashboard.services.work_history_service import (
    FAILED,
    GROUP_KEY,
    SUCCEEDED,
    WITH_LINK,
    attach_titles,
    get_grouped_history,
    history_items,
    issue_of_key,
    merged_runs,
    newest_groups,
    newest_run_ids,
    round_counts,
    unsettled_keys,
)
from sova.dashboard.services.work_service import as_utc
from sova.db.models import FeedEventRecord, TaskRun
from sova.db.session import get_session
from sova.utils.formatting import iso_utc
from sova.utils.logging import get_logger
from sova.work.state import (
    HistoryState,
    WorkItemState,
    history_state_color,
    history_state_label,
    state_color,
    state_label,
)

log = get_logger(component="dashboard.feed_activity")

# Groups read for today: more than a day of work produces, so the cap is not a silent cut in practice.
_TODAY_GROUP_LIMIT = 200
# How far back attention reaches: a failure, a spec wait or a watchdog kill older than this is not offered again (the
# one explicit expiry; documented in docs/architecture-deep-dive.md). The read is capped at the history's page
# maximum, newest group first.
_ATTENTION_DAYS = 14
_ATTENTION_GROUP_LIMIT = 200
# A run row still non-terminal after this long is a leftover of a crash, not a run in progress (a whole pipeline is
# bounded by its step timeouts, well under this). It is not offered as working, so it cannot hide a failure.
_STALE_RUN_AFTER = timedelta(hours=12)
# How far back the period lines reach: this many whole weeks before this one, then this many months.
_WEEKS_BACK = 4
_MONTHS_BACK = 12
# The periods change slowly (only today's edge moves), so they are kept briefly per project and time zone.
PERIODS_TTL_SECONDS = 60.0

# Events that narrate a run: the run itself says it, so they are not background and not attention. A batch job
# (triage, harden) and the supervisor planner leave no run, so their events are background.
_RUN_CATEGORIES = frozenset({"agent", "handoff"})
# A batch emits once when it starts and once when it ends; only the end is counted, so a batch is one thing.
_BATCH = "batch"
# A finished-run state that waits on the user does not appear in ``today``.
_ATTENTION_STATES = frozenset({HistoryState.FAILED, HistoryState.NEEDS_APPROVAL})
# A group that needs the user keeps a ``today`` line only for a finished run that is none of these: they are the
# runs the need comes from (a spec, the review loop), so a line worded from them would restate or contradict it.
_NEED_ACTIONS = frozenset({"research", "review", "address-review"})
# A handoff action the server cannot start (see the phone page): never offered as a button.
_UNSUPPORTED_MODES = frozenset({"shell", "dashboard-only"})
_WATCHDOG_KILL = "kill"
# A kill whose run row is gone is keyed in its own namespace: ``run:<id>`` is a lone run's history key, and an event id
# and a run id are different id spaces.
_EVENT_KEY = "event:"
# The recency of a review budget that names no Address review run (the budget is spent, but when is unknown): it is
# not outdated by a failure, so it keeps the group's line as it always did.
_NO_RUN_WINS = sys.maxsize
# A kill with no run row to judge it by (the run is gone, or its event named none) cannot be settled by a later run, so
# it expires sooner than a need that can: it is offered for this long after the event.
_ORPHAN_KILL_AFTER = timedelta(days=2)

_periods_cache: dict[tuple[str, str, date], tuple[float, list[dict]]] = {}
# The work items whose review budget ran out, as last read from the snapshot, per project. The snapshot is dropped by
# any write that changes it and absent before its first build; a need that is still true must not vanish then.
# Process state: with more than one worker process each keeps its own copy, so a need whose snapshot was dropped in
# one worker only can flip between requests. The dashboard server is one process per project.
_last_exhausted: dict[str, list[dict]] = {}


def reset_activity_cache() -> None:
    """Forget the cached period lines and the remembered review budgets. Test-only."""
    _periods_cache.clear()
    _last_exhausted.clear()


def resolve_zone(name: str | None) -> tzinfo:
    """The IANA zone ``name`` names; UTC for a missing or unknown one (never an error: the client's zone is a hint)."""
    if not name:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        log.debug("feed_activity.unknown_zone", tz=name)
        return timezone.utc


def local_midnight(day: date, zone: tzinfo) -> datetime:
    """The start of ``day`` in ``zone``, as UTC."""
    return datetime.combine(day, day_time.min, tzinfo=zone).astimezone(timezone.utc)


def _month_start(day: date, months_back: int) -> date:
    index = day.year * 12 + day.month - 1 - months_back
    return date(index // 12, index % 12 + 1, 1)


def _period_ranges(today: date, zone: tzinfo) -> list[tuple[str, str, datetime, datetime]]:
    """Non-overlapping ``(key, label, start, end)`` UTC ranges before today, newest first: yesterday, earlier this
    week, the last weeks, then the months before those (each month clipped to what a newer range did not take)."""
    week_start = today - timedelta(days=today.weekday())
    yesterday = today - timedelta(days=1)
    raw: list[tuple[str, str, date, date]] = [
        ("yesterday", narrate.period_label("yesterday"), yesterday, today),
        ("this_week", narrate.period_label("this_week"), week_start, yesterday),
    ]
    for weeks in range(1, _WEEKS_BACK + 1):
        label = narrate.period_label("weeks", weeks=weeks)
        raw.append(
            (f"week-{weeks}", label, week_start - timedelta(weeks=weeks), week_start - timedelta(weeks=weeks - 1))
        )
    for months in range(_MONTHS_BACK):
        start = _month_start(today, months)
        label = narrate.period_label("month", month=start.month, year=None if start.year == today.year else start.year)
        raw.append((f"month-{start:%Y-%m}", label, start, _month_start(today, months - 1)))
    ranges = []
    ceiling = today
    for key, label, start, end in raw:
        end = min(end, ceiling)
        if start >= end:
            continue
        ceiling = start
        ranges.append((key, label, local_midnight(start, zone), local_midnight(end, zone)))
    return ranges


async def _period_lines(session: AsyncSession, today: date, zone: tzinfo) -> list[dict]:
    """One line per past period that had any activity, from SQL aggregates (one query) and the merged runs.

    Each line carries its local ``from`` and ``to`` dates (both inclusive), which the run history's custom range
    takes, so a click opens the history on exactly that period."""
    ranges = _period_ranges(today, zone)
    if not ranges:
        return []
    oldest, newest = min(r[2] for r in ranges), max(r[3] for r in ranges)
    columns = []
    for _, _, start, end in ranges:
        inside = (TaskRun.started_at >= start) & (TaskRun.started_at < end)
        columns += [
            func.count(func.distinct(case((inside, GROUP_KEY)))),
            func.count(case((inside & (TaskRun.status == FAILED), TaskRun.id))),
            func.coalesce(func.sum(case((inside, TaskRun.total_cost_usd), else_=0)), 0),
        ]
    totals = (
        await session.execute(
            select(*columns)
            .select_from(WITH_LINK)
            .where(TaskRun.status.in_(TASK_RUN_TERMINAL), TaskRun.started_at >= oldest, TaskRun.started_at < newest)
        )
    ).one()
    merged = await merged_runs(session, oldest, newest)
    lines = []
    for i, (key, label, start, end) in enumerate(ranges):
        worked, failed, cost = totals[3 * i : 3 * i + 3]
        merged_groups = {gk for started, gk in merged if start <= as_utc(started) < end}
        # A merging Integrate run is a finished run in the same range, so its group is already in ``worked``.
        if not worked:
            continue
        text = narrate.period(label, worked, len(merged_groups), failed, float(cost))
        lines.append(
            {
                "key": key,
                "label": label,
                "text": text,
                "starts_at": iso_utc(start),
                "from": start.astimezone(zone).date().isoformat(),
                "to": (end.astimezone(zone).date() - timedelta(days=1)).isoformat(),
            }
        )
    return lines


async def _cached_periods(session: AsyncSession, project_dir: Path | None, today: date, zone: tzinfo) -> list[dict]:
    key = (str(project_dir), _tz_key(zone), today)
    hit = _periods_cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < PERIODS_TTL_SECONDS:
        return hit[1]
    lines = await _period_lines(session, today, zone)
    now = time.monotonic()
    # Only a fresh entry for today is worth keeping: a past day or a lapsed TTL would never be read again.
    for stale in [k for k, (at, _) in _periods_cache.items() if k[2] != today or now - at >= PERIODS_TTL_SECONDS]:
        del _periods_cache[stale]
    _periods_cache[key] = (now, lines)
    return lines


def _tz_key(zone: tzinfo) -> str:
    return getattr(zone, "key", None) or "UTC"


def _matches_handoff(handoff: object, issue: str | None, pr_number: int | None) -> bool:
    """Whether a handoff entry is for this issue (by issue only), or for this PR (the PR as a number or a string)."""
    if not isinstance(handoff, dict):
        return False
    if issue:
        return str(handoff.get("issue") or "").lstrip("#") == issue
    return _is_pr(handoff, pr_number)


def _is_pr(handoff: dict, pr_number: int | None) -> bool:
    """Whether the handoff carries this PR, whatever type its ``pr_number`` is stored as (the router compares text)."""
    return pr_number is not None and str(handoff.get("pr_number") or "").lstrip("#") == str(pr_number)


def _offered_action(action: object, ref: str) -> dict | None:
    """The button one handoff action offers, or None when the feed cannot run it."""
    if not isinstance(action, dict) or action.get("mode") in _UNSUPPORTED_MODES or not action.get("id"):
        return None
    command = str(action.get("command") or "").strip().lstrip("/").split()
    label = action.get("label") or action["id"]
    return {
        "id": action["id"],
        "label": label,
        "ref": ref,
        "confirm": action.get("style") == "danger" or command[:1] == ["integrate-pr"],
        # The words around the button, so the client words nothing itself.
        "confirm_text": narrate.action_confirm(label, ref),
        "started_text": narrate.action_started(label),
        "failed_text": narrate.action_failed(label),
    }


def _handoff_action(project_dir: Path | None, issue: str | None, pr_number: int | None) -> dict | None:
    """The button a waiting handoff for this issue or PR offers, from the last handoff snapshot (never a build)."""
    snapshot = peek_snapshot("handoff", project_dir)
    if snapshot is None:
        return None
    handoffs = snapshot.payload.get("handoffs") or []
    # The issue's own handoff first. A handoff started from the PR number alone has no issue (an Address review or
    # Integrate one), so an item keyed by its issue falls back to the PR, and the button posts ``pr:N``, which the
    # handoff router resolves the same way.
    attempts = [(issue, lambda h: _matches_handoff(h, issue, None))] if issue else []
    if pr_number is not None:
        attempts.append((f"pr:{pr_number}", lambda h: isinstance(h, dict) and _is_pr(h, pr_number)))
    for ref, matches in attempts:
        for handoff in filter(matches, handoffs):
            for action in handoff.get("next_actions") or []:
                offered = _offered_action(action, ref)
                if offered is not None:
                    return offered
    return None


def _history_number(issue: str | None, pr_number: int | None) -> str | None:
    return issue or (str(pr_number) if pr_number else None)


class _Need(NamedTuple):
    """One thing that waits on the user, before its handoff button and title are known."""

    kind: str
    key: str
    issue: str | None
    pr_number: int | None
    text: str
    color: str
    # What the need is about beyond its group: the run that failed, was killed or ended the review loop, else the PR.
    # It makes a second failure on the same issue a new identity, so a line the user already saw cannot hide it.
    token: str
    # The id of that run (0 when none): when two needs share a group, the one about the newer run is the one to show.
    run: int = 0


def _attention(need: _Need, project_dir: Path | None) -> dict:
    return {
        "identity": f"{need.kind}:{need.key}:{need.token}",
        "kind": need.kind,
        "key": need.key,
        "issue_number": need.issue,
        "pr_number": need.pr_number,
        "title": None,
        "text": need.text,
        "color": need.color,
        "number": _history_number(need.issue, need.pr_number),
        "action": _handoff_action(project_dir, need.issue, need.pr_number),
    }


async def _running_runs(session: AsyncSession, now: datetime) -> list[tuple[TaskRun, str]]:
    """The runs in progress: non-terminal rows started within ``_STALE_RUN_AFTER`` (an older one is a leftover)."""
    rows = await session.execute(
        select(TaskRun, GROUP_KEY)
        .select_from(WITH_LINK)
        .where(TaskRun.status.notin_(TASK_RUN_TERMINAL), TaskRun.started_at >= now - _STALE_RUN_AFTER)
    )
    return [(run, gk) for run, gk in rows]


async def _background_counts(session: AsyncSession, since: datetime) -> dict[str, int]:
    """The count of every event since ``since`` that is not about one run, by kind (background), counted in SQL: the
    run events (the bulk of a busy day) are never loaded. A watchdog kill is attention, not background, and a batch
    counts once, when it ends (only its end event names a ``total``)."""
    action = func.coalesce(FeedEventRecord.metadata_json["action"].as_string(), "")
    rows = await session.execute(
        select(FeedEventRecord.category, func.count())
        .where(
            FeedEventRecord.created_at >= since,
            FeedEventRecord.category.notin_(_RUN_CATEGORIES),
            or_(FeedEventRecord.category != "watchdog", action != _WATCHDOG_KILL),
            or_(FeedEventRecord.category != _BATCH, FeedEventRecord.metadata_json["total"].as_string().is_not(None)),
        )
        .group_by(FeedEventRecord.category)
    )
    counts: dict[str, int] = {}
    for category, count in rows:
        kind = category if narrate.known_background(category) else narrate.OTHER_KIND
        counts[kind] = counts.get(kind, 0) + count
    return counts


def _run_id(value: object) -> int | None:
    """The run id a kill event names: an int, or a numeric string; None when it names none (a bool is not an id)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


async def _watchdog_kills(session: AsyncSession, since: datetime) -> list[dict]:
    """The watchdog kills since ``since`` (the attention window, not only today: a kill at 23:50 still waits), newest
    first and capped like the rest of attention. Only kills are read: the warnings are filtered out in SQL."""
    rows = await session.execute(
        select(FeedEventRecord.id, FeedEventRecord.created_at, FeedEventRecord.metadata_json)
        .where(
            FeedEventRecord.category == "watchdog",
            FeedEventRecord.created_at >= since,
            FeedEventRecord.metadata_json["action"].as_string() == _WATCHDOG_KILL,
        )
        .order_by(FeedEventRecord.id.desc())
        .limit(_ATTENTION_GROUP_LIMIT)
    )
    kills = []
    for event_id, created_at, metadata in rows:
        meta = metadata if isinstance(metadata, dict) else {}
        kills.append(
            {
                "event_id": event_id,
                "created_at": created_at,
                "run_id": _run_id(meta.get("run_id")),
                "signal": meta.get("signal"),
            }
        )
    kills.reverse()
    return kills


async def _kill_refs(session: AsyncSession, kills: list[dict]) -> dict[int, tuple[str, int | None]]:
    """Each killed run's group key and PR, for the kills whose run still exists."""
    ids = {k["run_id"] for k in kills if k["run_id"] is not None}
    if not ids:
        return {}
    rows = await session.execute(
        select(TaskRun.id, GROUP_KEY, TaskRun.pr_number).select_from(WITH_LINK).where(TaskRun.id.in_(ids))
    )
    return {run_id: (gk, pr) for run_id, gk, pr in rows}


def _entry(
    key: str, issue: str | None, pr_number: int | None, text: str, state: dict, started_at: str, **extra: object
) -> dict:
    return {
        "key": key,
        "issue_number": issue,
        "pr_number": pr_number,
        "title": None,
        "text": text,
        "state": state["value"],
        "state_label": state["label"],
        "state_color": state["color"],
        "started_at": started_at,
        "number": _history_number(issue, pr_number),
        **extra,
    }


def _running_entries(working: dict[str, tuple[TaskRun, dict]], now: datetime) -> list[dict]:
    """One in-progress entry per group with a run going: the newest run's action and how long it has run."""
    running = WorkItemState.AGENT_RUNNING
    state = {"value": running.value, "label": state_label(running), "color": state_color(running)}
    entries = []
    for key, (run, item) in working.items():
        started = as_utc(run.started_at)
        minutes = max(int((now - started).total_seconds() // 60), 0)
        text = narrate.running(item["action"], minutes)
        entries.append(
            _entry(key, issue_of_key(key), run.pr_number, text, state, iso_utc(started), running=True, cost_usd=None)
        )
    return entries


def _group_entries(
    groups: dict[str, dict],
    working: dict[str, tuple[TaskRun, dict]],
    rounds: dict[str, tuple[int, int]],
    needing: set[str],
) -> list[dict]:
    """One entry per finished group that does not need the user. A group that needs the user (``needing``: failed, a
    spec to approve, a review budget that ran out, a watchdog kill) has none, unless a finished run of another kind
    ran earlier today: that run gets the line, and it never mentions what needs the user."""
    entries = []
    for key, group in groups.items():
        if key in working:
            continue
        state = HistoryState(group["state"])
        if key in needing or state in _ATTENTION_STATES:
            done = next(
                (r for r in group["runs"] if r["status"] == SUCCEEDED and r["action"] not in _NEED_ACTIONS), None
            )
            if done is None:
                continue
            # What that one run did, in the past tense: where the group stands now is what the attention line says.
            state = HistoryState.FINISHED
            text = narrate.did(done["action"], group["pr_number"])
        else:
            text = narrate.settled(state, group["pr_number"], *rounds.get(key, (0, 0)))
        display = {"value": state.value, "label": history_state_label(state), "color": history_state_color(state)}
        entries.append(
            _entry(
                key,
                group["issue_number"],
                group["pr_number"],
                text,
                display,
                group["last_started_at"],
                running=False,
                cost_usd=group["total_cost_usd"],
            )
        )
    return entries


def _exhausted_items(project_dir: Path | None) -> list[dict]:
    """The work items whose address-review budget ran out, from the last work-items snapshot (never a build). With
    no snapshot (none built yet, or a write dropped it) the items last seen stand, so a need that is still true does
    not vanish and come back as new."""
    memory_key = str(project_dir)
    snapshot = peek_snapshot("work_items", project_dir)
    if snapshot is None:
        return list(_last_exhausted.get(memory_key, []))
    exhausted = WorkItemState.PR_REVIEW_EXHAUSTED
    found = []
    for item in snapshot.payload.get("items") or []:
        if not isinstance(item, dict) or item.get("state") != exhausted.value:
            continue
        issue = str(item["issue_number"]) if item.get("issue_number") else None
        pr = item.get("pr_number")
        if issue or pr:
            found.append({"issue_number": issue, "pr_number": pr})
    _last_exhausted[memory_key] = found
    return found


def _forget_exhausted(project_dir: Path | None, item: dict) -> None:
    """Drop ``item`` from the remembered review budgets: its group has since merged, so it is no longer true."""
    memory_key = str(project_dir)
    if memory_key in _last_exhausted:
        _last_exhausted[memory_key] = [i for i in _last_exhausted[memory_key] if i != item]


def _exhausted_key(item: dict, pr_keys: dict[int, str]) -> str:
    """The group key of an exhausted work item: its issue, else the issue its PR's runs belong to, else the PR."""
    issue, pr = item["issue_number"], item["pr_number"]
    return f"issue:{issue}" if issue else pr_keys.get(pr, f"pr:{pr}")


async def _pr_keys(session: AsyncSession, prs: set[int]) -> dict[int, str]:
    """The group key each PR's runs fall under (its issue when a run links one), as the run history keys them."""
    if not prs:
        return {}
    rows = await session.execute(
        select(TaskRun.pr_number, GROUP_KEY)
        .select_from(WITH_LINK)
        .where(TaskRun.pr_number.in_(prs))
        .order_by(TaskRun.started_at.desc(), TaskRun.id.desc())
    )
    keys: dict[int, str] = {}
    for pr, gk in rows:
        keys.setdefault(pr, gk)
    return keys


def _group_attention(
    groups: dict[str, dict], candidates: list[str], working: dict[str, tuple[TaskRun, dict]]
) -> list[_Need]:
    """One need per candidate group whose newest finished run failed or waits for approval, skipping groups with a
    run going. ``groups`` may hold more groups than the window's ``candidates`` (a kill's or a budget's group)."""
    found = []
    for key in candidates:
        group = groups.get(key)
        if group is None:
            continue
        state = HistoryState(group["state"])
        if key in working or state not in _ATTENTION_STATES:
            continue
        if state == HistoryState.NEEDS_APPROVAL:
            text, kind = narrate.needs_approval(), "needs_approval"
        else:
            text, kind = narrate.failed(group["result"]["text"]), "failed"
        token = f"run:{group['state_inputs']['newest_run_id']}"
        newest_run = group["state_inputs"]["newest_run_id"]
        found.append(
            _Need(kind, key, group["issue_number"], group["pr_number"], text, group["state_color"], token, newest_run)
        )
    return found


def _kill_settled(group: dict | None, run_id: int) -> bool:
    """Whether a run that started after the killed one has finished (the group's newest finished run is newer than the
    killed run), or the killed run itself ended in a state that does not wait on the user. A killed row left
    non-terminal is never the newest finished run, so an older finished run settles nothing."""
    if group is None:
        return False
    newest = group["state_inputs"]["newest_run_id"]
    if newest > run_id:
        return True
    return newest == run_id and HistoryState(group["state"]) not in (HistoryState.FAILED, HistoryState.STOPPED)


def _kill_attention(
    groups: dict[str, dict],
    working: dict[str, tuple[TaskRun, dict]],
    kills: list[dict],
    kill_refs: dict[int, tuple[str, int | None]],
    now: datetime,
) -> list[_Need]:
    """One need per watchdog kill that still needs the user: its issue has no run going, and no run newer than the
    killed one has finished (a later run, whatever it did, settles the kill: a later failure is its own need, with
    its own identity). A kill with no run row to judge by expires after ``_ORPHAN_KILL_AFTER``."""
    color = history_state_color(HistoryState.FAILED)
    found = []
    for kill in kills:
        run_id = kill["run_id"]
        ref = kill_refs.get(run_id) if run_id is not None else None
        token = f"run:{run_id}" if run_id is not None else f"event:{kill['event_id']}"
        text = narrate.watchdog_kill(kill["signal"])
        if ref is None:
            if now - as_utc(kill["created_at"]) <= _ORPHAN_KILL_AFTER:
                found.append(_Need("watchdog_kill", f"{_EVENT_KEY}{kill['event_id']}", None, None, text, color, token))
            continue
        key, pr = ref
        if key in working or _kill_settled(groups.get(key), run_id):
            continue
        found.append(_Need("watchdog_kill", key, issue_of_key(key), pr, text, color, token, run_id))
    return found


def _exhausted_attention(
    items: list[dict],
    pr_keys: dict[int, str],
    groups: dict[str, dict],
    working: dict[str, tuple[TaskRun, dict]],
    address_runs: dict[str, int],
    project_dir: Path | None,
) -> list[_Need]:
    """One need per work item whose address-review budget ran out, under the key the run history gives its group
    (a PR-only item joins its issue when a run links them), skipping groups with a run going (a hand-started Address
    review is in progress, not out of budget) and groups that have merged (the item is forgotten: it is stale, as
    when the snapshot was dropped by the merge write). The token is the PR and its last Address review run, which
    stay fixed while the budget stays spent, so an unrelated later run does not make the same need new."""
    color = state_color(WorkItemState.PR_REVIEW_EXHAUSTED)
    found = []
    for item in items:
        pr = item["pr_number"]
        key = _exhausted_key(item, pr_keys)
        if key in working:
            continue
        group = groups.get(key)
        if group is not None and HistoryState(group["state"]) == HistoryState.MERGED:
            _forget_exhausted(project_dir, item)
            continue
        run_id = address_runs.get(key)
        if pr and run_id:
            token = f"pr:{pr}:run:{run_id}"
        elif run_id:
            token = f"run:{run_id}"
        elif pr:
            token = f"pr:{pr}"
        else:
            token = key
        need_text = narrate.review_exhausted()
        found.append(
            _Need("review_exhausted", key, issue_of_key(key), pr, need_text, color, token, run_id or _NO_RUN_WINS)
        )
    return found


def _attention_items(needs: list[_Need], project_dir: Path | None) -> list[dict]:
    """What waits on the user, one item per issue (else PR, else run). On a clash the need about the newer run wins
    (a failure after the review budget ran out is shown as the failure, with its own identity); a tie goes to the
    later need, so the caller lists a plain failure first, then a watchdog kill, then a review budget."""
    chosen: dict[str, _Need] = {}
    for need in needs:
        held = chosen.get(need.key)
        if held is None or need.run >= held.run:
            chosen[need.key] = need
    return [_attention(need, project_dir) for need in chosen.values()]


async def build_activity(project_dir: Path | None, tz_name: str | None, *, now: datetime | None = None) -> dict:
    """The feed payload for one project in the zone ``tz_name`` (UTC when it is missing or unknown)."""
    zone = resolve_zone(tz_name)
    now_utc = now or datetime.now(timezone.utc)
    today = now_utc.astimezone(zone).date()
    since = local_midnight(today, zone)
    attention_since = local_midnight(today - timedelta(days=_ATTENTION_DAYS), zone)
    exhausted = _exhausted_items(project_dir)

    async with await get_session(project_dir) as session, session.begin():
        history = await get_grouped_history(session, since=since, limit=_TODAY_GROUP_LIMIT)
        kills = await _watchdog_kills(session, attention_since)
        killed_ids = {k["run_id"] for k in kills if k["run_id"] is not None}
        # A run the watchdog killed is not in progress, even when its row was never made terminal.
        going = [(run, gk) for run, gk in await _running_runs(session, now_utc) if run.id not in killed_ids]
        running_items = await history_items(session, [run for run, _ in going])
        rounds = await round_counts(session, [g["key"] for g in history["groups"]])
        background_counts = await _background_counts(session, since)
        # Attention is not a day's work: what still waits on the user from before midnight stays in view. It reads the
        # group keys whose newest run did not succeed, plus the groups of the kills and review budgets, and judges
        # only those: no run of a group that is fine is loaded.
        kill_refs = await _kill_refs(session, kills)
        pr_keys = await _pr_keys(session, {i["pr_number"] for i in exhausted if not i["issue_number"]})
        exhausted_keys = [_exhausted_key(item, pr_keys) for item in exhausted]
        candidates = await unsettled_keys(session, attention_since, _ATTENTION_GROUP_LIMIT)
        wanted = [*candidates, *(ref[0] for ref in kill_refs.values()), *exhausted_keys]
        unsettled = await newest_groups(session, list(dict.fromkeys(wanted)))
        address_runs = await newest_run_ids(session, exhausted_keys, "address-review")
        periods = await _cached_periods(session, project_dir, today, zone)

    groups = {g["key"]: g for g in history["groups"]}
    working: dict[str, tuple[TaskRun, dict]] = {}
    for run, gk in going:
        newest = working.get(gk)
        if newest is None or as_utc(run.started_at) > as_utc(newest[0].started_at):
            working[gk] = (run, running_items[run.id])

    needs = [
        *_group_attention(unsettled, candidates, working),
        *_kill_attention(unsettled, working, kills, kill_refs, now_utc),
        *_exhausted_attention(exhausted, pr_keys, unsettled, working, address_runs, project_dir),
    ]
    attention = _attention_items(needs, project_dir)
    needing = {item["key"] for item in attention}
    today_entries = [*_running_entries(working, now_utc), *_group_entries(groups, working, rounds, needing)]
    today_entries.sort(key=lambda entry: entry["started_at"], reverse=True)

    # Titles may need a tracker round trip: resolved after the transaction closed, never inside it.
    lines = [*today_entries, *attention]
    titled = await attach_titles(project_dir, {"groups": lines})
    for line, group in zip(lines, titled["groups"], strict=True):
        line["title"] = group["title"]

    background_total = sum(background_counts.values())
    return {
        "generated_at": iso_utc(now_utc),
        "tz": _tz_key(zone),
        "today": today_entries,
        "attention": attention,
        "background": [
            {"kind": kind, "count": count, "text": narrate.background_line(kind, count)}
            for kind, count in background_counts.items()
        ],
        "background_summary": narrate.background_summary(background_total),
        "periods": periods,
        "empty_today_text": narrate.nothing_today(),
        "labels": narrate.labels(),
    }
