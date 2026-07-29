"""Task complexity scoring for cost-optimized model routing.

Analyzes task metadata (title, description, labels, file count) and returns
a complexity tier. Pure function, no side effects, no DB access.
"""

from __future__ import annotations

import re
from enum import StrEnum


class ComplexityTier(StrEnum):
    TRIVIAL = "trivial"
    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"
    EPIC = "epic"


# Ordered from lowest to highest for index-based scoring
_TIER_ORDER: list[ComplexityTier] = list(ComplexityTier)

# O(1) tier-to-index lookup (avoids repeated list scans)
_TIER_INDEX: dict[ComplexityTier, int] = {t: i for i, t in enumerate(_TIER_ORDER)}

# ---------------------------------------------------------------------------
# Keyword patterns (case-insensitive)
# ---------------------------------------------------------------------------
# Each entry: (compiled regex, tier)

_KEYWORD_SIGNALS: list[tuple[re.Pattern[str], ComplexityTier]] = [
    # Trivial
    (re.compile(r"\b(?:typo|typos)\b", re.IGNORECASE), ComplexityTier.TRIVIAL),
    (re.compile(r"\brename\b", re.IGNORECASE), ComplexityTier.TRIVIAL),
    (re.compile(r"\bbump\s+version\b", re.IGNORECASE), ComplexityTier.TRIVIAL),
    (re.compile(r"\bupdate\s+(?:readme|docs|changelog)\b", re.IGNORECASE), ComplexityTier.TRIVIAL),
    (re.compile(r"\bconfig\s+change\b", re.IGNORECASE), ComplexityTier.TRIVIAL),
    # Simple
    (re.compile(r"\badd\s+test\b", re.IGNORECASE), ComplexityTier.SIMPLE),
    (re.compile(r"\bsingle[- ]file\b", re.IGNORECASE), ComplexityTier.SIMPLE),
    (re.compile(r"\bsmall\s+fix\b", re.IGNORECASE), ComplexityTier.SIMPLE),
    (re.compile(r"\bminor\s+(?:fix|change|update)\b", re.IGNORECASE), ComplexityTier.SIMPLE),
    # Moderate
    (re.compile(r"\bmulti[- ]file\b", re.IGNORECASE), ComplexityTier.MODERATE),
    (re.compile(r"\bnew\s+(?:function|endpoint|route|command)\b", re.IGNORECASE), ComplexityTier.MODERATE),
    (re.compile(r"\bintegrat(?:e|ion)\b", re.IGNORECASE), ComplexityTier.MODERATE),
    # Complex
    (re.compile(r"\brefactor\b", re.IGNORECASE), ComplexityTier.COMPLEX),
    (re.compile(r"\bmigrat(?:e|ion)\b", re.IGNORECASE), ComplexityTier.COMPLEX),
    (re.compile(r"\bnew\s+module\b", re.IGNORECASE), ComplexityTier.COMPLEX),
    (re.compile(r"\barchitectur(?:e|al)\b", re.IGNORECASE), ComplexityTier.COMPLEX),
    (re.compile(r"\bredesign\b", re.IGNORECASE), ComplexityTier.COMPLEX),
    # Epic
    (re.compile(r"\bcross[- ]cutting\b", re.IGNORECASE), ComplexityTier.EPIC),
    (re.compile(r"\bmulti[- ]system\b", re.IGNORECASE), ComplexityTier.EPIC),
    (re.compile(r"\bbreaking\s+change\b", re.IGNORECASE), ComplexityTier.EPIC),
    (re.compile(r"\bfull\s+rewrite\b", re.IGNORECASE), ComplexityTier.EPIC),
]

# ---------------------------------------------------------------------------
# Label mapping
# ---------------------------------------------------------------------------

_LABEL_TIERS: dict[str, ComplexityTier] = {
    "good first issue": ComplexityTier.TRIVIAL,
    "good-first-issue": ComplexityTier.TRIVIAL,
    "beginner": ComplexityTier.TRIVIAL,
    "trivial": ComplexityTier.TRIVIAL,
    "easy": ComplexityTier.SIMPLE,
    "simple": ComplexityTier.SIMPLE,
    "bug": ComplexityTier.SIMPLE,
    "moderate": ComplexityTier.MODERATE,
    "complex": ComplexityTier.COMPLEX,
    "epic": ComplexityTier.EPIC,
}

# Pre-normalized keys for O(1) lookup without per-call string allocation
_LABEL_NORMALIZED: dict[str, ComplexityTier] = {k.strip().lower().replace(":", ""): v for k, v in _LABEL_TIERS.items()}

# ---------------------------------------------------------------------------
# Description length thresholds (character count)
# ---------------------------------------------------------------------------

_LENGTH_THRESHOLDS: list[tuple[int, ComplexityTier]] = [
    (100, ComplexityTier.TRIVIAL),
    (500, ComplexityTier.SIMPLE),
    (1500, ComplexityTier.MODERATE),
    (4000, ComplexityTier.COMPLEX),
    # >4000 -> EPIC
]

# ---------------------------------------------------------------------------
# File count thresholds
# ---------------------------------------------------------------------------

_FILE_COUNT_THRESHOLDS: list[tuple[int, ComplexityTier]] = [
    (1, ComplexityTier.TRIVIAL),
    (3, ComplexityTier.SIMPLE),
    (7, ComplexityTier.MODERATE),
    (15, ComplexityTier.COMPLEX),
    # >15 -> EPIC
]


def _tier_index(tier: ComplexityTier) -> int:
    return _TIER_INDEX[tier]


def _score_keywords(text: str) -> ComplexityTier | None:
    """Return the highest-complexity keyword match, or None if no match."""
    best: ComplexityTier | None = None
    for pattern, tier in _KEYWORD_SIGNALS:
        if pattern.search(text):
            if best is None or _tier_index(tier) > _tier_index(best):
                best = tier
    return best


def _score_labels(labels: list[str]) -> ComplexityTier | None:
    """Return the highest-complexity label match, or None."""
    best: ComplexityTier | None = None
    for label in labels:
        # Extract value portion of compound labels (e.g. "type:bug" -> "bug")
        # so SOVA's namespace:value taxonomy matches the flat _LABEL_NORMALIZED keys.
        value = label.split(":")[-1] if ":" in label else label
        normalized = value.strip().lower()
        tier = _LABEL_NORMALIZED.get(normalized)
        if tier is not None and (best is None or _tier_index(tier) > _tier_index(best)):
            best = tier
    return best


def _score_length(text: str) -> ComplexityTier:
    """Score based on description length."""
    length = len(text)
    for threshold, tier in _LENGTH_THRESHOLDS:
        if length <= threshold:
            return tier
    return ComplexityTier.EPIC


def _score_file_count(file_count: int) -> ComplexityTier:
    """Score based on estimated file count."""
    for threshold, tier in _FILE_COUNT_THRESHOLDS:
        if file_count <= threshold:
            return tier
    return ComplexityTier.EPIC


def assess_complexity(
    title: str,
    description: str,
    labels: list[str] | None = None,
    file_count_estimate: int | None = None,
) -> ComplexityTier:
    """Score task complexity from metadata.

    Uses a weighted voting system across four signals: keyword matches in the
    combined title+description, label-based hints, description length, and
    estimated file count. Labels carry the highest weight (4.0), followed by
    file count (3.0), keywords (2.0), and description length (1.0). This
    ensures multiple strong signals (labels + file count) can override a
    single misleading keyword.

    Returns ``ComplexityTier.MODERATE`` when no signals are available.
    """
    combined_text = f"{title} {description}".strip()
    if not combined_text:
        return ComplexityTier.MODERATE

    # Collect weighted votes: (tier_index, weight)
    votes: list[tuple[int, float]] = []

    keyword_tier = _score_keywords(combined_text)
    if keyword_tier is not None:
        votes.append((_tier_index(keyword_tier), 2.0))

    if labels:
        label_tier = _score_labels(labels)
        if label_tier is not None:
            votes.append((_tier_index(label_tier), 4.0))

    # Length is a weak signal -- skip when description is empty or when a
    # keyword match already provides a strong directional signal (keywords
    # must dominate length-only conflicts per the scoring contract).
    if description.strip() and keyword_tier is None:
        length_tier = _score_length(description)
        votes.append((_tier_index(length_tier), 1.0))

    if file_count_estimate is not None and file_count_estimate > 0:
        file_tier = _score_file_count(file_count_estimate)
        votes.append((_tier_index(file_tier), 3.0))

    if not votes:
        return ComplexityTier.MODERATE

    # Weighted average, rounded to nearest tier
    total_weight = sum(w for _, w in votes)
    weighted_sum = sum(idx * w for idx, w in votes)
    avg_index = weighted_sum / total_weight
    rounded_index = min(max(round(avg_index), 0), len(_TIER_ORDER) - 1)
    return _TIER_ORDER[rounded_index]
