"""DependencyHealthProvider: surfaces high-urgency dependency findings to agent briefings.

Reads each registered project's own dependency_health config and the cached
scan snapshot (via dependency_health_service, same cache the dashboard page
reads). Cache-only: this provider never triggers a scan itself, so a briefing
never fans out to package registries. Only projects with
dependency_health.enabled=True are considered.
Follows the cross-project pattern from agent_runs.py: per-project timeout,
bounded concurrency, failures isolated per project.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from sova.awareness import register_provider
from sova.awareness.base import AwarenessItem, AwarenessProvider, ItemCategory
from sova.config.registry import list_projects
from sova.utils.logging import get_logger

_log = get_logger(component="awareness.dependency_health")

_QUERY_TIMEOUT_SECONDS = 15.0
_MAX_CONCURRENT = 5
_MAX_ITEMS_PER_PROJECT = 5
_HIGH_URGENCY_THRESHOLD = 60


class DependencyHealthProvider(AwarenessProvider):
    """Awareness provider surfacing high-urgency dependencies across registered projects."""

    name = "dependency_health"
    display_name = "Dependency Health"

    async def is_configured(self) -> bool:
        """Return True if at least one registered project has dependency health scanning enabled."""
        return any(_project_enabled(Path(path_str)) for path_str in list_projects().values())

    async def fetch_items(
        self,
        since: datetime | None = None,
    ) -> list[AwarenessItem]:
        """Fetch high-urgency dependency findings from all enabled registered projects."""
        registry = list_projects()
        if not registry:
            return []

        sem = asyncio.Semaphore(_MAX_CONCURRENT)
        tasks = [_safe_project_items(slug, Path(path_str), sem) for slug, path_str in registry.items()]
        results = await asyncio.gather(*tasks)

        items: list[AwarenessItem] = []
        for result in results:
            items.extend(result)
        return items


def _project_enabled(project_dir: Path) -> bool:
    try:
        from sova.config.loader import load_config

        cfg = load_config(project_dir)
        return cfg.dependency_health.enabled
    except Exception:  # noqa: BLE001 (config may fail for many reasons; fail closed to not-enabled)
        _log.debug("dependency_health.config_load_failed", project_dir=str(project_dir), exc_info=True)
        return False


async def _safe_project_items(slug: str, project_dir: Path, sem: asyncio.Semaphore) -> list[AwarenessItem]:
    async with sem:
        try:
            return await asyncio.wait_for(_project_items(slug, project_dir), timeout=_QUERY_TIMEOUT_SECONDS)
        except TimeoutError:
            _log.warning("dependency_health.query_timeout", slug=slug, exc_info=True)
            return []
        except Exception:  # noqa: BLE001 (per-project isolation: one project's failure must not sink the briefing)
            _log.warning("dependency_health.query_failed", slug=slug, exc_info=True)
            return []


async def _project_items(slug: str, project_dir: Path) -> list[AwarenessItem]:
    from sova.config.loader import load_config
    from sova.dashboard.services import dependency_health_service

    cfg = load_config(project_dir)
    dh_cfg = cfg.dependency_health
    if not dh_cfg.enabled:
        return []

    # cache_only: a cold-cache scan issues one registry request per package, which
    # cannot finish inside _QUERY_TIMEOUT_SECONDS. Without this the briefing would
    # start a scan, be cancelled at the timeout before anything is cached, and repeat
    # that wasted work on every cycle. The dashboard page and POST /refresh own scanning.
    snapshot = await dependency_health_service.get_snapshot(
        project_dir,
        enabled=dh_cfg.enabled,
        cache_ttl_minutes=dh_cfg.cache_ttl_minutes,
        registry_timeout_seconds=dh_cfg.registry_timeout_seconds,
        cache_only=True,
    )

    flagged = [p for p in snapshot.packages if p.urgency >= _HIGH_URGENCY_THRESHOLD or p.deprecated or p.vulnerable]
    items: list[AwarenessItem] = []
    for pkg in flagged[:_MAX_ITEMS_PER_PROJECT]:
        reasons = []
        if pkg.vulnerable:
            reasons.append("known vulnerability")
        if pkg.deprecated:
            reasons.append("deprecated")
        if pkg.lag_category == "major":
            reasons.append("major version behind")
        reason_text = ", ".join(reasons) if reasons else pkg.lag_category
        items.append(
            AwarenessItem(
                id=f"dependency_health:{slug}:{pkg.ecosystem}:{pkg.name}",
                provider="dependency_health",
                category=ItemCategory.NEEDS_ATTENTION,
                title=f"[{slug}] {pkg.name}: {reason_text}",
                body=f"Current {pkg.current_version or 'unknown'} -> latest {pkg.latest_version or 'unknown'}",
                urgency=2 if (pkg.vulnerable or pkg.deprecated) else 1,
                action_hint="Review dependency health page",
                metadata={"project_slug": slug, "package": pkg.name, "ecosystem": pkg.ecosystem},
            )
        )
    return items


register_provider("dependency_health", DependencyHealthProvider)
