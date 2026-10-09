"""LiteLLM provider -- routes LLM calls through LiteLLM's unified API.

Supports 100+ models from all major providers (OpenAI, Anthropic, Google,
Mistral, DeepSeek, Cohere, Ollama, etc.) via a single integration.

Requires the optional ``litellm`` dependency::

    pip install sova[litellm]
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import time
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from sova.llm.client import cached_enumeration
from sova.llm.errors import LLMError, ProviderUnavailableError, classify_exception
from sova.llm.gcp_auth import VertexTokenProvider
from sova.llm.models import (
    CURATED_MODELS,
    CostSource,
    LLMResult,
    ModelFamily,
    ModelInfo,
    StreamEvent,
    classify_model_family,
    is_local_model_id,
    model_tier_for_id,
)
from sova.llm.provider import LLMProvider, ProviderCapabilities, _measure_ms
from sova.utils.logging import get_logger

log = get_logger(component="llm.litellm")

# Enumeration sources (Vertex publisher catalog, Ollama /api/tags, an
# OpenAI-compatible /v1/models) are all best-effort discovery, not the model
# invocation path, so they get their own short bound distinct from
# _DEFAULT_TIMEOUT below.
_ENUMERATION_HTTP_TIMEOUT = 10.0

# Every publisher this provider knows how to enumerate on Vertex AI. "openai"
# is included because Vertex's openai publisher serves gpt-oss (open weight)
# models, filtered below to exclude any proprietary GPT/o-series entry.
_VERTEX_PUBLISHERS: tuple[str, ...] = ("anthropic", "google", "openai")

# Page size for the Vertex publisher-models API. Only the first page is read:
# a catalog larger than this is silently truncated rather than paged, which is
# acceptable here in a way it would not be for a count-based gate (see the
# reviewThreads truncation rule in .claude/rules/architecture.md), because a
# short enumeration list only degrades a suggestion, never a decision.
_VERTEX_PAGE_SIZE = 1000

_OLLAMA_DEFAULT_BASE = "http://localhost:11434"

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _credential_safe_target(url: str) -> bool:
    """Return True when *url* may carry a bearer credential.

    A plaintext credential must never leave the machine over an unencrypted
    channel: safe when the scheme is ``https`` (the wire is encrypted end to
    end) or when the host resolves to loopback (the request never leaves the
    machine even over plain HTTP, e.g. a local OpenAI-compatible shim). An
    unparseable host is treated as unsafe, the fail-closed direction.
    """
    parsed = urlsplit(url)
    if parsed.scheme == "https":
        return True
    host = (parsed.hostname or "").strip("[]")
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


async def _fetch_json_entries(
    url: str,
    key: str,
    log_event: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, int] | None = None,
) -> list[dict]:
    """GET *url* and return the dict entries of its ``data[key]`` list.

    Shared by every enumeration source (Vertex publisher catalog, Ollama
    ``/api/tags``, an OpenAI-compatible ``/v1/models``): all three are
    best-effort discovery, so an unreachable or malformed source contributes
    nothing rather than raising.

    Every layer of the payload is shape-checked rather than trusted, because
    ``api_base`` is operator-supplied and an OpenAI-compatible endpoint is an
    arbitrary third-party server: a top-level scalar, a ``data[key]`` that is
    not a list, or a non-dict entry inside it would each otherwise reach a
    caller's ``entry.get(...)`` as an ``AttributeError`` that escapes this
    module's never-raise contract (the same "valid JSON is not necessarily a
    JSON object" trap documented in .claude/rules/architecture.md).
    """
    try:
        async with httpx.AsyncClient(timeout=_ENUMERATION_HTTP_TIMEOUT) as client:
            resp = await client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception:  # noqa: BLE001 (enumeration is advisory: a failed source yields no models, never an error)
        log.debug(log_event, url=url, exc_info=True)
        return []
    if not isinstance(data, dict):
        return []
    entries = data.get(key, [])
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _vertex_project_id() -> str:
    """Return the configured Vertex AI project, or "" when Vertex is not in use."""
    return os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "").strip()


def _vertex_region() -> str:
    """Return the Vertex AI region the publisher catalog should be read from."""
    return os.environ.get("CLOUD_ML_REGION", "us-east5")


def _dedup_models(models: list[ModelInfo]) -> list[ModelInfo]:
    """Keep the first entry for each id, in encounter order."""
    by_id: dict[str, ModelInfo] = {}
    for model in models:
        by_id.setdefault(model.id, model)
    return list(by_id.values())


def _vertex_model_id(entry: dict) -> str:
    """Extract the bare model id from a Vertex PublisherModel's ``name`` field.

    ``name`` is formatted ``publishers/{publisher}/models/{model}``.
    """
    name = str(entry.get("name") or "")
    return name.rsplit("/", 1)[-1] if name else ""


try:
    import litellm  # type: ignore[import-untyped]

    _HAS_LITELLM = True
except Exception:  # noqa: BLE001 (catch broken installs (AttributeError, SyntaxError, etc.))
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
        vendor: str = "litellm",
    ) -> None:
        _check_litellm()
        self.model = model
        self.fallback_model = fallback_model
        self.api_base = api_base
        self.timeout = timeout or self._DEFAULT_TIMEOUT
        # Which cfg.llm.provider value constructed this instance (litellm,
        # hybrid, openai, ollama, or vertex; see create_provider()).
        # check_available() uses it to run a vendor-specific credential
        # check instead of reporting "litellm is importable" as authenticated
        # for a vendor it has no actual evidence about.
        self.vendor = vendor
        # Warn once per model per provider instance: a long-running process can
        # invoke the same unpriced model many times, and repeating the warning
        # on every call would bury the signal it is meant to raise. Scoped to
        # the instance (not module level) so it resets naturally when a new
        # provider is constructed (e.g. reload_provider()) rather than needing
        # an explicit reset hook.
        self._warned_unpriced_models: set[str] = set()
        self._vertex_token_provider = VertexTokenProvider()

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
        except Exception as exc:  # noqa: BLE001 (LiteLLM raises arbitrary exceptions from providers)
            if not self.fallback_model or target_model == self.fallback_model:
                raise
            reason = "connection_error" if _is_connection_error(exc) else "api_error"
            log.warning(
                "llm.litellm.fallback",
                primary=target_model,
                fallback=self.fallback_model,
                reason=reason,
                exc_info=True,
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
        except Exception as exc:  # noqa: BLE001 (LiteLLM raises arbitrary exceptions from providers)
            if not self.fallback_model or target_model == self.fallback_model:
                raise
            reason = "connection_error" if _is_connection_error(exc) else "api_error"
            log.warning(
                "llm.litellm.stream_fallback",
                primary=target_model,
                fallback=self.fallback_model,
                reason=reason,
                exc_info=True,
            )
            start = time.monotonic()
            async for event in self._stream(self.fallback_model, prompt, start):
                yield event

    async def list_available_models(self, *, allow_probe: bool = True) -> list[ModelInfo]:
        """Enumerate reachable models across every configured backend.

        Attempts Vertex AI (if ``ANTHROPIC_VERTEX_PROJECT_ID`` is set),
        Ollama (if ``self.model`` targets it), and an OpenAI-compatible
        endpoint (if ``self.api_base`` is set and the model doesn't already
        identify a local or Vertex target). Falls back to the curated static
        list when none of those are configured or all of them fail.
        """
        if not allow_probe:
            return list(CURATED_MODELS)

        return await cached_enumeration(self._enumeration_identity(), self._enumerate_all_backends)

    async def _enumerate_all_backends(self) -> list[ModelInfo] | None:
        # Each source returns None when it is not configured at all, so
        # "configured but produced nothing" is read off the outcomes rather
        # than from a second copy of the three configuration conditions.
        #
        # Gathered rather than awaited in sequence: the sources are independent
        # and each is bounded by _ENUMERATION_HTTP_TIMEOUT, so running them one
        # after another would multiply that bound by the number configured for
        # no benefit. return_exceptions=True keeps this module's never-raise
        # contract intact even if a source grows an unguarded failure path, and
        # such a raise maps to [] (configured, produced nothing), never to None
        # (not configured): the former retries on the next call, while the
        # latter would cache the curated fallback as this deployment's answer.
        results = await asyncio.gather(
            self._enumerate_vertex(),
            self._enumerate_ollama(),
            self._enumerate_openai_compatible(),
            return_exceptions=True,
        )
        outcomes: list[list[ModelInfo] | None] = []
        for outcome in results:
            if isinstance(outcome, BaseException):
                log.debug("llm.litellm.enumeration_source_failed", error=str(outcome))
                outcomes.append([])
            else:
                outcomes.append(outcome)
        models = [model for outcome in outcomes if outcome for model in outcome]
        if models:
            return _dedup_models(models)
        if any(outcome is not None for outcome in outcomes):
            # Something was configured and produced nothing, so this is an
            # outage (ADC expired, Ollama daemon down, api_base unreachable),
            # not a fact about the deployment. None makes cached_enumeration()
            # serve the curated fallback without pinning it for the whole TTL,
            # so the next call retries instead of reporting a stale catalog.
            return None
        # Nothing to enumerate at all: the curated list is this deployment's
        # stable answer, so it is safe to cache.
        return list(CURATED_MODELS)

    def _enumeration_identity(self) -> str:
        """Return the enumeration cache key for this provider's deployment.

        May differ from ``client._provider_identity()`` (the config-derived
        key for the reactive negative cache): every vendor-alias provider
        type (litellm, vertex, openai, ollama, hybrid) constructs this same
        class, so this key is derived from what the instance actually knows
        (model, api_base) rather than the config section name. A mismatch
        only means the enumeration cache misses a sharing opportunity with
        the fallback loop's negative cache, never a correctness failure.

        The Vertex project and region are part of the key even though they
        come from the environment rather than the instance: they select which
        publisher catalog ``_enumerate_vertex()`` reads, so two deployments
        differing only in those would otherwise share one cached catalog.

        ``OPENAI_API_KEY`` is folded in the same way, as a SHA-256
        fingerprint rather than the raw key (matching
        ``_anthropic_api_enumeration_identity()``'s never-leaks-the-key
        contract): ``_enumerate_openai_compatible()`` resolves this same env
        var at call time, so two different accounts hitting the same
        ``api_base`` would otherwise share one cached catalog for up to the
        enumeration TTL.
        """
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        key_fingerprint = hashlib.sha256(api_key.encode()).hexdigest()[:12] if api_key else ""
        return f"litellm:{self.model}:{self.api_base or ''}:{key_fingerprint}:{_vertex_project_id()}:{_vertex_region()}"

    async def _enumerate_vertex(self) -> list[ModelInfo] | None:
        """Read the Vertex publisher catalog, or None when Vertex is not configured."""
        if not _vertex_project_id():
            return None

        try:
            token = await self._vertex_token_provider.get_token()
        except Exception:  # noqa: BLE001 (ADC unavailable/google-auth missing: fall back to curated, never raise)
            log.debug("llm.litellm.vertex_token_failed", exc_info=True)
            return []

        region = _vertex_region()
        domain = "aiplatform.googleapis.com" if region == "global" else f"{region}-aiplatform.googleapis.com"
        outcomes = await asyncio.gather(
            *(self._fetch_vertex_publisher(domain, token, publisher) for publisher in _VERTEX_PUBLISHERS),
            return_exceptions=True,
        )
        models: list[ModelInfo] = []
        for publisher, outcome in zip(_VERTEX_PUBLISHERS, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                log.debug("llm.litellm.vertex_publisher_failed", publisher=publisher, error=str(outcome))
                continue
            models.extend(outcome)
        return models

    async def _fetch_vertex_publisher(self, domain: str, token: str, publisher: str) -> list[ModelInfo]:
        entries = await _fetch_json_entries(
            f"https://{domain}/v1beta1/publishers/{publisher}/models",
            "publisherModels",
            # Distinct from the gather's llm.litellm.vertex_publisher_failed
            # below, which reports a publisher task raising rather than an
            # HTTP/payload failure this helper already absorbed.
            "llm.litellm.vertex_publisher_fetch_failed",
            headers={"Authorization": f"Bearer {token}"},
            params={"pageSize": _VERTEX_PAGE_SIZE},
        )

        models: list[ModelInfo] = []
        for entry in entries:
            model_id = _vertex_model_id(entry)
            if not model_id:
                continue
            family = classify_model_family(model_id)
            # Vertex's openai publisher serves gpt-oss (open weight) models
            # only; a proprietary GPT/o-series entry must never be reported
            # as reachable through a credential that does not grant it.
            if publisher == "openai" and family is not ModelFamily.OPENAI_OSS:
                continue
            models.append(
                ModelInfo(
                    id=model_id,
                    family=family,
                    tier=model_tier_for_id(model_id),
                    display_name=str(entry.get("displayName") or model_id),
                    source="vertex",
                )
            )
        return models

    async def _enumerate_ollama(self) -> list[ModelInfo] | None:
        """Read Ollama's tag list, or None when the model does not target Ollama."""
        if not self.model.startswith("ollama/"):
            return None
        base = (self.api_base or _OLLAMA_DEFAULT_BASE).rstrip("/")
        entries = await _fetch_json_entries(f"{base}/api/tags", "models", "llm.litellm.ollama_enumeration_failed")

        models: list[ModelInfo] = []
        for entry in entries:
            name = str(entry.get("name") or entry.get("model") or "")
            if not name:
                continue
            models.append(
                ModelInfo(id=f"ollama/{name}", family=ModelFamily.LOCAL, tier="", display_name=name, source="ollama")
            )
        return models

    async def _enumerate_openai_compatible(self) -> list[ModelInfo] | None:
        """Read an OpenAI-compatible /v1/models list, or None when no such endpoint applies."""
        if not self.api_base or self.model.startswith(("ollama/", "vertex_ai/")):
            return None
        base = self.api_base.rstrip("/")
        prefix = base if base.endswith("/v1") else f"{base}/v1"
        url = f"{prefix}/models"
        # Same credential source the LiteLLM invocation itself resolves this
        # endpoint's key from (see docs/model-selection-migration-guide.md,
        # "OpenAI"); an unauthenticated probe against a key-protected
        # endpoint gets a 401, which _fetch_json_entries silently turns into
        # an empty list, so enumeration never caches and keeps retrying.
        headers = None
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if api_key and _credential_safe_target(url):
            headers = {"Authorization": f"Bearer {api_key}"}
        entries = await _fetch_json_entries(
            url, "data", "llm.litellm.openai_compatible_enumeration_failed", headers=headers
        )

        models: list[ModelInfo] = []
        for entry in entries:
            model_id = str(entry.get("id") or "")
            if not model_id:
                continue
            models.append(
                ModelInfo(
                    id=model_id,
                    family=classify_model_family(model_id),
                    tier=model_tier_for_id(model_id),
                    display_name=model_id,
                    source="openai_compatible",
                )
            )
        return models

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
        except Exception as exc:  # noqa: BLE001 (LiteLLM raises arbitrary exceptions from providers)
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
        """Check LiteLLM availability plus, for a known vendor, its credentials.

        ``litellm`` being importable only proves the package is installed; it
        says nothing about whether the active vendor (openai/ollama/vertex)
        can actually serve a request. ``vendor="litellm"``/``"hybrid"`` (generic
        routing, no fixed vendor) keep the package-only check, since there is
        no single credential shape to probe.
        """
        if not _HAS_LITELLM:
            return False, "litellm is not installed -- pip install sova[litellm]"
        version = getattr(litellm, "__version__", "unknown")

        if self.vendor == "openai":
            if not os.environ.get("OPENAI_API_KEY", "").strip():
                return False, f"litellm {version} but OPENAI_API_KEY is not set"
            return True, f"litellm {version} (OPENAI_API_KEY set)"

        if self.vendor == "ollama":
            base = (self.api_base or _OLLAMA_DEFAULT_BASE).rstrip("/")
            # api_base is caller-supplied (the Connections page lets an operator
            # test a candidate endpoint before activating it), so only http/https
            # are allowed: a file://, gopher://, or other exotic scheme turned
            # this reachability probe into an SSRF primitive with a stronger
            # blast radius than "is this host up".
            if urlsplit(base).scheme not in ("http", "https"):
                return False, f"litellm {version} but {base!r} is not an http(s) URL"
            try:
                async with httpx.AsyncClient(timeout=_ENUMERATION_HTTP_TIMEOUT) as client:
                    resp = await client.get(f"{base}/api/tags")
                resp.raise_for_status()
            except Exception:  # noqa: BLE001 (any connectivity failure means "not reachable", detail not load-bearing)
                return False, f"litellm {version} but Ollama is not reachable at {base}"
            return True, f"litellm {version} (Ollama reachable at {base})"

        if self.vendor == "vertex":
            if not _vertex_project_id():
                return False, f"litellm {version} but ANTHROPIC_VERTEX_PROJECT_ID is not set"
            try:
                await self._vertex_token_provider.get_token()
            except Exception:  # noqa: BLE001 (ADC unavailable/expired all mean "not authenticated" here)
                return False, f"litellm {version} but Google Application Default Credentials are not available"
            return True, f"litellm {version} (Vertex AI credentials available)"

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
        except Exception:  # noqa: BLE001 (litellm pricing tables raise varied errors for unknown models)
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
