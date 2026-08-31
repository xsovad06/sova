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
    recurring_event_id: str | None = None,
    status: str | None = None,
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

    if recurring_event_id is not None:
        event["recurringEventId"] = recurring_event_id

    if status is not None:
        event["status"] = status

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


# ---------------------------------------------------------------------------
# Recurring event processing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_recurring_events_collapsed_into_representative(
    mock_build, mock_auth, awareness_config: AwarenessConfig
) -> None:
    """Multiple unchanged instances of a recurring event collapse into one item."""
    from sova.awareness.providers.gcal import CalendarProvider

    in_1h = datetime.now() + timedelta(hours=1)
    in_25h = datetime.now() + timedelta(hours=25)
    events = [
        _build_event(
            "standup_20260831",
            "Daily Standup",
            in_1h,
            description="Team sync",
            recurring_event_id="standup_base",
        ),
        _build_event(
            "standup_20260901",
            "Daily Standup",
            in_25h,
            description="Team sync",
            recurring_event_id="standup_base",
        ),
    ]
    _setup_gcal_mocks(mock_build, mock_auth, events)

    provider = CalendarProvider(awareness_config)
    items = await provider.fetch_items()

    assert len(items) == 1
    item = items[0]
    assert item.occurrence_count == 2
    assert item.title == "Daily Standup"
    assert item.metadata.get("recurring_event_id") == "standup_base"


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_cancelled_recurring_instance_is_exception(
    mock_build, mock_auth, awareness_config: AwarenessConfig
) -> None:
    """Cancelled recurring instance is surfaced as urgency=2 exception."""
    from sova.awareness.providers.gcal import CalendarProvider

    in_1h = datetime.now() + timedelta(hours=1)
    in_25h = datetime.now() + timedelta(hours=25)
    events = [
        _build_event(
            "standup_20260831",
            "Daily Standup",
            in_1h,
            description="Team sync",
            recurring_event_id="standup_base",
        ),
        _build_event(
            "standup_20260901",
            "Daily Standup",
            in_25h,
            description="Team sync",
            recurring_event_id="standup_base",
            status="cancelled",
        ),
    ]
    _setup_gcal_mocks(mock_build, mock_auth, events)

    provider = CalendarProvider(awareness_config)
    items = await provider.fetch_items()

    assert len(items) == 2
    exceptions = [i for i in items if i.metadata.get("is_recurring_exception")]
    unchanged = [i for i in items if not i.metadata.get("is_recurring_exception")]

    assert len(exceptions) == 1
    assert exceptions[0].urgency == 2
    assert exceptions[0].category == ItemCategory.NEEDS_ATTENTION
    assert "cancelled" in exceptions[0].action_hint.lower()

    assert len(unchanged) == 1
    assert unchanged[0].occurrence_count == 1


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_changed_attendees_is_exception(mock_build, mock_auth, awareness_config: AwarenessConfig) -> None:
    """Recurring instance with different attendee count is an exception."""
    from sova.awareness.providers.gcal import CalendarProvider

    in_1h = datetime.now() + timedelta(hours=1)
    in_25h = datetime.now() + timedelta(hours=25)
    events = [
        _build_event(
            "sync_20260831",
            "Team Sync",
            in_1h,
            description="Weekly sync",
            attendees=[{"email": "a@example.com"}, {"email": "b@example.com"}],
            recurring_event_id="sync_base",
        ),
        _build_event(
            "sync_20260901",
            "Team Sync",
            in_25h,
            description="Weekly sync",
            attendees=[{"email": "a@example.com"}, {"email": "b@example.com"}, {"email": "c@example.com"}],
            recurring_event_id="sync_base",
        ),
    ]
    _setup_gcal_mocks(mock_build, mock_auth, events)

    provider = CalendarProvider(awareness_config)
    items = await provider.fetch_items()

    assert len(items) == 2
    exceptions = [i for i in items if i.metadata.get("is_recurring_exception")]
    assert len(exceptions) == 1
    assert exceptions[0].urgency == 2
    assert "attendees" in exceptions[0].action_hint.lower()


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_changed_title_is_exception(mock_build, mock_auth, awareness_config: AwarenessConfig) -> None:
    """Recurring instance with different title is an exception."""
    from sova.awareness.providers.gcal import CalendarProvider

    in_1h = datetime.now() + timedelta(hours=1)
    in_25h = datetime.now() + timedelta(hours=25)
    events = [
        _build_event(
            "sync_20260831",
            "Team Sync",
            in_1h,
            description="Weekly sync",
            recurring_event_id="sync_base",
        ),
        _build_event(
            "sync_20260901",
            "Team Sync RENAMED",
            in_25h,
            description="Weekly sync",
            recurring_event_id="sync_base",
        ),
    ]
    _setup_gcal_mocks(mock_build, mock_auth, events)

    provider = CalendarProvider(awareness_config)
    items = await provider.fetch_items()

    assert len(items) == 2
    exceptions = [i for i in items if i.metadata.get("is_recurring_exception")]
    assert len(exceptions) == 1
    assert exceptions[0].urgency == 2
    assert "title" in exceptions[0].action_hint.lower()


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_changed_description_is_exception(mock_build, mock_auth, awareness_config: AwarenessConfig) -> None:
    """Recurring instance with different description is an exception."""
    from sova.awareness.providers.gcal import CalendarProvider

    in_1h = datetime.now() + timedelta(hours=1)
    in_25h = datetime.now() + timedelta(hours=25)
    events = [
        _build_event(
            "retro_20260831",
            "Sprint Retro",
            in_1h,
            description="Standard retro format",
            recurring_event_id="retro_base",
        ),
        _build_event(
            "retro_20260901",
            "Sprint Retro",
            in_25h,
            description="Special format: hackathon debrief",
            recurring_event_id="retro_base",
        ),
    ]
    _setup_gcal_mocks(mock_build, mock_auth, events)

    provider = CalendarProvider(awareness_config)
    items = await provider.fetch_items()

    assert len(items) == 2
    exceptions = [i for i in items if i.metadata.get("is_recurring_exception")]
    assert len(exceptions) == 1
    assert "description" in exceptions[0].action_hint.lower()


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_changed_location_is_exception(mock_build, mock_auth, awareness_config: AwarenessConfig) -> None:
    """Recurring instance with different location is an exception."""
    from sova.awareness.providers.gcal import CalendarProvider

    in_1h = datetime.now() + timedelta(hours=1)
    in_25h = datetime.now() + timedelta(hours=25)
    events = [
        _build_event(
            "meet_20260831",
            "Standup",
            in_1h,
            description="Sync",
            location="Room A",
            recurring_event_id="meet_base",
        ),
        _build_event(
            "meet_20260901",
            "Standup",
            in_25h,
            description="Sync",
            location="Room B",
            recurring_event_id="meet_base",
        ),
    ]
    _setup_gcal_mocks(mock_build, mock_auth, events)

    provider = CalendarProvider(awareness_config)
    items = await provider.fetch_items()

    assert len(items) == 2
    exceptions = [i for i in items if i.metadata.get("is_recurring_exception")]
    assert len(exceptions) == 1
    assert "location" in exceptions[0].action_hint.lower()


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_rescheduled_recurring_instance_is_exception(
    mock_build, mock_auth, awareness_config: AwarenessConfig
) -> None:
    """Rescheduled recurring instance (time changed) is surfaced as exception."""
    from sova.awareness.providers.gcal import CalendarProvider

    in_1h = datetime.now() + timedelta(hours=1)
    in_25h = datetime.now() + timedelta(hours=25)
    base_event = _build_event(
        "standup_day1",
        "Standup",
        in_1h,
        description="Daily sync",
        recurring_event_id="standup_base",
    )
    rescheduled = _build_event(
        "standup_day2",
        "Standup",
        in_25h,
        description="Daily sync",
        recurring_event_id="standup_base",
    )
    rescheduled["originalStartTime"] = {"dateTime": (in_25h - timedelta(hours=2)).isoformat()}

    _setup_gcal_mocks(mock_build, mock_auth, [base_event, rescheduled])

    provider = CalendarProvider(awareness_config)
    items = await provider.fetch_items()

    assert len(items) == 2
    exceptions = [i for i in items if i.metadata.get("is_recurring_exception")]
    assert len(exceptions) == 1
    assert "time" in exceptions[0].action_hint.lower()
    assert exceptions[0].urgency == 2


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_one_off_events_pass_through(mock_build, mock_auth, awareness_config: AwarenessConfig) -> None:
    """Non-recurring events are not affected by recurring processing."""
    from sova.awareness.providers.gcal import CalendarProvider

    in_1h = datetime.now() + timedelta(hours=1)
    events = [
        _build_event("one-off-1", "Lunch with mentor", in_1h, description="Career chat"),
    ]
    _setup_gcal_mocks(mock_build, mock_auth, events)

    provider = CalendarProvider(awareness_config)
    items = await provider.fetch_items()

    assert len(items) == 1
    item = items[0]
    assert item.occurrence_count == 0
    assert "recurring_event_id" not in item.metadata


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_single_recurring_instance(mock_build, mock_auth, awareness_config: AwarenessConfig) -> None:
    """Single recurring instance in window has occurrence_count=1."""
    from sova.awareness.providers.gcal import CalendarProvider

    in_1h = datetime.now() + timedelta(hours=1)
    events = [
        _build_event(
            "weekly_20260831",
            "Weekly Planning",
            in_1h,
            description="Sprint planning",
            recurring_event_id="weekly_base",
        ),
    ]
    _setup_gcal_mocks(mock_build, mock_auth, events)

    provider = CalendarProvider(awareness_config)
    items = await provider.fetch_items()

    assert len(items) == 1
    assert items[0].occurrence_count == 1
    assert items[0].metadata.get("recurring_event_id") == "weekly_base"


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_mix_of_recurring_and_one_off(mock_build, mock_auth, awareness_config: AwarenessConfig) -> None:
    """Mix of recurring and one-off events are processed correctly."""
    from sova.awareness.providers.gcal import CalendarProvider

    in_1h = datetime.now() + timedelta(hours=1)
    in_2h = datetime.now() + timedelta(hours=2)
    in_25h = datetime.now() + timedelta(hours=25)
    events = [
        _build_event(
            "standup_day1",
            "Standup",
            in_1h,
            description="Daily sync",
            recurring_event_id="standup_base",
        ),
        _build_event(
            "standup_day2",
            "Standup",
            in_25h,
            description="Daily sync",
            recurring_event_id="standup_base",
        ),
        _build_event("interview", "Interview: candidate A", in_2h, description="Technical round"),
    ]
    _setup_gcal_mocks(mock_build, mock_auth, events)

    provider = CalendarProvider(awareness_config)
    items = await provider.fetch_items()

    assert len(items) == 2
    recurring = [i for i in items if i.occurrence_count > 0]
    one_off = [i for i in items if i.occurrence_count == 0]
    assert len(recurring) == 1
    assert recurring[0].occurrence_count == 2
    assert len(one_off) == 1
    assert one_off[0].title == "Interview: candidate A"


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_all_day_recurring_events_grouped(mock_build, mock_auth, awareness_config: AwarenessConfig) -> None:
    """All-day recurring events are grouped like timed events."""
    from sova.awareness.providers.gcal import CalendarProvider

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)
    events = [
        _build_event(
            "holiday_day1",
            "Company Holiday",
            today,
            all_day=True,
            recurring_event_id="holiday_base",
        ),
        _build_event(
            "holiday_day2",
            "Company Holiday",
            tomorrow,
            all_day=True,
            recurring_event_id="holiday_base",
        ),
    ]
    _setup_gcal_mocks(mock_build, mock_auth, events)

    provider = CalendarProvider(awareness_config)
    items = await provider.fetch_items()

    assert len(items) == 1
    assert items[0].occurrence_count == 2
    assert items[0].category == ItemCategory.INFORMATIONAL


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_cancelled_past_recurring_instance_surfaced(
    mock_build, mock_auth, awareness_config: AwarenessConfig
) -> None:
    """Cancelled recurring instance in the past is surfaced as exception."""
    from sova.awareness.providers.gcal import CalendarProvider

    past_1h = datetime.now() - timedelta(hours=1)
    in_23h = datetime.now() + timedelta(hours=23)
    events = [
        _build_event(
            "standup_past",
            "Daily Standup",
            past_1h,
            description="Team sync",
            recurring_event_id="standup_base",
            status="cancelled",
        ),
        _build_event(
            "standup_tomorrow",
            "Daily Standup",
            in_23h,
            description="Team sync",
            recurring_event_id="standup_base",
        ),
    ]
    _setup_gcal_mocks(mock_build, mock_auth, events)

    provider = CalendarProvider(awareness_config)
    items = await provider.fetch_items(since=datetime.now() - timedelta(hours=2))

    cancelled_items = [i for i in items if i.metadata.get("is_recurring_exception")]
    assert len(cancelled_items) == 1
    assert cancelled_items[0].urgency == 2
    assert "(cancelled)" in cancelled_items[0].title


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_all_instances_cancelled_surfaced_as_exceptions(
    mock_build, mock_auth, awareness_config: AwarenessConfig
) -> None:
    """All instances of a recurring event cancelled are surfaced as individual exceptions."""
    from sova.awareness.providers.gcal import CalendarProvider

    in_1h = datetime.now() + timedelta(hours=1)
    in_25h = datetime.now() + timedelta(hours=25)
    in_49h = datetime.now() + timedelta(hours=49)
    events = [
        _build_event(
            "standup_day1",
            "Daily Standup",
            in_1h,
            description="Team sync",
            recurring_event_id="standup_base",
            status="cancelled",
        ),
        _build_event(
            "standup_day2",
            "Daily Standup",
            in_25h,
            description="Team sync",
            recurring_event_id="standup_base",
            status="cancelled",
        ),
        _build_event(
            "standup_day3",
            "Daily Standup",
            in_49h,
            description="Team sync",
            recurring_event_id="standup_base",
            status="cancelled",
        ),
    ]
    _setup_gcal_mocks(mock_build, mock_auth, events)

    provider = CalendarProvider(awareness_config)
    items = await provider.fetch_items()

    assert len(items) == 3
    for item in items:
        assert item.urgency == 2
        assert item.category == ItemCategory.NEEDS_ATTENTION
        assert item.metadata.get("is_recurring_exception") is True
        assert item.metadata.get("recurring_event_id") == "standup_base"
        assert "cancelled" in item.action_hint.lower()


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_event_with_zero_attendees(mock_build, mock_auth, awareness_config: AwarenessConfig) -> None:
    """Recurring event with 0 attendees compares correctly."""
    from sova.awareness.providers.gcal import CalendarProvider

    in_1h = datetime.now() + timedelta(hours=1)
    in_25h = datetime.now() + timedelta(hours=25)
    events = [
        _build_event(
            "focus_day1",
            "Focus Time",
            in_1h,
            description="Deep work block",
            recurring_event_id="focus_base",
        ),
        _build_event(
            "focus_day2",
            "Focus Time",
            in_25h,
            description="Deep work block",
            recurring_event_id="focus_base",
        ),
    ]
    _setup_gcal_mocks(mock_build, mock_auth, events)

    provider = CalendarProvider(awareness_config)
    items = await provider.fetch_items()

    assert len(items) == 1
    assert items[0].occurrence_count == 2


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_recurring_missing_updated_field_treated_as_unchanged(
    mock_build, mock_auth, awareness_config: AwarenessConfig
) -> None:
    """Event with recurringEventId but no updated field is treated as unchanged."""
    from sova.awareness.providers.gcal import CalendarProvider

    in_1h = datetime.now() + timedelta(hours=1)
    in_25h = datetime.now() + timedelta(hours=25)
    events = [
        _build_event(
            "sync_day1",
            "Team Sync",
            in_1h,
            description="Regular sync",
            recurring_event_id="sync_base",
        ),
        _build_event(
            "sync_day2",
            "Team Sync",
            in_25h,
            description="Regular sync",
            recurring_event_id="sync_base",
        ),
    ]
    _setup_gcal_mocks(mock_build, mock_auth, events)

    provider = CalendarProvider(awareness_config)
    items = await provider.fetch_items()

    assert len(items) == 1
    assert items[0].occurrence_count == 2


# ---------------------------------------------------------------------------
# Unit tests for recurring helper functions
# ---------------------------------------------------------------------------


def test_detect_instance_change_cancelled() -> None:
    """Cancelled instance is detected as changed."""
    from sova.awareness.providers.gcal import _detect_instance_change

    instance = {"status": "cancelled"}
    baseline = {"description": "Sync", "attendees": []}
    assert _detect_instance_change(instance, baseline) == "Recurring event cancelled"


def test_detect_instance_change_attendees() -> None:
    """Different attendee set is detected."""
    from sova.awareness.providers.gcal import _detect_instance_change

    instance = {"attendees": [{"email": "a@x.com"}, {"email": "b@x.com"}]}
    baseline = {"attendees": [{"email": "a@x.com"}]}
    assert "attendees" in _detect_instance_change(instance, baseline).lower()


def test_detect_instance_change_attendees_swapped() -> None:
    """Swapped attendees (same count, different people) is detected."""
    from sova.awareness.providers.gcal import _detect_instance_change

    instance = {"attendees": [{"email": "a@x.com"}, {"email": "c@x.com"}]}
    baseline = {"attendees": [{"email": "a@x.com"}, {"email": "b@x.com"}]}
    result = _detect_instance_change(instance, baseline)
    assert "attendees" in result.lower()


def test_detect_instance_change_description() -> None:
    """Different description is detected."""
    from sova.awareness.providers.gcal import _detect_instance_change

    instance = {"description": "New agenda"}
    baseline = {"description": "Old agenda"}
    assert "description" in _detect_instance_change(instance, baseline).lower()


def test_detect_instance_change_location() -> None:
    """Different location is detected."""
    from sova.awareness.providers.gcal import _detect_instance_change

    instance = {"location": "Room B"}
    baseline = {"location": "Room A"}
    assert "location" in _detect_instance_change(instance, baseline).lower()


def test_detect_instance_change_time_rescheduled() -> None:
    """Rescheduled instance (originalStartTime != start) is detected."""
    from sova.awareness.providers.gcal import _detect_instance_change

    instance = {
        "start": {"dateTime": "2026-09-01T11:00:00+02:00"},
        "originalStartTime": {"dateTime": "2026-09-01T09:00:00+02:00"},
    }
    baseline = {"description": "Sync", "attendees": []}
    assert "time" in _detect_instance_change(instance, baseline).lower()


def test_detect_instance_change_no_change() -> None:
    """Identical instances return empty string."""
    from sova.awareness.providers.gcal import _detect_instance_change

    instance = {"description": "Sync", "attendees": [], "location": "Room A"}
    baseline = {"description": "Sync", "attendees": [], "location": "Room A"}
    assert _detect_instance_change(instance, baseline) == ""


def test_detect_instance_change_title() -> None:
    """Different title (summary) is detected."""
    from sova.awareness.providers.gcal import _detect_instance_change

    instance = {"summary": "Renamed Meeting", "description": "Sync", "attendees": []}
    baseline = {"summary": "Original Meeting", "description": "Sync", "attendees": []}
    result = _detect_instance_change(instance, baseline)
    assert "title" in result.lower()


def test_detect_instance_change_multiple_fields() -> None:
    """Multiple changed fields are reported in a combined message."""
    from sova.awareness.providers.gcal import _detect_instance_change

    instance = {"description": "New agenda", "location": "Room B", "attendees": [{"email": "a@x.com"}]}
    baseline = {"description": "Old agenda", "location": "Room A", "attendees": []}
    result = _detect_instance_change(instance, baseline)
    assert "attendees" in result.lower()
    assert "description" in result.lower()
    assert "location" in result.lower()


def test_find_baseline_event_skips_cancelled() -> None:
    """Baseline finder skips cancelled events."""
    from sova.awareness.providers.gcal import _find_baseline_event

    events = [
        {"id": "a", "status": "cancelled"},
        {"id": "b", "status": "confirmed"},
        {"id": "c"},
    ]
    baseline = _find_baseline_event(events)
    assert baseline is not None
    assert baseline["id"] == "b"


def test_find_baseline_event_all_cancelled() -> None:
    """Baseline finder returns None when all events are cancelled."""
    from sova.awareness.providers.gcal import _find_baseline_event

    events = [
        {"id": "a", "status": "cancelled"},
        {"id": "b", "status": "cancelled"},
    ]
    assert _find_baseline_event(events) is None


# ---------------------------------------------------------------------------
# Coverage: is_configured / fetch_items when imports are missing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_configured_returns_false_when_build_is_none(awareness_config: AwarenessConfig) -> None:
    """is_configured returns False when google libraries failed to import."""
    from sova.awareness.providers.gcal import CalendarProvider

    provider = CalendarProvider(awareness_config)
    with (
        patch("sova.awareness.providers.gcal.build", None),
        patch("sova.awareness.providers.gcal.authenticate_google", None),
    ):
        assert await provider.is_configured() is False


@pytest.mark.asyncio
async def test_fetch_items_returns_empty_when_build_is_none(awareness_config: AwarenessConfig) -> None:
    """fetch_items returns [] when google libraries failed to import."""
    from sova.awareness.providers.gcal import CalendarProvider

    provider = CalendarProvider(awareness_config)
    with (
        patch("sova.awareness.providers.gcal.build", None),
        patch("sova.awareness.providers.gcal.authenticate_google", None),
    ):
        items = await provider.fetch_items()
        assert items == []


@pytest.mark.asyncio
async def test_is_configured_invalid_credentials(awareness_config: AwarenessConfig) -> None:
    """is_configured returns False when credentials are not valid."""
    from sova.awareness.providers.gcal import CalendarProvider

    provider = CalendarProvider(awareness_config)
    mock_creds = MagicMock()
    mock_creds.valid = False
    with (
        patch("sova.awareness.providers.gcal.build", MagicMock()),
        patch("sova.awareness.providers.gcal.authenticate_google", return_value=mock_creds),
    ):
        assert await provider.is_configured() is False


@pytest.mark.asyncio
async def test_is_configured_generic_exception(awareness_config: AwarenessConfig) -> None:
    """is_configured returns False on unexpected exceptions."""
    from sova.awareness.providers.gcal import CalendarProvider

    provider = CalendarProvider(awareness_config)
    with (
        patch("sova.awareness.providers.gcal.build", MagicMock()),
        patch("sova.awareness.providers.gcal.authenticate_google", side_effect=RuntimeError("unexpected")),
    ):
        assert await provider.is_configured() is False


@pytest.mark.asyncio
@patch("sova.awareness.providers.gcal.authenticate_google")
@patch("sova.awareness.providers.gcal.build")
async def test_fetch_items_service_build_failure(mock_build, mock_auth, awareness_config: AwarenessConfig) -> None:
    """fetch_items returns [] when service build raises."""
    from sova.awareness.providers.gcal import CalendarProvider

    mock_auth.return_value = MagicMock()
    mock_build.side_effect = RuntimeError("service build failed")

    provider = CalendarProvider(awareness_config)
    items = await provider.fetch_items()
    assert items == []


# ---------------------------------------------------------------------------
# Coverage: _build_item edge cases
# ---------------------------------------------------------------------------


def test_build_item_missing_summary() -> None:
    """_build_item returns None when event has no summary."""
    from sova.awareness.providers.gcal import _build_item

    event = {"id": "ev-1", "start": {"dateTime": "2026-09-01T10:00:00"}}
    assert _build_item(event, datetime(2026, 9, 1, 8, 0)) is None


def test_build_item_missing_id() -> None:
    """_build_item returns None when event has no id."""
    from sova.awareness.providers.gcal import _build_item

    event = {"summary": "Test", "start": {"dateTime": "2026-09-01T10:00:00"}}
    assert _build_item(event, datetime(2026, 9, 1, 8, 0)) is None


def test_build_item_invalid_start_time() -> None:
    """_build_item returns None when start time is unparseable."""
    from sova.awareness.providers.gcal import _build_item

    event = {"id": "ev-1", "summary": "Test", "start": {"dateTime": "not-a-date"}}
    assert _build_item(event, datetime(2026, 9, 1, 8, 0)) is None


# ---------------------------------------------------------------------------
# Coverage: _parse_start_time edge cases
# ---------------------------------------------------------------------------


def test_parse_start_time_invalid_datetime() -> None:
    """_parse_start_time returns None on invalid dateTime string."""
    from sova.awareness.providers.gcal import _parse_start_time

    assert _parse_start_time({"dateTime": "garbage"}) is None


def test_parse_start_time_invalid_date() -> None:
    """_parse_start_time returns None on invalid all-day date string."""
    from sova.awareness.providers.gcal import _parse_start_time

    assert _parse_start_time({"date": "not-a-date"}) is None


def test_parse_start_time_empty_dict() -> None:
    """_parse_start_time returns None when neither dateTime nor date is present."""
    from sova.awareness.providers.gcal import _parse_start_time

    assert _parse_start_time({}) is None


# ---------------------------------------------------------------------------
# Coverage: _collapse_recurring_group edge cases
# ---------------------------------------------------------------------------


def test_collapse_recurring_group_no_baseline() -> None:
    """When all built events are cancelled, they are surfaced as exceptions."""
    from sova.awareness.providers.gcal import _collapse_recurring_group

    now = datetime(2026, 9, 1, 8, 0)
    in_1h = now + timedelta(hours=1)
    in_2h = now + timedelta(hours=2)

    instances = [
        {
            "id": "ev1",
            "summary": "Meeting",
            "start": {"dateTime": in_1h.isoformat()},
            "status": "cancelled",
            "recurringEventId": "base",
        },
        {
            "id": "ev2",
            "summary": "Meeting",
            "start": {"dateTime": in_2h.isoformat()},
            "status": "cancelled",
            "recurringEventId": "base",
        },
    ]

    result = _collapse_recurring_group("base", instances, now)
    assert len(result) == 2
    for item in result:
        assert item.urgency == 2
        assert item.metadata.get("is_recurring_exception") is True
        assert item.metadata.get("recurring_event_id") == "base"


def test_collapse_recurring_group_empty_when_all_past() -> None:
    """When all instances are past and produce no items, returns empty."""
    from sova.awareness.providers.gcal import _collapse_recurring_group

    now = datetime(2026, 9, 1, 12, 0)
    past_1h = now - timedelta(hours=1)
    past_2h = now - timedelta(hours=2)

    instances = [
        {
            "id": "ev1",
            "summary": "Meeting",
            "start": {"dateTime": past_1h.isoformat()},
            "recurringEventId": "base",
        },
        {
            "id": "ev2",
            "summary": "Meeting",
            "start": {"dateTime": past_2h.isoformat()},
            "recurringEventId": "base",
        },
    ]

    result = _collapse_recurring_group("base", instances, now)
    assert result == []


# ---------------------------------------------------------------------------
# Coverage: _build_cancelled_item edge cases
# ---------------------------------------------------------------------------


def test_build_cancelled_item_no_id() -> None:
    """_build_cancelled_item returns None when event has no id."""
    from sova.awareness.providers.gcal import _build_cancelled_item

    event = {"summary": "Meeting", "start": {"dateTime": "2026-09-01T10:00:00"}}
    assert _build_cancelled_item(event, []) is None


def test_build_cancelled_item_summary_from_sibling() -> None:
    """_build_cancelled_item falls back to sibling summary when event has none."""
    from sova.awareness.providers.gcal import _build_cancelled_item

    event = {
        "id": "cancelled-ev",
        "status": "cancelled",
        "start": {"dateTime": "2026-09-01T10:00:00"},
        "recurringEventId": "base",
    }
    siblings = [
        {"id": "sibling-1", "summary": "Daily Standup"},
    ]
    item = _build_cancelled_item(event, siblings)
    assert item is not None
    assert "Daily Standup" in item.title


def test_build_cancelled_item_no_summary_anywhere() -> None:
    """_build_cancelled_item returns None when no summary in event or siblings."""
    from sova.awareness.providers.gcal import _build_cancelled_item

    event = {
        "id": "cancelled-ev",
        "status": "cancelled",
        "start": {"dateTime": "2026-09-01T10:00:00"},
    }
    siblings = [{"id": "sib-1"}]
    assert _build_cancelled_item(event, siblings) is None


def test_build_cancelled_item_no_start_time() -> None:
    """_build_cancelled_item returns None when event has no start or originalStartTime."""
    from sova.awareness.providers.gcal import _build_cancelled_item

    event = {
        "id": "cancelled-ev",
        "summary": "Meeting",
        "status": "cancelled",
    }
    assert _build_cancelled_item(event, []) is None
