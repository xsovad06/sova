"""Knowledge management for SOVA agents.

Provides CRUD operations on the Memory model, search with filtering,
tier promotion, supersession tracking, persona detection, and review patterns.
"""

from sova.knowledge.extraction import extract_memories
from sova.knowledge.graph import auto_link, create_edge, delete_edge, discover_edges, get_edges, get_neighbors
from sova.knowledge.memory import delete, find_similar, get, promote, search, semantic_search, store, supersede, update
from sova.knowledge.personas import detect_persona, load_persona
from sova.knowledge.review_patterns import get_common_patterns, record_review_finding
from sova.knowledge.sharing import export_memories, import_memories, parse_shared_file, render_shared_file
from sova.knowledge.tiers import format_for_prompt, load_context, load_tier

__all__ = [
    "auto_link",
    "create_edge",
    "delete",
    "delete_edge",
    "detect_persona",
    "discover_edges",
    "export_memories",
    "extract_memories",
    "find_similar",
    "format_for_prompt",
    "get",
    "get_common_patterns",
    "get_edges",
    "get_neighbors",
    "import_memories",
    "load_context",
    "load_persona",
    "load_tier",
    "parse_shared_file",
    "promote",
    "record_review_finding",
    "render_shared_file",
    "search",
    "semantic_search",
    "store",
    "supersede",
    "update",
]
