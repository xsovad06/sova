"""Embedding utilities for semantic memory search.

Provides text embedding via sentence-transformers (optional dependency),
cosine similarity computation, and semantic search over Memory records.
"""

from __future__ import annotations

import math

from sova.utils.logging import get_logger

log = get_logger(component="knowledge.embeddings")

_MODEL_NAME = "all-MiniLM-L6-v2"
_model_cache: object | None = None

SIMILARITY_THRESHOLD = 0.85


def _load_model() -> object | None:
    """Lazy-load the sentence-transformers model. Returns None if unavailable."""
    global _model_cache  # noqa: PLW0603
    if _model_cache is not None:
        return _model_cache

    try:
        from sentence_transformers import SentenceTransformer

        _model_cache = SentenceTransformer(_MODEL_NAME)
        log.info("embeddings.model_loaded", model=_MODEL_NAME)
        return _model_cache
    except ImportError:
        log.debug("embeddings.unavailable", reason="sentence-transformers not installed")
        return None
    except Exception:
        log.warning("embeddings.load_failed", model=_MODEL_NAME, exc_info=True)
        return None


def embed_text(text: str) -> list[float] | None:
    """Compute an embedding vector for the given text.

    Returns None if sentence-transformers is not installed or embedding fails.
    """
    model = _load_model()
    if model is None:
        return None

    try:
        vector = model.encode(text, convert_to_numpy=True)
        return vector.tolist()
    except Exception:
        log.warning("embeddings.encode_failed", text_preview=text[:80], exc_info=True)
        return None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors. Pure Python, no deps."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def is_available() -> bool:
    """Check if the embedding model can be loaded."""
    return _load_model() is not None
