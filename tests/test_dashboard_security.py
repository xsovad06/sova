"""Tests for sova.dashboard.security: origin/CSRF guard for state-changing endpoints."""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, Request, Response
from fastapi.testclient import TestClient

from sova.dashboard.security import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    build_allowed_hosts,
    build_allowed_origins,
    is_loopback_host,
    is_wildcard_host,
    issue_csrf_cookie,
    require_same_origin_csrf,
    validate_origin,
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

    def test_get_request_bypasses_even_when_fail_closed(self, guarded_app: FastAPI) -> None:
        guarded_app.state.csrf_fail_closed = True
        client = TestClient(guarded_app)
        response = client.get("/guarded-read")
        assert response.status_code == 200


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

    def test_websocket_with_allowed_login_and_proxy_origin_connects(self, proxied_app: FastAPI) -> None:
        client = TestClient(proxied_app, base_url=_PROXY_ORIGIN)
        with client.websocket_connect("/api/ws/agents/status", headers=_proxied_headers(origin=_PROXY_ORIGIN)) as ws:
            assert ws.receive_json()["type"] == "status_update"


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


class TestNoProxyConfigured:
    def test_proxy_host_is_refused_and_no_identity_middleware_is_installed(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sova.dashboard.app import create_app
        from sova.dashboard.security import ProxyIdentityMiddleware

        monkeypatch.setattr("sova.dashboard.services.agent_pool._default_project_dir", None)
        app = create_app(project_dir=tmp_path, host="127.0.0.1", port=8111)
        assert all(m.cls is not ProxyIdentityMiddleware for m in app.user_middleware)
        assert app.state.allowed_origins == build_allowed_origins("127.0.0.1", 8111)
        response = TestClient(app).get("/healthz", headers=_proxied_headers())
        assert response.status_code == 400
