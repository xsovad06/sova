"""Query-aware, token-budgeted retrieval orchestration layer.

Composes existing semantic_search(), graph expansion, and embedding primitives
into a retrieval pipeline that selects relevant memories based on task context
and respects a token budget for prompt injection.
"""

from __future__ import annotations

import asyncio

from sova.db.models import Memory
from sova.knowledge.embeddings import is_available
from sova.knowledge.memory import increment_retrieval, search, semantic_search
from sova.utils.logging import get_logger

log = get_logger(component="knowledge.retrieval")

DEFAULT_MAX_CONTEXT_TOKENS = 2000


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
    # Steps 1+3: fetch shared and project tiers concurrently
    shared_memories, project_result = await asyncio.gather(
        search(tier="shared", category=category, limit=20),
        _search_project_tier(query=query, category=category),
    )
    project_results, used_semantic = project_result

    results, shared_tokens = _collect_shared(shared_memories, max_context_tokens)

    if project_results:
        shared_ids = {m.id for m, _ in results}
        _append_project_results(
            results,
            project_results,
            shared_ids,
            used_semantic,
            max_context_tokens - shared_tokens,
        )

    await _track_retrieval(results)
    return results


def _collect_shared(
    shared_memories: list[Memory],
    max_context_tokens: int,
) -> tuple[list[tuple[Memory, float]], int]:
    """Collect shared-tier memories and compute their token usage."""
    results: list[tuple[Memory, float]] = []
    shared_tokens = 0
    for mem in shared_memories:
        tokens = estimate_tokens(mem.content) + estimate_tokens(mem.title)
        shared_tokens += tokens
        results.append((mem, 1.0))

    if shared_tokens > max_context_tokens:
        log.warning(
            "retrieval.shared_exceeds_budget",
            shared_tokens=shared_tokens,
            budget=max_context_tokens,
        )
    return results, shared_tokens


def _append_project_results(
    results: list[tuple[Memory, float]],
    project_results: list[tuple[Memory, float]],
    shared_ids: set[int],
    used_semantic: bool,
    remaining_budget: int,
) -> None:
    """Append project-tier results to the results list, respecting budget when semantic."""
    remaining_budget = max(0, remaining_budget)
    used_tokens = 0
    for mem, score in project_results:
        if mem.id in shared_ids:
            continue
        if used_semantic:
            mem_tokens = estimate_tokens(mem.content) + estimate_tokens(mem.title)
            if used_tokens + mem_tokens > remaining_budget and used_tokens > 0:
                break
            used_tokens += mem_tokens
        results.append((mem, score))


async def _track_retrieval(results: list[tuple[Memory, float]]) -> None:
    """Track retrieval counts for lifecycle scoring (non-fatal)."""
    retrieved_ids = [m.id for m, _ in results if m.id is not None]
    if not retrieved_ids:
        return
    try:
        await increment_retrieval(retrieved_ids)
    except Exception:
        log.warning("retrieval.increment_failed", exc_info=True)


async def _search_project_tier(
    *,
    query: str,
    category: str | None,
) -> tuple[list[tuple[Memory, float]], bool]:
    """Search project-tier memories, with semantic-to-lexical fallback.

    Returns:
        Tuple of (results, used_semantic). When used_semantic is False,
        callers should bypass the token budget (exhaustive fallback).
    """
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
            return semantic_results, True
        # Semantic returned empty (no embeddings on stored memories) -- fall through

    # Lexical fallback -- no limit so callers get truly exhaustive results
    lexical = await search(query=query if stripped else None, tier="project", category=category)
    return [(m, 0.0) for m in lexical], False


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
