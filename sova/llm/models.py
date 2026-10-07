"""Data models for the LLM interaction layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum


class ModelFamily(StrEnum):
    """Vendor family a model ID belongs to, for enumeration/UI grouping.

    ``OPENAI_OSS`` is deliberately distinct from ``OPENAI``: a Vertex AI
    deployment's ``openai`` publisher only ever serves ``gpt-oss*`` (open
    weights), never proprietary GPT/o-series models, and collapsing the two
    would let enumeration results imply access to models the deployment does
    not actually grant. ``LOCAL`` covers Ollama-served and other self-hosted
    models. ``UNKNOWN`` is the conservative default for an ID that matches no
    recognized prefix: the model still exists and must not be dropped from
    results, it is just unclassified.
    """

    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    OPENAI = "openai"
    OPENAI_OSS = "openai-oss"
    LOCAL = "local"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ModelInfo:
    """A single model a provider knows how to reach.

    ``tier`` reuses this codebase's generic tier vocabulary (``fast``,
    ``smart``, ``cheap``, the keys of ``_MODEL_ALIASES``/``model_aliases``),
    or ``""`` when a model does not map to one. ``source`` names where the
    entry came from (e.g. ``"curated"``, ``"probed"``, ``"anthropic_api"``,
    ``"vertex"``, ``"ollama"``, ``"openai_compatible"``), so a caller can tell
    a confirmed-reachable entry from a static fallback one.
    """

    id: str
    family: ModelFamily
    tier: str
    display_name: str
    source: str


class BatchTimeoutError(Exception):
    """Raised when a batch does not complete within the timeout."""


class CostSource(StrEnum):
    """Provenance of an ``LLMResult.cost_usd`` figure.

    ``UNKNOWN`` is the conservative default: a hand-built ``LLMResult`` (a test
    fixture, a third-party call site) is treated as unpriced rather than
    silently claiming a trusted cost. ``FREE_LOCAL`` is distinct from
    ``UNKNOWN``: local inference has no USD line item, so treating it as
    unpriced would fire spurious budget-blind-spot warnings on exactly the
    low-cost configurations it targets.
    """

    PRICED = "priced"
    FREE_LOCAL = "free_local"
    UNKNOWN = "unknown"


@dataclass
class LLMResult:
    """Result from a Claude Code CLI invocation."""

    text: str
    model: str
    cost_usd: Decimal = Decimal("0")
    cost_source: CostSource = CostSource.UNKNOWN
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    duration_ms: int = 0
    session_id: str = ""
    stop_reason: str = ""
    # Compression accounting: None when compression did not run (disabled, below
    # min_chars, package missing, or error); an int (>= 0) when it did.
    pre_compression_input_tokens: int | None = None
    tokens_saved: int | None = None
    # Codex-only breakdown detail: how many of the turn's output tokens were
    # reasoning tokens. Deliberately absent from total_tokens, because Codex
    # reports it as a component of output_tokens (OpenAI Responses API
    # convention), so adding it would double-count. Stays None on every
    # non-Codex result, which never populates it; a Codex result that omits
    # the field reports 0.
    reasoning_output_tokens: int | None = None
    # Codex-only: whether `text` is a cut version of a longer raw message.
    # Set from the raw, pre-redaction/pre-cap agent-message length, not from
    # len(text) against the display cap: redaction can shrink `text` below
    # the cap even when the original message was truncated, which would make
    # a length-based check on `text` miss exactly the inputs it exists to
    # catch. Always False on every non-Codex result.
    truncated: bool = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def is_error(self) -> bool:
        return self.stop_reason == "error"


@dataclass
class StreamEvent:
    """A streaming event from Claude Code CLI.

    Types:
    - "content": partial text output (text field populated)
    - "result": final result with costs (result field populated)
    """

    type: str
    text: str = ""
    result: LLMResult | None = field(default=None)


@dataclass
class BatchRequest:
    """A single request in a batch submission."""

    custom_id: str
    prompt: str
    model: str = ""
    max_tokens: int = 4096
    system: str = ""


@dataclass
class BatchResult:
    """Result for a single request in a batch submission."""

    request: BatchRequest
    result: LLMResult | None = None
    error: str = ""

    @property
    def succeeded(self) -> bool:
        return self.result is not None and not self.error


# Per-million-token pricing for Anthropic Messages API.
# Keys are model ID prefixes matched left-to-right; the first match wins.
# Values: (input_cost_per_mtok, output_cost_per_mtok).
_ANTHROPIC_RATE_CARD: dict[str, tuple[Decimal, Decimal]] = {
    "claude-fable-5": (Decimal("10"), Decimal("50")),
    "claude-opus-5": (Decimal("5"), Decimal("25")),
    "claude-sonnet-5": (Decimal("2"), Decimal("10")),
    "claude-haiku-4-5": (Decimal("1"), Decimal("5")),
    "claude-opus-4": (Decimal("15"), Decimal("75")),
    "claude-sonnet-4": (Decimal("3"), Decimal("15")),
}

_MTOK = Decimal("1_000_000")

# Bare family aliases carry no version, but the rate card is keyed by full model
# IDs. Map each alias to the current release in its family so rate lookups (used
# for cost/savings estimates) resolve instead of falling back to 0. Keep in sync
# with _ANTHROPIC_RATE_CARD as new releases ship.
# These values are also sent to raw HTTP model endpoints via resolve_model_alias()
# (batch submission, AnthropicAPIProvider.normalize_model_name), so a stale entry
# does not merely skew a cost estimate: it makes those calls fail outright.
_ALIAS_TO_CURRENT_MODEL: dict[str, str] = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5-20251001",
    "smart": "claude-opus-5",
    "fast": "claude-sonnet-5",
    "cheap": "claude-haiku-4-5-20251001",
}


def resolve_model_alias(model: str) -> str:
    """Expand a bare family alias to the current full model ID in that family.

    Unrecognized values (already-full IDs, third-party model names, empty
    strings) pass through unchanged. Raw HTTP model endpoints reject bare
    aliases, so any caller that hands a model straight to an API rather than to
    the Claude CLI (which resolves aliases itself) must run it through here.
    """
    return _ALIAS_TO_CURRENT_MODEL.get(model, model)


# Generic (capability) tiers, as opposed to the family aliases (opus/sonnet/haiku)
# that share _ALIAS_TO_CURRENT_MODEL with them.
_GENERIC_TIERS: tuple[str, ...] = ("smart", "fast", "cheap")

# Derived from _ALIAS_TO_CURRENT_MODEL rather than restated, so a curated or
# enumerated model ID reports its generic tier and a model-ID revision above
# cannot leave a hand-maintained reverse map silently stale.
_TIER_BY_MODEL_ID: dict[str, str] = {_ALIAS_TO_CURRENT_MODEL[tier]: tier for tier in _GENERIC_TIERS}


def model_tier_for_id(model_id: str) -> str:
    """Return the generic tier (fast/smart/cheap) for a known model ID, or ""."""
    return _TIER_BY_MODEL_ID.get(model_id, "")


def classify_model_family(model_id: str) -> ModelFamily:
    """Classify a model ID into a vendor family by ID prefix.

    Checked in a specific order: "gpt-oss" before the broader "gpt-" (Vertex's
    openai publisher serves open-weight gpt-oss models only, never proprietary
    GPT/o-series, so the two must never collapse to the same family), and
    local-backend prefixes before anything else since "ollama/llama3" carries
    no vendor-name prefix of its own. Returns ModelFamily.UNKNOWN rather than
    raising or dropping the model: an unrecognized ID is still a real model.
    """
    lowered = model_id.lower()
    if is_local_model_id(lowered):
        return ModelFamily.LOCAL
    if lowered.startswith("gpt-oss"):
        return ModelFamily.OPENAI_OSS
    if lowered.startswith("claude"):
        return ModelFamily.ANTHROPIC
    if lowered.startswith("gemini"):
        return ModelFamily.GOOGLE
    if lowered.startswith(("gpt-", "o1", "o3", "o4", "chatgpt")):
        return ModelFamily.OPENAI
    return ModelFamily.UNKNOWN


# The ABC's concrete default and every provider's "enumeration unavailable"
# fallback. Limited to the current-release Anthropic family (matching
# _ALIAS_TO_CURRENT_MODEL): this is a safety net for callers that cannot
# reach a real enumeration source, not a full model catalog.
# Tiers come from model_tier_for_id() rather than being restated per entry, so
# a model-ID revision in _ALIAS_TO_CURRENT_MODEL cannot leave a curated entry
# advertising a tier it no longer holds.
CURATED_MODELS: tuple[ModelInfo, ...] = tuple(
    ModelInfo(
        id=model_id,
        family=ModelFamily.ANTHROPIC,
        tier=model_tier_for_id(model_id),
        display_name=display_name,
        source="curated",
    )
    for model_id, display_name in (
        ("claude-opus-5", "Claude Opus 5"),
        ("claude-sonnet-5", "Claude Sonnet 5"),
        ("claude-haiku-4-5-20251001", "Claude Haiku 4.5"),
        ("claude-fable-5-1", "Claude Fable 5.1"),
    )
)


def compute_anthropic_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> Decimal:
    """Compute USD cost from token counts using the Anthropic rate card.

    Returns ``Decimal("0")`` for unknown models rather than raising.
    """
    rates = _lookup_rates(model)
    if rates is None:
        return Decimal("0")
    input_rate, output_rate = rates
    base_input = max(0, input_tokens - cache_read_tokens - cache_creation_tokens)
    cost = (
        input_rate * base_input / _MTOK
        + output_rate * output_tokens / _MTOK
        + input_rate * Decimal("0.1") * cache_read_tokens / _MTOK
        + input_rate * Decimal("1.25") * cache_creation_tokens / _MTOK
    )
    return cost.quantize(Decimal("0.000001"))


def input_rate_per_mtok(model: str) -> Decimal:
    """Return the per-million-token input rate for a model, or 0 if unknown.

    Bare family aliases ("opus", "sonnet", "haiku") are resolved to the current
    release in their family before lookup, since the rate card is keyed by full
    model IDs and config commonly stores bare aliases.
    """
    rates = _lookup_rates(resolve_model_alias(model))
    return rates[0] if rates else Decimal("0")


def _lookup_rates(model: str) -> tuple[Decimal, Decimal] | None:
    for prefix, rates in _ANTHROPIC_RATE_CARD.items():
        if model.startswith(prefix):
            return rates
    return None


# Local/self-hosted backends LiteLLM can route to are never in its hosted
# pricing database, so a cost lookup miss for one of these is expected and
# harmless, not a genuine pricing gap (R6, docs/model-selection-risk-assessment.md).
LOCAL_MODEL_PREFIXES: tuple[str, ...] = (
    "ollama/",
    "vllm/",
    "huggingface/",
    "text-generation-inference/",
)


def is_local_model_id(model: str) -> bool:
    """Return True if *model* is routed to a local/self-hosted backend."""
    return model.startswith(LOCAL_MODEL_PREFIXES)


def compute_model_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> tuple[Decimal, CostSource]:
    """Compute USD cost and its provenance for *model*.

    A wider entry point than ``compute_anthropic_cost()``, which stays
    Anthropic-only and pinned to its existing zero-for-unknown contract
    (``compute_anthropic_cost("gpt-4o", ...) == 0`` and
    ``input_rate_per_mtok("gpt-4o") == 0`` are load-bearing test contracts).
    Local/self-hosted models are trusted-but-zero (``FREE_LOCAL``) rather than
    unpriced, and any model the rate card does not recognize is ``UNKNOWN``
    rather than a silent ``$0`` indistinguishable from a genuinely free result.
    """
    if is_local_model_id(model):
        return Decimal("0"), CostSource.FREE_LOCAL
    resolved = resolve_model_alias(model)
    if _lookup_rates(resolved) is None:
        return Decimal("0"), CostSource.UNKNOWN
    cost = compute_anthropic_cost(resolved, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens)
    return cost, CostSource.PRICED
