"""Tests for sova.dashboard.services.output_stream_service and SSE output endpoint."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest

from sova.dashboard.services.output_stream_service import (
    OutputStreamService,
    get_output_stream_service,
)
from sova.db.session import close_db, init_db

# -- Unit tests for OutputStreamService --


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


# -- Integration tests for SSE output endpoint --


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

    async def is_disconnected():
        nonlocal call_count
        call_count += 1
        return call_count > 2

    mock_request.is_disconnected = is_disconnected

    # Pre-publish a line so it's available when the generator reads the queue
    sub_id, queue = svc.subscribe(run_id=42)
    await queue.put("test output line")
    svc.unsubscribe(42, sub_id)

    # Re-subscribe via the endpoint (it creates its own subscription)
    # We need to publish after the endpoint subscribes, so use a task
    async def publish_after_delay():
        await asyncio.sleep(0.05)
        svc.publish(42, "hello from agent")

    task = asyncio.create_task(publish_after_delay())

    response = await stream_agent_output(run_id=42, request=mock_request)
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)

    await task
    combined = "".join(chunks)
    assert "event: output" in combined
    assert "hello from agent" in combined

    mod._output_stream_service = None


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

    with patch("sova.dashboard.routers.agents._check_run_terminal", return_value=True), \
         patch("sova.dashboard.routers.agents.asyncio.wait_for", side_effect=asyncio.TimeoutError):
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
