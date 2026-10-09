"""Models service: read-only LLM model enumeration for the dashboard.

Mirrors ``setup_service.get_auth_status()`` (#933): resolves the project's
configured provider, calls ``list_available_models()``, and caches the
result per project directory so the model dropdown this feeds (#A4, not yet
built) does not re-run ``load_config()`` (a blocking TOML + DB read) and
``create_provider()`` on every poll.

Two independent caches are involved, and both stay: the provider's own
``cached_enumeration()`` (``sova.llm.client``, 1800s TTL) prevents duplicate
probes/API calls across every caller of ``list_available_models()``, while
this module's cache additionally avoids the config-load/provider-construction
work on top of that for this one read path.

A result is only cached here when it carries a real answer: at least one
model, and an aggregated ``source`` other than ``"curated"``.
``CURATED_MODELS`` is the universal "enumeration did not produce a real
answer" fallback shared by a provider that never overrides enumeration, an
unauthenticated ``ClaudeCodeProvider``, a misconfigured provider, and a
timed-out enumeration attempt alike: caching any of those for the full TTL
would serve a stale fallback for minutes after the underlying condition
(login, config fix, transient slowness) resolved, defeating the "next poll
retries" contract ``cached_enumeration()`` already honors one layer down.
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

from sova.llm.models import ModelInfo
from sova.utils.logging import get_logger

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


def _build_result(provider_identity: str, models: list[ModelInfo], *, layer: str, detail: str = "") -> dict:
    """Build a freshly enumerated payload.

    ``cached`` is part of the stored value so a cache read only has to
    override it (``{**cached, "cached": True}``) and every fresh path can
    return the result as-is. ``layer`` is echoed back because the endpoint
    contracts one shape across both the LLM and (Phase B) runtime axes, so a
    client holding a payload must be able to tell which axis produced it.
    """
    return {
        "layer": layer,
        "provider_identity": provider_identity,
        "models": [_serialize_model(m) for m in models],
        "source": _aggregate_source(models),
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

    Cached per project directory for ``_MODELS_CACHE_TTL`` seconds, except
    when the result carries no real answer (see module docstring).
    ``refresh=True`` drops the provider-level enumeration cache first, forcing
    a fresh pass, and is rate-limited per project via
    ``ModelsRefreshRateLimitedError``.

    The cache key is the project directory alone: only ``layer="llm"`` is
    implemented today (the router rejects ``runtime`` before reaching here),
    so a second layer must extend the key before it can be served.
    """
    from sova.config.loader import load_config
    from sova.llm.backends import detect_backend
    from sova.llm.client import clear_enumeration
    from sova.llm.models import CURATED_MODELS
    from sova.llm.provider import create_provider

    cache_key = str(project_dir.resolve())
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
        provider_identity = f"{cfg.llm.provider}:{detect_backend(cfg.llm)}"

        try:
            provider = create_provider(cfg.llm, project_dir)
        except (ValueError, ImportError) as exc:
            # A misconfigured/unknown provider type, or a configured provider
            # whose optional SDK extra (litellm, anthropic) isn't installed,
            # is a routine reportable state, mirroring
            # setup_service.get_auth_status() and
            # doctor.py:_check_llm_provider, not a genuine service failure.
            # The refresh allowance is deliberately not spent here (see
            # below): no refresh happened, so a retry should not be
            # throttled.
            return _build_result(provider_identity, list(CURATED_MODELS), layer=layer, detail=str(exc))

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
                layer=layer,
                detail="Enumeration timed out; showing curated fallback",
            )

        result = _build_result(provider_identity, models, layer=layer)
        if models and result["source"] != "curated":
            # Cache its own dict: the caller gets the original, and the read
            # path's ``{**cached, "cached": True}`` is likewise a copy, so no
            # caller holds a reference into the cached entry.
            _models_cache[cache_key] = (time.monotonic(), dict(result))
        return result
