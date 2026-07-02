"""Tests for CodeRabbit quota tracking -- config, DB model, service, and API."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from sova.config.models import CodeRabbitQuotaConfig
from sova.db.session import close_db, init_db
from sova.utils.shell import ShellResult


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
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
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
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
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
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
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
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/quota/coderabbit/sync")
        assert resp.status_code == 200
        data = resp.json()
        assert data["synced"] == 0
        assert "error" in data

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
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/quota/coderabbit/sync")
        assert resp.status_code == 500


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


class TestRecordEventDefaultTimestamp:
    async def test_default_recorded_at(self) -> None:
        """Line 148: recorded_at defaults to now when None."""
        from sova.db.session import get_session
        from sova.supervisor.coderabbit_quota import record_event

        async with await get_session() as session:
            result = await record_event(session, pr_number=99, event_type="review", review_id="auto_ts")
        assert result is True


class TestFetchCodeRabbitReviewsFromGitHub:
    async def test_pr_fetch_failure(self) -> None:
        """Lines 236-238: PR list fetch fails."""
        from sova.supervisor.coderabbit_quota import _fetch_coderabbit_reviews_from_github

        with patch("sova.supervisor.coderabbit_quota.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = ShellResult(returncode=1, stdout="", stderr="gh error")
            reviews = await _fetch_coderabbit_reviews_from_github("owner/repo")
        assert reviews == []

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
                return ShellResult(returncode=0, stdout="10\n20\n", stderr="")
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
                return ShellResult(returncode=0, stdout="10\n", stderr="")
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
