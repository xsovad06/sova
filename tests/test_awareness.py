"""Tests for the awareness subsystem foundation."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from sova.awareness.base import AwarenessItem, AwarenessProvider, ItemCategory
from sova.awareness.briefing import BriefingService
from sova.awareness.rendering.models import Briefing, ProviderStatus

# ---------------------------------------------------------------------------
# ItemCategory
# ---------------------------------------------------------------------------


def test_item_category_values() -> None:
    assert ItemCategory.NEEDS_ATTENTION == "needs_attention"
    assert ItemCategory.INFORMATIONAL == "informational"
    assert ItemCategory.SCHEDULE == "schedule"
    assert ItemCategory.DISMISSED == "dismissed"


def test_item_category_is_str_enum() -> None:
    assert isinstance(ItemCategory.NEEDS_ATTENTION, str)


# ---------------------------------------------------------------------------
# AwarenessItem
# ---------------------------------------------------------------------------


def test_awareness_item_defaults() -> None:
    item = AwarenessItem(
        id="test:1",
        provider="test",
        category=ItemCategory.NEEDS_ATTENTION,
        title="Test item",
    )
    assert item.body == ""
    assert item.source_url == ""
    assert item.timestamp is None
    assert item.metadata == {}
    assert item.urgency == 0
    assert item.action_hint == ""


def test_awareness_item_full() -> None:
    now = datetime.now()
    item = AwarenessItem(
        id="gmail:abc123",
        provider="gmail",
        category=ItemCategory.NEEDS_ATTENTION,
        title="Important email",
        body="Please review the attached document.",
        source_url="https://mail.google.com/mail/u/0/#inbox/abc123",
        timestamp=now,
        metadata={"from": "boss@example.com", "labels": ["INBOX"]},
        urgency=2,
        action_hint="Reply to email",
    )
    assert item.provider == "gmail"
    assert item.urgency == 2
    assert item.timestamp == now
    assert item.metadata["from"] == "boss@example.com"


# ---------------------------------------------------------------------------
# AwarenessProvider ABC
# ---------------------------------------------------------------------------


class StubProvider(AwarenessProvider):
    """Minimal concrete provider for testing the ABC contract."""

    name = "stub"
    display_name = "Stub Provider"

    def __init__(self, config=None, items: list[AwarenessItem] | None = None, configured: bool = True) -> None:
        self._items = items or []
        self._configured = configured

    async def fetch_items(self, since: datetime | None = None) -> list[AwarenessItem]:
        if since:
            return [i for i in self._items if i.timestamp and i.timestamp >= since]
        return list(self._items)

    async def is_configured(self) -> bool:
        return self._configured


class FailingProvider(AwarenessProvider):
    """Provider that raises on fetch."""

    name = "failing"

    def __init__(self, config=None) -> None:
        pass

    display_name = "Failing Provider"

    async def fetch_items(self, since: datetime | None = None) -> list[AwarenessItem]:
        raise ConnectionError("API unreachable")

    async def is_configured(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_provider_health_check_ok() -> None:
    provider = StubProvider(configured=True)
    ok, msg = await provider.health_check()
    assert ok is True
    assert "ok" in msg


@pytest.mark.asyncio
async def test_provider_health_check_not_configured() -> None:
    provider = StubProvider(configured=False)
    ok, msg = await provider.health_check()
    assert ok is False
    assert "not configured" in msg


@pytest.mark.asyncio
async def test_provider_health_check_exception() -> None:
    class BadProvider(StubProvider):
        async def is_configured(self) -> bool:
            raise RuntimeError("credential error")

    bad = BadProvider()
    ok, msg = await bad.health_check()
    assert ok is False
    assert "credential error" in msg


@pytest.mark.asyncio
async def test_provider_fetch_with_since() -> None:
    now = datetime.now()
    old_item = AwarenessItem(
        id="test:old",
        provider="stub",
        category=ItemCategory.INFORMATIONAL,
        title="Old item",
        timestamp=now - timedelta(hours=48),
    )
    new_item = AwarenessItem(
        id="test:new",
        provider="stub",
        category=ItemCategory.NEEDS_ATTENTION,
        title="New item",
        timestamp=now - timedelta(hours=1),
    )
    provider = StubProvider(items=[old_item, new_item])

    since = now - timedelta(hours=24)
    items = await provider.fetch_items(since=since)
    assert len(items) == 1
    assert items[0].id == "test:new"


# ---------------------------------------------------------------------------
# Provider Registry
# ---------------------------------------------------------------------------


def test_register_and_create_providers() -> None:
    from sova.awareness import _PROVIDER_REGISTRY, register_provider
    from sova.config.models import AwarenessConfig

    original_registry = dict(_PROVIDER_REGISTRY)
    try:
        register_provider("stub", StubProvider)
        assert "stub" in _PROVIDER_REGISTRY

        config = AwarenessConfig(providers=["stub"])
        from sova.awareness import create_providers

        providers = create_providers(config)
        assert len(providers) == 1
        assert isinstance(providers[0], StubProvider)
    finally:
        _PROVIDER_REGISTRY.clear()
        _PROVIDER_REGISTRY.update(original_registry)


def test_create_providers_skips_unknown() -> None:
    from sova.awareness import create_providers
    from sova.config.models import AwarenessConfig

    config = AwarenessConfig(providers=["nonexistent_provider"])
    providers = create_providers(config)
    assert len(providers) == 0


# ---------------------------------------------------------------------------
# BriefingService
# ---------------------------------------------------------------------------


def _make_items() -> list[AwarenessItem]:
    now = datetime.now()
    return [
        AwarenessItem(
            id="email:1",
            provider="gmail",
            category=ItemCategory.NEEDS_ATTENTION,
            title="Urgent email",
            urgency=2,
            timestamp=now - timedelta(hours=1),
        ),
        AwarenessItem(
            id="email:2",
            provider="gmail",
            category=ItemCategory.INFORMATIONAL,
            title="Newsletter",
            timestamp=now - timedelta(hours=2),
        ),
        AwarenessItem(
            id="pr:1",
            provider="pr_status",
            category=ItemCategory.NEEDS_ATTENTION,
            title="Review requested: fix auth",
            urgency=1,
            timestamp=now - timedelta(minutes=30),
        ),
        AwarenessItem(
            id="cal:1",
            provider="gcal",
            category=ItemCategory.SCHEDULE,
            title="Team standup",
            timestamp=now + timedelta(hours=1),
        ),
    ]


@pytest.mark.asyncio
async def test_briefing_service_generates_briefing() -> None:
    items = _make_items()
    provider = StubProvider(items=items)
    service = BriefingService(providers=[provider])

    briefing = await service.generate_briefing()

    assert isinstance(briefing, Briefing)
    assert len(briefing.attention_items) == 2
    assert len(briefing.informational_items) == 1  # gcal items go to schedule, not informational
    assert len(briefing.schedule) == 1
    assert len(briefing.provider_statuses) == 1
    assert briefing.provider_statuses[0].ok is True
    assert briefing.provider_statuses[0].items_fetched == 4


@pytest.mark.asyncio
async def test_briefing_attention_sorted_by_urgency() -> None:
    items = _make_items()
    provider = StubProvider(items=items)
    service = BriefingService(providers=[provider])

    briefing = await service.generate_briefing()

    assert briefing.attention_items[0].urgency == 2
    assert briefing.attention_items[1].urgency == 1


@pytest.mark.asyncio
async def test_briefing_same_urgency_sorted_by_timestamp() -> None:
    now = datetime.now()
    provider = StubProvider(
        items=[
            AwarenessItem(
                id="old:1",
                provider="stub",
                category=ItemCategory.NEEDS_ATTENTION,
                title="Older high-urgency",
                urgency=2,
                timestamp=now - timedelta(hours=3),
            ),
            AwarenessItem(
                id="new:1",
                provider="stub",
                category=ItemCategory.NEEDS_ATTENTION,
                title="Newer high-urgency",
                urgency=2,
                timestamp=now - timedelta(hours=1),
            ),
        ]
    )
    service = BriefingService(providers=[provider])
    briefing = await service.generate_briefing()

    assert briefing.attention_items[0].id == "new:1"
    assert briefing.attention_items[1].id == "old:1"


@pytest.mark.asyncio
async def test_briefing_schedule_extracted_from_gcal() -> None:
    items = _make_items()
    provider = StubProvider(items=items)
    service = BriefingService(providers=[provider])

    briefing = await service.generate_briefing()

    assert len(briefing.schedule) == 1
    assert briefing.schedule[0].provider == "gcal"


@pytest.mark.asyncio
async def test_briefing_schedule_items_excluded_from_informational() -> None:
    """Schedule items are excluded from the informational list."""
    provider = StubProvider(
        items=[
            AwarenessItem(
                id="cal:1",
                provider="gcal",
                category=ItemCategory.SCHEDULE,
                title="Team standup",
                timestamp=datetime.now() + timedelta(hours=1),
            ),
            AwarenessItem(
                id="email:1",
                provider="gmail",
                category=ItemCategory.INFORMATIONAL,
                title="Newsletter",
                timestamp=datetime.now(),
            ),
        ]
    )
    service = BriefingService(providers=[provider])
    briefing = await service.generate_briefing()

    assert len(briefing.schedule) == 1
    assert len(briefing.informational_items) == 1
    assert briefing.informational_items[0].id == "email:1"


@pytest.mark.asyncio
async def test_briefing_tolerates_provider_failure() -> None:
    good_provider = StubProvider(
        items=[
            AwarenessItem(
                id="ok:1",
                provider="stub",
                category=ItemCategory.INFORMATIONAL,
                title="Good item",
            ),
        ]
    )
    bad_provider = FailingProvider()

    service = BriefingService(providers=[good_provider, bad_provider])
    briefing = await service.generate_briefing()

    assert len(briefing.informational_items) == 1
    assert len(briefing.provider_statuses) == 2

    statuses_by_name = {s.name: s for s in briefing.provider_statuses}
    assert statuses_by_name["stub"].ok is True
    assert statuses_by_name["failing"].ok is False
    assert "unreachable" in statuses_by_name["failing"].message


@pytest.mark.asyncio
async def test_briefing_with_no_providers() -> None:
    service = BriefingService(providers=[])
    briefing = await service.generate_briefing()

    assert len(briefing.attention_items) == 0
    assert len(briefing.informational_items) == 0
    assert len(briefing.provider_statuses) == 0


@pytest.mark.asyncio
async def test_briefing_since_passed_to_providers() -> None:
    now = datetime.now()
    old_item = AwarenessItem(
        id="old:1",
        provider="stub",
        category=ItemCategory.NEEDS_ATTENTION,
        title="Old",
        timestamp=now - timedelta(hours=48),
    )
    new_item = AwarenessItem(
        id="new:1",
        provider="stub",
        category=ItemCategory.NEEDS_ATTENTION,
        title="New",
        timestamp=now - timedelta(hours=1),
    )
    provider = StubProvider(items=[old_item, new_item])
    service = BriefingService(providers=[provider])

    briefing = await service.generate_briefing(since=now - timedelta(hours=24))

    assert len(briefing.attention_items) == 1
    assert briefing.attention_items[0].id == "new:1"


@pytest.mark.asyncio
async def test_briefing_dismissed_items_excluded() -> None:
    provider = StubProvider(
        items=[
            AwarenessItem(
                id="d:1",
                provider="stub",
                category=ItemCategory.DISMISSED,
                title="Dismissed item",
            ),
            AwarenessItem(
                id="a:1",
                provider="stub",
                category=ItemCategory.NEEDS_ATTENTION,
                title="Active item",
            ),
        ]
    )
    service = BriefingService(providers=[provider])
    briefing = await service.generate_briefing()

    assert len(briefing.attention_items) == 1
    assert len(briefing.informational_items) == 0


@pytest.mark.asyncio
async def test_briefing_multiple_providers() -> None:
    now = datetime.now()

    email_provider = StubProvider(
        items=[
            AwarenessItem(
                id="e:1", provider="gmail", category=ItemCategory.NEEDS_ATTENTION, title="Email", timestamp=now
            ),
        ]
    )
    email_provider.name = "gmail"

    pr_provider = StubProvider(
        items=[
            AwarenessItem(
                id="p:1", provider="pr_status", category=ItemCategory.NEEDS_ATTENTION, title="PR", timestamp=now
            ),
        ]
    )
    pr_provider.name = "pr_status"

    service = BriefingService(providers=[email_provider, pr_provider])
    briefing = await service.generate_briefing()

    assert len(briefing.attention_items) == 2
    assert len(briefing.provider_statuses) == 2
    assert all(s.ok for s in briefing.provider_statuses)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_awareness_config_defaults() -> None:
    from sova.config.models import AwarenessConfig

    cfg = AwarenessConfig()
    assert cfg.enabled is False
    assert cfg.providers == []
    assert cfg.gmail_lookback_hours == 24
    assert cfg.gmail_ignore_labels == ["SPAM", "TRASH"]
    assert cfg.gcal_calendars == ["primary"]
    assert cfg.gcal_lookahead_hours == 36
    assert cfg.reminders_lists == ["Reminders"]
    assert cfg.pr_github_user == ""
    assert cfg.gmail_token_path == ""


def test_awareness_config_in_project_config() -> None:
    from sova.config.models import ProjectConfig

    cfg = ProjectConfig()
    assert hasattr(cfg, "awareness")
    assert cfg.awareness.enabled is False


def test_awareness_config_from_toml(tmp_path: Path) -> None:
    from sova.config.loader import load_config

    toml_content = """
[awareness]
enabled = true
providers = ["gmail", "pr_status"]
gmail_lookback_hours = 12
pr_github_user = "testuser"
"""
    toml_file = tmp_path / "sova.toml"
    toml_file.write_text(toml_content)

    cfg = load_config(tmp_path)
    assert cfg.awareness.enabled is True
    assert cfg.awareness.providers == ["gmail", "pr_status"]
    assert cfg.awareness.gmail_lookback_hours == 12
    assert cfg.awareness.pr_github_user == "testuser"


# ---------------------------------------------------------------------------
# Settings Metadata
# ---------------------------------------------------------------------------


def test_awareness_settings_meta_registered() -> None:
    from sova.dashboard.settings_meta import GROUP_ORDER, GROUPS, get_meta

    assert "awareness" in GROUPS
    assert "awareness" in GROUP_ORDER
    assert get_meta("awareness.enabled") is not None
    assert get_meta("awareness.providers") is not None
    assert get_meta("awareness.gmail_token_path") is not None
    assert get_meta("awareness.pr_github_user") is not None


# ---------------------------------------------------------------------------
# Rendering models
# ---------------------------------------------------------------------------


def test_briefing_model_defaults() -> None:
    briefing = Briefing()
    assert briefing.attention_items == []
    assert briefing.informational_items == []
    assert briefing.schedule == []
    assert briefing.project_pulses == []
    assert briefing.provider_statuses == []
    assert briefing.since is None
    assert isinstance(briefing.generated_at, datetime)


def test_provider_status_model() -> None:
    status = ProviderStatus(
        name="gmail",
        ok=True,
        message="ok",
        items_fetched=5,
        fetch_time_ms=120,
    )
    assert status.name == "gmail"
    assert status.ok is True
    assert status.items_fetched == 5
