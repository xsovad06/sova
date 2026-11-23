"""Tests for CodeRabbit quota tracking: config, DB model, service, and API."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sova.config.models import CodeRabbitQuotaConfig
from sova.supervisor.coderabbit_quota import _fetch_star_count as _REAL_FETCH_STAR_COUNT
from sova.utils.shell import ShellResult
from tests.conftest import csrf_request_headers as _csrf_request_headers

# Uses the shared per-worker in-memory DB, emptied before each test
# (see ``tests/conftest.py:reset_db_rows``).
pytestmark = pytest.mark.usefixtures("reset_db_rows")


@pytest.fixture(autouse=True)
def _clear_sync_cache() -> Iterator[None]:
    """Empty the per-repo caches, and keep a sync from running a real ``gh`` for the star count."""
    from sova.supervisor import coderabbit_quota as quota

    def _reset() -> None:
        for task in list(quota._background_syncs.values()):
            task.cancel()
        quota._background_syncs.clear()
        from sova.supervisor import github_quota

        github_quota._trackers.clear()  # a rate-limit hit recorded by another test must not block a sync
        for cache in (quota._sync_cache, quota._stars_cache, quota._sync_attempted, quota._stars_attempted):
            cache.clear()

    _reset()
    # No real gh: an endpoint test that GETs with the quota enabled would start a background sync that shells out
    # and outlives the test (and leaves a task in _background_syncs for the next one).
    with (
        patch("sova.supervisor.coderabbit_quota._fetch_star_count", new_callable=AsyncMock, return_value=None),
        patch("sova.dashboard.routers.quota.start_background_sync", return_value=False),
    ):
        yield
    _reset()


async def _record_event(
    session: AsyncSession,
    *,
    pr_number: int,
    event_type: str,
    review_id: str,
    recorded_at: datetime | None = None,
    project_slug: str = "",
) -> bool:
    """Test-only seeding helper around the private _upsert_event, committing like the old record_event did."""
    from sova.supervisor.coderabbit_quota import _upsert_event

    if recorded_at is None:
        recorded_at = datetime.now(timezone.utc)
    inserted = await _upsert_event(session, pr_number, event_type, review_id, recorded_at, project_slug)
    await session.commit()
    return inserted


# ---------------------------------------------------------------------------
# Config model tests
# ---------------------------------------------------------------------------


class TestCodeRabbitQuotaConfig:
    def test_defaults(self) -> None:
        cfg = CodeRabbitQuotaConfig()
        assert cfg.enabled is False
        assert cfg.plan == "free"
        assert cfg.reviews_per_hour is None, "an unset limit stays unset: the plan and stars derive it"
        assert cfg.reviews_per_hour_is_explicit is False

    @pytest.mark.parametrize(
        ("plan", "expected"),
        [("free", 1), ("oss", 1), ("pro", 5), ("pro_plus", 10)],
    )
    def test_plan_default_is_the_below_10_stars_value(self, plan: str, expected: int) -> None:
        """Stars are unknown here, so free and oss get the conservative 1; paid plans their fixed limit."""
        from sova.supervisor.coderabbit_quota import effective_reviews_per_hour

        cfg = CodeRabbitQuotaConfig(plan=plan)
        assert effective_reviews_per_hour(cfg, None) == expected
        assert cfg.reviews_per_hour_is_explicit is False

    @pytest.mark.parametrize("dump", ["python", "json"])
    def test_a_round_trip_keeps_an_unset_limit_unset(self, dump: str) -> None:
        """A filled default would round-trip as an explicit 1 and pin a 10+-star repo to one an hour."""
        cfg = CodeRabbitQuotaConfig(plan="free")
        data = cfg.model_dump() if dump == "python" else json.loads(cfg.model_dump_json())
        assert CodeRabbitQuotaConfig.model_validate(data).reviews_per_hour_is_explicit is False
        explicit = CodeRabbitQuotaConfig.model_validate(CodeRabbitQuotaConfig(reviews_per_hour=0).model_dump())
        assert explicit.reviews_per_hour_is_explicit is True and explicit.reviews_per_hour == 0

    @pytest.mark.parametrize("value", [8, 0])
    def test_explicit_override(self, value: int) -> None:
        cfg = CodeRabbitQuotaConfig(plan="free", reviews_per_hour=value)
        assert cfg.reviews_per_hour == value
        assert cfg.reviews_per_hour_is_explicit is True

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
        from sova.supervisor.coderabbit_quota import get_quota_status

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free", reviews_per_hour=4)
        now = datetime.now(timezone.utc)

        async with await get_session() as session:
            await _record_event(session, pr_number=1, event_type="review", review_id="r1", recorded_at=now)
            await _record_event(session, pr_number=2, event_type="review", review_id="r2", recorded_at=now)

        async with await get_session() as session:
            status = await get_quota_status(session, cfg)
        assert status.reviews_in_window == 2
        assert status.can_create_pr is True

    async def test_at_limit_blocked(self) -> None:
        from sova.db.session import get_session
        from sova.supervisor.coderabbit_quota import get_quota_status

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        now = datetime.now(timezone.utc)

        async with await get_session() as session:
            for i in range(4):
                await _record_event(
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
        from sova.supervisor.coderabbit_quota import get_quota_status

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        old = datetime.now(timezone.utc) - timedelta(minutes=120)

        async with await get_session() as session:
            for i in range(4):
                await _record_event(
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
        from sova.supervisor.coderabbit_quota import get_quota_status

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        now = datetime.now(timezone.utc)

        async with await get_session() as session:
            for i in range(10):
                await _record_event(
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


class TestUpsertEvent:
    """Dedup and project-isolation behavior of the private _upsert_event, via the _record_event test helper."""

    async def test_deduplication(self) -> None:
        from sova.db.session import get_session

        now = datetime.now(timezone.utc)
        async with await get_session() as session:
            first = await _record_event(session, pr_number=1, event_type="review", review_id="dup1", recorded_at=now)
            second = await _record_event(session, pr_number=1, event_type="review", review_id="dup1", recorded_at=now)
        assert first is True
        assert second is False

    async def test_project_isolation(self) -> None:
        from sova.db.session import get_session
        from sova.supervisor.coderabbit_quota import get_quota_status

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        now = datetime.now(timezone.utc)

        async with await get_session() as session:
            for i in range(4):
                await _record_event(
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

    async def test_sync_cache_skips_api_on_fresh_cache(self) -> None:
        """sync_from_github should skip the API call when cache is fresh."""
        from sova.db.session import get_session
        from sova.supervisor.coderabbit_quota import sync_from_github

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        now = datetime.now(timezone.utc)
        mock_reviews = [{"pr_number": 1, "review_id": "r1", "submitted_at": now}]

        mock_fetch = AsyncMock(return_value=mock_reviews)
        with patch(
            "sova.supervisor.coderabbit_quota._fetch_coderabbit_reviews_from_github",
            mock_fetch,
        ):
            async with await get_session() as session:
                first = await sync_from_github(session, "owner/repo", cfg)
            assert first == 1
            assert mock_fetch.call_count == 1

            # Second call within TTL should skip the API entirely
            async with await get_session() as session:
                second = await sync_from_github(session, "owner/repo", cfg)
            assert second == 0
            assert mock_fetch.call_count == 1  # no additional call

    async def test_sync_cache_force_bypasses_cache(self) -> None:
        """force=True should ignore the cache and call the API."""
        from sova.db.session import get_session
        from sova.supervisor.coderabbit_quota import sync_from_github

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        mock_fetch = AsyncMock(return_value=[])
        with patch(
            "sova.supervisor.coderabbit_quota._fetch_coderabbit_reviews_from_github",
            mock_fetch,
        ):
            async with await get_session() as session:
                await sync_from_github(session, "owner/repo", cfg)
            assert mock_fetch.call_count == 1

            async with await get_session() as session:
                await sync_from_github(session, "owner/repo", cfg, force=True)
            assert mock_fetch.call_count == 2

    async def test_synced_at_populated_after_sync(self) -> None:
        """get_quota_status should include synced_at after a successful sync."""
        from sova.db.session import get_session
        from sova.supervisor.coderabbit_quota import get_quota_status, sync_from_github

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        with patch(
            "sova.supervisor.coderabbit_quota._fetch_coderabbit_reviews_from_github",
            new_callable=AsyncMock,
            return_value=[],
        ):
            async with await get_session() as session:
                await sync_from_github(session, "owner/repo", cfg)

        async with await get_session() as session:
            status = await get_quota_status(session, cfg, repo="owner/repo")
        assert status.synced_at is not None
        assert status.synced_at.endswith("Z")

    async def test_synced_at_none_before_sync(self) -> None:
        """synced_at should be None when no sync has occurred."""
        from sova.db.session import get_session
        from sova.supervisor.coderabbit_quota import get_quota_status

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        async with await get_session() as session:
            status = await get_quota_status(session, cfg, repo="fresh/repo")
        assert status.synced_at is None

    async def test_sync_cache_invalidation_forces_refetch(self) -> None:
        """Popping the repo's sync-cache entry should force the next sync to call the API."""
        from sova.db.session import get_session
        from sova.supervisor import coderabbit_quota
        from sova.supervisor.coderabbit_quota import sync_from_github

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        mock_fetch = AsyncMock(return_value=[])
        with patch(
            "sova.supervisor.coderabbit_quota._fetch_coderabbit_reviews_from_github",
            mock_fetch,
        ):
            async with await get_session() as session:
                await sync_from_github(session, "owner/repo", cfg)
            assert mock_fetch.call_count == 1

            coderabbit_quota._sync_cache.pop("owner/repo", None)

            async with await get_session() as session:
                await sync_from_github(session, "owner/repo", cfg)
            assert mock_fetch.call_count == 2


# ---------------------------------------------------------------------------
# API router tests
# ---------------------------------------------------------------------------


class TestQuotaAPI:
    async def test_disabled_endpoint(self) -> None:
        from httpx import ASGITransport, AsyncClient

        from sova.config.models import ProjectConfig
        from sova.dashboard.app import create_app

        cfg = ProjectConfig(coderabbit_quota=CodeRabbitQuotaConfig(enabled=False))
        app = create_app()
        with patch("sova.dashboard.routers.quota.load_config", return_value=cfg):
            async with AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
            ) as client:
                client.headers.update(_csrf_request_headers(client))
                resp = await client.get("/api/quota/coderabbit")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False

    async def test_sync_endpoint_disabled(self) -> None:
        from httpx import ASGITransport, AsyncClient

        from sova.config.models import ProjectConfig
        from sova.dashboard.app import create_app

        cfg = ProjectConfig(coderabbit_quota=CodeRabbitQuotaConfig(enabled=False))
        app = create_app()
        with patch("sova.dashboard.routers.quota.load_config", return_value=cfg):
            async with AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
            ) as client:
                client.headers.update(_csrf_request_headers(client))
                resp = await client.post("/api/quota/coderabbit/sync")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False

    async def test_enabled_endpoint_returns_status(self) -> None:
        from httpx import ASGITransport, AsyncClient

        from sova.config.models import ProjectConfig
        from sova.dashboard.app import create_app
        from sova.supervisor.coderabbit_quota import QuotaStatus

        mock_status = QuotaStatus(
            enabled=True,
            reviews_in_window=2,
            reviews_per_hour=4,
            can_create_pr=True,
            next_available_minutes=None,
            window_minutes=60,
        )
        cfg = ProjectConfig(coderabbit_quota=CodeRabbitQuotaConfig(enabled=True))
        app = create_app()
        with (
            patch("sova.dashboard.routers.quota.load_config", return_value=cfg),
            patch("sova.dashboard.routers.quota.get_quota_status", new_callable=AsyncMock, return_value=mock_status),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
            ) as client:
                client.headers.update(_csrf_request_headers(client))
                resp = await client.get("/api/quota/coderabbit")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is True
        assert data["reviews_in_window"] == 2
        assert data["can_create_pr"] is True

    async def test_enabled_endpoint_error_returns_500(self) -> None:
        from httpx import ASGITransport, AsyncClient

        from sova.config.models import ProjectConfig
        from sova.dashboard.app import create_app

        cfg = ProjectConfig(coderabbit_quota=CodeRabbitQuotaConfig(enabled=True))
        app = create_app()
        with (
            patch("sova.dashboard.routers.quota.load_config", return_value=cfg),
            patch(
                "sova.dashboard.routers.quota.get_quota_status",
                new_callable=AsyncMock,
                side_effect=RuntimeError("db fail"),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
            ) as client:
                client.headers.update(_csrf_request_headers(client))
                resp = await client.get("/api/quota/coderabbit")
        assert resp.status_code == 500

    async def test_sync_endpoint_enabled_success(self) -> None:
        from httpx import ASGITransport, AsyncClient

        from sova.config.models import ProjectConfig
        from sova.dashboard.app import create_app

        cfg = ProjectConfig(
            github_repo="owner/repo",
            coderabbit_quota=CodeRabbitQuotaConfig(enabled=True),
        )
        app = create_app()
        with (
            patch("sova.dashboard.routers.quota.load_config", return_value=cfg),
            patch("sova.dashboard.routers.quota.sync_from_github", new_callable=AsyncMock, return_value=3),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
            ) as client:
                client.headers.update(_csrf_request_headers(client))
                resp = await client.post("/api/quota/coderabbit/sync")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is True
        assert data["synced"] == 3

    async def test_sync_endpoint_no_repo(self) -> None:
        from httpx import ASGITransport, AsyncClient

        from sova.config.models import ProjectConfig
        from sova.dashboard.app import create_app

        cfg = ProjectConfig(
            github_repo="",
            coderabbit_quota=CodeRabbitQuotaConfig(enabled=True),
        )
        app = create_app()
        with patch("sova.dashboard.routers.quota.load_config", return_value=cfg):
            async with AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
            ) as client:
                client.headers.update(_csrf_request_headers(client))
                resp = await client.post("/api/quota/coderabbit/sync")
        assert resp.status_code == 503
        assert "github_repo" in resp.json()["detail"]

    async def test_sync_endpoint_error_returns_500(self) -> None:
        from httpx import ASGITransport, AsyncClient

        from sova.config.models import ProjectConfig
        from sova.dashboard.app import create_app

        cfg = ProjectConfig(
            github_repo="owner/repo",
            coderabbit_quota=CodeRabbitQuotaConfig(enabled=True),
        )
        app = create_app()
        with (
            patch("sova.dashboard.routers.quota.load_config", return_value=cfg),
            patch(
                "sova.dashboard.routers.quota.sync_from_github",
                new_callable=AsyncMock,
                side_effect=RuntimeError("api fail"),
            ),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
            ) as client:
                client.headers.update(_csrf_request_headers(client))
                resp = await client.post("/api/quota/coderabbit/sync")
        assert resp.status_code == 500

    async def test_pr_queue_disabled(self) -> None:
        from httpx import ASGITransport, AsyncClient

        from sova.config.models import ProjectConfig
        from sova.dashboard.app import create_app

        cfg = ProjectConfig(coderabbit_quota=CodeRabbitQuotaConfig(enabled=False))
        app = create_app()
        with patch("sova.dashboard.routers.quota.load_config", return_value=cfg):
            async with AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
            ) as client:
                client.headers.update(_csrf_request_headers(client))
                resp = await client.get("/api/quota/pr-queue")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False
        assert data["pending"] == 0
        assert data["entries"] == []

    async def test_pr_queue_error_returns_500(self) -> None:
        from httpx import ASGITransport, AsyncClient

        from sova.config.models import ProjectConfig
        from sova.dashboard.app import create_app

        cfg = ProjectConfig(
            github_repo="owner/repo",
            coderabbit_quota=CodeRabbitQuotaConfig(enabled=True),
        )
        app = create_app()
        with (
            patch("sova.dashboard.routers.quota.load_config", return_value=cfg),
            patch("sova.dashboard.routers.quota.get_session", new_callable=AsyncMock, side_effect=RuntimeError("db")),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
            ) as client:
                client.headers.update(_csrf_request_headers(client))
                resp = await client.get("/api/quota/pr-queue")
        assert resp.status_code == 500

    async def test_pr_queue_enabled_with_entries(self) -> None:
        from httpx import ASGITransport, AsyncClient

        from sova.config.models import ProjectConfig
        from sova.dashboard.app import create_app
        from sova.db.models import PRCreationQueue, PRQueueStatus, TaskRun
        from sova.db.session import get_session

        # Seed a TaskRun and a PR queue entry
        async with await get_session() as session:
            async with session.begin():
                tr = TaskRun(issue_number="42", role="developer", status="running")
                session.add(tr)
                await session.flush()
                entry = PRCreationQueue(
                    task_run_id=tr.id,
                    issue_number="42",
                    title="test PR",
                    body="body",
                    base_branch="main",
                    head_branch="feat/test",
                    repo="owner/repo",
                    status=PRQueueStatus.PENDING,
                    project_slug="owner/repo",
                )
                session.add(entry)

        cfg = ProjectConfig(
            github_repo="owner/repo",
            coderabbit_quota=CodeRabbitQuotaConfig(enabled=True),
        )
        app = create_app()
        with patch("sova.dashboard.routers.quota.load_config", return_value=cfg):
            async with AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
            ) as client:
                client.headers.update(_csrf_request_headers(client))
                resp = await client.get("/api/quota/pr-queue")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is True
        assert data["pending"] == 1
        assert len(data["entries"]) == 1
        assert data["entries"][0]["issue_number"] == "42"
        assert data["entries"][0]["status"] == PRQueueStatus.PENDING


# ---------------------------------------------------------------------------
# Config loader integration
# ---------------------------------------------------------------------------


class TestConfigLoader:
    def test_toml_loading(self, tmp_path: object) -> None:
        from pathlib import Path

        from sova.config.loader import load_config
        from sova.supervisor.coderabbit_quota import effective_reviews_per_hour

        p = Path(str(tmp_path))
        toml = p / "sova.toml"
        toml.write_text('[coderabbit_quota]\nenabled = true\nplan = "pro"\n')
        cfg = load_config(p)
        assert cfg.coderabbit_quota.enabled is True
        assert cfg.coderabbit_quota.plan == "pro"
        assert cfg.coderabbit_quota.reviews_per_hour is None  # not stored: the plan derives it
        assert effective_reviews_per_hour(cfg.coderabbit_quota, None) == 5  # the pro plan's limit


# ---------------------------------------------------------------------------
# Coverage: uncovered code paths
# ---------------------------------------------------------------------------


class TestGetQuotaStatusUnlimited:
    async def test_unlimited_reviews_per_hour_zero(self) -> None:
        """Line 57: reviews_per_hour == 0 means unlimited."""
        from sova.db.session import get_session
        from sova.supervisor.coderabbit_quota import get_quota_status

        cfg = CodeRabbitQuotaConfig(enabled=True, reviews_per_hour=0)
        # Force reviews_per_hour to 0 (validator sets plan default)
        object.__setattr__(cfg, "reviews_per_hour", 0)
        async with await get_session() as session:
            status = await get_quota_status(session, cfg)
        assert status.enabled is True
        assert status.can_create_pr is True
        assert status.reviews_per_hour == 0
        assert status.next_available_minutes is None


class TestSyncFromGitHubEmpty:
    async def test_empty_reviews_returns_zero(self) -> None:
        """Line 114: sync returns 0 when API returns empty list."""
        from sova.db.session import get_session
        from sova.supervisor.coderabbit_quota import sync_from_github

        cfg = CodeRabbitQuotaConfig(enabled=True)
        with patch(
            "sova.supervisor.coderabbit_quota._fetch_coderabbit_reviews_from_github",
            new_callable=AsyncMock,
            return_value=[],
        ):
            async with await get_session() as session:
                count = await sync_from_github(session, "owner/repo", cfg)
        assert count == 0


class TestFetchCodeRabbitReviewsFromGitHub:
    async def test_pr_fetch_failure(self) -> None:
        """Lines 236-238: PR list fetch fails."""
        from sova.supervisor.coderabbit_quota import _fetch_coderabbit_reviews_from_github

        with patch("sova.supervisor.coderabbit_quota.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = ShellResult(returncode=1, stdout="", stderr="gh error")
            reviews = await _fetch_coderabbit_reviews_from_github("owner/repo")
        assert reviews is None

    async def test_no_pr_numbers(self) -> None:
        """Lines 246-247: stdout has no valid PR numbers."""
        from sova.supervisor.coderabbit_quota import _fetch_coderabbit_reviews_from_github

        with patch("sova.supervisor.coderabbit_quota.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = ShellResult(returncode=0, stdout="", stderr="")
            reviews = await _fetch_coderabbit_reviews_from_github("owner/repo")
        assert reviews == []

    async def test_successful_fetch(self) -> None:
        """Lines 213-264: full successful fetch with semaphore and gather."""
        from sova.supervisor.coderabbit_quota import _fetch_coderabbit_reviews_from_github

        now_iso = datetime.now(timezone.utc).isoformat()
        review_data = json.dumps(
            [
                {
                    "id": 123,
                    "user": {"login": "coderabbitai[bot]"},
                    "state": "COMMENTED",
                    "submitted_at": now_iso,
                }
            ]
        )

        async def mock_run_side_effect(*args: object, **kwargs: object) -> ShellResult:
            cmd_args = args
            if "pulls" in str(cmd_args) and "reviews" not in str(cmd_args):
                recent = datetime.now(timezone.utc).isoformat()
                return ShellResult(returncode=0, stdout=f"10 {recent}\n20 {recent}\n", stderr="")
            return ShellResult(returncode=0, stdout=review_data, stderr="")

        with patch("sova.supervisor.coderabbit_quota.run", new_callable=AsyncMock, side_effect=mock_run_side_effect):
            with patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None):
                reviews = await _fetch_coderabbit_reviews_from_github("owner/repo", github_user="testuser")
        assert len(reviews) == 2  # one review per PR (10 and 20)

    async def test_gather_exception_skipped(self) -> None:
        """Lines 260-262: exceptions from gather are skipped."""
        from sova.supervisor.coderabbit_quota import _fetch_coderabbit_reviews_from_github

        call_count = 0

        async def mock_run_side_effect(*args: object, **kwargs: object) -> ShellResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # PR list
                recent = datetime.now(timezone.utc).isoformat()
                return ShellResult(returncode=0, stdout=f"10 {recent}\n", stderr="")
            # Review fetch raises
            raise RuntimeError("network timeout")

        with patch("sova.supervisor.coderabbit_quota.run", new_callable=AsyncMock, side_effect=mock_run_side_effect):
            reviews = await _fetch_coderabbit_reviews_from_github("owner/repo")
        assert reviews == []


class TestFetchReviewsForPR:
    async def test_fetch_failure(self) -> None:
        """Lines 281-283: review fetch fails."""
        from sova.supervisor.coderabbit_quota import _fetch_reviews_for_pr

        with patch("sova.supervisor.coderabbit_quota.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = ShellResult(returncode=1, stdout="", stderr="api error")
            result = await _fetch_reviews_for_pr("owner/repo", 1)
        assert result == []

    async def test_bad_json(self) -> None:
        """Lines 285-289: invalid JSON response."""
        from sova.supervisor.coderabbit_quota import _fetch_reviews_for_pr

        with patch("sova.supervisor.coderabbit_quota.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = ShellResult(returncode=0, stdout="not json", stderr="")
            result = await _fetch_reviews_for_pr("owner/repo", 1)
        assert result == []

    async def test_non_list_json(self) -> None:
        """Lines 291-292: valid JSON but not a list."""
        from sova.supervisor.coderabbit_quota import _fetch_reviews_for_pr

        with patch("sova.supervisor.coderabbit_quota.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = ShellResult(returncode=0, stdout='{"error": "not found"}', stderr="")
            result = await _fetch_reviews_for_pr("owner/repo", 1)
        assert result == []

    async def test_non_coderabbit_review_skipped(self) -> None:
        """Lines 298-299: reviews from non-CodeRabbit users skipped."""
        from sova.supervisor.coderabbit_quota import _fetch_reviews_for_pr

        review = {"id": 1, "user": {"login": "humanuser"}, "state": "APPROVED", "submitted_at": "2026-01-01T00:00:00Z"}
        data = json.dumps([review])
        with patch("sova.supervisor.coderabbit_quota.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = ShellResult(returncode=0, stdout=data, stderr="")
            result = await _fetch_reviews_for_pr("owner/repo", 1)
        assert result == []

    async def test_missing_fields_skipped(self) -> None:
        """Lines 305-306: reviews missing state/submitted_at/review_id."""
        from sova.supervisor.coderabbit_quota import _fetch_reviews_for_pr

        data = json.dumps([{"id": "", "user": {"login": "coderabbitai[bot]"}, "state": "", "submitted_at": ""}])
        with patch("sova.supervisor.coderabbit_quota.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = ShellResult(returncode=0, stdout=data, stderr="")
            result = await _fetch_reviews_for_pr("owner/repo", 1)
        assert result == []

    async def test_pending_review_skipped(self) -> None:
        """Lines 309-310: PENDING state reviews not counted."""
        from sova.supervisor.coderabbit_quota import _fetch_reviews_for_pr

        data = json.dumps(
            [
                {
                    "id": 42,
                    "user": {"login": "coderabbitai[bot]"},
                    "state": "PENDING",
                    "submitted_at": "2026-01-01T00:00:00Z",
                }
            ]
        )
        with patch("sova.supervisor.coderabbit_quota.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = ShellResult(returncode=0, stdout=data, stderr="")
            result = await _fetch_reviews_for_pr("owner/repo", 1)
        assert result == []

    async def test_bad_date_skipped(self) -> None:
        """Lines 312-316: invalid submitted_at date."""
        from sova.supervisor.coderabbit_quota import _fetch_reviews_for_pr

        data = json.dumps(
            [
                {
                    "id": 42,
                    "user": {"login": "coderabbitai[bot]"},
                    "state": "COMMENTED",
                    "submitted_at": "not-a-date",
                }
            ]
        )
        with patch("sova.supervisor.coderabbit_quota.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = ShellResult(returncode=0, stdout=data, stderr="")
            result = await _fetch_reviews_for_pr("owner/repo", 1)
        assert result == []

    async def test_valid_coderabbit_review(self) -> None:
        """Lines 318-326: valid CodeRabbit review returned."""
        from sova.supervisor.coderabbit_quota import _fetch_reviews_for_pr

        data = json.dumps(
            [
                {
                    "id": 42,
                    "user": {"login": "coderabbitai[bot]"},
                    "state": "COMMENTED",
                    "submitted_at": "2026-01-01T00:00:00Z",
                }
            ]
        )
        with patch("sova.supervisor.coderabbit_quota.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = ShellResult(returncode=0, stdout=data, stderr="")
            result = await _fetch_reviews_for_pr("owner/repo", 1)
        assert len(result) == 1
        assert result[0]["pr_number"] == 1
        assert result[0]["review_id"] == "42"

    async def test_null_user_login_skipped(self) -> None:
        """Lines 296-299: user with None login skipped."""
        from sova.supervisor.coderabbit_quota import _fetch_reviews_for_pr

        data = json.dumps(
            [
                {
                    "id": 42,
                    "user": {"login": None},
                    "state": "COMMENTED",
                    "submitted_at": "2026-01-01T00:00:00Z",
                }
            ]
        )
        with patch("sova.supervisor.coderabbit_quota.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = ShellResult(returncode=0, stdout=data, stderr="")
            result = await _fetch_reviews_for_pr("owner/repo", 1)
        assert result == []


# ---------------------------------------------------------------------------
# Star-aware limit
# ---------------------------------------------------------------------------


class TestPlanReviewLimit:
    @pytest.mark.parametrize(
        ("plan", "stars", "limit", "eligible"),
        [
            ("free", None, 1, False),
            ("free", 3, 1, False),
            ("free", 9, 1, False),
            ("free", 10, 4, True),
            ("free", 5000, 4, True),
            ("oss", 5, 1, False),
            ("oss", 10, 1, True),
            ("oss", 25, 2, True),
            ("oss", 99, 9, True),
            ("oss", 100, 10, True),
            ("oss", 5000, 10, True),
            ("oss", None, 1, False),
            ("pro", None, 5, True),
            ("pro", 3, 5, True),
            ("pro_plus", None, 10, True),
        ],
    )
    def test_limit_and_eligibility(self, plan: str, stars: int | None, limit: int, eligible: bool) -> None:
        from sova.supervisor.coderabbit_quota import auto_review_eligible, plan_review_limit

        assert plan_review_limit(plan, stars) == limit
        assert auto_review_eligible(plan, stars) is eligible


class TestStarAwareStatus:
    async def _status(self, cfg: CodeRabbitQuotaConfig, repo: str = "owner/repo"):  # noqa: ANN202 (test helper)
        from sova.db.session import get_session
        from sova.supervisor.coderabbit_quota import get_quota_status

        async with await get_session() as session:
            return await get_quota_status(session, cfg, repo=repo)

    async def test_sync_caches_the_star_count_and_status_uses_it(self) -> None:
        from sova.db.session import get_session
        from sova.supervisor.coderabbit_quota import sync_from_github

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        with (
            patch("sova.supervisor.coderabbit_quota._fetch_star_count", new_callable=AsyncMock, return_value=12),
            patch(
                "sova.supervisor.coderabbit_quota._fetch_coderabbit_reviews_from_github",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            async with await get_session() as session:
                await sync_from_github(session, "owner/repo", cfg)

        status = await self._status(cfg)
        assert (status.stars, status.reviews_per_hour, status.auto_review_eligible) == (12, 4, True)
        assert status.limit_from_10_stars == 4

    async def test_below_10_stars_is_not_eligible_and_limited_to_one(self) -> None:
        from sova.supervisor.coderabbit_quota import _stars_cache

        _stars_cache["owner/repo"] = 3
        status = await self._status(CodeRabbitQuotaConfig(enabled=True, plan="free"))
        assert (status.stars, status.reviews_per_hour, status.auto_review_eligible) == (3, 1, False)

    async def test_unknown_stars_take_the_below_10_limit(self) -> None:
        status = await self._status(CodeRabbitQuotaConfig(enabled=True, plan="oss"))
        assert (status.stars, status.reviews_per_hour, status.auto_review_eligible) == (None, 1, False)

    async def test_failed_fetch_clears_a_known_count_and_never_raises_the_limit(self) -> None:
        from sova.db.session import get_session
        from sova.supervisor.coderabbit_quota import _stars_cache, sync_from_github

        _stars_cache["owner/repo"] = 500
        cfg = CodeRabbitQuotaConfig(enabled=True, plan="oss")
        with patch(
            "sova.supervisor.coderabbit_quota._fetch_coderabbit_reviews_from_github",
            new_callable=AsyncMock,
            return_value=[],
        ):
            async with await get_session() as session:
                await sync_from_github(session, "owner/repo", cfg, force=True)

        status = await self._status(cfg)
        assert (status.stars, status.reviews_per_hour) == (None, 1)

    @pytest.mark.parametrize("explicit", [8, 0])
    async def test_explicit_limit_wins_over_the_star_rule(self, explicit: int) -> None:
        from sova.supervisor.coderabbit_quota import _stars_cache

        _stars_cache["owner/repo"] = 3
        status = await self._status(CodeRabbitQuotaConfig(enabled=True, plan="free", reviews_per_hour=explicit))
        assert status.reviews_per_hour == explicit
        assert status.auto_review_eligible is False
        if explicit == 0:
            assert status.can_create_pr is True
            assert status.next_available_minutes is None

    async def test_a_process_that_never_synced_reads_the_stars_for_the_gate(self) -> None:
        """The gate runs in processes that never synced (a spawned run, a server just restarted): an empty cache
        must not drop a 10+-star free repo to one an hour once the count is fetchable."""
        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        with patch("sova.supervisor.coderabbit_quota._fetch_star_count", new_callable=AsyncMock, return_value=40) as f:
            status = await self._status(cfg)
            again = await self._status(cfg)
        assert (status.stars, status.reviews_per_hour, status.auto_review_eligible) == (40, 4, True)
        assert again.reviews_per_hour == 4
        assert f.await_count == 1, "the count is cached for the process"

    async def test_a_failed_star_read_is_not_repeated_within_the_backoff(self) -> None:
        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        with patch(
            "sova.supervisor.coderabbit_quota._fetch_star_count", new_callable=AsyncMock, return_value=None
        ) as f:
            first = await self._status(cfg)
            await self._status(cfg)
        assert (first.stars, first.reviews_per_hour) == (None, 1), "a failure never yields a larger limit"
        assert f.await_count == 1

    async def test_concurrent_gate_checks_share_one_star_read(self) -> None:
        import asyncio

        cfg = CodeRabbitQuotaConfig(enabled=True, plan="free")
        release = asyncio.Event()

        async def slow_fetch(*_args: object, **_kwargs: object) -> int:
            await release.wait()
            return 12

        with patch(
            "sova.supervisor.coderabbit_quota._fetch_star_count", new_callable=AsyncMock, side_effect=slow_fetch
        ) as f:
            tasks = [asyncio.create_task(self._status(cfg)) for _ in range(4)]
            # The first read is now parked on the event; give the other three time to reach the lock behind it.
            while f.await_count == 0:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
            release.set()
            results = await asyncio.gather(*tasks)
        assert f.await_count == 1
        assert [r.stars for r in results] == [12] * 4

    @pytest.mark.parametrize(
        "cfg",
        [
            CodeRabbitQuotaConfig(enabled=True, plan="free", reviews_per_hour=8),
            CodeRabbitQuotaConfig(enabled=True, plan="pro"),
            CodeRabbitQuotaConfig(enabled=False, plan="free"),
        ],
        ids=["explicit", "paid", "disabled"],
    )
    async def test_no_star_read_when_the_stars_cannot_change_the_limit(self, cfg: CodeRabbitQuotaConfig) -> None:
        with patch("sova.supervisor.coderabbit_quota._fetch_star_count", new_callable=AsyncMock) as f:
            await self._status(cfg)
        f.assert_not_awaited()

    async def test_status_says_when_the_limit_is_the_operators_and_when_it_scales(self) -> None:
        explicit = await self._status(CodeRabbitQuotaConfig(enabled=True, plan="free", reviews_per_hour=8))
        assert (explicit.limit_is_explicit, explicit.limit_scales_with_stars) == (True, False)
        oss = await self._status(CodeRabbitQuotaConfig(enabled=True, plan="oss"))
        assert (oss.limit_is_explicit, oss.limit_scales_with_stars) == (False, True)

    async def test_the_star_read_feeds_the_github_rate_limit_tracker(self) -> None:
        result = ShellResult(returncode=0, stdout="5\n", stderr="")
        with (
            patch("sova.supervisor.coderabbit_quota.run", new_callable=AsyncMock, return_value=result),
            # resolve_gh_env is imported inside _fetch_star_count, so patch its source module.
            patch("sova.utils.gh.resolve_gh_env", new_callable=AsyncMock, return_value=None),
            patch("sova.supervisor.github_quota.track_rate_limit") as track,
        ):
            assert await _REAL_FETCH_STAR_COUNT("owner/repo", github_user="bot") == 5
        track.assert_called_once_with(result, "bot")

    async def test_pro_is_eligible_without_stars(self) -> None:
        status = await self._status(CodeRabbitQuotaConfig(enabled=False, plan="pro"))
        assert (status.reviews_per_hour, status.auto_review_eligible) == (5, True)


class TestFetchStarCount:
    @pytest.mark.parametrize(
        ("result", "expected"),
        [
            (ShellResult(returncode=0, stdout="42\n", stderr=""), 42),
            (ShellResult(returncode=1, stdout="", stderr="rate limited"), None),
            (ShellResult(returncode=0, stdout="not a number", stderr=""), None),
        ],
    )
    async def test_parses_gh_output(self, result: ShellResult, expected: int | None) -> None:
        with patch("sova.supervisor.coderabbit_quota.run", new_callable=AsyncMock, return_value=result) as run:
            assert await _REAL_FETCH_STAR_COUNT("owner/repo") == expected
        assert run.await_args.args[:3] == ("gh", "api", "repos/owner/repo")

    async def test_gh_missing_is_unknown(self) -> None:
        with patch("sova.supervisor.coderabbit_quota.run", new_callable=AsyncMock, side_effect=OSError("no gh")):
            assert await _REAL_FETCH_STAR_COUNT("owner/repo") is None


class TestBackgroundSync:
    async def test_stale_get_starts_one_sync_for_two_quick_requests(self) -> None:
        import asyncio
        from pathlib import Path

        from sova.supervisor.coderabbit_quota import _background_syncs, _sync_attempted, start_background_sync

        release = asyncio.Event()
        calls: list[str] = []

        async def _slow_sync(*args: object, **kwargs: object) -> int:
            calls.append("sync")
            await release.wait()
            return 0

        cfg = CodeRabbitQuotaConfig(enabled=True)
        with patch("sova.supervisor.coderabbit_quota.sync_from_github", side_effect=_slow_sync):
            first = start_background_sync("owner/repo", cfg, project_dir=Path("."))
            second = start_background_sync("owner/repo", cfg, project_dir=Path("."))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(*_background_syncs.values())
            await asyncio.sleep(0)
            _sync_attempted.clear()  # past the retry backoff
            third = start_background_sync("owner/repo", cfg, project_dir=Path("."))
            await asyncio.gather(*_background_syncs.values())

        assert (first, second, third) == (True, False, True)
        assert calls == ["sync", "sync"]
        assert _background_syncs == {}

    async def test_a_failed_sync_is_not_retried_within_the_backoff(self) -> None:
        """A failed sync caches nothing, so the count stays stale: without a backoff every GET would start another."""
        import asyncio
        from pathlib import Path

        from sova.supervisor.coderabbit_quota import (
            _background_syncs,
            _sync_attempted,
            is_sync_stale,
            start_background_sync,
        )

        cfg = CodeRabbitQuotaConfig(enabled=True)
        with patch("sova.supervisor.coderabbit_quota.sync_from_github", new_callable=AsyncMock, return_value=0) as sync:
            first = start_background_sync("owner/repo", cfg, project_dir=Path("."))
            await asyncio.gather(*_background_syncs.values())
            await asyncio.sleep(0)
            assert is_sync_stale("owner/repo") is True, "the failed sync wrote no cache entry"
            second = start_background_sync("owner/repo", cfg, project_dir=Path("."))
            _sync_attempted["owner/repo"] -= 61
            third = start_background_sync("owner/repo", cfg, project_dir=Path("."))
            await asyncio.gather(*_background_syncs.values())

        assert (first, second, third) == (True, False, True)
        assert sync.await_count == 2

    async def test_no_sync_starts_while_github_is_rate_limited(self) -> None:
        from pathlib import Path

        from sova.supervisor.coderabbit_quota import start_background_sync

        tracker = AsyncMock()
        tracker.should_skip = lambda: True
        with patch("sova.supervisor.github_quota.get_github_quota_tracker", return_value=tracker):
            started = start_background_sync(
                "owner/repo", CodeRabbitQuotaConfig(enabled=True), project_dir=Path("."), github_user="bot"
            )
        assert started is False

    def test_is_sync_stale_follows_the_sync_cache(self) -> None:
        import time

        from sova.supervisor.coderabbit_quota import _SYNC_CACHE_TTL, _sync_cache, is_sync_stale

        assert is_sync_stale("owner/repo") is True
        _sync_cache["owner/repo"] = time.monotonic()
        assert is_sync_stale("owner/repo") is False
        _sync_cache["owner/repo"] = time.monotonic() - _SYNC_CACHE_TTL - 1
        assert is_sync_stale("owner/repo") is True


class TestQuotaEndpointRules:
    async def _get(self, cfg: object) -> dict:
        from httpx import ASGITransport, AsyncClient

        from sova.dashboard.app import create_app

        app = create_app()
        with patch("sova.dashboard.routers.quota.load_config", return_value=cfg):
            async with AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
            ) as client:
                client.headers.update(_csrf_request_headers(client))
                resp = await client.get("/api/quota/coderabbit")
        assert resp.status_code == 200
        return resp.json()

    async def test_configured_follows_the_tool_list_even_when_tracking_is_off(self) -> None:
        from sova.config.models import ExternalReviewsConfig, ProjectConfig

        listed = ProjectConfig(external_reviews=ExternalReviewsConfig(tools=["sonarcloud", "coderabbit"]))
        assert (await self._get(listed)) == {"enabled": False, "configured": True}
        assert (await self._get(ProjectConfig()))["configured"] is False

    async def test_status_carries_the_star_fields_and_a_stale_get_syncs_in_the_background(self) -> None:
        from sova.config.models import ExternalReviewsConfig, ProjectConfig
        from sova.supervisor.coderabbit_quota import _stars_cache

        _stars_cache["owner/repo"] = 3
        cfg = ProjectConfig(
            github_repo="owner/repo",
            coderabbit_quota=CodeRabbitQuotaConfig(enabled=True, plan="free"),
            external_reviews=ExternalReviewsConfig(tools=["coderabbit"]),
        )
        with patch("sova.dashboard.routers.quota.start_background_sync") as start:
            data = await self._get(cfg)

        assert data["configured"] is True
        assert (data["stars"], data["auto_review_eligible"], data["reviews_per_hour"]) == (3, False, 1)
        assert data["limit_from_10_stars"] == 4
        start.assert_called_once()
        assert start.call_args.args[0] == "owner/repo"

    async def test_an_unconfigured_project_starts_no_background_sync(self) -> None:
        """CodeRabbit is not a listed review tool, so the tile is hidden and nobody reads the figures."""
        from sova.config.models import ProjectConfig

        cfg = ProjectConfig(github_repo="owner/repo", coderabbit_quota=CodeRabbitQuotaConfig(enabled=True))
        with patch("sova.dashboard.routers.quota.start_background_sync") as start:
            data = await self._get(cfg)
        assert data["configured"] is False
        start.assert_not_called()

    async def test_the_get_never_reads_stars_from_github(self) -> None:
        """The status read answers from the cache: the background sync, not the request, fetches the stars."""
        from sova.config.models import ExternalReviewsConfig, ProjectConfig

        cfg = ProjectConfig(
            github_repo="owner/repo",
            coderabbit_quota=CodeRabbitQuotaConfig(enabled=True),
            external_reviews=ExternalReviewsConfig(tools=["coderabbit"]),
        )
        with (
            patch("sova.dashboard.routers.quota.start_background_sync"),
            patch("sova.supervisor.coderabbit_quota._fetch_star_count", new_callable=AsyncMock) as fetch,
        ):
            data = await self._get(cfg)
        fetch.assert_not_awaited()
        assert (data["stars"], data["limit_is_explicit"], data["limit_scales_with_stars"]) == (None, False, False)

    async def test_a_fresh_sync_starts_no_background_sync(self) -> None:
        import time

        from sova.config.models import ExternalReviewsConfig, ProjectConfig
        from sova.supervisor.coderabbit_quota import _sync_cache

        _sync_cache["owner/repo"] = time.monotonic()
        cfg = ProjectConfig(
            github_repo="owner/repo",
            coderabbit_quota=CodeRabbitQuotaConfig(enabled=True),
            external_reviews=ExternalReviewsConfig(tools=["coderabbit"]),
        )
        with patch("sova.dashboard.routers.quota.start_background_sync") as start:
            data = await self._get(cfg)
        start.assert_not_called()
        assert data["refreshing"] is False

    async def test_a_stale_sync_is_reported_as_refreshing(self) -> None:
        from sova.config.models import ExternalReviewsConfig, ProjectConfig
        from sova.supervisor.coderabbit_quota import _sync_cache

        _sync_cache.pop("owner/repo", None)
        cfg = ProjectConfig(
            github_repo="owner/repo",
            coderabbit_quota=CodeRabbitQuotaConfig(enabled=True),
            external_reviews=ExternalReviewsConfig(tools=["coderabbit"]),
        )
        with patch("sova.dashboard.routers.quota.start_background_sync") as start:
            data = await self._get(cfg)
        start.assert_called_once()
        assert data["refreshing"] is True

    async def test_an_unloadable_config_answers_503_not_500(self) -> None:
        from httpx import ASGITransport, AsyncClient

        from sova.dashboard.app import create_app

        app = create_app()
        with patch("sova.dashboard.routers.quota.load_config", side_effect=OSError("disk")):
            async with AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
            ) as client:
                client.headers.update(_csrf_request_headers(client))
                resp = await client.get("/api/quota/coderabbit")
        assert resp.status_code == 503
