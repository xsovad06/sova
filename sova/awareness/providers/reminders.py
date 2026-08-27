"""RemindersProvider: Apple Reminders awareness via JXA.

Queries incomplete reminders with due dates from configured lists,
categorizes by overdue/today/tomorrow. macOS-only; gracefully skipped
on other platforms.
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timedelta

from sova.awareness import register_provider
from sova.awareness.base import AwarenessItem, AwarenessProvider, ItemCategory
from sova.utils.logging import get_logger
from sova.utils.shell import run

_log = get_logger(component="awareness.reminders")


class RemindersProvider(AwarenessProvider):
    """Awareness provider for Apple Reminders via JXA."""

    name = "reminders"
    display_name = "Apple Reminders"

    async def is_configured(self) -> bool:
        """Check if running on macOS with osascript available."""
        if sys.platform != "darwin":
            _log.debug("reminders.platform_skip", platform=sys.platform)
            return False
        if shutil.which("osascript") is None:
            _log.debug("reminders.osascript_missing")
            return False
        return True

    async def fetch_items(
        self,
        since: datetime | None = None,
    ) -> list[AwarenessItem]:
        """Fetch reminders from configured lists and categorize by due date."""
        if not await self.is_configured():
            return []

        script = _build_jxa_script(self.config.reminders_lists)

        try:
            result = await run("osascript", "-l", "JavaScript", "-e", script)
        except Exception:
            _log.warning("reminders.fetch_failed", exc_info=True)
            return []

        if not result.success:
            _log.warning("reminders.osascript_error", stderr=result.stderr)
            return []

        try:
            reminders = json.loads(result.stdout)
        except json.JSONDecodeError:
            _log.warning("reminders.malformed_json", stdout=result.stdout[:100])
            return []

        if not isinstance(reminders, list):
            _log.warning("reminders.unexpected_format", type=type(reminders).__name__)
            return []

        items: list[AwarenessItem] = []
        now = datetime.now()

        for reminder in reminders:
            item = _build_item(reminder, now)
            if item:
                items.append(item)

        return items


def _build_jxa_script(lists: list[str]) -> str:
    """Build JXA script to query incomplete reminders from specified lists."""
    lists_json = json.dumps(lists)

    script = f"""
const app = Application('Reminders');
const targetLists = {lists_json};
const results = [];

for (const listName of targetLists) {{
    try {{
        const list = app.lists.byName(listName);
        const reminders = list.reminders.whose({{completed: false}});
        for (const reminder of reminders()) {{
            const dueDate = reminder.dueDate();
            if (dueDate) {{
                results.push({{
                    id: reminder.id(),
                    name: reminder.name(),
                    dueDate: dueDate.toISOString(),
                    list: listName,
                    notes: reminder.body() || ''
                }});
            }}
        }}
    }} catch (e) {{
        // List not found or other error - skip
    }}
}}

JSON.stringify(results);
"""
    return script


def _build_item(reminder: dict, now: datetime) -> AwarenessItem | None:
    """Convert a reminder dict into an AwarenessItem."""
    reminder_id: str | None = reminder.get("id")
    name: str | None = reminder.get("name")
    due_date_str: str | None = reminder.get("dueDate")
    list_name: str | None = reminder.get("list")
    notes: str | None = reminder.get("notes")

    if not reminder_id or not name or not due_date_str:
        return None

    try:
        due_date = datetime.fromisoformat(due_date_str.replace("Z", "+00:00"))
        if due_date.tzinfo is not None:
            due_date = due_date.astimezone().replace(tzinfo=None)
    except (ValueError, AttributeError):
        _log.debug("reminders.invalid_date", due_date_str=due_date_str)
        return None

    result = _categorize(due_date, now)
    if result is None:
        return None
    category, urgency, action_hint = result

    body_parts = []
    if list_name:
        body_parts.append(f"List: {list_name}")
    if notes:
        body_parts.append(notes)

    return AwarenessItem(
        id=f"reminder:{reminder_id}",
        provider="reminders",
        category=category,
        title=name,
        body="\n".join(body_parts),
        source_url="",
        timestamp=due_date,
        urgency=urgency,
        action_hint=action_hint,
        metadata={
            "list": list_name or "",
            "notes": notes or "",
        },
    )


def _categorize(due_date: datetime, now: datetime) -> tuple[ItemCategory, int, str] | None:
    """Categorize a reminder by its due date relative to now.

    Returns (category, urgency, action_hint) for overdue/today/tomorrow,
    or None for reminders beyond tomorrow (filtered out).
    """
    now_date = now.date()
    due_date_date = due_date.date()

    if due_date_date < now_date:
        return ItemCategory.NEEDS_ATTENTION, 2, "Overdue reminder"

    if due_date_date == now_date:
        return ItemCategory.NEEDS_ATTENTION, 1, "Due today"

    tomorrow_date = (now + timedelta(days=1)).date()
    if due_date_date == tomorrow_date:
        return ItemCategory.INFORMATIONAL, 0, "Due tomorrow"

    return None


register_provider("reminders", RemindersProvider)
