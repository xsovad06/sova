"""Anthropic Messages API provider (direct SDK, no CLI wrapper).

Uses the ``anthropic`` SDK as an optional dependency::

    pip install sova[anthropic]
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

from sova.llm.errors import classify_exception
from sova.llm.models import LLMResult, StreamEvent, compute_anthropic_cost
from sova.llm.provider import LLMProvider, _measure_ms
from sova.utils.logging import get_logger

log = get_logger(component="llm.anthropic_api")

try:
    import anthropic  # type: ignore[import-untyped]

    _HAS_ANTHROPIC = True
except ImportError:
    _HAS_ANTHROPIC = False

_MODEL_ALIASES: dict[str, str] = {
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-5",
    "haiku": "claude-haiku-4-5-20251001",
    "fast": "claude-sonnet-5",
    "smart": "claude-opus-5",
    "cheap": "claude-haiku-4-5-20251001",
}

_DEFAULT_MODEL = "claude-sonnet-5"
_DEFAULT_MAX_TOKENS = 4096


def _check_anthropic() -> None:
    if not _HAS_ANTHROPIC:
        raise ImportError("anthropic is not installed. Install it with: pip install sova[anthropic]")


def _sanitize_error(exc: Exception, api_key: str = "") -> str:
    """Return an error message that never leaks the API key."""
    msg = str(exc)
    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if key and key in msg:
        msg = msg.replace(key, "***")
    return msg


class AnthropicAPIProvider(LLMProvider):
    """LLM provider using the Anthropic Messages API directly."""

    def __init__(self, *, model: str = "", max_tokens: int | None = None, api_key: str = "") -> None:
        _check_anthropic()
        self._default_model = model or _DEFAULT_MODEL
        self._default_max_tokens = max_tokens or _DEFAULT_MAX_TOKENS
        self._api_key = (api_key or "").strip()
        self._client: anthropic.AsyncAnthropic | None = None
        self._init_lock = asyncio.Lock()

    def _resolve_api_key(self) -> str:
        """Return the configured key, falling back to the env var."""
        return self._api_key or os.environ.get("ANTHROPIC_API_KEY", "").strip()

    def _validate_base_url(self, base_url: str | None) -> None:
        """Validate ANTHROPIC_BASE_URL is HTTPS and from an approved origin."""
        if not base_url:
            return  # Default Anthropic API endpoint is used

        # Parse the URL to check scheme and host
        from urllib.parse import urlparse

        parsed = urlparse(base_url)

        # Require HTTPS to prevent cleartext transmission of API key
        if parsed.scheme != "https":
            raise RuntimeError(
                f"ANTHROPIC_BASE_URL must use HTTPS (got {parsed.scheme}://). "
                "API keys cannot be sent over insecure connections."
            )

        # Validate against approved origins (official Anthropic endpoints)
        approved_hosts = {
            "api.anthropic.com",  # Direct API
            "api.claude.ai",  # Claude.ai backend
        }
        # Allow Vertex AI endpoints
        if parsed.hostname and (
            parsed.hostname in approved_hosts
            or parsed.hostname.endswith(".googleapis.com")
            or parsed.hostname.endswith(".anthropic.com")
        ):
            return

        raise RuntimeError(
            f"ANTHROPIC_BASE_URL '{base_url}' is not an approved endpoint. "
            f"Approved: {', '.join(sorted(approved_hosts))} or *.googleapis.com"
        )

    async def _get_client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            async with self._init_lock:
                if self._client is None:
                    key = self._resolve_api_key()
                    if not key:
                        raise RuntimeError("Anthropic API key is not configured (set llm.api_key or ANTHROPIC_API_KEY)")

                    # Validate base URL before creating client
                    base_url = os.environ.get("ANTHROPIC_BASE_URL")
                    self._validate_base_url(base_url)

                    self._client = anthropic.AsyncAnthropic(api_key=key)
        return self._client

    def normalize_model_name(self, model: str) -> str:
        return _MODEL_ALIASES.get(model, model)

    async def invoke(
        self,
        prompt: str,
        *,
        model: str | None = None,
        fallback_model: str | None = None,
        cwd: Path | str | None = None,
        max_budget_usd: Decimal | None = None,
        timeout: float | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        resolved_model = self.normalize_model_name(model or self._default_model)
        resolved_max_tokens = max_tokens if max_tokens is not None else self._default_max_tokens
        start = time.monotonic()

        log.info("llm.anthropic_api.invoke", model=resolved_model, prompt_len=len(prompt))

        kwargs: dict = {
            "model": resolved_model,
            "max_tokens": resolved_max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if timeout is not None:
            kwargs["timeout"] = timeout

        try:
            client = await self._get_client()
            response = await client.messages.create(**kwargs)
        except Exception as exc:
            message = f"Anthropic API error: {_sanitize_error(exc, self._resolve_api_key())}"
            raise classify_exception(exc)(message) from exc

        text = ""
        for block in response.content:
            if block.type == "text":
                text += block.text

        usage = response.usage
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0

        cost = compute_anthropic_cost(
            resolved_model,
            input_tokens,
            output_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
        )

        return LLMResult(
            text=text,
            model=response.model,
            cost_usd=cost,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            duration_ms=_measure_ms(start),
            stop_reason=response.stop_reason or "end_turn",
        )

    async def invoke_streaming(
        self,
        prompt: str,
        *,
        model: str | None = None,
        cwd: Path | str | None = None,
        max_budget_usd: Decimal | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        resolved_model = self.normalize_model_name(model or self._default_model)
        resolved_max_tokens = max_tokens if max_tokens is not None else self._default_max_tokens
        start = time.monotonic()

        log.info("llm.anthropic_api.stream", model=resolved_model, prompt_len=len(prompt))

        kwargs: dict = {
            "model": resolved_model,
            "max_tokens": resolved_max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if timeout is not None:
            kwargs["timeout"] = timeout

        text_parts: list[str] = []
        input_tokens = 0
        output_tokens = 0
        cache_read = 0
        cache_creation = 0
        response_model = resolved_model
        stop_reason = "end_turn"
        error: Exception | None = None

        client = await self._get_client()
        try:
            async with client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    if event.type == "content_block_delta" and hasattr(event.delta, "text"):
                        text_parts.append(event.delta.text)
                        yield StreamEvent(type="content", text=event.delta.text)
                    elif event.type == "message_start" and hasattr(event, "message"):
                        response_model = getattr(event.message, "model", resolved_model)
                        msg_usage = getattr(event.message, "usage", None)
                        if msg_usage:
                            input_tokens = getattr(msg_usage, "input_tokens", 0) or 0
                            cache_read = getattr(msg_usage, "cache_read_input_tokens", 0) or 0
                            cache_creation = getattr(msg_usage, "cache_creation_input_tokens", 0) or 0
                    elif event.type == "message_delta":
                        delta_usage = getattr(event, "usage", None)
                        if delta_usage:
                            output_tokens = getattr(delta_usage, "output_tokens", 0) or 0
                        stop_reason = getattr(event.delta, "stop_reason", None) or stop_reason
        except Exception as exc:
            stop_reason = "error"
            error = exc

        accumulated = "".join(text_parts)
        cost = compute_anthropic_cost(
            response_model,
            input_tokens,
            output_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
        )
        result = LLMResult(
            text=accumulated,
            model=response_model,
            cost_usd=cost,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            duration_ms=_measure_ms(start),
            stop_reason=stop_reason,
        )
        yield StreamEvent(type="result", text=accumulated, result=result)

        if error is not None:
            message = f"Anthropic streaming error: {_sanitize_error(error, self._resolve_api_key())}"
            raise classify_exception(error)(message) from error

    async def check_available(self) -> tuple[bool, str]:
        if not _HAS_ANTHROPIC:
            return False, "anthropic SDK is not installed (pip install sova[anthropic])"
        key = self._resolve_api_key()
        if not key:
            return False, "Anthropic API key is not configured (set llm.api_key or ANTHROPIC_API_KEY)"
        try:
            client = await self._get_client()
            # Issue a minimal authenticated API request to validate credentials
            # Use a very small max_tokens to minimize cost (~$0.0001)
            await client.messages.create(
                model=self._default_model,
                max_tokens=1,
                messages=[{"role": "user", "content": "test"}],
            )
            return True, f"anthropic SDK {anthropic.__version__}"
        except Exception as exc:
            return False, f"Anthropic API unavailable: {_sanitize_error(exc, key)}"
