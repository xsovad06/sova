"""The Work board payload: one column per issue, the ranking behind Up next, hiding, queue pruning, done today
and chains."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.adapters.base import Task, TaskState
from sova.adapters.priority import Priority
from sova.dashboard.services import work_board_service as board
from sova.dashboard.services.work_item_service import rank_graph_entries
from sova.supervisor.dependency_graph import DependencyGraph

PROJECT = Path("/tmp/board-test-project")


@pytest.fixture(autouse=True)
def _fresh_cache():
    board.reset_board_cache()
    yield
    board.reset_board_cache()


def _task(
    number: int,
    *,
    state: TaskState = TaskState.RESEARCHED,
    labels: tuple[str, ...] = ("agent:ready",),
    body: str = "",
    milestone: str = "",
) -> Task:
    return Task(
        id=str(number),
        title=f"Issue {number}",
        state=state,
        labels=list(labels),
        priority=Priority.from_labels(list(labels)),
        body=body,
        milestone=milestone,
        url=f"https://example.test/{number}",
    )


def _item(
    number: int | None,
    state: str = "researched",
    *,
    mode: str | None = "autonomous",
    reasons: list[dict] | None = None,
    run_status: str | None = None,
    pr_number: int | None = None,
    pr_details: dict | None = None,
    running_agent: dict | None = None,
    blocked_by: list[int] | None = None,
) -> dict:
    return {
        "issue_number": str(number) if number is not None else None,
        "pr_number": pr_number,
        "title": f"Issue {number}",
        "url": f"https://example.test/{number}",
        "state": state,
        "issue_priority": "medium",
        "last_run": {"status": run_status} if run_status else None,
        "suitability": None
        if mode is None
        else {"mode": mode, "reasons": reasons or [], "blocked_by": blocked_by or []},
        "pr_details": pr_details,
        "running_agent": running_agent,
    }


@contextmanager
def _sources(
    *,
    items: list[dict] | None = None,
    tasks: list[Task] | None = None,
    queue: list[int] | None = None,
    hidden: list[int] | None = None,
    runs: dict | None = None,
    runs_unavailable: bool = False,
    done_today: list[dict] | None = (),  # type: ignore[assignment]
    running: set[int] | None = None,
    ranking_graph: DependencyGraph | None = None,
):
    graph = DependencyGraph(tasks or [])
    ranking = ranking_graph or graph
    loaded = AsyncMock(side_effect=lambda _project, milestone="": ranking if milestone else graph)
    remove = AsyncMock(return_value=[])
    with (
        patch.object(board, "load_ranking_graph", loaded),
        patch.object(board, "get_work_items", AsyncMock(return_value={"items": items or []})),
        patch.object(board, "next_task_exclusions", AsyncMock(return_value=(running or set(), set()))),
        patch.object(board, "load_task_queue", AsyncMock(return_value=list(queue or []))),
        patch.object(board, "load_hidden_issues", AsyncMock(return_value=list(hidden or []))),
        patch.object(board, "_active_runs", AsyncMock(return_value=None if runs_unavailable else runs or {})),
        patch.object(
            board, "_done_today", AsyncMock(return_value=list(done_today) if done_today is not None else None)
        ),
        patch.object(board, "remove_from_task_queue", remove),
    ):
        yield SimpleNamespace(remove=remove, loaded=loaded)


def _numbers(entries: list[dict]) -> list[int | None]:
    return [e["number"] for e in entries]


COLUMNS = ("up_next", "queued", "running", "review", "needs", "yours", "blocked", "done_today")


class TestColumns:
    async def test_every_issue_is_in_exactly_one_column_with_its_stage_and_state_display(self) -> None:
        items = [
            _item(1),  # next
            _item(2, mode="autonomous"),  # queued
            _item(3, "agent_running", running_agent={"run_id": 7, "role": "developer", "elapsed_seconds": 90}),
            _item(
                4,
                "pr_ci_running",
                pr_number=40,
                pr_details={"ci_status": "running", "thread_total": 3, "thread_resolved": 1},
            ),
            _item(5, "pr_ready_to_merge", pr_number=50),
            _item(6, "triaged", mode="epic", reasons=[{"source": "label", "value": "type: epic"}]),
            _item(7, "triaged", mode="blocked", blocked_by=[1]),
            _item(8, "triaged", mode="unclassified"),
            _item(9, "triaged", mode="blocked", run_status="failed"),
        ]
        tasks = [_task(n) for n in range(1, 10)]
        with _sources(items=items, tasks=tasks, queue=[2]):
            result = await board.build_board(PROJECT)

        placed = [n for column in COLUMNS for n in _numbers(result[column])]
        assert sorted(placed) == list(range(1, 10))
        assert len(placed) == len(set(placed))
        assert _numbers(result["up_next"]) == [1]
        assert _numbers(result["queued"]) == [2]
        assert _numbers(result["running"]) == [3]
        assert _numbers(result["review"]) == [4]
        assert _numbers(result["needs"]) == [5, 9]
        assert _numbers(result["yours"]) == [6, 8]
        assert _numbers(result["blocked"]) == [7]
        for column in COLUMNS[:-1]:
            for entry in result[column]:
                assert entry["stage"] == ("next" if column == "up_next" else column)
                assert entry["state_label"]
                assert entry["state_color"]
        assert result["stages"] == ["next", "queued", "running", "review", "needs", "yours", "blocked", "done"]

    async def test_unclassified_and_blocked_appears_once_as_yours(self) -> None:
        items = [_item(1, "triaged", mode="unclassified", blocked_by=[2])]
        with _sources(items=items, tasks=[_task(1, labels=()), _task(2, state=TaskState.DONE)], queue=[1]):
            result = await board.build_board(PROJECT)

        assert [n for c in COLUMNS for n in _numbers(result[c])] == [1]
        assert _numbers(result["yours"]) == [1]

    async def test_running_review_and_needs_entries_carry_their_details(self) -> None:
        items = [
            _item(
                3,
                "agent_running",
                running_agent={"run_id": 7, "role": "developer", "elapsed_seconds": 90},
                pr_details={"ci_status": "running"},
            ),
            _item(
                4,
                "pr_awaiting_review",
                pr_number=40,
                pr_details={"ci_status": "passed", "thread_total": 3, "thread_resolved": 1},
            ),
            _item(5, "pr_ready_to_merge", pr_number=50),
            _item(6, "spec_review"),
            _item(7, "triaged", run_status="failed"),
            _item(8, "human_only", mode="human", reasons=[{"source": "label", "value": "agent:human-only"}]),
        ]
        runs = {
            "run:7": {
                "role": "developer",
                "current_step": "develop",
                "step_index": 5,
                "total_steps": 17,
                "total_cost_usd": "1.25",
            }
        }
        with _sources(items=items, tasks=[_task(n) for n in range(3, 9)], runs=runs):
            result = await board.build_board(PROJECT)

        running = result["running"][0]
        assert (running["role"], running["step_name"], running["step_index"], running["total_steps"]) == (
            "developer",
            "develop",
            5,
            17,
        )
        assert (running["ci_state"], running["elapsed_seconds"], running["cost_usd"]) == ("running", 90, "1.25")
        review = result["review"][0]
        assert (review["pr_number"], review["ci_state"], review["threads_resolved"], review["threads_total"]) == (
            40,
            "passed",
            1,
            3,
        )
        assert [(e["number"], e["kind"]) for e in result["needs"]] == [(5, "merge"), (6, "spec"), (7, "failed_run")]
        assert result["yours"][0]["reason"] == "agent:human-only"
        assert result["yours"][0]["reason_source"] == "label"

    async def test_standalone_pr_without_an_issue_is_listed_with_its_pr_number(self) -> None:
        items = [_item(None, "pr_ready_to_merge", mode=None, pr_number=77)]
        with _sources(items=items):
            result = await board.build_board(PROJECT)

        assert result["needs"][0]["number"] is None
        assert result["needs"][0]["pr_number"] == 77


class TestUpNext:
    def _pool(self, count: int) -> tuple[list[dict], list[Task]]:
        return [_item(n) for n in range(1, count + 1)], [_task(n) for n in range(1, count + 1)]

    async def test_at_most_five_in_ranking_order_with_the_ranking_fields(self) -> None:
        items = [_item(n) for n in range(1, 9)]
        tasks = [
            _task(1, milestone="Phase 1: Ship"),
            *(_task(n) for n in range(2, 6)),
            _task(6, labels=("agent:ready", "priority: critical")),
            _task(7, body="## Dependencies\n- #8\n"),
            _task(8),
        ]
        with _sources(items=items, tasks=tasks):
            result = await board.build_board(PROJECT)

        expected = rank_graph_entries(DependencyGraph(tasks), running=set(), open_prs=set())
        assert _numbers(result["up_next"]) == [e["id"] for e in expected][:5]
        assert result["more_ready"] == len(expected) - 5
        by_number = {e["number"]: e for e in result["up_next"]}
        assert by_number[6]["rank"] == 1
        assert by_number[6]["priority"] == "critical"
        assert by_number[1]["milestone"] == "Phase 1: Ship"
        assert by_number[8]["unblocks"] == 1

    async def test_an_issue_that_unblocks_others_reports_how_many_are_open(self) -> None:
        tasks = [_task(1), _task(2, labels=(), state=TaskState.BACKLOG, body="## Dependencies\n- #1\n")]
        with _sources(items=[_item(1)], tasks=tasks):
            result = await board.build_board(PROJECT)

        assert result["up_next"][0]["unblocks"] == 1

    async def test_milestone_ranks_from_the_milestone_graph_and_the_rest_from_the_full_one(self) -> None:
        full = [_task(1), _task(2, milestone="M1")]
        only_m1 = DependencyGraph([full[1]])
        with _sources(items=[_item(1), _item(2)], tasks=full, ranking_graph=only_m1) as src:
            result = await board.build_board(PROJECT, "M1")

        assert _numbers(result["up_next"]) == [2]
        assert src.loaded.await_args_list[-1].args == (PROJECT, "M1")

    async def test_hiding_frees_a_slot_for_the_next_ranked_issue_and_lists_the_hidden_one(self) -> None:
        items, tasks = self._pool(7)
        with _sources(items=items, tasks=tasks, hidden=[1]) as _:
            result = await board.build_board(PROJECT)

        assert _numbers(result["up_next"]) == [2, 3, 4, 5, 6]
        assert result["more_ready"] == 1
        assert _numbers(result["hidden"]) == [1]

    async def test_fewer_than_five_after_hiding_never_reports_a_negative_more_ready(self) -> None:
        items, tasks = self._pool(5)
        with _sources(items=items, tasks=tasks, hidden=[1, 2]):
            result = await board.build_board(PROJECT)

        assert _numbers(result["up_next"]) == [3, 4, 5]
        assert result["more_ready"] == 0

    async def test_a_hidden_issue_that_is_running_queued_or_in_review_still_appears_there(self) -> None:
        items = [
            _item(1, "agent_running"),
            _item(2),
            _item(3, "pr_awaiting_review", pr_number=30, pr_details={}),
        ]
        with _sources(items=items, tasks=[_task(n) for n in (1, 2, 3)], queue=[2], hidden=[1, 2, 3]):
            result = await board.build_board(PROJECT)

        assert _numbers(result["running"]) == [1]
        assert _numbers(result["queued"]) == [2]
        assert _numbers(result["review"]) == [3]
        assert result["up_next"] == []
        assert _numbers(result["hidden"]) == [1, 2, 3]

    async def test_a_hidden_issue_that_was_closed_or_deleted_is_still_listed(self) -> None:
        tasks = [_task(1, state=TaskState.DONE)]
        with _sources(items=[], tasks=tasks, hidden=[1, 404]):
            result = await board.build_board(PROJECT)

        assert _numbers(result["hidden"]) == [1, 404]
        assert result["hidden"][1]["title"] == ""

    async def test_a_queued_issue_is_not_offered_again_in_up_next(self) -> None:
        with _sources(items=[_item(1), _item(2)], tasks=[_task(1), _task(2)], queue=[1]):
            result = await board.build_board(PROJECT)

        assert _numbers(result["up_next"]) == [2]


class TestQueue:
    async def test_a_queued_issue_the_work_items_lack_is_not_offered_in_up_next(self) -> None:
        with _sources(items=[_item(2)], tasks=[_task(1), _task(2)], queue=[1]):
            result = await board.build_board(PROJECT)

        assert _numbers(result["queued"]) == [1]
        assert _numbers(result["up_next"]) == [2]

    async def test_queued_follows_the_queue_order(self) -> None:
        items = [_item(n) for n in (1, 2, 3)]
        with _sources(items=items, tasks=[_task(n) for n in (1, 2, 3)], queue=[3, 1, 2]):
            result = await board.build_board(PROJECT)

        assert _numbers(result["queued"]) == [3, 1, 2]

    async def test_closed_issues_leave_the_queue_and_are_saved_once(self) -> None:
        tasks = [_task(1), _task(2, state=TaskState.DONE), _task(3, state=TaskState.DONE)]
        with _sources(items=[_item(1)], tasks=tasks, queue=[2, 1, 3]) as src:
            result = await board.build_board(PROJECT)

        assert _numbers(result["queued"]) == [1]
        src.remove.assert_awaited_once_with(PROJECT, [2, 3])

    async def test_an_unchanged_queue_is_not_saved(self) -> None:
        with _sources(items=[_item(1)], tasks=[_task(1)], queue=[1]) as src:
            await board.build_board(PROJECT)

        src.remove.assert_not_awaited()

    async def test_the_board_never_adds_a_queue_entry(self) -> None:
        with _sources(items=[_item(1), _item(2)], tasks=[_task(1), _task(2)], queue=[]) as src:
            result = await board.build_board(PROJECT)

        assert result["queued"] == []
        src.remove.assert_not_awaited()

    async def test_a_failed_save_still_hides_the_closed_issue(self) -> None:
        tasks = [_task(1), _task(2, state=TaskState.DONE)]
        with _sources(items=[_item(1)], tasks=tasks, queue=[2, 1]) as src:
            src.remove.side_effect = RuntimeError("db gone")
            result = await board.build_board(PROJECT)

        assert _numbers(result["queued"]) == [1]

    async def test_a_failed_save_is_named_unavailable(self) -> None:
        tasks = [_task(1), _task(2, state=TaskState.DONE)]
        with _sources(items=[_item(1)], tasks=tasks, queue=[2, 1]) as src:
            src.remove.side_effect = RuntimeError("db gone")
            result = await board.build_board(PROJECT)

        assert result["unavailable"] == ["queue_prune"]

    async def test_a_queued_issue_the_graph_shows_in_progress_is_running_not_queued(self) -> None:
        tasks = [_task(7, state=TaskState.IN_PROGRESS), _task(8, state=TaskState.HUMAN_ONLY)]
        with _sources(items=[], tasks=tasks, queue=[7, 8]):
            result = await board.build_board(PROJECT)

        assert result["queued"] == []
        assert _numbers(result["running"]) == [7]
        assert _numbers(result["yours"]) == [8]
        assert result["running"][0]["stage"] == "running"

    async def test_the_adapter_of_a_queue_lookup_is_closed_after_a_failed_lookup(self) -> None:
        adapter = SimpleNamespace(get_state=AsyncMock(side_effect=RuntimeError("down")), close=AsyncMock())
        with (
            _sources(items=[], tasks=[], queue=[7]),
            patch("sova.adapters.create_adapter", return_value=adapter),
            patch("sova.config.loader.load_config", return_value=SimpleNamespace()),
        ):
            await board.build_board(PROJECT)

        adapter.close.assert_awaited_once()

    async def test_a_failed_lookup_for_the_whole_batch_keeps_the_queue_and_does_not_save(self) -> None:
        # 7 and 8 are not in the graph, so the tracker is asked, and it fails for both
        adapter = SimpleNamespace(get_state=AsyncMock(side_effect=RuntimeError("tracker down")))
        with (
            _sources(items=[], tasks=[], queue=[7, 8]) as src,
            patch("sova.adapters.create_adapter", return_value=adapter),
            patch("sova.config.loader.load_config", return_value=SimpleNamespace()),
        ):
            result = await board.build_board(PROJECT)

        src.remove.assert_not_awaited()
        assert _numbers(result["queued"]) == [7, 8]

    async def test_an_issue_the_graph_lacks_is_pruned_only_on_tracker_evidence(self) -> None:
        adapter = SimpleNamespace(get_state=AsyncMock(side_effect=[TaskState.DONE, RuntimeError("down")]))
        with (
            _sources(items=[], tasks=[], queue=[7, 8]) as src,
            patch("sova.adapters.create_adapter", return_value=adapter),
            patch("sova.config.loader.load_config", return_value=SimpleNamespace()),
        ):
            result = await board.build_board(PROJECT)

        src.remove.assert_awaited_once_with(PROJECT, [7])
        assert _numbers(result["queued"]) == [8]


class TestDoneToday:
    NOW = datetime(2026, 11, 13, 15, 30, tzinfo=timezone.utc)

    @staticmethod
    def _merged(number: int, merged_at: datetime, body: str = "", closing: list[int] | None = None) -> dict:
        return {
            "number": number,
            "title": f"PR {number}",
            "url": f"https://example.test/pull/{number}",
            "mergedAt": merged_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "body": body,
            "headRefName": "",
            "closingIssuesReferences": [{"number": n} for n in closing or []],
        }

    @contextmanager
    def _merged_prs(self, prs: list[dict] | Exception):
        cfg = SimpleNamespace(github_repo="owner/repo", github_user="")
        fetch = AsyncMock(side_effect=prs) if isinstance(prs, Exception) else AsyncMock(return_value=prs)
        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch.object(board, "list_merged_prs_since", fetch),
        ):
            yield fetch

    async def test_only_prs_merged_since_local_midnight_with_pr_number_and_time(self) -> None:
        midnight = board._local_midnight(self.NOW)
        prs = [
            self._merged(1, midnight - timedelta(seconds=1), closing=[11]),  # the day before
            self._merged(2, midnight, closing=[12]),  # exactly midnight is the new day
            self._merged(3, midnight + timedelta(hours=5), closing=[13]),
        ]
        with self._merged_prs(prs):
            entries = await board._done_today(PROJECT, self.NOW)

        assert [(e["number"], e["pr_number"]) for e in entries] == [(13, 3), (12, 2)]  # newest first
        assert entries[1]["merged_at"] == midnight.astimezone(timezone.utc).isoformat()
        assert entries[0]["stage"] == "done"
        assert entries[0]["state_label"] == "Merged"

    async def test_a_pr_closing_several_issues_lists_each(self) -> None:
        midnight = board._local_midnight(self.NOW)
        with self._merged_prs([self._merged(9, midnight + timedelta(hours=1), closing=[21, 22])]):
            entries = await board._done_today(PROJECT, self.NOW)

        assert sorted(e["number"] for e in entries) == [21, 22]

    async def test_an_issue_two_merged_prs_link_appears_once_with_the_latest_merge(self) -> None:
        midnight = board._local_midnight(self.NOW)
        prs = [
            self._merged(4, midnight + timedelta(hours=1), closing=[30]),
            self._merged(5, midnight + timedelta(hours=3), closing=[30]),
            self._merged(6, midnight + timedelta(hours=2)),  # links no issue
            self._merged(7, midnight + timedelta(hours=4)),  # links no issue
        ]
        with self._merged_prs(prs):
            entries = await board._done_today(PROJECT, self.NOW)

        assert [(e["number"], e["pr_number"]) for e in entries] == [(None, 7), (30, 5), (None, 6)]

    async def test_a_merged_issue_the_cached_items_still_show_in_another_column_is_only_done(self) -> None:
        stale = _item(5, "pr_ready_to_merge", pr_number=50)
        merged = {
            "number": 5,
            "title": "Issue 5",
            "url": "",
            "stage": "done",
            "state": "merged",
            "state_label": "",
            "state_color": "",
            "priority": "",
        }
        with _sources(items=[stale, _item(6)], tasks=[_task(5), _task(6)], done_today=[merged]):
            result = await board.build_board(PROJECT)

        assert _numbers(result["done_today"]) == [5]
        assert result["needs"] == []
        assert _numbers(result["up_next"]) == [6]

    async def test_the_search_day_is_the_utc_day_of_local_midnight(self) -> None:
        with self._merged_prs([]) as fetch:
            await board._done_today(PROJECT, self.NOW)

        assert fetch.await_args.kwargs["since"] == board._local_midnight(self.NOW).astimezone(timezone.utc).date()

    async def test_the_merged_list_is_cached_briefly(self) -> None:
        with self._merged_prs([]) as fetch:
            await board._done_today(PROJECT, self.NOW)
            await board._done_today(PROJECT, self.NOW)

        assert fetch.await_count == 1

    async def test_a_failed_fetch_is_unavailable_never_an_empty_day(self) -> None:
        with self._merged_prs(RuntimeError("gh down")):
            assert await board._done_today(PROJECT, self.NOW) is None

        with _sources(done_today=None):
            result = await board.build_board(PROJECT)
        assert result["unavailable"] == ["done_today"]
        assert result["done_today"] == []


class TestChains:
    def _graph(self) -> list[Task]:
        return [
            _task(1),
            _task(2, labels=(), state=TaskState.BACKLOG, body="## Dependencies\n- #1\n"),
            _task(3, labels=(), state=TaskState.BACKLOG, body="## Dependencies\n- #2\n"),
            # a chain no board issue belongs to: 10 is open and unclassified, but not on the board
            _task(10, labels=(), state=TaskState.BACKLOG),
            _task(11, labels=(), state=TaskState.BACKLOG, body="## Dependencies\n- #10\n"),
            # closed work and epics are not part of a chain
            _task(20, state=TaskState.DONE),
            _task(21, labels=(), state=TaskState.BACKLOG, body="## Dependencies\n- #20\n"),
        ]

    async def test_only_chains_with_a_board_issue_are_listed_and_other_blocked_excludes_shown(self) -> None:
        with _sources(items=[_item(1)], tasks=self._graph()):
            result = await board.build_board(PROJECT)

        assert len(result["chains"]) == 1
        chain = result["chains"][0]["issues"]
        assert [i["number"] for i in chain] == [1, 2, 3]  # dependencies first
        assert chain[0]["stage"] == "next"
        assert chain[2]["depends_on"] == [2]
        # 11 waits on the open 10 and is on no chain shown; 21's only dependency is closed; 2 and 3 are shown
        assert result["other_blocked"] == 1

    async def test_a_chain_member_on_the_board_pulls_the_whole_chain_in(self) -> None:
        with _sources(items=[_item(3, "triaged", mode="blocked", blocked_by=[2])], tasks=self._graph()):
            result = await board.build_board(PROJECT)

        assert [i["number"] for i in result["chains"][0]["issues"]] == [1, 2, 3]

    async def test_no_chains_when_nothing_on_the_board_is_in_one(self) -> None:
        tasks = self._graph()
        tasks[0] = _task(1, labels=())  # no longer ready, so it is not offered in Up next either
        with _sources(items=[], tasks=tasks):
            result = await board.build_board(PROJECT)

        assert result["chains"] == []
        assert result["other_blocked"] == 3  # 2, 3 and 11 wait on an open issue; 21 waits on a closed one


class TestActiveRuns:
    async def test_runs_are_indexed_by_run_and_issue_with_the_newest_winning(self) -> None:
        runs = [{"id": 9, "issue_number": "5"}, {"id": 3, "issue_number": "5"}]  # newest first
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=session)
        with (
            patch("sova.db.session.get_session", AsyncMock(return_value=session)),
            patch("sova.dashboard.services.work_service.get_active_work", AsyncMock(return_value=runs)),
        ):
            indexed = await board._active_runs(PROJECT)

        assert indexed["issue:5"]["id"] == 9
        assert set(indexed) == {"run:9", "run:3", "issue:5"}

    async def test_a_failed_lookup_is_unavailable_never_no_runs(self) -> None:
        with patch("sova.db.session.get_session", AsyncMock(side_effect=RuntimeError("db gone"))):
            assert await board._active_runs(PROJECT) is None

    async def test_the_board_names_failed_active_runs_as_unavailable(self) -> None:
        items = [_item(3, "agent_running")]
        with _sources(items=items, tasks=[_task(3)], runs_unavailable=True):
            result = await board.build_board(PROJECT)

        assert result["unavailable"] == ["active_runs"]
        assert _numbers(result["running"]) == [3]
        assert result["running"][0]["step_name"] is None


class TestEntryDetails:
    def test_an_exhausted_review_budget_needs_the_user(self) -> None:
        assert board._reason_for_needs("pr_review_exhausted") == ("review_budget", "Review budget exhausted")

    def test_a_human_only_state_is_yours_even_with_an_autonomous_mode(self) -> None:
        item = _item(1, "human_only", mode="autonomous")
        assert board._reason_for_yours(item) == {"reason": "human_only", "reason_source": "state"}

    @pytest.mark.parametrize("raw", [None, "", "not a time"])
    def test_an_unreadable_merge_time_is_none(self, raw: str | None) -> None:
        assert board._parse_merged_at(raw) is None


class TestDoneTodaySources:
    NOW = datetime(2026, 11, 13, 15, 30, tzinfo=timezone.utc)

    async def test_a_project_without_a_github_repo_has_an_empty_day(self) -> None:
        with patch("sova.config.loader.load_config", return_value=SimpleNamespace(github_repo="", github_user="")):
            assert await board._done_today(PROJECT, self.NOW) == []

    async def test_a_new_day_evicts_the_previous_days_cache(self) -> None:
        cfg = SimpleNamespace(github_repo="owner/repo", github_user="")
        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch.object(board, "list_merged_prs_since", AsyncMock(return_value=[])),
        ):
            await board._done_today(PROJECT, self.NOW)
            await board._done_today(PROJECT, self.NOW + timedelta(days=1))

        assert len(board._merged_cache) == 1


class TestQueueLookupAdapter:
    async def test_no_adapter_keeps_every_unknown_entry(self) -> None:
        with (
            _sources(items=[], tasks=[], queue=[7, 8]) as src,
            patch("sova.config.loader.load_config", side_effect=RuntimeError("no config")),
        ):
            result = await board.build_board(PROJECT)

        src.remove.assert_not_awaited()
        assert _numbers(result["queued"]) == [7, 8]


class TestDependencyOrder:
    def test_a_cycle_is_ordered_by_number_after_its_dependencies(self) -> None:
        tasks = [
            _task(1),
            _task(2, body="## Dependencies\n- #1\n- #3\n"),
            _task(3, body="## Dependencies\n- #2\n"),
        ]
        graph = DependencyGraph(tasks)

        assert board._dependency_order({1, 2, 3}, graph) == [1, 2, 3]
