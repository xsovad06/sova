"""The /ws/agents/status socket carries the feed, activity and handoff the sidebar used to poll (issue #1459)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.dashboard.routers import agents as agents_router
from sova.dashboard.routers.agents import _ConnectionManager
from sova.dashboard.services.feed_service import FeedEventSeverity, FeedService


@pytest.fixture(autouse=True)
def _no_real_work_items_build():
    """The producer rebuilds the work-items snapshot on its first tick: keep these tests off the tracker."""
    with patch.object(agents_router, "_refresh_work_items_snapshot", AsyncMock()) as refresh:
        yield refresh


def _socket() -> MagicMock:
    ws = MagicMock()
    ws.send_json = AsyncMock()
    return ws


def _sent_types(ws: MagicMock) -> list[str]:
    return [call.args[0]["type"] for call in ws.send_json.await_args_list]


class TestPushChanged:
    async def test_an_unchanged_message_is_sent_once(self) -> None:
        mgr = _ConnectionManager()
        ws = _socket()
        mgr._groups[None] = [ws]
        build = AsyncMock(return_value={"type": "activity", "data": {"agents": []}})
        last_sent: dict[str, str] = {}

        await mgr._push_changed(None, "activity", build, last_sent)
        await mgr._push_changed(None, "activity", build, last_sent)

        assert _sent_types(ws) == ["activity"]

    async def test_a_changed_message_is_sent_again(self) -> None:
        mgr = _ConnectionManager()
        ws = _socket()
        mgr._groups[None] = [ws]
        build = AsyncMock(side_effect=[{"type": "activity", "data": 1}, {"type": "activity", "data": 2}])
        last_sent: dict[str, str] = {}

        await mgr._push_changed(None, "activity", build, last_sent)
        await mgr._push_changed(None, "activity", build, last_sent)

        assert [c.args[0]["data"] for c in ws.send_json.await_args_list] == [1, 2]

    async def test_a_failed_build_is_reported_as_unavailable_not_as_empty(self) -> None:
        mgr = _ConnectionManager()
        ws = _socket()
        mgr._groups[None] = [ws]
        build = AsyncMock(side_effect=RuntimeError("db locked"))
        last_sent: dict[str, str] = {}

        await mgr._push_changed(None, "handoff", build, last_sent)
        await mgr._push_changed(None, "handoff", build, last_sent)

        assert _sent_types(ws) == ["handoff_unavailable"]

    async def test_a_build_that_recovers_to_the_same_payload_is_sent_again_after_unavailable(self) -> None:
        mgr = _ConnectionManager()
        ws = _socket()
        mgr._groups[None] = [ws]
        payload = {"type": "handoff", "data": {"handoffs": []}}
        build = AsyncMock(side_effect=[payload, RuntimeError("github down"), payload])
        last_sent: dict[str, str] = {}

        for _ in range(3):
            await mgr._push_changed(None, "handoff", build, last_sent)

        assert _sent_types(ws) == ["handoff", "handoff_unavailable", "handoff"]


class TestFeedForwarding:
    async def test_a_feed_event_reaches_the_socket_and_the_subscription_is_released(self) -> None:
        feed = FeedService()
        feed._schedule_persist = MagicMock()  # type: ignore[method-assign]  (history persistence is not under test)
        mgr = _ConnectionManager()
        ws = _socket()
        mgr._groups[None] = [ws]
        with patch.object(agents_router, "get_feed_service", return_value=feed):
            task = asyncio.create_task(mgr._feed_loop(None))
            await asyncio.sleep(0)
            feed.emit("PR merged", severity=FeedEventSeverity.info)
            for _ in range(20):
                if ws.send_json.await_count:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        message = ws.send_json.await_args_list[0].args[0]
        assert message["type"] == "feed"
        assert message["event"]["title"] == "PR merged"
        assert feed._subscribers == {}

    async def test_feed_events_are_process_wide_and_reach_the_sockets_of_every_project(self, tmp_path: Path) -> None:
        # FeedEvent carries no project, so nothing here can filter by one: the same stream feeds /feed/stream.
        feed = FeedService()
        feed._schedule_persist = MagicMock()  # type: ignore[method-assign]  (history persistence is not under test)
        mgr = _ConnectionManager()
        other = tmp_path / "other"
        ws_a, ws_b = _socket(), _socket()
        mgr._groups[None] = [ws_a]
        mgr._groups[other] = [ws_b]
        with patch.object(agents_router, "get_feed_service", return_value=feed):
            tasks = [asyncio.create_task(mgr._feed_loop(key)) for key in (None, other)]
            await asyncio.sleep(0)
            feed.emit("PR merged", severity=FeedEventSeverity.info)
            for _ in range(20):
                if ws_a.send_json.await_count and ws_b.send_json.await_count:
                    break
                await asyncio.sleep(0.01)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        assert _sent_types(ws_a) == ["feed"]
        assert _sent_types(ws_b) == ["feed"]


class TestProducerLoop:
    async def test_activity_and_handoff_are_pushed_on_the_first_tick_and_not_repeated_while_unchanged(self) -> None:
        mgr = _ConnectionManager()
        ws = _socket()
        mgr._groups[None] = [ws]
        ticks = 0
        real_sleep = asyncio.sleep

        async def fake_sleep(_seconds: float) -> None:
            nonlocal ticks
            ticks += 1
            for _ in range(5):  # let the handoff push, which runs beside the tick, finish its broadcast
                await real_sleep(0)
            if ticks >= 2:
                mgr._groups.clear()

        with (
            patch("sova.dashboard.services.agent_status.get_all_agent_statuses", AsyncMock(return_value=[])),
            patch.object(agents_router, "_build_activity_message", AsyncMock(return_value={"type": "activity"})),
            patch.object(agents_router, "_build_handoff_message", AsyncMock(return_value={"type": "handoff"})),
            patch.object(agents_router.asyncio, "sleep", fake_sleep),
        ):
            await mgr._produce_loop(None)

        assert sorted(_sent_types(ws)) == ["activity", "handoff", "status_update", "status_update"]

    async def test_a_slow_handoff_build_does_not_hold_back_the_status_tick(self) -> None:
        mgr = _ConnectionManager()
        ws = _socket()
        mgr._groups[None] = [ws]
        handoff_started = asyncio.Event()
        ticks = 0
        real_sleep = asyncio.sleep

        async def hung_handoff() -> dict:
            handoff_started.set()
            await asyncio.Event().wait()  # a GitHub call that never answers
            return {"type": "handoff"}

        async def fake_sleep(_seconds: float) -> None:
            nonlocal ticks
            ticks += 1
            await real_sleep(0)
            if ticks >= 4:
                mgr._groups.clear()

        build = AsyncMock(side_effect=hung_handoff)
        with (
            patch("sova.dashboard.services.agent_status.get_all_agent_statuses", AsyncMock(return_value=[])),
            patch.object(agents_router, "_build_activity_message", AsyncMock(return_value={"type": "activity"})),
            patch.object(agents_router, "_build_handoff_message", build),
            patch.object(agents_router.asyncio, "sleep", fake_sleep),
        ):
            await asyncio.wait_for(mgr._produce_loop(None), timeout=5)

        assert handoff_started.is_set()
        assert _sent_types(ws).count("status_update") == 4
        # Tick 3 is a handoff tick, but the first build is still running, so it is not started twice.
        assert build.await_count == 1

    async def test_the_producer_runs_in_the_project_context_of_its_socket_slug(self, tmp_path: Path) -> None:
        from sova.config.context import get_project_slug

        mgr = _ConnectionManager()
        mgr._groups[tmp_path] = [_socket()]
        mgr._slugs[tmp_path] = "demo"
        seen: list[str | None] = []

        async def build() -> dict:
            seen.append(get_project_slug())
            mgr._groups.clear()
            return {"type": "activity"}

        with (
            patch("sova.dashboard.services.agent_status.get_all_agent_statuses", AsyncMock(return_value=[])),
            patch.object(agents_router, "_build_activity_message", build),
            patch.object(agents_router, "_build_handoff_message", AsyncMock(return_value={"type": "handoff"})),
            patch.object(agents_router.asyncio, "sleep", AsyncMock()),
        ):
            await mgr._produce_loop(tmp_path)

        assert seen == ["demo"]

    async def test_an_unprefixed_socket_still_gets_the_group_project_context(self, tmp_path: Path) -> None:
        from sova.config.context import get_project_dir

        mgr = _ConnectionManager()
        mgr._groups[tmp_path] = [_socket()]
        seen: list[Path | None] = []

        async def build() -> dict:
            seen.append(get_project_dir())
            mgr._groups.clear()
            return {"type": "activity"}

        with (
            patch("sova.dashboard.services.agent_status.get_all_agent_statuses", AsyncMock(return_value=[])),
            patch.object(agents_router, "_build_activity_message", build),
            patch.object(agents_router, "_build_handoff_message", AsyncMock(return_value={"type": "handoff"})),
            patch.object(agents_router.asyncio, "sleep", AsyncMock()),
        ):
            await mgr._produce_loop(tmp_path)

        assert seen == [tmp_path]

    async def test_a_slow_activity_build_does_not_hold_back_the_status_tick(self) -> None:
        mgr = _ConnectionManager()
        ws = _socket()
        mgr._groups[None] = [ws]
        ticks = 0
        real_sleep = asyncio.sleep

        async def hung_activity() -> dict:
            await asyncio.Event().wait()
            return {"type": "activity"}

        async def fake_sleep(_seconds: float) -> None:
            nonlocal ticks
            ticks += 1
            await real_sleep(0)
            if ticks >= 3:
                mgr._groups.clear()

        build = AsyncMock(side_effect=hung_activity)
        with (
            patch("sova.dashboard.services.agent_status.get_all_agent_statuses", AsyncMock(return_value=[])),
            patch.object(agents_router, "_build_activity_message", build),
            patch.object(agents_router, "_build_handoff_message", AsyncMock(return_value={"type": "handoff"})),
            patch.object(agents_router.asyncio, "sleep", fake_sleep),
        ):
            await asyncio.wait_for(mgr._produce_loop(None), timeout=5)

        assert _sent_types(ws).count("status_update") == 3
        assert build.await_count == 1

    async def test_a_socket_connecting_while_the_old_producer_cleans_up_gets_a_new_producer(self) -> None:
        mgr = _ConnectionManager()
        first, second = _socket(), _socket()
        release = asyncio.Event()
        started = asyncio.Event()

        async def blocked_handoff() -> dict:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                # Cleanup that takes a while: the old producer awaits it in its finally.
                await asyncio.sleep(0.05)
                raise
            return {"type": "handoff"}

        with (
            patch("sova.dashboard.services.agent_status.get_all_agent_statuses", AsyncMock(return_value=[])),
            patch.object(agents_router, "_build_activity_message", AsyncMock(return_value={"type": "activity"})),
            patch.object(agents_router, "_build_handoff_message", blocked_handoff),
            patch.object(agents_router, "_STATUS_BROADCAST_INTERVAL", 0.01),
        ):
            mgr.connect(first, None)
            old_producer = mgr._producer_tasks[None]
            await asyncio.wait_for(started.wait(), timeout=2)
            mgr.disconnect(first)
            await asyncio.sleep(0.03)  # the loop saw no group and is now cancelling the blocked build
            assert not old_producer.done()
            mgr.connect(second, None)
            assert mgr._producer_tasks[None] is not old_producer
            for _ in range(50):
                if "status_update" in _sent_types(second):
                    break
                await asyncio.sleep(0.01)
            await mgr.cancel_all()
            release.set()

        assert "status_update" in _sent_types(second)


class TestConnectionLifecycle:
    @pytest.mark.parametrize("slug", [None, "demo"])
    async def test_connect_starts_both_tasks_and_cancel_all_stops_them(self, slug: str | None) -> None:
        mgr = _ConnectionManager()
        ws = _socket()
        with (
            patch("sova.dashboard.services.agent_status.get_all_agent_statuses", AsyncMock(return_value=[])),
            patch.object(agents_router, "_build_activity_message", AsyncMock(return_value={"type": "activity"})),
            patch.object(agents_router, "_build_handoff_message", AsyncMock(return_value={"type": "handoff"})),
        ):
            mgr.connect(ws, None, slug)
            assert mgr._slugs[None] == slug
            assert len(mgr._producer_tasks) == 1 and len(mgr._feed_tasks) == 1
            await mgr.cancel_all()

        assert not mgr._producer_tasks and not mgr._feed_tasks and not mgr._slugs


class TestPushChangedEncoding:
    async def test_a_payload_with_a_datetime_and_a_decimal_is_sent_as_valid_json_and_keeps_the_socket(self) -> None:
        from datetime import UTC, datetime
        from decimal import Decimal

        mgr = _ConnectionManager()
        ws = _socket()
        mgr._groups[None] = [ws]
        build = AsyncMock(
            return_value={
                "type": "activity",
                "data": {"started": datetime(2026, 11, 14, tzinfo=UTC), "cost": Decimal("1.50"), "dir": Path("/x")},
            }
        )

        await mgr._push_changed(None, "activity", build, {})

        sent = ws.send_json.await_args_list[0].args[0]
        # What reached the socket is JSON-native, and the socket was not dropped from its group.
        assert sent["data"] == {"started": "2026-11-14T00:00:00+00:00", "cost": 1.5, "dir": "/x"}
        json.dumps(sent)
        assert mgr._groups[None] == [ws]

    async def test_the_digest_and_the_bytes_sent_cannot_disagree_on_a_changed_decimal(self) -> None:
        from decimal import Decimal

        mgr = _ConnectionManager()
        ws = _socket()
        mgr._groups[None] = [ws]
        build = AsyncMock(
            side_effect=[
                {"type": "activity", "data": {"cost": Decimal("1.50")}},
                {"type": "activity", "data": {"cost": Decimal("1.75")}},
            ]
        )
        last_sent: dict[str, str] = {}

        await mgr._push_changed(None, "activity", build, last_sent)
        await mgr._push_changed(None, "activity", build, last_sent)

        assert [c.args[0]["data"]["cost"] for c in ws.send_json.await_args_list] == [1.5, 1.75]


class TestFeedLoopGuard:
    async def test_one_event_that_fails_to_forward_does_not_end_the_forwarder(self) -> None:
        feed = FeedService()
        feed._schedule_persist = MagicMock()  # type: ignore[method-assign]  (history persistence is not under test)
        mgr = _ConnectionManager()
        ws = _socket()
        mgr._groups[None] = [ws]
        with patch.object(agents_router, "get_feed_service", return_value=feed):
            task = asyncio.create_task(mgr._feed_loop(None))
            await asyncio.sleep(0)
            real_broadcast = mgr._broadcast
            calls = 0

            async def flaky_broadcast(data: dict, project_dir: Path | None) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("to_dict blew up")
                await real_broadcast(data, project_dir)

            mgr._broadcast = flaky_broadcast  # type: ignore[method-assign]
            feed.emit("first", severity=FeedEventSeverity.info)
            feed.emit("second", severity=FeedEventSeverity.info)
            for _ in range(40):
                if ws.send_json.await_count:
                    break
                await asyncio.sleep(0.01)
            alive = not task.done()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert alive
        assert [c.args[0]["event"]["title"] for c in ws.send_json.await_args_list] == ["second"]


class TestDrainingProducers:
    async def test_cancel_all_cancels_a_producer_that_is_in_its_cleanup_await(self) -> None:
        mgr = _ConnectionManager()
        mgr._groups[None] = [_socket()]
        in_cleanup = asyncio.Event()
        release = asyncio.Event()
        real_sleep = asyncio.sleep

        async def slow_to_cancel_handoff() -> dict:
            in_cleanup.set()
            # A build that is slow to honour its cancel (cleanup of its own), so the producer's cleanup await hangs.
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            return {"type": "handoff"}

        async def fake_sleep(_seconds: float) -> None:
            # The last socket leaves: the loop ends and the producer enters its cleanup.
            mgr._groups.clear()
            await real_sleep(0)

        with (
            patch("sova.dashboard.services.agent_status.get_all_agent_statuses", AsyncMock(return_value=[])),
            patch.object(agents_router, "_build_activity_message", AsyncMock(return_value={"type": "activity"})),
            patch.object(agents_router, "_build_handoff_message", slow_to_cancel_handoff),
            patch.object(agents_router.asyncio, "sleep", fake_sleep),
        ):
            producer = asyncio.create_task(mgr._produce_loop(None))
            mgr._producer_tasks[None] = producer
            await asyncio.wait_for(in_cleanup.wait(), timeout=5)
            for _ in range(50):
                if producer in mgr._draining:
                    break
                await real_sleep(0.01)
            still_cleaning_up = not producer.done()
            out_of_the_slots = producer not in mgr._producer_tasks.values()
            tracked = producer in mgr._draining
            shutdown = asyncio.create_task(mgr.cancel_all())
            await real_sleep(0.01)
            release.set()  # the slow build finishes, so the producer's cleanup can end
            await asyncio.wait_for(shutdown, timeout=5)

        assert still_cleaning_up and out_of_the_slots and tracked
        # cancel_all() reached the producer through the draining set: it ended cancelled, not by finishing normally.
        assert producer.cancelled()
        assert not mgr._draining


class TestWorkItemsSnapshotWiring:
    async def test_the_producer_refreshes_the_work_items_snapshot_on_its_first_tick_and_every_30th(
        self, _no_real_work_items_build: AsyncMock
    ) -> None:
        mgr = _ConnectionManager()
        mgr._groups[None] = [_socket()]
        ticks = 0
        real_sleep = asyncio.sleep

        async def fake_sleep(_seconds: float) -> None:
            nonlocal ticks
            ticks += 1
            await real_sleep(0)
            if ticks >= 61:
                mgr._groups.clear()

        with (
            patch("sova.dashboard.services.agent_status.get_all_agent_statuses", AsyncMock(return_value=[])),
            patch.object(agents_router, "_build_activity_message", AsyncMock(return_value={"type": "activity"})),
            patch.object(agents_router, "_build_handoff_message", AsyncMock(return_value={"type": "handoff"})),
            patch.object(agents_router.asyncio, "sleep", fake_sleep),
        ):
            await asyncio.wait_for(mgr._produce_loop(None), timeout=5)

        # Ticks 0, 30 and 60: once on connect and then about a minute apart at the 2 s cadence.
        assert _no_real_work_items_build.await_count == 3

    async def test_a_changed_snapshot_reaches_the_sockets_as_work_items_changed_and_an_unchanged_one_sends_nothing(
        self,
    ) -> None:
        from sova.dashboard.services import snapshot_service

        mgr = _ConnectionManager()
        ws = _socket()
        mgr._groups[None] = [ws]
        payloads = iter([{"items": [1]}, {"items": [1]}, {"items": [1, 2]}])

        async def build() -> dict:
            return next(payloads)

        with patch.object(agents_router, "_ws_manager", mgr):
            for _ in range(3):
                await snapshot_service.refresh_snapshot("work_items", None, build)

        assert _sent_types(ws) == ["work_items_changed"]
        assert ws.send_json.await_args_list[0].args[0] == {"type": "work_items_changed"}


class TestGraphChangeReachesTheStatusSocket:
    """A rebuilt dependency graph tells open boards to reload, in single-project mode too (#1459)."""

    @staticmethod
    async def _rebuild_with_a_changed_graph() -> None:
        from sova.dashboard.services import work_item_service
        from sova.supervisor.dependency_graph import DependencyGraph

        def graph(title: str) -> DependencyGraph:
            from sova.adapters.base import Task

            return DependencyGraph([Task(id="1", title=title)])

        work_item_service.reset_graph_load_state()
        loads = [graph("old"), graph("new")]
        with patch.object(work_item_service, "load_dependency_graph", AsyncMock(side_effect=loads)):
            await work_item_service.load_ranking_graph(None)  # a cold build, awaited
            await work_item_service.load_ranking_graph(None)  # answers from it, rebuild runs behind
            await asyncio.gather(*work_item_service._graph_builds.values(), return_exceptions=True)
            await asyncio.gather(*work_item_service._graph_notify_tasks)
        work_item_service.reset_graph_load_state()

    def test_the_status_router_registers_itself_as_the_graph_change_listener(self) -> None:
        from sova.dashboard.services import work_item_service

        assert work_item_service._graph_change_listener is agents_router._on_graph_changed

    async def test_a_socket_registered_under_the_default_project_gets_work_items_changed(self, tmp_path: Path) -> None:
        mgr = _ConnectionManager()
        ws = _socket()
        mgr._groups[tmp_path.resolve()] = [ws]  # where a socket lands once a default project is set
        with (
            patch.object(agents_router, "_ws_manager", mgr),
            patch("sova.dashboard.services.agent_pool.get_default_project_dir", return_value=tmp_path),
        ):
            await self._rebuild_with_a_changed_graph()

        assert _sent_types(ws) == ["work_items_changed"]

    async def test_a_socket_registered_under_none_gets_work_items_changed(self) -> None:
        mgr = _ConnectionManager()
        ws = _socket()
        mgr._groups[None] = [ws]  # no default project: the socket registers under None
        with (
            patch.object(agents_router, "_ws_manager", mgr),
            patch("sova.dashboard.services.agent_pool.get_default_project_dir", return_value=None),
        ):
            await self._rebuild_with_a_changed_graph()

        assert _sent_types(ws) == ["work_items_changed"]

    async def test_a_socket_of_another_project_hears_nothing(self, tmp_path: Path) -> None:
        mgr = _ConnectionManager()
        other = _socket()
        mgr._groups[(tmp_path / "other").resolve()] = [other]
        with (
            patch.object(agents_router, "_ws_manager", mgr),
            patch("sova.dashboard.services.agent_pool.get_default_project_dir", return_value=tmp_path),
        ):
            await self._rebuild_with_a_changed_graph()

        assert _sent_types(other) == []


class TestStatusUpdateCarriesTheRunRole:
    def test_a_status_update_run_names_its_role_and_pipeline_variant(self) -> None:
        # The step-less run badge is built from these two fields on every push, so a push without them would
        # overwrite the correct label with "Starting...".
        from sova.dashboard.services.agent_status import AgentStatus, format_status_update

        status = AgentStatus(
            run_id=7,
            status="running",
            role="command:review-pr",
            pipeline_variant="review",
            current_step="agent",
            step_index=0,
            total_steps=0,
            step_progress_pct=0.0,
            time_in_step_ms=0,
            time_in_step_known=False,
            is_stuck=False,
            estimated_remaining_ms=None,
            issue="12",
            pid=1,
            elapsed_seconds=4,
            cost_usd="0.00",
            pr_number=3,
        )

        (run,) = format_status_update([status])["runs"]

        assert run["role"] == "command:review-pr"
        assert run["pipeline_variant"] == "review"
