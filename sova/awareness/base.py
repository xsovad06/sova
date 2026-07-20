"""Base abstractions for awareness data sources."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.config.models import AwarenessConfig

_log = get_logger(component="awareness.base")


class ItemCategory(StrEnum):
    """Classification of an awareness item's actionability."""

    NEEDS_ATTENTION = "needs_attention"
    INFORMATIONAL = "informational"
    DISMISSED = "dismissed"


@dataclass
class AwarenessItem:
    """A single piece of awareness data from any provider."""

    id: str
    provider: str
    category: ItemCategory
    title: str
    body: str = ""
    source_url: str = ""
    timestamp: datetime | None = None
    metadata: dict = field(default_factory=dict)
    urgency: int = 0
    action_hint: str = ""


class AwarenessProvider(ABC):
    """Base class for awareness data sources.

    AwarenessProvider is separate from TaskAdapter. TaskAdapter manages
    issue lifecycles (create, transition, assign, comment). AwarenessProvider
    is read-only: fetch items from a source, categorize them, and report
    what needs attention.
    """

    name: str = ""
    display_name: str = ""

    def __init__(self, config: AwarenessConfig) -> None:
        self.config = config

    @abstractmethod
    async def fetch_items(
        self,
        since: datetime | None = None,
    ) -> list[AwarenessItem]:
        """Fetch awareness items from this source.

        Args:
            since: Only return items created/changed after this time.
                   If None, use provider's default lookback.
        """

    @abstractmethod
    async def is_configured(self) -> bool:
        """Check if this provider has valid credentials/config."""

    async def health_check(self) -> tuple[bool, str]:
        """Check if the provider can connect. Returns (ok, message)."""
        try:
            configured = await self.is_configured()
            if not configured:
                return False, f"{self.display_name}: not configured"
            return True, f"{self.display_name}: ok"
        except Exception as e:
            _log.warning("health_check_failed", provider=self.name, error=str(e))
            return False, f"{self.display_name}: {e}"
