"""Tier management for the 4-tier knowledge system.

Tiers:
- Tier 0 (shared): Cross-project knowledge from shared_knowledge_path (file-based)
- Tier 1 (rules): Project rules in .claude/rules/*.md (file-based)
- Tier 2 (project): Project-specific learned patterns (DB-backed)
- Tier 3 (session): Session memory (managed outside this module)
"""

from __future__ import annotations

from pathlib import Path

from sova.db.models import Memory
from sova.knowledge.memory import search
from sova.utils.logging import get_logger

log = get_logger(component="knowledge.tiers")


async def load_tier(tier: str) -> list[Memory]:
    """Load all non-superseded memories for a given tier.

    Args:
        tier: The tier to load (e.g., "project", "shared").

    Returns:
        List of Memory records in that tier.
    """
    return await search(tier=tier)


def _load_md_files(directory: Path) -> str:
    """Read all .md files from a directory and concatenate contents."""
    if not directory.is_dir():
        return ""

    parts = []
    for md_file in sorted(directory.glob("*.md")):
        try:
            content = md_file.read_text(encoding="utf-8")
            parts.append(f"## {md_file.stem}\n\n{content}")
        except OSError:
            log.warning("tiers.read_error", path=str(md_file))
    return "\n\n".join(parts)


async def load_context(
    session: object,
    project_dir: Path,
    config: object,
    tier: str | None = None,
    category: str | None = None,
    ctx: object | None = None,
) -> str:
    """Load knowledge from file-based and DB tiers, formatted for prompts.

    Combines:
    - Tier 0: shared knowledge from config.shared_knowledge_path (if exists)
    - Tier 1: project rules from project_dir/.claude/rules/*.md
    - Tier 2: DB-backed memories (filtered by relevance when ctx is provided,
      or exhaustive when ctx is None)

    When ``ctx`` is provided (an object with ``role``, ``task``, ``files_changed``),
    uses relevance filtering via ``retrieve_relevant()`` to select the most
    relevant memories within a token budget. Falls back to exhaustive injection
    when ctx is None or embeddings are unavailable.

    Args:
        session: Database session (reserved for future use).
        project_dir: Root directory of the target project.
        config: ProjectConfig instance (needs shared_knowledge_path).
        tier: Optional DB tier filter (e.g., "project", "shared"). Ignored when
            ctx is provided (retrieve_relevant handles tier selection internally).
        category: Optional category filter for DB memories.
        ctx: Optional execution context for relevance-based retrieval.

    Returns:
        Formatted string combining all tiers, suitable for prompt injection.
    """
    sections: list[str] = []

    # Tier 0: shared knowledge
    raw_shared = getattr(config, "shared_knowledge_path", None)
    shared_path = Path(raw_shared) if raw_shared is not None else None
    if shared_path is not None and shared_path.is_dir():
        shared_content = _load_md_files(shared_path)
        if shared_content:
            sections.append(f"# Shared Knowledge (Tier 0)\n\n{shared_content}")

    # Tier 1: project rules
    rules_dir = project_dir / ".claude" / "rules"
    rules_content = _load_md_files(rules_dir)
    if rules_content:
        sections.append(f"# Project Rules (Tier 1)\n\n{rules_content}")

    # Tier 2: DB-backed memories
    if ctx is not None:
        formatted_db = await _load_relevant_memories(ctx, category=category, config=config)
    else:
        db_memories = await search(tier=tier or "project", category=category)
        formatted_db = format_for_prompt(db_memories)

    if formatted_db:
        sections.append(f"# Agent Memory (Tier 2)\n\n{formatted_db}")

    return "\n\n---\n\n".join(sections)


async def _load_relevant_memories(ctx: object, category: str | None = None, config: object | None = None) -> str:
    """Load memories using relevance filtering from an execution context."""
    from sova.knowledge.retrieval import (
        DEFAULT_MAX_CONTEXT_TOKENS,
        build_context_query,
        format_relevant_context,
        retrieve_relevant,
    )

    role = getattr(ctx, "role", "")
    task = getattr(ctx, "task", None)
    files_changed = getattr(ctx, "files_changed", [])

    # Read max_context_tokens from config.knowledge if available
    knowledge_cfg = getattr(config, "knowledge", None)
    max_tokens = getattr(knowledge_cfg, "max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS)

    query = build_context_query(role=role, task=task, files_changed=files_changed)
    results = await retrieve_relevant(query=query, max_context_tokens=max_tokens, category=category)
    return format_relevant_context(results)


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
