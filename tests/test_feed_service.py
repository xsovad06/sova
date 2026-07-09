"""Tests for sova.dashboard.services.feed_service and feed router."""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from sova.dashboard.services.feed_service import (
    FeedEvent,
    FeedEventSeverity,
    FeedService,
    emit_safe,
    get_feed_service,
)
from sova.db.session import close_db, init_db

# -- Unit tests for FeedService --


class TestFeedService:
    def test_emit_returns_event(self) -> None:
        svc = FeedService()
        event = svc.emit("Test event")
        assert isinstance(event, FeedEvent)
        assert event.id == 1
        assert event.title == "Test event"
        assert event.severity == FeedEventSeverity.info

    def test_emit_increments_id(self) -> None:
        svc = FeedService()
        e1 = svc.emit("First")
        e2 = svc.emit("Second")
        assert e2.id == e1.id + 1

    def test_emit_with_severity_and_metadata(self) -> None:
        svc = FeedService()
        event = svc.emit(
            "Agent failed",
            severity=FeedEventSeverity.error,
            detail="Exit code 1",
            category="agent",
            metadata={"run_id": 42},
        )
        assert event.severity == FeedEventSeverity.error
        assert event.detail == "Exit code 1"
        assert event.category == "agent"
        assert event.metadata == {"run_id": 42}

    def test_history_returns_all_events(self) -> None:
        svc = FeedService()
        svc.emit("One")
        svc.emit("Two")
        svc.emit("Three")
        events, _ = svc.history()
        assert len(events) == 3

    def test_history_since_id(self) -> None:
        svc = FeedService()
        svc.emit("One")
        e2 = svc.emit("Two")
        svc.emit("Three")
        events, _ = svc.history(since_id=e2.id)
        assert len(events) == 1
        assert events[0].title == "Three"

    def test_ring_buffer_caps_at_max(self) -> None:
        svc = FeedService()
        for i in range(600):
            svc.emit(f"Event {i}")
        events, _ = svc.history()
        assert len(events) == 500  # _BUFFER_SIZE

    def test_subscribe_receives_events(self) -> None:
        svc = FeedService()
        sub_id, queue = svc.subscribe()
        svc.emit("Hello")
        assert not queue.empty()
        event = queue.get_nowait()
        assert event.title == "Hello"
        svc.unsubscribe(sub_id)

    def test_unsubscribe_stops_delivery(self) -> None:
        svc = FeedService()
        sub_id, queue = svc.subscribe()
        svc.unsubscribe(sub_id)
        svc.emit("Missed")
        assert queue.empty()

    def test_multiple_subscribers(self) -> None:
        svc = FeedService()
        id1, q1 = svc.subscribe()
        id2, q2 = svc.subscribe()
        svc.emit("Broadcast")
        assert q1.get_nowait().title == "Broadcast"
        assert q2.get_nowait().title == "Broadcast"
        svc.unsubscribe(id1)
        svc.unsubscribe(id2)

    def test_slow_consumer_drops_events(self) -> None:
        svc = FeedService()
        _sub_id, queue = svc.subscribe()
        # Fill the queue to capacity (100)
        for i in range(110):
            svc.emit(f"Event {i}")
        assert queue.qsize() == 100  # capped at _QUEUE_MAX

    def test_to_sse_format(self) -> None:
        svc = FeedService()
        event = svc.emit("Test")
        sse = svc.to_sse(event)
        assert sse.startswith(f"id: {event.id}\n")
        assert "event: feed\n" in sse
        assert "data: " in sse
        data_line = [line for line in sse.split("\n") if line.startswith("data: ")][0]
        parsed = json.loads(data_line[6:])
        assert parsed["title"] == "Test"

    def test_event_to_dict(self) -> None:
        svc = FeedService()
        event = svc.emit("Test", category="agent", metadata={"x": 1})
        d = event.to_dict()
        assert d["id"] == event.id
        assert d["severity"] == "info"
        assert d["title"] == "Test"
        assert d["category"] == "agent"
        assert d["metadata"] == {"x": 1}
        assert isinstance(d["timestamp"], float)


class TestGetFeedService:
    def test_returns_singleton(self) -> None:
        import sova.dashboard.services.feed_service as mod

        mod._feed_service = None
        s1 = get_feed_service()
        s2 = get_feed_service()
        assert s1 is s2
        mod._feed_service = None  # cleanup


# -- Integration tests for feed router --


@pytest.fixture(autouse=True)
async def setup_db():
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


@pytest.fixture
def feed_service():
    """Provide a fresh FeedService for each test."""
    import sova.dashboard.services.feed_service as mod

    svc = FeedService()
    mod._feed_service = svc
    yield svc
    mod._feed_service = None


@pytest.fixture
async def client(feed_service):
    from sova.dashboard.app import create_app

    app = create_app(project_dir=Path.cwd())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_feed_history_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/feed/history")
    assert resp.status_code == 200
    data = resp.json()
    assert data["events"] == []


@pytest.mark.asyncio
async def test_feed_history_returns_events(client: AsyncClient, feed_service: FeedService) -> None:
    feed_service.emit("Event A")
    feed_service.emit("Event B")
    resp = await client.get("/api/feed/history")
    assert resp.status_code == 200
    events = resp.json()["events"]
    assert len(events) == 2
    assert events[0]["title"] == "Event A"
    assert events[1]["title"] == "Event B"


@pytest.mark.asyncio
async def test_feed_history_since_id(client: AsyncClient, feed_service: FeedService) -> None:
    feed_service.emit("Event A")
    e2 = feed_service.emit("Event B")
    feed_service.emit("Event C")
    resp = await client.get(f"/api/feed/history?since_id={e2.id}")
    assert resp.status_code == 200
    events = resp.json()["events"]
    assert len(events) == 1
    assert events[0]["title"] == "Event C"


def test_feed_service_to_sse_includes_all_fields() -> None:
    """Verify SSE formatting includes id, event, and data lines."""
    svc = FeedService()
    event = svc.emit("SSE test", severity=FeedEventSeverity.warning, category="agent")
    sse = svc.to_sse(event)
    lines = sse.strip().split("\n")
    assert lines[0] == f"id: {event.id}"
    assert lines[1] == "event: feed"
    assert lines[2].startswith("data: ")
    parsed = json.loads(lines[2][6:])
    assert parsed["severity"] == "warning"
    assert parsed["category"] == "agent"


class TestEmitSafe:
    def test_emit_safe_succeeds(self) -> None:
        import sova.dashboard.services.feed_service as mod

        svc = FeedService()
        mod._feed_service = svc
        emit_safe("safe event", severity=FeedEventSeverity.info)
        assert svc.history()[0][-1].title == "safe event"
        mod._feed_service = None

    def test_emit_safe_swallows_exception(self) -> None:
        """emit_safe should not raise even when get_feed_service().emit raises."""
        with patch("sova.dashboard.services.feed_service.get_feed_service", side_effect=RuntimeError("boom")):
            emit_safe("will fail")  # should not raise


@pytest.mark.asyncio
async def test_feed_stream_returns_sse_content_type(feed_service: FeedService) -> None:
    """SSE stream endpoint returns event-stream content type."""
    # Call the endpoint function directly to avoid httpx stream hang
    from unittest.mock import AsyncMock

    from sova.dashboard.routers.feed import feed_stream

    mock_request = AsyncMock()
    mock_request.is_disconnected = AsyncMock(return_value=True)

    response = await feed_stream(mock_request)
    assert response.media_type == "text/event-stream"
    assert response.headers.get("Cache-Control") == "no-cache"
    assert response.headers.get("X-Accel-Buffering") == "no"


class TestEmitFinalizeEvent:
    def test_emit_finalize_event_success(self) -> None:
        import sova.dashboard.services.feed_service as mod
        from sova.dashboard.services.agent_db import _emit_finalize_event
        from sova.dashboard.services.agent_pool import AgentState

        svc = FeedService()
        mod._feed_service = svc

        agent = AgentState.__new__(AgentState)
        agent.issue = "42"
        agent.role = "developer"
        agent.project_dir = Path.cwd()
        agent.last_result_cost = None

        _emit_finalize_event(1, status="done", exit_code=0, agent=agent, cost=Decimal("1.50"))
        events, _ = svc.history()
        assert len(events) == 1
        assert "#42 Developer done" in events[0].title
        assert events[0].severity == FeedEventSeverity.success

        mod._feed_service = None

    def test_emit_finalize_event_failure(self) -> None:
        import sova.dashboard.services.feed_service as mod
        from sova.dashboard.services.agent_db import _emit_finalize_event
        from sova.dashboard.services.agent_pool import AgentState

        svc = FeedService()
        mod._feed_service = svc

        agent = AgentState.__new__(AgentState)
        agent.issue = None
        agent.role = None
        agent.project_dir = Path.cwd()
        agent.last_result_cost = None

        _emit_finalize_event(2, status="failed", exit_code=1, agent=agent, cost=Decimal("0"))
        events, _ = svc.history()
        assert len(events) == 1
        assert events[0].severity == FeedEventSeverity.error
        assert "Agent" in events[0].title
        assert events[0].detail == "Exit code: 1"

        mod._feed_service = None
