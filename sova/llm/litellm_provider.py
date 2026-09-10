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

from sova.llm.errors import LLMError, ProviderUnavailableError, classify_exception
from sova.llm.models import CostSource, LLMResult, StreamEvent, is_local_model_id
from sova.llm.provider import LLMProvider, ProviderCapabilities, _measure_ms
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


def _extract_chunk_usage(chunk: object) -> tuple[int | None, int | None, str | None]:
    """Extract token usage and model name from a LiteLLM stream chunk.

    Returns ``(None, None, ...)`` for usage when the chunk carries none, so callers
    can distinguish "no usage on this chunk" from "usage present but zero".
    """
    input_tokens = output_tokens = None
    if hasattr(chunk, "usage") and chunk.usage:
        input_tokens = getattr(chunk.usage, "prompt_tokens", 0) or 0
        output_tokens = getattr(chunk.usage, "completion_tokens", 0) or 0
    model = chunk.model if hasattr(chunk, "model") and chunk.model else None
    return input_tokens, output_tokens, model


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


def _as_llm_error(exc: BaseException) -> LLMError:
    """Map an SDK exception onto the typed hierarchy, preserving its message.

    Connection failures are resolved via _is_connection_error, which walks the
    cause chain LiteLLM wraps its transport errors in; everything else goes
    through the shared classifier.
    """
    error_cls = ProviderUnavailableError if _is_connection_error(exc) else classify_exception(exc)
    return error_cls(str(exc))


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

    @property
    def capabilities(self) -> ProviderCapabilities:
        # reports_cost=True: LiteLLM populates a real per-result cost for any
        # model in its pricing database, and _get_cost()/cost_source now flag
        # the unpriced case (CostSource.UNKNOWN/FREE_LOCAL) instead of a value
        # that lies. supports_budget_cap stays False: max_budget_usd is still
        # accepted but ignored by this provider (see class docstring), so the
        # client-side budget-cap gate is the only enforcement for it.
        return ProviderCapabilities(reports_cost=True, dynamic_models=True)

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
        # Warn once per model per provider instance: a long-running process can
        # invoke the same unpriced model many times, and repeating the warning
        # on every call would bury the signal it is meant to raise. Scoped to
        # the instance (not module level) so it resets naturally when a new
        # provider is constructed (e.g. reload_provider()) rather than needing
        # an explicit reset hook.
        self._warned_unpriced_models: set[str] = set()

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
        target_model = model or self.model
        effective_timeout = timeout if timeout is not None else self.timeout
        start = time.monotonic()

        try:
            return await self._call(
                target_model,
                prompt,
                timeout=effective_timeout,
                start=start,
                system_prompt=system_prompt,
                max_tokens=max_tokens,
            )
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
            return await self._call(
                self.fallback_model,
                prompt,
                timeout=effective_timeout,
                start=start,
                system_prompt=system_prompt,
                max_tokens=max_tokens,
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

        try:
            response = await litellm.acompletion(  # type: ignore[union-attr]
                messages=messages,
                stream=True,
                **kwargs,
            )
        except Exception as exc:
            raise _as_llm_error(exc) from exc

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

                chunk_input, chunk_output, chunk_model = _extract_chunk_usage(chunk)
                if chunk_input is not None:
                    input_tokens, output_tokens = chunk_input, chunk_output
                if chunk_model:
                    response_model = chunk_model
        except Exception as exc:
            log.error("llm.litellm.stream_error", model=model, exc_info=True)
            accumulated_text = "".join(text_parts)
            cost, cost_source = self._get_cost(response_model, input_tokens, output_tokens, requested_model=model)
            yield StreamEvent(
                type="result",
                text=accumulated_text,
                result=LLMResult(
                    text=accumulated_text,
                    model=response_model,
                    cost_usd=cost,
                    cost_source=cost_source,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    duration_ms=_measure_ms(start),
                    stop_reason="error",
                ),
            )
            raise _as_llm_error(exc) from exc

        accumulated_text = "".join(text_parts)
        cost, cost_source = self._get_cost(response_model, input_tokens, output_tokens, requested_model=model)
        result = LLMResult(
            text=accumulated_text,
            model=response_model,
            cost_usd=cost,
            cost_source=cost_source,
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
        system_prompt: str | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        messages = _build_messages(prompt, system_prompt=system_prompt)
        kwargs = self._base_kwargs(model, max_tokens=max_tokens)
        if timeout:
            kwargs["timeout"] = timeout

        log.info("llm.litellm.invoke", model=model, prompt_len=len(prompt))

        try:
            response = await litellm.acompletion(  # type: ignore[union-attr]
                messages=messages,
                **kwargs,
            )
        except Exception as exc:
            raise _as_llm_error(exc) from exc

        text = response.choices[0].message.content or ""
        usage = response.usage
        input_tokens = getattr(usage, "prompt_tokens", 0) or 0
        output_tokens = getattr(usage, "completion_tokens", 0) or 0
        response_model = getattr(response, "model", model) or model
        cost, cost_source = self._get_cost(
            response_model, input_tokens, output_tokens, completion_response=response, requested_model=model
        )
        stop = response.choices[0].finish_reason or "end_turn"

        return LLMResult(
            text=text,
            model=response_model,
            cost_usd=cost,
            cost_source=cost_source,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=_measure_ms(start),
            stop_reason=stop if stop != "stop" else "end_turn",
        )

    def _base_kwargs(self, model: str, *, max_tokens: int | None = None) -> dict:
        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens if max_tokens is not None else 4096,
        }
        if self.api_base:
            kwargs["api_base"] = self.api_base
        return kwargs

    def _report_unpriced(self, model: str, requested_model: str, *, exc_info: bool = False) -> CostSource:
        """Log a cost lookup miss (once per model per instance) and return its provenance.

        *model* is the ID the provider echoed back; *requested_model* is the ID the
        call was made with. Both are checked, because LiteLLM does not guarantee
        the echoed ID carries the provider prefix the request used. Local/
        self-hosted models (e.g. Ollama) are never in LiteLLM's hosted pricing
        database, so a lookup miss there is expected and harmless (logged at debug,
        ``CostSource.FREE_LOCAL``), unlike a genuine pricing gap on a hosted model
        (logged as a warning, ``CostSource.UNKNOWN``). Dedup is keyed on the echoed
        *model*, matching what is stored and compared on every call.
        """
        already_warned = model in self._warned_unpriced_models
        self._warned_unpriced_models.add(model)
        if is_local_model_id(model) or is_local_model_id(requested_model):
            if not already_warned:
                log.debug("llm.litellm.cost_unknown_local", model=model)
            return CostSource.FREE_LOCAL
        if not already_warned:
            log.warning("llm.litellm.cost_unknown", model=model, exc_info=exc_info)
        return CostSource.UNKNOWN

    def _get_cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        *,
        completion_response: object | None = None,
        requested_model: str = "",
    ) -> tuple[Decimal, CostSource]:
        """Get cost and its provenance from LiteLLM's cost tracking.

        When *completion_response* is provided (non-streaming calls), passes it
        directly to ``litellm.completion_cost`` for accurate pricing.  For
        streaming calls where only token counts are available, uses
        ``litellm.cost_per_token`` for per-token pricing.

        Returns ``(Decimal('0'), CostSource.UNKNOWN | CostSource.FREE_LOCAL)`` when
        cost calculation fails, *and* when it succeeds but reports a non-positive
        figure: LiteLLM's documented failure mode for an unpriced model is
        returning ``0.0`` from ``completion_cost()``/``cost_per_token()`` instead
        of raising, so a bare try/except would otherwise let that case through as
        a silent, unlogged $0: exactly the budget blind spot this reporting
        exists to surface.
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
            decimal_cost = Decimal(str(cost))
        except Exception:  # noqa: BLE001
            return Decimal("0"), self._report_unpriced(model, requested_model, exc_info=True)
        if decimal_cost <= 0:
            return Decimal("0"), self._report_unpriced(model, requested_model)
        return decimal_cost, CostSource.PRICED


def _build_messages(prompt: str, *, system_prompt: str | None = None) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    return messages
