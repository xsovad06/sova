"""Look up the Jira tickets that PR rows link to but the work queue does not carry.

The queue's JQL (``task_source.jira_jql_filter``) deliberately excludes some tickets, so a PR linked to one
of them has no queue task to take its priority and status from. This fetches those tickets by key in one
bounded request per refresh, cached per Jira project. The result feeds display and ranking only, never what
SOVA picks up for work.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Collection
from typing import TYPE_CHECKING

from sova.adapters.base import AdapterError, Task
from sova.adapters.jira import MAX_BULK_FETCH_KEYS
from sova.utils.log_dedup import log_once
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.config.models import ProjectConfig

log = get_logger(component="dashboard.jira_tickets")

# Same lifetime as the queue's task list (queue_service._QUEUE_CACHE_TTL). The work items are rebuilt by the
# snapshot producer off the request path, so a lapsed entry never blocks a request on Jira.
_CACHE_TTL_SECONDS = 120.0

# A Jira outage fails every refresh; one warning per window per project is enough to notice it.
_FAILURE_LOG_TTL_SECONDS = 300.0

# Keyed by Jira base URL, project key, the credentials that fetched the tickets (the token only as a hash) and
# the mappings that shaped them, so one project's tickets never answer a row of a project that would see or
# read them differently, and a credential or mapping edit misses instead of serving the old view.
_CacheKey = tuple[str, str, str, str, frozenset[tuple[str, str]], frozenset[tuple[str, str]]]
# Value: (fetched at, keys requested, tickets found by key).
_cache: dict[_CacheKey, tuple[float, frozenset[str], dict[str, Task]]] = {}


def clear_jira_ticket_cache() -> None:
    """Drop every cached lookup (tests, and a settings change that moves the project)."""
    _cache.clear()


async def lookup_jira_tickets(cfg: ProjectConfig, keys: Collection[str]) -> dict[str, Task]:
    """Return the tickets for ``keys`` by upper-cased key; a missing, hidden or failed one is simply absent.

    Makes no call when there is nothing to look up or the project is not Jira-backed. Past
    ``MAX_BULK_FETCH_KEYS`` keys the rest are skipped (logged once), and their rows keep their label-derived
    values. A failed request is logged (rate limited) and returns no tickets, so every row falls back the same
    way, and is not cached. Any failure, an unexpected one included, ends the same way: this lookup only
    decorates rows, so it must never fail the work items it feeds.
    """
    ts = cfg.task_source
    wanted = frozenset(k.strip().upper() for k in keys if k and k.strip())
    if not wanted or not ts.is_jira:
        return {}
    cache_key: _CacheKey = (
        ts.jira_base_url.rstrip("/"),
        ts.jira_project_key.upper(),
        ts.jira_email,
        hashlib.sha256(ts.jira_api_token.encode()).hexdigest(),
        frozenset(ts.jira_priority_mapping.items()),
        frozenset(ts.jira_status_mapping.items()),
    )
    project = f"{cache_key[0]}|{cache_key[1]}"
    if len(wanted) > MAX_BULK_FETCH_KEYS:
        log_once(
            log,
            "jira_tickets.lookup_capped",
            project,
            level="info",
            requested=len(wanted),
            cap=MAX_BULK_FETCH_KEYS,
        )
        wanted = frozenset(sorted(wanted)[:MAX_BULK_FETCH_KEYS])

    now = time.monotonic()
    cached = _cache.get(cache_key)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS and wanted <= cached[1]:
        return {k: t for k, t in cached[2].items() if k in wanted}

    from sova.adapters import close_adapter, create_adapter
    from sova.adapters.jira import JiraAdapter

    try:
        adapter = create_adapter(cfg)
    except Exception:  # noqa: BLE001 (a misconfigured adapter falls back to labels and never fails the page)
        log_once(log, "jira_tickets.adapter_unavailable", project, ttl_seconds=_FAILURE_LOG_TTL_SECONDS, exc_info=True)
        return {}
    try:
        if not isinstance(adapter, JiraAdapter):
            return {}
        tasks = await adapter.get_tasks_by_keys(sorted(wanted))
    except AdapterError as e:
        log_once(log, "jira_tickets.lookup_failed", project, ttl_seconds=_FAILURE_LOG_TTL_SECONDS, error=str(e))
        return {}
    except Exception:  # noqa: BLE001 (any lookup failure falls back to labels and never fails the page)
        log_once(log, "jira_tickets.lookup_error", project, ttl_seconds=_FAILURE_LOG_TTL_SECONDS, exc_info=True)
        return {}
    finally:
        await close_adapter(adapter)

    found = {str(t.metadata.get("key", "")).upper(): t for t in tasks if t.metadata.get("key")}
    _cache[cache_key] = (time.monotonic(), wanted, found)
    return found
