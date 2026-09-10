"""LLM client -- thin delegation layer to the active provider.

All existing callers (``from sova.llm.client import invoke``) continue
to work unchanged.  The actual implementation lives in the configured
:class:`~sova.llm.provider.LLMProvider` (default: ClaudeCodeProvider).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
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
from sova.llm.models import BatchRequest, BatchResult, LLMResult, StreamEvent, resolve_model_alias

# Module-level, unlike get_provider()'s lazy per-call ClaudeCodeProvider import:
# reload_provider() is the chokepoint tests patch as sova.llm.client.create_provider,
# and a name bound at import time is what makes that the correct, unambiguous
# patch target rather than sova.llm.provider.create_provider.
from sova.llm.provider import create_provider
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


# Once-per-process dedup for the reports_cost=False warning below. A plain
# module global (not a ContextVar): the warning is about which provider TYPE
# is installed process-wide, not about a single run's context, so it must
# stay suppressed across every reload_provider() call (dashboard settings
# saves, hot-reloads) as long as the provider type hasn't actually changed.
_last_warned_provider_type: str | None = None


def reload_provider(cfg: ProjectConfig) -> None:
    """Recreate the global LLM provider from fresh config.

    Python's GIL ensures the reference swap is atomic. In-flight calls hold
    their own reference to the old provider, which stays alive via refcount.
    """
    global _last_warned_provider_type  # noqa: PLW0603

    # The whole llm section, not a hand-picked subset: forwarding individual
    # kwargs is what let a new field keep applying at startup but silently stop
    # applying after a settings hot-reload (R12).
    provider = create_provider(cfg.llm)
    set_provider(provider)

    # Single chokepoint for the CLI callback, dashboard lifespan, and the
    # "llm" hot-reload dispatch target in settings.py: a provider that can't
    # report real cost makes the dollar-based budget guard blind (R6). Logged
    # once per provider type, not once per call: reload_provider() re-runs on
    # every dashboard settings save and every hot-reload, and a provider type
    # that already warned would otherwise re-log indefinitely. Re-warns only
    # when the provider type actually changes (e.g. litellm -> anthropic ->
    # litellm), including back to a previously-warned type, since that is a
    # genuine new swap the operator should see.
    if not provider.capabilities.reports_cost:
        if cfg.llm.provider != _last_warned_provider_type:
            log.warning(
                "llm.provider_reports_cost_false",
                provider=cfg.llm.provider,
                detail="budget caps (agent.max_budget, agent.max_issue_budget) cannot be enforced "
                "reliably against this provider's reported cost; rely on the wall-clock/step-count/"
                "call-count runaway guard instead",
            )
        _last_warned_provider_type = cfg.llm.provider
    else:
        _last_warned_provider_type = None


def reset_provider_warning_state() -> None:
    """Clear the once-per-process reports_cost warning dedup (for testing)."""
    global _last_warned_provider_type  # noqa: PLW0603
    _last_warned_provider_type = None


# ---------------------------------------------------------------------------
# LLM invocation-count runaway guard (docs/model-selection-risk-assessment.md, R6)
#
# A ContextVar holding a *mutable* counter object, not a plain int: plain-int
# ContextVars are copy-on-inherit into child asyncio tasks, so a mutation made
# inside a child task (e.g. a step's own asyncio.gather sub-tasks) would never
# propagate back to the parent run. A shared mutable object inherited by
# reference propagates correctly, while a separate top-level run that installs
# its own counter object stays isolated (required because the dashboard
# process runs batch triage concurrently with other work).
# ---------------------------------------------------------------------------


@dataclass
class _CallCounter:
    count: int = 0


_call_counter: ContextVar[_CallCounter | None] = ContextVar("_call_counter", default=None)


def start_call_counter() -> None:
    """Install a fresh LLM invocation counter for the current run.

    Called once per run (WorkflowEngine.run(), guarded like its own
    ``_run_started_at`` so a resumed run does not reset the count mid-flight).
    """
    _call_counter.set(_CallCounter())


def get_call_count() -> int:
    """Return the current run's LLM invocation count, or 0 if none installed."""
    counter = _call_counter.get()
    return counter.count if counter is not None else 0


def reset_call_counter() -> None:
    """Uninstall the current run's call counter (for testing)."""
    _call_counter.set(None)


def _increment_call_counter(n: int = 1) -> None:
    """Increment the current run's LLM invocation count by *n*, a no-op if none installed.

    A run outside WorkflowEngine (reviewer/custom roles, which don't call
    start_call_counter()) simply never accumulates a count, which is correct:
    the guard that reads it is only checked from WorkflowEngine's step loop.
    """
    counter = _call_counter.get()
    if counter is not None:
        counter.count += n


def _check_runaway_call_limit(cfg: ProjectConfig | None) -> None:
    """Reject a new invocation once ``runaway.max_llm_calls`` is already reached.

    WorkflowEngine._check_runaway_guard() only checks between steps, so a
    step that issues many invoke() calls inside a single execute() (e.g.
    MonitorCIStep's CI-fix loop, AddressReviewStep's consensus loop) could
    otherwise keep dispatching provider calls past the configured ceiling.
    Checked before the counter increments and before any provider attempt, at
    every one of the four call-counting entry points (invoke, invoke_command,
    invoke_streaming, invoke_batch).

    The message is prefixed identically to _check_runaway_guard's own
    ("Runaway guard: ..."), so once this propagates up through a step's
    execute() and is stringified into StepResult.error, WorkflowEngine's
    _is_runaway_failure() recognizes it and routes the failure through the
    same PAUSED path as every other runaway trip, instead of a generic
    FAILED.

    0 disables the check (matching every other runaway limit); a missing
    config (no project resolved yet) never blocks.
    """
    if cfg is None or not cfg.runaway.max_llm_calls:
        return
    if get_call_count() >= cfg.runaway.max_llm_calls:
        raise RuntimeError(f"Runaway guard: LLM call count exceeded {cfg.runaway.max_llm_calls} calls")


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

    Fallback candidates go through ``select_model`` for the same reason the
    primary does at the call sites: an alias map that resolved only the primary
    would leave every fallback hop sending an unmapped name to the provider.
    The primary arrives already resolved, and a second lookup is a no-op because
    resolution is a single hop.

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
    for entry in cfg.agent.fallback_models:
        if not entry:
            continue
        candidate = select_model(entry, cfg)
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
    _DEADLINE_SAFETY_MARGIN``, and every attempt gets whatever is left of it
    rather than a per-candidate slice. The primary therefore runs with the full
    window the caller configured; a fast failure leaves nearly all of it for the
    next hop, and a primary that consumes the whole deadline legitimately spent
    the caller's time, so no fallback follows. Each attempt also receives the
    chain's next hop as its provider-level fallback, so the CLI's inner fallback
    agrees with this one.

    A caller-supplied *max_budget_usd* is passed to each attempt in full, for
    the same reason: pre-dividing it would silently halve the ceiling the caller
    set. This cannot spend the cap repeatedly, because an exhausted budget
    raises ``BillingError``, which is not fallback-eligible and re-raises before
    another candidate is tried.

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
    last_error: Exception | None = None

    for index, model in enumerate(chain):
        remaining = deadline - time.monotonic()
        if last_error is not None and remaining < _MIN_ATTEMPT_SECONDS:
            log.warning("llm.fallback.budget_exhausted", model=model, remaining_s=round(remaining, 1))
            break

        # Every attempt gets what is left of the shared deadline, so the
        # primary runs with the whole window the caller asked for. Splitting
        # the window across candidates up front instead would hand the model
        # that almost always succeeds a fraction of the configured timeout
        # (half of it with a single fallback entry), turning a slow-but-healthy
        # call into a timeout. A fast failure still leaves nearly the full
        # window for the next hop, and a primary that burns the whole deadline
        # has genuinely spent the caller's budget, so there is nothing left to
        # fall back with, which is the correct outcome, not a lost chance.
        attempt_timeout = remaining
        attempt_budget = max_budget_usd
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
            # A failed attempt reports no cost, so there is nothing to deduct.
            # The chain cannot spend the cap repeatedly: budget exhaustion
            # raises BillingError, which is not fallback-eligible and re-raises
            # above before another candidate is tried.
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
            When set, a configured ``llm.routing[task_type]`` entry selects the
            model, outranking *model*. Under the default empty ``llm.routing``
            no key matches and *model* is used unchanged. Routing to a local
            model (``ollama/*``) additionally requires a provider that can reach
            it (``litellm`` or ``hybrid``).
        system_prompt: Optional system prompt for the LLM call.
        max_tokens: Optional max output tokens (provider-dependent).
    """
    from sova.llm.guard import guard_prompt

    guard_prompt(prompt)
    original_prompt = prompt
    # Loaded unconditionally, and before compression: the fallback chain lives
    # in agent.fallback_models, so it is needed even when both model and
    # timeout are supplied, and passing it into maybe_compress avoids loading
    # config twice per call (it would otherwise reload internally). Also
    # needed before the counter increments, so the runaway call-limit check
    # can reject the invocation before any provider attempt.
    cfg = await _try_load_config_async(cwd)
    _check_runaway_call_limit(cfg)
    _increment_call_counter()
    prompt = maybe_compress(prompt, cwd, cfg=cfg)
    resolved = select_model(_resolve_task_type_model(model, task_type, cfg=cfg), cfg)
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


@lru_cache(maxsize=256)
def _resolve_config_root(start: Path) -> Path:
    """Walk *start* up to the checkout that owns its config.

    Cached because config is loaded once per LLM call and ``_resolve_primary_root``
    shells out to git. The cache is bounded because worktree paths are ephemeral
    (one per issue, removed on cleanup), so an unbounded map would only ever grow.
    Process-local, so a config source created after the first lookup is not
    picked up until restart.
    """
    if (start / "sova.toml").exists() or (start / ".claude" / "sova.db").exists():
        return start

    from sova.llm.provider import _resolve_primary_root

    return _resolve_primary_root(start) or start


def _config_root(cwd: Path | str | None) -> Path | None:
    """Return the checkout that owns *cwd*'s config, or None for the process default.

    A linked git worktree carries no config of its own: ``.claude/sova.db`` is
    gitignored and ``sova.toml`` is frequently untracked. Pipeline steps pass
    ``ctx.working_dir`` (that worktree) as *cwd*, so a lookup scoped to it
    returned bare defaults and silently emptied ``llm.routing``,
    ``agent.fallback_models``, and ``compression`` for every worktree-scoped
    step, which is what kept task-type routing dead in the pipeline.

    A directory that holds its own config source is used as-is, so a project
    installed inside a monorepo subtree keeps resolving to itself. Only a
    directory with neither source resolves upward, which is the worktree case.
    """
    if not cwd:
        return None
    # Resolved before the cache lookup: two spellings of the same directory
    # (relative vs. absolute, or a symlinked worktree path) would otherwise be
    # distinct lru_cache keys, repeating the git shell-out and consuming the
    # bounded cache faster. load_config() resolves its own project_dir anyway,
    # so this is the same normalization the loader would apply regardless.
    return _resolve_config_root(Path(cwd).resolve())


def reset_config_root_cache() -> None:
    """Clear the resolved-config-root cache (for testing)."""
    _resolve_config_root.cache_clear()


def _try_load_config(cwd: Path | str | None = None) -> ProjectConfig | None:
    """Load project config, returning None on failure."""
    try:
        from sova.config.loader import load_config

        return load_config(_config_root(cwd))
    except Exception:
        log.debug("llm.config_load_failed", exc_info=True)
        return None


async def _try_load_config_async(cwd: Path | str | None = None) -> ProjectConfig | None:
    """Async-safe wrapper for ``_try_load_config``.

    ``_config_root`` may shell out to git (``_resolve_primary_root``, a
    blocking ``subprocess.run``) on a worktree cwd with no config of its own,
    which every pipeline step now passes. Run on the event loop, that call
    would stall it (and any concurrent cancellation) for up to its 5s
    timeout. Offloaded to a worker thread so the four ``invoke*`` entry
    points never block on it directly.
    """
    return await asyncio.to_thread(_try_load_config, cwd)


# Sentinel default for the *cfg* keyword: distinguishes "caller did not load config,
# load it here" from "caller loaded it and got None", so a failed load is never
# retried against the process cwd (a different project under the dashboard server).
_CFG_UNSET = object()


def _resolve_task_type_model(
    model: str | None,
    task_type: str | None,
    *,
    cfg: ProjectConfig | None = _CFG_UNSET,  # type: ignore[assignment]
    cwd: Path | str | None = None,
) -> str | None:
    """Resolve *model* from ``llm.routing[task_type]`` when a route is configured.

    A configured route outranks *model*. It has to: every pipeline step passes
    ``model=ctx.resolved_model or ctx.config.agent.model``, so a route that lost
    to an explicit model could never fire. Under the default empty
    ``llm.routing`` no key matches and *model* is returned untouched, which keeps
    resolution identical to having no routing at all.

    A matched route is pinned to ``agent.model`` when the two share a model
    family, mirroring ``route_model()`` so a bare alias is never handed to the
    CLI to resolve into a version the deployment may not have. Routes to
    non-family models (``ollama/*`` and other third-party IDs) are returned
    verbatim, so local-model offloading is unaffected.
    """
    if not task_type:
        return model

    resolved_cfg = _try_load_config(cwd) if cfg is _CFG_UNSET else cfg
    if resolved_cfg is None:
        return model

    from sova.llm.routing import route_task_type

    routed = route_task_type(task_type, llm_config=resolved_cfg.llm, agent_model=resolved_cfg.agent.model)
    if routed is None:
        return model

    routed_model, reason = routed
    log.info("llm.task_type_route", task_type=task_type, model=routed_model, reason=reason)
    return routed_model


def resolve_alias(model: str, aliases: dict[str, str]) -> str:
    """Resolve *model* through *aliases*, the raw ``llm.model_aliases`` map.

    Shared by ``select_model`` (which holds a full ``ProjectConfig``) and
    ``create_provider`` (which only ever holds the ``llm`` section, so it
    cannot call ``select_model`` directly). Lookup is a single hop: an alias
    whose target is itself an alias key is not chased, which keeps a
    self-referential map from looping. An unmapped name, or a name mapped to
    itself, passes through unchanged.
    """
    resolved = aliases.get(model)
    if resolved is None or resolved == model:
        return model

    log.info("llm.model_alias", alias=model, model=resolved)
    return resolved


def select_model(model: str | None, cfg: ProjectConfig | None) -> str | None:
    """Resolve *model* through the client-side ``llm.model_aliases`` map.

    Alias resolution is client-side on purpose. ``normalize_model_name`` is
    provider-owned and a no-op on the default claude-code path, so it cannot
    serve as the alias layer (docs/model-selection-architecture.md, Q3).

    Takes the whole project config, like its sibling resolvers
    (``_resolve_task_type_model``, ``_resolve_timeout``): every call site
    already holds one, and the resolution paths folded in below read outside
    the ``llm`` section.

    A ``None`` model means "provider default" and is never aliased; an unmapped
    name passes through unchanged, so the default empty map reproduces today's
    resolution exactly.

    Scope: today this applies the alias map only. PR4 (#913) folds task-type,
    role and complexity resolution into this same function to make it the single
    precedence choke point the architecture doc describes; until then the other
    resolution paths stay where they are (``_resolve_task_type_model`` here,
    ``resolve_model``/``route_model`` in sova/llm/routing.py).
    """
    if model is None or cfg is None:
        return model

    return resolve_alias(model, cfg.llm.model_aliases)


def _resolve_timeout(
    timeout: float | None,
    *,
    cfg: ProjectConfig | None = _CFG_UNSET,  # type: ignore[assignment]
    cwd: Path | str | None = None,
) -> float:
    """Resolve timeout from config when None, with hardcoded fallback."""
    if timeout is not None:
        return timeout

    resolved_cfg = _try_load_config(cwd) if cfg is _CFG_UNSET else cfg
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
    task_type: str | None = None,
    cwd: Path | str | None = None,
    max_budget_usd: Decimal | None = None,
    timeout: float | None = None,
) -> LLMResult:
    """Run a slash command via the active LLM provider.

    Args:
        task_type: Routing category (e.g. "develop", "self_review"). A routing
            key only, never part of the payload: it is neither appended to
            *command*/*args* nor compressed.
    """
    # Loaded before compression so args is compressed with the same cfg used
    # for timeout/chain resolution below, instead of loading config twice.
    cfg = await _try_load_config_async(cwd)
    _check_runaway_call_limit(cfg)
    _increment_call_counter()
    if args:
        from sova.llm.guard import guard_prompt

        assembled = f"{command} {args}".strip()
        guard_prompt(assembled)
        args = maybe_compress(args, cwd, cfg=cfg)
    resolved = select_model(_resolve_task_type_model(model, task_type, cfg=cfg), cfg)
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
            primary=resolved,
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
    task_type: str | None = None,
) -> list[BatchResult]:
    """Submit a batch of prompts. Uses a dedicated batch provider if available,
    otherwise falls back to the global provider's sequential default.

    Args:
        task_type: Routing category (e.g. "triage"). Used to resolve a model for
            requests that carry none; an explicit ``BatchRequest.model`` wins.
    """
    if not requests:
        return []

    import dataclasses

    from sova.llm.guard import guard_prompt

    # Loaded once for the whole batch and shared with every per-request
    # resolution, so a 50-issue triage batch does not reload config 50 times.
    cfg = await _try_load_config_async(cwd)

    # The batch backends post the model straight into an HTTP request body,
    # which (unlike the Claude CLI) rejects bare aliases. Resolve task-type
    # routing, the client-side alias map (matching invoke()/invoke_streaming()),
    # and the hardcoded tier expansion here so both backends are covered at the
    # one choke point.
    #
    # Unlike invoke()'s "route outranks model" contract (pipeline steps always
    # pass a default model, so the route would never fire otherwise), an
    # explicit BatchRequest.model is a genuine per-request caller choice and
    # must win over task_type routing: routing only fills in for requests that
    # carry none. _resolve_task_type_model() is therefore only consulted when
    # req.model is empty, rather than delegating priority to its own contract.
    #
    # When cfg failed to load, routing can never resolve anything anyway, so
    # the call is skipped entirely rather than made once per request: cfg=None
    # is indistinguishable from "not passed" to that helper, and it would
    # otherwise retry _try_load_config(cwd=None) on every iteration, silently
    # reloading from the wrong cwd besides.
    # Checked once against the whole batch, before any request is prepared or
    # dispatched: a batch submitted once the ceiling is already reached is
    # rejected outright rather than partially processed.
    _check_runaway_call_limit(cfg)
    # One increment per request: each is an independent LLM invocation, even
    # though the batch backends may submit them as a single HTTP call.
    _increment_call_counter(len(requests))

    prepared: list[BatchRequest] = []
    for req in requests:
        guard_prompt(req.prompt)
        resolved = req.model
        if cfg is not None and not req.model:
            # Only consulted when req.model is empty: _resolve_task_type_model()
            # itself lets a configured route outrank whatever model it is given
            # (see its docstring), so calling it with a non-empty req.model would
            # let routing override an explicit per-request choice regardless of
            # this function's own precedence contract.
            routed = _resolve_task_type_model(None, task_type, cfg=cfg)
            # Explicit None check, not `or`: a routed model can legitimately be
            # the empty-string "provider default" sentinel, which `or` would
            # mask by falling back to req.model.
            resolved = routed if routed is not None else req.model
        # An empty model is this dataclass's "provider default" sentinel and is
        # never aliased, the counterpart of select_model's own None handling.
        resolved = select_model(resolved, cfg) if resolved else resolved
        prepared.append(
            dataclasses.replace(
                req,
                prompt=maybe_compress(req.prompt, cwd, cfg=cfg),
                model=resolve_model_alias(resolved) if resolved else resolved,
            )
        )
    requests = prepared

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
    # Loaded once and shared, matching invoke(): compression, task-type routing
    # and the alias map all need it, and each would otherwise reload it. Also
    # needed before the counter increments, so the runaway call-limit check
    # can reject the invocation before any provider attempt.
    cfg = await _try_load_config_async(cwd)
    _check_runaway_call_limit(cfg)
    _increment_call_counter()
    prompt = maybe_compress(prompt, cwd, cfg=cfg)
    resolved = select_model(_resolve_task_type_model(model, task_type, cfg=cfg), cfg)
    async for event in get_provider().invoke_streaming(prompt, model=resolved, cwd=cwd, max_budget_usd=max_budget_usd):
        yield event


# Both the task-type key ("review") and the role name ("reviewer") are mapped:
# callers may hold either, depending on whether they read TASK_TYPE_KEYS or
# ``ctx.role`` (which dispatcher.py sets to the role's ``name``).
_ROLE_MODEL_FIELDS: dict[str, str] = {
    "researcher": "researcher_model",
    "triage": "triage_model",
    "review": "reviewer_model",
    "reviewer": "reviewer_model",
    "developer": "developer_model",
    "planner": "planner_model",
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
