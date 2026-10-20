"""Models service: read-only model enumeration for the dashboard.

Mirrors ``setup_service.get_auth_status()`` (#933): resolves the project's
configured provider (``layer="llm"``) or agent runtime (``layer="runtime"``),
calls its ``list_available_models()``, and caches the result per layer and
project directory so the model dropdowns this feeds do not re-run
``load_config()`` (a blocking TOML + DB read) and ``create_provider()`` on
every poll.

Two independent caches are involved, and both stay: the provider's own
``cached_enumeration()`` (``sova.llm.client``, 1800s TTL) prevents duplicate
probes/API calls across every caller of ``list_available_models()``, while
this module's cache additionally avoids the config-load/provider-construction
work on top of that for this one read path.

On the llm layer, a result is only cached here when it carries a real
answer: at least one model, and an aggregated ``source`` other than
``"curated"``.
``CURATED_MODELS`` is the universal "enumeration did not produce a real
answer" fallback shared by a provider that never overrides enumeration, an
unauthenticated ``ClaudeCodeProvider``, a misconfigured provider, and a
timed-out enumeration attempt alike: caching any of those for the full TTL
would serve a stale fallback for minutes after the underlying condition
(login, config fix, transient slowness) resolved, defeating the "next poll
retries" contract ``cached_enumeration()`` already honors one layer down.
(The runtime layer has its own rule, see ``_resolve_runtime_models()``: it
caches every reachable answer, an empty one included, and never an
``"unavailable"`` one.)
An empty list is excluded for the same reason rather than on its
``"unknown"`` source alone: every provider shipped here reports an outage as
``None`` (which ``cached_enumeration()`` turns into the curated fallback) and
never as ``[]``, so a zero-model result reaching this layer is a shape no
in-tree provider produces, and pinning it for ten minutes would report "no
models available" for a condition nothing here can vouch is stable.

``clear_enumeration()`` (used by ``?refresh=1``) does not clear per-model
probe outcomes: a model that previously worked stays cached as working for
the remainder of the provider-level enumeration TTL, while a failing probe
still expires on its own short reactive TTL. A refresh therefore forces a
fresh enumeration *pass* but may still reuse known-good per-model probe
answers gathered during an earlier pass; this is intentional.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from sova.llm.models import ModelInfo
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.config.models import ProjectConfig

log = get_logger(component="dashboard.models")

# 10 minutes: long enough that a dropdown polling periodically doesn't pay
# config-load/provider-construction cost every time, short enough that a
# newly authenticated account or a config fix is visible without a restart.
_MODELS_CACHE_TTL = 600.0

# Rate limit for ?refresh=1, independent of and shorter than the cache TTL
# above: a refresh is an explicit forced re-enumeration, not a cache read, so
# it gets its own, much tighter, per-project throttle.
_REFRESH_MIN_INTERVAL = 60.0

# Bounds a single enumeration call (e.g. ClaudeCodeProvider's curated-model
# probe loop, which shells out per candidate). On expiry, the curated list is
# served and nothing is cached, so the next call retries.
#
# Must stay above the largest single step a provider can take internally, or
# the retry contract above silently becomes a starvation loop. ClaudeCodeProvider
# runs one auth probe (_AUTH_CHECK_TIMEOUT, 15s) and then probes each curated
# candidate sequentially (_MODEL_PROBE_TIMEOUT, 20s each), caching each outcome
# only *after* that candidate's probe returns. A budget below 15+20 lets a single
# slow model consume the whole window, so the cancellation lands mid-probe, no
# outcome is recorded, and every later poll repeats the identical 20s failure
# with the UI pinned on the curated fallback forever. Above that floor each
# attempt is guaranteed to bank at least one probe outcome, so repeated polls
# converge. test_enumeration_timeout_exceeds_claude_code_probe_bounds pins this.
_ENUMERATION_TIMEOUT_SECONDS = 45.0

_models_cache: dict[str, tuple[float, dict]] = {}
# project dir -> single-flight lock, mirroring setup_service's
# _auth_status_locks so concurrent cold requests for the same project share
# one enumeration pass instead of each spawning duplicate provider calls.
_models_locks: dict[str, asyncio.Lock] = {}
# project dir -> monotonic time of the last accepted ?refresh=1 call.
_last_refresh_at: dict[str, float] = {}

# How long a project dir's cache/refresh entries sit untouched before
# _prune_stale_entries() drops them. A large multiple of the longer-lived TTL
# above, not the TTL itself: an entry just past _MODELS_CACHE_TTL is a normal
# "expired, refresh on next read" state, not something to evict. This only
# clears directories nothing has queried in a long while, so a dashboard
# instance that is ever pointed at many distinct project directories (not
# just the handful it serves steadily) does not accumulate one entry per
# directory for the life of the process, the way setup_service's sibling
# _auth_status_cache currently does.
_PRUNE_STALE_AFTER_SECONDS = max(_MODELS_CACHE_TTL, _REFRESH_MIN_INTERVAL) * 10


def _prune_stale_entries(now: float) -> None:
    """Drop per-project cache/lock/refresh entries untouched for a long time.

    Has no ``await`` of its own, so it always runs atomically with respect to
    every other coroutine on the event loop: a lock this function observes as
    free (``not lock.locked()``) cannot be acquired by a waiter mid-prune,
    since a waiter is only ever created at an ``await`` point and none occurs
    here. Called at the top of every request rather than on a timer, so it
    costs nothing when the project count stays small (the common case) and
    self-heals if it does not.
    """
    for key in [k for k, (cached_at, _) in _models_cache.items() if now - cached_at > _PRUNE_STALE_AFTER_SECONDS]:
        del _models_cache[key]
    for key in [k for k, last in _last_refresh_at.items() if now - last > _PRUNE_STALE_AFTER_SECONDS]:
        del _last_refresh_at[key]
    for key in [
        k
        for k in _models_locks
        if k not in _models_cache and k not in _last_refresh_at and not _models_locks[k].locked()
    ]:
        del _models_locks[key]


class ModelsRefreshRateLimitedError(Exception):
    """Raised when ``?refresh=1`` is requested again inside the throttle window."""

    def __init__(self, retry_after_seconds: float) -> None:
        self.retry_after_seconds = max(1, round(retry_after_seconds))
        super().__init__(f"Refresh rate-limited; retry after {self.retry_after_seconds}s")


def _serialize_model(model: ModelInfo) -> dict:
    """Project a ModelInfo onto the fields this endpoint contracts to expose.

    An explicit literal rather than a dataclass dump: ``source`` is aggregated
    into one top-level field instead of repeated per entry (see
    ``_aggregate_source``), and a field a future ModelInfo adds should not
    reach the response until it is deliberately added here.
    """
    return {
        "id": model.id,
        "family": str(model.family),
        "display_name": model.display_name,
        "tier": model.tier,
    }


def _aggregate_source(models: list[ModelInfo]) -> str:
    """Collapse every entry's ``source`` into one top-level value.

    The distinct value when every entry agrees, ``"mixed"`` when they
    disagree (e.g. a provider that enumerates some backends but falls back to
    curated for others), ``"unknown"`` for a genuinely empty list. An empty
    list is a legitimate answer (a deployment granting nothing), not an
    error, but one with no per-entry source to report.
    """
    sources = {model.source for model in models}
    if not sources:
        return "unknown"
    if len(sources) == 1:
        return next(iter(sources))
    return "mixed"


def _build_result(
    provider_identity: str, models: list[ModelInfo], *, layer: str, detail: str = "", source: str | None = None
) -> dict:
    """Build a freshly enumerated payload.

    ``cached`` is part of the stored value so a cache read only has to
    override it (``{**cached, "cached": True}``) and every fresh path can
    return the result as-is. ``layer`` is echoed back because the endpoint
    contracts one shape across both the LLM and runtime axes, so a client
    holding a payload must be able to tell which axis produced it.

    ``source`` defaults to the per-model aggregation (``_aggregate_source``),
    which collapses to ``"unknown"`` for an empty model list. The runtime
    layer needs an explicit override for an empty list: a reachable runtime
    with nothing to offer (``"reachable"``) and an unreachable one
    (``"unavailable"``) both carry ``models=[]``, and those are not the same
    condition for the UI's fail-open check.
    """
    return {
        "layer": layer,
        "provider_identity": provider_identity,
        "models": [_serialize_model(m) for m in models],
        "source": source if source is not None else _aggregate_source(models),
        "detail": detail,
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "cached": False,
    }


def _check_refresh_rate_limit(cache_key: str, now: float) -> None:
    last_refresh = _last_refresh_at.get(cache_key)
    if last_refresh is not None and (now - last_refresh) < _REFRESH_MIN_INTERVAL:
        raise ModelsRefreshRateLimitedError(_REFRESH_MIN_INTERVAL - (now - last_refresh))


async def get_available_models(project_dir: Path, *, layer: str = "llm", refresh: bool = False) -> dict:
    """Assemble the read-only available-models payload for the dashboard.

    Cached per layer and project directory for ``_MODELS_CACHE_TTL`` seconds,
    except when the result carries no real answer (see module docstring).
    ``refresh=True`` drops the provider-level enumeration cache first, forcing
    a fresh pass, and is rate-limited per project via
    ``ModelsRefreshRateLimitedError``.

    The cache key is ``f"{layer}:{project_dir.resolve()}"``: ``layer="llm"``
    and ``layer="runtime"`` for the same project must not collide or share a
    refresh allowance, since they enumerate independent credentials.
    """
    from sova.config.loader import load_config

    cache_key = f"{layer}:{project_dir.resolve()}"
    now = time.monotonic()
    _prune_stale_entries(now)

    if not refresh:
        cached = _models_cache.get(cache_key)
        if cached and (now - cached[0]) < _MODELS_CACHE_TTL:
            return {**cached[1], "cached": True}
    else:
        _check_refresh_rate_limit(cache_key, now)

    lock = _models_locks.setdefault(cache_key, asyncio.Lock())
    async with lock:
        # Recheck after acquiring: another task may have refreshed/populated
        # the cache, or consumed the refresh allowance, while we waited.
        now = time.monotonic()
        if not refresh:
            cached = _models_cache.get(cache_key)
            if cached and (now - cached[0]) < _MODELS_CACHE_TTL:
                return {**cached[1], "cached": True}
        else:
            _check_refresh_rate_limit(cache_key, now)

        cfg = await asyncio.to_thread(load_config, project_dir)

        if layer == "runtime":
            return await _resolve_runtime_models(cfg, refresh=refresh, cache_key=cache_key, now=now)
        return await _resolve_llm_models(cfg, project_dir, refresh=refresh, cache_key=cache_key, now=now)


async def _resolve_llm_models(
    cfg: ProjectConfig, project_dir: Path, *, refresh: bool, cache_key: str, now: float
) -> dict:
    """Enumerate layer-2 (LLM provider) models. Runs with the per-project lock already held."""
    from sova.llm.backends import detect_backend
    from sova.llm.client import clear_enumeration
    from sova.llm.models import CURATED_MODELS
    from sova.llm.provider import create_provider

    provider_identity = f"{cfg.llm.provider}:{detect_backend(cfg.llm)}"

    try:
        provider = create_provider(cfg.llm, project_dir)
    except (ValueError, ImportError) as exc:
        # A misconfigured/unknown provider type, or a configured provider
        # whose optional SDK extra (litellm, anthropic) isn't installed,
        # is a routine reportable state, mirroring
        # setup_service.get_auth_status() and
        # diagnostics.py:_check_llm_provider, not a genuine service failure.
        # The refresh allowance is deliberately not spent here (see
        # below): no refresh happened, so a retry should not be
        # throttled.
        return _build_result(provider_identity, list(CURATED_MODELS), layer="llm", detail=str(exc))

    if refresh:
        # The refresh allowance is consumed here, only once the config
        # load and provider construction have actually succeeded and a
        # refresh is really about to happen: spending it any earlier
        # (e.g. right after the rate-limit check) would throttle a retry
        # for the full window even though load_config/create_provider
        # failed and no refresh occurred.
        _last_refresh_at[cache_key] = now
        # Also deliberately after the config load and provider
        # construction, and inside the lock: this wipes every identity's
        # entry process-wide (see client.clear_enumeration), so a
        # request that was going to fail before it could enumerate
        # anything must not pay that cost for every other project
        # sharing the process.
        clear_enumeration()

    try:
        # On timeout, asyncio.wait_for cancels the awaited coroutine, and
        # that cancellation is delivered wherever it is currently
        # suspended. For ClaudeCodeProvider that is always inside
        # run()'s `await proc.communicate()` (sova/utils/shell.py,
        # _probe_model() in providers/claude_code.py has no subprocess
        # handling of its own), and run() already guarantees
        # kill-then-reap before re-raising on asyncio.CancelledError
        # (sova/utils/shell.py's `except asyncio.CancelledError` branch
        # in run()), pinned by
        # tests/test_shell_timeout.py::test_run_kills_process_on_cancelled_error.
        # No orphaned subprocess is possible from this timeout.
        models = await asyncio.wait_for(
            provider.list_available_models(allow_probe=True),
            timeout=_ENUMERATION_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        log.warning("models.enumeration_timeout", provider_identity=provider_identity)
        return _build_result(
            provider_identity,
            list(CURATED_MODELS),
            layer="llm",
            detail="Enumeration timed out; showing curated fallback",
        )

    result = _build_result(provider_identity, models, layer="llm")
    if models and result["source"] != "curated":
        # Cache its own dict: the caller gets the original, and the read
        # path's ``{**cached, "cached": True}`` is likewise a copy, so no
        # caller holds a reference into the cached entry.
        _models_cache[cache_key] = (time.monotonic(), dict(result))
    return result


def _runtime_unavailable(provider_identity: str, detail: str, *, honours_model_override: bool | None = None) -> dict:
    """Report a runtime that could not be enumerated.

    There is no universal cross-runtime curated fallback to fall open to: a
    runtime's model ID shape is specific to that runtime, so an unreachable
    one is reported as an empty list with an explicit ``"unavailable"``
    source rather than a borrowed catalog.

    ``honours_model_override`` is ``None`` only when there is no runtime
    instance to ask (an unknown runtime type): see
    ``_resolve_runtime_models()`` on why that key is always present on this
    layer's payloads.
    """
    result = _build_result(provider_identity, [], layer="runtime", detail=detail, source="unavailable")
    result["honours_model_override"] = honours_model_override
    return result


async def _resolve_runtime_models(cfg: ProjectConfig, *, refresh: bool, cache_key: str, now: float) -> dict:
    """Enumerate layer-1 (agent runtime) models. Runs with the per-project lock already held.

    Every failure path reports through ``_runtime_unavailable()``; see its
    docstring for why there is no curated fallback on this axis.

    Payloads on this layer carry one extra key the llm layer has no analogue
    for: ``honours_model_override``, mirrored straight from
    ``AgentRuntime.honours_model_override``. A runtime that ignores a
    caller-supplied model (Codex) must render no model selector at all, and
    that is a capability of the runtime, not something a client can derive
    from a catalog; reporting it here keeps the single source of truth on the
    runtime class instead of a runtime name repeated in each template.

    The runtime is constructed per call (``create_runtime``, deliberately not
    the ``get_runtime()`` process singleton, which is bound to whichever
    project configured it at startup rather than ``project_dir``), so
    ``AgentRuntime``'s own per-instance ``_RuntimeModelCache`` never carries
    across requests for this caller: an "unavailable" answer is therefore
    re-probed on the next request rather than pinned, which is the same
    retry-soon contract the llm branch gets by not caching its curated
    fallback.
    """
    from sova.ipc.runtime import create_runtime
    from sova.llm.models import CURATED_MODELS, classify_model_family, model_tier_for_id

    provider_identity = cfg.agent.runtime

    try:
        runtime = create_runtime(cfg.agent.runtime, codex=cfg.codex)
    except ValueError as exc:
        # Unknown runtime type: a routine reportable misconfiguration, not a
        # service failure. The refresh allowance is not spent (mirrors the
        # llm branch's create_provider failure path).
        return _runtime_unavailable(provider_identity, str(exc))

    if refresh:
        _last_refresh_at[cache_key] = now

    try:
        model_ids = await asyncio.wait_for(runtime.list_available_models(), timeout=_ENUMERATION_TIMEOUT_SECONDS)
    except TimeoutError:
        log.warning("models.enumeration_timeout", provider_identity=provider_identity)
        return _runtime_unavailable(
            provider_identity, "Enumeration timed out", honours_model_override=runtime.honours_model_override
        )

    if model_ids is None:
        # CLI missing or unauthenticated: distinct from a reachable runtime
        # that simply has no catalog of its own (see module/AgentRuntime
        # docstrings on None vs []).
        return _runtime_unavailable(
            provider_identity,
            "Runtime unavailable or unauthenticated",
            honours_model_override=runtime.honours_model_override,
        )

    # A runtime reports bare IDs. Reusing the curated display name for an ID
    # SOVA already names keeps one model from reading as "claude-opus-5" in
    # this dropdown and "Claude Opus 5" in the llm-layer one; an ID with no
    # curated entry keeps itself as its own label. source "runtime" says the
    # runtime reported the entry, not that it was probed (ClaudeCodeRuntime
    # reports its list without probing) and not "curated", which every model
    # dropdown treats as a fallback rather than an answer.
    curated_display_names = {m.id: m.display_name for m in CURATED_MODELS}
    models = [
        ModelInfo(
            id=model_id,
            family=classify_model_family(model_id),
            tier=model_tier_for_id(model_id),
            display_name=curated_display_names.get(model_id, model_id),
            source="runtime",
        )
        for model_id in model_ids
    ]

    result = _build_result(provider_identity, models, layer="runtime", source=None if models else "reachable")
    result["honours_model_override"] = runtime.honours_model_override
    # [] is a real, cacheable "reachable, nothing to offer" answer here,
    # distinct from the llm branch's curated/empty exclusion: every runtime
    # that reaches this point already passed check_available(), so an empty
    # list is a confirmed answer, not an outage.
    _models_cache[cache_key] = (time.monotonic(), dict(result))
    return result
