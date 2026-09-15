"""Origin/CSRF guard for state-changing dashboard endpoints.

The dashboard has no user accounts or sessions (see `.claude/rules/architecture.md`,
"Dashboard Security Model": single-user loopback tool, access implies machine access).
This module exists to stop a *different* attacker: any web page the user's browser
visits can otherwise issue cross-origin `fetch()` calls at the dashboard's local port.
DNS rebinding makes same-origin policy alone an incomplete defense, since a page's
`Origin` header reflects the address it was loaded from, not where that name resolves.

Two independent checks compose into `require_same_origin_csrf`, a FastAPI dependency
for state-changing routes only (never `GET`/status endpoints):

1. Origin/Referer must match the dashboard's own scheme+host+port
   (`validate_origin` / `build_allowed_origins`).
2. A double-submit CSRF token: a cookie value that a cross-origin page cannot read,
   echoed back as a header by the dashboard's own JS (`issue_csrf_cookie`).

Both are required because a misconfigured proxy can strip Origin/Referer, and because
Origin/Referer are sometimes legitimately absent (e.g. strict referrer policies).

When bound to a non-loopback host, both checks alone are not enough (Origin lets any
page hosted on that same non-loopback address through). In that case the dependency
fails closed unless `dashboard.csrf_secret` is configured, matching the "refuse to
mount unless a shared secret is configured" requirement.

Scope limit worth stating plainly: `csrf_secret` is an opt-in switch, not a
credential. Only its presence is checked, never its value, and neither check in this
module authenticates a caller. Both defend against a *browser* driven cross-origin
request; a direct client (curl) sets Origin and both halves of the double-submit pair
itself. A non-loopback bind is therefore still reachable by anyone who can reach the
port. Giving the secret real force means signing the token with it, reusing the HMAC
generate/validate pair in `sova/dashboard/services/mcp_service.py`, and giving the
operator a way to supply it; that belongs with the auth router that first consumes
this guard.
"""

from __future__ import annotations

import hmac
import ipaddress
import secrets

from fastapi import HTTPException, Request, Response

CSRF_COOKIE_NAME = "sova_csrf"
CSRF_HEADER_NAME = "x-csrf-token"

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_SCHEMES = ("http", "https")


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


def _format_host(host: str) -> str:
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


def build_allowed_origins(host: str, port: int) -> frozenset[str]:
    """Build the set of `Origin` header values considered same-origin as the dashboard.

    For a loopback bind, all loopback hostname spellings are accepted (a user may
    reach the dashboard via `127.0.0.1` or `localhost` interchangeably), plus the
    configured host itself, which need not be one of the canonical spellings
    (`--host 127.0.0.2` is still loopback). For a non-loopback bind, only the
    configured host is accepted.
    """
    hosts = (_LOOPBACK_HOSTS | {host}) if is_loopback_host(host) else {host}
    return frozenset(f"{scheme}://{_format_host(h)}:{port}" for h in hosts for scheme in _SCHEMES)


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


def issue_csrf_cookie(response: Response, request: Request) -> str:
    """Set a fresh double-submit CSRF cookie on `response` and return its value.

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
    # Rationale for the two suppressed lines below lives in the docstring above.
    token = secrets.token_urlsafe(32)
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
    """FastAPI dependency guarding state-changing routes.

    Raises `HTTPException(403)` when the origin check fails, the CSRF token is
    missing or mismatched, or the app is bound non-loopback with no shared secret
    configured (fail-closed).
    """
    if getattr(request.app.state, "csrf_fail_closed", False):
        raise HTTPException(status_code=403, detail="Dashboard is bound to a non-loopback host without a shared secret")

    allowed_origins: frozenset[str] = getattr(request.app.state, "allowed_origins", frozenset())
    if not validate_origin(request, allowed_origins):
        raise HTTPException(status_code=403, detail="Origin check failed")

    # Double-submit only: this proves the two values match, not that either is
    # trustworthy. A direct (non-browser) client can set an identical arbitrary
    # value in both places and pass. That is deliberate for this PR (see the
    # module docstring's "Scope limit worth stating plainly" paragraph) and is
    # closed by a future issue that signs the token with `dashboard.csrf_secret`.
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    header_token = request.headers.get(CSRF_HEADER_NAME)
    if not cookie_token or not header_token or not hmac.compare_digest(cookie_token, header_token):
        raise HTTPException(status_code=403, detail="Missing or invalid CSRF token")
