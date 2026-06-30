"""Memory graph -- edge CRUD, neighbor traversal, auto-linking, and batch discovery."""

from __future__ import annotations

from collections import deque

from sqlalchemy import or_, select

from sova.db.models import Memory, MemoryEdge
from sova.db.session import get_session
from sova.knowledge.embeddings import cosine_similarity, is_available
from sova.utils.logging import get_logger

log = get_logger(component="knowledge.graph")

RELATION_TYPES = frozenset({"relates_to", "refines", "depends_on", "supersedes", "contradicts"})

AUTO_LINK_THRESHOLD = 0.75
AUTO_LINK_MAX_EDGES = 5
_DISCOVER_BATCH_SIZE = 100


async def create_edge(
    source_id: int,
    target_id: int,
    relation: str = "relates_to",
    weight: float = 1.0,
) -> MemoryEdge | None:
    """Create a directed edge between two memories.

    Returns the created edge, or None if a duplicate already exists.
    Raises ValueError for self-edges or invalid relation types.
    """
    if source_id == target_id:
        raise ValueError("Self-edges are not allowed")
    if relation not in RELATION_TYPES:
        raise ValueError(f"Invalid relation '{relation}' (valid: {', '.join(sorted(RELATION_TYPES))})")

    async with await get_session() as session:
        async with session.begin():
            existing = await session.execute(
                select(MemoryEdge).where(
                    MemoryEdge.source_id == source_id,
                    MemoryEdge.target_id == target_id,
                    MemoryEdge.relation == relation,
                )
            )
            if existing.scalar_one_or_none() is not None:
                return None
            edge = MemoryEdge(source_id=source_id, target_id=target_id, relation=relation, weight=weight)
            session.add(edge)
            await session.flush()
            log.info("graph.edge_created", source=source_id, target=target_id, relation=relation)
            return edge


async def delete_edge(edge_id: int) -> bool:
    """Delete an edge by ID. Returns True if deleted, False if not found."""
    async with await get_session() as session:
        async with session.begin():
            result = await session.execute(select(MemoryEdge).where(MemoryEdge.id == edge_id))
            edge = result.scalar_one_or_none()
            if edge is None:
                return False
            await session.delete(edge)
            log.info("graph.edge_deleted", edge_id=edge_id)
            return True


async def get_edges(memory_id: int) -> list[MemoryEdge]:
    """Get all edges where the given memory is source or target."""
    async with await get_session() as session:
        async with session.begin():
            result = await session.execute(
                select(MemoryEdge).where(or_(MemoryEdge.source_id == memory_id, MemoryEdge.target_id == memory_id))
            )
            return list(result.scalars().all())


async def get_neighbors(
    memory_id: int,
    *,
    depth: int = 1,
    relation: str | None = None,
) -> list[Memory]:
    """Get neighbor memories via BFS traversal up to the given depth.

    Filters out superseded memories. Treats all edges as bidirectional for traversal
    (even directional ones like refines/depends_on), since the goal is context discovery.
    """
    if depth < 1 or depth > 2:
        raise ValueError("depth must be 1 or 2")

    visited: set[int] = {memory_id}
    queue: deque[tuple[int, int]] = deque([(memory_id, 0)])
    neighbor_ids: list[int] = []

    async with await get_session() as session:
        async with session.begin():
            while queue:
                current_id, current_depth = queue.popleft()
                if current_depth >= depth:
                    continue

                stmt = select(MemoryEdge).where(
                    or_(MemoryEdge.source_id == current_id, MemoryEdge.target_id == current_id)
                )
                if relation is not None:
                    stmt = stmt.where(MemoryEdge.relation == relation)

                result = await session.execute(stmt)
                edges = list(result.scalars().all())

                for edge in edges:
                    other_id = edge.target_id if edge.source_id == current_id else edge.source_id
                    if other_id not in visited:
                        visited.add(other_id)
                        neighbor_ids.append(other_id)
                        queue.append((other_id, current_depth + 1))

            if not neighbor_ids:
                return []

            mem_result = await session.execute(
                select(Memory).where(
                    Memory.id.in_(neighbor_ids),
                    Memory.superseded_by.is_(None),
                )
            )
            return list(mem_result.scalars().all())


async def auto_link(memory_id: int) -> list[MemoryEdge]:
    """Auto-discover and create edges for a memory based on embedding similarity.

    Returns empty list if embeddings are unavailable or the memory has no embedding.
    """
    if not is_available():
        return []

    async with await get_session() as session:
        async with session.begin():
            result = await session.execute(select(Memory).where(Memory.id == memory_id))
            source = result.scalar_one_or_none()
            if source is None or source.embedding is None:
                return []

            candidates = await session.execute(
                select(Memory).where(
                    Memory.id != memory_id,
                    Memory.category == source.category,
                    Memory.superseded_by.is_(None),
                    Memory.embedding.isnot(None),
                )
            )
            all_candidates = list(candidates.scalars().all())

    scored: list[tuple[int, float]] = []
    for cand in all_candidates:
        score = cosine_similarity(source.embedding, cand.embedding)
        if score >= AUTO_LINK_THRESHOLD:
            scored.append((cand.id, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    scored = scored[:AUTO_LINK_MAX_EDGES]

    created: list[MemoryEdge] = []
    for target_id, score in scored:
        edge = await create_edge(memory_id, target_id, relation="relates_to", weight=round(score, 4))
        if edge is not None:
            created.append(edge)

    log.info("graph.auto_linked", memory_id=memory_id, edges_created=len(created))
    return created


async def discover_edges(category: str | None = None) -> int:
    """Batch-discover edges across all memories in a category.

    Compares within same category only. Processes in batches of 100 for large corpora.
    Returns the total number of new edges created.
    """
    if not is_available():
        log.info("graph.discover_skipped", reason="embeddings unavailable")
        return 0

    async with await get_session() as session:
        async with session.begin():
            stmt = select(Memory).where(
                Memory.superseded_by.is_(None),
                Memory.embedding.isnot(None),
            )
            if category is not None:
                stmt = stmt.where(Memory.category == category)
            result = await session.execute(stmt)
            all_memories = list(result.scalars().all())

    if not all_memories:
        return 0

    # Group by category for within-category comparison
    by_category: dict[str, list[Memory]] = {}
    for mem in all_memories:
        by_category.setdefault(mem.category, []).append(mem)

    total_created = 0
    for cat_memories in by_category.values():
        for batch_start in range(0, len(cat_memories), _DISCOVER_BATCH_SIZE):
            batch_end = min(batch_start + _DISCOVER_BATCH_SIZE, len(cat_memories))
            for i in range(batch_start, batch_end):
                mem_a = cat_memories[i]
                for mem_b in cat_memories[i + 1 :]:
                    score = cosine_similarity(mem_a.embedding, mem_b.embedding)
                    if score >= AUTO_LINK_THRESHOLD:
                        edge = await create_edge(mem_a.id, mem_b.id, relation="relates_to", weight=round(score, 4))
                        if edge is not None:
                            total_created += 1

    log.info("graph.discover_complete", total_edges=total_created, memories_scanned=len(all_memories))
    return total_created
