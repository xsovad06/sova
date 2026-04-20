"""Review pattern tracking for continuous learning.

Records findings from code reviews and retrieves common patterns
to inject into future review prompts.
"""

from __future__ import annotations

from sova.db.models import Memory
from sova.knowledge.memory import search, store


async def record_review_finding(
    session: object,
    category: str,
    pattern: str,
    source_pr: str = "",
) -> Memory:
    """Record a review finding as a memory entry.

    Thin wrapper around ``store()`` with ``category="review_pattern"``.

    Args:
        session: Database session (reserved for future use).
        category: Sub-category of the finding (e.g., "style", "bug", "perf").
        pattern: Description of the pattern found.
        source_pr: PR reference where the pattern was found (e.g., "#42").

    Returns:
        The created Memory record.
    """
    tags = ["review_pattern", category]
    if source_pr:
        tags.append(source_pr)

    return await store(
        category="review_pattern",
        title=f"Review: {category}",
        content=pattern,
        tags=tags,
        tier="project",
    )


async def get_common_patterns(
    session: object,
    min_count: int = 2,
) -> list[Memory]:
    """Retrieve review pattern memories, sorted by most recent.

    For v1, returns all review_pattern memories ordered by updated_at desc.
    True frequency counting (duplicate detection) is deferred to v2.

    Args:
        session: Database session (reserved for future use).
        min_count: Minimum occurrence count (unused in v1).

    Returns:
        List of review_pattern Memory records.
    """
    return await search(category="review_pattern")
