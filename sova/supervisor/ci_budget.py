"""CI minutes budget tracking via GitHub Actions billing API.

Queries the GitHub Actions billing endpoint and caches results in-memory
with a configurable TTL. Consumed by the supervisor progression engine
(gate check) and the dashboard (stat tile).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sova.utils.logging import get_logger

log = get_logger(component="supervisor.ci_budget")

_DEFAULT_TTL_SECONDS = 600.0
_UNLIMITED_SENTINEL = 999_999


@dataclass(frozen=True, slots=True)
class CIBudget:
    """Snapshot of GitHub Actions CI minutes usage for the current billing period."""

    total: int
    used: int
    remaining: int
    pct_used: float


def _zero_budget() -> CIBudget:
    return CIBudget(total=0, used=0, remaining=0, pct_used=0.0)


class CIBudgetTracker:
    """Tracks GitHub Actions CI minutes via the billing API with TTL caching."""

    def __init__(self, ttl_seconds: float = _DEFAULT_TTL_SECONDS) -> None:
        self._ttl_seconds = ttl_seconds
        self._cache: dict[str, tuple[float, CIBudget]] = {}

    async def get_budget(self, repo: str, github_user: str = "") -> CIBudget:
        """Return the current CI budget, fetching from the API if the cache is stale."""
        if not repo:
            return _zero_budget()

        now = time.monotonic()
        entry = self._cache.get(repo)
        if entry is not None:
            cached_at, cached_budget = entry
            if (now - cached_at) < self._ttl_seconds:
                return cached_budget

        budget = await self._fetch(repo, github_user)
        self._cache[repo] = (time.monotonic(), budget)
        return budget

    async def _fetch(self, repo: str, github_user: str) -> CIBudget:
        """Fetch billing data from the GitHub Actions API. Fail-open on errors."""
        import json

        try:
            from sova.utils.gh import resolve_gh_env

            env = await resolve_gh_env(github_user) if github_user else None
        except Exception:
            env = None

        owner = repo.split("/")[0] if "/" in repo else repo
        result = await self._try_billing_endpoints(owner, env)
        if result is None:
            return _zero_budget()

        try:
            data = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            log.warning("ci_budget.bad_json", repo=repo)
            return _zero_budget()

        if not isinstance(data, dict):
            log.warning("ci_budget.bad_json", repo=repo)
            return _zero_budget()

        return _parse_billing_response(data)

    @staticmethod
    async def _try_billing_endpoints(owner: str, env: dict | None) -> object | None:
        """Try user then org billing endpoints. Return the first successful result, or None."""
        from sova.utils.shell import run

        endpoints = [
            f"users/{owner}/settings/billing/actions",
            f"orgs/{owner}/settings/billing/actions",
        ]
        for endpoint in endpoints:
            try:
                result = await run("gh", "api", endpoint, env=env)
            except Exception:
                continue
            if result.success:
                return result
            stderr = (result.stderr or "").lower()
            if "404" not in stderr and "not found" not in stderr:
                truncated = result.stderr[:200] if result.stderr else ""
                log.warning("ci_budget.api_error", endpoint=endpoint, stderr=truncated)
                return None
        log.warning("ci_budget.no_billing_endpoint", owner=owner)
        return None


def _parse_billing_response(data: dict) -> CIBudget:
    """Parse the GitHub billing API response into a CIBudget."""
    try:
        total_used = int(data.get("total_minutes_used", 0))
        included = int(data.get("included_minutes", 0))
    except (TypeError, ValueError):
        log.warning("ci_budget.parse_error", data=data)
        return _zero_budget()

    if included <= 0:
        return CIBudget(
            total=0,
            used=total_used,
            remaining=_UNLIMITED_SENTINEL,
            pct_used=0.0,
        )

    remaining = max(0, included - total_used)
    pct_used = round((total_used / included) * 100, 1)
    return CIBudget(total=included, used=total_used, remaining=remaining, pct_used=pct_used)


_trackers: dict[str, CIBudgetTracker] = {}
_DEFAULT_KEY = "__default__"


def get_ci_budget_tracker(identity: str = "") -> CIBudgetTracker:
    """Return a per-identity CI budget tracker (or the default one)."""
    key = identity or _DEFAULT_KEY
    if key not in _trackers:
        _trackers[key] = CIBudgetTracker()
    return _trackers[key]
