"""Data models for the LLM interaction layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


class BatchTimeoutError(Exception):
    """Raised when a batch does not complete within the timeout."""


@dataclass
class LLMResult:
    """Result from a Claude Code CLI invocation."""

    text: str
    model: str
    cost_usd: Decimal = Decimal("0")
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    duration_ms: int = 0
    session_id: str = ""
    stop_reason: str = ""
    # Compression accounting: None when compression did not run (disabled, below
    # min_chars, package missing, or error); an int (>= 0) when it did.
    pre_compression_input_tokens: int | None = None
    tokens_saved: int | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def is_error(self) -> bool:
        return self.stop_reason == "error"


@dataclass
class StreamEvent:
    """A streaming event from Claude Code CLI.

    Types:
    - "content": partial text output (text field populated)
    - "result": final result with costs (result field populated)
    """

    type: str
    text: str = ""
    result: LLMResult | None = field(default=None)


@dataclass
class BatchRequest:
    """A single request in a batch submission."""

    custom_id: str
    prompt: str
    model: str = ""
    max_tokens: int = 4096
    system: str = ""


@dataclass
class BatchResult:
    """Result for a single request in a batch submission."""

    request: BatchRequest
    result: LLMResult | None = None
    error: str = ""

    @property
    def succeeded(self) -> bool:
        return self.result is not None and not self.error


# Per-million-token pricing for Anthropic Messages API.
# Keys are model ID prefixes matched left-to-right; the first match wins.
# Values: (input_cost_per_mtok, output_cost_per_mtok).
_ANTHROPIC_RATE_CARD: dict[str, tuple[Decimal, Decimal]] = {
    "claude-fable-5": (Decimal("10"), Decimal("50")),
    "claude-opus-5": (Decimal("5"), Decimal("25")),
    "claude-sonnet-5": (Decimal("2"), Decimal("10")),
    "claude-haiku-4-5": (Decimal("1"), Decimal("5")),
    "claude-opus-4": (Decimal("15"), Decimal("75")),
    "claude-sonnet-4": (Decimal("3"), Decimal("15")),
}

_MTOK = Decimal("1_000_000")

# Bare family aliases carry no version, but the rate card is keyed by full model
# IDs. Map each alias to the current release in its family so rate lookups (used
# for cost/savings estimates) resolve instead of falling back to 0. Keep in sync
# with _ANTHROPIC_RATE_CARD as new releases ship.
_ALIAS_TO_CURRENT_MODEL: dict[str, str] = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
    "smart": "claude-opus-5",
    "fast": "claude-sonnet-5",
    "cheap": "claude-haiku-4-5",
}


def compute_anthropic_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> Decimal:
    """Compute USD cost from token counts using the Anthropic rate card.

    Returns ``Decimal("0")`` for unknown models rather than raising.
    """
    rates = _lookup_rates(model)
    if rates is None:
        return Decimal("0")
    input_rate, output_rate = rates
    base_input = max(0, input_tokens - cache_read_tokens - cache_creation_tokens)
    cost = (
        input_rate * base_input / _MTOK
        + output_rate * output_tokens / _MTOK
        + input_rate * Decimal("0.1") * cache_read_tokens / _MTOK
        + input_rate * Decimal("1.25") * cache_creation_tokens / _MTOK
    )
    return cost.quantize(Decimal("0.000001"))


def input_rate_per_mtok(model: str) -> Decimal:
    """Return the per-million-token input rate for a model, or 0 if unknown.

    Bare family aliases ("opus", "sonnet", "haiku") are resolved to the current
    release in their family before lookup, since the rate card is keyed by full
    model IDs and config commonly stores bare aliases.
    """
    resolved = _ALIAS_TO_CURRENT_MODEL.get(model, model)
    rates = _lookup_rates(resolved)
    return rates[0] if rates else Decimal("0")


def _lookup_rates(model: str) -> tuple[Decimal, Decimal] | None:
    for prefix, rates in _ANTHROPIC_RATE_CARD.items():
        if model.startswith(prefix):
            return rates
    return None
