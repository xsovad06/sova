"""CalendarProvider: Google Calendar awareness via googleapis.

Queries upcoming calendar events from configured calendars, categorizes
by time-to-start and agenda presence. Requires OAuth2 credentials.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sova.awareness import register_provider
from sova.awareness.base import AwarenessItem, AwarenessProvider, ItemCategory
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    pass

try:
    from googleapiclient.discovery import build

    from sova.awareness.auth.google_oauth import authenticate_google
except ImportError:
    build = None  # type: ignore[assignment,misc]
    authenticate_google = None  # type: ignore[assignment,misc]

_log = get_logger(component="awareness.gcal")

_IMMINENT_THRESHOLD_MINUTES = 30


class CalendarProvider(AwarenessProvider):
    """Awareness provider for Google Calendar via googleapis."""

    name = "gcal"
    display_name = "Google Calendar"

    async def is_configured(self) -> bool:
        """Check if Google Calendar API credentials are available with calendar scope."""
        if build is None or authenticate_google is None:
            _log.debug("gcal.import_missing")
            return False

        try:
            creds = authenticate_google(self.config)
            if not getattr(creds, "valid", False):
                return False

            # Verify credentials have calendar.readonly scope
            scopes = getattr(creds, "scopes", [])
            required_scope = "https://www.googleapis.com/auth/calendar.readonly"
            if required_scope not in scopes:
                _log.debug("gcal.missing_calendar_scope", scopes=scopes)
                return False

            return True
        except (FileNotFoundError, ImportError):
            _log.debug("gcal.credentials_unavailable", exc_info=True)
            return False
        except Exception:
            _log.warning("gcal.auth_check_failed", exc_info=True)
            return False

    async def fetch_items(
        self,
        since: datetime | None = None,
    ) -> list[AwarenessItem]:
        """Fetch calendar events from configured calendars and categorize.

        Args:
            since: If provided, used as timeMin (event start time filter).
                   Calendar API doesn't support filtering by modification time.
        """
        if not await self.is_configured():
            return []

        try:
            creds = authenticate_google(self.config)
        except Exception:
            _log.warning("gcal.auth_failed", exc_info=True)
            return []

        try:
            service = build("calendar", "v3", credentials=creds)
        except Exception:
            _log.warning("gcal.service_build_failed", exc_info=True)
            return []

        now = datetime.now()
        now_utc = datetime.now(timezone.utc)
        if since is not None:
            if since.tzinfo is None:
                since_utc = since.astimezone().astimezone(timezone.utc)
            else:
                since_utc = since.astimezone(timezone.utc)
        else:
            since_utc = now_utc
        time_min = since_utc.isoformat()
        time_max = (now_utc + timedelta(hours=self.config.gcal_lookahead_hours)).isoformat()

        all_events: dict[str, dict] = {}

        for calendar_id in self.config.gcal_calendars:
            try:
                page_token: str | None = None
                while True:
                    request_kwargs: dict = {
                        "calendarId": calendar_id,
                        "timeMin": time_min,
                        "timeMax": time_max,
                        "singleEvents": True,
                        "orderBy": "startTime",
                        "maxResults": 250,
                    }
                    if page_token:
                        request_kwargs["pageToken"] = page_token

                    events_result = service.events().list(**request_kwargs).execute()
                    events = events_result.get("items", [])

                    for event in events:
                        event_id = event.get("id")
                        if event_id and event_id not in all_events:
                            all_events[event_id] = event

                    page_token = events_result.get("nextPageToken")
                    if not page_token:
                        break

            except Exception:
                _log.warning("gcal.calendar_fetch_failed", calendar=calendar_id, exc_info=True)
                continue

        items: list[AwarenessItem] = []
        for event in all_events.values():
            item = _build_item(event, now)
            if item:
                items.append(item)

        return items


def _build_item(event: dict, now: datetime) -> AwarenessItem | None:
    """Convert a Google Calendar event into an AwarenessItem."""
    event_id: str | None = event.get("id")
    summary: str | None = event.get("summary")
    html_link: str | None = event.get("htmlLink")

    if not event_id or not summary:
        return None

    start = event.get("start", {})
    start_dt = _parse_start_time(start)
    if start_dt is None:
        _log.debug("gcal.invalid_start_time", event_id=event_id)
        return None

    is_all_day = "date" in start

    # Check if user declined
    if _is_declined(event):
        return None

    description = event.get("description", "").strip()
    has_agenda = bool(description)

    result = _categorize(start_dt, now, has_agenda, is_all_day)
    if result is None:
        return None
    category, urgency, action_hint = result

    attendees = event.get("attendees", [])
    attendee_names = _extract_attendees(attendees)

    organizer = event.get("organizer", {})
    is_organizer = organizer.get("self", False)

    response_status = _get_response_status(attendees)

    location = event.get("location", "")

    body_parts = []
    if location:
        body_parts.append(f"Location: {location}")
    if description:
        body_parts.append(description)

    return AwarenessItem(
        id=f"gcal:{event_id}",
        provider="gcal",
        category=category,
        title=summary,
        body="\n".join(body_parts),
        source_url=html_link or "",
        timestamp=start_dt,
        urgency=urgency,
        action_hint=action_hint,
        metadata={
            "attendees": attendee_names,
            "location": location,
            "calendar_link": html_link or "",
            "is_organizer": is_organizer,
            "response_status": response_status,
        },
    )


def _parse_start_time(start: dict) -> datetime | None:
    """Parse start time from event, handling both dateTime and date (all-day) formats.

    Converts timezone-aware datetimes to local time before stripping timezone info,
    so time-to-start calculations in _categorize() are correct.
    """
    if "dateTime" in start:
        try:
            dt_str = start["dateTime"]
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            # Convert to local time, then strip timezone (matches reminders.py pattern)
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
            return dt
        except (ValueError, AttributeError):
            return None
    elif "date" in start:
        try:
            date_str = start["date"]
            return datetime.strptime(date_str, "%Y-%m-%d")
        except (ValueError, AttributeError):
            return None
    return None


def _is_declined(event: dict) -> bool:
    """Check if the user declined this event."""
    attendees = event.get("attendees", [])
    for attendee in attendees:
        if attendee.get("self"):
            return attendee.get("responseStatus") == "declined"
    return False


def _get_response_status(attendees: list[dict]) -> str:
    """Get the current user's response status."""
    for attendee in attendees:
        if attendee.get("self"):
            return attendee.get("responseStatus", "needsAction")
    return "needsAction"


def _extract_attendees(attendees: list[dict]) -> list[str]:
    """Extract attendee names or emails."""
    names = []
    for attendee in attendees:
        name = attendee.get("displayName") or attendee.get("email", "")
        if name:
            names.append(name)
    return names


def _categorize(
    start_dt: datetime,
    now: datetime,
    has_agenda: bool,
    is_all_day: bool,
) -> tuple[ItemCategory, int, str] | None:
    """Categorize a calendar event by time-to-start and agenda presence.

    Returns (category, urgency, action_hint) or None if the event should be filtered.
    """
    if is_all_day:
        return ItemCategory.INFORMATIONAL, 0, "All-day event"

    time_to_start = (start_dt - now).total_seconds() / 60

    if time_to_start < 0:
        return None

    # Imminent meetings (within 30 minutes)
    if time_to_start <= _IMMINENT_THRESHOLD_MINUTES:
        return ItemCategory.NEEDS_ATTENTION, 2, "Meeting starting soon"

    # Meetings with no agenda
    if not has_agenda:
        return ItemCategory.NEEDS_ATTENTION, 1, "Meeting with no agenda"

    # Meetings later today or tomorrow
    return ItemCategory.INFORMATIONAL, 0, "Upcoming meeting"


if build is not None:
    register_provider("gcal", CalendarProvider)
