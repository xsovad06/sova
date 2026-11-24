"""What tells the Work board its sources changed (#1534): local run transitions and a GitHub probe.

Both end in ``invalidate_work_item_caches()``, which drops the stale source caches and rebuilds the board and
work-items snapshots in the background; a rebuilt payload that differs reaches the sockets as
``work_items_changed``.

* **Run transitions.** The status socket's producer reads the active runs every tick. ``board_transitions()``
  compares two ticks and reports a run leaving the active set, a run gaining a ``pr_number``, or a run finishing
  ``create_pr`` or ``monitor_ci``: the moments a PR list, a queue or a verdict can have changed locally. It covers
  CLI runs and any exit the dashboard's own finalize never sees; an exit finalize already invalidated for is not
  invalidated again.
* **GitHub probe.** While a visible Work tab is connected, ``run_github_probe()`` asks GitHub every
  ``interval`` seconds whether the PR list or the issue list changed, with ``If-None-Match``. A 304 costs no
  rate limit and invalidates nothing; a 200 invalidates the caches of the list that changed. A changed PR page
  drops only the verdicts of the PRs that moved on it (every verdict only when the change cannot be bounded: the
  first page, an unreadable one, or a full page of new entries). The new ETags are committed only after that
  invalidation, so a probe cancelled in between sees the same change again.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from sova.config.loader import load_config
from sova.git.conditional import ConditionalProbe, ProbeOutcome
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.dashboard.services.agent_status import AgentStatus

log = get_logger(component="dashboard.work_refresh")

# The steps whose completion changes what GitHub reports about a run's PR (opened, CI settled).
_BOARD_STEPS = frozenset({"create_pr", "monitor_ci"})

# After the probe itself was rate-limited, the next one waits this long instead of the usual interval.
PROBE_RATE_LIMIT_BACKOFF_SECONDS = 300.0

# Sorted by update time, newest first: any change to a PR or issue (opened, closed, merged, relabeled, edited,
# commented) moves it to the top with a new ``updated_at``, so the page changes with it. ``state=all`` keeps a close
# or merge visible, which the open-only list would not show once the item is no longer first.
# A 200 costs one request whatever its size, so the page is long enough to name the PRs that moved and drop only
# their verdicts, rather than every verdict of the project on each push, label or CI-driven update.
_PULLS_PAGE_SIZE = 20
_PULLS_ENDPOINT = "repos/{repo}/pulls?state=all&sort=updated&direction=desc&per_page=" + str(_PULLS_PAGE_SIZE)
# The issues endpoint lists pull requests too (with a ``pull_request`` key), and bots, CI and pushes keep them on
# top while agents run. A 200 costs one request whatever its size, so the page is long enough to tell a change to
# an issue from one to a PR alone (which the PR probe already covers) and skip the issue-side refetch for the latter.
_ISSUES_PAGE_SIZE = 20
_ISSUES_ENDPOINT = "repos/{repo}/issues?state=all&sort=updated&direction=desc&per_page=" + str(_ISSUES_PAGE_SIZE)


@dataclass(frozen=True)
class RunMark:
    """What one tick knew about an active run, for comparing with the next tick."""

    pr_number: int | None
    finished_steps: frozenset[str]
    status: str = ""
    current_step: str = ""


def mark_runs(statuses: Iterable[AgentStatus]) -> dict[int, RunMark]:
    """The ``RunMark`` of each active run, from the producer's ``AgentStatus`` list."""
    return {
        status.run_id: RunMark(
            pr_number=status.pr_number,
            finished_steps=frozenset(status.completed_steps) & _BOARD_STEPS,
            status=status.status,
            current_step=status.current_step or "",
        )
        for status in statuses
    }


def board_transitions(
    previous: Mapping[int, RunMark] | None,
    current: Mapping[int, RunMark],
    *,
    finalized: Collection[int] = frozenset(),
) -> tuple[bool, set[int]]:
    """``(changed, pr_numbers)``: whether any run moved in a way the board's caches care about, and its PRs.

    ``previous`` is ``None`` on the producer's first tick, which only records: a run already past ``create_pr``
    when the server started is not a transition. A run that appears between ticks is recorded the same way.
    Only the empty-to-set change of a ``pr_number`` counts. A run in ``finalized`` leaving the active set is not a
    transition either: the dashboard's finalize already invalidated the caches for that exit.
    """
    if previous is None:
        return False, set()
    changed = False
    pr_numbers: set[int] = set()
    for run_id, before in previous.items():
        moved, pr_number = _run_transition(before, current.get(run_id), finalized=run_id in finalized)
        if moved:
            changed = True
            if pr_number is not None:
                pr_numbers.add(pr_number)
    return changed, pr_numbers


def _run_transition(before: RunMark, after: RunMark | None, *, finalized: bool) -> tuple[bool, int | None]:
    """``(moved, pr_number)`` for one run between two ticks, by the rules of ``board_transitions()``."""
    if after is None:
        return (False, None) if finalized else (True, before.pr_number)
    gained_pr = before.pr_number is None and after.pr_number is not None
    if gained_pr or after.finished_steps - before.finished_steps:
        return True, after.pr_number
    return False, None


def apply_run_changes(
    project_dir: Path, previous: Mapping[int, RunMark] | None, current: Mapping[int, RunMark]
) -> bool:
    """Act on what moved between two ticks: invalidate once for a transition, else just rebuild the snapshots.

    A run that started, changed status or moved to another step changes the board's Running column but no source
    cache, so the snapshots are rebuilt from the caches as they are. So is a dashboard run's exit, whose caches its
    finalize already dropped (``finalized_run_ids()``): a second invalidation would refetch every PR again.

    Returns whether something moved that no snapshot rebuild will report (none was built yet, or every board key
    lapsed): the caller then tells the sockets itself, so an open board refetches instead of going stale.
    """
    from sova.dashboard.services.agent_finalize import finalized_run_ids, invalidate_work_item_caches
    from sova.dashboard.services.work_board_service import refresh_work_snapshots

    changed, pr_numbers = board_transitions(previous, current, finalized=finalized_run_ids())
    if changed:
        log.info("work_refresh.run_transition", project_dir=str(project_dir), pr_numbers=sorted(pr_numbers))
        rebuilding = invalidate_work_item_caches(project_dir, *sorted(pr_numbers))
    elif previous is not None and previous != current:
        rebuilding = refresh_work_snapshots(project_dir)
    else:
        return False
    return not rebuilding


# GitHub probe ------------------------------------------------------------------


# A page item: ``(number, updated_at, is_pr)``. The pulls endpoint lists PRs alone; the issues endpoint marks them.
_PageItem = tuple[int, str, bool]


@dataclass
class _PageMark:
    """The newest ``updated_at`` of the page last acted on, and each of its items as ``(number, updated_at)``.

    An item of a later page not among them and no older than the mark is what changed since.
    """

    mark: str | None = None
    seen: frozenset[tuple[int, str]] = frozenset()

    def fresh(self, page: list[_PageItem] | None, page_size: int) -> list[_PageItem] | None:
        """The items of a changed page that moved since the last one remembered; None when that cannot be told.

        Unreadable, or the first page, cannot be told. A full page whose every item is new may hide more past its
        end, so it cannot be told either.
        """
        if page is None or self.mark is None:
            return None
        fresh = [item for item in page if item[1] >= self.mark and item[:2] not in self.seen]
        if len(page) >= page_size and len(fresh) == len(page):
            return None
        return fresh

    def remember(self, page: list[_PageItem]) -> None:
        if page:
            self.mark = max(updated for _number, updated, _is_pr in page)
            self.seen = frozenset((number, updated) for number, updated, _is_pr in page)


@dataclass
class _ProjectProbe:
    """One project's probe: the tracker identity it was made for, its ETags, and the pages last acted on."""

    identity: tuple[str, str, str]
    probe: ConditionalProbe = field(default_factory=ConditionalProbe)
    issues: _PageMark = field(default_factory=_PageMark)
    pulls: _PageMark = field(default_factory=_PageMark)

    def issues_changed(self, page: list[_PageItem] | None) -> bool:
        """Whether a changed issue page shows a change to an issue, not only to PRs (which the PR probe covers)."""
        fresh = self.issues.fresh(page, _ISSUES_PAGE_SIZE)
        return fresh is None or any(not is_pr for _number, _updated, is_pr in fresh)

    def changed_prs(self, page: list[_PageItem] | None) -> list[int] | None:
        """The PRs a changed pulls page shows moving, sorted; None when every verdict must go."""
        fresh = self.pulls.fresh(page, _PULLS_PAGE_SIZE)
        return None if fresh is None else sorted({number for number, _updated, _is_pr in fresh})

    def remember(self, page: list[_PageItem]) -> None:
        """Remember an issue page acted on."""
        self.issues.remember(page)


# One probe per project, keyed by the resolved project: a changed repo, tracker or user starts over with no ETags.
# Lost on restart, when the first probe answers CHANGED and refreshes the caches once.
_probes: dict[str, _ProjectProbe] = {}


def reset_probes() -> None:
    """Forget every stored ETag. Test-only."""
    _probes.clear()


def _probe_for(project_dir: Path, identity: tuple[str, str, str]) -> _ProjectProbe:
    key = str(project_dir.resolve())
    known = _probes.get(key)
    if known is None or known.identity != identity:
        known = _ProjectProbe(identity=identity)
        _probes[key] = known
    return known


def _issue_page(body: str | None) -> list[_PageItem] | None:
    """``(number, updated_at, is_pr)`` per item of a pulls or issues page; None when the body is not one."""
    if body is None:
        return None
    try:
        items = json.loads(body)
    except ValueError:
        log.debug("work_refresh.issue_page_unparsed")
        return None
    if not isinstance(items, list):
        return None
    page: list[_PageItem] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("number"), int):
            return None
        page.append((item["number"], str(item.get("updated_at") or ""), "pull_request" in item))
    return page


def _invalidate_changed(
    project_dir: Path,
    repo: str,
    state: _ProjectProbe,
    *,
    prs_changed: bool,
    issues_changed: bool,
    pr_page: list[_PageItem] | None,
) -> bool:
    """Drop the caches a changed PR or issue list made stale. Synchronous, so it runs in the probe's no-await step.

    Returns whether a snapshot rebuild was requested, which then reports the change to the sockets itself.
    """
    from sova.dashboard.services.agent_finalize import invalidate_work_item_caches

    if issues_changed:
        from sova.dashboard.services.work_item_service import request_graph_rebuild
        from sova.supervisor.dependency_graph import invalidate_graph_cache

        # Up next ranks from the board's own copy of the dependency graph: drop the tracker-level cache, then
        # rebuild that copy behind the old one, which keeps answering. A changed graph rebuilds the snapshots
        # (``_on_graph_changed``).
        invalidate_graph_cache(repo)
        request_graph_rebuild(project_dir)
    if not prs_changed:
        return invalidate_work_item_caches(project_dir, prs=False)
    # The PR list goes, and the verdicts of the PRs that moved; every verdict only when which PRs moved
    # cannot be told. The queue goes either way.
    moved = state.changed_prs(pr_page)
    if moved is None:
        return invalidate_work_item_caches(project_dir, every_verdict=True)
    return invalidate_work_item_caches(project_dir, *moved)


async def probe_github_once(
    project_dir: Path, *, on_unreported: Callable[[Path], Awaitable[None]] | None = None
) -> ProbeOutcome | None:
    """Probe the PR and issue lists once and invalidate what changed; ``None`` when the project is not on GitHub.

    The returned outcome paces the next probe: ``RATE_LIMITED`` when either read was rate-limited. A change no
    snapshot rebuild will report (a board key lapsed while its tab was hidden, or no board built yet) is passed to
    *on_unreported*, which tells the sockets, so a visible tab never stays deaf to a change the probe saw.
    """
    cfg = await asyncio.to_thread(load_config, project_dir)
    if cfg.task_source.type != "github" or not cfg.github_repo:
        return None
    repo, user = cfg.github_repo, cfg.github_user
    state = _probe_for(project_dir, (cfg.task_source.type, repo, user))
    probe = state.probe
    pulls_endpoint = _PULLS_ENDPOINT.format(repo=repo)
    issues_endpoint = _ISSUES_ENDPOINT.format(repo=repo)
    prs = await probe.check(pulls_endpoint, github_user=user)
    if prs is ProbeOutcome.RATE_LIMITED:
        return prs
    issues = await probe.check(issues_endpoint, github_user=user)
    # No await from here on: the caches are invalidated and the new ETags committed in one step, so a probe
    # cancelled during a read (its tab hid) commits nothing and the next one sees the same change again.
    page = _issue_page(probe.body(issues_endpoint)) if issues is ProbeOutcome.CHANGED else None
    pr_page = _issue_page(probe.body(pulls_endpoint)) if prs is ProbeOutcome.CHANGED else None
    issues_changed = issues is ProbeOutcome.CHANGED and state.issues_changed(page)
    unreported = False
    if prs is ProbeOutcome.CHANGED or issues_changed:
        log.info("work_refresh.github_changed", project_dir=str(project_dir), prs=prs.value, issues=issues_changed)
        unreported = not _invalidate_changed(
            project_dir,
            repo,
            state,
            prs_changed=prs is ProbeOutcome.CHANGED,
            issues_changed=issues_changed,
            pr_page=pr_page,
        )
    elif issues is ProbeOutcome.CHANGED:
        log.debug("work_refresh.issue_list_moved_by_prs_only", project_dir=str(project_dir))
    probe.commit(pulls_endpoint)
    probe.commit(issues_endpoint)
    if page is not None:
        state.issues.remember(page)
    if pr_page is not None:
        state.pulls.remember(pr_page)
    if unreported and on_unreported is not None:
        # After the commit above: a cancel here costs only the broadcast, never the stored ETags.
        await on_unreported(project_dir)
    return issues


async def run_github_probe(
    project_dir: Path, *, interval: float, on_unreported: Callable[[Path], Awaitable[None]] | None = None
) -> None:
    """Probe every *interval* seconds, the first time at once, until cancelled; returns for a non-GitHub project."""
    while True:
        try:
            outcome = await probe_github_once(project_dir, on_unreported=on_unreported)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 (a failed probe is retried at the next interval, never ends the loop)
            log.warning("work_refresh.probe_failed", project_dir=str(project_dir), exc_info=True)
            outcome = ProbeOutcome.FAILED
        if outcome is None:
            log.debug("work_refresh.probe_skipped_not_github", project_dir=str(project_dir))
            return
        await asyncio.sleep(PROBE_RATE_LIMIT_BACKOFF_SECONDS if outcome is ProbeOutcome.RATE_LIMITED else interval)
