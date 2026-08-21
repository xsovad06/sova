"""LiteLLM provider -- routes LLM calls through LiteLLM's unified API.

Supports 100+ models from all major providers (OpenAI, Anthropic, Google,
Mistral, DeepSeek, Cohere, Ollama, etc.) via a single integration.

Requires the optional ``litellm`` dependency::

    pip install sova[litellm]
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

from sova.llm.models import LLMResult, StreamEvent
from sova.llm.provider import LLMProvider, _measure_ms
from sova.utils.logging import get_logger

log = get_logger(component="llm.litellm")

try:
    import litellm  # type: ignore[import-untyped]

    _HAS_LITELLM = True
except Exception:  # noqa: BLE001 -- catch broken installs (AttributeError, SyntaxError, etc.)
    _HAS_LITELLM = False


def _check_litellm() -> None:
    if not _HAS_LITELLM:
        raise ImportError("litellm is not installed. Install it with: pip install sova[litellm]")


def _is_connection_error(exc: BaseException) -> bool:
    """Check if an exception indicates a connection failure (e.g. Ollama down)."""
    error_types = ("ConnectionError", "ConnectError", "ConnectionRefusedError")
    if type(exc).__name__ in error_types:
        return True
    # LiteLLM wraps connection errors; check the chain
    cause = exc.__cause__ or exc.__context__
    if cause and type(cause).__name__ in error_types:
        return True
    msg = str(exc).lower()
    return "connection refused" in msg or "connect error" in msg


class LiteLLMProvider(LLMProvider):
    """Routes LLM calls through LiteLLM's unified API.

    Supports automatic fallback: if the primary model fails, the fallback
    model is tried. Cost tracking maps LiteLLM's response metadata to
    SOVA's LLMResult format.

    Note: ``cwd`` and ``max_budget_usd`` parameters are accepted by the
    interface but ignored -- they are Claude Code CLI-specific concepts.
    LiteLLM uses API keys and model-level pricing instead.
    """

    _DEFAULT_TIMEOUT: float = 300.0

    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        fallback_model: str | None = None,
        api_base: str | None = None,
        timeout: float | None = None,
    ) -> None:
        _check_litellm()
        self.model = model
        self.fallback_model = fallback_model
        self.api_base = api_base
        self.timeout = timeout or self._DEFAULT_TIMEOUT

    async def invoke(
        self,
        prompt: str,
        *,
        model: str | None = None,
        fallback_model: str | None = None,
        cwd: Path | str | None = None,
        max_budget_usd: Decimal | None = None,
        timeout: float | None = None,
    ) -> LLMResult:
        target_model = model or self.model
        effective_timeout = timeout if timeout is not None else self.timeout
        start = time.monotonic()

        try:
            return await self._call(target_model, prompt, timeout=effective_timeout, start=start)
        except Exception as exc:
            if not self.fallback_model or target_model == self.fallback_model:
                raise
            reason = "connection_error" if _is_connection_error(exc) else "api_error"
            log.warning(
                "llm.litellm.fallback",
                primary=target_model,
                fallback=self.fallback_model,
                reason=reason,
            )
            start = time.monotonic()
            return await self._call(self.fallback_model, prompt, timeout=effective_timeout, start=start)

    async def invoke_streaming(
        self,
        prompt: str,
        *,
        model: str | None = None,
        cwd: Path | str | None = None,
        max_budget_usd: Decimal | None = None,
    ) -> AsyncIterator[StreamEvent]:
        target_model = model or self.model
        start = time.monotonic()

        try:
            async for event in self._stream(target_model, prompt, start):
                yield event
        except Exception as exc:
            if not self.fallback_model or target_model == self.fallback_model:
                raise
            reason = "connection_error" if _is_connection_error(exc) else "api_error"
            log.warning(
                "llm.litellm.stream_fallback",
                primary=target_model,
                fallback=self.fallback_model,
                reason=reason,
            )
            start = time.monotonic()
            async for event in self._stream(self.fallback_model, prompt, start):
                yield event

    async def _stream(
        self,
        model: str,
        prompt: str,
        start: float,
    ) -> AsyncIterator[StreamEvent]:
        messages = _build_messages(prompt)
        kwargs = self._base_kwargs(model)

        log.info("llm.litellm.stream", model=model, prompt_len=len(prompt))

        response = await litellm.acompletion(  # type: ignore[union-attr]
            messages=messages,
            stream=True,
            **kwargs,
        )

        text_parts: list[str] = []
        input_tokens = 0
        output_tokens = 0
        response_model = model

        try:
            async for chunk in response:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    text_parts.append(delta.content)
                    yield StreamEvent(type="content", text=delta.content)

                if hasattr(chunk, "usage") and chunk.usage:
                    input_tokens = getattr(chunk.usage, "prompt_tokens", 0) or 0
                    output_tokens = getattr(chunk.usage, "completion_tokens", 0) or 0

                if hasattr(chunk, "model") and chunk.model:
                    response_model = chunk.model
        except Exception:
            log.error("llm.litellm.stream_error", model=model, exc_info=True)
            accumulated_text = "".join(text_parts)
            cost = _get_cost(response_model, input_tokens, output_tokens)
            yield StreamEvent(
                type="result",
                text=accumulated_text,
                result=LLMResult(
                    text=accumulated_text,
                    model=response_model,
                    cost_usd=cost,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    duration_ms=_measure_ms(start),
                    stop_reason="error",
                ),
            )
            raise

        accumulated_text = "".join(text_parts)
        cost = _get_cost(response_model, input_tokens, output_tokens)
        result = LLMResult(
            text=accumulated_text,
            model=response_model,
            cost_usd=cost,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=_measure_ms(start),
            stop_reason="end_turn",
        )
        yield StreamEvent(type="result", text=accumulated_text, result=result)

    async def check_available(self) -> tuple[bool, str]:
        if not _HAS_LITELLM:
            return False, "litellm is not installed -- pip install sova[litellm]"
        version = getattr(litellm, "__version__", "unknown")
        return True, f"litellm {version}"

    async def _call(
        self,
        model: str,
        prompt: str,
        *,
        timeout: float | None,
        start: float,
    ) -> LLMResult:
        messages = _build_messages(prompt)
        kwargs = self._base_kwargs(model)
        if timeout:
            kwargs["timeout"] = timeout

        log.info("llm.litellm.invoke", model=model, prompt_len=len(prompt))

        response = await litellm.acompletion(  # type: ignore[union-attr]
            messages=messages,
            **kwargs,
        )

        text = response.choices[0].message.content or ""
        usage = response.usage
        input_tokens = getattr(usage, "prompt_tokens", 0) or 0
        output_tokens = getattr(usage, "completion_tokens", 0) or 0
        response_model = getattr(response, "model", model) or model
        cost = _get_cost(response_model, input_tokens, output_tokens, completion_response=response)
        stop = response.choices[0].finish_reason or "end_turn"

        return LLMResult(
            text=text,
            model=response_model,
            cost_usd=cost,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=_measure_ms(start),
            stop_reason=stop if stop != "stop" else "end_turn",
        )

    def _base_kwargs(self, model: str) -> dict:
        kwargs: dict = {
            "model": model,
            "max_tokens": 4096,
        }
        if self.api_base:
            kwargs["api_base"] = self.api_base
        return kwargs


def _build_messages(prompt: str) -> list[dict[str, str]]:
    return [{"role": "user", "content": prompt}]


def _get_cost(
    model: str, input_tokens: int, output_tokens: int, *, completion_response: object | None = None
) -> Decimal:
    """Get cost from LiteLLM's cost tracking.

    When *completion_response* is provided (non-streaming calls), passes it
    directly to ``litellm.completion_cost`` for accurate pricing.  For
    streaming calls where only token counts are available, uses
    ``litellm.cost_per_token`` for per-token pricing.

    Returns Decimal('0') when cost calculation fails, including models that
    are not in LiteLLM's pricing database.
    """
    try:
        if completion_response is not None:
            cost = litellm.completion_cost(completion_response=completion_response)  # type: ignore[union-attr]
        else:
            prompt_cost, completion_cost = litellm.cost_per_token(  # type: ignore[union-attr]
                model=model,
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
            )
            cost = prompt_cost + completion_cost
        return Decimal(str(cost))
    except Exception:  # noqa: BLE001
        log.warning("llm.litellm.cost_unknown", model=model, exc_info=True)
        return Decimal("0")
