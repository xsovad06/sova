"""CalendarProvider: Google Calendar awareness via googleapis.

Queries upcoming calendar events from configured calendars, categorizes
by time-to-start and agenda presence. Requires OAuth2 credentials.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sova.awareness import register_provider
from sova.awareness.base import AwarenessItem, AwarenessProvider, ItemCategory
from sova.utils.logging import get_logger

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
        if build is None or authenticate_google is None:
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

        now_utc = datetime.now(timezone.utc)
        now = now_utc.astimezone().replace(tzinfo=None)
        since_utc = since.astimezone(timezone.utc) if since is not None else now_utc
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

        items = _process_recurring_events(all_events, now)
        _log.info("gcal.fetch_complete", total=len(items))
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
    return [name for a in attendees if (name := a.get("displayName") or a.get("email", ""))]


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


def _process_recurring_events(
    all_events: dict[str, dict],
    now: datetime,
) -> list[AwarenessItem]:
    """Post-process fetched events: collapse recurring, surface exceptions.

    Groups instances by recurringEventId. Unchanged instances collapse into
    a single representative with occurrence_count. Changed or cancelled
    instances are surfaced as exceptions with urgency=2.
    """
    recurring_groups: dict[str, list[dict]] = {}
    one_off: list[dict] = []

    for event in all_events.values():
        recurring_id = event.get("recurringEventId")
        if recurring_id:
            recurring_groups.setdefault(recurring_id, []).append(event)
        else:
            one_off.append(event)

    items: list[AwarenessItem] = []

    for event in one_off:
        item = _build_item(event, now)
        if item:
            items.append(item)

    for recurring_id, instances in recurring_groups.items():
        items.extend(_collapse_recurring_group(recurring_id, instances, now))

    return items


def _collapse_recurring_group(
    recurring_id: str,
    instances: list[dict],
    now: datetime,
) -> list[AwarenessItem]:
    """Collapse a single recurring event group into representative + exceptions."""
    result: list[AwarenessItem] = []

    built: list[tuple[dict, AwarenessItem]] = []
    unbuilt_cancelled: list[dict] = []

    for ev in instances:
        item = _build_item(ev, now)
        if item is not None:
            built.append((ev, item))
        elif ev.get("status") == "cancelled":
            unbuilt_cancelled.append(ev)

    for ev in unbuilt_cancelled:
        exc = _build_cancelled_item(ev, instances)
        if exc:
            result.append(exc)

    if not built:
        return result

    baseline_ev = _find_baseline_event([ev for ev, _ in built])
    if baseline_ev is None:
        for _, item in built:
            item.urgency = 2
            item.category = ItemCategory.NEEDS_ATTENTION
            item.action_hint = "Recurring event cancelled"
            item.metadata["is_recurring_exception"] = True
            item.metadata["recurring_event_id"] = recurring_id
            result.append(item)
        return result

    exceptions: list[AwarenessItem] = []
    unchanged: list[AwarenessItem] = []

    for ev, item in built:
        reason = _detect_instance_change(ev, baseline_ev)
        if reason:
            item.urgency = 2
            item.category = ItemCategory.NEEDS_ATTENTION
            item.action_hint = reason
            item.metadata["is_recurring_exception"] = True
            item.metadata["recurring_event_id"] = recurring_id
            exceptions.append(item)
        else:
            unchanged.append(item)

    result.extend(exceptions)

    if unchanged:
        representative = unchanged[0]
        representative.occurrence_count = len(unchanged)
        representative.metadata["recurring_event_id"] = recurring_id
        result.append(representative)

    return result


def _find_baseline_event(events: list[dict]) -> dict | None:
    """Find the first non-cancelled event to use as baseline for comparison."""
    return next((ev for ev in events if ev.get("status") != "cancelled"), None)


def _detect_instance_change(instance: dict, baseline: dict) -> str:
    """Detect if a recurring instance differs from the baseline.

    Returns a combined reason string if changed, empty string if unchanged.
    Checks: cancellation, title, time, attendees, description, location.
    """
    if instance.get("status") == "cancelled":
        return "Recurring event cancelled"

    changes: list[str] = []

    inst_summary = instance.get("summary", "").strip()
    base_summary = baseline.get("summary", "").strip()
    if inst_summary and base_summary and inst_summary != base_summary:
        changes.append("title")

    original_start = instance.get("originalStartTime", {})
    current_start = instance.get("start", {})
    orig_time = original_start.get("dateTime") or original_start.get("date")
    curr_time = current_start.get("dateTime") or current_start.get("date")
    if orig_time and curr_time and orig_time != curr_time:
        changes.append("time")

    inst_emails = {a.get("email", "").lower() for a in instance.get("attendees", []) if a.get("email")}
    base_emails = {a.get("email", "").lower() for a in baseline.get("attendees", []) if a.get("email")}
    if inst_emails != base_emails:
        changes.append("attendees")

    inst_desc = instance.get("description", "").strip()
    base_desc = baseline.get("description", "").strip()
    if inst_desc != base_desc:
        changes.append("description")

    inst_loc = instance.get("location", "").strip()
    base_loc = baseline.get("location", "").strip()
    if inst_loc != base_loc:
        changes.append("location")

    if not changes:
        return ""
    return " and ".join(changes).capitalize() + " changed"


def _build_cancelled_item(
    event: dict,
    all_instances: list[dict],
) -> AwarenessItem | None:
    """Build an AwarenessItem for a cancelled recurring instance that _build_item filtered."""
    event_id = event.get("id")
    if not event_id:
        return None

    summary = event.get("summary")
    if not summary:
        summary = next((s.get("summary") for s in all_instances if s.get("summary")), None)
    if not summary:
        return None

    start = event.get("start") or event.get("originalStartTime")
    if not start or not (start.get("dateTime") or start.get("date")):
        return None
    start_dt = _parse_start_time(start)

    return AwarenessItem(
        id=f"gcal:{event_id}",
        provider="gcal",
        category=ItemCategory.NEEDS_ATTENTION,
        title=f"{summary} (cancelled)",
        body="",
        source_url=event.get("htmlLink", ""),
        timestamp=start_dt,
        urgency=2,
        action_hint="Recurring event cancelled",
        metadata={
            "attendees": [],
            "location": event.get("location", ""),
            "calendar_link": event.get("htmlLink", ""),
            "is_organizer": False,
            "response_status": "needsAction",
            "is_recurring_exception": True,
            "recurring_event_id": event.get("recurringEventId", ""),
        },
    )


if build is not None:
    register_provider("gcal", CalendarProvider)
