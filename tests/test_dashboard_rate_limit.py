"""Tests for dashboard.rate_limit_per_minute on included-router routes (issue #1518)."""

from __future__ import annotations

import importlib
import pkgutil
import typing
from pathlib import Path

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from sova.dashboard.rate_limit import (
    API_RATE_LIMITER_STATE,
    STREAM_ROUTE_PATHS,
    ApiRateLimiter,
    rate_limit_dependency,
    rate_limit_key,
)

_LIMIT = 3


def _real_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: int = _LIMIT) -> FastAPI:
    from sova.dashboard.app import create_app

    monkeypatch.setenv("SOVA_DASHBOARD_RATE_LIMIT_PER_MINUTE", str(limit))
    monkeypatch.setattr("sova.dashboard.services.agent_pool._default_project_dir", None)
    return create_app(project_dir=tmp_path, host="127.0.0.1", port=8111)


def _client(app: FastAPI, address: str = "10.0.0.1") -> TestClient:
    return TestClient(app, client=(address, 50000))


@pytest.mark.usefixtures("reset_db_rows")
class TestRealAppLimits:
    @pytest.mark.parametrize(
        "path",
        [
            pytest.param("/api/agents/active", id="included-router-read"),
            pytest.param("/healthz", id="app-level-route"),
        ],
    )
    def test_request_past_the_limit_gets_429(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str) -> None:
        client = _client(_real_app(tmp_path, monkeypatch))
        statuses = [client.get(path).status_code for _ in range(_LIMIT)]
        assert 429 not in statuses
        response = client.get(path)
        assert response.status_code == 429

    def test_429_carries_retry_after(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _client(_real_app(tmp_path, monkeypatch))
        for _ in range(_LIMIT):
            client.get("/api/agents/active")
        response = client.get("/api/agents/active")
        assert response.status_code == 429
        assert 1 <= int(response.headers["retry-after"]) <= 60

    def test_one_client_exhausting_its_budget_does_not_throttle_another(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _real_app(tmp_path, monkeypatch)
        phone, laptop = _client(app, "100.64.0.1"), _client(app, "100.64.0.2")
        for _ in range(_LIMIT + 1):
            phone.get("/api/agents/active")
        assert phone.get("/api/agents/active").status_code == 429
        assert laptop.get("/api/agents/active").status_code == 200

    def test_each_route_has_its_own_budget(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _client(_real_app(tmp_path, monkeypatch))
        for _ in range(_LIMIT + 1):
            client.get("/api/agents/active")
        assert client.get("/api/runs").status_code == 200

    def test_path_parameters_share_the_route_budget(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _client(_real_app(tmp_path, monkeypatch))
        statuses = [client.get(f"/api/runs/{run_id}").status_code for run_id in range(1, _LIMIT + 2)]
        assert 429 not in statuses[:_LIMIT]
        assert statuses[-1] == 429

    def test_csrf_rejected_request_still_counts(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _client(_real_app(tmp_path, monkeypatch))
        statuses = [client.post("/api/queue/batch", json={}).status_code for _ in range(_LIMIT + 1)]
        assert statuses[:_LIMIT] == [403] * _LIMIT
        assert statuses[-1] == 429

    def test_static_files_are_exempt(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _client(_real_app(tmp_path, monkeypatch))
        statuses = {client.get("/static/app.js").status_code for _ in range(_LIMIT + 2)}
        assert statuses == {200}

    def test_zero_disables_limiting(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        app = _real_app(tmp_path, monkeypatch, limit=0)
        assert getattr(app.state, API_RATE_LIMITER_STATE, None) is None
        client = _client(app)
        statuses = {client.get("/api/agents/active").status_code for _ in range(65)}
        assert statuses == {200}

    def test_scheduler_routes_added_by_the_server_are_limited(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sova.config.models import ProjectConfig
        from sova.scheduler.server import SOVAServer

        monkeypatch.setenv("SOVA_DASHBOARD_RATE_LIMIT_PER_MINUTE", str(_LIMIT))
        monkeypatch.setattr("sova.dashboard.services.agent_pool._default_project_dir", None)
        server = SOVAServer(config=ProjectConfig(github_repo="owner/repo"), project_dir=tmp_path)
        client = _client(server.create_app())
        statuses = [client.get("/api/scheduler/status").status_code for _ in range(_LIMIT + 1)]
        assert statuses == [200] * _LIMIT + [429]

    def test_limiter_failure_lets_the_request_through(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def _broken_hit(self: ApiRateLimiter, client: str, bucket: str) -> int | None:
            raise RuntimeError("storage down")

        monkeypatch.setattr(ApiRateLimiter, "hit", _broken_hit)
        client = _client(_real_app(tmp_path, monkeypatch))
        with capture_logs() as logs:
            statuses = {client.get("/api/agents/active").status_code for _ in range(_LIMIT + 1)}
        assert statuses == {200}
        failures = [entry for entry in logs if entry["event"] == "dashboard.rate_limit_check_failed"]
        assert len(failures) == _LIMIT + 1
        assert {entry["log_level"] for entry in failures} == {"warning"}

    def test_a_parameterized_route_named_stream_is_still_limited(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`/api/roles/stream` matches `/roles/{name}`, which is not an SSE route."""
        client = _client(_real_app(tmp_path, monkeypatch))
        statuses = [client.get("/api/roles/stream").status_code for _ in range(_LIMIT + 1)]
        assert 429 not in statuses[:_LIMIT]
        assert statuses[-1] == 429


@pytest.mark.usefixtures("reset_db_rows")
class TestMultiProjectLimits:
    def test_project_scoped_and_fleet_manager_routes_are_limited(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sova.config.registry import register_project, unregister_project
        from sova.dashboard.app import create_app

        monkeypatch.setenv("SOVA_DASHBOARD_RATE_LIMIT_PER_MINUTE", str(_LIMIT))
        monkeypatch.setattr("sova.dashboard.services.agent_pool._default_project_dir", None)
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        slug = register_project(project_dir)
        try:
            client = _client(create_app(multi_project=True, host="127.0.0.1", port=8111))
            scoped = [client.get(f"/p/{slug}/api/agents/active").status_code for _ in range(_LIMIT + 1)]
            assert 429 not in scoped[:_LIMIT]
            assert scoped[-1] == 429
            assert client.get("/api/agents/active").status_code != 429
            fleet = [client.get("/api/fleet-manager/status").status_code for _ in range(_LIMIT + 1)]
            assert 429 not in fleet[:_LIMIT]
            assert fleet[-1] == 429
        finally:
            unregister_project(slug)


class TestRateLimitKey:
    @pytest.mark.parametrize(
        "path",
        [
            "/ws/agents/status",
            "/static/app.js",
        ],
    )
    def test_exempt_paths_have_no_key(self, path: str) -> None:
        request = Request({"type": "http", "method": "GET", "path": path, "headers": [], "client": ("1.2.3.4", 1)})
        assert rate_limit_key(request) is None

    def test_other_paths_are_keyed_by_client_address(self) -> None:
        request = Request(
            {"type": "http", "method": "GET", "path": "/api/agents/active", "headers": [], "client": ("1.2.3.4", 1)}
        )
        assert rate_limit_key(request) == "1.2.3.4"


def test_two_prefixes_of_one_router_do_not_share_a_budget() -> None:
    """Multi-project mode registers every router under both `/p/{slug}/api` and `/api`."""
    router = APIRouter()

    @router.get("/ping")
    def _ping() -> dict:
        return {"ok": True}

    app = FastAPI()
    setattr(app.state, API_RATE_LIMITER_STATE, ApiRateLimiter(_LIMIT))
    for prefix in ("/p/{slug}/api", "/api"):
        app.include_router(router, prefix=prefix, dependencies=[Depends(rate_limit_dependency(prefix))])
    client = _client(app)
    for _ in range(_LIMIT + 1):
        client.get("/p/sova/api/ping")
    assert client.get("/p/other/api/ping").status_code == 429
    assert client.get("/api/ping").status_code == 200


def _limited_app(router: APIRouter, prefix: str = "/api") -> FastAPI:
    app = FastAPI()
    setattr(app.state, API_RATE_LIMITER_STATE, ApiRateLimiter(_LIMIT))
    app.include_router(router, prefix=prefix, dependencies=[Depends(rate_limit_dependency(prefix))])
    return app


@pytest.mark.parametrize("prefix", ["/api", "/p/{slug}/api"])
def test_stream_routes_are_exempt_by_template(prefix: str) -> None:
    router = APIRouter()

    @router.get("/feed/stream")
    def _feed() -> dict:
        return {"ok": True}

    @router.get("/agents/{run_id}/output/stream")
    def _output(run_id: int) -> dict:
        return {"run_id": run_id}

    client = _client(_limited_app(router, prefix))
    base = prefix.replace("{slug}", "sova")
    for path in (f"{base}/feed/stream", f"{base}/agents/7/output/stream"):
        assert {client.get(path).status_code for _ in range(_LIMIT + 2)} == {200}


def test_every_streaming_route_is_listed_as_exempt() -> None:
    """A new SSE route must be added to STREAM_ROUTE_PATHS, or its reconnects count against the budget."""
    import sova.dashboard.routers as routers_pkg

    streaming: set[str] = set()
    for info in pkgutil.iter_modules(routers_pkg.__path__):
        module = importlib.import_module(f"{routers_pkg.__name__}.{info.name}")
        for router in (value for value in vars(module).values() if isinstance(value, APIRouter)):
            for route in router.routes:
                if not isinstance(route, APIRoute):
                    continue
                returns = typing.get_type_hints(route.endpoint).get("return")
                if isinstance(returns, type) and issubclass(returns, StreamingResponse):
                    streaming.add(route.path)
    assert streaming
    assert streaming <= STREAM_ROUTE_PATHS


async def test_route_without_a_path_template_is_let_through_with_one_warning() -> None:
    """No resolvable route must not pool every route under the prefix into one bucket."""
    app = FastAPI()
    setattr(app.state, API_RATE_LIMITER_STATE, ApiRateLimiter(_LIMIT))
    dependency = rate_limit_dependency("/api")
    scope = {"type": "http", "method": "GET", "path": "/api/ping", "headers": [], "client": ("1.2.3.4", 1), "app": app}
    with capture_logs() as logs:
        for _ in range(_LIMIT + 2):
            await dependency(Request(scope))
    warnings = [entry for entry in logs if entry["event"] == "dashboard.rate_limit_route_unresolved"]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
