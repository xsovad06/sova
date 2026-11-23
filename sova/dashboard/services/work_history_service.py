"""Grouped run history: one row per issue (else PR), with its state judged from all of its finished runs.

``get_grouped_history()`` pages over groups ordered by the newest *matching* run, and returns each group's matching
runs. The group's state comes from an unfiltered read of the page's groups, so a date or action filter never changes
it (a merged issue stays Merged under a narrow range). Like ``get_work_history()`` it sees finished runs only, so a
run in flight does not change a state. The client keeps no state table: it maps the returned colour token.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import ColumnElement, Over, Select, String, Subquery, and_, case, cast, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from sova.dashboard.services.run_outcome import ADDRESS_REVIEW_NEXT_ACTION, next_action_of
from sova.dashboard.services.work_service import (
    _TERMINAL,
    _action_clause,
    _batch_step_names,
    _build_history_item,
    _format_duration_ms,
    history_filters,
)
from sova.dashboard.services.work_state import (
    HistoryState,
    history_state_color,
    history_state_label,
)
from sova.dashboard.services.work_title_service import resolve_titles
from sova.db.models import TaskRun
from sova.utils.formatting import format_cost_usd

# Statuses of a run that did its job. A researcher run that waits for the spec to be approved ends awaiting_approval.
_SUCCEEDED = "done"
_RESEARCH_DONE = frozenset({"done", "awaiting_approval"})
_FAILED = "failed"

# The group a run belongs to: its issue, else the issue of the newest run carrying the same PR (an Integrate or
# Address review started from a PR number alone joins its issue; a run with no PR never joins by this rule), else its
# PR, else itself (so it still shows as its own row). The literals are inline, not bound parameters, so every query
# renders one expression. A query groups or partitions by the key through ``_keyed()``, which names it as a column,
# never by the expression itself.
_NO_ISSUE = literal_column("''")
_HAS_ISSUE = (TaskRun.issue_number.isnot(None)) & (TaskRun.issue_number != _NO_ISSUE)
_linked = aliased(TaskRun)
# Each PR's issue, computed once per query (a window over the issue-carrying runs, newest first) and joined in,
# not evaluated per row as a correlated subquery: the grouped reads stay linear-ish as the history grows.
_ranked_links = (
    select(
        _linked.pr_number.label("pr_number"),
        _linked.issue_number.label("issue_number"),
        func.row_number()
        .over(partition_by=_linked.pr_number, order_by=(_linked.started_at.desc(), _linked.id.desc()))
        .label("rn"),
    )
    .where(_linked.pr_number.isnot(None), _linked.issue_number.isnot(None), _linked.issue_number != _NO_ISSUE)
    .subquery("pr_issue_ranked")
)
_PR_ISSUE = (
    select(_ranked_links.c.pr_number, _ranked_links.c.issue_number).where(_ranked_links.c.rn == 1).subquery("pr_issue")
)
_WITH_LINK = TaskRun.__table__.outerjoin(_PR_ISSUE, _PR_ISSUE.c.pr_number == TaskRun.pr_number)
_ISSUE_PREFIX = "issue:"
_GROUP_KEY = case(
    (_HAS_ISSUE, literal_column(f"'{_ISSUE_PREFIX}'") + TaskRun.issue_number),
    (_PR_ISSUE.c.issue_number.isnot(None), literal_column(f"'{_ISSUE_PREFIX}'") + _PR_ISSUE.c.issue_number),
    (TaskRun.pr_number.isnot(None), literal_column("'pr:'") + cast(TaskRun.pr_number, String)),
    else_=literal_column("'run:'") + cast(TaskRun.id, String),
)


def _keyed(*conditions: ColumnElement[bool], research: bool = False) -> Subquery:
    """The runs matching ``conditions``: id, status, start time and group key (``gk``), and with ``research`` whether
    the run is a researcher run."""
    columns = [TaskRun.id.label("id"), TaskRun.status.label("status"), TaskRun.started_at.label("started_at")]
    if research:
        columns.append(case((_action_clause("research"), 1), else_=0).label("research"))
    return select(*columns, _GROUP_KEY.label("gk")).select_from(_WITH_LINK).where(*conditions).subquery()


def _newest_first(runs: Subquery) -> Over:
    """Each run's rank in its group, newest first."""
    return func.row_number().over(partition_by=runs.c.gk, order_by=(runs.c.started_at.desc(), runs.c.id.desc()))


def history_state(merged: bool, newest: dict, next_action: str) -> HistoryState:
    """The state of a group: ``newest`` is its newest finished run's history item, ``merged`` whether an Integrate
    run succeeded."""
    if merged:
        return HistoryState.MERGED
    if newest["status"] == _FAILED:
        return HistoryState.FAILED
    action = newest["action"]
    if action == "research" and newest["status"] in _RESEARCH_DONE:
        return HistoryState.NEEDS_APPROVAL
    # Stopped, interrupted, paused or rejected: the run did not do its job, so it waits for nothing it would have made.
    if newest["status"] != _SUCCEEDED:
        return HistoryState.STOPPED
    if action == "review":
        return HistoryState.WAITING_FIXES if next_action == ADDRESS_REVIEW_NEXT_ACTION else HistoryState.WAITING_REVIEW
    if action in ("develop", "address-review", "rebase"):
        return HistoryState.WAITING_REVIEW
    return HistoryState.FINISHED


async def _page_of_groups(session: AsyncSession, filters: list, limit: int, offset: int) -> tuple[list[str], int]:
    """The group keys of one page, newest matching run first, and the number of matching groups."""
    runs = _keyed(*filters)
    newest = func.max(runs.c.started_at).label("newest")
    groups = select(runs.c.gk, newest).group_by(runs.c.gk)
    total = await session.scalar(select(func.count()).select_from(groups.subquery()))
    rows = await session.execute(
        groups.order_by(newest.desc(), runs.c.gk.desc()).limit(max(min(limit, 200), 0)).offset(max(offset, 0))
    )
    return [row.gk for row in rows], total or 0


async def _newest_runs(session: AsyncSession, keys: list[str]) -> dict[str, TaskRun]:
    """Each group's newest finished run, whatever the filters."""
    runs = _keyed(TaskRun.status.in_(_TERMINAL))
    ranked = select(runs.c.id, runs.c.gk, _newest_first(runs).label("rn")).where(runs.c.gk.in_(keys)).subquery()
    rows = await session.execute(
        select(TaskRun, ranked.c.gk).join(ranked, ranked.c.id == TaskRun.id).where(ranked.c.rn == 1)
    )
    return {gk: run for run, gk in rows}


async def _group_prs(session: AsyncSession, keys: list[str]) -> dict[str, int]:
    """Each group's PR: the one on its newest run that carries a PR, whatever the filters (a filter that hides the
    Develop run does not hide the PR the group has)."""
    rows = await session.execute(
        select(_GROUP_KEY, TaskRun.pr_number)
        .select_from(_WITH_LINK)
        .where(TaskRun.pr_number.isnot(None), _GROUP_KEY.in_(keys))
        .order_by(TaskRun.started_at.desc(), TaskRun.id.desc())
    )
    prs: dict[str, int] = {}
    for gk, pr in rows:
        prs.setdefault(gk, pr)
    return prs


def _did_merge(handoff: object) -> bool:
    """Whether a successful Integrate run merged: only when its closing handoff says ``merged: true``. A run that
    stopped at a gate says ``false``, and a run with no marker (from before it existed, or one that ended without
    reaching the closing block) is not claimed as merged: its group reads Finished, never a wrong Merged."""
    details = handoff if isinstance(handoff, dict) else {}
    return details.get("merged") is True


async def _merged_keys(session: AsyncSession, keys: list[str] | Select) -> set[str]:
    """The groups among ``keys`` (a list or a subquery) that merged their PR: a successful Integrate run that merged."""
    merged = and_(TaskRun.status == _SUCCEEDED, _action_clause("integrate"), _GROUP_KEY.in_(keys))
    rows = await session.execute(select(_GROUP_KEY, TaskRun.handoff_json).select_from(_WITH_LINK).where(merged))
    return {gk for gk, handoff in rows if _did_merge(handoff)}


async def _summarize(session: AsyncSession, filters: list) -> dict:
    """Merged, need-you and failed counts over every matching group, not only the page's."""
    matching = select(_keyed(*filters).c.gk)
    runs = _keyed(TaskRun.status.in_(_TERMINAL), research=True)
    ranked = (
        select(runs.c.gk, runs.c.status, runs.c.research, _newest_first(runs).label("rn"))
        .where(runs.c.gk.in_(matching))
        .subquery()
    )
    newest = (
        await session.execute(select(ranked.c.gk, ranked.c.status, ranked.c.research).where(ranked.c.rn == 1))
    ).all()
    merged = await _merged_keys(session, matching)
    summary = {"merged": 0, "need_you": 0, "failed": 0}
    for gk, status, research in newest:
        if gk in merged:
            summary["merged"] += 1
        elif status == _FAILED:
            summary["failed"] += 1
        elif research and status in _RESEARCH_DONE:
            summary["need_you"] += 1
    return summary


def _group_payload(key: str, runs: list[dict], newest_run: TaskRun, newest: dict, merged: bool, pr: int | None) -> dict:
    next_action = next_action_of(newest_run.handoff_json)
    state = history_state(merged, newest, next_action)
    latest = runs[0]
    durations = [r["duration_ms"] for r in runs if r["duration_ms"] is not None]
    duration_ms = sum(durations) if durations else None
    # From the key, not the runs: a group's only matching run may be a PR-only run that joined its issue.
    issue = key.removeprefix(_ISSUE_PREFIX) if key.startswith(_ISSUE_PREFIX) else None
    return {
        "key": key,
        "issue_number": issue,
        "pr_number": pr,
        "title": None,
        "state": state.value,
        "state_label": history_state_label(state),
        "state_color": history_state_color(state),
        # What the state was judged from: the newest finished run overall and whether an Integrate run succeeded.
        "state_inputs": {
            "merged": merged,
            "newest_run_id": newest["id"],
            "newest_action": newest["action"],
            "newest_status": newest["status"],
            "next_action": next_action,
        },
        "run_count": len(runs),
        "duration_ms": duration_ms,
        "duration_formatted": _format_duration_ms(duration_ms) if duration_ms else None,
        "total_cost_usd": format_cost_usd(sum((Decimal(r["total_cost_usd"]) for r in runs), Decimal(0))),
        "started_at": runs[-1]["started_at"],
        "last_started_at": latest["started_at"],
        "result": {"text": latest["outcome"], "color": "red" if latest["status"] == _FAILED else "gray"},
        "runs": runs,
    }


async def get_grouped_history(
    session: AsyncSession,
    *,
    status: str | None = None,
    role: str | None = None,
    action: str | None = None,
    number: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict:
    """One page of groups (by issue, else PR, else the run alone), each with its matching runs and its state.

    A group is listed when any of its runs match the filters, ordered by its newest matching run; its run count,
    duration, cost and result cover the matching runs only. Returns ``{"groups": [...], "total": <groups>,
    "summary": {"groups", "merged", "need_you", "failed"}, "runs_total": N, "total_cost_usd": "...",
    "failed_total": N}``; a page past the last group has no groups and the right ``total``. Every group's ``title``
    is None: ``attach_titles()`` fills them, and the caller runs it after the transaction closes, because a title
    may need a tracker round trip that must not hold a database transaction open.
    """
    shared, filters = history_filters(status=status, role=role, action=action, number=number, since=since, until=until)
    keys, total = await _page_of_groups(session, filters, limit, offset)
    runs_total, cost = (
        await session.execute(
            select(func.count(TaskRun.id), func.coalesce(func.sum(TaskRun.total_cost_usd), 0)).where(*filters)
        )
    ).one()
    failed_total = await session.scalar(select(func.count(TaskRun.id)).where(*shared, TaskRun.status == _FAILED))
    summary = {"groups": total, **await _summarize(session, filters)}
    payload = {
        "groups": [],
        "total": total,
        "summary": summary,
        "runs_total": runs_total or 0,
        "total_cost_usd": format_cost_usd(cost),
        "failed_total": failed_total or 0,
    }
    if not keys:
        return payload

    matching = (
        await session.execute(
            select(TaskRun, _GROUP_KEY.label("gk"))
            .select_from(_WITH_LINK)
            .where(*filters, _GROUP_KEY.in_(keys))
            .order_by(TaskRun.started_at.desc(), TaskRun.id.desc())
        )
    ).all()
    newest_by_key = await _newest_runs(session, keys)
    merged = await _merged_keys(session, keys)
    group_prs = await _group_prs(session, keys)
    every_run = {run.id: run for run, _ in matching} | {run.id: run for run in newest_by_key.values()}
    steps = await _batch_step_names(session, list(every_run))
    items = {run_id: _build_history_item(run, steps.get(run_id, set())) for run_id, run in every_run.items()}

    by_key: dict[str, list[TaskRun]] = {}
    for run, gk in matching:
        by_key.setdefault(gk, []).append(run)
    # A run linked to a PR or issue between the reads above changes its key, so a page key can have no run left
    # under it: that group is skipped (it shows under its new key on the next load) rather than failing the page.
    groups = [
        _group_payload(
            gk,
            [items[r.id] for r in by_key[gk]],
            newest_by_key[gk],
            items[newest_by_key[gk].id],
            gk in merged,
            group_prs.get(gk),
        )
        for gk in keys
        if gk in by_key and gk in newest_by_key
    ]
    payload["groups"] = groups
    return payload


async def attach_titles(project_dir: Path | None, payload: dict) -> dict:
    """Fill each group's ``title`` from the snapshot, the cache or a bounded tracker lookup (see
    ``work_title_service``). Call it outside any database transaction. A title that cannot be had stays None."""
    groups = payload["groups"]
    if not groups:
        return payload
    issue_titles, pr_titles = await resolve_titles(
        project_dir,
        (g["issue_number"] for g in groups if g["issue_number"]),
        (g["pr_number"] for g in groups if g["pr_number"] and not g["issue_number"]),
    )
    titled = [
        {
            **group,
            "title": (
                issue_titles.get(group["issue_number"]) if group["issue_number"] else pr_titles.get(group["pr_number"])
            ),
        }
        for group in groups
    ]
    return {**payload, "groups": titled}
