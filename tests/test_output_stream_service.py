"""Tests for sova.dashboard.services.output_stream_service and SSE output endpoint."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.responses import StreamingResponse

from sova.dashboard.services.output_stream_service import (
    OutputStreamService,
    get_output_stream_service,
)
from sova.db.session import close_db, init_db

# Unit tests for OutputStreamService


class TestOutputStreamService:
    def test_subscribe_returns_id_and_queue(self) -> None:
        svc = OutputStreamService()
        sub_id, queue = svc.subscribe(run_id=1)
        assert isinstance(sub_id, int)
        assert isinstance(queue, asyncio.Queue)

    def test_publish_delivers_to_subscriber(self) -> None:
        svc = OutputStreamService()
        sub_id, queue = svc.subscribe(run_id=1)
        svc.publish(1, "hello")
        assert not queue.empty()
        assert queue.get_nowait() == "hello"
        svc.unsubscribe(1, sub_id)

    def test_publish_to_wrong_run_does_not_deliver(self) -> None:
        svc = OutputStreamService()
        _sub_id, queue = svc.subscribe(run_id=1)
        svc.publish(2, "wrong run")
        assert queue.empty()

    def test_unsubscribe_stops_delivery(self) -> None:
        svc = OutputStreamService()
        sub_id, queue = svc.subscribe(run_id=1)
        svc.unsubscribe(1, sub_id)
        svc.publish(1, "missed")
        assert queue.empty()

    def test_multiple_subscribers_same_run(self) -> None:
        svc = OutputStreamService()
        id1, q1 = svc.subscribe(run_id=1)
        id2, q2 = svc.subscribe(run_id=1)
        svc.publish(1, "broadcast")
        assert q1.get_nowait() == "broadcast"
        assert q2.get_nowait() == "broadcast"
        svc.unsubscribe(1, id1)
        svc.unsubscribe(1, id2)

    def test_subscribers_different_runs(self) -> None:
        svc = OutputStreamService()
        _id1, q1 = svc.subscribe(run_id=1)
        _id2, q2 = svc.subscribe(run_id=2)
        svc.publish(1, "run1 line")
        svc.publish(2, "run2 line")
        assert q1.get_nowait() == "run1 line"
        assert q2.get_nowait() == "run2 line"

    def test_slow_consumer_drops_lines(self) -> None:
        svc = OutputStreamService()
        _sub_id, queue = svc.subscribe(run_id=1)
        for i in range(210):
            svc.publish(1, f"line {i}")
        assert queue.qsize() == 200  # capped at _QUEUE_MAX

    def test_overflow_is_reported_once_to_the_subscriber_that_lost_a_line(self) -> None:
        svc = OutputStreamService()
        slow_id, _slow = svc.subscribe(run_id=1)
        fast_id, fast = svc.subscribe(run_id=1)
        for i in range(200):
            svc.publish(1, f"line {i}")
        fast.get_nowait()  # the fast reader keeps up; only the slow one is full
        assert not svc.take_overflow(1, slow_id)
        svc.publish(1, "one too many")
        assert svc.take_overflow(1, slow_id) is True
        assert svc.take_overflow(1, slow_id) is False, "reading the mark clears it"
        assert svc.take_overflow(1, fast_id) is False

    def test_unsubscribe_forgets_a_pending_overflow(self) -> None:
        svc = OutputStreamService()
        sub_id, _ = svc.subscribe(run_id=1)
        for i in range(201):
            svc.publish(1, f"line {i}")
        svc.unsubscribe(1, sub_id)
        assert not svc.take_overflow(1, sub_id)
        assert not svc._overflowed

    def test_has_subscribers(self) -> None:
        svc = OutputStreamService()
        assert not svc.has_subscribers(1)
        sub_id, _ = svc.subscribe(run_id=1)
        assert svc.has_subscribers(1)
        svc.unsubscribe(1, sub_id)
        assert not svc.has_subscribers(1)

    def test_unsubscribe_last_cleans_run_entry(self) -> None:
        svc = OutputStreamService()
        sub_id, _ = svc.subscribe(run_id=1)
        svc.unsubscribe(1, sub_id)
        assert 1 not in svc._subscribers

    def test_unsubscribe_nonexistent_is_noop(self) -> None:
        svc = OutputStreamService()
        svc.unsubscribe(99, 99)  # should not raise


class TestGetOutputStreamService:
    def test_returns_singleton(self) -> None:
        import sova.dashboard.services.output_stream_service as mod

        mod._output_stream_service = None
        s1 = get_output_stream_service()
        s2 = get_output_stream_service()
        assert s1 is s2
        mod._output_stream_service = None


# Integration tests for SSE output endpoint


@pytest.fixture
async def setup_db():
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


@pytest.mark.asyncio
async def test_output_stream_returns_sse_content_type(setup_db) -> None:
    """SSE output stream endpoint returns event-stream content type."""
    from sova.dashboard.routers.agents import stream_agent_output

    mock_request = AsyncMock()
    mock_request.is_disconnected = AsyncMock(return_value=True)

    response = await stream_agent_output(run_id=1, request=mock_request)
    assert response.media_type == "text/event-stream"
    assert response.headers.get("Cache-Control") == "no-cache"
    assert response.headers.get("X-Accel-Buffering") == "no"


@pytest.mark.asyncio
async def test_output_stream_sends_connected_event(setup_db) -> None:
    """SSE stream should emit 'connected' as the first event."""
    from sova.dashboard.routers.agents import stream_agent_output

    mock_request = AsyncMock()
    # Disconnect after first yield
    call_count = 0

    async def is_disconnected():
        nonlocal call_count
        call_count += 1
        return call_count > 1

    mock_request.is_disconnected = is_disconnected

    # Patch asyncio.wait_for to use shorter timeout (0.1s instead of 15s)
    original_wait_for = asyncio.wait_for

    async def fast_wait_for(coro, timeout):
        return await original_wait_for(coro, timeout=min(timeout, 0.1))

    with patch("sova.dashboard.routers.agents.asyncio.wait_for", fast_wait_for):
        response = await stream_agent_output(run_id=999, request=mock_request)
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)

    assert any("event: connected" in c for c in chunks)


@pytest.mark.asyncio
async def test_output_stream_delivers_published_lines(setup_db) -> None:
    """Lines published via OutputStreamService appear as SSE events."""
    import sova.dashboard.services.output_stream_service as mod

    svc = OutputStreamService()
    mod._output_stream_service = svc

    from sova.dashboard.routers.agents import stream_agent_output

    mock_request = AsyncMock()
    call_count = 0

    async def is_disconnected() -> bool:
        nonlocal call_count
        call_count += 1
        # The first check lets the generator read the published line; the
        # second ends the stream, so it never waits out the 15s keepalive.
        return call_count > 1

    mock_request.is_disconnected = is_disconnected

    # Pre-publish a line so it's available when the generator reads the queue
    sub_id, queue = svc.subscribe(run_id=42)
    await queue.put("test output line")
    svc.unsubscribe(42, sub_id)

    # The endpoint creates its own subscription; publish only once it exists,
    # ordered on the subscription itself rather than on a fixed delay.
    subscribed = asyncio.Event()
    original_subscribe = svc.subscribe

    def recording_subscribe(run_id: int) -> tuple[int, asyncio.Queue[str]]:
        result = original_subscribe(run_id)
        if run_id == 42:
            subscribed.set()
        return result

    async def publish_after_subscribe() -> None:
        await asyncio.wait_for(subscribed.wait(), timeout=5)
        svc.publish(42, "hello from agent")

    async def collect(response: StreamingResponse) -> list[str]:
        return [chunk async for chunk in response.body_iterator]

    task = asyncio.create_task(publish_after_subscribe())
    try:
        with patch.object(svc, "subscribe", recording_subscribe):
            response = await stream_agent_output(run_id=42, request=mock_request)
        chunks = await asyncio.wait_for(collect(response), timeout=5)
        await asyncio.wait_for(task, timeout=5)
    finally:
        if not task.done():
            task.cancel()
        mod._output_stream_service = None

    combined = "".join(chunks)
    assert "event: output" in combined
    assert "hello from agent" in combined


@pytest.mark.asyncio
async def test_output_stream_reports_a_dropped_line_as_a_gap_event(setup_db) -> None:
    """A slow reader's full queue drops a line; the stream says so, since the client cannot find the hole."""
    import sova.dashboard.services.output_stream_service as mod

    svc = OutputStreamService()
    mod._output_stream_service = svc

    from sova.dashboard.routers.agents import stream_agent_output

    mock_request = AsyncMock()
    call_count = 0

    async def is_disconnected() -> bool:
        nonlocal call_count
        call_count += 1
        return call_count > 1

    mock_request.is_disconnected = is_disconnected

    try:
        response = await stream_agent_output(run_id=77, request=mock_request)
        for i in range(201):  # the 201st finds the 200-line queue full
            svc.publish(77, f"line {i}")
        chunks = [chunk async for chunk in response.body_iterator]
    finally:
        mod._output_stream_service = None

    combined = "".join(chunks)
    assert "event: gap" in combined
    assert combined.index("event: gap") < combined.index("event: output")


@pytest.mark.asyncio
async def test_output_stream_sends_no_gap_when_nothing_was_dropped(setup_db) -> None:
    import sova.dashboard.services.output_stream_service as mod

    svc = OutputStreamService()
    mod._output_stream_service = svc

    from sova.dashboard.routers.agents import stream_agent_output

    mock_request = AsyncMock()
    call_count = 0

    async def is_disconnected() -> bool:
        nonlocal call_count
        call_count += 1
        return call_count > 1

    mock_request.is_disconnected = is_disconnected

    try:
        response = await stream_agent_output(run_id=78, request=mock_request)
        svc.publish(78, "only line")
        chunks = [chunk async for chunk in response.body_iterator]
    finally:
        mod._output_stream_service = None

    combined = "".join(chunks)
    assert "event: output" in combined
    assert "event: gap" not in combined


@pytest.mark.asyncio
async def test_output_stream_sends_done_on_terminal_run(setup_db) -> None:
    """When the run reaches terminal status, stream sends 'done' event."""
    import sova.dashboard.services.output_stream_service as mod

    svc = OutputStreamService()
    mod._output_stream_service = svc

    from sova.dashboard.routers.agents import stream_agent_output

    mock_request = AsyncMock()
    call_count = 0

    async def is_disconnected():
        nonlocal call_count
        call_count += 1
        return call_count > 1

    mock_request.is_disconnected = is_disconnected

    with (
        patch("sova.dashboard.routers.agents._check_run_terminal", return_value=True),
        patch("sova.dashboard.routers.agents.asyncio.wait_for", side_effect=asyncio.TimeoutError),
    ):
        response = await stream_agent_output(run_id=1, request=mock_request)
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)

    combined = "".join(chunks)
    assert "event: done" in combined

    mod._output_stream_service = None


@pytest.mark.asyncio
async def test_buffer_line_publishes_to_output_stream(setup_db) -> None:
    """_buffer_line should push lines to OutputStreamService subscribers."""
    import sova.dashboard.services.output_stream_service as mod

    svc = OutputStreamService()
    mod._output_stream_service = svc

    from sova.dashboard.services.agent_output import _buffer_line
    from sova.dashboard.services.agent_pool import AgentState

    agent = AgentState.__new__(AgentState)
    agent.run_id = 10
    agent.output_lines = []

    class FakeDeque(list):
        def append(self, item):
            super().append(item)

    agent.output_lines = FakeDeque()
    agent.output_writer = None

    _sub_id, queue = svc.subscribe(run_id=10)
    await _buffer_line(agent, "test line")

    assert not queue.empty()
    assert queue.get_nowait() == "test line"

    mod._output_stream_service = None


@pytest.mark.asyncio
async def test_check_run_terminal_exception_returns_false(setup_db) -> None:
    """_check_run_terminal returns False and logs on DB errors."""
    from sova.dashboard.routers.agents import _check_run_terminal

    with patch("sova.dashboard.routers.agents.get_session", side_effect=RuntimeError("db down")):
        result = await _check_run_terminal(run_id=999)
    assert result is False


# Line sequence numbers: let a reconnecting log viewer drop exactly the lines its backfill and its stream share


def _fake_agent(run_id: int, existing: list[str] | None = None):
    from collections import deque

    from sova.dashboard.services.agent_pool import AgentState

    agent = AgentState.__new__(AgentState)
    agent.run_id = run_id
    agent.output_lines = deque(existing or [], maxlen=5000)
    agent.output_seq = len(existing or [])
    agent.output_writer = None
    return agent


@pytest.mark.asyncio
async def test_buffer_line_counts_every_line_and_keeps_counting_past_the_deque_cap(setup_db) -> None:
    """output_seq is the run's lifetime line count, so it is not capped like the deque is."""
    from collections import deque

    from sova.dashboard.services.agent_output import _buffer_line

    agent = _fake_agent(11)
    agent.output_lines = deque(maxlen=2)
    for text in ("a", "b", "c"):
        await _buffer_line(agent, text)

    assert list(agent.output_lines) == ["b", "c"]
    assert agent.output_seq == 3


@pytest.mark.asyncio
async def test_buffer_line_publishes_before_the_db_flush_can_suspend(setup_db) -> None:
    """A subscriber that reads output_seq must receive every later line, so the count and the publish happen
    in one step with the flush (the only await) after them."""
    import sova.dashboard.services.output_stream_service as mod
    from sova.dashboard.services.agent_output import _buffer_line

    svc = OutputStreamService()
    mod._output_stream_service = svc
    flush_started = asyncio.Event()
    release_flush = asyncio.Event()

    class SlowWriter:
        def write_line(self, text: str) -> None:
            pass

        def should_flush(self) -> bool:
            return True

        async def flush(self) -> None:
            flush_started.set()
            await release_flush.wait()

    agent = _fake_agent(12)
    agent.output_writer = SlowWriter()
    _sub_id, queue = svc.subscribe(run_id=12)
    try:
        task = asyncio.create_task(_buffer_line(agent, "line"))
        await asyncio.wait_for(flush_started.wait(), timeout=5)
        assert agent.output_seq == 1
        assert queue.get_nowait() == "line", "published while the flush is still suspended"
        release_flush.set()
        await asyncio.wait_for(task, timeout=5)
    finally:
        mod._output_stream_service = None


def test_get_output_seq_reports_an_in_memory_agent_and_none_otherwise() -> None:
    from unittest.mock import MagicMock

    from sova.dashboard.services.agent_output import get_output_seq

    pa = MagicMock()
    pa.agents = {42: _fake_agent(42, ["x", "y", "z"])}
    with patch("sova.dashboard.services.agent_pool._get_project_agents", return_value=pa):
        assert get_output_seq(run_id=42) == 3
        assert get_output_seq(run_id=43) is None


@pytest.mark.asyncio
async def test_output_endpoint_returns_the_sequence_read_with_the_lines(setup_db) -> None:
    from unittest.mock import MagicMock

    from sova.dashboard.routers.agents import get_agent_output

    pa = MagicMock()
    pa.agents = {42: _fake_agent(42, ["x", "y", "z"])}
    with patch("sova.dashboard.services.agent_pool._get_project_agents", return_value=pa):
        in_memory = await get_agent_output(run_id=42, since=0)
        absent = await get_agent_output(run_id=99, since=0)

    assert in_memory == {"lines": ["x", "y", "z"], "total": 3, "seq": 3}
    assert absent["seq"] is None


@pytest.mark.asyncio
async def test_connected_event_carries_the_sequence_at_subscribe_time(setup_db) -> None:
    """The count is read right after the subscribe, so a line buffered while the generator waits to run is
    pushed to the stream but not counted in the event."""
    import json
    from unittest.mock import MagicMock

    import sova.dashboard.services.output_stream_service as mod
    from sova.dashboard.routers.agents import stream_agent_output

    svc = OutputStreamService()
    mod._output_stream_service = svc
    agent = _fake_agent(42, ["x", "y"])
    pa = MagicMock()
    pa.agents = {42: agent}

    mock_request = AsyncMock()
    mock_request.is_disconnected = AsyncMock(side_effect=[False, True])
    try:
        with patch("sova.dashboard.services.agent_pool._get_project_agents", return_value=pa):
            response = await stream_agent_output(run_id=42, request=mock_request)
            svc.publish(42, "z")
            agent.output_lines.append("z")
            agent.output_seq += 1
            chunks = [chunk async for chunk in response.body_iterator]
    finally:
        mod._output_stream_service = None

    connected = next(c for c in chunks if c.startswith("event: connected"))
    assert json.loads(connected.split("data: ", 1)[1]) == {"seq": 2}
    assert any("data: z" in c for c in chunks)


@pytest.mark.asyncio
async def test_connected_event_has_no_sequence_for_an_agent_not_in_memory(setup_db) -> None:
    import json
    from unittest.mock import MagicMock

    from sova.dashboard.routers.agents import stream_agent_output

    pa = MagicMock()
    pa.agents = {}
    mock_request = AsyncMock()
    mock_request.is_disconnected = AsyncMock(return_value=True)
    with patch("sova.dashboard.services.agent_pool._get_project_agents", return_value=pa):
        response = await stream_agent_output(run_id=77, request=mock_request)
        chunks = [chunk async for chunk in response.body_iterator]

    connected = next(c for c in chunks if c.startswith("event: connected"))
    assert json.loads(connected.split("data: ", 1)[1]) == {}
