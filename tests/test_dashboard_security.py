"""Tests for sova.dashboard.security: origin/CSRF guard for state-changing endpoints."""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, Request, Response
from fastapi.testclient import TestClient

from sova.dashboard.security import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    build_allowed_origins,
    is_loopback_host,
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


def _request(headers: dict[str, str]) -> Request:
    """Build a bare Request carrying only the headers under test."""
    raw = [(key.lower().encode(), value.encode()) for key, value in headers.items()]
    return Request({"type": "http", "method": "POST", "path": "/", "headers": raw})


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

    @test_app.get("/issue-cookie")
    def issue_cookie(response: Response) -> dict:
        token = issue_csrf_cookie(response)
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
