"""Tests for sova.dashboard.security: origin/CSRF guard for state-changing endpoints."""

from __future__ import annotations

import time
from collections import defaultdict, deque

import pytest
from fastapi import Depends, FastAPI, Request, Response
from fastapi.testclient import TestClient

from sova.dashboard.security import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    CSRF_REJECTED_DETAIL,
    CSRF_TOKEN_MAX_AGE_SECONDS,
    build_allowed_hosts,
    build_allowed_origins,
    is_loopback_host,
    is_strong_csrf_secret,
    is_wildcard_host,
    issue_csrf_cookie,
    require_same_origin_csrf,
    sign_csrf_token,
    validate_origin,
    verify_signed_csrf_token,
)


class TestIsLoopbackHost:
    def test_127_0_0_1_is_loopback(self) -> None:
        assert is_loopback_host("127.0.0.1") is True

    def test_localhost_is_loopback(self) -> None:
        assert is_loopback_host("localhost") is True

    def test_ipv6_loopback_is_loopback(self) -> None:
        assert is_loopback_host("::1") is True

    def test_0_0_0_0_is_not_loopback(self) -> None:
        assert is_loopback_host("0.0.0.0") is False

    def test_remote_host_is_not_loopback(self) -> None:
        assert is_loopback_host("192.168.1.5") is False

    def test_bracketed_ipv6_loopback_is_loopback(self) -> None:
        """`--host [::1]` is a legal spelling and must not fail closed."""
        assert is_loopback_host("[::1]") is True

    def test_other_127_addresses_are_loopback(self) -> None:
        """The whole 127.0.0.0/8 block is loopback, not just 127.0.0.1."""
        assert is_loopback_host("127.0.0.2") is True
        assert is_loopback_host("127.1.2.3") is True

    def test_unparseable_host_is_not_loopback(self) -> None:
        assert is_loopback_host("not-an-ip") is False


class TestIsWildcardHost:
    def test_ipv4_wildcard_is_wildcard(self) -> None:
        assert is_wildcard_host("0.0.0.0") is True

    def test_ipv6_wildcard_is_wildcard(self) -> None:
        assert is_wildcard_host("::") is True

    def test_bracketed_ipv6_wildcard_is_wildcard(self) -> None:
        assert is_wildcard_host("[::]") is True

    def test_loopback_is_not_wildcard(self) -> None:
        assert is_wildcard_host("127.0.0.1") is False

    def test_remote_host_is_not_wildcard(self) -> None:
        assert is_wildcard_host("192.168.1.5") is False

    def test_unparseable_host_is_not_wildcard(self) -> None:
        assert is_wildcard_host("not-an-ip") is False


class TestBuildAllowedOrigins:
    def test_loopback_bind_accepts_all_loopback_spellings(self) -> None:
        origins = build_allowed_origins("127.0.0.1", 8111)
        assert "http://127.0.0.1:8111" in origins
        assert "http://localhost:8111" in origins
        assert "http://[::1]:8111" in origins

    def test_loopback_bind_includes_https_variants(self) -> None:
        origins = build_allowed_origins("localhost", 8111)
        assert "https://127.0.0.1:8111" in origins
        assert "https://localhost:8111" in origins

    def test_non_loopback_bind_only_accepts_configured_host(self) -> None:
        origins = build_allowed_origins("192.168.1.5", 8111)
        assert origins == frozenset({"http://192.168.1.5:8111", "https://192.168.1.5:8111"})

    def test_non_loopback_bind_excludes_loopback_hosts(self) -> None:
        origins = build_allowed_origins("192.168.1.5", 8111)
        assert "http://127.0.0.1:8111" not in origins
        assert "http://localhost:8111" not in origins

    def test_bracketed_ipv6_is_not_double_bracketed(self) -> None:
        """`[::1]` must normalize to one bracket pair, else no Origin can ever match."""
        origins = build_allowed_origins("[::1]", 8111)
        assert "http://[::1]:8111" in origins
        assert "http://[[::1]]:8111" not in origins

    def test_loopback_bind_includes_the_configured_host_itself(self) -> None:
        """A 127.0.0.2 bind must accept its own Origin, not only canonical spellings."""
        origins = build_allowed_origins("127.0.0.2", 8111)
        assert "http://127.0.0.2:8111" in origins
        assert "http://127.0.0.1:8111" in origins

    def test_port_is_scoped(self) -> None:
        origins = build_allowed_origins("127.0.0.1", 9999)
        assert "http://127.0.0.1:8111" not in origins
        assert "http://127.0.0.1:9999" in origins


class TestBuildAllowedHosts:
    def test_loopback_bind_accepts_all_loopback_spellings(self) -> None:
        hosts = build_allowed_hosts("127.0.0.1")
        assert "127.0.0.1" in hosts
        assert "localhost" in hosts
        assert "::1" in hosts

    def test_non_loopback_bind_only_accepts_configured_host(self) -> None:
        assert build_allowed_hosts("192.168.1.5") == ["192.168.1.5"]

    def test_bracketed_ipv6_is_stripped_to_bare_form(self) -> None:
        """TrustedHostMiddleware matches the bare Host header, never bracketed."""
        hosts = build_allowed_hosts("[::1]")
        assert "::1" in hosts
        assert "[::1]" not in hosts

    def test_loopback_bind_includes_the_configured_host_itself(self) -> None:
        hosts = build_allowed_hosts("127.0.0.2")
        assert "127.0.0.2" in hosts
        assert "127.0.0.1" in hosts


def _request(headers: dict[str, str], method: str = "POST") -> Request:
    """Build a bare Request carrying only the headers under test."""
    raw = [(key.lower().encode(), value.encode()) for key, value in headers.items()]
    return Request({"type": "http", "method": method, "path": "/", "headers": raw})


class TestValidateOrigin:
    def test_matching_origin_is_accepted(self) -> None:
        allowed = frozenset({"http://127.0.0.1:8111"})
        request = _request({"origin": "http://127.0.0.1:8111"})
        assert validate_origin(request, allowed) is True

    def test_mismatched_origin_is_rejected(self) -> None:
        allowed = frozenset({"http://127.0.0.1:8111"})
        request = _request({"origin": "http://evil.example.com"})
        assert validate_origin(request, allowed) is False

    def test_falls_back_to_referer_when_origin_absent(self) -> None:
        allowed = frozenset({"http://127.0.0.1:8111"})
        request = _request({"referer": "http://127.0.0.1:8111/agents"})
        assert validate_origin(request, allowed) is True

    def test_mismatched_referer_is_rejected(self) -> None:
        allowed = frozenset({"http://127.0.0.1:8111"})
        request = _request({"referer": "http://evil.example.com/agents"})
        assert validate_origin(request, allowed) is False

    def test_both_absent_is_rejected(self) -> None:
        allowed = frozenset({"http://127.0.0.1:8111"})
        request = _request({})
        assert validate_origin(request, allowed) is False

    def test_origin_takes_precedence_over_referer(self) -> None:
        allowed = frozenset({"http://127.0.0.1:8111"})
        request = _request({"origin": "http://evil.example.com", "referer": "http://127.0.0.1:8111/agents"})
        assert validate_origin(request, allowed) is False


@pytest.fixture
def guarded_app() -> FastAPI:
    """A minimal app with one route guarded by require_same_origin_csrf."""
    test_app = FastAPI()
    test_app.state.is_loopback_bind = True
    test_app.state.allowed_origins = build_allowed_origins("127.0.0.1", 8111)
    test_app.state.csrf_fail_closed = False

    @test_app.post("/guarded", dependencies=[Depends(require_same_origin_csrf)])
    def guarded() -> dict:
        return {"ok": True}

    @test_app.get("/guarded-read", dependencies=[Depends(require_same_origin_csrf)])
    def guarded_read() -> dict:
        return {"ok": True}

    @test_app.get("/issue-cookie")
    def issue_cookie(response: Response, request: Request) -> dict:
        token = issue_csrf_cookie(response, request)
        return {"token": token}

    return test_app


class TestRequireSameOriginCsrf:
    def test_missing_origin_and_csrf_is_rejected(self, guarded_app: FastAPI) -> None:
        client = TestClient(guarded_app)
        response = client.post("/guarded")
        assert response.status_code == 403

    def test_cross_origin_request_is_rejected(self, guarded_app: FastAPI) -> None:
        client = TestClient(guarded_app)
        client.cookies.set(CSRF_COOKIE_NAME, "matching-token")
        response = client.post(
            "/guarded",
            headers={"Origin": "http://evil.example.com", CSRF_HEADER_NAME: "matching-token"},
        )
        assert response.status_code == 403

    def test_same_origin_missing_csrf_header_is_rejected(self, guarded_app: FastAPI) -> None:
        client = TestClient(guarded_app)
        client.cookies.set(CSRF_COOKIE_NAME, "matching-token")
        response = client.post("/guarded", headers={"Origin": "http://127.0.0.1:8111"})
        assert response.status_code == 403

    def test_mismatched_csrf_token_is_rejected(self, guarded_app: FastAPI) -> None:
        client = TestClient(guarded_app)
        client.cookies.set(CSRF_COOKIE_NAME, "cookie-token")
        response = client.post(
            "/guarded",
            headers={"Origin": "http://127.0.0.1:8111", CSRF_HEADER_NAME: "different-token"},
        )
        assert response.status_code == 403

    def test_valid_same_origin_request_with_matching_csrf_is_accepted(self, guarded_app: FastAPI) -> None:
        client = TestClient(guarded_app)
        client.cookies.set(CSRF_COOKIE_NAME, "matching-token")
        response = client.post(
            "/guarded",
            headers={"Origin": "http://127.0.0.1:8111", CSRF_HEADER_NAME: "matching-token"},
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}

    def test_valid_request_via_referer_fallback_is_accepted(self, guarded_app: FastAPI) -> None:
        client = TestClient(guarded_app)
        client.cookies.set(CSRF_COOKIE_NAME, "matching-token")
        response = client.post(
            "/guarded",
            headers={"Referer": "http://127.0.0.1:8111/agents", CSRF_HEADER_NAME: "matching-token"},
        )
        assert response.status_code == 200

    def test_fail_closed_rejects_even_valid_requests(self, guarded_app: FastAPI) -> None:
        guarded_app.state.csrf_fail_closed = True
        client = TestClient(guarded_app)
        client.cookies.set(CSRF_COOKIE_NAME, "matching-token")
        response = client.post(
            "/guarded",
            headers={"Origin": "http://127.0.0.1:8111", CSRF_HEADER_NAME: "matching-token"},
        )
        assert response.status_code == 403

    def test_issue_csrf_cookie_sets_cookie_readable_by_client(self, guarded_app: FastAPI) -> None:
        client = TestClient(guarded_app)
        response = client.get("/issue-cookie")
        assert response.status_code == 200
        assert client.cookies.get(CSRF_COOKIE_NAME) == response.json()["token"]

    def test_issue_csrf_cookie_omits_secure_flag_over_http(self, guarded_app: FastAPI) -> None:
        """Loopback HTTP is the default bind: a Secure cookie would never be returned."""
        client = TestClient(guarded_app, base_url="http://127.0.0.1:8111")
        set_cookie = client.get("/issue-cookie").headers["set-cookie"]
        assert "Secure" not in set_cookie

    def test_issue_csrf_cookie_sets_secure_flag_over_https(self, guarded_app: FastAPI) -> None:
        client = TestClient(guarded_app, base_url="https://127.0.0.1:8111")
        set_cookie = client.get("/issue-cookie").headers["set-cookie"]
        assert "Secure" in set_cookie

    def test_issue_csrf_cookie_is_not_httponly_and_is_samesite_strict(self, guarded_app: FastAPI) -> None:
        """HttpOnly must stay off so the dashboard's own JS can echo the token back."""
        client = TestClient(guarded_app)
        set_cookie = client.get("/issue-cookie").headers["set-cookie"]
        assert "HttpOnly" not in set_cookie
        assert "SameSite=strict" in set_cookie

    def test_get_request_bypasses_the_guard_entirely(self, guarded_app: FastAPI) -> None:
        """A GET is a no-op for the guard (#1204): this is what makes it safe to
        register once at router level, covering every route it carries, since a
        router mixes mutating and read-only routes."""
        client = TestClient(guarded_app)
        response = client.get("/guarded-read")
        assert response.status_code == 200

    def test_get_request_is_refused_when_fail_closed(self, guarded_app: FastAPI) -> None:
        """A non-loopback bind without a secret cannot verify a token, so reads fail closed too."""
        guarded_app.state.csrf_fail_closed = True
        client = TestClient(guarded_app)
        response = client.get("/guarded-read")
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Reverse proxy (Tailscale Serve) access: dashboard.proxy_origins (issue #1454)
# ---------------------------------------------------------------------------

_PROXY_ORIGIN = "https://mac.example.ts.net"
_PROXY_HOST = "mac.example.ts.net"
_LOGIN = "owner@github"


class TestProxyOriginsConfig:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("https://mac.example.ts.net", "https://mac.example.ts.net", id="plain"),
            pytest.param(" HTTPS://Mac.Example.TS.net/ ", "https://mac.example.ts.net", id="case-space-slash"),
            pytest.param("https://mac.example.ts.net:443", "https://mac.example.ts.net", id="default-port-dropped"),
            pytest.param("https://mac.example.ts.net:8443", "https://mac.example.ts.net:8443", id="other-port-kept"),
        ],
    )
    def test_entry_is_normalized_to_browser_origin(self, raw: str, expected: str) -> None:
        from sova.config.models import DashboardConfig

        assert DashboardConfig(proxy_origins=[raw]).proxy_origins == [expected]

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("http://mac.example.ts.net", id="plain-http"),
            pytest.param("mac.example.ts.net", id="no-scheme"),
            pytest.param("https://mac.example.ts.net/agents", id="path"),
            pytest.param("https://mac.example.ts.net?x=1", id="query"),
            pytest.param("https://user@mac.example.ts.net", id="userinfo"),
            pytest.param("https://localhost", id="localhost"),
            pytest.param("https://127.0.0.1", id="loopback-ip"),
            pytest.param("https://[::1]", id="ipv6-loopback"),
            pytest.param("https://0.0.0.0", id="wildcard"),
            pytest.param("https://*", id="glob-any-host"),
            pytest.param("https://*.example.ts.net", id="glob-subdomain"),
            pytest.param("https://mac.example.ts.net:notaport", id="bad-port"),
            pytest.param("https://", id="no-host"),
        ],
    )
    def test_invalid_entry_is_rejected(self, raw: str) -> None:
        from pydantic import ValidationError

        from sova.config.models import DashboardConfig

        with pytest.raises(ValidationError):
            DashboardConfig(proxy_origins=[raw])

    def test_defaults_are_empty(self) -> None:
        from sova.config.models import DashboardConfig

        cfg = DashboardConfig()
        assert cfg.proxy_origins == []
        assert cfg.proxy_allowed_logins == []

    def test_empty_login_is_rejected(self) -> None:
        from pydantic import ValidationError

        from sova.config.models import DashboardConfig

        with pytest.raises(ValidationError):
            DashboardConfig(proxy_allowed_logins=["  "])

    def test_env_overrides_take_json_arrays(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sova.config.models import DashboardConfig

        monkeypatch.setenv("SOVA_DASHBOARD_PROXY_ORIGINS", f'["{_PROXY_ORIGIN}"]')
        monkeypatch.setenv("SOVA_DASHBOARD_PROXY_ALLOWED_LOGINS", f'["{_LOGIN}"]')
        cfg = DashboardConfig()
        assert cfg.proxy_origins == [_PROXY_ORIGIN]
        assert cfg.proxy_allowed_logins == [_LOGIN]

    def test_both_settings_have_restart_metadata(self) -> None:
        from sova.dashboard.settings_meta import get_meta

        for key in ("dashboard.proxy_origins", "dashboard.proxy_allowed_logins"):
            meta = get_meta(key)
            assert meta is not None
            assert meta.value_type == "list"
            assert meta.requires_restart is True


class TestProxyAllowlists:
    def test_empty_proxy_origins_change_nothing(self) -> None:
        assert build_allowed_origins("127.0.0.1", 8111, ()) == build_allowed_origins("127.0.0.1", 8111)
        assert build_allowed_hosts("127.0.0.1", ()) == build_allowed_hosts("127.0.0.1")

    def test_proxy_origin_joins_origins_and_hosts(self) -> None:
        origins = build_allowed_origins("127.0.0.1", 8111, [_PROXY_ORIGIN])
        assert _PROXY_ORIGIN in origins
        assert "http://127.0.0.1:8111" in origins
        assert _PROXY_HOST in build_allowed_hosts("127.0.0.1", [_PROXY_ORIGIN])

    def test_proxy_hostnames_drop_scheme_and_port(self) -> None:
        from sova.dashboard.security import proxy_hostnames

        assert proxy_hostnames([_PROXY_ORIGIN, "https://other.example.ts.net:8443"]) == {
            _PROXY_HOST,
            "other.example.ts.net",
        }


def _proxied_headers(login: str | None = _LOGIN, **extra: str) -> dict[str, str]:
    headers = {"host": _PROXY_HOST, **extra}
    if login is not None:
        headers["tailscale-user-login"] = login
    return headers


def _stub_work_items_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the status socket's producer from rebuilding the work items, which lists issues through `gh`."""
    from unittest.mock import AsyncMock

    from sova.dashboard.routers import agents as agents_router

    monkeypatch.setattr(agents_router, "_refresh_work_items_snapshot", AsyncMock())


@pytest.fixture
def proxied_app(tmp_path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """A real dashboard app with one proxy origin and one allowed login."""
    from sova.dashboard.app import create_app

    monkeypatch.setenv("SOVA_DASHBOARD_PROXY_ORIGINS", f'["{_PROXY_ORIGIN}"]')
    monkeypatch.setenv("SOVA_DASHBOARD_PROXY_ALLOWED_LOGINS", f'["{_LOGIN}"]')
    monkeypatch.setattr("sova.dashboard.services.agent_pool._default_project_dir", None)
    app = create_app(project_dir=tmp_path, host="127.0.0.1", port=8111)

    @app.post("/__test/guarded", dependencies=[Depends(require_same_origin_csrf)])
    def _guarded() -> dict:
        return {"ok": True}

    @app.get("/__test/cookie")
    def _cookie(response: Response, request: Request) -> dict:
        return {"token": issue_csrf_cookie(response, request)}

    return app


# One representative route per kind the identity check must cover.
_PROXIED_ROUTES = [
    pytest.param("/agents", id="page"),
    pytest.param("/static/app.js", id="static-file"),
    pytest.param("/api/agents/active", id="api-read"),
    pytest.param("/api/feed/stream", id="sse-stream"),
]


class TestProxyIdentityAccess:
    """The access matrix from issue #1454's acceptance criteria, against `create_app()`."""

    @pytest.mark.parametrize("path", _PROXIED_ROUTES)
    @pytest.mark.parametrize(
        "login",
        [
            pytest.param(None, id="missing-login"),
            pytest.param("", id="empty-login"),
            pytest.param("intruder@github", id="other-login"),
        ],
    )
    def test_proxied_request_without_allowed_login_is_refused(
        self, proxied_app: FastAPI, path: str, login: str | None
    ) -> None:
        client = TestClient(proxied_app, base_url=_PROXY_ORIGIN)
        response = client.get(path, headers=_proxied_headers(login))
        assert response.status_code == 403
        assert response.json()["detail"] == "Proxied request without an allowed Tailscale login"
        # The refusal still carries the dashboard's security headers.
        assert response.headers["x-frame-options"] == "DENY"

    @pytest.mark.parametrize("path", ["/static/app.js", "/healthz", "/api/agents/active"])
    def test_proxied_request_with_allowed_login_passes(self, proxied_app: FastAPI, path: str) -> None:
        client = TestClient(proxied_app, base_url=_PROXY_ORIGIN)
        response = client.get(path, headers=_proxied_headers("Owner@GitHub"))
        assert response.status_code == 200

    def test_proxied_mutating_call_passes_csrf_with_proxy_origin(self, proxied_app: FastAPI) -> None:
        client = TestClient(proxied_app, base_url=_PROXY_ORIGIN)
        token = client.get("/__test/cookie", headers=_proxied_headers()).json()["token"]
        client.cookies.set(CSRF_COOKIE_NAME, token)
        response = client.post(
            "/__test/guarded", headers=_proxied_headers(origin=_PROXY_ORIGIN, **{CSRF_HEADER_NAME: token})
        )
        assert response.status_code == 200

    def test_proxied_mutating_call_with_foreign_origin_is_refused(self, proxied_app: FastAPI) -> None:
        client = TestClient(proxied_app, base_url=_PROXY_ORIGIN)
        client.cookies.set(CSRF_COOKIE_NAME, "t")
        response = client.post(
            "/__test/guarded", headers=_proxied_headers(origin="https://evil.example", **{CSRF_HEADER_NAME: "t"})
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "Origin check failed"

    def test_csrf_cookie_is_secure_over_https_proxy(self, proxied_app: FastAPI) -> None:
        client = TestClient(proxied_app, base_url=_PROXY_ORIGIN)
        response = client.get("/__test/cookie", headers=_proxied_headers())
        assert "secure" in response.headers["set-cookie"].lower()

    @pytest.mark.parametrize("host", [_PROXY_HOST, "127.0.0.1:8111"])
    def test_funnel_request_is_refused_on_any_host(self, proxied_app: FastAPI, host: str) -> None:
        client = TestClient(proxied_app)
        headers = {"host": host, "tailscale-user-login": _LOGIN, "tailscale-funnel-request": "?1"}
        response = client.get("/healthz", headers=headers)
        assert response.status_code == 403
        assert response.json()["detail"] == "Requests through Tailscale Funnel are refused"

    def test_loopback_request_needs_no_login(self, proxied_app: FastAPI) -> None:
        client = TestClient(proxied_app, base_url="http://127.0.0.1:8111")
        response = client.get("/healthz")
        assert response.status_code == 200
        assert "secure" not in response.headers.get("set-cookie", "").lower()

    # Serve forwards the client's own Host, so a tailnet client can send a loopback one through it;
    # the headers Serve adds still mark the request as proxied (measured live, issue #1454).
    @pytest.mark.parametrize(
        "marker",
        [
            pytest.param({"x-forwarded-for": "100.64.0.9"}, id="x-forwarded-for"),
            pytest.param({"x-forwarded-proto": "https"}, id="x-forwarded-proto"),
            pytest.param({"x-forwarded-host": "127.0.0.1:8111"}, id="x-forwarded-host"),
            pytest.param({"forwarded": "for=100.64.0.9;proto=https"}, id="forwarded"),
            pytest.param({"tailscale-user-login": "intruder@github"}, id="other-login"),
        ],
    )
    def test_loopback_host_with_proxy_headers_still_needs_allowed_login(
        self, proxied_app: FastAPI, marker: dict[str, str]
    ) -> None:
        client = TestClient(proxied_app, base_url="http://127.0.0.1:8111")
        response = client.get("/healthz", headers=marker)
        assert response.status_code == 403
        assert response.json()["detail"] == "Proxied request without an allowed Tailscale login"

    def test_loopback_host_with_proxy_headers_and_allowed_login_passes(self, proxied_app: FastAPI) -> None:
        client = TestClient(proxied_app, base_url="http://127.0.0.1:8111")
        headers = {"x-forwarded-for": "100.64.0.9", "x-forwarded-proto": "https", "tailscale-user-login": _LOGIN}
        assert client.get("/healthz", headers=headers).status_code == 200

    def test_websocket_with_loopback_host_and_proxy_headers_is_refused(self, proxied_app: FastAPI) -> None:
        from starlette.websockets import WebSocketDisconnect

        client = TestClient(proxied_app, base_url="http://127.0.0.1:8111")
        headers = {"origin": "http://127.0.0.1:8111", "x-forwarded-for": "100.64.0.9"}
        with (
            pytest.raises(WebSocketDisconnect) as exc_info,
            client.websocket_connect("/api/ws/agents/status", headers=headers),
        ):
            pass
        assert exc_info.value.code == 1008

    def test_unknown_host_is_still_refused(self, proxied_app: FastAPI) -> None:
        client = TestClient(proxied_app)
        response = client.get("/healthz", headers={"host": "evil.example.ts.net", "tailscale-user-login": _LOGIN})
        assert response.status_code == 400

    def test_proxy_host_with_explicit_port_is_still_checked(self, proxied_app: FastAPI) -> None:
        client = TestClient(proxied_app)
        response = client.get("/healthz", headers={"host": f"{_PROXY_HOST}:443"})
        assert response.status_code == 403

    @pytest.mark.parametrize(
        "login",
        [pytest.param(None, id="missing-login"), pytest.param("intruder@github", id="other-login")],
    )
    def test_websocket_without_allowed_login_is_refused(self, proxied_app: FastAPI, login: str | None) -> None:
        from starlette.websockets import WebSocketDisconnect

        client = TestClient(proxied_app, base_url=_PROXY_ORIGIN)
        with (
            pytest.raises(WebSocketDisconnect) as exc_info,
            client.websocket_connect("/api/ws/agents/status", headers=_proxied_headers(login, origin=_PROXY_ORIGIN)),
        ):
            pass
        assert exc_info.value.code == 1008

    def test_websocket_with_allowed_login_and_proxy_origin_connects(
        self, proxied_app: FastAPI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_work_items_refresh(monkeypatch)
        client = TestClient(proxied_app, base_url=_PROXY_ORIGIN)
        with client.websocket_connect("/api/ws/agents/status", headers=_proxied_headers(origin=_PROXY_ORIGIN)) as ws:
            assert ws.receive_json()["type"] == "status_update"


@pytest.mark.usefixtures("reset_db_rows")
class TestProxyWithoutAllowedLogins:
    def test_every_proxied_request_is_refused_and_startup_warns(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from structlog.testing import capture_logs

        from sova.dashboard.app import create_app

        monkeypatch.setenv("SOVA_DASHBOARD_PROXY_ORIGINS", f'["{_PROXY_ORIGIN}"]')
        monkeypatch.setattr("sova.dashboard.services.agent_pool._default_project_dir", None)
        with capture_logs() as logs:
            app = create_app(project_dir=tmp_path, host="127.0.0.1", port=8111)
        assert any(e["event"] == "dashboard.proxy_no_allowed_logins" for e in logs)
        client = TestClient(app, base_url=_PROXY_ORIGIN)
        assert client.get("/healthz", headers=_proxied_headers()).status_code == 403
        assert TestClient(app, base_url="http://127.0.0.1:8111").get("/healthz").status_code == 200


@pytest.mark.usefixtures("reset_db_rows")
class TestNoProxyConfigured:
    def test_identity_middleware_is_installed_and_refuses_a_proxy_marker(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sova.dashboard.app import create_app
        from sova.dashboard.security import ProxyIdentityMiddleware

        monkeypatch.setattr("sova.dashboard.services.agent_pool._default_project_dir", None)
        app = create_app(project_dir=tmp_path, host="127.0.0.1", port=8111)
        assert any(m.cls is ProxyIdentityMiddleware for m in app.user_middleware)
        assert app.state.allowed_origins == build_allowed_origins("127.0.0.1", 8111)
        client = TestClient(app, base_url="http://127.0.0.1:8111")
        assert client.get("/healthz").status_code == 200
        assert client.get("/healthz", headers={"x-forwarded-for": "100.64.0.9"}).status_code == 403
        assert client.get("/healthz", headers=_proxied_headers()).status_code == 403
        assert TestClient(app).get("/healthz", headers={"host": _PROXY_HOST}).status_code == 400

    def test_an_allowed_login_does_not_pass_without_a_proxy_origin(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Clearing proxy_origins turns remote access off even while Serve still forwards an allowed login."""
        from sova.dashboard.app import create_app

        monkeypatch.setenv("SOVA_DASHBOARD_PROXY_ALLOWED_LOGINS", f'["{_LOGIN}"]')
        monkeypatch.setattr("sova.dashboard.services.agent_pool._default_project_dir", None)
        app = create_app(project_dir=tmp_path, host="127.0.0.1", port=8111)
        client = TestClient(app, base_url="http://127.0.0.1:8111")
        forwarded = {"x-forwarded-for": "100.64.0.9", "tailscale-user-login": _LOGIN}
        assert client.get("/healthz", headers=forwarded).status_code == 403
        assert client.get("/healthz", headers={"tailscale-user-login": _LOGIN}).status_code == 403
        assert client.get("/healthz").status_code == 200


@pytest.mark.usefixtures("reset_db_rows")
class TestHarnessTrustedHosts:
    def test_test_transport_hosts_are_refused_unless_the_harness_passes_them(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sova.dashboard.app import create_app

        monkeypatch.setattr("sova.dashboard.app.HARNESS_TRUSTED_HOSTS", ())
        monkeypatch.setattr("sova.dashboard.services.agent_pool._default_project_dir", None)
        app = create_app(project_dir=tmp_path, host="127.0.0.1", port=8111)
        assert TestClient(app, base_url="http://testserver").get("/healthz").status_code == 400
        assert TestClient(app, base_url="http://test").get("/healthz").status_code == 400
        assert TestClient(app, base_url="http://127.0.0.1:8111").get("/healthz").status_code == 200


# ---------------------------------------------------------------------------
# Signed CSRF token on a non-loopback bind (issue #1568)
# ---------------------------------------------------------------------------

_LAN_HOST = "192.168.1.5"
_LAN_ORIGIN = f"http://{_LAN_HOST}:8111"
# Long and varied enough for is_strong_csrf_secret(), which a non-loopback bind requires.
_SECRET = "Kq7vN2xR9mPz4LwT8bYc1HdF6sJg3AeU"


@pytest.fixture
def signed_app() -> FastAPI:
    """`guarded_app`'s routes on a non-loopback bind that has a CSRF secret."""
    test_app = FastAPI()
    test_app.state.is_loopback_bind = False
    test_app.state.allowed_origins = build_allowed_origins(_LAN_HOST, 8111)
    test_app.state.csrf_fail_closed = False
    test_app.state.csrf_signing_secret = _SECRET

    @test_app.post("/guarded", dependencies=[Depends(require_same_origin_csrf)])
    def guarded() -> dict:
        return {"ok": True}

    @test_app.get("/guarded-read", dependencies=[Depends(require_same_origin_csrf)])
    def guarded_read() -> dict:
        return {"ok": True}

    @test_app.get("/issue-cookie")
    def issue_cookie(response: Response, request: Request) -> dict:
        return {"token": issue_csrf_cookie(response, request)}

    return test_app


def _tampered(token: str) -> str:
    return token[:-1] + ("0" if token[-1] != "0" else "1")


def _bad_pair(case: str) -> tuple[str, str]:
    """A (cookie, header) pair the signed guard must reject."""
    good = sign_csrf_token(_SECRET)
    nonce = good.rpartition(".")[0]
    return {
        "self-chosen": ("abc", "abc"),
        "tampered": (_tampered(good), _tampered(good)),
        "other-secret": (sign_csrf_token("other-secret"),) * 2,
        "valid-nonce-wrong-signature": (f"{nonce}.{'0' * 64}",) * 2,
        "no-signature": (nonce, nonce),
        "empty-cookie": ("", good),
        "empty-header": (good, ""),
        "non-ascii": (f"{nonce}.\u00e9", f"{nonce}.\u00e9"),
    }[case]


_BAD_PAIRS = [
    "self-chosen",
    "tampered",
    "other-secret",
    "valid-nonce-wrong-signature",
    "no-signature",
    "empty-cookie",
    "empty-header",
    "non-ascii",
]


class TestSignedCsrfToken:
    def test_token_round_trips_only_under_its_own_secret(self) -> None:
        token = sign_csrf_token(_SECRET)
        assert verify_signed_csrf_token(token, _SECRET) is True
        assert verify_signed_csrf_token(token, "other") is False
        assert verify_signed_csrf_token(token, "") is False
        assert verify_signed_csrf_token("", _SECRET) is False

    def test_issued_cookie_is_signed_and_passes_a_write_and_a_read(self, signed_app: FastAPI) -> None:
        client = TestClient(signed_app, base_url=_LAN_ORIGIN)
        token = client.get("/issue-cookie").json()["token"]
        assert verify_signed_csrf_token(token, _SECRET)
        write = client.post("/guarded", headers={"Origin": _LAN_ORIGIN, CSRF_HEADER_NAME: token})
        assert write.status_code == 200
        assert client.get("/guarded-read").status_code == 200
        assert client.get("/guarded-read", headers={CSRF_HEADER_NAME: token}).status_code == 200

    @pytest.mark.parametrize("case", _BAD_PAIRS)
    @pytest.mark.parametrize("method", ["POST", "GET"])
    def test_a_forged_or_empty_pair_is_rejected(self, signed_app: FastAPI, case: str, method: str) -> None:
        cookie, header = _bad_pair(case)
        client = TestClient(signed_app, base_url=_LAN_ORIGIN)
        # Raw bytes, so a non-ASCII value reaches the server as a browser could send it.
        headers = {
            "Origin": _LAN_ORIGIN,
            "Cookie": f"{CSRF_COOKIE_NAME}={cookie}".encode(),
            CSRF_HEADER_NAME: header.encode(),
        }
        path = "/guarded" if method == "POST" else "/guarded-read"
        response = client.request(method, path, headers=headers)
        assert response.status_code == 403
        assert response.json()["detail"] == CSRF_REJECTED_DETAIL

    def test_token_carries_its_issue_time_and_expires(self) -> None:
        now = int(time.time())
        assert sign_csrf_token(_SECRET, issued_at=now).startswith(f"{now}.")
        fresh = sign_csrf_token(_SECRET, issued_at=now - CSRF_TOKEN_MAX_AGE_SECONDS + 60)
        expired = sign_csrf_token(_SECRET, issued_at=now - CSRF_TOKEN_MAX_AGE_SECONDS - 60)
        far_future = sign_csrf_token(_SECRET, issued_at=now + 3600)
        assert verify_signed_csrf_token(fresh, _SECRET) is True
        assert verify_signed_csrf_token(expired, _SECRET) is False
        assert verify_signed_csrf_token(far_future, _SECRET) is False

    def test_a_restamped_token_fails_its_signature(self) -> None:
        """Moving an old token's issue time forward without the secret breaks the HMAC."""
        old = sign_csrf_token(_SECRET, issued_at=int(time.time()) - CSRF_TOKEN_MAX_AGE_SECONDS - 60)
        _, nonce, signature = old.split(".")
        assert verify_signed_csrf_token(f"{int(time.time())}.{nonce}.{signature}", _SECRET) is False

    @pytest.mark.parametrize("method", ["POST", "GET"])
    def test_an_expired_token_is_rejected(self, signed_app: FastAPI, method: str) -> None:
        expired = sign_csrf_token(_SECRET, issued_at=int(time.time()) - CSRF_TOKEN_MAX_AGE_SECONDS - 60)
        client = TestClient(signed_app, base_url=_LAN_ORIGIN)
        client.cookies.set(CSRF_COOKIE_NAME, expired)
        path = "/guarded" if method == "POST" else "/guarded-read"
        response = client.request(method, path, headers={"Origin": _LAN_ORIGIN, CSRF_HEADER_NAME: expired})
        assert response.status_code == 403

    @pytest.mark.parametrize(
        ("secret", "strong"),
        [
            pytest.param("", False, id="empty"),
            pytest.param("1", False, id="opt-in-value"),
            pytest.param("operator-secret", False, id="short"),
            pytest.param("ab" * 20, False, id="long-but-two-characters"),
            pytest.param(_SECRET, True, id="generated"),
        ],
    )
    def test_secret_strength(self, secret: str, strong: bool) -> None:
        assert is_strong_csrf_secret(secret) is strong

    def test_a_read_without_a_cookie_is_rejected(self, signed_app: FastAPI) -> None:
        assert TestClient(signed_app, base_url=_LAN_ORIGIN).get("/guarded-read").status_code == 403

    def test_no_cookie_is_issued_to_a_foreign_origin(self, signed_app: FastAPI) -> None:
        client = TestClient(signed_app, base_url=_LAN_ORIGIN)
        response = client.get("/issue-cookie", headers={"Origin": "http://evil.example.com"})
        assert response.json() == {"token": None}
        assert "set-cookie" not in response.headers


@pytest.fixture
def lan_app(tmp_path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """A real dashboard app bound to a LAN address with a CSRF secret configured."""
    from sova.dashboard.app import create_app

    monkeypatch.setenv("SOVA_DASHBOARD_CSRF_SECRET", _SECRET)
    monkeypatch.setattr("sova.dashboard.services.agent_pool._default_project_dir", None)
    return create_app(project_dir=tmp_path, host=_LAN_HOST, port=8111)


class TestNonLoopbackBind:
    # The websocket test stays off reset_db_rows: its shared in-memory engine belongs to the test's event
    # loop, and the status socket's producer reaching it from TestClient's portal thread never returns.
    @pytest.mark.usefixtures("reset_db_rows")
    def test_page_issues_a_signed_cookie_and_api_reads_require_it(self, lan_app: FastAPI) -> None:
        client = TestClient(lan_app, base_url=_LAN_ORIGIN)
        assert client.get("/api/agents/active").status_code == 403
        client.get("/work")
        assert verify_signed_csrf_token(client.cookies[CSRF_COOKIE_NAME], _SECRET)
        assert client.get("/api/agents/active").status_code == 200

    @pytest.mark.parametrize(
        ("cookie", "accepted"),
        [
            pytest.param("signed", True, id="signed-cookie"),
            pytest.param(None, False, id="no-cookie"),
            pytest.param("abc", False, id="self-chosen"),
            pytest.param("other-secret", False, id="other-secret"),
        ],
    )
    def test_websocket_follows_the_read_rule(
        self, lan_app: FastAPI, cookie: str | None, accepted: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from starlette.websockets import WebSocketDisconnect

        _stub_work_items_refresh(monkeypatch)
        client = TestClient(lan_app, base_url=_LAN_ORIGIN)
        if cookie is not None:
            value = {"signed": sign_csrf_token(_SECRET), "other-secret": sign_csrf_token("other-secret")}.get(cookie)
            client.cookies.set(CSRF_COOKIE_NAME, value or cookie)
        headers = {"origin": _LAN_ORIGIN}
        if accepted:
            with client.websocket_connect("/api/ws/agents/status", headers=headers) as ws:
                assert ws.receive_json()["type"] == "status_update"
            return
        with (
            pytest.raises(WebSocketDisconnect) as exc_info,
            client.websocket_connect("/api/ws/agents/status", headers=headers),
        ):
            pass
        assert exc_info.value.code == 1008

    @pytest.mark.usefixtures("reset_db_rows")
    def test_a_cookie_past_half_its_life_is_renewed_on_any_request(self, lan_app: FastAPI) -> None:
        aging = sign_csrf_token(_SECRET, issued_at=int(time.time()) - CSRF_TOKEN_MAX_AGE_SECONDS // 2 - 60)
        client = TestClient(lan_app, base_url=_LAN_ORIGIN)
        client.cookies.set(CSRF_COOKIE_NAME, aging)
        response = client.get("/api/agents/active")
        assert response.status_code == 200
        renewed = response.cookies[CSRF_COOKIE_NAME]
        assert renewed != aging
        assert verify_signed_csrf_token(renewed, _SECRET)
        # A young cookie is left alone, and a forged one is never swapped for a valid one.
        young = TestClient(lan_app, base_url=_LAN_ORIGIN)
        young.cookies.set(CSRF_COOKIE_NAME, renewed)
        assert "set-cookie" not in young.get("/api/agents/active").headers
        forged = TestClient(lan_app, base_url=_LAN_ORIGIN)
        forged.cookies.set(CSRF_COOKIE_NAME, "abc")
        assert "set-cookie" not in forged.get("/healthz").headers

    @pytest.mark.usefixtures("reset_db_rows")
    def test_a_fail_closed_hub_renders_no_project_path(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A wildcard bind trusts any Host, so its pages must refuse too: the hub lists every project path."""
        import sova.config.registry as reg_mod
        from sova.dashboard.app import create_app

        monkeypatch.setattr(reg_mod, "_REGISTRY_FILE", tmp_path / "registry" / "projects.json")
        monkeypatch.setattr(reg_mod, "_REGISTRY_DIR", tmp_path / "registry")
        project = tmp_path / "private-project-name"
        (project / ".claude").mkdir(parents=True)
        reg_mod.register_project(project, slug="private")
        app = create_app(multi_project=True, host="0.0.0.0", port=8111)
        assert app.state.csrf_fail_closed is True
        client = TestClient(app, base_url=_LAN_ORIGIN)
        for path in ("/", "/p/private/work", "/p/private/supervisor", "/api/projects"):
            response = client.get(path)
            assert response.status_code == 403, path
            assert "private-project-name" not in response.text
            assert response.headers["X-Frame-Options"] == "DENY"
        assert client.get("/healthz").status_code != 403
        assert client.get("/api/health/connectivity").status_code == 200

    @pytest.mark.usefixtures("reset_db_rows")
    def test_a_fail_closed_healthz_names_no_project(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The probe needs no token on a fail-closed bind, so it reports one aggregate db result."""
        import sova.config.registry as reg_mod
        from sova.dashboard.app import create_app

        monkeypatch.setattr(reg_mod, "_REGISTRY_FILE", tmp_path / "registry" / "projects.json")
        monkeypatch.setattr(reg_mod, "_REGISTRY_DIR", tmp_path / "registry")
        project = tmp_path / "proj"
        (project / ".claude").mkdir(parents=True)
        reg_mod.register_project(project, slug="private-slug")

        async def _db_ok(project_dir=None, **_: object) -> str:
            return "ok"

        # No real session: the TestClient's loop closes after the request and would strand a connection.
        monkeypatch.setattr("sova.dashboard.app._check_db_health", _db_ok)
        app = create_app(multi_project=True, host="0.0.0.0", port=8111)
        assert app.state.csrf_fail_closed is True
        response = TestClient(app, base_url=_LAN_ORIGIN).get("/healthz")
        assert "private-slug" not in response.text
        assert response.json()["checks"]["db"] == "ok"
        assert not any(key.startswith("db_") for key in response.json()["checks"])

    @pytest.mark.usefixtures("reset_db_rows")
    def test_hub_project_list_needs_the_signed_cookie(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        import sova.config.registry as reg_mod
        from sova.dashboard.app import create_app

        monkeypatch.setattr(reg_mod, "_REGISTRY_FILE", tmp_path / "registry" / "projects.json")
        monkeypatch.setattr(reg_mod, "_REGISTRY_DIR", tmp_path / "registry")
        monkeypatch.setenv("SOVA_DASHBOARD_CSRF_SECRET", _SECRET)
        client = TestClient(create_app(multi_project=True, host=_LAN_HOST, port=8111), base_url=_LAN_ORIGIN)
        assert client.get("/api/projects").status_code == 403
        client.get("/")
        assert client.get("/api/projects").status_code == 200

    @pytest.mark.usefixtures("reset_db_rows")
    def test_a_wildcard_bind_still_takes_authenticated_telemetry(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Machine endpoints with their own authentication stay reachable on a fail-closed bind; pages do not."""
        from sova.dashboard.app import create_app
        from sova.dashboard.routers import telemetry as telemetry_mod

        monkeypatch.setenv("SOVA_TELEMETRY_TOKEN", "telemetry-token")
        monkeypatch.setattr("sova.dashboard.services.agent_pool._default_project_dir", None)
        monkeypatch.setattr(telemetry_mod, "_rate_windows", defaultdict(deque))
        app = create_app(project_dir=tmp_path, host="0.0.0.0", port=8111)
        assert app.state.csrf_fail_closed is True
        client = TestClient(app, base_url=_LAN_ORIGIN)
        payload = {
            "machine_id": "machine-001",
            "run_id": "run-001",
            "project_slug": "my-project",
            "role": "developer",
            "status": "done",
            "run_at": "2026-07-26T10:00:00Z",
        }
        ingest = client.post("/api/telemetry/ingest", json=payload, headers={"Authorization": "Bearer telemetry-token"})
        assert ingest.status_code == 201
        assert client.post("/api/telemetry/ingest", json=payload).status_code == 401
        assert client.post("/api/mcp", json={}).status_code == 401
        for path in ("/", "/work", "/api/agents/active"):
            assert client.get(path).status_code == 403, path

    @pytest.mark.parametrize("secret", ["", "1", "ab" * 20], ids=["no-secret", "opt-in-value", "two-characters"])
    def test_websocket_is_refused_on_a_fail_closed_bind(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch, secret: str
    ) -> None:
        """No usable secret leaves no signing key, and the handshake must not fall back to Origin alone."""
        from starlette.websockets import WebSocketDisconnect

        from sova.dashboard.app import create_app

        monkeypatch.setenv("SOVA_DASHBOARD_CSRF_SECRET", secret)
        monkeypatch.setattr("sova.dashboard.services.agent_pool._default_project_dir", None)
        app = create_app(project_dir=tmp_path, host=_LAN_HOST, port=8111)
        assert app.state.csrf_fail_closed is True
        assert app.state.csrf_signing_secret == ""
        client = TestClient(app, base_url=_LAN_ORIGIN)
        with (
            pytest.raises(WebSocketDisconnect) as exc_info,
            client.websocket_connect("/api/ws/agents/status", headers={"origin": _LAN_ORIGIN}),
        ):
            pass
        assert exc_info.value.code == 1008

    @pytest.mark.parametrize(
        "headers",
        [
            pytest.param({"X-Forwarded-For": "100.64.0.9"}, id="proxied-without-login"),
            pytest.param({"host": "evil.example.com"}, id="untrusted-host"),
        ],
    )
    def test_a_refused_request_gets_no_renewed_cookie(self, lan_app: FastAPI, headers: dict[str, str]) -> None:
        """The renewal runs inside the Host and proxy identity checks, so their refusals carry no token."""
        aging = sign_csrf_token(_SECRET, issued_at=int(time.time()) - CSRF_TOKEN_MAX_AGE_SECONDS // 2 - 60)
        client = TestClient(lan_app, base_url=_LAN_ORIGIN)
        client.cookies.set(CSRF_COOKIE_NAME, aging)
        response = client.get("/api/agents/active", headers=headers)
        assert response.status_code in (400, 403)
        assert "set-cookie" not in response.headers


class TestFailClosedExemptions:
    @pytest.mark.parametrize(
        ("path", "exempt"),
        [
            ("/healthz", True),
            ("/api/health", True),
            ("/api/health/connectivity", True),
            ("/api/telemetry/ingest", True),
            ("/api/mcp", True),
            ("/p/alpha/api/mcp", True),
            ("/a2a/tasks/sova-run-1/cancel", True),
            ("/.well-known/agent.json", True),
            ("/a2a/planner/agent.json", True),
            ("/a2a/tasks", False),
            ("/a2a/tasks/sova-run-1", False),
            ("/a2a/tasks//cancel", False),
            ("/a2a//agent.json", False),
            ("/.well-known/other.json", False),
            ("/", False),
            ("/work", False),
            ("/api/projects", False),
            ("/api/scheduler/digest", False),
            ("/api/telemetry/ingest/x", False),
            ("/p/alpha/api/mcp/x", False),
            ("/p//api/mcp", False),
            ("/a2a", False),
        ],
    )
    def test_exempt_paths(self, path: str, exempt: bool) -> None:
        from sova.dashboard.security import is_fail_closed_exempt

        assert is_fail_closed_exempt(path) is exempt


class TestStartupLogs:
    @pytest.mark.parametrize(
        ("host", "secret", "events"),
        [
            pytest.param(_LAN_HOST, "", ["dashboard.nonloopback_no_secret"], id="no-secret"),
            pytest.param(_LAN_HOST, "1", ["dashboard.csrf_secret_too_weak"], id="weak-secret"),
            pytest.param("0.0.0.0", "1", ["dashboard.nonloopback_no_secret"], id="wildcard"),
            pytest.param(_LAN_HOST, _SECRET, [], id="strong-secret"),
        ],
    )
    def test_fail_closed_event_names_the_fix(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch, host: str, secret: str, events: list[str]
    ) -> None:
        from structlog.testing import capture_logs

        from sova.dashboard.app import create_app

        monkeypatch.setenv("SOVA_DASHBOARD_CSRF_SECRET", secret)
        with capture_logs() as logs:
            create_app(project_dir=tmp_path, host=host, port=8111)
        names = {"dashboard.nonloopback_no_secret", "dashboard.csrf_secret_too_weak"}
        assert [e["event"] for e in logs if e["event"] in names] == events

    def test_logins_without_origins_are_reported(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        from structlog.testing import capture_logs

        from sova.dashboard.app import create_app

        monkeypatch.setenv("SOVA_DASHBOARD_PROXY_ALLOWED_LOGINS", '["owner@github"]')
        with capture_logs() as logs:
            create_app(project_dir=tmp_path)
        assert [e["event"] for e in logs if e["event"].startswith("dashboard.proxy_")] == [
            "dashboard.proxy_logins_ignored"
        ]
