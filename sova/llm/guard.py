"""Mid-pipeline prompt injection guard for LLM calls.

Scans assembled prompts for injection patterns before sending them to the
LLM provider. Defense-in-depth -- not a guarantee. Residual risks include
novel attack patterns, heavily obfuscated payloads that bypass normalization,
and semantic-level injections that require LLM-level understanding.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field

from sova.utils.logging import get_logger

log = get_logger(component="llm.guard")


@dataclass(frozen=True)
class ScanResult:
    """Result of scanning a prompt for injection patterns."""

    safe: bool
    risk_score: float
    flags: list[str] = field(default_factory=list)
    details: dict[str, float] = field(default_factory=dict)


class PromptInjectionError(Exception):
    """Raised when a prompt exceeds the injection risk threshold."""

    def __init__(self, scan_result: ScanResult) -> None:
        self.scan_result = scan_result
        super().__init__(
            f"Prompt injection detected (risk_score={scan_result.risk_score:.2f}, flags={scan_result.flags})"
        )


# ---------------------------------------------------------------------------
# Zero-width and homoglyph normalization
# ---------------------------------------------------------------------------

_ZERO_WIDTH_CHARS = re.compile("[\u2060\ufeff\u00ad\u034f\u180e\u2000-\u200f\u202a-\u202f\u2066-\u2069]")


def _normalize_text(text: str) -> str:
    """Normalize Unicode and strip zero-width characters for pattern matching."""
    text = _ZERO_WIDTH_CHARS.sub("", text)
    text = unicodedata.normalize("NFKD", text)
    return text


# ---------------------------------------------------------------------------
# Pattern categories
# ---------------------------------------------------------------------------

# Each tuple: (compiled regex, category name, weight)
_PatternEntry = tuple[re.Pattern[str], str, float]

_DIRECT_INJECTION_PATTERNS: list[_PatternEntry] = [
    (
        re.compile(
            r"(?i)ignore\s+(?:all\s+)?(?:previous|prior|above|earlier|preceding)\s+"
            r"(?:instructions?|prompts?|directives?|context|rules?|guidelines?)"
        ),
        "direct_injection",
        0.9,
    ),
    (
        re.compile(r"(?i)disregard\s+(?:all\s+)?(?:previous|prior|above|earlier)\s+(?:instructions?|context)"),
        "direct_injection",
        0.9,
    ),
    (
        re.compile(r"(?i)forget\s+(?:everything|all)\s+(?:above|before|previously)"),
        "direct_injection",
        0.85,
    ),
    (
        re.compile(r"(?i)override\s+(?:your|the|all)?\s*(?:system|previous|prior)\s+(?:prompt|instructions?)"),
        "direct_injection",
        0.9,
    ),
    (
        re.compile(r"(?i)do\s+not\s+follow\s+(?:any\s+)?(?:previous|prior|above)\s+(?:instructions?|rules?)"),
        "direct_injection",
        0.9,
    ),
]

_ROLE_SWITCHING_PATTERNS: list[_PatternEntry] = [
    (
        re.compile(r"(?i)you\s+are\s+now\s+(?:a|an|the)\s+\w+"),
        "role_switch",
        0.65,
    ),
    (
        re.compile(r"(?i)act\s+as\s+(?:a|an|the)\s+(?:different|new)\s+\w+"),
        "role_switch",
        0.65,
    ),
    (
        re.compile(r"(?i)switch\s+(?:to|into)\s+(?:a\s+)?(?:new\s+)?(?:role|mode|persona|character)"),
        "role_switch",
        0.75,
    ),
    (
        re.compile(r"(?i)from\s+now\s+on\s+you\s+(?:are|will\s+be|must\s+act\s+as)"),
        "role_switch",
        0.8,
    ),
]

_SYSTEM_PROMPT_EXTRACTION: list[_PatternEntry] = [
    (
        re.compile(r"(?i)(?:print|show|reveal|display|output|repeat|echo)\s+(?:your|the)\s+system\s+prompt"),
        "system_extraction",
        0.85,
    ),
    (
        re.compile(r"(?i)what\s+(?:is|are)\s+your\s+(?:system\s+)?(?:instructions?|rules?|prompt)"),
        "system_extraction",
        0.6,
    ),
    (
        re.compile(
            r"(?i)(?:dump|leak|expose|extract)\s+(?:your|the)\s+(?:system|initial|original)\s+(?:prompt|context)"
        ),
        "system_extraction",
        0.9,
    ),
]

_BOUNDARY_MANIPULATION: list[_PatternEntry] = [
    (
        re.compile(r"<\|(?:system|im_start|im_end|endoftext)\|>"),
        "boundary_manipulation",
        0.95,
    ),
    (
        re.compile(r"(?i)\[INST\]|\[/INST\]|<<SYS>>|<</SYS>>"),
        "boundary_manipulation",
        0.9,
    ),
    (
        re.compile(r"```system\b"),
        "boundary_manipulation",
        0.8,
    ),
    (
        re.compile(r"<system>.*?</system>", re.DOTALL),
        "boundary_manipulation",
        0.7,
    ),
]

_OBFUSCATION_PATTERNS: list[_PatternEntry] = [
    (
        re.compile(r"(?i)base64[:\s]+[a-z0-9+/]{40,}={0,2}"),
        "obfuscation",
        0.6,
    ),
    (
        re.compile(r"(?i)decode\s+(?:the\s+following|this)\s*:\s*[a-z0-9+/]{20,}"),
        "obfuscation",
        0.7,
    ),
    (
        re.compile(r"(?i)\\x[0-9a-f]{2}(?:\\x[0-9a-f]{2}){5,}"),
        "obfuscation",
        0.65,
    ),
    (
        re.compile(r"(?i)&#x?[0-9a-f]+;(?:&#x?[0-9a-f]+;){5,}"),
        "obfuscation",
        0.65,
    ),
]

_ALL_PATTERNS: list[_PatternEntry] = (
    _DIRECT_INJECTION_PATTERNS
    + _ROLE_SWITCHING_PATTERNS
    + _SYSTEM_PROMPT_EXTRACTION
    + _BOUNDARY_MANIPULATION
    + _OBFUSCATION_PATTERNS
)


# ---------------------------------------------------------------------------
# Allowlist (SHA-256 hashes of known-safe prompt segments)
# ---------------------------------------------------------------------------


def _compute_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _build_allowlist() -> frozenset[str]:
    """Build the allowlist from known-safe prompt segments.

    Computes hashes at import time from the actual constants to avoid
    hash instability when the constants change.
    """
    hashes: list[str] = []
    try:
        from sova.ipc.runtime import _HEADLESS_PREAMBLE

        hashes.append(_compute_hash(_HEADLESS_PREAMBLE))
    except ImportError:
        pass
    return frozenset(hashes)


_ALLOWLIST: frozenset[str] = _build_allowlist()


# ---------------------------------------------------------------------------
# Custom deny pattern cache
# ---------------------------------------------------------------------------

_compiled_custom_cache: dict[str, re.Pattern[str] | None] = {}


def _get_compiled_custom(raw: str) -> re.Pattern[str] | None:
    """Return a compiled regex for a custom deny pattern, caching the result."""
    if raw not in _compiled_custom_cache:
        try:
            _compiled_custom_cache[raw] = re.compile(raw, re.IGNORECASE)
        except re.error:
            log.warning("Invalid custom deny pattern: %s", raw)
            _compiled_custom_cache[raw] = None
    return _compiled_custom_cache[raw]


# ---------------------------------------------------------------------------
# Core scanner
# ---------------------------------------------------------------------------


def _check_builtin_patterns(
    normalized: str,
    flags: list[str],
    details: dict[str, float],
) -> float:
    """Check built-in patterns and return the max score."""
    max_score = 0.0
    for pattern, category, weight in _ALL_PATTERNS:
        if pattern.search(normalized):
            flag = f"{category}:{pattern.pattern[:60]}"
            if flag not in flags:
                flags.append(flag)
            details[category] = max(details.get(category, 0.0), weight)
            max_score = max(max_score, weight)
    return max_score


def _check_custom_patterns(
    normalized: str,
    custom_deny_patterns: list[str],
    flags: list[str],
    details: dict[str, float],
) -> float:
    """Check custom deny patterns and return the max score."""
    max_score = 0.0
    for raw_pattern in custom_deny_patterns:
        compiled = _get_compiled_custom(raw_pattern)
        if compiled is not None and compiled.search(normalized):
            flag = f"custom:{raw_pattern[:60]}"
            if flag not in flags:
                flags.append(flag)
            details["custom"] = max(details.get("custom", 0.0), 0.8)
            max_score = max(max_score, 0.8)
    return max_score


def scan_prompt(text: str, *, custom_deny_patterns: list[str] | None = None) -> ScanResult:
    """Scan a prompt for injection patterns.

    Normalizes Unicode, strips zero-width characters, then checks against
    all pattern categories. Returns a ScanResult with aggregated risk score.
    """
    if _compute_hash(text) in _ALLOWLIST:
        return ScanResult(safe=True, risk_score=0.0)

    normalized = _normalize_text(text)
    flags: list[str] = []
    details: dict[str, float] = {}

    max_score = _check_builtin_patterns(normalized, flags, details)
    if custom_deny_patterns:
        max_score = max(max_score, _check_custom_patterns(normalized, custom_deny_patterns, flags, details))

    return ScanResult(
        safe=max_score < 0.5,  # Advisory: guard_prompt uses the configurable threshold
        risk_score=round(max_score, 2),
        flags=flags,
        details=details,
    )


def guard_prompt(prompt: str) -> None:
    """Check a prompt and raise PromptInjectionError if it exceeds the threshold.

    Loads config lazily to avoid circular imports. When the guard is disabled
    via config, this is a no-op.
    """
    from sova.config.loader import load_config

    try:
        config = load_config()
    except Exception:
        log.debug("guard_prompt: config load failed, guard bypassed", exc_info=True)
        return

    security = config.security
    if not security.prompt_guard:
        return

    result = scan_prompt(
        prompt,
        custom_deny_patterns=security.custom_deny_patterns or None,
    )

    if result.risk_score >= security.prompt_guard_threshold:
        prompt_hash = _compute_hash(prompt)
        log.warning(
            "Prompt injection guard triggered: hash=%s risk_score=%.2f flags=%s",
            prompt_hash,
            result.risk_score,
            result.flags,
        )
        raise PromptInjectionError(result)

    if result.flags:
        log.info(
            "Prompt guard: low-risk flags detected (score=%.2f, flags=%s)",
            result.risk_score,
            result.flags,
        )
