"""Query-aware, token-budgeted retrieval orchestration layer.

Composes existing semantic_search(), graph expansion, and embedding primitives
into a retrieval pipeline that selects relevant memories based on task context
and respects a token budget for prompt injection.
"""

from __future__ import annotations

import asyncio

from sova.db.models import Memory
from sova.knowledge.embeddings import is_available
from sova.knowledge.memory import search, semantic_search
from sova.utils.logging import get_logger

log = get_logger(component="knowledge.retrieval")

DEFAULT_MAX_CONTEXT_TOKENS = 4000


def estimate_tokens(text: str) -> int:
    """Estimate token count using word-count proxy (tokens ~= 1.3 * words)."""
    if not text:
        return 0
    return int(len(text.split()) * 1.3)


def build_context_query(
    *,
    role: str,
    task: object | None,
    files_changed: list[str],
) -> str:
    """Build a search query from execution context.

    Combines task title/body, role, and changed files into a single query string
    suitable for semantic_search(). Never returns an empty string.

    Args:
        role: Agent role (developer, reviewer, etc.).
        task: Task object with title and body attributes, or None.
        files_changed: List of file paths changed in this run.

    Returns:
        A non-empty query string.
    """
    parts: list[str] = []

    if task is not None:
        title = getattr(task, "title", "")
        body = getattr(task, "body", "")
        if title:
            parts.append(title)
        if body:
            # Truncate body to first 200 chars to keep query focused
            parts.append(body[:200])

    if files_changed:
        parts.append(" ".join(files_changed[:10]))

    if role:
        parts.append(role)

    query = " ".join(parts).strip()
    if not query:
        return "general"
    return query


async def retrieve_relevant(
    *,
    query: str,
    max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
    category: str | None = None,
) -> list[tuple[Memory, float]]:
    """Retrieve relevance-ranked memories within a token budget.

    1. Fetch all shared-tier memories (always included).
    2. Subtract shared token usage from budget.
    3. Fill remaining budget with relevance-ranked project-tier memories.
    4. Falls back to lexical search when embeddings are unavailable.

    Args:
        query: Search query (from build_context_query).
        max_context_tokens: Advisory token budget for total memory content.
        category: Optional category filter.

    Returns:
        List of (Memory, score) tuples, shared first then project by relevance.
    """
    results: list[tuple[Memory, float]] = []

    # Steps 1+3: fetch shared and project tiers concurrently
    shared_memories, project_results = await asyncio.gather(
        search(tier="shared", category=category),
        _search_project_tier(query=query, category=category),
    )

    # Process shared-tier memories (always included)
    shared_tokens = 0
    for mem in shared_memories:
        tokens = estimate_tokens(mem.content) + estimate_tokens(mem.title)
        shared_tokens += tokens
        results.append((mem, 1.0))  # Score 1.0 = always relevant

    if shared_tokens > max_context_tokens:
        log.warning(
            "retrieval.shared_exceeds_budget",
            shared_tokens=shared_tokens,
            budget=max_context_tokens,
        )

    # Calculate remaining budget for project-tier memories
    remaining_budget = max(0, max_context_tokens - shared_tokens)

    if not project_results:
        return results

    # Step 4: fill remaining budget with highest-scored project memories
    shared_ids = {m.id for m, _ in results}
    used_tokens = 0
    for mem, score in project_results:
        if mem.id in shared_ids:
            continue
        mem_tokens = estimate_tokens(mem.content) + estimate_tokens(mem.title)
        if used_tokens + mem_tokens > remaining_budget and used_tokens > 0:
            break
        used_tokens += mem_tokens
        results.append((mem, score))

    return results


async def _search_project_tier(
    *,
    query: str,
    category: str | None,
) -> list[tuple[Memory, float]]:
    """Search project-tier memories, with semantic-to-lexical fallback."""
    stripped = query.strip()
    if is_available() and stripped:
        semantic_results = await semantic_search(
            query=query,
            tier="project",
            category=category,
            limit=20,
            expand=True,
        )
        if semantic_results:
            return semantic_results
        # Semantic returned empty (no embeddings on stored memories) -- fall through

    # Lexical fallback
    lexical = await search(query=query if stripped else None, tier="project", category=category)
    return [(m, 0.0) for m in lexical]


def format_relevant_context(results: list[tuple[Memory, float]]) -> str:
    """Format retrieval results into a prompt-friendly string.

    Args:
        results: List of (Memory, score) tuples from retrieve_relevant().

    Returns:
        Formatted string for prompt injection, or empty string if no results.
    """
    from sova.knowledge.tiers import format_for_prompt

    memories = [mem for mem, _score in results]
    return format_for_prompt(memories)
