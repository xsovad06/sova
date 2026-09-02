"""Tests for the briefing CLI command and renderer."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from io import StringIO

import pytest
from rich.console import Console

from sova.awareness.base import AwarenessItem, ItemCategory
from sova.awareness.rendering.cli_renderer import render_briefing_cli
from sova.awareness.rendering.models import Briefing, ProjectPulse, ProviderStatus


@pytest.fixture
def console() -> Console:
    """Create a test console for rendering."""
    return Console(file=StringIO(), width=80, legacy_windows=False)


def test_render_empty_briefing(console: Console) -> None:
    """Empty briefing shows helpful message."""
    briefing = Briefing()

    render_briefing_cli(briefing, console, quiet=False)

    output = console.file.getvalue()
    assert "No awareness providers configured" in output
    # Guidance points to the supported env var / dashboard workflow, not editing a TOML file.
    assert "SOVA_AWARENESS_ENABLED=true" in output
    assert "settings page" in output
    assert "sova.toml" not in output


def test_render_briefing_with_attention_items(console: Console) -> None:
    """Attention items rendered with urgency markers."""
    now = datetime.now()
    briefing = Briefing(
        generated_at=now,
        attention_items=[
            AwarenessItem(
                id="email:1",
                provider="gmail",
                category=ItemCategory.NEEDS_ATTENTION,
                title="Urgent email from boss",
                body="Please review quarterly report",
                urgency=2,
                timestamp=now - timedelta(hours=1),
                action_hint="Reply to email",
            ),
            AwarenessItem(
                id="pr:1",
                provider="pr_status",
                category=ItemCategory.NEEDS_ATTENTION,
                title="Review requested: fix auth",
                urgency=1,
                timestamp=now - timedelta(minutes=30),
            ),
        ],
        provider_statuses=[
            ProviderStatus(name="gmail", ok=True, message="ok", items_fetched=1, fetch_time_ms=120),
            ProviderStatus(name="pr_status", ok=True, message="ok", items_fetched=1, fetch_time_ms=80),
        ],
    )

    render_briefing_cli(briefing, console, quiet=False)

    output = console.file.getvalue()
    assert "Urgent email from boss" in output
    assert "Review requested: fix auth" in output
    assert "2 items" in output


def test_render_briefing_quiet_mode(console: Console) -> None:
    """Quiet mode only shows attention items."""
    now = datetime.now()
    briefing = Briefing(
        generated_at=now,
        attention_items=[
            AwarenessItem(
                id="email:1",
                provider="gmail",
                category=ItemCategory.NEEDS_ATTENTION,
                title="Urgent email",
                urgency=2,
                timestamp=now,
            ),
        ],
        informational_items=[
            AwarenessItem(
                id="email:2",
                provider="gmail",
                category=ItemCategory.INFORMATIONAL,
                title="Newsletter",
                timestamp=now,
            ),
        ],
        schedule=[
            AwarenessItem(
                id="cal:1",
                provider="gcal",
                category=ItemCategory.SCHEDULE,
                title="Team standup",
                timestamp=now + timedelta(hours=1),
            ),
        ],
        provider_statuses=[
            ProviderStatus(name="gmail", ok=True, message="ok", items_fetched=2, fetch_time_ms=120),
        ],
    )

    render_briefing_cli(briefing, console, quiet=True)

    output = console.file.getvalue()
    assert "Urgent email" in output
    assert "Newsletter" not in output
    assert "Team standup" not in output


def test_render_briefing_with_schedule(console: Console) -> None:
    """Schedule items rendered in time order."""
    now = datetime.now()
    briefing = Briefing(
        generated_at=now,
        schedule=[
            AwarenessItem(
                id="cal:1",
                provider="gcal",
                category=ItemCategory.SCHEDULE,
                title="Team standup",
                timestamp=now + timedelta(hours=1),
            ),
            AwarenessItem(
                id="cal:2",
                provider="gcal",
                category=ItemCategory.SCHEDULE,
                title="1:1 with manager",
                timestamp=now + timedelta(hours=2),
            ),
        ],
        provider_statuses=[
            ProviderStatus(name="gcal", ok=True, message="ok", items_fetched=2, fetch_time_ms=150),
        ],
    )

    render_briefing_cli(briefing, console, quiet=False)

    output = console.file.getvalue()
    assert "Team standup" in output
    assert "1:1 with manager" in output


def test_render_briefing_with_informational(console: Console) -> None:
    """Informational items rendered."""
    now = datetime.now()
    briefing = Briefing(
        generated_at=now,
        informational_items=[
            AwarenessItem(
                id="email:1",
                provider="gmail",
                category=ItemCategory.INFORMATIONAL,
                title="Weekly newsletter",
                timestamp=now,
            ),
        ],
        provider_statuses=[
            ProviderStatus(name="gmail", ok=True, message="ok", items_fetched=1, fetch_time_ms=100),
        ],
    )

    render_briefing_cli(briefing, console, quiet=False)

    output = console.file.getvalue()
    assert "Weekly newsletter" in output


def test_render_briefing_with_provider_failures(console: Console) -> None:
    """Provider failures shown in output."""
    briefing = Briefing(
        generated_at=datetime.now(),
        provider_statuses=[
            ProviderStatus(name="gmail", ok=True, message="ok", items_fetched=2, fetch_time_ms=120),
            ProviderStatus(name="gcal", ok=False, message="API unreachable", items_fetched=0, fetch_time_ms=0),
        ],
    )

    render_briefing_cli(briefing, console, quiet=False)

    output = console.file.getvalue()
    assert "gcal" in output
    assert "API unreachable" in output


def test_render_briefing_with_project_pulses(console: Console) -> None:
    """Project pulses rendered in non-quiet mode."""
    briefing = Briefing(
        generated_at=datetime.now(),
        project_pulses=[
            ProjectPulse(project_slug="my-app", open_prs=3, agent_status="running", last_ci="passing"),
            ProjectPulse(project_slug="lib-core", open_prs=0, agent_status="idle", last_ci="failing"),
        ],
        provider_statuses=[
            ProviderStatus(name="sova", ok=True, message="ok", items_fetched=0, fetch_time_ms=50),
        ],
    )

    render_briefing_cli(briefing, console, quiet=False)

    output = console.file.getvalue()
    assert "Project Status" in output
    assert "my-app" in output
    assert "lib-core" in output


def test_render_briefing_project_pulses_hidden_in_quiet(console: Console) -> None:
    """Project pulses not rendered in quiet mode."""
    briefing = Briefing(
        generated_at=datetime.now(),
        project_pulses=[
            ProjectPulse(project_slug="my-app", open_prs=1, agent_status="idle", last_ci="passing"),
        ],
        provider_statuses=[
            ProviderStatus(name="sova", ok=True, message="ok", items_fetched=0, fetch_time_ms=50),
        ],
    )

    render_briefing_cli(briefing, console, quiet=True)

    output = console.file.getvalue()
    assert "Project Status" not in output


def test_parse_since_hours() -> None:
    """Parse '2h' to 2 hours ago."""
    from sova.cli.commands.briefing import _parse_since

    result = _parse_since("2h")
    assert result is not None
    delta = datetime.now() - result
    assert 1.9 * 3600 < delta.total_seconds() < 2.1 * 3600


def test_parse_since_minutes() -> None:
    """Parse '30m' to 30 minutes ago."""
    from sova.cli.commands.briefing import _parse_since

    result = _parse_since("30m")
    assert result is not None
    delta = datetime.now() - result
    assert 29 * 60 < delta.total_seconds() < 31 * 60


def test_parse_since_days() -> None:
    """Parse '1d' to 1 day ago."""
    from sova.cli.commands.briefing import _parse_since

    result = _parse_since("1d")
    assert result is not None
    delta = datetime.now() - result
    assert 0.9 * 86400 < delta.total_seconds() < 1.1 * 86400


def test_parse_since_invalid() -> None:
    """Invalid since format returns None."""
    from sova.cli.commands.briefing import _parse_since

    assert _parse_since("invalid") is None
    assert _parse_since("2x") is None
    assert _parse_since("") is None


def test_json_output_serialization() -> None:
    """JSON output correctly serializes datetime objects."""
    import json

    from sova.cli.commands.briefing import _briefing

    test_console = Console(file=StringIO(), width=80, legacy_windows=False)
    now = datetime.now()

    briefing_obj = Briefing(
        generated_at=now,
        since=now - timedelta(hours=2),
        attention_items=[
            AwarenessItem(
                id="test:1",
                provider="test",
                category=ItemCategory.NEEDS_ATTENTION,
                title="Test item",
                urgency=2,
                timestamp=now - timedelta(minutes=30),
            ),
        ],
        provider_statuses=[
            ProviderStatus(name="test", ok=True, message="ok", items_fetched=1, fetch_time_ms=100),
        ],
    )

    from unittest.mock import AsyncMock, patch

    mock_service = AsyncMock()
    mock_service.generate_briefing.return_value = briefing_obj

    with (
        patch("sova.awareness.briefing.BriefingService", return_value=mock_service),
        patch("sova.awareness.create_providers", return_value=[]),
        patch("sova.config.loader.load_config"),
        patch("sova.cli.commands.briefing.console", test_console),
        patch("typer.echo") as mock_echo,
    ):
        import asyncio

        asyncio.run(
            _briefing(
                project_dir=None,
                since_str=None,
                providers_str=None,
                output_json=True,
                quiet=False,
            )
        )

    mock_echo.assert_called_once()
    output = mock_echo.call_args[0][0]
    parsed = json.loads(output)

    assert "generated_at" in parsed
    assert "since" in parsed
    assert isinstance(parsed["generated_at"], str)
    assert isinstance(parsed["since"], str)

    assert len(parsed["attention_items"]) == 1
    item = parsed["attention_items"][0]
    assert item["title"] == "Test item"
    assert isinstance(item["timestamp"], str)


def test_format_time_ago_future() -> None:
    """Future timestamps show absolute time instead of 'just now'."""
    from sova.awareness.rendering.cli_renderer import _format_time_ago

    result = _format_time_ago(datetime.now() + timedelta(hours=2))
    assert result != "just now"


def test_parse_since_overflow() -> None:
    """Very large duration values return None instead of raising OverflowError."""
    from sova.cli.commands.briefing import _parse_since

    assert _parse_since("10000000d") is None


def test_parse_and_validate_since_valid() -> None:
    """_parse_and_validate_since returns datetime for valid input."""
    from sova.cli.commands.briefing import _parse_and_validate_since

    result = _parse_and_validate_since("2h")
    assert result is not None
    delta = datetime.now() - result
    assert 1.9 * 3600 < delta.total_seconds() < 2.1 * 3600


def test_parse_and_validate_since_none() -> None:
    """_parse_and_validate_since returns None when input is None."""
    from sova.cli.commands.briefing import _parse_and_validate_since

    result = _parse_and_validate_since(None)
    assert result is None


def test_parse_and_validate_since_invalid() -> None:
    """_parse_and_validate_since raises typer.Exit for invalid input."""
    import typer

    from sova.cli.commands.briefing import _parse_and_validate_since

    with pytest.raises(typer.Exit) as exc_info:
        _parse_and_validate_since("invalid")

    assert exc_info.value.exit_code == 1


def test_serialize_briefing_to_json_with_since() -> None:
    """_serialize_briefing_to_json handles briefings with since field."""
    from sova.cli.commands.briefing import _serialize_briefing_to_json

    now = datetime.now()
    briefing = Briefing(
        generated_at=now,
        since=now - timedelta(hours=2),
        attention_items=[],
        informational_items=[],
        schedule=[],
        provider_statuses=[],
    )

    result = _serialize_briefing_to_json(briefing)

    assert "generated_at" in result
    assert "since" in result
    assert isinstance(result["generated_at"], str)
    assert isinstance(result["since"], str)


def test_serialize_briefing_to_json_without_since() -> None:
    """_serialize_briefing_to_json handles briefings without since field."""
    from sova.cli.commands.briefing import _serialize_briefing_to_json

    now = datetime.now()
    briefing = Briefing(
        generated_at=now,
        since=None,
        attention_items=[],
        informational_items=[],
        schedule=[],
        provider_statuses=[],
    )

    result = _serialize_briefing_to_json(briefing)

    assert "generated_at" in result
    assert result.get("since") is None
    assert isinstance(result["generated_at"], str)


def test_serialize_datetime_fields_all_item_types() -> None:
    """_serialize_datetime_fields converts datetimes in all item categories."""
    from sova.cli.commands.briefing import _serialize_datetime_fields

    now = datetime.now()
    data = {
        "attention_items": [{"timestamp": now, "title": "Attention"}],
        "informational_items": [{"timestamp": now, "title": "Info"}],
        "schedule": [{"timestamp": now, "title": "Schedule"}],
    }

    _serialize_datetime_fields(data)

    assert isinstance(data["attention_items"][0]["timestamp"], str)
    assert isinstance(data["informational_items"][0]["timestamp"], str)
    assert isinstance(data["schedule"][0]["timestamp"], str)


def test_serialize_datetime_fields_missing_timestamp() -> None:
    """_serialize_datetime_fields handles items without timestamps."""
    from sova.cli.commands.briefing import _serialize_datetime_fields

    data = {
        "attention_items": [{"title": "No timestamp"}],
        "informational_items": [],
        "schedule": [],
    }

    _serialize_datetime_fields(data)

    assert "timestamp" not in data["attention_items"][0]


def test_serialize_datetime_fields_non_datetime_timestamp() -> None:
    """_serialize_datetime_fields ignores non-datetime timestamp values."""
    from sova.cli.commands.briefing import _serialize_datetime_fields

    data = {
        "attention_items": [{"timestamp": "already-a-string", "title": "Item"}],
    }

    _serialize_datetime_fields(data)

    assert data["attention_items"][0]["timestamp"] == "already-a-string"


def test_briefing_with_providers_str() -> None:
    """_briefing correctly parses and applies providers_str argument."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from sova.cli.commands.briefing import _briefing

    test_console = Console(file=StringIO(), width=80, legacy_windows=False)
    mock_config = MagicMock()
    mock_config.awareness = MagicMock()
    mock_config.awareness.providers = []

    mock_service = AsyncMock()
    mock_service.generate_briefing.return_value = Briefing(
        generated_at=datetime.now(),
        provider_statuses=[],
    )

    with (
        patch("sova.config.loader.load_config", return_value=mock_config),
        patch("sova.awareness.create_providers", return_value=[]) as mock_create_providers,
        patch("sova.awareness.briefing.BriefingService", return_value=mock_service),
        patch("sova.cli.commands.briefing.console", test_console),
    ):
        import asyncio

        asyncio.run(
            _briefing(
                project_dir=None,
                since_str=None,
                providers_str="gmail, gcal",
                output_json=False,
                quiet=False,
            )
        )

    assert mock_config.awareness.providers == ["gmail", "gcal"]
    mock_create_providers.assert_called_once()


def test_json_output_quiet_filters_sections() -> None:
    """JSON output with --quiet only includes attention items."""
    from unittest.mock import AsyncMock, patch

    from sova.cli.commands.briefing import _briefing

    now = datetime.now()
    test_console = Console(file=StringIO(), width=80, legacy_windows=False)
    mock_service = AsyncMock()
    mock_service.generate_briefing.return_value = Briefing(
        generated_at=now,
        attention_items=[
            AwarenessItem(
                id="test:1",
                provider="test",
                category=ItemCategory.NEEDS_ATTENTION,
                title="Important",
                urgency=2,
                timestamp=now,
            ),
        ],
        informational_items=[
            AwarenessItem(
                id="test:2",
                provider="test",
                category=ItemCategory.INFORMATIONAL,
                title="Newsletter",
                timestamp=now,
            ),
        ],
        schedule=[
            AwarenessItem(
                id="cal:1",
                provider="gcal",
                category=ItemCategory.SCHEDULE,
                title="Standup",
                timestamp=now + timedelta(hours=1),
            ),
        ],
        project_pulses=[
            ProjectPulse(project_slug="my-app", open_prs=1, agent_status="idle", last_ci="passing"),
        ],
        provider_statuses=[
            ProviderStatus(name="test", ok=True, message="ok", items_fetched=2, fetch_time_ms=100),
        ],
    )

    with (
        patch("sova.awareness.briefing.BriefingService", return_value=mock_service),
        patch("sova.awareness.create_providers", return_value=[]),
        patch("sova.config.loader.load_config"),
        patch("sova.cli.commands.briefing.console", test_console),
        patch("typer.echo") as mock_echo,
    ):
        import asyncio

        asyncio.run(
            _briefing(
                project_dir=None,
                since_str=None,
                providers_str=None,
                output_json=True,
                quiet=True,
            )
        )

    mock_echo.assert_called_once()
    output = mock_echo.call_args[0][0]
    parsed = json.loads(output)

    assert len(parsed["attention_items"]) == 1
    assert parsed["attention_items"][0]["title"] == "Important"
    assert "informational_items" not in parsed
    assert "schedule" not in parsed
    assert "project_pulses" not in parsed
    assert "provider_statuses" in parsed


def test_briefing_cli_output() -> None:
    """_briefing renders CLI output when output_json=False."""
    from unittest.mock import AsyncMock, patch

    from sova.cli.commands.briefing import _briefing

    test_console = Console(file=StringIO(), width=80, legacy_windows=False)
    mock_service = AsyncMock()
    mock_service.generate_briefing.return_value = Briefing(
        generated_at=datetime.now(),
        attention_items=[
            AwarenessItem(
                id="test:1",
                provider="test",
                category=ItemCategory.NEEDS_ATTENTION,
                title="Test attention",
                urgency=2,
                timestamp=datetime.now(),
            ),
        ],
        provider_statuses=[],
    )

    with (
        patch("sova.awareness.briefing.BriefingService", return_value=mock_service),
        patch("sova.awareness.create_providers", return_value=[]),
        patch("sova.config.loader.load_config"),
        patch("sova.cli.commands.briefing.console", test_console),
        patch("sova.awareness.rendering.cli_renderer.render_briefing_cli") as mock_render,
    ):
        import asyncio

        asyncio.run(
            _briefing(
                project_dir=None,
                since_str=None,
                providers_str=None,
                output_json=False,
                quiet=False,
            )
        )

    mock_render.assert_called_once()
    call_args = mock_render.call_args
    assert call_args[1]["quiet"] is False
