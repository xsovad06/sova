"""LLM Provider abstraction layer.

Defines the abstract interface that all LLM backends must implement.
The default provider (ClaudeCodeProvider) wraps the Claude Code CLI.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

from sova.llm.models import LLMResult, StreamEvent


class LLMProvider(ABC):
    """Abstract interface for LLM providers.

    All LLM interactions in SOVA flow through this interface, enabling
    provider-agnostic code in workflow steps and roles.
    """

    @abstractmethod
    async def invoke(
        self,
        prompt: str,
        *,
        model: str | None = None,
        cwd: Path | str | None = None,
        max_budget_usd: Decimal | None = None,
        timeout: float | None = 600,
    ) -> LLMResult:
        """Run a prompt and return the parsed result."""
        ...

    @abstractmethod
    async def invoke_streaming(
        self,
        prompt: str,
        *,
        model: str | None = None,
        cwd: Path | str | None = None,
        max_budget_usd: Decimal | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Run a prompt with streaming output, yielding events."""
        ...
        yield  # pragma: no cover -- required for async generator typing

    async def invoke_command(
        self,
        command: str,
        args: str = "",
        *,
        model: str | None = None,
        cwd: Path | str | None = None,
        max_budget_usd: Decimal | None = None,
        timeout: float | None = 600,
    ) -> LLMResult:
        """Run a slash command (e.g., /develop, /review).

        Default implementation constructs a prompt and delegates to invoke().
        Providers that support native command dispatch can override this.
        """
        prompt = f"{command} {args}".strip() if args else command
        log.info("llm.invoke_command", command=command, args_len=len(args), model=model)
        return await self.invoke(prompt, model=model, cwd=cwd, max_budget_usd=max_budget_usd, timeout=timeout)

    def normalize_model_name(self, model: str) -> str:
        """Map generic model tiers (fast/smart/cheap) to provider-specific IDs.

        Default implementation returns the model unchanged. Providers can
        override to map generic tiers to provider-specific identifiers.
        """
        return model

    @abstractmethod
    async def check_available(self) -> tuple[bool, str]:
        """Check if this provider is available and configured.

        Returns:
            Tuple of (available, detail_message).
        """
        ...


def create_provider(provider_type: str = "claude-code") -> LLMProvider:
    """Create an LLM provider instance by type name.

    Args:
        provider_type: Provider identifier (e.g., "claude-code", "litellm").

    Returns:
        An LLMProvider instance.

    Raises:
        ValueError: If the provider type is unknown.
        ImportError: If litellm is not installed.
    """
    if provider_type == "claude-code":
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        return ClaudeCodeProvider()

    if provider_type == "litellm":
        from sova.llm.litellm_provider import LiteLLMProvider

        return LiteLLMProvider()

    available = ["claude-code", "litellm"]
    raise ValueError(f"Unknown LLM provider: {provider_type!r}. Available: {', '.join(available)}")


def _measure_ms(start: float) -> int:
    """Convert a time.monotonic() start to elapsed milliseconds."""
    return int((time.monotonic() - start) * 1000)
