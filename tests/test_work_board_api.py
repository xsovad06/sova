"""Work board routes: GET /api/work/board, the hide and unhide routes, and PUT /api/issues/{n}/priority."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from sova.adapters.base import PriorityUnavailableError, Task, TaskState
from sova.adapters.priority import Priority
from sova.dashboard.services import work_board_service, work_item_service
from sova.dashboard.services.work_item_service import GraphConfigError
from sova.supervisor.dependency_graph import invalidate_graph_cache
from tests.conftest import csrf_request_headers

NO_CSRF = {"origin": "", "x-csrf-token": ""}


@pytest.fixture
async def client(dashboard_app) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=dashboard_app), base_url="http://test") as ac:
        ac.headers.update(csrf_request_headers(ac))
        yield ac


@pytest.fixture(autouse=True)
def _clean_caches():
    work_item_service.reset_graph_load_state()
    work_board_service.reset_board_cache()
    invalidate_graph_cache()
    yield
    work_item_service.reset_graph_load_state()
    work_board_service.reset_board_cache()
    invalidate_graph_cache()


class TestBoardRoute:
    async def test_board_is_not_parsed_as_a_run_id(self, client: AsyncClient) -> None:
        payload = {"up_next": [], "stages": []}
        with patch.object(work_board_service, "build_board", AsyncMock(return_value=payload)) as build:
            resp = await client.get("/api/work/board", params={"milestone": "M1"})

        assert resp.status_code == 200
        assert resp.json() == payload
        assert build.await_args.args[1] == "M1"

    async def test_a_graph_config_failure_is_a_503_not_an_empty_board(self, client: AsyncClient) -> None:
        with patch.object(work_board_service, "build_board", AsyncMock(side_effect=GraphConfigError("no repo"))):
            resp = await client.get("/api/work/board")

        assert resp.status_code == 503

    async def test_any_other_failure_is_a_500(self, client: AsyncClient) -> None:
        with patch.object(work_board_service, "build_board", AsyncMock(side_effect=RuntimeError("boom"))):
            resp = await client.get("/api/work/board")

        assert resp.status_code == 500


class TestHiddenRoutes:
    async def test_hide_and_unhide_return_the_new_list_and_are_idempotent(self, client: AsyncClient) -> None:
        saved: list[int] = []

        async def fake_set(_project, number: int, *, hidden: bool) -> list[int]:
            if hidden and number not in saved:
                saved.append(number)
            if not hidden and number in saved:
                saved.remove(number)
            return list(saved)

        with patch("sova.dashboard.routers.work.set_issue_hidden", fake_set):
            assert (await client.post("/api/work/hidden/5")).json() == {"hidden": [5]}
            assert (await client.post("/api/work/hidden/5")).json() == {"hidden": [5]}
            assert (await client.delete("/api/work/hidden/9")).json() == {"hidden": [5]}
            assert (await client.delete("/api/work/hidden/5")).json() == {"hidden": []}

    async def test_a_save_failure_is_a_503(self, client: AsyncClient) -> None:
        with patch("sova.dashboard.routers.work.set_issue_hidden", AsyncMock(side_effect=RuntimeError("db"))):
            resp = await client.post("/api/work/hidden/5")

        assert resp.status_code == 503

    @pytest.mark.parametrize("number", ["0", "-3", "abc"])
    async def test_a_number_that_is_not_an_issue_is_rejected(self, client: AsyncClient, number: str) -> None:
        assert (await client.post(f"/api/work/hidden/{number}")).status_code == 422

    @pytest.mark.usefixtures("reset_db_rows")
    async def test_hiding_persists_through_the_real_setting(self, client: AsyncClient, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("sova.dashboard.routers.work.get_project_dir", lambda: tmp_path)
        assert (await client.post("/api/work/hidden/12")).json() == {"hidden": [12]}
        # a fresh read of the stored value, as after a restart
        from sova.config.db_loader import get_setting
        from sova.db.session import get_session

        async with await get_session(project_dir=tmp_path) as session:
            assert await get_setting(session, "dashboard.hidden_issues") == [12]
        assert (await client.delete("/api/work/hidden/12")).json() == {"hidden": []}


class TestMutatingRoutesRequireCsrf:
    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("POST", "/api/work/hidden/5", None),
            ("DELETE", "/api/work/hidden/5", None),
            ("PUT", "/api/issues/5/priority", {"priority": "high"}),
        ],
    )
    async def test_a_request_without_the_csrf_pair_is_rejected(
        self, client: AsyncClient, method: str, path: str, body: dict | None
    ) -> None:
        client.cookies.clear()
        resp = await client.request(method, path, json=body, headers=NO_CSRF)

        assert resp.status_code == 403


class _FakeTracker:
    """An adapter whose priority labels live in a dict, standing in for the tracker."""

    repo = "owner/repo"

    def __init__(self, priorities: dict[int, Priority]) -> None:
        self.priorities = priorities
        self.fail: Exception | None = None
        self.closed = 0
        self.close_fail: Exception | None = None

    async def close(self) -> None:
        self.closed += 1
        if self.close_fail is not None:
            raise self.close_fail

    async def set_priority(self, task_id: str, priority: Priority) -> None:
        if self.fail is not None:
            raise self.fail
        self.priorities[int(task_id)] = priority

    async def list_tasks(self, _filters: object = None) -> list[Task]:
        return [
            Task(
                id=str(n),
                title=f"Issue {n}",
                state=TaskState.RESEARCHED,
                labels=["agent:ready", p.github_label],
                priority=p,
            )
            for n, p in sorted(self.priorities.items())
        ]


class TestPriorityRoute:
    @pytest.fixture
    def tracker(self) -> _FakeTracker:
        tracker = _FakeTracker({1: Priority.LOW, 2: Priority.MEDIUM})
        with (
            patch("sova.adapters.create_adapter", return_value=tracker),
            patch("sova.config.loader.load_config", return_value=SimpleNamespace()),
        ):
            yield tracker

    async def test_success_sets_the_priority(self, client: AsyncClient, tracker: _FakeTracker) -> None:
        resp = await client.put("/api/issues/1/priority", json={"priority": "blocker"})

        assert resp.status_code == 200
        assert resp.json() == {"issue": 1, "priority": "blocker"}
        assert tracker.priorities[1] is Priority.BLOCKER

    async def test_a_level_the_tracker_lacks_is_a_400_with_the_message(
        self, client: AsyncClient, tracker: _FakeTracker
    ) -> None:
        tracker.fail = PriorityUnavailableError("Jira has no priority for 'blocker' (available: High, Low)")

        resp = await client.put("/api/issues/1/priority", json={"priority": "blocker"})

        assert resp.status_code == 400
        assert "Jira has no priority for 'blocker'" in resp.json()["detail"]
        assert tracker.priorities[1] is Priority.LOW

    async def test_a_tracker_failure_is_a_502(self, client: AsyncClient, tracker: _FakeTracker) -> None:
        tracker.fail = RuntimeError("gh exploded")

        assert (await client.put("/api/issues/1/priority", json={"priority": "high"})).status_code == 502

    @pytest.mark.parametrize(
        ("fail", "status"),
        [
            pytest.param(None, 200, id="success"),
            pytest.param(PriorityUnavailableError("no such level"), 400, id="unavailable"),
            pytest.param(RuntimeError("gh exploded"), 502, id="tracker failure"),
        ],
    )
    async def test_the_request_owned_adapter_is_closed_on_every_exit_path(
        self, client: AsyncClient, tracker: _FakeTracker, fail: Exception | None, status: int
    ) -> None:
        tracker.fail = fail

        resp = await client.put("/api/issues/1/priority", json={"priority": "high"})

        assert resp.status_code == status
        assert tracker.closed == 1

    async def test_a_close_failure_does_not_change_the_response(
        self, client: AsyncClient, tracker: _FakeTracker
    ) -> None:
        tracker.close_fail = RuntimeError("socket gone")

        resp = await client.put("/api/issues/1/priority", json={"priority": "high"})

        assert resp.status_code == 200
        assert tracker.priorities[1] is Priority.HIGH

    async def test_an_adapter_without_close_is_fine(self, client: AsyncClient) -> None:
        tracker = SimpleNamespace(repo="owner/repo", set_priority=AsyncMock())
        with (
            patch("sova.adapters.create_adapter", return_value=tracker),
            patch("sova.config.loader.load_config", return_value=SimpleNamespace()),
        ):
            resp = await client.put("/api/issues/1/priority", json={"priority": "high"})

        assert resp.status_code == 200

    async def test_an_unknown_level_is_a_422(self, client: AsyncClient, tracker: _FakeTracker) -> None:
        assert (await client.put("/api/issues/1/priority", json={"priority": "urgent"})).status_code == 422

    async def test_a_config_failure_is_a_503(self, client: AsyncClient) -> None:
        with patch("sova.config.loader.load_config", side_effect=RuntimeError("no config")):
            resp = await client.put("/api/issues/1/priority", json={"priority": "high"})

        assert resp.status_code == 503

    async def test_the_next_board_ranks_with_the_new_priority_despite_the_graph_caches(
        self, client: AsyncClient, tracker: _FakeTracker
    ) -> None:
        """The first board warms the graph cache and the shared build; the PUT must drop both."""
        items = {
            "items": [
                {
                    "issue_number": str(n),
                    "state": "researched",
                    "title": f"Issue {n}",
                    "suitability": {"mode": "autonomous", "reasons": [], "blocked_by": []},
                    "issue_priority": "x",
                    "last_run": None,
                }
                for n in (1, 2)
            ]
        }
        with (
            patch.object(work_board_service, "get_work_items", AsyncMock(return_value=items)),
            patch.object(work_board_service, "next_task_exclusions", AsyncMock(return_value=(set(), set()))),
            patch.object(work_board_service, "load_task_queue", AsyncMock(return_value=[])),
            patch.object(work_board_service, "load_hidden_issues", AsyncMock(return_value=[])),
            patch.object(work_board_service, "_active_runs", AsyncMock(return_value={})),
            patch.object(work_board_service, "_done_today", AsyncMock(return_value=[])),
        ):
            before = (await client.get("/api/work/board")).json()
            assert [e["number"] for e in before["up_next"]] == [2, 1]  # medium outranks low

            assert (await client.put("/api/issues/1/priority", json={"priority": "critical"})).status_code == 200
            after = (await client.get("/api/work/board")).json()

        assert [e["number"] for e in after["up_next"]] == [1, 2]
        assert after["up_next"][0]["priority"] == "critical"
