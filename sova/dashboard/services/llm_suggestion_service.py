"""LLM-based PR action suggestion service for the dual-evaluation experiment.

Asks the configured LLM provider (via ``sova.llm.client.invoke()``, the same
choke point every other provider-routed caller in SOVA uses) to suggest a next
action for a PR, then compares it against the deterministic model's choice.
Only runs when ``llm.provider`` is Anthropic-capable (``claude-code`` or
``anthropic`` unconditionally, or one of the LiteLLM-backed types
``vertex``/``litellm``/``hybrid`` with an Anthropic ``llm.model`` configured):
this is an advisory comparison widget, not a configured workflow step, so it
must never reach a vendor the operator did not choose (#924).

Routing through ``sova.llm.client.invoke()`` rather than constructing a
provider per call means this widget's cost is recorded (``CostRecord``), its
calls count against the runaway-call guard, and no provider instance (with
its own connection pool) is leaked on every cache miss. The call is also made
``isolated=True``: on the default ``claude-code`` provider that disables
CLAUDE.md/hook/MCP auto-discovery and every built-in tool for this one
tool-free, advisory turn (see ``LLMProvider.invoke()``'s docstring), so this
module never needs to isolate itself from the inherited cwd.

Results are cached server-side for 5 minutes per (pr_number,
deterministic_state, pr_computed_state, merge_state, mergeable) key.

All results (agreements and disagreements) are cached and returned to the UI.
The UI shows a comparison widget when the LLM disagrees, and a standalone
"State is wrong" button on all PR-stage cards for user feedback.
"""

from __future__ import annotations

import asyncio
import json

from cachetools import TTLCache

from sova.config.loader import load_config
from sova.config.models import LLMConfig, ProjectConfig
from sova.llm.backends import TIER_NAMES, is_anthropic_capable
from sova.llm.client import invoke as llm_invoke
from sova.llm.client import resolve_alias
from sova.llm.models import resolve_model_alias
from sova.utils.json import extract_json
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.llm_suggestion")

# The cheap tier, named rather than pinned: _model_for_provider() expands it
# into the dialect the detected backend actually accepts.
_TIER = "haiku"
_MAX_TOKENS = 200
_CACHE_TTL = 300  # 5 minutes

# One minimal, isolated (--safe-mode --tools "") LLM turn: matches the
# allowance providers/claude_code.py gives its own single-turn probe
# (_MODEL_PROBE_TIMEOUT), which is isolated the same way.
_TIMEOUT = 20.0

# Bounds how many suggestion calls can be in flight at once: the dashboard's
# agents page fans out one request per PR-stage card via Promise.all, and
# without a cap a single page load could spawn as many concurrent provider
# calls (CLI subprocesses, on the default provider) as there are cards.
_CONCURRENCY_LIMIT = 3

_cache: TTLCache[str, dict] = TTLCache(maxsize=100, ttl=_CACHE_TTL)
_semaphore = asyncio.Semaphore(_CONCURRENCY_LIMIT)
_warned_not_anthropic_capable: bool = False

# Valid action IDs for PR-stage work items and their human-readable labels.
# Must match action ids used in work_item_service._get_actions().
_PR_ACTION_LABELS: dict[str, str] = {
    "review_pr": "Review PR",
    "address_review": "Address (SOVA findings)",
    "address_pr": "Address PR (threads)",
    "integrate": "Integrate PR",
}

_PROMPT = """\
You are deciding the best next action for a pull request in a software development workflow.

Available actions (choose exactly one):
- review_pr: Run SOVA code review. Posts approve/revise/block verdict to GitHub. \
Use when PR has no SOVA verdict yet, or after findings were addressed.
- address_review: Spawn developer agent to fix SOVA's code findings (rebase + write fixes + \
push). Use when SOVA reviewer said revise/block.
- address_pr: Resolve GitHub comment threads from CodeRabbit or human reviewers \
(reply + dismiss). No code changes. Use when external reviewer requested changes.
- integrate: Rebase, squash-merge, delete branch. Use when PR is approved, CI green, \
no blocking threads.

Current PR signals:
- computed_state: {pr_computed_state}
- has_sova_review: {has_sova_review}
- sova_verdict: {sova_verdict}
- mergeable: {mergeable}
- merge_state: {merge_state}
- review_decision: {review_decision}
- ci_passed: {ci_passed}
- external_reviews_enabled: {external_reviews_enabled}

The rule-based system chose: {deterministic_action_id} ("{deterministic_action_label}")

Return ONLY valid JSON, no other text:
{{"action_id": "one_of_the_above", "reasoning": "one sentence why"}}"""


async def _load_config_or_none() -> ProjectConfig | None:
    """Load the project config fresh. Returns None (fail closed) on any error.

    Unlike a plain feature toggle, a config-load failure here must not fall
    through to an unconditional suggestion call: without a loaded ``LLMConfig``
    there is no way to know which provider is configured, and guessing would
    risk reaching a vendor the operator never chose (#924).
    """
    try:
        return await asyncio.to_thread(load_config)
    except Exception:  # noqa: BLE001 (suggestions are disabled when config cannot be loaded)
        log.warning("llm_suggestion.config_load_failed", exc_info=True)
        return None


def _make_cache_key(
    pr_number: int, deterministic_state: str, pr_computed_state: str, merge_state: str, mergeable: str
) -> str:
    """Include mergeable as well as merge_state: while merge_state stays UNKNOWN (GitHub
    still recomputing mergeStateStatus), mergeable can independently flip from UNKNOWN to
    MERGEABLE, and the resolver reads that fallback value (CodeRabbit, PR #1114)."""
    return f"{pr_number}|{deterministic_state}|{pr_computed_state}|{merge_state}|{mergeable}"


def _vendor_prefix(model: str) -> str:
    """Return the litellm vendor-prefix segment of *model* (e.g. ``"vertex_ai/"``), or ``""``."""
    return f"{model.split('/', 1)[0]}/" if "/" in model else ""


def _model_for_provider(llm_cfg: LLMConfig) -> str | None:
    """Return the model to request, in the dialect the active backend accepts.

    ``litellm``/``hybrid`` resolves the cheap tier too, rather than deferring
    to the operator's primary ``cfg.model``: the whole point of ``_TIER`` is
    that this advisory widget runs on the cheap tier, and letting the call
    fall through to ``cfg.model`` would run it on whatever model (including
    Opus) the operator pinned for real work. The vendor prefix is taken from
    ``cfg.model`` and reapplied to the resolved tier, but only when that
    prefix is ``"anthropic/"``: that is the only prefix for which
    ``resolve_model_alias()``'s firstParty ID is also the correct dialect.
    ``"bedrock/"`` needs the ``anthropic.``/``us.anthropic.`` ID form,
    ``"vertex_ai/"`` needs an ``@``-pinned snapshot, and a two-segment prefix
    like ``"openrouter/anthropic/"`` would lose its vendor segment entirely.
    For any other prefix (or no prefix at all, a bare model name), ``None``
    is returned so ``invoke()`` falls back to ``cfg.model`` unchanged rather
    than sending an ID a litellm route may not accept.

    Every other capable provider gets ``_TIER`` resolved through
    ``resolve_alias()``, the same choke point ``create_provider()`` uses, rather
    than a hardcoded ID: a per-call ``model=`` never passes through
    ``resolve_alias()`` on its own, so a literal would reach the provider
    verbatim and only one backend's dialect can be written down at a time. A
    firstParty ID (``"claude-haiku-4-5-20251001"``, the value this module used
    to pin) serves ``claude-code``/``anthropic``; an ``@``-pinned snapshot
    serves a ``CLAUDE_CODE_USE_VERTEX``-routed CLI, which rejects the
    firstParty form; and ``llm.provider="vertex"`` additionally needs litellm's
    ``vertex_ai/`` prefix, without which litellm reads a bare ``claude-...`` ID
    as Anthropic-direct and leaves the operator's Vertex project entirely
    (#924).

    ``resolve_alias()``'s own tier-candidate expansion only runs when
    ``cfg.resolve_tier_aliases`` is set; with it cleared (an explicit,
    system-wide "byte-identical passthrough" opt-out), a bare tier name like
    ``_TIER`` would otherwise reach a non-CLI provider verbatim and fail every
    call. This module owns the choice to use a bare tier name as its
    implementation detail, so it resolves it to a concrete ID itself
    regardless of that global opt-out, for every provider except
    ``claude-code`` (whose CLI resolves a bare tier name natively).
    """
    if llm_cfg.provider in ("litellm", "hybrid"):
        prefix = _vendor_prefix(llm_cfg.model or "")
        if prefix != "anthropic/":
            return None
        return f"{prefix}{resolve_model_alias(_TIER)}"
    resolved = resolve_alias(_TIER, llm_cfg)
    if resolved in TIER_NAMES and llm_cfg.provider != "claude-code":
        resolved = resolve_model_alias(resolved)
    return resolved


async def get_llm_suggestion(
    *,
    pr_number: int,
    deterministic_state: str,
    deterministic_action_id: str,
    pr_computed_state: str,
    has_sova_review: bool,
    sova_verdict: str | None,
    mergeable: str,
    merge_state: str = "UNKNOWN",
    review_decision: str | None = None,
    ci_passed: bool = False,
    external_reviews_enabled: bool = True,
) -> dict | None:
    """Ask the LLM to suggest a PR action. Returns None on any error.

    Result shape when non-None:
        {action_id, action_label, reasoning, disagrees}
    """
    global _warned_not_anthropic_capable

    cache_key = _make_cache_key(pr_number, deterministic_state, pr_computed_state, merge_state, mergeable)
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    cfg = await _load_config_or_none()
    if cfg is None:
        return None
    if not cfg.dashboard.llm_suggestions:
        return None

    if not is_anthropic_capable(cfg.llm):
        if not _warned_not_anthropic_capable:
            log.info(
                "llm_suggestion.disabled_non_anthropic_provider",
                provider=cfg.llm.provider,
                hint="LLM action suggestions require an Anthropic-capable llm.provider "
                "(claude-code, anthropic, or vertex/litellm/hybrid with an Anthropic llm.model)",
            )
            _warned_not_anthropic_capable = True
        return None

    try:
        prompt = _PROMPT.format(
            pr_computed_state=pr_computed_state or "unknown",
            has_sova_review=has_sova_review,
            sova_verdict=sova_verdict or "none",
            mergeable=mergeable or "unknown",
            merge_state=merge_state or "unknown",
            review_decision=review_decision or "none",
            ci_passed=ci_passed,
            external_reviews_enabled=external_reviews_enabled,
            deterministic_action_id=deterministic_action_id,
            deterministic_action_label=_PR_ACTION_LABELS.get(deterministic_action_id, deterministic_action_id),
        )
    except (KeyError, ValueError):
        log.warning("llm_suggestion.prompt_format_failed", pr=pr_number, exc_info=True)
        return None

    try:
        async with _semaphore:
            result = await llm_invoke(
                prompt,
                model=_model_for_provider(cfg.llm),
                task_type="pr_suggestion",
                max_tokens=_MAX_TOKENS,
                timeout=_TIMEOUT,
                isolated=True,
            )
        text = result.text.strip()
        json_str = extract_json(text)
        if not json_str:
            log.warning("llm_suggestion.no_json_found", pr=pr_number, response_preview=text[:200])
            return None
        parsed = json.loads(json_str)
    except Exception:  # noqa: BLE001 (provider call, auth and JSON parse all fail here; suggestions are optional)
        log.warning("llm_suggestion.call_failed", pr=pr_number, exc_info=True)
        return None

    action_id = parsed.get("action_id", "")
    if action_id not in _PR_ACTION_LABELS:
        log.warning("llm_suggestion.invalid_action_id", pr=pr_number, action_id=action_id)
        return None

    result_dict: dict = {
        "action_id": action_id,
        "action_label": _PR_ACTION_LABELS[action_id],
        "reasoning": str(parsed.get("reasoning", "")),
        "disagrees": action_id != deterministic_action_id,
    }
    _cache[cache_key] = result_dict
    return result_dict


def clear_cache() -> None:
    """Clear the suggestion cache. Intended for testing."""
    _cache.clear()
