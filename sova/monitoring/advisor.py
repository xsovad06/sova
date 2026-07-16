"""Capacity advisor -- recommends max_parallel_agents based on historical resource data.

Pure logic module. All DB queries and psutil calls happen in the caller
(resource_service.get_capacity_recommendation); this module receives
pre-fetched data and returns a recommendation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

ConfidenceLevel = Literal["insufficient", "low", "medium", "high"]

_MIN_SUMMARIES = 3
_STALE_HOURS = 24


@dataclass(frozen=True, slots=True)
class CapacityRecommendation:
    """Result of the capacity advisor analysis."""

    recommended_max: int
    current_max: int
    confidence: ConfidenceLevel
    headroom_cpu_percent: float | None
    headroom_memory_percent: float | None
    reason: str


def recommend_capacity(
    summaries: list[dict],
    current_max: int,
    cpu_count: int,
    total_memory_bytes: int,
    current_cpu_percent: float,
    current_memory_percent: float,
    cross_project_cpu_percent: float = 0.0,
    safety_margin: float = 0.2,
    latest_summary_time: datetime | None = None,
) -> CapacityRecommendation:
    """Compute a capacity recommendation from historical resource summaries.

    Args:
        summaries: List of dicts with keys: avg_cpu_percent, peak_cpu_percent,
            peak_memory_rss_bytes.
        current_max: Current max_parallel_agents setting.
        cpu_count: Number of logical CPUs.
        total_memory_bytes: Total system memory in bytes.
        current_cpu_percent: Current system CPU usage.
        current_memory_percent: Current system memory usage.
        cross_project_cpu_percent: CPU used by other SOVA instances.
        safety_margin: Fraction of capacity to reserve (0.0-0.5).
        latest_summary_time: Timestamp of the most recent summary (for staleness).
    """
    if len(summaries) < _MIN_SUMMARIES:
        return CapacityRecommendation(
            recommended_max=current_max,
            current_max=current_max,
            confidence="insufficient",
            headroom_cpu_percent=None,
            headroom_memory_percent=None,
            reason=f"Need at least {_MIN_SUMMARIES} completed runs with resource data; have {len(summaries)}.",
        )

    confidence = _compute_confidence(len(summaries), latest_summary_time)

    # Single-pass accumulation
    avg_cpu_sum = 0.0
    peak_cpu_per_agent = 0.0
    peak_mem_sum = 0
    for s in summaries:
        avg_cpu_sum += s["avg_cpu_percent"]
        peak_cpu_per_agent = max(peak_cpu_per_agent, s["peak_cpu_percent"])
        peak_mem_sum += s["peak_memory_rss_bytes"]
    avg_cpu_per_agent = avg_cpu_sum / len(summaries)
    avg_peak_memory_per_agent = peak_mem_sum / len(summaries)

    # Available capacity after safety margin, cross-project usage, and current load
    total_cpu_capacity = 100.0 * cpu_count
    reserved_cpu = total_cpu_capacity * safety_margin + cross_project_cpu_percent
    # Reduce available capacity by current non-SOVA system load
    current_system_cpu = max(0.0, current_cpu_percent - cross_project_cpu_percent)
    available_cpu = max(0.0, total_cpu_capacity - reserved_cpu - current_system_cpu)

    reserved_memory = total_memory_bytes * safety_margin
    current_memory_used = total_memory_bytes * (current_memory_percent / 100.0)
    available_memory = max(0, int(total_memory_bytes - reserved_memory - current_memory_used))

    # How many agents can fit?
    if avg_cpu_per_agent > 0:
        cpu_based_max = int(available_cpu / avg_cpu_per_agent)
    else:
        cpu_based_max = current_max

    if avg_peak_memory_per_agent > 0:
        memory_based_max = int(available_memory / avg_peak_memory_per_agent)
    else:
        memory_based_max = current_max

    recommended = max(1, min(cpu_based_max, memory_based_max))
    # Cap: never recommend more than 2x current setting in one step
    recommended = min(recommended, current_max * 2)

    # Headroom: how much capacity remains at current_max
    headroom_cpu = available_cpu - (avg_cpu_per_agent * current_max)
    headroom_cpu_pct = round((headroom_cpu / total_cpu_capacity) * 100, 1) if total_cpu_capacity > 0 else 0.0
    headroom_memory = available_memory - (avg_peak_memory_per_agent * current_max)
    headroom_memory_pct = round((headroom_memory / total_memory_bytes) * 100, 1) if total_memory_bytes > 0 else 0.0

    parts = [
        f"Based on {len(summaries)} runs:",
        f"avg CPU/agent={avg_cpu_per_agent:.1f}%, peak={peak_cpu_per_agent:.1f}%,",
        f"avg memory/agent={avg_peak_memory_per_agent / (1024 * 1024):.0f}MB.",
    ]
    if cross_project_cpu_percent > 0:
        parts.append(f"Cross-project CPU: {cross_project_cpu_percent:.1f}%.")
    else:
        parts.append("No cross-project data (single-project mode).")

    return CapacityRecommendation(
        recommended_max=recommended,
        current_max=current_max,
        confidence=confidence,
        headroom_cpu_percent=headroom_cpu_pct,
        headroom_memory_percent=headroom_memory_pct,
        reason=" ".join(parts),
    )


def _compute_confidence(count: int, latest_time: datetime | None) -> ConfidenceLevel:
    """Determine confidence level from sample count and data freshness."""
    if count < _MIN_SUMMARIES:
        return "insufficient"

    base: ConfidenceLevel
    if count >= 10:
        base = "high"
    elif count >= 5:
        base = "medium"
    else:
        base = "low"

    # Stale data reduces confidence by one tier
    if latest_time is not None:
        now = datetime.now(timezone.utc)
        if latest_time.tzinfo is None:
            latest_time = latest_time.replace(tzinfo=timezone.utc)
        age_hours = (now - latest_time).total_seconds() / 3600
        if age_hours > _STALE_HOURS:
            if base == "high":
                return "medium"
            if base == "medium":
                return "low"

    return base
