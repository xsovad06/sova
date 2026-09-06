"""LLM client -- thin delegation layer to the active provider.

All existing callers (``from sova.llm.client import invoke``) continue
to work unchanged.  The actual implementation lives in the configured
:class:`~sova.llm.provider.LLMProvider` (default: ClaudeCodeProvider).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from sova.config.models import LLMConfig, RolesConfig
from sova.llm.complexity import ComplexityTier
from sova.llm.errors import (
    LLMInvocationError,
    ModelUnavailableError,
    is_fallback_eligible,
    resolve_error_category,
)
from sova.llm.models import BatchRequest, BatchResult, LLMResult, StreamEvent
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.config.models import ProjectConfig
    from sova.llm.provider import LLMProvider

log = get_logger(component="llm.client")

_provider: LLMProvider | None = None


def get_provider() -> LLMProvider:
    """Return the active LLM provider, creating a default if needed."""
    global _provider  # noqa: PLW0603
    if _provider is None:
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        _provider = ClaudeCodeProvider()
    return _provider


def set_provider(provider: LLMProvider) -> None:
    """Replace the active LLM provider (e.g., from config at startup)."""
    global _provider  # noqa: PLW0603
    _provider = provider


def reset_provider() -> None:
    """Reset the global provider to None (for testing)."""
    global _provider  # noqa: PLW0603
    _provider = None


def reload_provider(cfg: ProjectConfig) -> None:
    """Recreate the global LLM provider from fresh config.

    Python's GIL ensures the reference swap is atomic. In-flight calls hold
    their own reference to the old provider, which stays alive via refcount.
    """
    from sova.llm.provider import create_provider

    set_provider(
        create_provider(
            cfg.llm.provider,
            model=cfg.llm.model,
            fallback_model=cfg.llm.fallback_model,
            api_base=cfg.llm.api_base,
            api_key=cfg.llm.api_key,
        )
    )


# ---------------------------------------------------------------------------
# Model fallback loop
#
# SOVA owns the fallback chain (docs/model-selection-architecture.md, Q5). The
# Claude CLI's --fallback-model stays as a fast provider-internal inner layer:
# the loop hands it the same next hop it would pick itself, so both layers
# agree. WorkflowEngine's own advance is off unless llm.engine_owned_fallback
# is set, so the two walks never nest.
# ---------------------------------------------------------------------------

# Floor for one attempt: a shorter slice is not worth starting.
_MIN_ATTEMPT_SECONDS = 60.0

# Multi-candidate chains stop just short of the caller's timeout so the loop
# raises its own terminal error before the engine's outer asyncio.timeout fires
# and mislabels the failure as step_hard_timeout (which also commits WIP work).
_DEADLINE_SAFETY_MARGIN = 0.95

# Negative entries expire quickly so a model enabled mid-run is picked up
# without restarting the process.
_UNAVAILABLE_TTL_SECONDS = 300.0

# (model, next_hop, timeout, max_budget_usd) -> result. Lets invoke() and
# invoke_command() share one chain walk without the loop knowing which
# provider call it drives.
_AttemptFn = Callable[[str | None, str | None, float, Decimal | None], Awaitable[LLMResult]]


class ModelAvailabilityCache:
    """Process-local negative cache of models that failed as unavailable.

    Populated purely reactively from ``ModelUnavailableError`` caught inside the
    fallback loop: it never probes the provider, so it adds no hang surface to
    the CLI callback or to ``spawn_direct`` subprocesses. Entries are per
    process, not per issue, and are lost on resume or restart.

    Keyed by ``(provider_identity, model)`` per docs/model-selection-architecture.md
    (Q2), not by model name alone: a process hosting multiple projects
    (``sova server start --multi``) must not let a model disabled on one
    project's deployment poison the lookup for another project whose deployment
    enables it.

    The cache is an optimization, never a hard gate. Callers must keep the last
    candidate even when every entry is marked, so a stale negative entry can
    never break all LLM calls in the process.
    """

    def __init__(self, ttl_seconds: float = _UNAVAILABLE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._expiry: dict[tuple[str, str], float] = {}

    def mark_unavailable(self, identity: str, model: str | None) -> None:
        """Record *model* as unavailable on *identity* for the TTL. ``None`` has no identity."""
        if model is None:
            return
        self._expiry[(identity, model)] = time.monotonic() + self._ttl

    def is_unavailable(self, identity: str, model: str | None) -> bool:
        """Return True while *model* has a live negative entry on *identity*."""
        if model is None:
            return False
        key = (identity, model)
        expiry = self._expiry.get(key)
        if expiry is None:
            return False
        if time.monotonic() >= expiry:
            del self._expiry[key]
            return False
        return True

    def reset(self) -> None:
        """Drop every entry (used by tests to avoid cross-test leakage)."""
        self._expiry.clear()


_availability_cache = ModelAvailabilityCache()


def get_availability_cache() -> ModelAvailabilityCache:
    """Return the process-local model availability cache."""
    return _availability_cache


def reset_availability_cache() -> None:
    """Clear the process-local availability cache (for testing)."""
    _availability_cache.reset()


def _normalize_model(model: str | None) -> str | None:
    """Normalize *model* so aliases and native IDs compare (and cache) as one."""
    if model is None:
        return None
    return get_provider().normalize_model_name(model)


def _provider_identity(cfg: ProjectConfig | None) -> str:
    """Return the availability cache's scoping key for the active deployment.

    Combines ``llm.provider`` and ``llm.api_base`` so two projects on the same
    provider type but different deployments (e.g. two Vertex projects) are
    never conflated, and a missing config degrades to a single shared identity
    rather than raising.
    """
    if cfg is None:
        return "unknown:"
    return f"{cfg.llm.provider}:{cfg.llm.api_base}"


def _build_candidate_chain(primary: str | None, cfg: ProjectConfig | None) -> list[str | None]:
    """Return the ordered chain: *primary* followed by ``agent.fallback_models``.

    De-duplication runs on the normalized name so an alias cannot repeat the
    model it resolves to, matching ``WorkflowEngine._advance_fallback``'s
    skip-duplicates behavior. A ``None`` primary means "provider default": it is
    never normalized and never compared against a named model. Candidates are
    appended unnormalized so what reaches the provider is exactly what config
    asked for.
    """
    chain: list[str | None] = [primary]
    if cfg is None:
        return chain

    seen = {_normalize_model(primary)} if primary else set()
    for candidate in cfg.agent.fallback_models:
        if not candidate:
            continue
        normalized = _normalize_model(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        chain.append(candidate)
    return chain


def _drop_unavailable(chain: list[str | None], identity: str) -> list[str | None]:
    """Filter cached-unavailable candidates, keeping the last one as a fail-open."""
    cache = get_availability_cache()
    kept = [model for model in chain if not cache.is_unavailable(identity, _normalize_model(model))]
    return kept or chain[-1:]


async def _invoke_with_fallback(
    attempt: _AttemptFn,
    *,
    primary: str | None,
    cfg: ProjectConfig | None,
    caller_fallback: str | None,
    timeout: float,
    max_budget_usd: Decimal | None = None,
) -> LLMResult:
    """Walk the model chain until an attempt succeeds, sharing one deadline and budget.

    A single-candidate chain (the default, empty ``agent.fallback_models``)
    delegates straight through with the timeout, *max_budget_usd*, and the
    caller-supplied *caller_fallback* unchanged, so the no-fallback path stays
    identical to a direct provider call.

    Multi-candidate chains share one deadline of ``timeout *
    _DEADLINE_SAFETY_MARGIN`` and give each attempt ``remaining /
    candidates_left`` (floored at ``_MIN_ATTEMPT_SECONDS``, capped at what is
    left), so a hanging first attempt cannot consume the whole budget and a
    fast-failing one leaves nearly the full window for the next. Each attempt
    also receives the chain's next hop as its provider-level fallback, so the
    CLI's inner fallback agrees with this one.

    A caller-supplied *max_budget_usd* is divided the same way: each attempt is
    allocated ``budget_remaining / candidates_left`` and that allocation is
    deducted from ``budget_remaining`` regardless of what the attempt actually
    spent (failed attempts report no cost), so the worst case across the whole
    chain still sums to *max_budget_usd* instead of granting every candidate a
    fresh ceiling.

    Only errors accepted by ``is_fallback_eligible`` advance the chain; anything
    else re-raises immediately. Eligibility is category-based, not type-based,
    so the bare ``RuntimeError`` every provider still raises is classified from
    its message rather than dropping straight through. Exhaustion re-raises the
    last eligible error.
    """
    identity = _provider_identity(cfg)
    chain = _drop_unavailable(_build_candidate_chain(primary, cfg), identity)
    if len(chain) == 1:
        return await attempt(chain[0], caller_fallback, timeout, max_budget_usd)

    deadline = time.monotonic() + timeout * _DEADLINE_SAFETY_MARGIN
    budget_remaining = max_budget_usd
    last_error: Exception | None = None

    for index, model in enumerate(chain):
        remaining = deadline - time.monotonic()
        if last_error is not None and remaining < _MIN_ATTEMPT_SECONDS:
            log.warning("llm.fallback.budget_exhausted", model=model, remaining_s=round(remaining, 1))
            break

        candidates_left = len(chain) - index
        attempt_timeout = min(remaining, max(remaining / candidates_left, _MIN_ATTEMPT_SECONDS))
        attempt_budget = budget_remaining / candidates_left if budget_remaining is not None else None
        next_hop = chain[index + 1] if index + 1 < len(chain) else None

        try:
            return await attempt(model, next_hop, attempt_timeout, attempt_budget)
        except Exception as exc:
            if not is_fallback_eligible(exc):
                raise
            # Providers still raise bare RuntimeError, so eligibility and the
            # unavailable-model signal both come from the resolved category,
            # never from the raised type alone.
            category = resolve_error_category(exc)
            if category is ModelUnavailableError:
                get_availability_cache().mark_unavailable(identity, _normalize_model(model))
            if attempt_budget is not None:
                budget_remaining = budget_remaining - attempt_budget
            last_error = exc
            log.warning(
                "llm.fallback.advance",
                from_model=model,
                to_model=next_hop,
                error_type=type(exc).__name__,
                category=category.__name__,
                error=str(exc)[:200],
                exc_info=True,
            )

    if last_error is None:  # pragma: no cover (the loop always returns or sets it)
        raise LLMInvocationError("Model fallback chain produced no attempt")
    log.error("llm.fallback.exhausted", chain=[str(m) for m in chain], error=str(last_error)[:200])
    raise last_error


async def invoke(
    prompt: str,
    *,
    model: str | None = None,
    fallback_model: str | None = None,
    task_type: str | None = None,
    cwd: Path | str | None = None,
    max_budget_usd: Decimal | None = None,
    timeout: float | None = None,
    system_prompt: str | None = None,
    max_tokens: int | None = None,
) -> LLMResult:
    """Run a prompt via the active LLM provider.

    Args:
        task_type: Routing category (e.g. "triage", "harden", "planner").
            When set and *model* is ``None``, looks up ``llm.routing[task_type]``
            to select a model. Ignored when *model* is explicitly provided.
            Requires ``provider = "litellm"`` or ``"hybrid"``.
        system_prompt: Optional system prompt for the LLM call.
        max_tokens: Optional max output tokens (provider-dependent).
    """
    from sova.llm.guard import guard_prompt

    guard_prompt(prompt)
    original_prompt = prompt
    # Loaded unconditionally, and before compression: the fallback chain lives
    # in agent.fallback_models, so it is needed even when both model and
    # timeout are supplied, and passing it into maybe_compress avoids loading
    # config twice per call (it would otherwise reload internally).
    cfg = _try_load_config(cwd)
    prompt = maybe_compress(prompt, cwd, cfg=cfg)
    resolved = _resolve_task_type_model(model, task_type, cfg=cfg)
    resolved_timeout = _resolve_timeout(timeout, cfg=cfg)

    async def _attempt(
        candidate: str | None, next_hop: str | None, attempt_timeout: float, attempt_budget: Decimal | None
    ) -> LLMResult:
        return await get_provider().invoke(
            prompt,
            model=candidate,
            fallback_model=next_hop,
            cwd=cwd,
            max_budget_usd=attempt_budget,
            timeout=attempt_timeout,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
        )

    result = await _invoke_with_fallback(
        _attempt,
        primary=resolved,
        cfg=cfg,
        caller_fallback=fallback_model,
        timeout=resolved_timeout,
        max_budget_usd=max_budget_usd,
    )
    # Runs once, against the winning attempt: failed attempts raise before
    # producing a result, so nothing is double-counted.
    _record_compression_savings(result, original_prompt, prompt)
    return result


def _record_compression_savings(result: LLMResult, original: str, compressed: str) -> None:
    """Estimate and record compression savings on *result*.

    ``maybe_compress`` returns the exact same string object on every passthrough
    path (compression disabled, unavailable, below ``min_chars``, or Headroom
    error), so an identity check reliably detects "compression did not run" and
    leaves both columns NULL. When compression ran, ``tokens_saved`` is estimated
    from the character delta (~4 chars/token) and clamped at 0 for net-zero or
    expanded payloads.
    """
    if compressed is original:
        return
    result.tokens_saved = max(0, (len(original) - len(compressed)) // 4)
    result.pre_compression_input_tokens = result.input_tokens + result.tokens_saved


def _try_load_config(cwd: Path | str | None = None) -> ProjectConfig | None:
    """Load project config, returning None on failure."""
    try:
        from sova.config.loader import load_config

        return load_config(Path(cwd) if cwd else None)
    except Exception:
        log.debug("llm.config_load_failed", exc_info=True)
        return None


def _resolve_task_type_model(
    model: str | None,
    task_type: str | None,
    *,
    cfg: ProjectConfig | None = None,
    cwd: Path | str | None = None,
) -> str | None:
    """Resolve model from task_type routing if no explicit model is provided."""
    if model or not task_type:
        return model

    resolved_cfg = cfg if cfg is not None else _try_load_config(cwd)
    if resolved_cfg is None:
        return model

    if not resolved_cfg.llm.routing:
        return model

    override = resolved_cfg.llm.routing.get(task_type)
    if override is not None:
        log.info("llm.task_type_route", task_type=task_type, model=override)
        return override

    return model


def _resolve_timeout(
    timeout: float | None,
    *,
    cfg: ProjectConfig | None = None,
    cwd: Path | str | None = None,
) -> float:
    """Resolve timeout from config when None, with hardcoded fallback."""
    if timeout is not None:
        return timeout

    resolved_cfg = cfg if cfg is not None else _try_load_config(cwd)
    if resolved_cfg is None:
        return 900.0

    return float(resolved_cfg.llm.cli_timeout)


_DIFF_PREFIXES = ("diff --git", "--- ", "+++ ", "@@ ")
_CODE_PREFIXES = ("def ", "class ", "import ", "from ", "function ", "const ", "public ", "package ", "#include")


def classify_content_type(text: str) -> str:
    """Return a fast compression strategy hint from the payload prefix.

    Only the first 100 characters are inspected so classification stays cheap on
    large payloads. Unrecognized content falls back to "text".

    Design tradeoffs:
    - Code prefixes like 'from ' may match natural language ('from the perspective
      of...'), optimizing for precision over recall. Ambiguous cases fall back to
      'text', which is acceptable for a fast heuristic.
    - Prompts with >100 leading spaces may be misclassified as 'text' after
      slice+strip produces empty string. This pathological edge case is acceptable
      given the function's goal of fast classification.
    """
    head = text[:100].lstrip()
    if head.startswith(_DIFF_PREFIXES):
        return "diff"
    if head.startswith(("{", "[")):
        return "json"
    if head.startswith(_CODE_PREFIXES):
        return "code"
    return "text"


_CFG_UNSET = object()


def maybe_compress(
    prompt: str,
    cwd: Path | str | None = None,
    *,
    cfg: ProjectConfig | None = _CFG_UNSET,  # type: ignore[assignment]
) -> str:
    """Compress *prompt* via Headroom when compression is enabled.

    Gated on ``compression.enabled`` so the optional ``headroom-ai`` import path
    is never touched when disabled. Returns the prompt unchanged on any failure,
    so compression can never break the LLM call path.

    *cfg* lets a caller that already loaded config (e.g. ``invoke()``) pass it
    straight through instead of triggering a second ``load_config()`` round
    trip. The sentinel default (rather than ``None``) distinguishes "caller
    didn't load config, load it here" from "caller loaded it and got None
    (load failure)", so a failed load is never retried pointlessly.
    """
    if cfg is _CFG_UNSET:
        cfg = _try_load_config(cwd)
    if cfg is None or not cfg.compression.enabled:
        return prompt

    try:
        from sova.llm.compression import compress

        return compress(prompt, content_type=classify_content_type(prompt), cwd=cwd)
    except Exception:
        log.warning("llm.compression_failed", exc_info=True)
        return prompt


async def invoke_command(
    command: str,
    args: str = "",
    *,
    model: str | None = None,
    fallback_model: str | None = None,
    cwd: Path | str | None = None,
    max_budget_usd: Decimal | None = None,
    timeout: float | None = None,
) -> LLMResult:
    """Run a slash command via the active LLM provider."""
    # Loaded before compression so args is compressed with the same cfg used
    # for timeout/chain resolution below, instead of loading config twice.
    cfg = _try_load_config(cwd)
    if args:
        from sova.llm.guard import guard_prompt

        assembled = f"{command} {args}".strip()
        guard_prompt(assembled)
        args = maybe_compress(args, cwd, cfg=cfg)
    resolved_timeout = _resolve_timeout(timeout, cfg=cfg)

    async def _attempt(
        candidate: str | None, next_hop: str | None, attempt_timeout: float, attempt_budget: Decimal | None
    ) -> LLMResult:
        return await get_provider().invoke_command(
            command,
            args,
            model=candidate,
            fallback_model=next_hop,
            cwd=cwd,
            max_budget_usd=attempt_budget,
            timeout=attempt_timeout,
        )

    # The outer timeout stays a hard backstop; the loop's own deadline is a
    # safety margin below it, so this never fires before the chain is walked.
    async with asyncio.timeout(resolved_timeout):
        return await _invoke_with_fallback(
            _attempt,
            primary=model,
            cfg=cfg,
            caller_fallback=fallback_model,
            timeout=resolved_timeout,
            max_budget_usd=max_budget_usd,
        )


async def invoke_batch(
    requests: list[BatchRequest],
    *,
    poll_interval: int = 60,
    timeout: int = 86400,
    gcs_bucket: str = "",
    gcs_prefix: str = "sova-batch",
    cwd: Path | str | None = None,
) -> list[BatchResult]:
    """Submit a batch of prompts. Uses a dedicated batch provider if available,
    otherwise falls back to the global provider's sequential default."""
    if not requests:
        return []

    import dataclasses

    from sova.llm.guard import guard_prompt

    compressed_requests: list[BatchRequest] = []
    for req in requests:
        guard_prompt(req.prompt)
        compressed_requests.append(dataclasses.replace(req, prompt=maybe_compress(req.prompt, cwd)))
    requests = compressed_requests

    from sova.llm.providers.anthropic_batch import create_batch_provider

    batch_provider = create_batch_provider(gcs_bucket=gcs_bucket, gcs_prefix=gcs_prefix)
    if batch_provider is not None:
        return await batch_provider.invoke_batch(requests, poll_interval=poll_interval, timeout=timeout)

    return await get_provider().invoke_batch(requests, poll_interval=poll_interval, timeout=timeout)


async def invoke_streaming(
    prompt: str,
    *,
    model: str | None = None,
    task_type: str | None = None,
    cwd: Path | str | None = None,
    max_budget_usd: Decimal | None = None,
) -> AsyncIterator[StreamEvent]:
    """Stream output from the active LLM provider."""
    from sova.llm.guard import guard_prompt

    guard_prompt(prompt)
    prompt = maybe_compress(prompt, cwd)
    resolved = _resolve_task_type_model(model, task_type, cwd=cwd)
    async for event in get_provider().invoke_streaming(prompt, model=resolved, cwd=cwd, max_budget_usd=max_budget_usd):
        yield event


_ROLE_MODEL_FIELDS: dict[str, str] = {
    "researcher": "researcher_model",
    "triage": "triage_model",
}


def resolve_model(
    role: str,
    roles_config: RolesConfig,
    *,
    complexity: ComplexityTier | None = None,
    llm_config: LLMConfig | None = None,
    agent_model: str | None = None,
) -> tuple[str, str] | None:
    """Resolve the model for a given agent role.

    Priority: role-specific config > complexity-based routing > None.

    Args:
        agent_model: pinned model from ``agent.model`` config. Passed through
            to ``route_model()`` so generic aliases are replaced with the
            pinned version when they share the same family.

    Returns:
        (model_alias, reason) tuple, or None if no model is resolved.
    """
    field_name = _ROLE_MODEL_FIELDS.get(role)
    if field_name:
        value = getattr(roles_config, field_name, None)
        if value:
            return value, f"role:{role}->{value}"

    if complexity is not None:
        from sova.llm.routing import route_model

        return route_model(complexity, llm_config=llm_config, agent_model=agent_model)

    return None
