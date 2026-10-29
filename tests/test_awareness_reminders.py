"""Tests for RemindersProvider (Apple Reminders via JXA)."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from sova.awareness.base import ItemCategory
from sova.awareness.providers.reminders import RemindersProvider, _build_item
from sova.config.models import AwarenessConfig
from sova.utils.shell import ShellResult


@pytest.fixture
def awareness_config() -> AwarenessConfig:
    return AwarenessConfig(
        enabled=True,
        providers=["reminders"],
        reminders_lists=["Reminders"],
    )


@pytest.fixture
def reminders_provider(awareness_config: AwarenessConfig) -> RemindersProvider:
    return RemindersProvider(awareness_config)


# ---------------------------------------------------------------------------
# is_configured()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("sys.platform", "darwin")
@patch("shutil.which", return_value="/usr/bin/osascript")
async def test_is_configured_macos_with_osascript(mock_which, reminders_provider: RemindersProvider) -> None:
    assert await reminders_provider.is_configured() is True
    mock_which.assert_called_once_with("osascript")


@pytest.mark.asyncio
@patch("sys.platform", "darwin")
@patch("shutil.which", return_value=None)
async def test_is_configured_macos_without_osascript(mock_which, reminders_provider: RemindersProvider) -> None:
    assert await reminders_provider.is_configured() is False


@pytest.mark.asyncio
@patch("sys.platform", "linux")
async def test_is_configured_non_macos(reminders_provider: RemindersProvider) -> None:
    assert await reminders_provider.is_configured() is False


# ---------------------------------------------------------------------------
# fetch_items() - categorization
# ---------------------------------------------------------------------------


def _build_jxa_output(reminders: list[dict]) -> str:
    """Build synthetic JXA JSON output."""
    return json.dumps(reminders)


@pytest.mark.asyncio
@patch("sys.platform", "darwin")
@patch("shutil.which", return_value="/usr/bin/osascript")
@patch("sova.awareness.providers.reminders.run")
async def test_fetch_overdue_reminder(mock_run: AsyncMock, _mock_which, reminders_provider: RemindersProvider) -> None:
    yesterday = (datetime.now() - timedelta(days=1)).isoformat()
    jxa_output = _build_jxa_output(
        [
            {
                "id": "reminder-1",
                "name": "Overdue task",
                "dueDate": yesterday,
                "list": "Reminders",
                "notes": "This is overdue",
            }
        ]
    )
    mock_run.return_value = ShellResult(returncode=0, stdout=jxa_output, stderr="")

    items = await reminders_provider.fetch_items()

    assert len(items) == 1
    item = items[0]
    assert item.category == ItemCategory.NEEDS_ATTENTION
    assert item.urgency == 2
    assert item.title == "Overdue task"
    assert item.action_hint == "Overdue reminder"
    assert "Reminders" in item.body


@pytest.mark.asyncio
@patch("sys.platform", "darwin")
@patch("shutil.which", return_value="/usr/bin/osascript")
@patch("sova.awareness.providers.reminders.run")
async def test_fetch_due_today_reminder(
    mock_run: AsyncMock, _mock_which, reminders_provider: RemindersProvider
) -> None:
    today = datetime.now().replace(hour=14, minute=0, second=0, microsecond=0).isoformat()
    jxa_output = _build_jxa_output(
        [
            {
                "id": "reminder-2",
                "name": "Today's task",
                "dueDate": today,
                "list": "Reminders",
                "notes": "",
            }
        ]
    )
    mock_run.return_value = ShellResult(returncode=0, stdout=jxa_output, stderr="")

    items = await reminders_provider.fetch_items()

    assert len(items) == 1
    item = items[0]
    assert item.category == ItemCategory.NEEDS_ATTENTION
    assert item.urgency == 1
    assert item.title == "Today's task"
    assert item.action_hint == "Due today"


@pytest.mark.asyncio
@patch("sys.platform", "darwin")
@patch("shutil.which", return_value="/usr/bin/osascript")
@patch("sova.awareness.providers.reminders.run")
async def test_fetch_due_tomorrow_reminder(
    mock_run: AsyncMock, _mock_which, reminders_provider: RemindersProvider
) -> None:
    tomorrow = (datetime.now() + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0).isoformat()
    jxa_output = _build_jxa_output(
        [
            {
                "id": "reminder-3",
                "name": "Tomorrow's task",
                "dueDate": tomorrow,
                "list": "Reminders",
                "notes": "Not urgent yet",
            }
        ]
    )
    mock_run.return_value = ShellResult(returncode=0, stdout=jxa_output, stderr="")

    items = await reminders_provider.fetch_items()

    assert len(items) == 1
    item = items[0]
    assert item.category == ItemCategory.INFORMATIONAL
    assert item.urgency == 0
    assert item.title == "Tomorrow's task"
    assert item.action_hint == "Due tomorrow"


@pytest.mark.asyncio
@patch("sys.platform", "darwin")
@patch("shutil.which", return_value="/usr/bin/osascript")
@patch("sova.awareness.providers.reminders.run")
async def test_fetch_multiple_reminders(
    mock_run: AsyncMock, _mock_which, reminders_provider: RemindersProvider
) -> None:
    yesterday = (datetime.now() - timedelta(days=1)).isoformat()
    today = datetime.now().replace(hour=14, minute=0, second=0, microsecond=0).isoformat()
    tomorrow = (datetime.now() + timedelta(days=1)).isoformat()

    jxa_output = _build_jxa_output(
        [
            {"id": "r1", "name": "Overdue", "dueDate": yesterday, "list": "Reminders", "notes": ""},
            {"id": "r2", "name": "Today", "dueDate": today, "list": "Reminders", "notes": ""},
            {"id": "r3", "name": "Tomorrow", "dueDate": tomorrow, "list": "Reminders", "notes": ""},
        ]
    )
    mock_run.return_value = ShellResult(returncode=0, stdout=jxa_output, stderr="")

    items = await reminders_provider.fetch_items()

    assert len(items) == 3
    assert items[0].urgency == 2
    assert items[1].urgency == 1
    assert items[2].urgency == 0


# ---------------------------------------------------------------------------
# fetch_items() - edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("sys.platform", "darwin")
@patch("shutil.which", return_value="/usr/bin/osascript")
@patch("sova.awareness.providers.reminders.run")
async def test_fetch_no_reminders(mock_run: AsyncMock, _mock_which, reminders_provider: RemindersProvider) -> None:
    mock_run.return_value = ShellResult(returncode=0, stdout="[]", stderr="")
    items = await reminders_provider.fetch_items()
    assert items == []


@pytest.mark.asyncio
@patch("sys.platform", "darwin")
@patch("shutil.which", return_value="/usr/bin/osascript")
@patch("sova.awareness.providers.reminders.run")
async def test_fetch_malformed_json(mock_run: AsyncMock, _mock_which, reminders_provider: RemindersProvider) -> None:
    mock_run.return_value = ShellResult(returncode=0, stdout="not json", stderr="")
    items = await reminders_provider.fetch_items()
    assert items == []


@pytest.mark.asyncio
@patch("sys.platform", "darwin")
@patch("shutil.which", return_value="/usr/bin/osascript")
@patch("sova.awareness.providers.reminders.run")
async def test_fetch_osascript_failure(mock_run: AsyncMock, _mock_which, reminders_provider: RemindersProvider) -> None:
    mock_run.return_value = ShellResult(returncode=1, stdout="", stderr="execution error: Error: (-1728)")
    items = await reminders_provider.fetch_items()
    assert items == []


@pytest.mark.asyncio
@patch("sys.platform", "darwin")
@patch("shutil.which", return_value="/usr/bin/osascript")
@patch("sova.awareness.providers.reminders.run")
async def test_fetch_reminder_no_due_date(
    mock_run: AsyncMock, _mock_which, reminders_provider: RemindersProvider
) -> None:
    now_iso = datetime.now().isoformat()
    jxa_output = _build_jxa_output(
        [
            {"id": "r1", "name": "No due date", "dueDate": None, "list": "Reminders", "notes": ""},
            {"id": "r2", "name": "Has due date", "dueDate": now_iso, "list": "Reminders", "notes": ""},
        ]
    )
    mock_run.return_value = ShellResult(returncode=0, stdout=jxa_output, stderr="")

    items = await reminders_provider.fetch_items()

    assert len(items) == 1
    assert items[0].title == "Has due date"


@pytest.mark.asyncio
@patch("sys.platform", "darwin")
@patch("shutil.which", return_value="/usr/bin/osascript")
@patch("sova.awareness.providers.reminders.run")
async def test_fetch_reminder_invalid_due_date(
    mock_run: AsyncMock, _mock_which, reminders_provider: RemindersProvider
) -> None:
    jxa_output = _build_jxa_output(
        [
            {"id": "r1", "name": "Invalid date", "dueDate": "not-a-date", "list": "Reminders", "notes": ""},
        ]
    )
    mock_run.return_value = ShellResult(returncode=0, stdout=jxa_output, stderr="")

    items = await reminders_provider.fetch_items()

    assert items == []


@pytest.mark.asyncio
@patch("sys.platform", "darwin")
@patch("shutil.which", return_value="/usr/bin/osascript")
@patch("sova.awareness.providers.reminders.run")
async def test_fetch_multiple_lists(mock_run: AsyncMock, _mock_which) -> None:
    config = AwarenessConfig(
        enabled=True,
        providers=["reminders"],
        reminders_lists=["Work", "Personal"],
    )
    provider = RemindersProvider(config)

    today = datetime.now().isoformat()
    jxa_output = _build_jxa_output(
        [
            {"id": "w1", "name": "Work task", "dueDate": today, "list": "Work", "notes": ""},
            {"id": "p1", "name": "Personal task", "dueDate": today, "list": "Personal", "notes": ""},
        ]
    )
    mock_run.return_value = ShellResult(returncode=0, stdout=jxa_output, stderr="")

    items = await provider.fetch_items()

    assert len(items) == 2
    assert any("Work" in item.body for item in items)
    assert any("Personal" in item.body for item in items)

    mock_run.assert_called_once()
    script_arg = mock_run.call_args[0][4]
    assert '"Work"' in script_arg and '"Personal"' in script_arg


@pytest.mark.asyncio
@patch("sys.platform", "darwin")
@patch("shutil.which", return_value="/usr/bin/osascript")
@patch("sova.awareness.providers.reminders.run")
async def test_fetch_utc_z_suffix_converts_to_local(
    mock_run: AsyncMock, _mock_which, reminders_provider: RemindersProvider
) -> None:
    """JXA toISOString() returns UTC with Z suffix; verify local-time conversion."""
    now = datetime.now()
    today_utc_iso = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    jxa_output = _build_jxa_output(
        [{"id": "r-utc", "name": "UTC task", "dueDate": today_utc_iso, "list": "Reminders", "notes": ""}]
    )
    mock_run.return_value = ShellResult(returncode=0, stdout=jxa_output, stderr="")

    items = await reminders_provider.fetch_items()

    assert len(items) == 1
    assert items[0].action_hint == "Due today"
    assert items[0].urgency == 1


@pytest.mark.asyncio
@patch("sys.platform", "darwin")
@patch("shutil.which", return_value="/usr/bin/osascript")
@patch("sova.awareness.providers.reminders.run")
async def test_fetch_reminder_beyond_tomorrow_filtered(
    mock_run: AsyncMock, _mock_which, reminders_provider: RemindersProvider
) -> None:
    next_week = (datetime.now() + timedelta(days=7)).isoformat()
    jxa_output = _build_jxa_output(
        [{"id": "r-future", "name": "Next week task", "dueDate": next_week, "list": "Reminders", "notes": ""}]
    )
    mock_run.return_value = ShellResult(returncode=0, stdout=jxa_output, stderr="")

    items = await reminders_provider.fetch_items()

    assert items == []


@pytest.mark.asyncio
@patch("sys.platform", "darwin")
@patch("shutil.which", return_value="/usr/bin/osascript")
@patch("sova.awareness.providers.reminders.run")
async def test_fetch_reminder_three_days_out_filtered(
    mock_run: AsyncMock, _mock_which, reminders_provider: RemindersProvider
) -> None:
    three_days = (datetime.now() + timedelta(days=3)).isoformat()
    jxa_output = _build_jxa_output(
        [{"id": "r-3d", "name": "In 3 days", "dueDate": three_days, "list": "Reminders", "notes": ""}]
    )
    mock_run.return_value = ShellResult(returncode=0, stdout=jxa_output, stderr="")

    items = await reminders_provider.fetch_items()

    assert items == []


@pytest.mark.asyncio
@patch("sys.platform", "darwin")
@patch("shutil.which", return_value="/usr/bin/osascript")
@patch("sova.awareness.providers.reminders.run")
async def test_fetch_run_exception_returns_empty(
    mock_run: AsyncMock, _mock_which, reminders_provider: RemindersProvider
) -> None:
    mock_run.side_effect = OSError("osascript crashed")

    items = await reminders_provider.fetch_items()

    assert items == []


@pytest.mark.asyncio
@patch("sys.platform", "darwin")
@patch("shutil.which", return_value="/usr/bin/osascript")
@patch("sova.awareness.providers.reminders.run")
async def test_fetch_non_list_json_returns_empty(
    mock_run: AsyncMock, _mock_which, reminders_provider: RemindersProvider
) -> None:
    mock_run.return_value = ShellResult(returncode=0, stdout='{"key": "value"}', stderr="")

    items = await reminders_provider.fetch_items()

    assert items == []


@pytest.mark.asyncio
@patch("sys.platform", "linux")
@patch("sova.awareness.providers.reminders.run")
async def test_fetch_on_non_macos_returns_empty(mock_run: AsyncMock, reminders_provider: RemindersProvider) -> None:
    items = await reminders_provider.fetch_items()
    assert items == []
    mock_run.assert_not_called()


@pytest.fixture
def new_york_local_time(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run the test with the process-local timezone set to America/New_York.

    monkeypatch restores ``TZ`` itself, but the C library only re-reads it on ``tzset()``. The ``finally``
    runs after the monkeypatch context has undone the variable, so even a ``tzset()`` that raises partway
    cannot leave the zone changed for later tests on this xdist worker.
    """
    try:
        with monkeypatch.context() as patch_env:
            patch_env.setenv("TZ", "America/New_York")
            time.tzset()
            yield
    finally:
        time.tzset()


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset() is unavailable on this platform")
@pytest.mark.usefixtures("new_york_local_time")
class TestBuildItemMixedDueDateForms:
    """_build_item() compares aware local datetimes, so a naive and a 'Z' due date must agree.

    The fixed ``now`` is 2026-03-08 12:00 EDT, just after the US spring-forward
    (02:00 local that day), so the local UTC offset is -05:00 before the
    transition and -04:00 after it. Each 'Z' case below falls on a different
    calendar date in UTC than in New York, which is exactly where comparing UTC
    dates (the old naive-compare behaviour) classifies it wrongly.
    """

    NOW = datetime(2026, 3, 8, 12, 0, tzinfo=ZoneInfo("America/New_York"))

    @pytest.mark.parametrize(
        ("due_date", "expected"),
        [
            # naive: local wall-clock time, on either side of the transition
            ("2026-03-07T23:30:00", (ItemCategory.NEEDS_ATTENTION, 2)),
            ("2026-03-08T01:30:00", (ItemCategory.NEEDS_ATTENTION, 1)),
            ("2026-03-08T23:30:00", (ItemCategory.NEEDS_ATTENTION, 1)),
            ("2026-03-09T00:30:00", (ItemCategory.INFORMATIONAL, 0)),
            ("2026-03-10T09:00:00", None),
            # 'Z': 23:59 EST on Mar 7 is already Mar 8 in UTC, but is overdue locally
            ("2026-03-08T04:59:00Z", (ItemCategory.NEEDS_ATTENTION, 2)),
            # 'Z': 23:30 EDT on Mar 8 is already Mar 9 in UTC, but is still today locally
            ("2026-03-09T03:30:00Z", (ItemCategory.NEEDS_ATTENTION, 1)),
            # 'Z': 00:30 EDT on Mar 9
            ("2026-03-09T04:30:00Z", (ItemCategory.INFORMATIONAL, 0)),
            ("2026-03-10T13:00:00Z", None),
        ],
    )
    def test_classification_matches_local_calendar_day(
        self, due_date: str, expected: tuple[ItemCategory, int] | None
    ) -> None:
        item = _build_item({"id": "r1", "name": "Pay rent", "dueDate": due_date}, self.NOW)

        if expected is None:
            assert item is None
        else:
            assert item is not None
            assert (item.category, item.urgency) == expected

    def test_naive_and_z_forms_of_the_same_instant_classify_alike(self) -> None:
        """23:30 local on Mar 8 (EDT) written both ways is the same reminder."""
        naive = _build_item({"id": "n", "name": "x", "dueDate": "2026-03-08T23:30:00"}, self.NOW)
        zulu = _build_item({"id": "z", "name": "x", "dueDate": "2026-03-09T03:30:00Z"}, self.NOW)

        assert naive is not None
        assert zulu is not None
        assert (naive.category, naive.urgency, naive.timestamp) == (zulu.category, zulu.urgency, zulu.timestamp)
