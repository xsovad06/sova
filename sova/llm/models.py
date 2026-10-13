"""Data models for the LLM interaction layer."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
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
    low-cost configurations it targets. ``SUBSCRIPTION`` is likewise distinct
    from ``UNKNOWN``: a run billed against a flat-rate subscription (e.g.
    Codex CLI authenticated via ``codex login``'s ChatGPT account rather than
    an API key) has no incremental per-call dollar cost, which is a known
    fact, not a gap in SOVA's pricing data. Conflating the two would make a
    verified-free subscription run indistinguishable from a run whose cost
    simply could not be determined.
    """

    PRICED = "priced"
    FREE_LOCAL = "free_local"
    SUBSCRIPTION = "subscription"
    UNKNOWN = "unknown"


def combine_cost_sources(current: CostSource | None, new: CostSource) -> CostSource:
    """Provenance of a cost summed from several results, independent of call order.

    The label answers whether the summed ``cost_usd`` is a complete figure.
    One ``UNKNOWN`` contribution makes the sum a lower bound, so it wins over
    everything. Contributions that are all known but differ (priced dollars
    plus a subscription or local call's known zero) still sum to a complete
    figure, which is what ``PRICED`` asserts. ``current`` is ``None`` before
    the first contribution.
    """
    if current is None or current == new:
        return new
    if CostSource.UNKNOWN in (current, new):
        return CostSource.UNKNOWN
    return CostSource.PRICED


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
    # None means the provider never reported a cache-write figure at all
    # (today: Codex, whose JSONL carries no cache-creation field), distinct
    # from an explicit 0 (a provider that reports cache writes and confirms
    # none happened). Conflating the two stored a fabricated 0 in the nullable
    # CostRecord.cache_write_tokens column, the exact NULL-vs-zero confusion
    # that column's own convention exists to avoid. Every producer that does
    # report the figure (Claude CLI, Anthropic API, litellm where available)
    # still passes an explicit int.
    cache_creation_tokens: int | None = None
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
    # Name of the model-ignoring AgentRuntime that produced this result (today
    # only Codex), set by agent_dispatch._dispatch_to_runtime. None for every
    # LLMProvider call and for a runtime that runs the model SOVA chose (Claude
    # Code), whose results stay on the step's provider CostRecord.
    # ExecutionContext.add_usage() routes a tagged result to the runtime's own
    # record instead, so a step that dispatches to Codex and also makes a priced
    # provider call never merges the two (issue #1128).
    runtime_name: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def is_error(self) -> bool:
        return self.stop_reason == "error"


def build_claude_cli_result(data: dict, *, stop_reason: str = "") -> LLMResult:
    """Build an ``LLMResult`` from a Claude CLI ``"result"`` event payload.

    Shared by ``ClaudeCodeRuntime.parse_output()`` (streaming,
    ``sova/ipc/runtime.py``) and ``_parse_result()`` (batch,
    ``sova/llm/providers/claude_code.py``), which both parse the same
    stream-json "result" event shape. Before this was shared, the two had
    drifted apart: the runtime parser read ``cache_read_tokens``/
    ``cache_creation_tokens`` and a bare ``data["model"]``, while the CLI
    actually emits ``cache_read_input_tokens``/``cache_creation_input_tokens``
    and the model under ``modelUsage``, so every tool-using pipeline step
    recorded zero cache tokens and an empty model (#1125 round 4 review).
    """
    raw_usage = data.get("usage")
    usage = raw_usage if isinstance(raw_usage, dict) else {}
    raw_model_usage = data.get("modelUsage")
    model_usage = raw_model_usage if isinstance(raw_model_usage, dict) else {}
    model = next(iter(model_usage), "") if model_usage else str(data.get("model", ""))

    try:
        cost_usd = Decimal(str(data.get("total_cost_usd", 0)))
    except (ArithmeticError, ValueError, TypeError):
        cost_usd = Decimal(0)

    return LLMResult(
        text=str(data.get("result", "")),
        model=model,
        cost_usd=cost_usd,
        cost_source=CostSource.PRICED,
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cache_read_tokens=usage.get("cache_read_input_tokens", 0),
        cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
        duration_ms=data.get("duration_ms", 0),
        session_id=str(data.get("session_id", "")),
        stop_reason=stop_reason,
    )


def _sum_optional(a: int | None, b: int | None) -> int | None:
    """Sum two optional counters, keeping ``None`` only when both are unreported."""
    if a is None and b is None:
        return None
    return (a or 0) + (b or 0)


def accumulate_llm_result(previous: LLMResult | None, latest: LLMResult) -> LLMResult:
    """Merge a new terminal result onto whatever was already accumulated for this spawn.

    Codex is the only source that can emit more than one terminal ``result``
    per spawn (one thread can run several turns), and each turn reports its
    own token usage, not a thread-cumulative total (see
    ``CodexStreamParser._reset_for_turn``). Keeping only the first or last
    terminal event therefore drops every other turn's spend. Descriptive
    fields (``text``, ``model``, ``session_id``, ``stop_reason``,
    ``truncated``) come from *latest*, since those describe what just
    happened rather than a running total; cost, token counts, elapsed
    duration and compression counters are all summed, including
    ``tokens_saved``/``pre_compression_input_tokens``: no multi-turn source
    populates either today, but leaving them un-summed would silently
    under-report the moment one does (issue #1128 review, round 4).
    """
    if previous is None:
        return latest
    return replace(
        latest,
        cost_usd=previous.cost_usd + latest.cost_usd,
        input_tokens=previous.input_tokens + latest.input_tokens,
        output_tokens=previous.output_tokens + latest.output_tokens,
        cache_read_tokens=previous.cache_read_tokens + latest.cache_read_tokens,
        cache_creation_tokens=_sum_optional(previous.cache_creation_tokens, latest.cache_creation_tokens),
        reasoning_output_tokens=_sum_optional(previous.reasoning_output_tokens, latest.reasoning_output_tokens),
        duration_ms=previous.duration_ms + latest.duration_ms,
        tokens_saved=_sum_optional(previous.tokens_saved, latest.tokens_saved),
        pre_compression_input_tokens=_sum_optional(
            previous.pre_compression_input_tokens, latest.pre_compression_input_tokens
        ),
    )


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
