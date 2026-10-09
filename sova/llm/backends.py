"""Backend detection and per-backend model-ID serviceability.

A leaf module: no imports from elsewhere in ``sova`` at runtime (only
``LLMConfig`` under ``TYPE_CHECKING`` for the type hint), so both
``sova/llm/client.py`` and ``sova/llm/provider.py`` can depend on it without
risking an import cycle.

The bug this exists to fix: ``llm.provider`` defaults to ``"claude-code"``,
which sends model names straight to the Claude CLI. On a native Anthropic
subscription the CLI resolves a bare tier name (``"opus"``, ``"sonnet"``, ...)
itself, so passing one through unchanged is correct today. But the CLI also
honours ``CLAUDE_CODE_USE_VERTEX``/``CLAUDE_CODE_USE_BEDROCK``, which silently
redirect it to a deployment that does NOT resolve bare tier names: it just
forwards them to Vertex/Bedrock, which reject anything but a fully-qualified
model ID. ``llm.provider`` alone can't see that redirection; only the process
environment can (docs/model-selection-architecture.md, R14).

``detect_backend()`` names which model-ID dialect the active deployment
speaks; ``backend_can_serve()`` and ``tier_candidates_for()`` let the alias
resolver in ``sova/llm/client.py`` fall through to a dialect-appropriate ID
only when the bare tier name it would otherwise send is known not to work.
"""

from __future__ import annotations

import json
import os
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sova.config.models import LLMConfig


class Backend(StrEnum):
    """Which model-ID dialect the active deployment expects."""

    FIRSTPARTY = "firstparty"
    VERTEX = "vertex"
    BEDROCK = "bedrock"
    # Permissive catch-all: LiteLLM (and its vendor-specific shortcuts
    # litellm/hybrid/openai/ollama) prefixes or otherwise owns its own model
    # ID resolution per configured provider, so nothing here should second-
    # guess it. Also the fail-open default for an unrecognized llm.provider.
    LITELLM = "litellm"


# The six generic tier names a deployment's llm.model_aliases map may target
# (docs/model-selection-architecture.md, Q3). Kept independent of
# sova.llm.models._ALIAS_TO_CURRENT_MODEL's private table (same six names) so
# this module stays a leaf with no sova.llm.models import.
TIER_NAMES: frozenset[str] = frozenset({"opus", "sonnet", "haiku", "fast", "smart", "cheap"})

# Fully-qualified, per-tier candidate IDs for backends that reject a bare tier
# name. Ordered: the first entry ``backend_can_serve()`` accepts wins.
# Deliberately absent for FIRSTPARTY/LITELLM: backend_can_serve() never
# rejects a bare tier name on either, so their candidate lists would never be
# consulted.
_VERTEX_TIER_CANDIDATES: dict[str, list[str]] = {
    "opus": ["claude-opus-4-6@20260401", "claude-opus-4-1@20250805"],
    "smart": ["claude-opus-4-6@20260401", "claude-opus-4-1@20250805"],
    "sonnet": ["claude-sonnet-4-5@20250929"],
    "fast": ["claude-sonnet-4-5@20250929"],
    "haiku": ["claude-haiku-4-5@20251001"],
    "cheap": ["claude-haiku-4-5@20251001"],
}

_BEDROCK_TIER_CANDIDATES: dict[str, list[str]] = {
    "opus": ["us.anthropic.claude-opus-4-1-20250805-v1:0"],
    "smart": ["us.anthropic.claude-opus-4-1-20250805-v1:0"],
    "sonnet": ["us.anthropic.claude-sonnet-4-5-20250929-v1:0"],
    "fast": ["us.anthropic.claude-sonnet-4-5-20250929-v1:0"],
    "haiku": ["us.anthropic.claude-haiku-4-5-20251001-v1:0"],
    "cheap": ["us.anthropic.claude-haiku-4-5-20251001-v1:0"],
}

_BUILTIN_TIER_CANDIDATES: dict[Backend, dict[str, list[str]]] = {
    Backend.VERTEX: _VERTEX_TIER_CANDIDATES,
    Backend.BEDROCK: _BEDROCK_TIER_CANDIDATES,
}

# Reverse of _BUILTIN_TIER_CANDIDATES: a known pinned candidate ID to the tier
# it was pinned for. Lets resolve_alias() recognize a pinned ID reached
# directly (not via a tier alias, e.g. agent.model set to a Vertex snapshot
# ID) whose backend has since drifted (#1029: the ID was pinned while
# Vertex-routed, then the deployment moved to firstParty/Bedrock without the
# pinned value being updated), so it can still be corrected to a servable one.
_CANDIDATE_TO_TIER: dict[str, str] = {
    candidate: tier
    for table in _BUILTIN_TIER_CANDIDATES.values()
    for tier, candidates in table.items()
    for candidate in candidates
}

# Bedrock's model-ID dialect (Anthropic models via the Bedrock Converse/Invoke
# API): a region-prefixed inference profile ID ("us.anthropic.claude-...",
# also published for "eu."/"apac." regions) or the bare provider-prefixed form
# ("anthropic.claude-..."). None of these are valid on any other backend:
# Anthropic's direct API and Vertex both reject the "anthropic." prefix
# outright.
_BEDROCK_ID_PREFIXES: tuple[str, ...] = ("anthropic.", "us.anthropic.", "eu.anthropic.", "apac.anthropic.")


def tier_for_known_candidate(model_id: str) -> str | None:
    """Return the tier a known Vertex/Bedrock-pinned candidate ID belongs to, else None."""
    return _CANDIDATE_TO_TIER.get(model_id)


def is_anthropic_model_id(model: str) -> bool:
    """Return whether *model* names an Anthropic (Claude) model, in any backend's dialect.

    Covers a bare generic tier name (``"haiku"``, ...), a firstParty/Vertex-style
    ``"claude-..."`` ID, and a Bedrock-dialect ``"anthropic.claude-..."``/
    ``"us.anthropic.claude-..."``/``"eu.anthropic.claude-..."``/
    ``"apac.anthropic.claude-..."`` ID, each optionally carrying the litellm
    vendor prefix its route needs (``"anthropic/claude-..."``,
    ``"vertex_ai/claude-..."``, ``"bedrock/us.anthropic.claude-..."``,
    ``"openrouter/anthropic/claude-..."``): a config reached through litellm
    names its vendor in the ID, so matching only the first path segment would
    miss a multi-segment route (``"openrouter/anthropic/..."``) where the
    Anthropic marker sits one segment deeper. Every leading segment is
    therefore checked in turn, not just the first. ``"ollama/"`` is rejected
    rather than stripped, since a locally-served model is not Anthropic
    whatever it was named. Used to decide whether a vendor-agnostic provider
    type is still pointed at Claude, per ``is_anthropic_capable()`` below.
    """
    if not model or model.startswith("ollama/"):
        return False
    segments = model.split("/")
    if any(segment == "anthropic" for segment in segments[:-1]):
        return True
    bare = segments[-1]
    if bare in TIER_NAMES:
        return True
    if bare.startswith("claude-"):
        return True
    return bare.startswith(_BEDROCK_ID_PREFIXES)


def is_anthropic_capable(cfg: LLMConfig) -> bool:
    """Return whether *cfg* can serve an Anthropic (Claude) model call.

    ``"claude-code"`` and ``"anthropic"`` are Anthropic-capable unconditionally:
    those provider types exist specifically to reach Claude (the CLI or the
    direct API), and both default to a Claude model when ``cfg.model`` is
    empty. ``"vertex"``, ``"litellm"`` and ``"hybrid"`` are all LiteLLM under a
    more discoverable name and can serve any vendor the route reaches
    (``_VENDOR_MODEL_EXAMPLES`` in ``sova/config/models.py`` documents
    ``"vertex_ai/gemini-2.5-pro"`` as the example ``"vertex"`` model), so
    capability there depends on whether ``cfg.model`` actually names an
    Anthropic model. A leftover Anthropic model id in ``cfg.model`` does not
    matter for ``"openai"``/``"ollama"``, which forward to a non-Anthropic
    vendor regardless of what ``cfg.model`` says. Used to gate code paths that
    must only ever reach an Anthropic model, never silently fall through to
    whatever vendor is actually configured (#924).
    """
    if cfg.provider in ("claude-code", "anthropic"):
        return True
    if cfg.provider in ("vertex", "litellm", "hybrid"):
        return is_anthropic_model_id(cfg.model)
    return False


def _env_flag(source: Mapping[str, str], name: str) -> bool:
    """Return whether *name* is set to a truthy value in *source*."""
    return source.get(name, "").strip().lower() not in ("", "0", "false")


def routing_env_vars_present() -> bool:
    """Return whether either CLI routing env var has a truthy raw value.

    Cheap pre-check (reads ``os.environ`` directly, no config load) so a
    caller can skip building a scrubbed environment (which needs
    ``agent.env_passthrough`` from a full config load) when neither var is
    set, the common case for a deployment that never routes through
    Vertex/Bedrock. See ``sova.llm.client.resolve_alias``.
    """
    return _env_flag(os.environ, "CLAUDE_CODE_USE_VERTEX") or _env_flag(os.environ, "CLAUDE_CODE_USE_BEDROCK")


def detect_backend(cfg: LLMConfig, env: Mapping[str, str] | None = None) -> Backend:
    """Return the model-ID dialect the active deployment actually speaks.

    Args:
        cfg: The project's ``llm`` config section.
        env: The environment to read ``CLAUDE_CODE_USE_VERTEX``/
            ``CLAUDE_CODE_USE_BEDROCK`` from. Defaults to ``os.environ``.
            A caller resolving a model that will be sent to a *spawned*
            Claude CLI child must instead pass that child's actual
            environment (see ``sova.utils.env.scrub_agent_env``): the
            routing vars are stripped from a spawned agent's environment
            unless explicitly passed through via ``agent.env_passthrough``,
            so reading the calling process's own unscrubbed ``os.environ``
            there would detect a backend the child never actually runs
            against.

    Never raises: an unrecognized ``cfg.provider`` value, or a missing
    attribute on a non-config object, degrades to ``Backend.LITELLM``, the
    permissive backend whose ``backend_can_serve()`` accepts everything,
    reproducing today's plain passthrough.
    """
    source = os.environ if env is None else env
    try:
        provider = cfg.provider
        if provider == "claude-code":
            # The CLI's own env-based routing overrides llm.provider, so it is
            # checked first regardless of which is set.
            if _env_flag(source, "CLAUDE_CODE_USE_VERTEX"):
                return Backend.VERTEX
            if _env_flag(source, "CLAUDE_CODE_USE_BEDROCK"):
                return Backend.BEDROCK
            return Backend.FIRSTPARTY
        if provider == "anthropic":
            return Backend.FIRSTPARTY
        if provider == "vertex":
            return Backend.VERTEX
        return Backend.LITELLM
    except Exception:  # noqa: BLE001 (unreadable config degrades to the permissive backend)
        return Backend.LITELLM


def backend_can_serve(backend: Backend, model_id: str) -> bool:
    """Return whether *backend* can be sent *model_id* as-is.

    Fail-open by design (docs Q5): everything not explicitly known to be
    wrong for *backend* passes, including a correctly-pinned ID this table
    has never seen. A closed default would silently reroute it to a stale
    candidate the moment a new model ships, which is worse than letting the
    provider report a real ``ModelUnavailableError`` the existing fallback
    chain already handles. Three cases are known to be wrong:

    - A bare tier name (``"opus"``, ``"sonnet"``, ...) on VERTEX/BEDROCK,
      which require a fully-qualified ID.
    - An ``@``-pinned Vertex snapshot ID (``"claude-sonnet-4-5@20250929"``)
      on FIRSTPARTY: Anthropic's direct API/CLI dialect has no ``@`` pinning
      syntax (#1029).
    - A Bedrock-dialect ID (``"anthropic.claude-..."``,
      ``"us.anthropic.claude-..."``) anywhere but BEDROCK.
    """
    if backend in (Backend.VERTEX, Backend.BEDROCK) and model_id in TIER_NAMES:
        return False
    if backend == Backend.FIRSTPARTY and "@" in model_id:
        return False
    if backend != Backend.BEDROCK and model_id.startswith(_BEDROCK_ID_PREFIXES):
        return False
    return True


def _parse_candidate_list(raw: str) -> list[str]:
    """Parse a ``tier_candidates`` value from a JSON array or a comma-separated string.

    Mirrors ``sova/dashboard/services/settings_service.py:_cast_list()``'s
    tolerance (duplicated rather than imported: this module is a leaf with no
    ``sova.dashboard`` dependency).
    """
    stripped = raw.strip()
    if not stripped:
        return []
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    return [item.strip() for item in stripped.split(",") if item.strip()]


def tier_candidates_for(backend: Backend, tier: str, overrides: dict[str, str] | None = None) -> list[str]:
    """Return the ordered, backend-servable candidate list for *tier*.

    Checks ``overrides`` (``llm.tier_candidates``, keyed ``"{backend}:{tier}"``,
    value a JSON array or comma-separated ordered list) before the built-in
    table. An override whose parsed list is empty, or whose every entry
    ``backend_can_serve()`` rejects, falls back to the built-in table for that
    key; an empty or all-rejected built-in table returns ``[]``, leaving the
    caller to pass the original name through unchanged rather than resolving
    to ``None``.
    """
    if overrides:
        raw = overrides.get(f"{backend.value}:{tier}", "")
        override_candidates = [c for c in _parse_candidate_list(raw) if backend_can_serve(backend, c)]
        if override_candidates:
            return override_candidates

    builtin = _BUILTIN_TIER_CANDIDATES.get(backend, {}).get(tier, [])
    return [c for c in builtin if backend_can_serve(backend, c)]
