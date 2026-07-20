"""Data structures for rendered briefing output."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sova.awareness.base import AwarenessItem


@dataclass
class ProviderStatus:
    """Health and fetch result for a single provider."""

    name: str
    ok: bool
    message: str
    items_fetched: int = 0
    fetch_time_ms: int = 0


@dataclass
class ProjectPulse:
    """One-line status summary for a registered SOVA project."""

    project_slug: str
    open_prs: int = 0
    agent_status: str = "idle"
    last_ci: str = "unknown"


@dataclass
class Briefing:
    """Aggregated, prioritized output from all awareness providers."""

    generated_at: datetime = field(default_factory=datetime.now)
    attention_items: list[AwarenessItem] = field(default_factory=list)
    informational_items: list[AwarenessItem] = field(default_factory=list)
    schedule: list[AwarenessItem] = field(default_factory=list)
    project_pulses: list[ProjectPulse] = field(default_factory=list)
    provider_statuses: list[ProviderStatus] = field(default_factory=list)
    since: datetime | None = None
