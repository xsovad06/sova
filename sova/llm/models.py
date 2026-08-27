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


def _lookup_rates(model: str) -> tuple[Decimal, Decimal] | None:
    for prefix, rates in _ANTHROPIC_RATE_CARD.items():
        if model.startswith(prefix):
            return rates
    return None
