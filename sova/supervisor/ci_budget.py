"""CI minutes budget tracking via GitHub Actions billing API.

Queries the GitHub Actions billing endpoint and caches results in-memory
with a configurable TTL. Consumed by the supervisor progression engine
(gate check) and the dashboard (stat tile).

Supports both the legacy ``/settings/billing/actions`` endpoint and the
new ``/settings/billing/usage`` endpoint (GitHub deprecated the legacy
endpoint with HTTP 410 in mid-2026).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.utils.shell import ShellResult

log = get_logger(component="supervisor.ci_budget")

_DEFAULT_TTL_SECONDS = 600.0
_UNLIMITED_SENTINEL = 999_999

_PLAN_INCLUDED_MINUTES: dict[str, int] = {
    "free": 2000,
    "pro": 3000,
    "team": 3000,
    "enterprise": 50000,
}


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

        budget = await self._try_new_usage_api(owner, env)
        if budget is not None:
            return budget

        result = await self._try_legacy_billing_endpoints(owner, env)
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

        return _parse_legacy_billing_response(data)

    async def _try_new_usage_api(self, owner: str, env: dict | None) -> CIBudget | None:
        """Try the new /settings/billing/usage endpoint. Return budget or None to fall back.

        Uses year/month params for detailed per-run data and filters out public
        repos (their Actions minutes are free and don't count against the quota).
        """
        import json

        from sova.utils.shell import run

        now = datetime.now(timezone.utc)
        endpoint = f"users/{owner}/settings/billing/usage?year={now.year}&month={now.month:02d}"
        try:
            result = await run("gh", "api", endpoint, "--paginate", env=env)
        except Exception:
            log.warning("ci_budget.usage_api_error", endpoint=endpoint, exc_info=True)
            return None

        if not result.success:
            return None

        try:
            data = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            return None

        if not isinstance(data, dict) or "usageItems" not in data:
            return None

        repo_names = {
            str(item.get("repositoryName", ""))
            for item in data.get("usageItems", [])
            if isinstance(item, dict) and str(item.get("product", "")).lower() == "actions"
        }
        public_repos = await self._get_public_repos(owner, repo_names, env)

        included = await self._get_plan_included_minutes(env)
        return _parse_usage_response(data, included, public_repos)

    @staticmethod
    async def _get_public_repos(owner: str, repo_names: set[str], env: dict | None) -> set[str]:
        """Return the subset of repo_names that are public (concurrent checks)."""
        import asyncio

        from sova.utils.shell import run

        async def _check(name: str) -> str | None:
            try:
                result = await run("gh", "api", f"repos/{owner}/{name}", "--jq", ".private", env=env)
            except Exception:
                log.warning("ci_budget.repo_visibility_error", repo=name, exc_info=True)
                return None
            return name if result.success and result.stdout.strip() == "false" else None

        results = await asyncio.gather(*(_check(n) for n in repo_names if n))
        return {r for r in results if r is not None}

    @staticmethod
    async def _get_plan_included_minutes(env: dict | None) -> int:
        """Fetch the user's GitHub plan and return included CI minutes."""
        import json

        from sova.utils.shell import run

        try:
            result = await run("gh", "api", "user", env=env)
        except Exception:
            log.warning("ci_budget.plan_lookup_error", exc_info=True)
            return _PLAN_INCLUDED_MINUTES["free"]

        if not result.success:
            return _PLAN_INCLUDED_MINUTES["free"]

        try:
            data = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            return _PLAN_INCLUDED_MINUTES["free"]

        plan_name = ""
        if isinstance(data, dict):
            plan = data.get("plan")
            if isinstance(plan, dict):
                plan_name = str(plan.get("name", "")).lower()

        return _PLAN_INCLUDED_MINUTES.get(plan_name, _PLAN_INCLUDED_MINUTES["free"])

    @staticmethod
    async def _try_legacy_billing_endpoints(owner: str, env: dict | None) -> ShellResult | None:
        """Try legacy user then org billing endpoints. Return the first successful result, or None."""
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
            if "404" not in stderr and "not found" not in stderr and "410" not in stderr and "moved" not in stderr:
                truncated = result.stderr[:200] if result.stderr else ""
                log.warning("ci_budget.api_error", endpoint=endpoint, stderr=truncated)
                return None
        log.warning("ci_budget.no_billing_endpoint", owner=owner)
        return None


def _parse_usage_response(data: dict, included_minutes: int, public_repos: set[str] | None = None) -> CIBudget:
    """Parse the new /settings/billing/usage response into a CIBudget.

    Only private repo Actions usage counts against the included minutes quota.
    Public repos get unlimited free Actions minutes.
    """
    _public = public_repos or set()

    used_minutes = 0.0
    for item in data.get("usageItems", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("product", "")).lower() != "actions":
            continue
        if str(item.get("unitType", "")).lower() != "minutes":
            continue
        repo_name = str(item.get("repositoryName", ""))
        if repo_name in _public:
            continue
        try:
            qty = item.get("quantity", 0)
            used_minutes += qty if isinstance(qty, (int, float)) else float(qty)
        except (TypeError, ValueError):
            continue
    used = round(used_minutes)

    if included_minutes <= 0:
        return CIBudget(
            total=0,
            used=used,
            remaining=_UNLIMITED_SENTINEL,
            pct_used=0.0,
        )

    remaining = max(0, included_minutes - used)
    pct_used = round((used / included_minutes) * 100, 1)
    return CIBudget(total=included_minutes, used=used, remaining=remaining, pct_used=pct_used)


def _parse_legacy_billing_response(data: dict) -> CIBudget:
    """Parse the legacy GitHub billing API response into a CIBudget."""
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
