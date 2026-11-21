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
    assignees: tuple[str, ...] = (),
    jira_key: str = "",
) -> Task:
    return Task(
        id=str(number),
        title=f"Issue {number}",
        state=state,
        labels=list(labels),
        priority=Priority.from_labels(list(labels)),
        body=body,
        milestone=milestone,
        assignees=list(assignees),
        metadata={"key": jira_key} if jira_key else {},
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
    github_user: str = "",
    jira_display_name: str = "",
    open_prs: dict[int, set[int]] | None = None,
    open_prs_unavailable: bool = False,
):
    graph = DependencyGraph(tasks or [])
    ranking = ranking_graph or graph
    loaded = AsyncMock(side_effect=lambda _project, milestone="": ranking if milestone else graph)
    remove = AsyncMock(return_value=[])
    with (
        patch.object(board, "load_ranking_graph", loaded),
        patch.object(
            board,
            "get_work_items",
            AsyncMock(
                return_value={
                    "items": items or [],
                    "github_user": github_user,
                    "jira_display_name": jira_display_name,
                }
            ),
        ),
        patch.object(
            board,
            "board_exclusions",
            AsyncMock(return_value=(running or set(), set(), None if open_prs_unavailable else open_prs or {})),
        ),
        patch.object(board, "load_task_queue", AsyncMock(return_value=list(queue or []))),
        patch.object(board, "load_hidden_issues", AsyncMock(return_value=list(hidden or []))),
        patch.object(board, "_active_runs", AsyncMock(return_value=None if runs_unavailable else runs or {})),
        patch.object(
            board, "_done_today", AsyncMock(return_value=list(done_today) if done_today is not None else None)
        ),
        patch.object(board, "remove_from_task_queue", remove),
    ):
        yield SimpleNamespace(remove=remove, loaded=loaded)


def _merged_entry(number: int, pr_number: int, *, closes: bool = False, age_seconds: float = 30.0) -> dict:
    """A ``_done_today()`` entry: one merged PR linking *number* *age_seconds* ago, closing it when *closes*."""
    return {
        **board._issue_entry(
            number,
            title=f"PR {pr_number}",
            url=f"https://example.test/{number}",
            stage=board.WorkStage.DONE,
            state="merged",
            priority="",
        ),
        "pr_number": pr_number,
        "merged_at": (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat(),
        "pr_author": "",
        "pr_assignees": [],
        "_pr_url": f"https://example.test/pull/{pr_number}",
        "_closes": closes,
    }


@contextmanager
def _tracker(states: dict[int, TaskState | Exception]):
    """The tracker a queue lookup asks (a DONE in the graph is confirmed against it before a queue entry is pruned)."""

    async def get_state(issue: str) -> TaskState:
        result = states[int(issue)]
        if isinstance(result, Exception):
            raise result
        return result

    adapter = SimpleNamespace(get_state=AsyncMock(side_effect=get_state))
    with (
        patch("sova.adapters.create_adapter", return_value=adapter),
        patch("sova.adapters.close_adapter", AsyncMock()),
        patch("sova.dashboard.services.work_board_service.load_config", return_value=SimpleNamespace()),
    ):
        yield adapter


def _numbers(entries: list[dict]) -> list[int | None]:
    return [e["number"] for e in entries]


COLUMNS = ("up_next", "queued", "running", "review", "needs", "yours", "blocked", "done_today")


class TestColumns:
    async def test_default_project_is_used_for_every_board_source(self) -> None:
        project = Path("/tmp/configured-checkout")
        with (
            patch("sova.dashboard.services.agent_pool.get_default_project_dir", return_value=project),
            _sources() as sources,
        ):
            result = await board.build_board(None)

        assert result["columns"]
        sources.loaded.assert_awaited_with(project)

    async def test_rebase_run_moves_its_issue_to_running_without_an_extra_card(self) -> None:
        item = _item(1066, "pr_conflicted", pr_number=1105, pr_details={"number": 1105})
        item["running_agent"] = {"run_id": 6, "role": "developer", "elapsed_seconds": 90}
        run = {
            "id": 7,
            "issue_number": "1066",
            "role": "supervisor:rebase",
            "status": "running",
            "current_step": "rebase",
            "elapsed_seconds": 70,
            "started_at": "2026-11-18T10:00:00Z",
        }
        done = {"number": 1066, "stage": "done", "title": "Earlier merged PR"}
        with _sources(items=[item], tasks=[_task(1066)], runs={"run:7": run, "issue:1066": run}, done_today=[done]):
            result = await board.build_board(PROJECT)

        assert _numbers(result["running"]) == [1066]
        assert result["review"] == result["other_runs"] == []
        assert _numbers(result["done_today"]) == [None]  # the open issue's earlier merge, without its number
        assert (result["running"][0]["run_id"], result["running"][0]["role"]) == (7, "supervisor:rebase")
        assert result["running"][0]["elapsed_seconds"] == 70

    async def test_a_run_without_a_work_item_keeps_its_owner_and_leaves_up_next(self) -> None:
        run = {
            "id": 7,
            "issue_number": "5",
            "role": "developer",
            "status": "running",
            "current_step": "develop",
            "started_at": "2026-11-18T10:00:00Z",
        }
        with _sources(
            tasks=[_task(5, assignees=("Damian Sova",))],
            runs={"run:7": run, "issue:5": run},
            jira_display_name="Damian Sova",
        ):
            result = await board.build_board(PROJECT, owner="mine")

        assert result["up_next"] == []
        assert len(result["other_runs"]) == 1
        assert result["other_runs"][0]["assignees"] == ["Damian Sova"]

    async def test_queued_issue_without_a_work_item_moves_to_its_active_run(self) -> None:
        run = {
            "id": 7,
            "issue_number": "5",
            "role": "supervisor:rebase",
            "status": "running",
            "current_step": "rebase",
            "started_at": "2026-11-18T10:00:00Z",
        }
        with _sources(tasks=[_task(5)], queue=[5], runs={"run:7": run, "issue:5": run}):
            result = await board.build_board(PROJECT)

        assert result["queued"] == result["other_runs"] == []
        assert _numbers(result["running"]) == [5]
        assert result["running"][0]["run_id"] == 7

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
    async def test_mine_picks_assigned_issues_beyond_the_first_five_ranked(self) -> None:
        tasks = [
            _task(n, assignees=("Damian Sova",) if n in {6, 8} else (), jira_key=f"ARVAC-{n}") for n in range(1, 9)
        ]
        with _sources(tasks=tasks, jira_display_name="Damian Sova"):
            result = await board.build_board(PROJECT, owner="mine")

        assert set(_numbers(result["up_next"])) == {6, 8}
        assert result["more_ready"] == 0
        assert result["jira_display_name"] == "Damian Sova"

    async def test_mine_keeps_graph_assignee_when_queue_snapshot_is_stale(self) -> None:
        item = _item(6)
        item["assignees"] = ["Other person"]
        task = _task(6, assignees=("Straße",), jira_key="ARVAC-6")
        with _sources(items=[item], tasks=[task], github_user="Other person", jira_display_name="STRASSE"):
            result = await board.build_board(PROJECT, owner="mine")

        assert _numbers(result["up_next"]) == [6]
        assert result["up_next"][0]["assignees"] == ["Straße"]
        assert result["up_next"][0]["mine"] is True

    async def test_jira_assignee_does_not_match_the_github_login(self) -> None:
        task = _task(6, assignees=("alex",), jira_key="ARVAC-6")
        with _sources(tasks=[task], github_user="alex", jira_display_name="Sam Rivera"):
            result = await board.build_board(PROJECT, owner="mine")

        assert result["up_next"] == []

    async def test_mine_shows_only_dependency_chains_touching_my_work(self) -> None:
        mine = _item(1, "pr_ci_failed", pr_number=10)
        mine["assignees"] = ["Damian Sova"]
        mine["jira_key"] = "ARVAC-1"
        other = _item(3, "pr_ci_failed", pr_number=30)
        tasks = [_task(1), _task(2, body="## Dependencies\n- #1\n"), _task(3), _task(4, body="## Dependencies\n- #3\n")]
        with _sources(items=[mine, other], tasks=tasks, jira_display_name="Damian Sova"):
            result = await board.build_board(PROJECT, owner="mine")

        assert [[entry["number"] for entry in chain["issues"]] for chain in result["chains"]] == [[1, 2]]

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
        with (
            _sources(items=[_item(1)], tasks=tasks, queue=[2, 1, 3]) as src,
            _tracker({2: TaskState.DONE, 3: TaskState.DONE}),
        ):
            result = await board.build_board(PROJECT)

        assert _numbers(result["queued"]) == [1]
        src.remove.assert_awaited_once_with(PROJECT, [2, 3])

    async def test_an_issue_the_previous_graph_shows_closed_but_the_tracker_reopened_stays_queued(self) -> None:
        # The graph may be the previous one (answered while a rebuild runs), so its DONE is not proof of a close
        tasks = [_task(1), _task(2, state=TaskState.DONE)]
        with (
            _sources(items=[_item(1)], tasks=tasks, queue=[2, 1]) as src,
            _tracker({2: TaskState.IN_PROGRESS}),
        ):
            await board.build_board(PROJECT)

        src.remove.assert_not_awaited()

    async def test_an_issue_the_graph_shows_closed_is_kept_when_the_tracker_cannot_confirm_it(self) -> None:
        tasks = [_task(2, state=TaskState.DONE)]
        with (
            _sources(items=[], tasks=tasks, queue=[2]) as src,
            _tracker({2: RuntimeError("tracker down")}),
        ):
            await board.build_board(PROJECT)

        src.remove.assert_not_awaited()

    async def test_an_issue_the_graph_shows_open_is_not_looked_up(self) -> None:
        with _sources(items=[_item(1)], tasks=[_task(1)], queue=[1]), _tracker({}) as adapter:
            await board.build_board(PROJECT)

        adapter.get_state.assert_not_awaited()

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
        with _sources(items=[_item(1)], tasks=tasks, queue=[2, 1]) as src, _tracker({2: TaskState.DONE}):
            src.remove.side_effect = RuntimeError("db gone")
            result = await board.build_board(PROJECT)

        assert _numbers(result["queued"]) == [1]

    async def test_a_failed_save_is_named_unavailable(self) -> None:
        tasks = [_task(1), _task(2, state=TaskState.DONE)]
        with _sources(items=[_item(1)], tasks=tasks, queue=[2, 1]) as src, _tracker({2: TaskState.DONE}):
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
            patch("sova.dashboard.services.work_board_service.load_config", return_value=SimpleNamespace()),
        ):
            await board.build_board(PROJECT)

        adapter.close.assert_awaited_once()

    async def test_a_failed_lookup_for_the_whole_batch_keeps_the_queue_and_does_not_save(self) -> None:
        # 7 and 8 are not in the graph, so the tracker is asked, and it fails for both
        adapter = SimpleNamespace(get_state=AsyncMock(side_effect=RuntimeError("tracker down")))
        with (
            _sources(items=[], tasks=[], queue=[7, 8]) as src,
            patch("sova.adapters.create_adapter", return_value=adapter),
            patch("sova.dashboard.services.work_board_service.load_config", return_value=SimpleNamespace()),
        ):
            result = await board.build_board(PROJECT)

        src.remove.assert_not_awaited()
        assert _numbers(result["queued"]) == [7, 8]

    async def test_an_issue_the_graph_lacks_is_pruned_only_on_tracker_evidence(self) -> None:
        adapter = SimpleNamespace(get_state=AsyncMock(side_effect=[TaskState.DONE, RuntimeError("down")]))
        with (
            _sources(items=[], tasks=[], queue=[7, 8]) as src,
            patch("sova.adapters.create_adapter", return_value=adapter),
            patch("sova.dashboard.services.work_board_service.load_config", return_value=SimpleNamespace()),
        ):
            result = await board.build_board(PROJECT)

        src.remove.assert_awaited_once_with(PROJECT, [7])
        assert _numbers(result["queued"]) == [8]

    async def test_a_stalled_queue_lookup_does_not_hold_up_the_board_and_keeps_its_entry(self) -> None:
        import asyncio

        async def get_state(issue: str) -> TaskState:
            if issue == "8":
                await asyncio.Event().wait()  # the tracker never answers for 8
            return TaskState.DONE

        adapter = SimpleNamespace(get_state=AsyncMock(side_effect=get_state))
        with (
            _sources(items=[], tasks=[], queue=[7, 8]) as src,
            patch.object(board, "_QUEUE_LOOKUP_TIMEOUT", 0.05),
            patch("sova.adapters.create_adapter", return_value=adapter),
            patch("sova.adapters.close_adapter", AsyncMock()),
            patch("sova.dashboard.services.work_board_service.load_config", return_value=SimpleNamespace()),
        ):
            result = await asyncio.wait_for(board.build_board(PROJECT), timeout=2.0)

        src.remove.assert_awaited_once_with(PROJECT, [7])
        assert _numbers(result["queued"]) == [8]

    async def test_stalled_queue_lookups_run_under_one_deadline_not_one_after_another(self) -> None:
        import asyncio
        import time

        async def get_state(issue: str) -> TaskState:
            if issue in ("8", "9"):
                await asyncio.Event().wait()  # the tracker never answers for 8 and 9
            return TaskState.DONE

        adapter = SimpleNamespace(get_state=AsyncMock(side_effect=get_state))
        with (
            _sources(items=[], tasks=[], queue=[7, 8, 9]) as src,
            patch.object(board, "_QUEUE_LOOKUP_TIMEOUT", 0.3),
            patch("sova.adapters.create_adapter", return_value=adapter),
            patch("sova.adapters.close_adapter", AsyncMock()),
            patch("sova.dashboard.services.work_board_service.load_config", return_value=SimpleNamespace()),
        ):
            started = time.monotonic()
            result = await asyncio.wait_for(board.build_board(PROJECT), timeout=3.0)
            elapsed = time.monotonic() - started

        assert elapsed < 0.55, f"two stalled lookups took {elapsed:.2f}s: they ran one after another"
        src.remove.assert_awaited_once_with(PROJECT, [7])
        assert _numbers(result["queued"]) == [8, 9]


class TestDoneToday:
    NOW = datetime(2026, 11, 13, 15, 30, tzinfo=timezone.utc)

    async def test_closed_jira_issue_keeps_its_assignee_for_mine(self) -> None:
        task = _task(42, state=TaskState.DONE, assignees=("Damian Sova",), jira_key="ARVAC-42")
        entry = {"number": 42, "jira_key": "ARVAC-42", "pr_author": "teammate"}
        adapter = SimpleNamespace(get_task=AsyncMock(return_value=task))
        with (
            patch("sova.adapters.create_adapter", return_value=adapter),
            patch("sova.adapters.close_adapter", AsyncMock()),
            patch("sova.dashboard.services.work_board_service.load_config", return_value=SimpleNamespace()),
        ):
            await board._enrich_done_assignees(PROJECT, DependencyGraph([]), [entry])
            again = {"number": 42, "jira_key": "ARVAC-42"}
            await board._enrich_done_assignees(PROJECT, DependencyGraph([]), [again])

        assert entry["assignees"] == ["Damian Sova"]
        assert again["assignees"] == ["Damian Sova"]
        adapter.get_task.assert_awaited_once_with("ARVAC-42")
        assert board._entry_is_mine(entry, "github-user", "Damian Sova") is True

    async def test_failed_jira_assignee_lookup_is_reported_as_unavailable(self) -> None:
        entry = {"number": 42, "jira_key": "ARVAC-42"}
        adapter = SimpleNamespace(get_task=AsyncMock(side_effect=RuntimeError("offline")))
        with (
            patch("sova.adapters.create_adapter", return_value=adapter),
            patch("sova.adapters.close_adapter", AsyncMock()),
            patch("sova.dashboard.services.work_board_service.load_config", return_value=SimpleNamespace()),
        ):
            complete = await board._enrich_done_assignees(PROJECT, DependencyGraph([]), [entry])

        assert complete is False
        assert "assignees" not in entry

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
            patch("sova.dashboard.services.work_board_service.load_config", return_value=cfg),
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
        assert all(e["_closes"] is True for e in entries)

    async def test_jira_board_uses_project_key_and_ignores_another_projects_pr(self) -> None:
        from sova.config.models import ProjectConfig, TaskSourceConfig

        midnight = board._local_midnight(self.NOW)
        prs = [
            self._merged(1, midnight, body="JIRA: https://jira.example.test/browse/RHCLOUD-42", closing=[42]),
            self._merged(2, midnight, body="JIRA: https://jira.example.test/browse/ARVAC-7", closing=[99]),
        ]
        prs[1]["author"] = {"login": "dsova"}
        cfg = ProjectConfig(
            github_repo="owner/repo",
            task_source=TaskSourceConfig(
                type="jira", jira_project_key="ARVAC", jira_base_url="https://jira.example.test"
            ),
        )
        with (
            patch("sova.dashboard.services.work_board_service.load_config", return_value=cfg),
            patch.object(board, "list_merged_prs_since", AsyncMock(return_value=prs)),
        ):
            entries = await board._done_today(PROJECT, self.NOW)

        assert [(entry["number"], entry["pr_number"]) for entry in entries] == [(7, 2)]
        assert entries[0]["_closes"] is False  # a Jira issue rests on the graph alone, never on closing references
        assert entries[0]["jira_key"] == "ARVAC-7"
        assert entries[0]["url"] == "https://jira.example.test/browse/ARVAC-7"
        assert entries[0]["pr_author"] == "dsova"

    async def test_every_merged_pr_is_listed_newest_first_with_its_own_entry(self) -> None:
        midnight = board._local_midnight(self.NOW)
        prs = [
            self._merged(4, midnight + timedelta(hours=1), closing=[30]),
            self._merged(5, midnight + timedelta(hours=3), closing=[30]),
            self._merged(6, midnight + timedelta(hours=2)),  # links no issue
            self._merged(7, midnight + timedelta(hours=4)),  # links no issue
        ]
        with self._merged_prs(prs):
            entries = await board._done_today(PROJECT, self.NOW)

        assert [(e["number"], e["pr_number"]) for e in entries] == [(None, 7), (30, 5), (None, 6), (30, 4)]
        assert [e["_closes"] for e in entries] == [False, True, False, True]

    async def test_a_merged_issue_the_cached_items_still_show_in_another_column_is_only_done(self) -> None:
        stale = _item(5, "pr_ready_to_merge", pr_number=50, pr_details={"number": 50})
        merged = _merged_entry(5, 50)
        with _sources(items=[stale, _item(6)], tasks=[_task(5), _task(6)], done_today=[merged]):
            result = await board.build_board(PROJECT)

        assert _numbers(result["done_today"]) == [5]
        assert result["needs"] == []
        assert _numbers(result["up_next"]) == [6]

    async def test_a_jira_merge_the_cached_graph_and_item_still_show_open_is_done(self) -> None:
        stale = _item(5, "pr_ready_to_merge", pr_number=50, pr_details={"number": 50})
        merged = _merged_entry(5, 50)  # a Jira entry never has closing references
        merged["jira_key"] = "ARVAC-5"
        with _sources(items=[stale], tasks=[_task(5)], done_today=[merged]):
            result = await board.build_board(PROJECT)

        assert _numbers(result["done_today"]) == [5]
        assert result["needs"] == []

    async def test_an_item_whose_earlier_pr_is_gone_does_not_keep_a_closed_issue_open(self) -> None:
        stale = _item(5, "pr_merged_issue_open", pr_number=40, pr_details={"number": 40})
        merged = _merged_entry(5, 50, closes=True)
        with _sources(items=[stale], tasks=[_task(5, state=TaskState.DONE)], done_today=[merged]):
            result = await board.build_board(PROJECT)

        assert _numbers(result["done_today"]) == [5]
        assert result["needs"] == []

    async def test_an_item_whose_earlier_pr_is_gone_yields_to_a_closing_merge_within_the_graph_lag(self) -> None:
        stale = _item(5, "pr_merged_issue_open", pr_number=40, pr_details={"number": 40})
        merged = _merged_entry(5, 50, closes=True)
        with _sources(items=[stale], tasks=[_task(5)], done_today=[merged]):
            result = await board.build_board(PROJECT)

        assert _numbers(result["done_today"]) == [5]
        assert result["needs"] == []

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


class TestPartOfMerges:
    """A merged phase PR finishes its issue only when nothing else keeps the issue open."""

    async def test_an_open_issue_with_another_open_pr_keeps_its_column_and_the_merge_loses_its_number(self) -> None:
        item = _item(5, "pr_ready_to_merge", pr_number=51, pr_details={"number": 51})
        with _sources(items=[item], tasks=[_task(5)], done_today=[_merged_entry(5, 50)]):
            result = await board.build_board(PROJECT)

        assert [(e["number"], e["pr_number"], e["url"]) for e in result["done_today"]] == [
            (None, 50, "https://example.test/pull/50")
        ]
        assert _numbers(result["needs"] + result["review"] + result["yours"]) == [5]

    async def test_an_open_issue_with_no_open_pr_is_not_claimed_by_done(self) -> None:
        with _sources(items=[_item(5)], tasks=[_task(5)], done_today=[_merged_entry(5, 50)]):
            result = await board.build_board(PROJECT)

        assert _numbers(result["done_today"]) == [None]
        assert 5 in [n for column in COLUMNS[:-1] for n in _numbers(result[column])]

    async def test_two_phase_prs_on_an_open_issue_are_two_entries_without_a_number(self) -> None:
        done = [_merged_entry(5, 52), _merged_entry(5, 50)]
        with _sources(tasks=[_task(5)], done_today=done):
            result = await board.build_board(PROJECT)

        assert [(e["number"], e["pr_number"]) for e in result["done_today"]] == [(None, 52), (None, 50)]

    async def test_two_prs_on_a_closed_issue_are_one_entry_with_the_newest_merge(self) -> None:
        done = [_merged_entry(5, 52), _merged_entry(5, 50)]
        with _sources(tasks=[_task(5, state=TaskState.DONE)], done_today=done):
            result = await board.build_board(PROJECT)

        assert [(e["number"], e["pr_number"]) for e in result["done_today"]] == [(5, 52)]

    async def test_a_closing_merge_wins_over_a_graph_and_items_that_still_show_the_issue_open(self) -> None:
        stale = _item(5, "pr_ready_to_merge", pr_number=50, pr_details={"number": 50})
        with _sources(items=[stale], tasks=[_task(5)], done_today=[_merged_entry(5, 50, closes=True)]):
            result = await board.build_board(PROJECT)

        assert _numbers(result["done_today"]) == [5]
        assert all(result[column] == [] for column in COLUMNS[:-1])

    async def test_a_closing_merge_older_than_the_graph_lag_no_longer_overrides_the_graph(self) -> None:
        # reopened after a closing merge (or never closed by it): the fresh graph shows it open, so it keeps its column
        age = board._GRAPH_LAG_SECONDS + 60
        with _sources(
            tasks=[_task(5)], items=[_item(5)], done_today=[_merged_entry(5, 50, closes=True, age_seconds=age)]
        ):
            result = await board.build_board(PROJECT)

        assert _numbers(result["done_today"]) == [None]
        assert 5 in [n for column in COLUMNS[:-1] for n in _numbers(result[column])]

    async def test_an_old_closing_merge_on_an_issue_the_graph_shows_closed_is_done(self) -> None:
        age = board._GRAPH_LAG_SECONDS + 60
        with _sources(
            tasks=[_task(5, state=TaskState.DONE)], done_today=[_merged_entry(5, 50, closes=True, age_seconds=age)]
        ):
            result = await board.build_board(PROJECT)

        assert _numbers(result["done_today"]) == [5]

    async def test_a_pr_linking_an_open_and_a_closed_issue_shows_once_under_the_closed_one(self) -> None:
        done = [_merged_entry(5, 50), _merged_entry(6, 50, closes=True)]
        with _sources(tasks=[_task(5), _task(6, state=TaskState.DONE)], done_today=done):
            result = await board.build_board(PROJECT)

        assert [(e["number"], e["pr_number"]) for e in result["done_today"]] == [(6, 50)]

    async def test_a_demoted_merge_stays_when_its_numbered_entry_lost_to_a_newer_merge(self) -> None:
        # PR 50 links open 5 and closed 6, but newer PR 52 finished 6: PR 50's merge must stay visible
        done = [_merged_entry(6, 52, closes=True), _merged_entry(5, 50), _merged_entry(6, 50, closes=True)]
        with _sources(tasks=[_task(5), _task(6, state=TaskState.DONE)], done_today=done):
            result = await board.build_board(PROJECT)

        assert [(e["number"], e["pr_number"]) for e in result["done_today"]] == [(6, 52), (None, 50)]

    async def test_a_failed_open_pr_lookup_is_named_unavailable(self) -> None:
        with _sources(tasks=[_task(5, state=TaskState.DONE)], open_prs_unavailable=True):
            result = await board.build_board(PROJECT)

        assert result["unavailable"] == ["open_prs"]

    async def test_an_issue_on_qa_after_its_merge_is_done(self) -> None:
        with _sources(
            items=[_item(5, "on_qa")], tasks=[_task(5, state=TaskState.ON_QA)], done_today=[_merged_entry(5, 50)]
        ):
            result = await board.build_board(PROJECT)

        assert _numbers(result["done_today"]) == [5]

    async def test_a_finished_issue_a_run_owns_stays_out_of_done(self) -> None:
        run = {"id": 7, "issue_number": "5", "role": "developer", "status": "running", "started_at": ""}
        with _sources(
            tasks=[_task(5, state=TaskState.DONE)],
            runs={"run:7": run, "issue:5": run},
            done_today=[_merged_entry(5, 50)],
        ):
            result = await board.build_board(PROJECT)

        assert result["done_today"] == []

    async def test_an_issue_missing_from_the_graph_is_closed_so_done_wins(self) -> None:
        with _sources(done_today=[_merged_entry(5, 50)]):
            result = await board.build_board(PROJECT)

        assert [(e["number"], e["pr_number"]) for e in result["done_today"]] == [(5, 50)]

    async def test_a_demoted_jira_entry_drops_its_key_and_keeps_the_pr_link(self) -> None:
        entry = _merged_entry(7, 50)
        entry["jira_key"] = "ARVAC-7"
        entry["url"] = "https://jira.example.test/browse/ARVAC-7"
        with _sources(tasks=[_task(7)], done_today=[entry]):
            result = await board.build_board(PROJECT)

        (demoted,) = result["done_today"]
        assert demoted["number"] is None
        assert "jira_key" not in demoted
        assert demoted["url"] == "https://example.test/pull/50"
        assert "_pr_url" not in demoted
        assert "_closes" not in demoted

    async def test_an_open_pr_linking_the_issue_second_keeps_it_out_of_done(self) -> None:
        # merged PR 50 closes 5; open PR 51 links 6 first and 5 second, so no work item carries it for 5
        with _sources(
            tasks=[_task(5), _task(6)],
            done_today=[_merged_entry(5, 50, closes=True)],
            open_prs={6: {51}, 5: {51}},
        ):
            result = await board.build_board(PROJECT)

        assert _numbers(result["done_today"]) == [None]

    async def test_an_open_pr_linking_a_finished_issue_second_does_not_hide_it(self) -> None:
        with _sources(
            tasks=[_task(5, state=TaskState.DONE), _task(6)],
            done_today=[_merged_entry(5, 50)],
            open_prs={6: {51}, 5: {51}},
        ):
            result = await board.build_board(PROJECT)

        assert _numbers(result["done_today"]) == [5]

    async def test_a_merge_an_item_shows_open_past_the_graph_lag_keeps_the_issue_in_its_column(self) -> None:
        stale = _item(5, "pr_ready_to_merge", pr_number=50, pr_details={"number": 50})
        age = board._GRAPH_LAG_SECONDS + 60
        with _sources(items=[stale], tasks=[_task(5)], done_today=[_merged_entry(5, 50, age_seconds=age)]):
            result = await board.build_board(PROJECT)

        assert _numbers(result["done_today"]) == [None]

    async def test_a_demoted_merge_stays_when_a_run_owns_the_issue_its_numbered_entry_was_for(self) -> None:
        run = {"id": 7, "issue_number": "6", "role": "developer", "status": "running", "started_at": ""}
        done = [_merged_entry(5, 50), _merged_entry(6, 50, closes=True)]
        with _sources(
            tasks=[_task(5), _task(6, state=TaskState.DONE)],
            runs={"run:7": run, "issue:6": run},
            done_today=done,
        ):
            result = await board.build_board(PROJECT)

        assert [(e["number"], e["pr_number"]) for e in result["done_today"]] == [(None, 50)]

    async def test_the_merged_pr_still_listed_open_does_not_keep_the_issue_out_of_done(self) -> None:
        with _sources(tasks=[_task(5, state=TaskState.DONE)], done_today=[_merged_entry(5, 50)], open_prs={5: {50}}):
            result = await board.build_board(PROJECT)

        assert _numbers(result["done_today"]) == [5]

    async def test_an_open_item_the_graph_lacks_is_not_settled_as_done(self) -> None:
        with _sources(items=[_item(5, "researched")], done_today=[_merged_entry(5, 50)]):
            result = await board.build_board(PROJECT)

        assert _numbers(result["done_today"]) == [None]

    async def test_a_done_item_the_graph_lacks_settles_as_done(self) -> None:
        with _sources(items=[_item(5, "merged")], done_today=[_merged_entry(5, 50)]):
            result = await board.build_board(PROJECT)

        assert _numbers(result["done_today"]) == [5]

    async def test_an_item_the_graph_lacks_still_showing_the_merged_pr_settles_as_done(self) -> None:
        item = _item(5, "pr_ci_running", pr_number=50, pr_details={"number": 50})
        with _sources(items=[item], done_today=[_merged_entry(5, 50)]):
            result = await board.build_board(PROJECT)

        assert _numbers(result["done_today"]) == [5]

    async def test_a_closing_merge_wins_over_an_open_item_the_graph_lacks(self) -> None:
        with _sources(items=[_item(5, "researched")], done_today=[_merged_entry(5, 50, closes=True)]):
            result = await board.build_board(PROJECT)

        assert _numbers(result["done_today"]) == [5]

    async def test_a_pr_demoted_for_two_issues_shows_once(self) -> None:
        with _sources(tasks=[_task(5), _task(6)], done_today=[_merged_entry(5, 50), _merged_entry(6, 50)]):
            result = await board.build_board(PROJECT)

        assert [(e["number"], e["pr_number"]) for e in result["done_today"]] == [(None, 50)]

    async def test_unnumbered_entries_without_a_pr_number_are_all_kept(self) -> None:
        first, second = _merged_entry(5, 50), _merged_entry(6, 51)
        for entry in (first, second):
            entry.update({"number": None, "pr_number": None})
        with _sources(done_today=[first, second]):
            result = await board.build_board(PROJECT)

        assert _numbers(result["done_today"]) == [None, None]


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
        runs = [{"id": 9, "issue_number": "005"}, {"id": 3, "issue_number": "5"}]  # newest first
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=session)
        with (
            patch("sova.dashboard.services.work_board_service.get_session", AsyncMock(return_value=session)),
            patch("sova.dashboard.services.work_service.get_active_work", AsyncMock(return_value=runs)),
        ):
            indexed = await board._active_runs(PROJECT)

        assert indexed["issue:5"]["id"] == 9
        assert set(indexed) == {"run:9", "run:3", "issue:5"}

    async def test_a_failed_lookup_is_unavailable_never_no_runs(self) -> None:
        with patch(
            "sova.dashboard.services.work_board_service.get_session", AsyncMock(side_effect=RuntimeError("db gone"))
        ):
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

    def test_a_stale_in_review_label_needs_the_user(self) -> None:
        assert board._reason_for_needs("pr_merged_issue_open")[0] == "stale_label"

    def test_a_human_only_state_is_yours_even_with_an_autonomous_mode(self) -> None:
        item = _item(1, "human_only", mode="autonomous")
        assert board._reason_for_yours(item) == {"reason": "human_only", "reason_source": "state"}

    @pytest.mark.parametrize("raw", [None, "", "not a time"])
    def test_an_unreadable_merge_time_is_none(self, raw: str | None) -> None:
        assert board._parse_merged_at(raw) is None


class TestDoneTodaySources:
    NOW = datetime(2026, 11, 13, 15, 30, tzinfo=timezone.utc)

    async def test_a_project_without_a_github_repo_has_an_empty_day(self) -> None:
        with patch(
            "sova.dashboard.services.work_board_service.load_config",
            return_value=SimpleNamespace(github_repo="", github_user=""),
        ):
            assert await board._done_today(PROJECT, self.NOW) == []

    async def test_a_new_day_evicts_the_previous_days_cache(self) -> None:
        cfg = SimpleNamespace(github_repo="owner/repo", github_user="")
        with (
            patch("sova.dashboard.services.work_board_service.load_config", return_value=cfg),
            patch.object(board, "list_merged_prs_since", AsyncMock(return_value=[])),
        ):
            await board._done_today(PROJECT, self.NOW)
            await board._done_today(PROJECT, self.NOW + timedelta(days=1))

        assert len(board._merged_cache) == 1


class TestQueueLookupAdapter:
    async def test_no_adapter_keeps_every_unknown_entry(self) -> None:
        with (
            _sources(items=[], tasks=[], queue=[7, 8]) as src,
            patch("sova.dashboard.services.work_board_service.load_config", side_effect=RuntimeError("no config")),
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


class TestStartAction:
    async def test_each_entry_carries_the_run_its_stage_calls_for(self) -> None:
        """Start on a triaged card runs the researcher, not a developer its Gate 3 would refuse (#1418)."""
        items = [_item(1, "triaged"), _item(2, "researched"), _item(3, "triaged", run_status="failed")]
        tasks = [_task(1, state=TaskState.TRIAGED), _task(2), _task(3, state=TaskState.TRIAGED)]
        with _sources(items=items, tasks=tasks):
            result = await board.build_board(PROJECT)

        roles = {e["number"]: (e["start"] or {}).get("handler_args", {}).get("role") for e in result["up_next"]}
        assert roles == {1: "researcher", 2: "developer"}
        failed = result["needs"][0]
        assert failed["number"] == 3
        assert failed["start"]["handler_args"]["role"] == "researcher"

    async def test_an_entry_with_a_pr_has_no_start(self) -> None:
        items = [_item(4, "pr_ci_running", pr_number=40)]
        with _sources(items=items, tasks=[_task(4)]):
            result = await board.build_board(PROJECT)

        assert result["review"][0]["start"] is None


class TestSearchIssues:
    @staticmethod
    def _graph_tasks() -> list[Task]:
        tasks = [_task(n) for n in (14, 141, 1418, 1500)]
        tasks[2] = Task(
            id="1418",
            title="Close the issue with every PR",
            state=TaskState.TRIAGED,
            labels=["agent:triaged"],
            priority=Priority.from_labels([]),
            url="https://example.test/1418",
        )
        tasks.append(_task(1419, state=TaskState.DONE))
        return tasks

    async def _search(self, query: str, limit: int = board.SEARCH_LIMIT) -> dict:
        with _sources(tasks=self._graph_tasks()):
            return await board.search_issues(PROJECT, query, limit)

    async def test_a_number_matches_that_issue_first_then_the_numbers_it_starts(self) -> None:
        result = await self._search("#14")

        assert _numbers(result["results"]) == [14, 1418, 141], "the exact number, then the rest newest first"

    async def test_words_match_titles_ignoring_case_and_skip_closed_issues(self) -> None:
        result = await self._search("close EVERY")

        assert _numbers(result["results"]) == [1418]
        entry = result["results"][0]
        assert entry["state"] == "triaged"
        assert entry["start"]["handler_args"]["role"] == "researcher"
        assert entry["actions"] == [entry["start"]]
        assert "stage" not in entry, "the state alone cannot place an issue the way the board does"
        assert entry["depends_on"] == []
        assert entry["dependents"] == []

    @pytest.mark.parametrize(
        "labels",
        [("type: epic",), ("agent:human-only",)],
        ids=["epic", "human"],
    )
    async def test_an_epic_or_human_issue_has_no_start_even_in_a_state_that_would_offer_one(
        self, labels: tuple[str, ...]
    ) -> None:
        """The issues a search uniquely surfaces are the ones the board leaves out: the drawer must not offer
        Research or Develop on them, as the board offers none."""
        tasks = [_task(1500, state=TaskState.TRIAGED, labels=labels), _task(1501, state=TaskState.TRIAGED)]
        with _sources(tasks=tasks):
            result = await board.search_issues(PROJECT, "Issue")

        by_number = {entry["number"]: entry for entry in result["results"]}
        assert by_number[1500]["start"] is None
        assert by_number[1500]["actions"] == []
        assert by_number[1501]["start"] is not None, "a plain triaged issue still offers its stage's run"

    async def test_a_closed_issue_never_matches(self) -> None:
        assert (await self._search("1419"))["results"] == []

    async def test_the_limit_counts_what_it_leaves_out(self) -> None:
        result = await self._search("Issue", limit=2)

        assert _numbers(result["results"]) == [1500, 1418], "every title holding the word, newest first"
        assert result["more"] == 2

    async def test_an_empty_query_reads_nothing(self) -> None:
        with _sources(tasks=self._graph_tasks()) as sources:
            result = await board.search_issues(PROJECT, "  # ")

        assert result == {"results": [], "more": 0}
        sources.loaded.assert_not_awaited()


class TestGraphRebuild:
    """The board answers from the previous graph while a rebuild is slow, instead of waiting on the tracker (#1459)."""

    @pytest.fixture(autouse=True)
    async def _cancel_builds_left_running(self):
        from sova.dashboard.services.work_item_service import cancel_graph_builds

        yield
        await cancel_graph_builds()

    async def test_a_hanging_rebuild_does_not_hold_up_the_board(self) -> None:
        import asyncio

        from sova.dashboard.services import work_item_service

        release = asyncio.Event()
        builds = 0

        async def load(project_dir, milestone=""):
            nonlocal builds
            builds += 1
            if builds > 1:
                await release.wait()  # the tracker stops answering once the first graph is built
            return DependencyGraph([_task(1)])

        with (
            _sources(),
            patch.object(board, "load_ranking_graph", work_item_service.load_ranking_graph),
            patch.object(work_item_service, "load_dependency_graph", AsyncMock(side_effect=load)),
        ):
            cold = await board.build_board(PROJECT)
            warm = await asyncio.wait_for(board.build_board(PROJECT), timeout=1.0)
            # The rebuild is still hanging after the board answered, so the board did not await it.
            rebuild = work_item_service._graph_builds[work_item_service._graph_key(PROJECT)]
            assert not rebuild.done()
            release.set()

        assert _numbers(cold["up_next"]) == _numbers(warm["up_next"]) == [1]
        assert builds == 2
