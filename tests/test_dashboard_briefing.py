"""Tests for the dashboard briefing page: awareness_service + briefing router."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from sova.db.session import close_db, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


@pytest.fixture(autouse=True)
def clear_dismissed():
    import sova.dashboard.services.awareness_service as mod

    mod._dismissed.clear()
    yield
    mod._dismissed.clear()


@pytest.fixture
async def client():
    from sova.dashboard.app import create_app

    app = create_app(project_dir=Path.cwd())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _mock_disabled_config() -> MagicMock:
    cfg = MagicMock()
    cfg.awareness.enabled = False
    cfg.awareness.providers = []
    return cfg


def _mock_enabled_config() -> MagicMock:
    cfg = MagicMock()
    cfg.awareness.enabled = True
    cfg.awareness.providers = ["gmail"]
    return cfg


# --- GET /api/briefing --------------------------------------------------------


@pytest.mark.asyncio
async def test_get_briefing_empty_when_disabled(client: AsyncClient) -> None:
    with patch("sova.dashboard.services.awareness_service.load_config", return_value=_mock_disabled_config()):
        resp = await client.get("/api/briefing")
    assert resp.status_code == 200
    data = resp.json()
    assert data["attention_items"] == []
    assert data["informational_items"] == []
    assert data["schedule"] == []
    assert data["project_pulses"] == []
    assert data["provider_statuses"] == []


@pytest.mark.asyncio
async def test_get_briefing_with_providers(client: AsyncClient) -> None:
    item = SimpleNamespace(
        id="gmail:1",
        provider="gmail",
        category=SimpleNamespace(value="needs_attention"),
        title="Reply to boss",
        body="Please review the attached doc",
        source_url="https://mail.google.com/1",
        timestamp=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        urgency=2,
        action_hint="Reply",
    )
    pulse = SimpleNamespace(project_slug="sova", open_prs=3, agent_status="idle", last_ci="passing")
    status = SimpleNamespace(name="gmail", ok=True, message="ok", items_fetched=1, fetch_time_ms=120)
    mock_briefing = SimpleNamespace(
        generated_at=datetime(2026, 1, 1, 12, 5, tzinfo=timezone.utc),
        attention_items=[item],
        informational_items=[],
        schedule=[],
        project_pulses=[pulse],
        provider_statuses=[status],
        since=None,
    )
    mock_service = AsyncMock()
    mock_service.generate_briefing = AsyncMock(return_value=mock_briefing)

    with (
        patch("sova.dashboard.services.awareness_service.load_config", return_value=_mock_enabled_config()),
        patch("sova.dashboard.services.awareness_service.create_providers", return_value=[MagicMock()]),
        patch("sova.dashboard.services.awareness_service.BriefingService", return_value=mock_service),
    ):
        resp = await client.get("/api/briefing")

    assert resp.status_code == 200
    data = resp.json()
    assert data["generated_at"] is not None
    assert len(data["attention_items"]) == 1
    assert data["attention_items"][0]["title"] == "Reply to boss"
    assert data["project_pulses"][0]["project_slug"] == "sova"
    assert data["provider_statuses"][0]["name"] == "gmail"


@pytest.mark.asyncio
async def test_get_briefing_error_returns_empty_briefing(client: AsyncClient) -> None:
    """A backend failure (e.g. load_config raising) degrades to an empty briefing.

    Mirrors /feed/briefing: never a 500 for a misconfigured/degraded awareness
    setup, so the page can always render gracefully.
    """
    with patch(
        "sova.dashboard.services.awareness_service.load_config",
        side_effect=RuntimeError("boom"),
    ):
        resp = await client.get("/api/briefing")
    assert resp.status_code == 200
    data = resp.json()
    assert data["attention_items"] == []
    assert data["informational_items"] == []
    assert data["provider_statuses"] == []


@pytest.mark.asyncio
async def test_get_briefing_all_providers_failing(client: AsyncClient) -> None:
    """provider_statuses can report every configured provider as failed (ok=False).

    Locks in the response shape the frontend relies on to render the
    "all providers failed" error banner distinct from a network failure.
    """
    status = SimpleNamespace(name="gmail", ok=False, message="unreachable", items_fetched=0, fetch_time_ms=0)
    mock_briefing = SimpleNamespace(
        generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        attention_items=[],
        informational_items=[],
        schedule=[],
        project_pulses=[],
        provider_statuses=[status],
        since=None,
    )
    mock_service = AsyncMock()
    mock_service.generate_briefing = AsyncMock(return_value=mock_briefing)

    with (
        patch("sova.dashboard.services.awareness_service.load_config", return_value=_mock_enabled_config()),
        patch("sova.dashboard.services.awareness_service.create_providers", return_value=[MagicMock()]),
        patch("sova.dashboard.services.awareness_service.BriefingService", return_value=mock_service),
    ):
        resp = await client.get("/api/briefing")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["provider_statuses"]) == 1
    assert all(not s["ok"] for s in data["provider_statuses"])


@pytest.mark.asyncio
async def test_briefing_status_carries_provider_display_name(client: AsyncClient) -> None:
    """provider_statuses is enriched with the human label, not just the config key."""
    provider = MagicMock()
    provider.name = "gcal"
    provider.display_name = "Google Calendar"
    status = SimpleNamespace(name="gcal", ok=True, message="ok", items_fetched=0, fetch_time_ms=5)
    mock_briefing = SimpleNamespace(
        generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        attention_items=[],
        informational_items=[],
        schedule=[],
        project_pulses=[],
        provider_statuses=[status],
        since=None,
    )
    mock_service = AsyncMock()
    mock_service.generate_briefing = AsyncMock(return_value=mock_briefing)

    with (
        patch("sova.dashboard.services.awareness_service.load_config", return_value=_mock_enabled_config()),
        patch("sova.dashboard.services.awareness_service.create_providers", return_value=[provider]),
        patch("sova.dashboard.services.awareness_service.BriefingService", return_value=mock_service),
    ):
        resp = await client.get("/api/briefing")

    assert resp.status_code == 200
    assert resp.json()["provider_statuses"][0]["display_name"] == "Google Calendar"


@pytest.mark.asyncio
async def test_briefing_status_display_name_falls_back_to_key(client: AsyncClient) -> None:
    """An unmapped provider key is echoed as its own display name rather than dropped."""
    status = SimpleNamespace(name="unmapped", ok=False, message="down", items_fetched=0, fetch_time_ms=0)
    mock_briefing = SimpleNamespace(
        generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        attention_items=[],
        informational_items=[],
        schedule=[],
        project_pulses=[],
        provider_statuses=[status],
        since=None,
    )
    mock_service = AsyncMock()
    mock_service.generate_briefing = AsyncMock(return_value=mock_briefing)

    with (
        patch("sova.dashboard.services.awareness_service.load_config", return_value=_mock_enabled_config()),
        patch("sova.dashboard.services.awareness_service.create_providers", return_value=[MagicMock()]),
        patch("sova.dashboard.services.awareness_service.BriefingService", return_value=mock_service),
    ):
        resp = await client.get("/api/briefing")

    assert resp.status_code == 200
    assert resp.json()["provider_statuses"][0]["display_name"] == "unmapped"


# --- GET /api/briefing/providers ----------------------------------------------


@pytest.mark.asyncio
async def test_get_providers_empty_when_disabled(client: AsyncClient) -> None:
    with patch("sova.dashboard.services.awareness_service.load_config", return_value=_mock_disabled_config()):
        resp = await client.get("/api/briefing/providers")
    assert resp.status_code == 200
    assert resp.json() == {"providers": []}


@pytest.mark.asyncio
async def test_get_providers_with_health_checks(client: AsyncClient) -> None:
    provider = MagicMock()
    provider.name = "gmail"
    provider.display_name = "Gmail"
    provider.health_check = AsyncMock(return_value=(True, "Gmail: ok"))

    with (
        patch("sova.dashboard.services.awareness_service.load_config", return_value=_mock_enabled_config()),
        patch("sova.dashboard.services.awareness_service.create_providers", return_value=[provider]),
    ):
        resp = await client.get("/api/briefing/providers")

    assert resp.status_code == 200
    data = resp.json()
    assert data["providers"] == [{"name": "gmail", "display_name": "Gmail", "ok": True, "message": "Gmail: ok"}]


@pytest.mark.asyncio
async def test_get_providers_handles_failing_health_check(client: AsyncClient) -> None:
    provider = MagicMock()
    provider.name = "gmail"
    provider.display_name = "Gmail"
    provider.health_check = AsyncMock(side_effect=ConnectionError("unreachable"))

    with (
        patch("sova.dashboard.services.awareness_service.load_config", return_value=_mock_enabled_config()),
        patch("sova.dashboard.services.awareness_service.create_providers", return_value=[provider]),
    ):
        resp = await client.get("/api/briefing/providers")

    assert resp.status_code == 200
    data = resp.json()
    assert data["providers"][0]["ok"] is False
    assert "unreachable" in data["providers"][0]["message"]


# --- POST /api/briefing/{item_id}/dismiss -------------------------------------


@pytest.mark.asyncio
async def test_dismiss_item(client: AsyncClient) -> None:
    resp = await client.post("/api/briefing/gmail:1/dismiss")
    assert resp.status_code == 200
    assert resp.json() == {"dismissed": True}


@pytest.mark.asyncio
async def test_dismissed_item_excluded_from_briefing(client: AsyncClient) -> None:
    item = SimpleNamespace(
        id="gmail:1",
        provider="gmail",
        category=SimpleNamespace(value="needs_attention"),
        title="Reply to boss",
        body="",
        source_url="",
        timestamp=None,
        urgency=1,
        action_hint="",
    )
    status = SimpleNamespace(name="gmail", ok=True, message="ok", items_fetched=1, fetch_time_ms=1)
    mock_briefing = SimpleNamespace(
        generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        attention_items=[item],
        informational_items=[],
        schedule=[],
        project_pulses=[],
        provider_statuses=[status],
        since=None,
    )
    mock_service = AsyncMock()
    mock_service.generate_briefing = AsyncMock(return_value=mock_briefing)

    await client.post("/api/briefing/gmail:1/dismiss")

    with (
        patch("sova.dashboard.services.awareness_service.load_config", return_value=_mock_enabled_config()),
        patch("sova.dashboard.services.awareness_service.create_providers", return_value=[MagicMock()]),
        patch("sova.dashboard.services.awareness_service.BriefingService", return_value=mock_service),
    ):
        resp = await client.get("/api/briefing")

    assert resp.status_code == 200
    assert resp.json()["attention_items"] == []


# --- Page route ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_briefing_page_renders(client: AsyncClient) -> None:
    resp = await client.get("/briefing")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


# --- Nav item visibility (gated on awareness.enabled) ---------------------------


@pytest.mark.asyncio
async def test_nav_hides_briefing_link_when_awareness_disabled(client: AsyncClient) -> None:
    with patch("sova.dashboard.services.awareness_service.load_config", return_value=_mock_disabled_config()):
        resp = await client.get("/dashboard")
    assert resp.status_code == 200
    assert 'href="/briefing"' not in resp.text


@pytest.mark.asyncio
async def test_nav_shows_briefing_link_when_awareness_enabled(client: AsyncClient) -> None:
    with patch("sova.dashboard.services.awareness_service.load_config", return_value=_mock_enabled_config()):
        resp = await client.get("/dashboard")
    assert resp.status_code == 200
    assert 'href="/briefing"' in resp.text
