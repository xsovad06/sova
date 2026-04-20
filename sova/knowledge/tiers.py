"""Tier management for the 4-tier knowledge system.

Tiers:
- project: Project-specific learned patterns (DB-backed, Tier 2)
- shared: Cross-project generalizable knowledge (DB-backed, Tier 0)

Tier 1 (project rules in .claude/rules/) and Tier 3 (session memory) are
file-based and managed outside this module.
"""

from __future__ import annotations

from sova.db.models import Memory
from sova.knowledge.memory import search


async def load_tier(tier: str) -> list[Memory]:
    """Load all non-superseded memories for a given tier.

    Args:
        tier: The tier to load (e.g., "project", "shared").

    Returns:
        List of Memory records in that tier.
    """
    return await search(tier=tier)


async def load_context(
    *,
    tier: str,
    category: str | None = None,
    tags: list[str] | None = None,
) -> list[Memory]:
    """Load knowledge relevant to a specific context.

    Combines tier filtering with optional category and tag filters.
    Useful for loading targeted knowledge before a workflow step.

    Args:
        tier: Knowledge tier to load from.
        category: Optional category filter.
        tags: Optional tag filter (any match).

    Returns:
        List of matching Memory records.
    """
    return await search(tier=tier, category=category, tags=tags)


def format_for_prompt(memories: list[Memory]) -> str:
    """Format a list of memories into a prompt-friendly string.

    Each memory is rendered as a titled section with its content,
    suitable for injection into an LLM prompt.

    Args:
        memories: List of Memory records to format.

    Returns:
        Formatted string for prompt injection.
    """
    if not memories:
        return ""

    sections = []
    for mem in memories:
        tag_str = f" [{mem.tags}]" if mem.tags else ""
        sections.append(f"### {mem.title}{tag_str}\n{mem.content}")

    return "\n\n".join(sections)
