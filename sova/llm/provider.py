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

from sova.llm.models import BatchRequest, BatchResult, LLMResult, StreamEvent
from sova.utils.logging import get_logger

log = get_logger(component="llm.provider")


def _assert_command_exists(command: str, cwd: Path) -> None:
    """Fail fast if a slash command file is missing from the target project."""
    name = command.lstrip("/")
    if not name or "/" in name or "\\" in name or ".." in name:
        raise RuntimeError(f"Invalid slash command: {command!r}")
    cmd_path = cwd / ".claude" / "commands" / f"{name}.md"
    if not cmd_path.is_file():
        raise RuntimeError(
            f"Command {command} not found at {cmd_path}. Run 'sova commands update --project {cwd}' to install it."
        )


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
        if command.startswith("/") and cwd:
            _assert_command_exists(command, Path(cwd))
        prompt = f"{command} {args}".strip() if args else command
        log.info("llm.invoke_command", command=command, args_len=len(args), model=model)
        return await self.invoke(prompt, model=model, cwd=cwd, max_budget_usd=max_budget_usd, timeout=timeout)

    async def invoke_batch(
        self,
        requests: list[BatchRequest],
        *,
        poll_interval: int = 60,
        timeout: int = 86400,
    ) -> list[BatchResult]:
        """Submit a batch of prompts and return results in input order.

        Default implementation calls invoke() sequentially.
        Batch-capable providers override this.

        Limitation: invoke() accepts neither a system prompt nor a token cap,
        so BatchRequest.system and BatchRequest.max_tokens are ignored on this
        path. Batch-capable backends honor both.
        """
        results: list[BatchResult] = []
        deadline = time.monotonic() + timeout
        for req in requests:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                results.append(BatchResult(request=req, error=f"Batch timeout of {timeout}s exhausted"))
                continue
            try:
                r = await self.invoke(
                    req.prompt,
                    model=req.model or None,
                    timeout=remaining,
                )
                results.append(BatchResult(request=req, result=r))
            except Exception as exc:
                results.append(BatchResult(request=req, error=str(exc)))
        return results

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


def create_provider(
    provider_type: str = "claude-code",
    *,
    model: str = "",
    fallback_model: str = "",
    api_base: str = "",
) -> LLMProvider:
    """Create an LLM provider instance by type name.

    Args:
        provider_type: Provider identifier (e.g., "claude-code", "litellm").
        model: Model name to use (LiteLLM only; ignored for claude-code).
        fallback_model: Fallback model on primary failure (LiteLLM only).
        api_base: Custom API base URL (LiteLLM only).

    Returns:
        An LLMProvider instance.

    Raises:
        ValueError: If the provider type is unknown.
        ImportError: If litellm is not installed.
    """
    if provider_type == "claude-code":
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        return ClaudeCodeProvider()

    if provider_type in ("litellm", "hybrid"):
        from sova.llm.litellm_provider import LiteLLMProvider

        return LiteLLMProvider(
            model=model or "claude-sonnet-4-6",
            fallback_model=fallback_model or None,
            api_base=api_base or None,
        )

    available = ["claude-code", "litellm", "hybrid"]
    raise ValueError(f"Unknown LLM provider: {provider_type!r}. Available: {', '.join(available)}")


def _measure_ms(start: float) -> int:
    """Convert a time.monotonic() start to elapsed milliseconds."""
    return int((time.monotonic() - start) * 1000)
