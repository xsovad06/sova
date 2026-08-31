"""Tests for sova.dashboard.services.feed_service and feed router."""

from __future__ import annotations

import asyncio
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


# DB persistence + pagination


class TestFeedPersistence:
    @pytest.mark.asyncio
    async def test_emit_persists_to_db(self, feed_service: FeedService) -> None:
        feed_service.emit("Persisted event", category="agent", metadata={"run_id": 7})
        # The DB write is scheduled as a background task; let it run.
        await asyncio.sleep(0.05)
        events, has_more = await feed_service.history_page(limit=50)
        assert has_more is False
        assert len(events) == 1
        assert events[0]["title"] == "Persisted event"
        assert events[0]["category"] == "agent"
        assert events[0]["metadata"] == {"run_id": 7}

    @pytest.mark.asyncio
    async def test_history_page_returns_oldest_first(self, feed_service: FeedService) -> None:
        for i in range(5):
            feed_service.emit(f"Event {i}")
        await asyncio.sleep(0.05)
        events, _ = await feed_service.history_page(limit=50)
        titles = [e["title"] for e in events]
        assert titles == ["Event 0", "Event 1", "Event 2", "Event 3", "Event 4"]

    @pytest.mark.asyncio
    async def test_history_page_pagination_before_id(self, feed_service: FeedService) -> None:
        for i in range(5):
            feed_service.emit(f"Event {i}")
        await asyncio.sleep(0.05)
        # First page: newest 2.
        page1, has_more1 = await feed_service.history_page(limit=2)
        assert has_more1 is True
        assert [e["title"] for e in page1] == ["Event 3", "Event 4"]
        # Older page using the oldest id of page1 as the cursor.
        cursor = page1[0]["id"]
        page2, has_more2 = await feed_service.history_page(before_id=cursor, limit=2)
        assert [e["title"] for e in page2] == ["Event 1", "Event 2"]
        assert has_more2 is True
        page3, has_more3 = await feed_service.history_page(before_id=page2[0]["id"], limit=2)
        assert [e["title"] for e in page3] == ["Event 0"]
        assert has_more3 is False

    @pytest.mark.asyncio
    async def test_init_counter_seeds_from_db_max(self, feed_service: FeedService) -> None:
        from sova.db.models import FeedEventRecord
        from sova.db.session import get_session

        # Persist a row with a high id, as if from a previous process.
        async with await get_session() as session:
            session.add(FeedEventRecord(id=500, severity="info", title="old", category="system"))
            await session.commit()

        await feed_service.init_counter()
        # The next emitted event must not collide with the persisted id.
        event = feed_service.emit("after restart")
        assert event.id > 500
        await asyncio.sleep(0.05)
        events, _ = await feed_service.history_page(limit=50)
        # Both the seeded row and the new event are present, ordered by id.
        titles = [e["title"] for e in events]
        assert titles == ["old", "after restart"]

    @pytest.mark.asyncio
    async def test_init_counter_only_advances(self, feed_service: FeedService) -> None:
        # No rows yet: emit advances the counter beyond the DB max.
        e1 = feed_service.emit("first")
        await asyncio.sleep(0.05)
        # Seeding from a DB whose max is below the current counter must not rewind.
        await feed_service.init_counter()
        e2 = feed_service.emit("second")
        assert e2.id > e1.id

    @pytest.mark.asyncio
    async def test_prune_old_events(self, feed_service: FeedService) -> None:
        from datetime import datetime, timedelta, timezone

        from sova.db.models import FeedEventRecord
        from sova.db.session import get_session

        # Insert one old and one recent event directly.
        async with await get_session() as session:
            session.add(
                FeedEventRecord(
                    severity="info",
                    title="old",
                    category="system",
                    created_at=datetime.now(timezone.utc) - timedelta(days=40),
                )
            )
            session.add(
                FeedEventRecord(
                    severity="info",
                    title="fresh",
                    category="system",
                    created_at=datetime.now(timezone.utc),
                )
            )
            await session.commit()

        deleted = await feed_service.prune_old_events(retention_days=30)
        assert deleted == 1
        events, _ = await feed_service.history_page(limit=50)
        assert [e["title"] for e in events] == ["fresh"]


@pytest.mark.asyncio
async def test_feed_history_before_id_endpoint(client: AsyncClient, feed_service: FeedService) -> None:
    for i in range(4):
        feed_service.emit(f"Event {i}")
    await asyncio.sleep(0.05)
    resp = await client.get("/api/feed/history?before_id=1000&limit=2")
    assert resp.status_code == 200
    data = resp.json()
    assert "has_more" in data
    assert data["has_more"] is True
    assert [e["title"] for e in data["events"]] == ["Event 2", "Event 3"]


@pytest.mark.asyncio
async def test_feed_briefing_empty_when_disabled(client: AsyncClient) -> None:
    """Briefing endpoint returns an empty briefing (200) when awareness is off."""
    resp = await client.get("/api/feed/briefing")
    assert resp.status_code == 200
    data = resp.json()
    assert data["attention_items"] == []
    assert data["informational_items"] == []
    assert data["schedule"] == []


@pytest.mark.asyncio
async def test_feed_history_gap_fill_backfills_from_db(client: AsyncClient, feed_service: FeedService) -> None:
    """When the buffer has dropped events, gap-fill paginates through DB."""
    import sova.dashboard.services.feed_service as mod

    old_size = mod._BUFFER_SIZE
    mod._BUFFER_SIZE = 3
    feed_service._buffer = __import__("collections").deque(maxlen=3)

    for i in range(6):
        feed_service.emit(f"Event {i}")
    await asyncio.sleep(0.05)

    first_event_id = 1
    resp = await client.get(f"/api/feed/history?since_id={first_event_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("gap_detected") is True
    titles = [e["title"] for e in data["events"]]
    assert "Event 1" in titles
    assert "Event 5" in titles

    mod._BUFFER_SIZE = old_size


@pytest.mark.asyncio
async def test_feed_history_no_gap_when_buffer_empty(client: AsyncClient, feed_service: FeedService) -> None:
    """Empty buffer does not trigger gap detection (oldest_id is 0)."""
    resp = await client.get("/api/feed/history?since_id=999")
    assert resp.status_code == 200
    data = resp.json()
    assert "gap_detected" not in data
    assert data["events"] == []


@pytest.mark.asyncio
async def test_feed_history_gap_multi_page_backfill(client: AsyncClient, feed_service: FeedService) -> None:
    """Gap-fill paginates through multiple DB pages to recover all events."""
    import sova.dashboard.services.feed_service as mod

    old_size = mod._BUFFER_SIZE
    mod._BUFFER_SIZE = 2
    feed_service._buffer = __import__("collections").deque(maxlen=2)

    for i in range(8):
        feed_service.emit(f"Event {i}")
    await asyncio.sleep(0.05)

    first_event_id = 1
    resp = await client.get(f"/api/feed/history?since_id={first_event_id}&limit=2")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("gap_detected") is True
    titles = [e["title"] for e in data["events"]]
    assert len(titles) >= 4

    mod._BUFFER_SIZE = old_size


@pytest.mark.asyncio
async def test_feed_drain_persist(feed_service: FeedService) -> None:
    """drain_persist awaits all in-flight persistence tasks."""
    feed_service.emit("Drain test")
    await feed_service.drain_persist()
    events, _ = await feed_service.history_page(limit=50)
    assert len(events) == 1
    assert events[0]["title"] == "Drain test"


@pytest.mark.asyncio
async def test_feed_briefing_exception_returns_empty(client: AsyncClient) -> None:
    """Briefing endpoint catches exceptions and returns empty briefing."""
    with patch("sova.config.loader.load_config", side_effect=RuntimeError("boom")):
        resp = await client.get("/api/feed/briefing")
        assert resp.status_code == 200
        data = resp.json()
        assert data["generated_at"] is None
        assert data["attention_items"] == []


@pytest.mark.asyncio
async def test_feed_briefing_with_providers(client: AsyncClient) -> None:
    """Briefing endpoint returns serialized briefing when awareness is configured."""
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    mock_cfg = MagicMock()
    mock_cfg.awareness.enabled = True
    mock_cfg.awareness.providers = ["github"]

    mock_item = SimpleNamespace(
        id="item-1",
        provider="github",
        category=SimpleNamespace(value="pr"),
        title="Review needed",
        body="PR #1 needs review",
        source_url="https://github.com/pr/1",
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        urgency=5,
        action_hint="Review",
    )
    mock_status = SimpleNamespace(name="github", ok=True, message="OK", items_fetched=1)
    mock_briefing = SimpleNamespace(
        generated_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        attention_items=[mock_item],
        informational_items=[],
        schedule=[],
        provider_statuses=[mock_status],
    )

    mock_service = AsyncMock()
    mock_service.generate_briefing = AsyncMock(return_value=mock_briefing)

    with (
        patch("sova.config.loader.load_config", return_value=mock_cfg),
        patch("sova.awareness.create_providers", return_value=[MagicMock()]),
        patch("sova.awareness.briefing.BriefingService", return_value=mock_service),
    ):
        resp = await client.get("/api/feed/briefing")
        assert resp.status_code == 200
        data = resp.json()
        assert data["generated_at"] is not None
        assert len(data["attention_items"]) == 1
        assert data["attention_items"][0]["title"] == "Review needed"
        assert data["provider_statuses"][0]["name"] == "github"


def test_serialize_item_and_briefing() -> None:
    """Cover the briefing serialization helpers."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from sova.dashboard.routers.feed import _serialize_briefing, _serialize_item

    item = SimpleNamespace(
        id="item-1",
        provider="github",
        category=SimpleNamespace(value="pr"),
        title="New PR",
        body="Details here",
        source_url="https://github.com/pr/1",
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        urgency=3,
        action_hint="Review it",
    )
    serialized = _serialize_item(item)
    assert serialized["id"] == "item-1"
    assert serialized["provider"] == "github"
    assert serialized["category"] == "pr"
    assert serialized["timestamp"] == "2026-01-01T00:00:00+00:00"
    assert serialized["urgency"] == 3
    assert serialized["source_url"] == "https://github.com/pr/1"

    status = SimpleNamespace(name="github", ok=True, message="OK", items_fetched=5)
    briefing = SimpleNamespace(
        generated_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        attention_items=[item],
        informational_items=[],
        schedule=[],
        provider_statuses=[status],
    )
    result = _serialize_briefing(briefing)
    assert result["generated_at"] == "2026-01-01T12:00:00+00:00"
    assert len(result["attention_items"]) == 1
    assert result["provider_statuses"][0]["name"] == "github"
    assert result["provider_statuses"][0]["items_fetched"] == 5

    none_ts_item = SimpleNamespace(
        id="x",
        provider="p",
        category="raw_string",
        title="T",
        body="B",
        source_url=None,
        timestamp=None,
        urgency=0,
        action_hint=None,
    )
    s2 = _serialize_item(none_ts_item)
    assert s2["timestamp"] is None
    assert s2["category"] == "raw_string"

    none_briefing = SimpleNamespace(
        generated_at=None,
        attention_items=[],
        informational_items=[],
        schedule=[],
        provider_statuses=[],
    )
    r2 = _serialize_briefing(none_briefing)
    assert r2["generated_at"] is None


def test_serialize_item_with_occurrence_count() -> None:
    """_serialize_item includes occurrence_count when non-zero."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from sova.dashboard.routers.feed import _serialize_item

    item = SimpleNamespace(
        id="gcal:standup",
        provider="gcal",
        category=SimpleNamespace(value="informational"),
        title="Daily Standup",
        body="",
        source_url="",
        timestamp=datetime(2026, 9, 1, tzinfo=timezone.utc),
        urgency=0,
        action_hint="Upcoming meeting",
        occurrence_count=3,
        metadata={"recurring_event_id": "base123"},
    )
    result = _serialize_item(item)
    assert result["occurrence_count"] == 3
    assert result["recurring_event_id"] == "base123"
    assert "is_recurring_exception" not in result


def test_serialize_item_with_recurring_exception() -> None:
    """_serialize_item includes is_recurring_exception when set in metadata."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from sova.dashboard.routers.feed import _serialize_item

    item = SimpleNamespace(
        id="gcal:standup-exc",
        provider="gcal",
        category=SimpleNamespace(value="needs_attention"),
        title="Daily Standup",
        body="",
        source_url="",
        timestamp=datetime(2026, 9, 1, tzinfo=timezone.utc),
        urgency=2,
        action_hint="Attendees changed",
        occurrence_count=0,
        metadata={"is_recurring_exception": True, "recurring_event_id": "base123"},
    )
    result = _serialize_item(item)
    assert result["is_recurring_exception"] is True
    assert result["recurring_event_id"] == "base123"
    assert "occurrence_count" not in result


def test_serialize_item_without_recurring_fields() -> None:
    """_serialize_item omits recurring fields when not present."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from sova.dashboard.routers.feed import _serialize_item

    item = SimpleNamespace(
        id="gcal:one-off",
        provider="gcal",
        category=SimpleNamespace(value="informational"),
        title="Lunch",
        body="",
        source_url="",
        timestamp=datetime(2026, 9, 1, tzinfo=timezone.utc),
        urgency=0,
        action_hint="Upcoming",
        occurrence_count=0,
        metadata={},
    )
    result = _serialize_item(item)
    assert "occurrence_count" not in result
    assert "is_recurring_exception" not in result
    assert "recurring_event_id" not in result


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
