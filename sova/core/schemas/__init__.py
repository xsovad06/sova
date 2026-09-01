"""JSON Schema definitions for structured LLM outputs."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

SCHEMAS_DIR = Path(__file__).parent


def load_schema(filename: str) -> dict[str, Any]:
    """Load a JSON Schema from the schemas directory."""
    path = SCHEMAS_DIR / filename
    with path.open() as f:
        return json.load(f)


@lru_cache(maxsize=1)
def get_review_schema() -> dict[str, Any]:
    """Get the review result schema (cached)."""
    return load_schema("review_result.json")


@lru_cache(maxsize=1)
def get_triage_schema() -> dict[str, Any]:
    """Get the triage assessment schema (cached)."""
    return load_schema("triage_assessment.json")


__all__ = [
    "get_review_schema",
    "get_triage_schema",
    "load_schema",
]
