"""Per-minute request limit for the dashboard's included API routers (issue #1518).

`dashboard.rate_limit_per_minute` used to rely on `SlowAPIMiddleware` alone, which finds a
route's handler by scanning `app.routes` flat. FastAPI keeps each included router as one
entry there, so every route registered through `include_router()` (all of `/api`) was
exempt. `rate_limit_dependency()` closes that gap the way `require_same_origin_csrf` covers
mutating routes: it is passed to `include_router()` once per router, so every route the
router carries is limited by construction, whatever shape FastAPI stores it in.

The middleware stays for the routes declared on the app itself (`/healthz`, the pages). It
does not see an included-router route in the FastAPI version in use; a version that flattened
`app.routes` would let it count those routes too, against the same per-minute limit, so the
limit a client meets stays the same.

Buckets are per route and per client, the same shape slowapi's `default_limits` gave the
routes it did limit: the Work and phone pages poll several `/api` routes at once, and one
shared bucket across routes would throttle normal dashboard use. The client key is
`rate_limit_key()`, the address uvicorn resolved (behind Tailscale Serve, each device's
tailnet IP from `X-Forwarded-For`), so one device exhausting its budget never throttles another.

Known gaps, accepted because the dashboard listens on loopback or a tailnet only:

- A request whose JSON body does not parse is answered 422 by FastAPI before any dependency
  runs, so it is never counted. A body that parses but fails validation is counted.
- Local agents share the loopback key, so every agent on the host would share one
  `POST /api/mcp` budget. No SOVA client calls that route today: the agents' MCP tools run
  in the stdio server (`sova mcp`), and the `SOVA_MCP_URL` an agent is handed is read by
  nothing in the repository.
"""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request
from limits import RateLimitItemPerMinute
from limits.storage import MemoryStorage
from limits.strategies import FixedWindowRateLimiter
from slowapi.util import get_remote_address

from sova.utils.logging import get_logger

log = get_logger(component="dashboard.rate_limit")

# `app.state` attribute holding the ApiRateLimiter; absent when the limit is 0 (disabled).
API_RATE_LIMITER_STATE = "api_rate_limiter"

# Path templates (relative to the registration prefix) of the SSE routes, which are exempt. Matched
# against the route template, not the request path, so a parameterized route such as
# `/roles/{name}` stays limited when a client asks for `/roles/stream`.
# tests/test_dashboard_rate_limit.py checks every StreamingResponse route is listed.
STREAM_ROUTE_PATHS = frozenset({"/feed/stream", "/agents/{run_id}/output/stream"})


def rate_limit_key(request: Request) -> str | None:
    """Return the client key a request is counted under, or None for an exempt path.

    WebSocket and static paths are exempt. Shared by the slowapi middleware and
    `rate_limit_dependency()`, which also exempts the SSE routes in `STREAM_ROUTE_PATHS`.
    """
    path = request.url.path
    if path.startswith("/ws/") or path.startswith("/static/"):
        return None
    return get_remote_address(request)


class ApiRateLimiter:
    """In-memory, per-process fixed-window counter: slowapi's default storage and strategy."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._item = RateLimitItemPerMinute(per_minute)
        self._storage = MemoryStorage()
        self._strategy = FixedWindowRateLimiter(self._storage)

    def hit(self, client: str, bucket: str) -> int | None:
        """Count one request; return None when allowed, else the seconds until the window resets."""
        if self._strategy.hit(self._item, client, bucket):
            return None
        reset_time, _remaining = self._strategy.get_window_stats(self._item, client, bucket)
        return max(1, math.ceil(reset_time - time.time()))

    def reset(self) -> None:
        """Clear every counter."""
        self._storage.reset()


def rate_limit_dependency(prefix: str) -> Callable[[Request], Awaitable[None]]:
    """Build the router-level dependency enforcing the limit for routes mounted under *prefix*.

    The bucket is the method plus *prefix* and the matched route's path template, so a route
    with path parameters is one bucket however many ids a client tries, and the `/api` and
    `/p/{slug}/api` registrations of one route are counted apart. The prefix is passed in
    because the route object FastAPI hands the request is shared by both registrations and
    carries no prefix. A router-level dependency only runs once a route matched, so a 404
    never creates a bucket. A request whose matched route has no path template is let
    through with one warning per process, rather than pooled into a single bucket shared by
    every route under the prefix.

    Place it before `require_same_origin_csrf`, so a request the CSRF guard rejects still
    counts against the budget.
    """

    warned_unresolved = False

    # Async with no await on purpose (python S7503 accepted): FastAPI runs a sync dependency in
    # its threadpool, and this check runs on every /api request.
    async def enforce_rate_limit(request: Request) -> None:  # NOSONAR(S7503)
        nonlocal warned_unresolved
        limiter: ApiRateLimiter | None = getattr(request.app.state, API_RATE_LIMITER_STATE, None)
        if limiter is None:
            return
        client = rate_limit_key(request)
        if client is None:
            return
        route_path = getattr(request.scope.get("route"), "path", None)
        if not isinstance(route_path, str) or not route_path:
            if not warned_unresolved:
                warned_unresolved = True
                log.warning("dashboard.rate_limit_route_unresolved", prefix=prefix, path=request.url.path)
            return
        if route_path in STREAM_ROUTE_PATHS:
            return
        bucket = f"{request.method} {prefix}{route_path}"
        try:
            retry_after = limiter.hit(client, bucket)
        except Exception:  # noqa: BLE001 (a limiter storage failure must not take the API down)
            # Log and allow the request, as slowapi's swallow_errors does.
            log.warning("dashboard.rate_limit_check_failed", bucket=bucket, exc_info=True)
            return
        if retry_after is not None:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded: {limiter.per_minute} per 1 minute",
                headers={"Retry-After": str(retry_after)},
            )

    return enforce_rate_limit
