"""Complexity-to-model routing rules.

Maps ComplexityTier values to model alias strings (haiku/sonnet/opus).
Supports per-tier config overrides via LLMConfig.routing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sova.llm.complexity import ComplexityTier

if TYPE_CHECKING:
    from sova.config.models import LLMConfig

_DEFAULT_ROUTING: dict[ComplexityTier, str] = {
    ComplexityTier.TRIVIAL: "haiku",
    ComplexityTier.SIMPLE: "sonnet",
    ComplexityTier.MODERATE: "sonnet",
    ComplexityTier.COMPLEX: "opus",
    ComplexityTier.EPIC: "opus",
}


def route_model(
    complexity: ComplexityTier,
    *,
    llm_config: LLMConfig | None = None,
) -> str:
    """Select model alias based on task complexity.

    Checks llm_config.routing overrides first, falls back to _DEFAULT_ROUTING.
    Returns a model alias string (e.g., "haiku", "sonnet", "opus").
    """
    if llm_config is not None:
        override = llm_config.routing.get(complexity.value)
        if override is not None:
            return override

    return _DEFAULT_ROUTING.get(complexity, "sonnet")
