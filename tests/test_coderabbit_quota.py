"""Tests for CodeRabbit quota tracking -- config, DB model, service, and API."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from sova.config.models import CodeRabbitQuotaConfig
from sova.db.session import close_db, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    """Initialize a fresh in-memory DB for each test."""
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


# ---------------------------------------------------------------------------
# Config model tests
# ---------------------------------------------------------------------------


class TestCodeRabbitQuotaConfig:
    def test_defaults(self) -> None:
        cfg = CodeRabbitQuotaConfig()
        assert cfg.enabled is False
        assert cfg.plan == "free"
        # reviews_per_hour=0 triggers plan-based default
        assert cfg.reviews_per_hour == 4

    def test_free_plan_default(self) -> None:
        cfg = CodeRabbitQuotaConfig(plan="free")
        assert cfg.reviews_per_hour == 4

    def test_pro_plan_default(self) -> None:
        cfg = CodeRabbitQuotaConfig(plan="pro")
        assert cfg.reviews_per_hour == 5

    def test_pro_plus_plan_default(self) -> None:
        cfg = CodeRabbitQuotaConfig(plan="pro_plus")
        assert cfg.reviews_per_hour == 10

    def test_explicit_override(self) -> None:
        cfg = CodeRabbitQuotaConfig(plan="free", reviews_per_hour=8)
        assert cfg.reviews_per_hour == 8

    def test_window_minutes_default(self) -> None:
        cfg = CodeRabbitQuotaConfig()
        assert cfg.window_minutes == 60


# ---------------------------------------------------------------------------
# Service tests
# ---------------------------------------------------------------------------


class TestGetQuotaStatus:
    async def test_disabled_returns_available(self) -> None:
        from sova.db.session import get_session
        from sova.supervisor.coderabbit_quota import get_quota_status

        cfg = CodeRabbitQuotaConfig(enabled=False)
        async with await get_session() as session:
            status = await get_quota_status(session, cfg)
        assert status.enabled is False
        assert status.can_create_pr is True
        assert status.next_available_minutes is None

    async def test_empty_history_available(self) -> None:
        from sova.db.session import get_session
        from sova.supervisor.coderabbit_quota import get_quota_status

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        async with await get_session() as session:
            status = await get_quota_status(session, cfg)
        assert status.enabled is True
        assert status.can_create_pr is True
        assert status.reviews_in_window == 0
        assert status.next_available_minutes is None

    async def test_under_limit_available(self) -> None:
        from sova.db.session import get_session
        from sova.supervisor.coderabbit_quota import get_quota_status, record_event

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        now = datetime.now(timezone.utc)

        async with await get_session() as session:
            await record_event(session, pr_number=1, event_type="review", review_id="r1", recorded_at=now)
            await record_event(session, pr_number=2, event_type="review", review_id="r2", recorded_at=now)

        async with await get_session() as session:
            status = await get_quota_status(session, cfg)
        assert status.reviews_in_window == 2
        assert status.can_create_pr is True

    async def test_at_limit_blocked(self) -> None:
        from sova.db.session import get_session
        from sova.supervisor.coderabbit_quota import get_quota_status, record_event

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        now = datetime.now(timezone.utc)

        async with await get_session() as session:
            for i in range(4):
                await record_event(
                    session,
                    pr_number=i + 1,
                    event_type="review",
                    review_id=f"r{i}",
                    recorded_at=now - timedelta(minutes=i * 10),
                )

        async with await get_session() as session:
            status = await get_quota_status(session, cfg)
        assert status.reviews_in_window == 4
        assert status.can_create_pr is False
        assert status.next_available_minutes is not None
        assert status.next_available_minutes >= 0

    async def test_old_events_outside_window(self) -> None:
        from sova.db.session import get_session
        from sova.supervisor.coderabbit_quota import get_quota_status, record_event

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        old = datetime.now(timezone.utc) - timedelta(minutes=120)

        async with await get_session() as session:
            for i in range(4):
                await record_event(
                    session,
                    pr_number=i + 1,
                    event_type="review",
                    review_id=f"old{i}",
                    recorded_at=old,
                )

        async with await get_session() as session:
            status = await get_quota_status(session, cfg)
        assert status.reviews_in_window == 0
        assert status.can_create_pr is True

    async def test_summary_events_not_counted(self) -> None:
        from sova.db.session import get_session
        from sova.supervisor.coderabbit_quota import get_quota_status, record_event

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        now = datetime.now(timezone.utc)

        async with await get_session() as session:
            for i in range(10):
                await record_event(
                    session,
                    pr_number=i + 1,
                    event_type="summary_only",
                    review_id=f"s{i}",
                    recorded_at=now,
                )

        async with await get_session() as session:
            status = await get_quota_status(session, cfg)
        assert status.reviews_in_window == 0
        assert status.can_create_pr is True


class TestRecordEvent:
    async def test_deduplication(self) -> None:
        from sova.db.session import get_session
        from sova.supervisor.coderabbit_quota import record_event

        now = datetime.now(timezone.utc)
        async with await get_session() as session:
            first = await record_event(session, pr_number=1, event_type="review", review_id="dup1", recorded_at=now)
            second = await record_event(session, pr_number=1, event_type="review", review_id="dup1", recorded_at=now)
        assert first is True
        assert second is False

    async def test_project_isolation(self) -> None:
        from sova.db.session import get_session
        from sova.supervisor.coderabbit_quota import get_quota_status, record_event

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        now = datetime.now(timezone.utc)

        async with await get_session() as session:
            for i in range(4):
                await record_event(
                    session,
                    pr_number=i + 1,
                    event_type="review",
                    review_id=f"p{i}",
                    recorded_at=now,
                    project_slug="other/repo",
                )

        async with await get_session() as session:
            status = await get_quota_status(session, cfg, project_slug="my/repo")
        assert status.reviews_in_window == 0
        assert status.can_create_pr is True


class TestSyncFromGitHub:
    async def test_disabled_noop(self) -> None:
        from sova.db.session import get_session
        from sova.supervisor.coderabbit_quota import sync_from_github

        cfg = CodeRabbitQuotaConfig(enabled=False)
        async with await get_session() as session:
            count = await sync_from_github(session, "owner/repo", cfg)
        assert count == 0

    async def test_no_repo_noop(self) -> None:
        from sova.db.session import get_session
        from sova.supervisor.coderabbit_quota import sync_from_github

        cfg = CodeRabbitQuotaConfig(enabled=True)
        async with await get_session() as session:
            count = await sync_from_github(session, "", cfg)
        assert count == 0

    async def test_api_failure_returns_zero(self) -> None:
        from sova.db.session import get_session
        from sova.supervisor.coderabbit_quota import sync_from_github

        cfg = CodeRabbitQuotaConfig(enabled=True)
        with patch(
            "sova.supervisor.coderabbit_quota._fetch_coderabbit_reviews_from_github",
            side_effect=RuntimeError("network error"),
        ):
            async with await get_session() as session:
                count = await sync_from_github(session, "owner/repo", cfg)
        assert count == 0

    async def test_sync_records_events(self) -> None:
        from sova.db.session import get_session
        from sova.supervisor.coderabbit_quota import get_quota_status, sync_from_github

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        now = datetime.now(timezone.utc)
        mock_reviews = [
            {"pr_number": 1, "review_id": "gh1", "submitted_at": now},
            {"pr_number": 2, "review_id": "gh2", "submitted_at": now - timedelta(minutes=10)},
        ]

        with patch(
            "sova.supervisor.coderabbit_quota._fetch_coderabbit_reviews_from_github",
            new_callable=AsyncMock,
            return_value=mock_reviews,
        ):
            async with await get_session() as session:
                count = await sync_from_github(session, "owner/repo", cfg)

        assert count == 2

        async with await get_session() as session:
            status = await get_quota_status(session, cfg)
        assert status.reviews_in_window == 2


# ---------------------------------------------------------------------------
# API router tests
# ---------------------------------------------------------------------------


class TestQuotaAPI:
    async def test_disabled_endpoint(self) -> None:
        from httpx import ASGITransport, AsyncClient

        from sova.dashboard.app import create_app

        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/quota/coderabbit")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False

    async def test_sync_endpoint_disabled(self) -> None:
        from httpx import ASGITransport, AsyncClient

        from sova.dashboard.app import create_app

        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/quota/coderabbit/sync")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False


# ---------------------------------------------------------------------------
# Config loader integration
# ---------------------------------------------------------------------------


class TestConfigLoader:
    def test_coderabbit_quota_in_project_config(self) -> None:
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig()
        assert hasattr(cfg, "coderabbit_quota")
        assert cfg.coderabbit_quota.enabled is False
        assert cfg.coderabbit_quota.plan == "free"

    def test_toml_loading(self, tmp_path: object) -> None:
        from pathlib import Path

        from sova.config.loader import load_config

        p = Path(str(tmp_path))
        toml = p / "sova.toml"
        toml.write_text('[coderabbit_quota]\nenabled = true\nplan = "pro"\n')
        cfg = load_config(p)
        assert cfg.coderabbit_quota.enabled is True
        assert cfg.coderabbit_quota.plan == "pro"
        assert cfg.coderabbit_quota.reviews_per_hour == 5
