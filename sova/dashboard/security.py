"""Origin/CSRF guard for state-changing dashboard endpoints.

The dashboard has no user accounts or sessions (see `.claude/rules/architecture.md`,
"Dashboard Security Model": single-user loopback tool, access implies machine access).
This module exists to stop a *different* attacker: any web page the user's browser
visits can otherwise issue cross-origin `fetch()` calls at the dashboard's local port.
DNS rebinding makes same-origin policy alone an incomplete defense, since a page's
`Origin` header reflects the address it was loaded from, not where that name resolves.

Two independent checks compose into `require_same_origin_csrf`, a FastAPI dependency
every router carries; on a loopback bind it checks state-changing requests only:

1. Origin/Referer must match the dashboard's own scheme+host+port
   (`validate_origin` / `build_allowed_origins`).
2. A double-submit CSRF token: a cookie value that a cross-origin page cannot read,
   echoed back as a header by the dashboard's own JS (`issue_csrf_cookie`).

Both are required because a misconfigured proxy can strip Origin/Referer, and because
Origin/Referer are sometimes legitimately absent (e.g. strict referrer policies).

When bound to a non-loopback host, both checks alone are not enough: a direct client
(curl) sets Origin and both halves of a double-submit pair itself. On such a bind the
token is signed: `issue_csrf_cookie` mints `<issued_at>.<nonce>.<HMAC-SHA256(
dashboard.csrf_secret, issued_at.nonce)>`, and the guard rejects any pair whose signature
does not verify or that is older than `CSRF_TOKEN_MAX_AGE_SECONDS`, so a client cannot
choose its own pair and an attacker page cannot mint one. The expiry bounds only an unused
token: its holder's own requests renew it (`CsrfCookieRefreshMiddleware`), and any client
that can load a page gets a fresh one, so it is no bearer-token lifetime. Reads
(and the websocket handshake) need a valid signed cookie too. With no secret configured,
a secret `is_strong_csrf_secret` rejects, or a wildcard bind, the guard fails closed
instead. A loopback bind keeps the plain
double-submit pair, which is what the MCP operator client (`sova/mcp/operator.py`) sends.

Scope limit worth stating plainly: the signed token is a forgery and CSRF defence, not
authentication. Anyone who can load a dashboard page over the bound interface gets a
valid cookie, which is why the cookie is issued only to a request whose Host passed
`TrustedHostMiddleware` and whose Origin, when it sends one, is the dashboard's own.
Tailscale Serve with proxy identity (below) remains the supported way to reach the
dashboard from another machine.

Reverse proxy (`dashboard.proxy_origins`, Tailscale Serve): the bind stays loopback, and
each configured HTTPS origin is added to the trusted Host and Origin sets. The proxy
forwards the client's Host unchanged, whatever it says, so `ProxyIdentityMiddleware`
treats a request as proxied when its Host names a proxy origin or it carries a header a
proxy adds (`X-Forwarded-For`, which Serve always sets), and refuses a proxied request
that does not carry an allowed `Tailscale-User-Login`. Serve strips a client-sent copy of that
header and sets its own for tailnet users only, so on the proxy path it identifies the
caller. A process on the machine can still forge it by calling loopback directly with a
proxy Host, which is the same "access to the machine implies access to the dashboard"
model as the plain loopback bind.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import time
from collections.abc import Iterable
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response, WebSocket
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketClose

CSRF_COOKIE_NAME = "sova_csrf"
CSRF_HEADER_NAME = "x-csrf-token"
# The 403 detail for a missing or mismatched double-submit pair. `static/app.js`
# matches this exact string to tell the user their page is stale (no cookie, or
# JS from before a deploy) and must be reloaded, so changing it here breaks that.
CSRF_REJECTED_DETAIL = "Missing or invalid CSRF token"
# The 403 detail on a fail-closed bind (non-loopback with no usable secret, or wildcard).
FAIL_CLOSED_DETAIL = "Dashboard is bound to a non-loopback host without a usable shared secret"
# A signed token (non-loopback bind) expires a day after issue; a holder gets a fresh one after half of
# that (`csrf_cookie_needs_refresh`), and every page load issues one anyway.
CSRF_TOKEN_MAX_AGE_SECONDS = 24 * 60 * 60
# A token stamped slightly in the future (clock adjusted between issue and use) still verifies.
_CSRF_TOKEN_CLOCK_SKEW_SECONDS = 300
# `dashboard.csrf_secret` keys the token's HMAC, so it must resist an offline guess. 32 characters is
# what `secrets.token_urlsafe(24)` produces, and the distinct-character floor rejects "aaaa...".
CSRF_SECRET_MIN_LENGTH = 32
CSRF_SECRET_MIN_DISTINCT_CHARS = 8

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_SCHEMES = ("http", "https")
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Headers Tailscale Serve sets on a proxied request (https://tailscale.com/docs/features/tailscale-serve).
TAILSCALE_LOGIN_HEADER = "tailscale-user-login"
TAILSCALE_FUNNEL_HEADER = "tailscale-funnel-request"
PROXY_LOGIN_REJECTED_DETAIL = "Proxied request without an allowed Tailscale login"
FUNNEL_REJECTED_DETAIL = "Requests through Tailscale Funnel are refused"
# Headers a reverse proxy adds and a direct loopback client never sends. Serve forwards the
# client's own Host unchanged, so a tailnet client can send a loopback Host through it; these
# still mark the request as proxied (Serve sets X-Forwarded-For and X-Forwarded-Proto itself).
_PROXY_MARKER_HEADERS = frozenset(
    {"x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "forwarded", TAILSCALE_LOGIN_HEADER}
)


def _strip_brackets(host: str) -> str:
    """Unwrap an IPv6 literal written in URL form (`[::1]` -> `::1`)."""
    return host[1:-1] if host.startswith("[") and host.endswith("]") else host


def is_loopback_host(host: str) -> bool:
    """Return True when `host` names the loopback interface.

    Accepts every spelling a `--host` flag may carry: the literal names in
    `_LOOPBACK_HOSTS`, any address in 127.0.0.0/8, and IPv6 loopback with or
    without URL brackets. A value that is neither `localhost` nor a parseable
    IP address is treated as non-loopback, which is the fail-closed direction.
    """
    bare = _strip_brackets(host)
    if bare in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(bare).is_loopback
    except ValueError:
        return False


def format_host(host: str) -> str:
    """Bracket an IPv6 literal for use in an Origin string (`::1` -> `[::1]`).

    Already-bracketed input is normalized rather than double-bracketed.
    """
    bare = _strip_brackets(host)
    return f"[{bare}]" if ":" in bare else bare


def is_wildcard_host(host: str) -> bool:
    """Return True when `host` binds every interface (`0.0.0.0`, `::`).

    A wildcard bind does not identify the origin a remote browser will present:
    the browser sends the server's real address (its LAN IP or hostname), never
    the literal wildcard. `build_allowed_origins` therefore builds an allowlist
    entry that can never match a real request for a wildcard host, so callers
    must fail closed for one regardless of whether `dashboard.csrf_secret` is
    configured, otherwise the guard looks "unlocked" while silently rejecting
    every legitimate remote request. A value that is neither a wildcard address
    nor a parseable IP is treated as not wildcard (the fail-open direction for
    this specific check, matched by fail-closed elsewhere in the guard).
    """
    bare = _strip_brackets(host)
    try:
        return ipaddress.ip_address(bare).is_unspecified
    except ValueError:
        return False


def build_allowed_origins(host: str, port: int, proxy_origins: Iterable[str] = ()) -> frozenset[str]:
    """Build the set of `Origin` header values considered same-origin as the dashboard.

    For a loopback bind, all loopback hostname spellings are accepted (a user may
    reach the dashboard via `127.0.0.1` or `localhost` interchangeably), plus the
    configured host itself, which need not be one of the canonical spellings
    (`--host 127.0.0.2` is still loopback). For a non-loopback bind, only the
    configured host is accepted.

    Each `proxy_origins` entry (already normalized by `DashboardConfig`, the exact
    string a browser sends) is added as is.
    """
    hosts = (_LOOPBACK_HOSTS | {host}) if is_loopback_host(host) else {host}
    own = {f"{scheme}://{format_host(h)}:{port}" for h in hosts for scheme in _SCHEMES}
    return frozenset(own | set(proxy_origins))


def proxy_hostnames(proxy_origins: Iterable[str]) -> frozenset[str]:
    """The bare, lowercase hostnames of `proxy_origins`, as a Host header names them without its port."""
    return frozenset(h for h in (urlsplit(o).hostname for o in proxy_origins) if h)


def build_allowed_hosts(host: str, proxy_origins: Iterable[str] = ()) -> list[str]:
    """Build the bare hostnames `TrustedHostMiddleware` should accept for a bind host.

    Mirrors `build_allowed_origins`'s loopback handling (all loopback spellings for a
    loopback bind, else only the configured host) so there is one place deciding what
    counts as this dashboard's own address, just without the scheme+port that
    `TrustedHostMiddleware.allowed_hosts` does not take. Callers must handle a
    wildcard bind (`is_wildcard_host`) separately: there is no bare hostname a real
    client's Host header could match, matching this module's CSRF fail-closed path.
    The hostname of each `proxy_origins` entry is added too.
    """
    hosts = (_LOOPBACK_HOSTS | {host}) if is_loopback_host(host) else {host}
    return sorted({_strip_brackets(h) for h in hosts} | proxy_hostnames(proxy_origins))


def _host_header_name(value: str) -> str:
    """The lowercase hostname of a Host header value, without its port or IPv6 brackets."""
    value = value.strip().lower()
    if value.startswith("["):
        return value[1 : value.find("]")] if "]" in value else value
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


class ProxyIdentityMiddleware:
    """Refuse a proxied request that does not carry an allowed tailnet identity.

    A pure ASGI middleware so it covers every HTTP route (pages, static files, the API,
    SSE streams) and the websocket handshake alike; Starlette's `@app.middleware("http")`
    never sees a websocket scope. Installed whatever `dashboard.proxy_origins` holds: with
    no proxy origin, `create_app()` passes an empty `allowed_logins`, so every request
    carrying a proxy marker header is refused whatever login it sends, while a plain
    loopback request passes.

    - A request carrying `Tailscale-Funnel-Request` is refused whatever its Host: the
      dashboard is never meant to be public, and Funnel traffic carries no identity.
    - A proxied request must carry `Tailscale-User-Login` equal (case-insensitively) to
      one of `allowed_logins`; an empty `allowed_logins` refuses every proxied request.
      A request is proxied when its Host names a proxy origin or it carries any header a
      proxy adds (`_PROXY_MARKER_HEADERS`): Serve forwards the client's Host unchanged, so
      the Host alone would let a tailnet client skip this check by sending a loopback Host.
    - Any other request (a direct loopback client) passes through untouched.

    An HTTP refusal is a 403 JSON response; a websocket refusal closes the handshake
    with 1008 before accept, which a server sends as HTTP 403, matching
    `validate_websocket_origin`'s refusal.
    """

    def __init__(self, app: ASGIApp, proxy_hosts: Iterable[str], allowed_logins: Iterable[str]) -> None:
        self.app = app
        self.proxy_hosts = frozenset(h.lower() for h in proxy_hosts)
        self.allowed_logins = frozenset(login.casefold() for login in allowed_logins)

    def _refusal(self, scope: Scope) -> str | None:
        """The reason to refuse `scope`, or None when it may pass."""
        headers: dict[str, str] = {}
        for raw_name, raw_value in scope.get("headers", ()):
            name = raw_name.decode("latin-1").lower()
            # The first occurrence wins, matching Starlette's `Headers.get()`.
            headers.setdefault(name, raw_value.decode("latin-1"))
        if TAILSCALE_FUNNEL_HEADER in headers:
            return FUNNEL_REJECTED_DETAIL
        to_proxy_host = _host_header_name(headers.get("host", "")) in self.proxy_hosts
        has_proxy_header = not _PROXY_MARKER_HEADERS.isdisjoint(headers)
        if not (to_proxy_host or has_proxy_header):
            return None
        login = headers.get(TAILSCALE_LOGIN_HEADER, "").strip().casefold()
        if login and login in self.allowed_logins:
            return None
        return PROXY_LOGIN_REJECTED_DETAIL

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        reason = self._refusal(scope)
        if reason is None:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await WebSocketClose(code=1008, reason=reason)(scope, receive, send)
            return
        await JSONResponse({"detail": reason}, status_code=403)(scope, receive, send)


# Reachable on a fail-closed bind (`create_app()`'s `refuse_when_fail_closed`): status probes that
# render no project data, the agent cards (static capability documents), and the machine-to-machine
# endpoints that carry their own authentication and sit outside the CSRF guard by design (telemetry
# ingest's token, MCP's and A2A cancel's run-scoped bearer token). The A2A task reads
# (`/a2a/tasks`, `/a2a/tasks/<id>`) carry no authentication, so they stay refused.
_FAIL_CLOSED_EXEMPT_PATHS = frozenset(
    {
        "/healthz",
        "/api/health",
        "/api/health/connectivity",
        "/api/telemetry/ingest",
        "/api/mcp",
        "/.well-known/agent.json",
    }
)


def is_fail_closed_exempt(path: str) -> bool:
    """True when a fail-closed bind still serves `path` (see `_FAIL_CLOSED_EXEMPT_PATHS`).

    The per-project MCP endpoint (`/p/<slug>/api/mcp`) is exempt like the top-level one, and so are
    the A2A cancel route (`/a2a/tasks/<id>/cancel`, which needs a run-scoped bearer token) and the
    per-role agent cards (`/a2a/<role>/agent.json`). Pages, the CSRF-guarded routers, the A2A task
    reads and the scheduler's activity routes stay refused.
    """
    if path in _FAIL_CLOSED_EXEMPT_PATHS:
        return True
    parts = path.split("/")
    if len(parts) == 5 and parts[1] == "p" and bool(parts[2]) and parts[3:] == ["api", "mcp"]:
        return True
    if len(parts) == 5 and parts[:3] == ["", "a2a", "tasks"] and bool(parts[3]) and parts[4] == "cancel":
        return True
    return len(parts) == 4 and parts[:2] == ["", "a2a"] and bool(parts[2]) and parts[3] == "agent.json"


def validate_origin(request: Request, allowed_origins: frozenset[str]) -> bool:
    """Check the request's Origin (falling back to Referer) against `allowed_origins`.

    Both absent is treated as a rejection: a legitimate same-origin request from the
    dashboard's own JS always sends at least one of them.
    """
    origin = request.headers.get("origin")
    if origin is not None:
        return origin in allowed_origins

    referer = request.headers.get("referer")
    if referer is not None:
        return any(referer == o or referer.startswith(f"{o}/") for o in allowed_origins)

    return False


def validate_websocket_origin(websocket: WebSocket) -> bool:
    """Check a websocket handshake's Origin against the dashboard's allowlist.

    Reads the same `app.state` the HTTP guard uses (`allowed_origins`, `csrf_fail_closed`),
    so both accept exactly the same origins. Browsers do not apply the same-origin policy
    to websockets, so any page the user visits could otherwise open this socket.

    A missing Origin is refused (browsers always send it on a handshake; a non-browser
    client must set it explicitly), and `Referer` is deliberately not a fallback because
    it is unreliable on handshakes. An unset `allowed_origins` fails closed.
    """
    state = websocket.app.state
    if getattr(state, "csrf_fail_closed", False):
        return False
    origin = websocket.headers.get("origin")
    if origin is None:
        return False
    allowed_origins: frozenset[str] = getattr(state, "allowed_origins", frozenset())
    if origin not in allowed_origins:
        return False
    # A browser cannot set a header on a handshake, so the socket follows the read rule: on a
    # non-loopback bind the cookie it sends automatically must carry a valid signature.
    secret: str = getattr(state, "csrf_signing_secret", "")
    return not secret or verify_signed_csrf_token(websocket.cookies.get(CSRF_COOKIE_NAME, ""), secret)


def _tokens_equal(a: str, b: str) -> bool:
    """Constant-time equality that, unlike `hmac.compare_digest` on str, accepts non-ASCII client input."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def is_strong_csrf_secret(secret: str) -> bool:
    """True when `secret` is long and varied enough to key the signed CSRF token.

    Before issue #1568 only the secret's presence was checked, so an existing install may hold a
    trivial opt-in value ("1", "yes"). Anyone who loads a page gets a signed token, and a weak key
    can be brute-forced from it offline, so a non-loopback bind refuses to start signing with one.
    """
    return len(secret) >= CSRF_SECRET_MIN_LENGTH and len(set(secret)) >= CSRF_SECRET_MIN_DISTINCT_CHARS


def _token_signature(payload: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_csrf_token(secret: str, *, issued_at: int | None = None) -> str:
    """A fresh `<issued_at>.<nonce>.<signature>` token.

    The signature is an HMAC-SHA256 of `<issued_at>.<nonce>` keyed by `secret`, and `issued_at` is
    the Unix time in whole seconds (now unless given), so a leaked token expires on its own.
    """
    ts = int(time.time()) if issued_at is None else issued_at
    payload = f"{ts}.{secrets.token_urlsafe(32)}"
    return f"{payload}.{_token_signature(payload, secret)}"


def _signed_token_issued_at(token: str, secret: str) -> int | None:
    """The issue time of a token `secret` signed, or None for a malformed or forged one (age not checked)."""
    payload, sep, signature = token.rpartition(".")
    ts_text, ts_sep, nonce = payload.partition(".")
    if not (sep and ts_sep and nonce and signature and secret and ts_text.isascii() and ts_text.isdigit()):
        return None
    if not _tokens_equal(signature, _token_signature(payload, secret)):
        return None
    return int(ts_text)


def _token_age(token: str, secret: str) -> float | None:
    """Seconds since a valid, unexpired token was issued, or None when it is not one."""
    issued_at = _signed_token_issued_at(token, secret)
    if issued_at is None:
        return None
    age = time.time() - issued_at
    if age < -_CSRF_TOKEN_CLOCK_SKEW_SECONDS or age > CSRF_TOKEN_MAX_AGE_SECONDS:
        return None
    return age


def verify_signed_csrf_token(token: str, secret: str) -> bool:
    """True only for a token `secret` signed no more than `CSRF_TOKEN_MAX_AGE_SECONDS` ago."""
    return _token_age(token, secret) is not None


def csrf_cookie_needs_refresh(request: Request) -> bool:
    """True when the request carries a valid signed cookie past half its lifetime.

    The dashboard's JS keeps a page open for days and reads the cookie on every call, so a holder
    of a valid token gets a fresh one before it expires (`CsrfCookieRefreshMiddleware`).
    Only a token that still verifies is renewed, so renewal never hands a token to a new client.
    """
    secret: str = getattr(request.app.state, "csrf_signing_secret", "")
    if not secret:
        return False
    age = _token_age(request.cookies.get(CSRF_COOKIE_NAME, ""), secret)
    return age is not None and age > CSRF_TOKEN_MAX_AGE_SECONDS / 2


def _sets_csrf_cookie(raw_headers: Iterable[tuple[bytes, bytes]]) -> bool:
    """True when `raw_headers` already carry a Set-Cookie for the CSRF cookie (a page route issued one)."""
    prefix = f"{CSRF_COOKIE_NAME}=".encode("latin-1")
    return any(name.lower() == b"set-cookie" and value.startswith(prefix) for name, value in raw_headers)


class CsrfCookieRefreshMiddleware:
    """Renew a signed CSRF cookie past half its lifetime, so a page left open never sees it expire.

    A pure ASGI middleware that `create_app()` adds before `TrustedHostMiddleware` and
    `ProxyIdentityMiddleware`, so it runs inside both: a request either refuses (bad Host, a proxied
    request without an allowed login) never reaches it, and its refusal carries no renewed token.
    A no-op on a loopback bind (the token is not signed there). Only a cookie that still verifies is
    renewed (`csrf_cookie_needs_refresh`), and `issue_csrf_cookie` itself refuses a foreign Origin.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if not csrf_cookie_needs_refresh(request):
            await self.app(scope, receive, send)
            return

        async def send_with_renewal(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", ()))
                if not _sets_csrf_cookie(headers):
                    carrier = Response()
                    if issue_csrf_cookie(carrier, request) is not None:
                        headers.extend(h for h in carrier.raw_headers if h[0] == b"set-cookie")
                        message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_renewal)


def issue_csrf_cookie(response: Response, request: Request) -> str | None:
    """Set a fresh double-submit CSRF cookie on `response` and return its value.

    On a non-loopback bind (`app.state.csrf_signing_secret` set) the token is signed, and
    no cookie is issued (None is returned) to a request whose Origin names another site:
    a page navigation sends no Origin, and a cross-site fetch must never be handed a token.

    Two flags are deliberately not hardcoded on:

    `HttpOnly` is off because the dashboard's own frontend JS must read the cookie to
    echo it back as the `X-CSRF-Token` header. That readability *is* the double-submit
    pattern: the defense rests on a cross-origin page being unable to read a cookie
    scoped to another origin, not on the cookie being hidden from same-origin script.

    `Secure` is derived from the scheme the request actually arrived on rather than
    forced to True, because the default deployment is loopback HTTP. A browser never
    returns a `Secure` cookie over plain HTTP, so forcing it would leave the cookie
    permanently absent and every state-changing request rejected. `request.url.scheme`
    already reflects `X-Forwarded-Proto` when uvicorn runs with `--proxy-headers`, so
    an HTTPS deployment behind a proxy gets the flag without this function having to
    trust a client-supplied header itself.
    """
    secret: str = getattr(request.app.state, "csrf_signing_secret", "")
    if secret:
        origin = request.headers.get("origin")
        if origin is not None and origin not in getattr(request.app.state, "allowed_origins", frozenset()):
            return None
        token = sign_csrf_token(secret)
    else:
        token = secrets.token_urlsafe(32)
    # Rationale for the two suppressed lines below lives in the docstring above.
    response.set_cookie(  # NOSONAR
        key=CSRF_COOKIE_NAME,
        value=token,
        httponly=False,  # NOSONAR
        secure=request.url.scheme == "https",  # NOSONAR
        samesite="strict",
        path="/",
    )
    return token


def require_same_origin_csrf(request: Request) -> None:
    """FastAPI dependency guarding every route of a router, reads included.

    Registered once at router level (covering every route the router carries)
    instead of opted into per mutating route. A per-route opt-in is exactly how
    most of this dashboard's mutating routes went unguarded: a new route is
    covered by construction only if the dependency is unconditionally present
    on every router and decides per method what it requires.

    - Loopback bind: safe methods (GET/HEAD/OPTIONS) pass; a mutating request
      needs a same-origin Origin/Referer and a matching double-submit pair.
    - Non-loopback bind with `dashboard.csrf_secret`: the pair must also carry a
      valid signature (`verify_signed_csrf_token`). A read needs the signed cookie,
      and a header it sends must match it; a browser's EventSource cannot set a
      header, so the cookie it sends by itself is what a read is checked on.
    - Non-loopback bind without a secret, with one `is_strong_csrf_secret`
      rejects, or a wildcard bind: every request is refused (fail-closed).

    Raises `HTTPException(403)` on any refusal.
    """
    state = request.app.state
    if getattr(state, "csrf_fail_closed", False):
        raise HTTPException(status_code=403, detail=FAIL_CLOSED_DETAIL)
    secret: str = getattr(state, "csrf_signing_secret", "")
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    header_token = request.headers.get(CSRF_HEADER_NAME)

    if request.method in _SAFE_METHODS:
        if not secret:
            return
        header_mismatch = header_token is not None and not _tokens_equal(header_token, cookie_token or "")
        if not cookie_token or header_mismatch or not verify_signed_csrf_token(cookie_token, secret):
            raise HTTPException(status_code=403, detail=CSRF_REJECTED_DETAIL)
        return

    allowed_origins: frozenset[str] = getattr(state, "allowed_origins", frozenset())
    if not validate_origin(request, allowed_origins):
        raise HTTPException(status_code=403, detail="Origin check failed")

    # On a loopback bind this proves only that the two values match: a direct client can set
    # the same value in both places, which is the "access to the machine implies access to the
    # dashboard" model. On a non-loopback bind the signature check below closes that.
    if not cookie_token or not header_token or not _tokens_equal(cookie_token, header_token):
        raise HTTPException(status_code=403, detail=CSRF_REJECTED_DETAIL)
    if secret and not verify_signed_csrf_token(cookie_token, secret):
        raise HTTPException(status_code=403, detail=CSRF_REJECTED_DETAIL)
