"""The /ws/agents/status socket carries the feed, activity and handoff the sidebar used to poll (issue #1459)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.dashboard.routers import agents as agents_router
from sova.dashboard.routers.agents import _ConnectionManager
from sova.dashboard.services.feed_service import FeedEventSeverity, FeedService


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
