"""Tests for fleet propose-issue feature: draft builder, label map, and API endpoints."""

from __future__ import annotations

import os
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from sova.dashboard.routers.fleet_insights import _get_fleet_service
from sova.dashboard.services.fleet_service import (
    STEP_AREA_MAP,
    FailureCluster,
    FleetInsights,
    StepFailureStat,
    build_issue_draft,
)
from sova.db.session import close_db, init_db


@pytest.fixture(autouse=True)
async def setup_db():
    os.environ["SOVA_DATABASE_URL"] = "sqlite+aiosqlite://"
    await init_db(run_migrations=False)
    yield
    await close_db()
    os.environ.pop("SOVA_DATABASE_URL", None)


def _make_insights(
    *,
    steps: list[StepFailureStat] | None = None,
    clusters: list[FailureCluster] | None = None,
) -> FleetInsights:
    return FleetInsights(
        generated_at=1700000000.0,
        projects_scanned=["alpha", "beta"],
        projects_skipped=[],
        total_runs=100,
        total_cost_usd=Decimal("12.50"),
        success_rate=0.85,
        retry_success_rate=0.6,
        step_failure_stats=steps or [],
        failure_clusters=clusters or [],
        cost_by_project=[],
    )


@pytest.fixture
async def client():
    from sova.dashboard.app import create_app

    app = create_app(multi_project=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def override_fleet(client: AsyncClient):
    app = client._transport.app  # type: ignore[union-attr]

    def _set(insights: FleetInsights) -> AsyncMock:
        mock_svc = AsyncMock()
        mock_svc.get_insights = AsyncMock(return_value=insights)
        app.dependency_overrides[_get_fleet_service] = lambda: mock_svc
        return mock_svc

    yield _set
    app.dependency_overrides.pop(_get_fleet_service, None)


# ---------------------------------------------------------------------------
# STEP_AREA_MAP completeness
# ---------------------------------------------------------------------------


class TestStepAreaMap:
    def test_all_known_steps_covered(self) -> None:
        known_steps = {
            "address_external_findings",
            "address_review",
            "assess",
            "capture_baseline",
            "commit",
            "create_pr",
            "create_worktree",
            "develop",
            "ensure_worktree",
            "extract_memory",
            "fetch_task",
            "generate_tasks",
            "handoff_to_reviewer",
            "handoff_to_user",
            "monitor_ci",
            "push",
            "rearrange_commits",
            "rebase",
            "research",
            "resolve_external_reviews",
            "scan_project",
            "self_review",
            "simplify",
            "spec",
            "sync",
            "validate",
            "validate_tasks",
            "wait_for_external_reviews",
        }
        assert set(STEP_AREA_MAP.keys()) == known_steps

    def test_all_areas_are_valid(self) -> None:
        valid_areas = {"core", "adapters", "agent"}
        for step, area in STEP_AREA_MAP.items():
            assert area in valid_areas, f"{step} has invalid area: {area}"

    def test_unknown_step_not_in_map(self) -> None:
        assert "nonexistent_step" not in STEP_AREA_MAP


# ---------------------------------------------------------------------------
# build_issue_draft unit tests
# ---------------------------------------------------------------------------


class TestBuildIssueDraft:
    def test_returns_draft_for_known_step(self) -> None:
        steps = [StepFailureStat("monitor_ci", 100, 62, 0.62)]
        clusters = [FailureCluster("timeout after <N>s", 15, ["sova", "gwym"])]
        insights = _make_insights(steps=steps, clusters=clusters)

        draft = build_issue_draft("monitor_ci", insights)

        assert draft is not None
        assert "monitor_ci" in draft["title"]
        assert "62.0%" in draft["title"]
        assert "fix(core)" in draft["title"]
        assert "monitor_ci" in draft["body"]
        assert "type: bug" in draft["labels"]
        assert "area: core" in draft["labels"]

    def test_returns_none_for_unknown_step(self) -> None:
        insights = _make_insights(steps=[])
        assert build_issue_draft("nonexistent_step", insights) is None

    def test_area_adapters_for_create_pr(self) -> None:
        steps = [StepFailureStat("create_pr", 50, 5, 0.1)]
        insights = _make_insights(steps=steps)

        draft = build_issue_draft("create_pr", insights)

        assert draft is not None
        assert "area: adapters" in draft["labels"]
        assert "fix(adapters)" in draft["title"]

    def test_area_agent_for_rebase(self) -> None:
        steps = [StepFailureStat("rebase", 30, 9, 0.3)]
        insights = _make_insights(steps=steps)

        draft = build_issue_draft("rebase", insights)

        assert draft is not None
        assert "area: agent" in draft["labels"]

    def test_clusters_included_in_body(self) -> None:
        steps = [StepFailureStat("develop", 80, 16, 0.2)]
        clusters = [
            FailureCluster("Tests failed at <PATH>", 10, ["alpha"]),
            FailureCluster("Lint error in <PATH>", 5, ["beta"]),
        ]
        insights = _make_insights(steps=steps, clusters=clusters)

        draft = build_issue_draft("develop", insights)

        assert draft is not None
        assert "Tests failed" in draft["body"]
        assert "Lint error" in draft["body"]

    def test_empty_clusters_handled(self) -> None:
        steps = [StepFailureStat("sync", 40, 4, 0.1)]
        insights = _make_insights(steps=steps, clusters=[])

        draft = build_issue_draft("sync", insights)

        assert draft is not None
        assert "Suggested investigation" in draft["body"]

    def test_body_has_suggested_investigation(self) -> None:
        steps = [StepFailureStat("validate", 20, 2, 0.1)]
        insights = _make_insights(steps=steps)

        draft = build_issue_draft("validate", insights)

        assert draft is not None
        assert "Suggested investigation" in draft["body"]
        assert "validate.py" in draft["body"]
        assert "SOVA fleet self-improvement loop" in draft["body"]


# ---------------------------------------------------------------------------
# GET /issue-draft endpoint tests
# ---------------------------------------------------------------------------


class TestIssueDraftEndpoint:
    async def test_returns_draft_for_known_step(self, client: AsyncClient, override_fleet) -> None:
        steps = [StepFailureStat("monitor_ci", 100, 62, 0.62)]
        clusters = [FailureCluster("timeout", 10, ["sova"])]
        override_fleet(_make_insights(steps=steps, clusters=clusters))

        resp = await client.get("/api/fleet-insights/issue-draft?step_name=monitor_ci")

        assert resp.status_code == 200
        data = resp.json()
        assert "monitor_ci" in data["title"]
        assert isinstance(data["labels"], list)
        assert len(data["labels"]) == 2

    async def test_returns_404_for_unknown_step(self, client: AsyncClient, override_fleet) -> None:
        override_fleet(_make_insights(steps=[]))

        resp = await client.get("/api/fleet-insights/issue-draft?step_name=nonexistent")

        assert resp.status_code == 404
        assert "nonexistent" in resp.json()["detail"]

    async def test_returns_503_on_service_error(self, client: AsyncClient) -> None:
        mock_svc = AsyncMock()
        mock_svc.get_insights = AsyncMock(side_effect=RuntimeError("db down"))
        app = client._transport.app  # type: ignore[union-attr]
        app.dependency_overrides[_get_fleet_service] = lambda: mock_svc

        resp = await client.get("/api/fleet-insights/issue-draft?step_name=develop")

        assert resp.status_code == 503
        app.dependency_overrides.pop(_get_fleet_service, None)


# ---------------------------------------------------------------------------
# POST /propose-issue endpoint tests
# ---------------------------------------------------------------------------


class TestProposeIssueEndpoint:
    async def test_success_creates_issue(self, client: AsyncClient) -> None:
        from sova.adapters.base import Task

        mock_task = Task(id="433", title="test issue", url="https://github.com/xsovad06/sova/issues/433")

        with (
            patch("sova.adapters.github.GitHubAdapter") as mock_adapter_cls,
            patch("sova.dashboard.project_context.get_project_dir", return_value=None),
        ):
            mock_adapter = AsyncMock()
            mock_adapter.create_issue = AsyncMock(return_value=mock_task)
            mock_adapter_cls.return_value = mock_adapter

            resp = await client.post(
                "/api/fleet-insights/propose-issue",
                json={
                    "title": "fix(core): monitor_ci fails",
                    "body": "## Problem\n\nTest body",
                    "labels": ["type: bug", "area: core"],
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["issue_number"] == 433
        assert "github.com" in data["issue_url"]

    async def test_adapter_error_returns_structured_error(self, client: AsyncClient) -> None:
        with (
            patch("sova.adapters.github.GitHubAdapter") as mock_adapter_cls,
            patch("sova.dashboard.project_context.get_project_dir", return_value=None),
        ):
            mock_adapter = AsyncMock()
            mock_adapter.create_issue = AsyncMock(side_effect=RuntimeError("gh auth: not logged in"))
            mock_adapter_cls.return_value = mock_adapter

            resp = await client.post(
                "/api/fleet-insights/propose-issue",
                json={
                    "title": "test issue",
                    "body": "body",
                    "labels": [],
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert "not logged in" in data["error"]

    async def test_uses_config_when_project_dir_available(self, client: AsyncClient) -> None:
        from sova.adapters.base import Task

        mock_task = Task(id="500", title="x", url="https://github.com/xsovad06/sova/issues/500")
        mock_cfg = AsyncMock()
        mock_cfg.github_user = "testuser"
        mock_cfg.fleet.sova_repo = "custom/repo"

        with (
            patch("sova.adapters.github.GitHubAdapter") as mock_adapter_cls,
            patch("sova.dashboard.project_context.get_project_dir", return_value="/tmp/proj"),
            patch("sova.config.loader.load_config", return_value=mock_cfg),
        ):
            mock_adapter = AsyncMock()
            mock_adapter.create_issue = AsyncMock(return_value=mock_task)
            mock_adapter_cls.return_value = mock_adapter

            resp = await client.post(
                "/api/fleet-insights/propose-issue",
                json={
                    "title": "test",
                    "body": "body",
                    "labels": [],
                },
            )

            assert resp.status_code == 200
            mock_adapter_cls.assert_called_once_with(repo="custom/repo", github_user="testuser")

    async def test_error_message_truncated(self, client: AsyncClient) -> None:
        long_error = "x" * 1000

        with (
            patch("sova.adapters.github.GitHubAdapter") as mock_adapter_cls,
            patch("sova.dashboard.project_context.get_project_dir", return_value=None),
        ):
            mock_adapter = AsyncMock()
            mock_adapter.create_issue = AsyncMock(side_effect=RuntimeError(long_error))
            mock_adapter_cls.return_value = mock_adapter

            resp = await client.post(
                "/api/fleet-insights/propose-issue",
                json={
                    "title": "test",
                    "body": "body",
                    "labels": [],
                },
            )

        data = resp.json()
        assert data["ok"] is False
        assert len(data["error"]) <= 500
