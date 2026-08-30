"""Tests for CalendarProvider (Google Calendar via googleapis)."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from sova.awareness.base import ItemCategory
from sova.config.models import AwarenessConfig


@pytest.fixture
def awareness_config() -> AwarenessConfig:
    return AwarenessConfig(
        enabled=True,
        providers=["gcal"],
        gcal_calendars=["primary"],
        gcal_lookahead_hours=36,
    )


def _setup_gcal_mocks(mock_build, mock_auth, events: list[dict]) -> None:
    """Configure Google Calendar API mocks with events."""
    mock_creds = MagicMock()
    mock_creds.valid = True
    mock_creds.scopes = ["https://www.googleapis.com/auth/calendar.readonly"]
    mock_auth.return_value = mock_creds

    mock_service = MagicMock()
    mock_events = MagicMock()
    mock_list = MagicMock()
    mock_list.execute.return_value = {"items": events}
    mock_events.list.return_value = mock_list
    mock_service.events.return_value = mock_events
    mock_build.return_value = mock_service


# ---------------------------------------------------------------------------
# is_configured()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.build")
@patch("sova.awareness.providers.gcal.authenticate_google")
async def test_is_configured_with_valid_credentials(mock_auth, _mock_build, awareness_config: AwarenessConfig) -> None:
    """Provider is configured when authenticate_google succeeds with calendar scope."""
    from sova.awareness.providers.gcal import CalendarProvider

    mock_creds = MagicMock()
    mock_creds.valid = True
    mock_creds.scopes = ["https://www.googleapis.com/auth/calendar.readonly"]
    mock_auth.return_value = mock_creds

    provider = CalendarProvider(awareness_config)
    assert await provider.is_configured() is True


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.build")
@patch("sova.awareness.providers.gcal.authenticate_google")
async def test_is_configured_with_missing_credentials(
    mock_auth, _mock_build, awareness_config: AwarenessConfig
) -> None:
    """Provider is not configured when authenticate_google raises FileNotFoundError."""
    from sova.awareness.providers.gcal import CalendarProvider

    mock_auth.side_effect = FileNotFoundError("google_credentials.json not found")

    provider = CalendarProvider(awareness_config)
    assert await provider.is_configured() is False


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.build")
@patch("sova.awareness.providers.gcal.authenticate_google")
async def test_is_configured_with_import_error(mock_auth, _mock_build, awareness_config: AwarenessConfig) -> None:
    """Provider is not configured when Google libraries are missing."""
    from sova.awareness.providers.gcal import CalendarProvider

    mock_auth.side_effect = ImportError("Google auth libraries not installed")

    provider = CalendarProvider(awareness_config)
    assert await provider.is_configured() is False


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.build")
@patch("sova.awareness.providers.gcal.authenticate_google")
async def test_is_configured_missing_calendar_scope(mock_auth, _mock_build, awareness_config: AwarenessConfig) -> None:
    """Provider is not configured when credentials lack calendar.readonly scope."""
    from sova.awareness.providers.gcal import CalendarProvider

    mock_creds = MagicMock()
    mock_creds.valid = True
    mock_creds.scopes = ["https://www.googleapis.com/auth/gmail.readonly"]  # Wrong scope
    mock_auth.return_value = mock_creds

    provider = CalendarProvider(awareness_config)
    assert await provider.is_configured() is False


# ---------------------------------------------------------------------------
# fetch_items() - categorization
# ---------------------------------------------------------------------------


def _build_event(
    event_id: str,
    summary: str,
    start_time: datetime,
    description: str | None = None,
    all_day: bool = False,
    attendees: list[dict] | None = None,
    location: str | None = None,
    organizer: dict | None = None,
    response_status: str = "accepted",
) -> dict:
    """Build a synthetic Google Calendar event dict."""
    event: dict = {
        "id": event_id,
        "summary": summary,
        "htmlLink": f"https://calendar.google.com/event?eid={event_id}",
    }

    if all_day:
        event["start"] = {"date": start_time.strftime("%Y-%m-%d")}
        event["end"] = {"date": (start_time + timedelta(days=1)).strftime("%Y-%m-%d")}
    else:
        event["start"] = {"dateTime": start_time.isoformat()}
        event["end"] = {"dateTime": (start_time + timedelta(hours=1)).isoformat()}

    if description is not None:
        event["description"] = description

    if attendees is not None:
        event["attendees"] = attendees

    if location is not None:
        event["location"] = location

    if organizer is not None:
        event["organizer"] = organizer

    # Find current user's response status
    if attendees:
        for attendee in attendees:
            if attendee.get("self"):
                attendee["responseStatus"] = response_status

    return event


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_fetch_imminent_meeting(mock_build, mock_auth, awareness_config: AwarenessConfig) -> None:
    """Meeting starting in 15 minutes is NEEDS_ATTENTION."""
    from sova.awareness.providers.gcal import CalendarProvider

    in_15_min = datetime.now() + timedelta(minutes=15)
    event = _build_event(
        "event-1",
        "Team standup",
        in_15_min,
        description="Daily sync",
        attendees=[{"email": "user@example.com", "self": True}],
        location="https://meet.google.com/abc-defg-hij",
    )

    _setup_gcal_mocks(mock_build, mock_auth, [event])

    provider = CalendarProvider(awareness_config)
    items = await provider.fetch_items()

    assert len(items) == 1
    item = items[0]
    assert item.category == ItemCategory.NEEDS_ATTENTION
    assert item.urgency == 2
    assert item.title == "Team standup"
    assert "https://meet.google.com" in item.metadata.get("location", "")


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_fetch_meeting_no_agenda(mock_build, mock_auth, awareness_config: AwarenessConfig) -> None:
    """Meeting with no description is NEEDS_ATTENTION."""
    from sova.awareness.providers.gcal import CalendarProvider

    in_2_hours = datetime.now() + timedelta(hours=2)
    event = _build_event(
        "event-2",
        "Client meeting",
        in_2_hours,
        description=None,  # No agenda
        attendees=[{"email": "user@example.com", "self": True}],
    )

    _setup_gcal_mocks(mock_build, mock_auth, [event])

    provider = CalendarProvider(awareness_config)
    items = await provider.fetch_items()

    assert len(items) == 1
    item = items[0]
    assert item.category == ItemCategory.NEEDS_ATTENTION
    assert item.urgency == 1
    assert "no agenda" in item.action_hint.lower()


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_fetch_meeting_later_today(mock_build, mock_auth, awareness_config: AwarenessConfig) -> None:
    """Meeting later today with agenda is INFORMATIONAL."""
    from sova.awareness.providers.gcal import CalendarProvider

    in_4_hours = datetime.now() + timedelta(hours=4)
    event = _build_event(
        "event-3",
        "Planning session",
        in_4_hours,
        description="Q4 roadmap review",
        attendees=[{"email": "user@example.com", "self": True}],
    )

    _setup_gcal_mocks(mock_build, mock_auth, [event])

    provider = CalendarProvider(awareness_config)
    items = await provider.fetch_items()

    assert len(items) == 1
    item = items[0]
    assert item.category == ItemCategory.INFORMATIONAL
    assert item.urgency == 0


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_fetch_all_day_event(mock_build, mock_auth, awareness_config: AwarenessConfig) -> None:
    """All-day events are INFORMATIONAL."""
    from sova.awareness.providers.gcal import CalendarProvider

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    event = _build_event(
        "event-4",
        "Company offsite",
        today,
        all_day=True,
    )

    _setup_gcal_mocks(mock_build, mock_auth, [event])

    provider = CalendarProvider(awareness_config)
    items = await provider.fetch_items()

    assert len(items) == 1
    item = items[0]
    assert item.category == ItemCategory.INFORMATIONAL


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_fetch_timezone_conversion(mock_build, mock_auth, awareness_config: AwarenessConfig) -> None:
    """Timezone-aware event times are correctly converted to local time for categorization."""
    from datetime import timezone as tz

    from sova.awareness.providers.gcal import CalendarProvider

    mock_creds = MagicMock()
    mock_auth.return_value = mock_creds

    # Create an event 20 minutes from now, but express it in a fixed offset
    # timezone (UTC-8 / PST). The categorization must convert to local time
    # before computing time-to-start; if it uses the raw hour value without
    # conversion the threshold check would be wrong.
    now_utc = datetime.now(tz.utc)
    event_utc = now_utc + timedelta(minutes=20)
    pst = tz(timedelta(hours=-8))
    event_pst = event_utc.astimezone(pst)

    event = {
        "id": "tz-event",
        "summary": "Timezone test meeting",
        "htmlLink": "https://calendar.google.com/event?eid=tz-event",
        "start": {"dateTime": event_pst.isoformat()},
        "end": {"dateTime": (event_pst + timedelta(hours=1)).isoformat()},
        "description": "Test agenda",
        "attendees": [{"email": "user@example.com", "self": True, "responseStatus": "accepted"}],
    }

    _setup_gcal_mocks(mock_build, mock_auth, [event])

    provider = CalendarProvider(awareness_config)
    items = await provider.fetch_items()

    assert len(items) == 1
    item = items[0]
    # Should be NEEDS_ATTENTION because it's within 30 minutes
    assert item.category == ItemCategory.NEEDS_ATTENTION
    assert item.urgency == 2
    assert "starting soon" in item.action_hint.lower()


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_fetch_declined_event_excluded(mock_build, mock_auth, awareness_config: AwarenessConfig) -> None:
    """Declined events are excluded."""
    from sova.awareness.providers.gcal import CalendarProvider

    in_1_hour = datetime.now() + timedelta(hours=1)
    event = _build_event(
        "event-5",
        "Meeting I declined",
        in_1_hour,
        attendees=[{"email": "user@example.com", "self": True}],
        response_status="declined",
    )

    _setup_gcal_mocks(mock_build, mock_auth, [event])

    provider = CalendarProvider(awareness_config)
    items = await provider.fetch_items()

    assert items == []


# ---------------------------------------------------------------------------
# fetch_items() - metadata extraction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_fetch_metadata_attendees(mock_build, mock_auth, awareness_config: AwarenessConfig) -> None:
    """Attendee metadata is correctly extracted."""
    from sova.awareness.providers.gcal import CalendarProvider

    in_1_hour = datetime.now() + timedelta(hours=1)
    event = _build_event(
        "event-6",
        "Team sync",
        in_1_hour,
        description="Weekly check-in",
        attendees=[
            {"email": "alice@example.com", "displayName": "Alice"},
            {"email": "bob@example.com", "displayName": "Bob"},
            {"email": "user@example.com", "self": True, "displayName": "Me"},
        ],
        organizer={"email": "user@example.com", "self": True},
    )

    _setup_gcal_mocks(mock_build, mock_auth, [event])

    provider = CalendarProvider(awareness_config)
    items = await provider.fetch_items()

    assert len(items) == 1
    item = items[0]
    assert item.metadata["is_organizer"] is True
    assert item.metadata["response_status"] == "accepted"
    attendees = item.metadata["attendees"]
    assert len(attendees) == 3
    assert any("Alice" in a for a in attendees)


# ---------------------------------------------------------------------------
# fetch_items() - multi-calendar
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_fetch_multiple_calendars(mock_build, mock_auth) -> None:
    """Events from multiple calendars are merged."""
    config = AwarenessConfig(
        enabled=True,
        providers=["gcal"],
        gcal_calendars=["primary", "work@example.com"],
        gcal_lookahead_hours=36,
    )

    from sova.awareness.providers.gcal import CalendarProvider

    mock_creds = MagicMock()
    mock_creds.valid = True
    mock_creds.scopes = ["https://www.googleapis.com/auth/calendar.readonly"]
    mock_auth.return_value = mock_creds

    in_1_hour = datetime.now() + timedelta(hours=1)
    event1 = _build_event("event-1", "Personal meeting", in_1_hour, description="Lunch")
    event2 = _build_event("event-2", "Work meeting", in_1_hour, description="Sprint planning")

    mock_service = MagicMock()
    mock_events = MagicMock()

    def mock_list_fn(**kwargs):
        mock_result = MagicMock()
        calendar_id = kwargs.get("calendarId", "")
        if calendar_id == "primary":
            mock_result.execute.return_value = {"items": [event1]}
        elif calendar_id == "work@example.com":
            mock_result.execute.return_value = {"items": [event2]}
        else:
            mock_result.execute.return_value = {"items": []}
        return mock_result

    mock_events.list.side_effect = mock_list_fn
    mock_service.events.return_value = mock_events
    mock_build.return_value = mock_service

    provider = CalendarProvider(config)
    items = await provider.fetch_items()

    assert len(items) == 2
    titles = {item.title for item in items}
    assert "Personal meeting" in titles
    assert "Work meeting" in titles


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_fetch_duplicate_event_deduped(mock_build, mock_auth) -> None:
    """Same event appearing in multiple calendars is deduped by ID."""
    config = AwarenessConfig(
        enabled=True,
        providers=["gcal"],
        gcal_calendars=["primary", "shared@example.com"],
        gcal_lookahead_hours=36,
    )

    from sova.awareness.providers.gcal import CalendarProvider

    in_1_hour = datetime.now() + timedelta(hours=1)
    event = _build_event("duplicate-id", "Shared meeting", in_1_hour, description="Agenda")

    _setup_gcal_mocks(mock_build, mock_auth, [event])

    provider = CalendarProvider(config)
    items = await provider.fetch_items()

    assert len(items) == 1


# ---------------------------------------------------------------------------
# fetch_items() - edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_fetch_no_events(mock_build, mock_auth, awareness_config: AwarenessConfig) -> None:
    """Empty calendar returns empty list."""
    from sova.awareness.providers.gcal import CalendarProvider

    _setup_gcal_mocks(mock_build, mock_auth, [])

    provider = CalendarProvider(awareness_config)
    items = await provider.fetch_items()

    assert items == []


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_fetch_api_error_returns_empty(mock_build, mock_auth, awareness_config: AwarenessConfig) -> None:
    """Google API errors return empty list."""
    from sova.awareness.providers.gcal import CalendarProvider

    mock_creds = MagicMock()
    mock_creds.valid = True
    mock_creds.scopes = ["https://www.googleapis.com/auth/calendar.readonly"]
    mock_auth.return_value = mock_creds

    mock_service = MagicMock()
    mock_events = MagicMock()
    mock_list = MagicMock()
    mock_list.execute.side_effect = Exception("API quota exceeded")
    mock_events.list.return_value = mock_list
    mock_service.events.return_value = mock_events
    mock_build.return_value = mock_service

    provider = CalendarProvider(awareness_config)
    items = await provider.fetch_items()

    assert items == []


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.build")
@patch("sova.awareness.providers.gcal.authenticate_google")
async def test_fetch_not_configured_returns_empty(mock_auth, _mock_build, awareness_config: AwarenessConfig) -> None:
    """fetch_items returns empty when not configured."""
    from sova.awareness.providers.gcal import CalendarProvider

    mock_auth.side_effect = FileNotFoundError("Credentials not found")

    provider = CalendarProvider(awareness_config)
    items = await provider.fetch_items()

    assert items == []


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_fetch_past_event_filtered_out(mock_build, mock_auth, awareness_config: AwarenessConfig) -> None:
    """Events that already started are filtered out, not shown as high-urgency."""
    from sova.awareness.providers.gcal import CalendarProvider

    started_1_hour_ago = datetime.now() - timedelta(hours=1)
    event = _build_event(
        "past-event",
        "Meeting already started",
        started_1_hour_ago,
        description="This meeting is in progress",
        attendees=[{"email": "user@example.com", "self": True}],
    )

    _setup_gcal_mocks(mock_build, mock_auth, [event])

    provider = CalendarProvider(awareness_config)
    items = await provider.fetch_items(since=datetime.now() - timedelta(hours=2))

    assert items == []


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_fetch_timezone_conversion_explicit_offset(
    mock_build, mock_auth, awareness_config: AwarenessConfig
) -> None:
    """Event with an explicit non-local timezone offset is correctly converted for categorization."""
    from datetime import timezone as tz

    from sova.awareness.providers.gcal import CalendarProvider

    mock_creds = MagicMock()
    mock_auth.return_value = mock_creds

    # Create an event 20 minutes from now, expressed in UTC
    now_utc = datetime.now(tz.utc)
    event_time_utc = now_utc + timedelta(minutes=20)
    utc_iso = event_time_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00")

    event = {
        "id": "tz-explicit-event",
        "summary": "UTC timezone meeting",
        "htmlLink": "https://calendar.google.com/event?eid=tz-explicit",
        "start": {"dateTime": utc_iso},
        "end": {"dateTime": (event_time_utc + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S+00:00")},
        "description": "Test agenda",
        "attendees": [{"email": "user@example.com", "self": True, "responseStatus": "accepted"}],
    }

    _setup_gcal_mocks(mock_build, mock_auth, [event])

    provider = CalendarProvider(awareness_config)
    items = await provider.fetch_items()

    assert len(items) == 1
    item = items[0]
    assert item.category == ItemCategory.NEEDS_ATTENTION
    assert item.urgency == 2


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_fetch_multi_page_pagination(mock_build, mock_auth, awareness_config: AwarenessConfig) -> None:
    """Pagination via nextPageToken fetches all pages."""
    from sova.awareness.providers.gcal import CalendarProvider

    mock_creds = MagicMock()
    mock_creds.valid = True
    mock_creds.scopes = ["https://www.googleapis.com/auth/calendar.readonly"]
    mock_auth.return_value = mock_creds

    in_1_hour = datetime.now() + timedelta(hours=1)
    event1 = _build_event("page1-event", "Page 1 meeting", in_1_hour, description="First page")
    event2 = _build_event("page2-event", "Page 2 meeting", in_1_hour, description="Second page")

    mock_service = MagicMock()
    mock_events = MagicMock()
    call_count = 0

    def mock_list_fn(**kwargs):
        nonlocal call_count
        mock_result = MagicMock()
        if kwargs.get("pageToken") == "token-page-2":
            mock_result.execute.return_value = {"items": [event2]}
        else:
            mock_result.execute.return_value = {"items": [event1], "nextPageToken": "token-page-2"}
        call_count += 1
        return mock_result

    mock_events.list.side_effect = mock_list_fn
    mock_service.events.return_value = mock_events
    mock_build.return_value = mock_service

    provider = CalendarProvider(awareness_config)
    items = await provider.fetch_items()

    assert len(items) == 2
    titles = {item.title for item in items}
    assert "Page 1 meeting" in titles
    assert "Page 2 meeting" in titles
    assert call_count == 2


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_fetch_naive_since_interpreted_as_local(mock_build, mock_auth, awareness_config: AwarenessConfig) -> None:
    """Naive since datetime is interpreted as local time, not replaced with now_utc."""
    from sova.awareness.providers.gcal import CalendarProvider

    in_1_hour = datetime.now() + timedelta(hours=1)
    event = _build_event("event-since", "Meeting", in_1_hour, description="Test")
    _setup_gcal_mocks(mock_build, mock_auth, [event])

    naive_since = datetime.now() - timedelta(hours=2)
    provider = CalendarProvider(awareness_config)
    await provider.fetch_items(since=naive_since)

    mock_service = mock_build.return_value
    call_kwargs = mock_service.events().list.call_args
    time_min_used = call_kwargs.kwargs.get("timeMin") or call_kwargs[1].get("timeMin")
    assert time_min_used is not None
    parsed = datetime.fromisoformat(time_min_used)
    assert abs((parsed - naive_since.astimezone()).total_seconds()) < 2
