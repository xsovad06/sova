"""Tests for PR metrics service and API endpoints."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sova.db.models import PREvent
from sova.db.session import close_db, get_session, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


@pytest.fixture
async def session() -> AsyncSession:
    return await get_session()


@pytest.fixture
async def seed_events(session: AsyncSession):
    now = datetime.now(timezone.utc)
    events = [
        PREvent(
            pr_number=100,
            repo="owner/repo",
            event_type="opened",
            timestamp=now - timedelta(days=10),
            actor="alice",
            metadata_json={"additions": 150, "deletions": 30},
        ),
        PREvent(
            pr_number=100,
            repo="owner/repo",
            event_type="reviewed",
            timestamp=now - timedelta(days=9),
            actor="bob",
        ),
        PREvent(
            pr_number=100,
            repo="owner/repo",
            event_type="approved",
            timestamp=now - timedelta(days=8),
            actor="bob",
        ),
        PREvent(
            pr_number=100,
            repo="owner/repo",
            event_type="merged",
            timestamp=now - timedelta(days=7),
            actor="alice",
            metadata_json={"additions": 150, "deletions": 30},
        ),
        PREvent(
            pr_number=101,
            repo="owner/repo",
            event_type="opened",
            timestamp=now - timedelta(days=5),
            actor="alice",
            metadata_json={"additions": 80, "deletions": 20},
        ),
        PREvent(
            pr_number=101,
            repo="owner/repo",
            event_type="approved",
            timestamp=now - timedelta(days=4),
            actor="carol",
        ),
        PREvent(
            pr_number=101,
            repo="owner/repo",
            event_type="merged",
            timestamp=now - timedelta(days=3),
            actor="alice",
            metadata_json={"additions": 80, "deletions": 20},
        ),
    ]
    for ev in events:
        session.add(ev)
    await session.commit()


class TestPRMetricsService:
    async def test_empty_summary(self) -> None:
        from sova.dashboard.services.pr_metrics_service import PRMetricsService

        svc = PRMetricsService()
        result = await svc.get_summary(days=90)
        assert result["total_prs_merged"] == 0
        assert result["cycle_time_median_hours"] is None

    async def test_summary_with_data(self, seed_events: None) -> None:
        from sova.dashboard.services.pr_metrics_service import PRMetricsService

        svc = PRMetricsService()
        result = await svc.get_summary(days=90)
        assert result["total_prs_merged"] == 2
        assert result["cycle_time_median_hours"] is not None
        assert result["cycle_time_median_hours"] > 0
        assert result["total_additions"] == 230
        assert result["total_deletions"] == 50
        assert result["throughput_per_week"] > 0

    async def test_trends_with_data(self, seed_events: None) -> None:
        from sova.dashboard.services.pr_metrics_service import PRMetricsService

        svc = PRMetricsService()
        result = await svc.get_trends(days=90)
        assert len(result) >= 1
        month = result[0]
        assert "month" in month
        assert "merged_count" in month
        assert month["merged_count"] > 0

    async def test_author_stats_with_data(self, seed_events: None) -> None:
        from sova.dashboard.services.pr_metrics_service import PRMetricsService

        svc = PRMetricsService()
        result = await svc.get_author_stats(days=90)
        assert len(result) >= 1
        alice = next(a for a in result if a["login"] == "alice")
        assert alice["prs_merged"] == 2
        assert alice["additions"] == 230
        assert alice["deletions"] == 50

    async def test_empty_trends(self) -> None:
        from sova.dashboard.services.pr_metrics_service import PRMetricsService

        svc = PRMetricsService()
        result = await svc.get_trends(days=90)
        assert result == []

    async def test_empty_author_stats(self) -> None:
        from sova.dashboard.services.pr_metrics_service import PRMetricsService

        svc = PRMetricsService()
        result = await svc.get_author_stats(days=90)
        assert result == []


class TestPRMetricsAPI:
    @pytest.fixture
    def client(self):
        from sova.dashboard.app import create_app

        app = create_app(multi_project=False)
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")

    async def test_summary_endpoint(self, client: AsyncClient, seed_events: None) -> None:
        resp = await client.get("/api/prs/metrics/summary?days=90")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_prs_merged" in data
        assert data["total_prs_merged"] == 2

    async def test_trends_endpoint(self, client: AsyncClient, seed_events: None) -> None:
        resp = await client.get("/api/prs/metrics/trends?days=90")
        assert resp.status_code == 200
        data = resp.json()
        assert "monthly" in data

    async def test_by_author_endpoint(self, client: AsyncClient, seed_events: None) -> None:
        resp = await client.get("/api/prs/metrics/by-author?days=90")
        assert resp.status_code == 200
        data = resp.json()
        assert "authors" in data

    async def test_summary_empty(self, client: AsyncClient) -> None:
        from sova.dashboard.services import pr_metrics_service

        pr_metrics_service._service = pr_metrics_service.PRMetricsService()
        resp = await client.get("/api/prs/metrics/summary?days=90")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_prs_merged"] == 0

    async def test_days_validation(self, client: AsyncClient) -> None:
        resp = await client.get("/api/prs/metrics/summary?days=3")
        assert resp.status_code == 422

    async def test_pr_metrics_page(self, client: AsyncClient) -> None:
        resp = await client.get("/pr-metrics")
        assert resp.status_code == 200
        assert b"PR Metrics" in resp.content

    async def test_project_scoped_pr_metrics_page(self, tmp_path) -> None:
        from sova.config.registry import register_project, unregister_project
        from sova.dashboard.app import create_app

        app = create_app(multi_project=True)
        transport = ASGITransport(app=app)
        project_dir = tmp_path / "test-project"
        project_dir.mkdir()
        slug = register_project(project_dir)
        try:
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get(f"/p/{slug}/pr-metrics")
                assert resp.status_code == 200
                assert b"PR Metrics" in resp.content
        finally:
            unregister_project(slug)


class TestPREventModel:
    async def test_create_event(self, session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)
        event = PREvent(
            pr_number=42,
            repo="owner/repo",
            event_type="opened",
            timestamp=now,
            actor="testuser",
        )
        session.add(event)
        await session.commit()
        await session.refresh(event)
        assert event.id is not None
        assert event.pr_number == 42
        assert event.event_type == "opened"

    async def test_dedup_constraint(self, session: AsyncSession) -> None:
        from sqlalchemy.exc import IntegrityError

        now = datetime.now(timezone.utc)
        ev1 = PREvent(pr_number=42, repo="owner/repo", event_type="opened", timestamp=now)
        session.add(ev1)
        await session.commit()

        ev2 = PREvent(pr_number=42, repo="owner/repo", event_type="opened", timestamp=now)
        session.add(ev2)
        with pytest.raises(IntegrityError):
            await session.commit()


class TestPRMetricsExtractHelper:
    def test_extract_basic_pr(self) -> None:
        from sova.dashboard.services.pr_metrics_service import _extract_pr_events

        pr = {
            "number": 10,
            "author": {"login": "alice"},
            "createdAt": "2026-01-01T00:00:00Z",
            "mergedAt": "2026-01-02T00:00:00Z",
            "closedAt": None,
            "additions": 50,
            "deletions": 10,
            "changedFiles": 3,
            "headRefName": "feat/test",
            "title": "Test PR",
            "latestReviews": [],
        }
        cutoff = datetime(2025, 12, 1, tzinfo=timezone.utc)
        events = _extract_pr_events(pr, repo="o/r", cutoff=cutoff)
        types = [e["event_type"] for e in events]
        assert "opened" in types
        assert "merged" in types
        assert "closed" not in types
        assert events[0]["actor"] == "alice"
        assert events[0]["metadata_json"]["additions"] == 50

    def test_extract_closed_not_merged(self) -> None:
        from sova.dashboard.services.pr_metrics_service import _extract_pr_events

        pr = {
            "number": 11,
            "author": {"login": "bob"},
            "createdAt": "2026-02-01T00:00:00Z",
            "mergedAt": None,
            "closedAt": "2026-02-05T00:00:00Z",
            "additions": 10,
            "deletions": 5,
            "changedFiles": 1,
            "headRefName": "fix/thing",
            "title": "Closed PR",
            "latestReviews": [],
        }
        cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
        events = _extract_pr_events(pr, repo="o/r", cutoff=cutoff)
        types = [e["event_type"] for e in events]
        assert "opened" in types
        assert "closed" in types
        assert "merged" not in types

    def test_extract_with_reviews_and_approval(self) -> None:
        from sova.dashboard.services.pr_metrics_service import _extract_pr_events

        pr = {
            "number": 12,
            "author": {"login": "alice"},
            "createdAt": "2026-03-01T00:00:00Z",
            "mergedAt": "2026-03-05T00:00:00Z",
            "closedAt": None,
            "additions": 20,
            "deletions": 5,
            "changedFiles": 2,
            "headRefName": "feat/x",
            "title": "PR with reviews",
            "latestReviews": [
                {"submittedAt": "2026-03-03T00:00:00Z", "state": "CHANGES_REQUESTED", "author": {"login": "carol"}},
                {"submittedAt": "2026-03-02T00:00:00Z", "state": "APPROVED", "author": {"login": "dave"}},
                {"submittedAt": "2026-03-04T00:00:00Z", "state": "APPROVED", "author": {"login": "eve"}},
            ],
        }
        cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
        events = _extract_pr_events(pr, repo="o/r", cutoff=cutoff)
        approved_ev = [e for e in events if e["event_type"] == "approved"]
        assert len(approved_ev) == 1
        assert approved_ev[0]["actor"] == "dave"

    def test_extract_before_cutoff(self) -> None:
        from sova.dashboard.services.pr_metrics_service import _extract_pr_events

        pr = {
            "number": 13,
            "author": {"login": "alice"},
            "createdAt": "2025-01-01T00:00:00Z",
            "mergedAt": None,
            "closedAt": None,
            "additions": 0,
            "deletions": 0,
            "changedFiles": 0,
            "headRefName": "old",
            "title": "Old PR",
            "latestReviews": [],
        }
        cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
        events = _extract_pr_events(pr, repo="o/r", cutoff=cutoff)
        assert events == []

    def test_extract_missing_author(self) -> None:
        from sova.dashboard.services.pr_metrics_service import _extract_pr_events

        pr = {
            "number": 14,
            "author": {},
            "createdAt": "2026-06-01T00:00:00Z",
            "mergedAt": None,
            "closedAt": None,
            "additions": 0,
            "deletions": 0,
            "changedFiles": 0,
            "headRefName": "test",
            "title": "No author",
            "latestReviews": [],
        }
        cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
        events = _extract_pr_events(pr, repo="o/r", cutoff=cutoff)
        assert events[0]["actor"] == ""


class TestPRMetricsCacheAndEdgeCases:
    async def test_cache_hit_skips_recompute(self, seed_events: None) -> None:
        from sova.dashboard.services.pr_metrics_service import PRMetricsService

        svc = PRMetricsService()
        r1 = await svc.get_summary(days=90)
        r2 = await svc.get_summary(days=90)
        assert r1 == r2

    async def test_force_bypasses_cache(self, seed_events: None) -> None:
        from sova.dashboard.services.pr_metrics_service import PRMetricsService

        svc = PRMetricsService()
        r1 = await svc.get_summary(days=90)
        r2 = await svc.get_summary(days=90, force=True)
        assert r1["total_prs_merged"] == r2["total_prs_merged"]

    async def test_invalidate_clears_cache(self, seed_events: None) -> None:
        from sova.dashboard.services.pr_metrics_service import PRMetricsService

        svc = PRMetricsService()
        await svc.get_summary(days=90)
        svc.invalidate()
        assert svc._cache == {}

    async def test_summary_includes_closed_count(self, seed_events: None) -> None:
        now = datetime.now(timezone.utc)
        async with await get_session() as s:
            s.add(
                PREvent(
                    pr_number=200,
                    repo="owner/repo",
                    event_type="opened",
                    timestamp=now - timedelta(days=4),
                    actor="bob",
                    metadata_json={"additions": 10, "deletions": 2},
                )
            )
            s.add(
                PREvent(
                    pr_number=200,
                    repo="owner/repo",
                    event_type="closed",
                    timestamp=now - timedelta(days=2),
                    actor="bob",
                )
            )
            await s.commit()

        from sova.dashboard.services.pr_metrics_service import PRMetricsService

        svc = PRMetricsService()
        result = await svc.get_summary(days=90)
        assert result["total_prs_closed"] == 1
        assert result["total_prs_merged"] == 2

    async def test_churn_excludes_non_merged(self, seed_events: None) -> None:
        now = datetime.now(timezone.utc)
        async with await get_session() as s:
            s.add(
                PREvent(
                    pr_number=201,
                    repo="owner/repo",
                    event_type="opened",
                    timestamp=now - timedelta(days=3),
                    actor="bob",
                    metadata_json={"additions": 999, "deletions": 999},
                )
            )
            s.add(
                PREvent(
                    pr_number=201,
                    repo="owner/repo",
                    event_type="closed",
                    timestamp=now - timedelta(days=1),
                    actor="bob",
                )
            )
            await s.commit()

        from sova.dashboard.services.pr_metrics_service import PRMetricsService

        svc = PRMetricsService()
        result = await svc.get_summary(days=90)
        assert result["total_additions"] == 230
        assert result["total_deletions"] == 50

    async def test_reviewed_event_uses_earliest(self) -> None:
        from sova.dashboard.services.pr_metrics_service import PRMetricsService

        now = datetime.now(timezone.utc)
        async with await get_session() as s:
            s.add(
                PREvent(
                    pr_number=300,
                    repo="o/r",
                    event_type="opened",
                    timestamp=now - timedelta(days=10),
                    actor="a",
                )
            )
            s.add(
                PREvent(
                    pr_number=300,
                    repo="o/r",
                    event_type="reviewed",
                    timestamp=now - timedelta(days=8),
                    actor="b",
                )
            )
            s.add(
                PREvent(
                    pr_number=300,
                    repo="o/r",
                    event_type="reviewed",
                    timestamp=now - timedelta(days=9),
                    actor="c",
                )
            )
            s.add(
                PREvent(
                    pr_number=300,
                    repo="o/r",
                    event_type="merged",
                    timestamp=now - timedelta(days=5),
                    actor="a",
                )
            )
            await s.commit()

        svc = PRMetricsService()
        result = await svc.get_summary(days=90)
        assert result["wait_to_review_median_hours"] is not None
        assert result["wait_to_review_median_hours"] <= 24.1

    async def test_trends_includes_closed(self) -> None:
        from sova.dashboard.services.pr_metrics_service import PRMetricsService

        now = datetime.now(timezone.utc)
        async with await get_session() as s:
            s.add(PREvent(pr_number=400, repo="o/r", event_type="opened", timestamp=now - timedelta(days=5), actor="a"))
            s.add(PREvent(pr_number=400, repo="o/r", event_type="closed", timestamp=now - timedelta(days=2), actor="a"))
            await s.commit()

        svc = PRMetricsService()
        trends = await svc.get_trends(days=90)
        assert len(trends) >= 1
        has_closed = any(t.get("closed_count", 0) > 0 for t in trends)
        assert has_closed


class TestParseGhTimestamp:
    def test_valid_z_suffix(self) -> None:
        from sova.dashboard.services.pr_metrics_service import _parse_gh_ts

        result = _parse_gh_ts("2026-01-15T10:30:00Z")
        assert result is not None
        assert result.tzinfo is not None

    def test_valid_offset(self) -> None:
        from sova.dashboard.services.pr_metrics_service import _parse_gh_ts

        result = _parse_gh_ts("2026-01-15T10:30:00+00:00")
        assert result is not None

    def test_none_input(self) -> None:
        from sova.dashboard.services.pr_metrics_service import _parse_gh_ts

        assert _parse_gh_ts(None) is None

    def test_empty_string(self) -> None:
        from sova.dashboard.services.pr_metrics_service import _parse_gh_ts

        assert _parse_gh_ts("") is None

    def test_invalid_format(self) -> None:
        from sova.dashboard.services.pr_metrics_service import _parse_gh_ts

        assert _parse_gh_ts("not-a-date") is None


class TestEmptySummary:
    def test_empty_summary_has_all_keys(self) -> None:
        from sova.dashboard.services.pr_metrics_service import _empty_summary

        result = _empty_summary()
        assert result["total_prs_merged"] == 0
        assert result["total_prs_closed"] == 0
        assert result["total_additions"] == 0
        assert result["total_deletions"] == 0
        assert result["cycle_time_median_hours"] is None
        assert result["wait_to_review_median_hours"] is None
        assert result["wait_to_merge_median_hours"] is None
        assert result["throughput_per_week"] == 0
        assert result["reviewers_per_pr"] is None


class TestModuleLevelWrappers:
    async def test_get_summary_wrapper(self) -> None:
        from sova.dashboard.services import pr_metrics_service

        pr_metrics_service._service = pr_metrics_service.PRMetricsService()
        result = await pr_metrics_service.get_summary(days=30)
        assert result["total_prs_merged"] == 0

    async def test_get_trends_wrapper(self) -> None:
        from sova.dashboard.services import pr_metrics_service

        pr_metrics_service._service = pr_metrics_service.PRMetricsService()
        result = await pr_metrics_service.get_trends(days=30)
        assert result == []

    async def test_get_author_stats_wrapper(self) -> None:
        from sova.dashboard.services import pr_metrics_service

        pr_metrics_service._service = pr_metrics_service.PRMetricsService()
        result = await pr_metrics_service.get_author_stats(days=30)
        assert result == []


class TestStateTransitionRecording:
    def test_computed_to_event_mapping(self) -> None:
        from sova.dashboard.services.pr_service import _COMPUTED_TO_EVENT

        assert _COMPUTED_TO_EVENT["approved"] == "approved"
        assert _COMPUTED_TO_EVENT["approved_ci_green"] == "approved"
        assert _COMPUTED_TO_EVENT["changes_requested"] == "reviewed"
        assert "awaiting_review" not in _COMPUTED_TO_EVENT
        assert "draft" not in _COMPUTED_TO_EVENT

    async def test_record_state_transitions_per_event_isolation(self) -> None:
        from sova.dashboard.services.pr_service import _record_state_transitions

        prs = [
            {
                "number": 500,
                "computed_state": "approved",
                "updated_at": "2026-07-01T00:00:00+00:00",
                "author": "testuser",
            }
        ]
        from sova.dashboard.services.pr_service import _last_known_states

        _last_known_states[500] = "awaiting_review"
        await _record_state_transitions(prs, repo="o/r")
        _last_known_states.pop(500, None)

    async def test_record_state_no_transition(self) -> None:
        from sova.dashboard.services.pr_service import _last_known_states, _record_state_transitions

        _last_known_states[501] = "approved"
        prs = [{"number": 501, "computed_state": "approved", "updated_at": "", "author": "u"}]
        await _record_state_transitions(prs, repo="o/r")
        _last_known_states.pop(501, None)

    async def test_record_state_first_seen(self) -> None:
        from sova.dashboard.services.pr_service import _last_known_states, _record_state_transitions

        _last_known_states.pop(502, None)
        prs = [{"number": 502, "computed_state": "approved", "updated_at": "", "author": "u"}]
        await _record_state_transitions(prs, repo="o/r")
        assert _last_known_states[502] == "approved"
        _last_known_states.pop(502, None)

    async def test_record_state_unmapped_transition(self) -> None:
        from sova.dashboard.services.pr_service import _last_known_states, _record_state_transitions

        _last_known_states[503] = "approved"
        prs = [{"number": 503, "computed_state": "draft", "updated_at": "", "author": "u"}]
        await _record_state_transitions(prs, repo="o/r")
        _last_known_states.pop(503, None)
