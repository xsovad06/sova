"""Async wrapper around BriefingService and the awareness provider registry.

Owns briefing serialization for every consumer: the `/api/briefing` page
endpoints and the activity feed's briefing card.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from sova.awareness import create_providers
from sova.awareness.base import AwarenessItem, AwarenessProvider
from sova.awareness.briefing import BriefingService
from sova.awareness.rendering.models import Briefing
from sova.config.loader import load_config
from sova.dashboard.services.agent_pool import get_default_project_dir
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.awareness")

# Session-scoped dismiss tracking, keyed by project so a dismissal in one
# project never suppresses the same item id in another (multi-project mode
# shares this module-level state across all registered projects). Each
# per-project bucket is bounded (FIFO eviction) so stale/garbage ids can't
# grow it unboundedly for the lifetime of the server process. Dismissed items
# reappear on server restart; persistent dismiss state can be added later via
# the DB if needed.
_dismissed: dict[str, dict[str, None]] = {}
_MAX_DISMISSED_PER_PROJECT = 500

# Ids seen in the most recently generated briefing per project, so a dismiss
# call for an id that was never actually shown (stale client, garbage input)
# is visible in logs instead of silently succeeding.
_known_item_ids: dict[str, set[str]] = {}


def _project_key(project_dir: Path | None) -> str:
    """Resolve a stable per-project key for scoping dismiss state."""
    resolved = project_dir or get_default_project_dir()
    return str(resolved.resolve()) if resolved else "__default__"


def _get_providers(project_dir: Path | None) -> list[AwarenessProvider]:
    """Instantiate configured providers for the project, or [] if unconfigured."""
    cfg = load_config(project_dir or get_default_project_dir())
    if not cfg.awareness.enabled or not cfg.awareness.providers:
        return []
    try:
        # create_providers() already catches per-provider constructor failures
        # (logs + skips that provider); this guards the call itself so a
        # malformed cfg.awareness object can't take down the whole page.
        return create_providers(cfg.awareness)
    except Exception:  # noqa: BLE001 (a malformed provider config must degrade, never crash the page)
        log.warning("create_providers_failed", exc_info=True)
        return []


def is_awareness_enabled(project_dir: Path | None) -> bool:
    """Whether awareness is enabled for the project, used to gate the nav item.

    Fails closed (False) on any config error so a broken/missing config never
    crashes page rendering, just hides the nav entry.
    """
    try:
        cfg = load_config(project_dir or get_default_project_dir())
    except Exception:  # noqa: BLE001 (fails closed so a broken config only hides the nav item)
        return False
    return bool(cfg.awareness.enabled)


async def get_briefing(project_dir: Path | None, *, since: datetime | None = None) -> dict[str, Any]:
    """Generate and serialize a full briefing for the given project.

    Returns an empty-but-valid briefing when awareness is disabled or no
    providers are configured, so callers can render an empty state instead
    of an error.
    """
    providers = _get_providers(project_dir)
    if not providers:
        return empty_briefing()

    briefing = await BriefingService(providers).generate_briefing(since=since)
    return serialize_briefing(
        briefing, display_names={p.name: p.display_name for p in providers}, project_dir=project_dir
    )


async def get_provider_statuses(project_dir: Path | None) -> list[dict[str, Any]]:
    """Return health status for each configured provider, without fetching items."""
    providers = _get_providers(project_dir)
    if not providers:
        return []

    results = await asyncio.gather(*(p.health_check() for p in providers), return_exceptions=True)
    statuses: list[dict[str, Any]] = []
    for provider, result in zip(providers, results):
        if isinstance(result, BaseException):
            log.warning("provider_health_check_failed", provider=provider.name, error=str(result))
            ok, message = False, str(result)
        else:
            ok, message = result
        statuses.append({"name": provider.name, "display_name": provider.display_name, "ok": ok, "message": message})
    return statuses


def dismiss_item(project_dir: Path | None, item_id: str) -> None:
    """Mark an awareness item as dismissed for the rest of this process's lifetime.

    Scoped to the project so the dismissal doesn't leak across other projects
    sharing this process in multi-project mode.
    """
    key = _project_key(project_dir)
    known = _known_item_ids.get(key)
    # Only warn once a briefing has actually been generated for this project;
    # an empty `known` set means we have no baseline to judge "unrecognized" by.
    if known and item_id not in known:
        log.warning("briefing.dismiss.unrecognized_id", project=key, item_id=item_id)
    bucket = _dismissed.setdefault(key, {})
    bucket.pop(item_id, None)  # re-insert at the end so repeat dismissals don't get FIFO-evicted early
    bucket[item_id] = None
    if len(bucket) > _MAX_DISMISSED_PER_PROJECT:
        log.debug("briefing.dismiss.evict_oldest", project=key, size=len(bucket))
        bucket.pop(next(iter(bucket)))
    log.debug("briefing.dismiss", project=key, item_id=item_id)


def empty_briefing() -> dict[str, Any]:
    """An empty briefing payload, shaped like `serialize_briefing()` output."""
    return {
        "generated_at": None,
        "attention_items": [],
        "informational_items": [],
        "schedule": [],
        "project_pulses": [],
        "provider_statuses": [],
        "since": None,
    }


def serialize_item(item: AwarenessItem) -> dict[str, Any]:
    """Serialize one awareness item, including optional recurrence metadata."""
    result = {
        "id": item.id,
        "provider": item.provider,
        "category": item.category.value if hasattr(item.category, "value") else str(item.category),
        "title": item.title,
        "body": item.body,
        "source_url": item.source_url,
        "timestamp": item.timestamp.isoformat() if item.timestamp else None,
        "urgency": item.urgency,
        "action_hint": item.action_hint,
    }
    occurrence_count = getattr(item, "occurrence_count", 0)
    if occurrence_count:
        result["occurrence_count"] = occurrence_count
    metadata = getattr(item, "metadata", {})
    if metadata.get("is_recurring_exception"):
        result["is_recurring_exception"] = True
    if metadata.get("recurring_event_id"):
        result["recurring_event_id"] = metadata["recurring_event_id"]
    return result


def _visible(project_dir: Path | None, items: list[AwarenessItem]) -> list[dict[str, Any]]:
    dismissed = _dismissed.get(_project_key(project_dir), {})
    return [serialize_item(i) for i in items if i.id not in dismissed]


def serialize_briefing(
    briefing: Briefing,
    display_names: dict[str, str] | None = None,
    project_dir: Path | None = None,
) -> dict[str, Any]:
    """Serialize a briefing to JSON, omitting items the user dismissed.

    `display_names` maps a provider's config key to its human label so status
    badges can render "Google Calendar" instead of the raw `gcal` key.
    `ProviderStatus` carries only the key, so the mapping is supplied by the
    caller, which still holds the provider instances. `project_dir` scopes
    dismissed-item filtering to the right project (defaults to the
    single-project fallback when omitted).
    """
    names = display_names or {}
    key = _project_key(project_dir)
    _known_item_ids[key] = {
        i.id for i in (*briefing.attention_items, *briefing.informational_items, *briefing.schedule)
    }
    return {
        "generated_at": briefing.generated_at.isoformat() if briefing.generated_at else None,
        "attention_items": _visible(project_dir, briefing.attention_items),
        "informational_items": _visible(project_dir, briefing.informational_items),
        "schedule": _visible(project_dir, briefing.schedule),
        "project_pulses": [
            {
                "project_slug": p.project_slug,
                "open_prs": p.open_prs,
                "agent_status": p.agent_status,
                "last_ci": p.last_ci,
            }
            for p in briefing.project_pulses
        ],
        "provider_statuses": [
            {
                "name": s.name,
                "display_name": names.get(s.name, s.name),
                "ok": s.ok,
                "message": s.message,
                "items_fetched": s.items_fetched,
                "fetch_time_ms": s.fetch_time_ms,
            }
            for s in briefing.provider_statuses
        ],
        "since": briefing.since.isoformat() if briefing.since else None,
    }
