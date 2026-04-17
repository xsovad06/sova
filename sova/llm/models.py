"""Data models for the LLM interaction layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


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
