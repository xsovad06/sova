"""BriefingService: aggregates awareness items from all providers."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import TYPE_CHECKING

from sova.awareness.base import AwarenessProvider, ItemCategory
from sova.awareness.rendering.models import Briefing, ProviderStatus
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.awareness.base import AwarenessItem

_log = get_logger(component="awareness.briefing")


class BriefingService:
    """Aggregates awareness items from all providers into a unified briefing."""

    def __init__(self, providers: list[AwarenessProvider]) -> None:
        self.providers = providers

    async def generate_briefing(
        self,
        since: datetime | None = None,
    ) -> Briefing:
        """Fetch all providers and build a prioritized briefing."""
        all_items: list[AwarenessItem] = []
        statuses: list[ProviderStatus] = []

        fetch_tasks = [self._fetch_provider(provider, since) for provider in self.providers]
        results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

        for provider, result in zip(self.providers, results):
            if isinstance(result, Exception):
                _log.warning(
                    "provider_fetch_failed",
                    provider=provider.name,
                    error=str(result),
                )
                statuses.append(
                    ProviderStatus(
                        name=provider.name,
                        ok=False,
                        message=str(result),
                    )
                )
                continue

            items, fetch_time_ms = result
            all_items.extend(items)
            statuses.append(
                ProviderStatus(
                    name=provider.name,
                    ok=True,
                    message="ok",
                    items_fetched=len(items),
                    fetch_time_ms=fetch_time_ms,
                )
            )

        attention = sorted(
            [i for i in all_items if i.category == ItemCategory.NEEDS_ATTENTION],
            key=lambda i: (-i.urgency, _ts_sort_key(i.timestamp)),
        )
        schedule = sorted(
            [i for i in all_items if i.provider == "gcal"],
            key=lambda i: i.timestamp or datetime.max,
        )
        # Calendar events go in the schedule section; exclude from informational
        # to avoid duplication. Urgent calendar items still appear in attention.
        schedule_ids = {i.id for i in schedule}
        informational = sorted(
            [i for i in all_items if i.category == ItemCategory.INFORMATIONAL and i.id not in schedule_ids],
            key=lambda i: _ts_sort_key(i.timestamp),
        )

        return Briefing(
            generated_at=datetime.now(),
            attention_items=attention,
            informational_items=informational,
            schedule=schedule,
            provider_statuses=statuses,
            since=since,
        )

    async def _fetch_provider(
        self,
        provider: AwarenessProvider,
        since: datetime | None,
    ) -> tuple[list[AwarenessItem], int]:
        """Fetch items from a single provider, returning items and fetch time in ms."""
        start = time.monotonic()
        items = await provider.fetch_items(since=since)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        _log.info(
            "provider_fetched",
            provider=provider.name,
            items=len(items),
            elapsed_ms=elapsed_ms,
        )
        return items, elapsed_ms


def _ts_sort_key(ts: datetime | None) -> tuple[int, float]:
    """Sort key for timestamps: newest first, None sorts last."""
    if ts is None:
        return (1, 0.0)
    return (0, -ts.timestamp())
