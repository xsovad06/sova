"""Regression tests for issue #924: no code outside sova/llm/ may call an LLM
provider directly, bypassing the LLMProvider/create_provider() abstraction,
either by importing an HTTP/SDK client itself or by constructing a concrete
provider class (LiteLLMProvider/AnthropicAPIProvider/ClaudeCodeProvider)
directly.

Three kinds of check:

1. AST-based import scans across the whole ``sova/`` tree (both ``import x``
   and ``from x import y`` forms), so a new direct-API call added anywhere
   (not just in the two call sites this issue fixed) fails CI instead of
   silently reintroducing the bypass.
2. An AST-based scan for direct construction of a concrete provider class
   outside ``sova/llm/``, the shape this issue originally fixed (rebase
   consensus, the LLM suggestion service): a new direct call site reached
   through ``LiteLLMProvider(...)``/``AnthropicAPIProvider(...)``/
   ``ClaudeCodeProvider(...)`` bypasses ``create_provider()`` just as
   thoroughly as a raw HTTP/SDK call would, so it is held to the same
   allowlist discipline.
3. Behavioral tests for the capability gate (``sova.llm.backends.
   is_anthropic_capable``) that the two fixed call sites (rebase consensus,
   the LLM suggestion service) now use to decide whether to run at all.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

import pytest

from sova.config.models import LLMConfig
from sova.llm.backends import is_anthropic_capable, is_anthropic_model_id

REPO_ROOT = Path(__file__).parent.parent
SOVA_ROOT = REPO_ROOT / "sova"

# Modules outside sova/llm/ known to need a direct HTTP/SDK client for
# something other than an LLM call: Jira REST API, push notifications,
# dependency registry lookups, the setup wizard's connection checks,
# fire-and-forget telemetry reporting to SOVA's own endpoint, and the
# awareness subsystem's Google OAuth flow (Gmail/GCal, unrelated to any LLM
# provider's ADC usage). Any new import of one of these identifiers outside
# sova/llm/ must be added here deliberately, after confirming it is not a
# direct LLM provider call (#924).
_ALLOWED_HTTP_IMPORT_USERS: dict[str, frozenset[Path]] = {
    "httpx": frozenset(
        {
            SOVA_ROOT / "adapters" / "jira.py",
            SOVA_ROOT / "dashboard" / "routers" / "setup.py",
            SOVA_ROOT / "dashboard" / "services" / "dependency_health_service.py",
            SOVA_ROOT / "dashboard" / "services" / "setup_service.py",
            SOVA_ROOT / "dashboard" / "services" / "telemetry_push.py",
            SOVA_ROOT / "ipc" / "notifications.py",
        }
    ),
    "google.auth": frozenset({SOVA_ROOT / "awareness" / "auth" / "google_oauth.py"}),
}

# Module identifiers that must only ever be imported inside sova/llm/ or an
# audited call site above: each is an LLM provider SDK or an HTTP client
# capable of reaching one directly. Two entries are scoped to their first two
# dotted segments rather than the bare root, because the bare root is also
# used for unrelated, legitimate purposes elsewhere: "urllib.parse" (URL
# parsing, six non-LLM call sites) vs. "urllib.request" (an actual HTTP
# client), and "google.auth" (ADC credentials; legitimately used both inside
# sova/llm/ and, for an unrelated OAuth flow, in sova/awareness/) vs. other
# "google.*" namespace packages. "requests"/"aiohttp" are included even
# though nothing in sova/ currently imports them outside sova/llm/,
# specifically so a future addition is caught rather than silently passing
# because no rule covered that root.
_LLM_CAPABLE_IMPORT_ROOTS: frozenset[str] = frozenset(
    {"anthropic", "openai", "litellm", "requests", "aiohttp", "urllib.request", "google.auth"}
)

# Concrete LLMProvider subclasses: direct construction outside sova/llm/
# bypasses create_provider() exactly as a raw HTTP/SDK call would.
_PROVIDER_CLASS_NAMES: frozenset[str] = frozenset({"LiteLLMProvider", "AnthropicAPIProvider", "ClaudeCodeProvider"})

# Audited call sites that legitimately construct a provider class directly
# outside sova/llm/, each for a reason documented at the call site itself:
# sova/git/rebase.py's multi-model consensus resolver builds one LiteLLMProvider
# per configured consensus model (gated by sova.llm.backends.detect_backend,
# see _load_consensus_config's docstring), and
# sova/dashboard/services/setup_service.py's Connections page builds a candidate
# provider to validate before persisting it (see architecture.md, "The
# Connections page builds a candidate provider without an LLMConfig").
_ALLOWED_PROVIDER_CONSTRUCTION_USERS: frozenset[Path] = frozenset(
    {
        SOVA_ROOT / "git" / "rebase.py",
        SOVA_ROOT / "dashboard" / "services" / "setup_service.py",
    }
)

# Anthropic API/Vertex-for-Claude endpoint path fragments that must only ever
# be constructed inside sova/llm/ (the provider layer), never by a caller
# building its own request. Scoped to the API path, not the bare hostname:
# sova/supervisor/network_health.py legitimately probes "api.anthropic.com"
# for reachability without ever building a Messages API request.
_ANTHROPIC_ENDPOINT_FRAGMENTS = (
    "api.anthropic.com/v1/messages",
    "publishers/anthropic/models",
)


@cache
def _modules_outside_llm() -> dict[Path, ast.Module]:
    """Map every ``sova/`` module outside ``sova/llm/`` to its parsed AST.

    Cached: all scans below walk the same tree, and re-parsing several
    hundred files once per test is the whole cost of this module. A file that
    fails to parse (should never happen for committed source) is skipped
    rather than raising, so a syntax error elsewhere doesn't mask this
    module's own failures.
    """
    modules: dict[Path, ast.Module] = {}
    for path in SOVA_ROOT.rglob("*.py"):
        if "llm" in path.relative_to(SOVA_ROOT).parts:
            continue
        try:
            modules[path] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # pragma: no cover (committed source always parses)
            continue
    return modules


@cache
def _sources_outside_llm() -> dict[Path, str]:
    """Map every ``sova/`` module outside ``sova/llm/`` to its source text.

    Used by the two substring-based checks (hardcoded endpoints, direct
    provider-module file assertions) that don't need import-statement
    precision.
    """
    return {
        path: path.read_text(encoding="utf-8")
        for path in SOVA_ROOT.rglob("*.py")
        if "llm" not in path.relative_to(SOVA_ROOT).parts
    }


def _imported_roots(tree: ast.Module) -> set[str]:
    """Return every imported module identifier in *tree*, at both granularities.

    ``import a.b.c`` and ``from a.b.c import d`` each contribute the bare root
    ``"a"`` *and* the two-segment form ``"a.b"``, so a caller checking a
    dotted identifier (``"urllib.request"``, ``"google.auth"``) can tell it
    apart from an unrelated sibling submodule (``"urllib.parse"``,
    ``"google.generativeai"``) that happens to share the same root.
    ``from . import x`` (a relative import, ``module`` is ``None``)
    contributes nothing, since a relative import can never reach an external
    SDK.
    """
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                roots.add(parts[0])
                if len(parts) > 1:
                    roots.add(f"{parts[0]}.{parts[1]}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            parts = node.module.split(".")
            roots.add(parts[0])
            if len(parts) > 1:
                roots.add(f"{parts[0]}.{parts[1]}")
    return roots


def _constructed_class_names(tree: ast.Module) -> set[str]:
    """Return every bare name directly called as ``Name(...)`` in *tree*.

    Catches ``LiteLLMProvider(...)`` and ``from x import LiteLLMProvider as Y;
    Y(...)`` would be missed, but no call site in this repo aliases a provider
    import, and the allowlist below is reviewed by file, not by alias.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            names.add(node.func.id)
    return names


class TestNoDirectProviderCallsOutsideLlm:
    def test_llm_capable_sdks_only_imported_inside_llm(self) -> None:
        offenders = [
            f"{path.relative_to(REPO_ROOT)} (imports {sorted(hit)})"
            for path, tree in _modules_outside_llm().items()
            for hit in [_imported_roots(tree) & _LLM_CAPABLE_IMPORT_ROOTS]
            if hit
            if not any(path in allowed for root, allowed in _ALLOWED_HTTP_IMPORT_USERS.items() if root in hit)
        ]
        assert offenders == [], (
            f"An LLM-capable SDK/HTTP client must only be imported inside sova/llm/: {offenders}. "
            "Route the call through sova.llm.client.invoke()/create_provider() instead, or if it is "
            "genuinely unrelated to any LLM provider, add it to _ALLOWED_HTTP_IMPORT_USERS deliberately (#924)."
        )

    def test_anthropic_endpoints_only_hardcoded_inside_llm(self) -> None:
        offenders = [
            f"{path.relative_to(REPO_ROOT)} ({fragment!r})"
            for path, text in _sources_outside_llm().items()
            for fragment in _ANTHROPIC_ENDPOINT_FRAGMENTS
            if fragment in text
        ]
        assert offenders == [], (
            f"An Anthropic/Vertex-for-Claude endpoint was hardcoded outside sova/llm/: {offenders}. "
            "Use create_provider()/LLMProvider.invoke() instead of a raw request (#924)."
        )

    def test_httpx_users_outside_llm_are_all_known_and_audited(self) -> None:
        allowed = _ALLOWED_HTTP_IMPORT_USERS["httpx"]
        actual = {path for path, tree in _modules_outside_llm().items() if "httpx" in _imported_roots(tree)}
        unexpected = actual - allowed
        assert unexpected == set(), (
            f"New httpx usage outside sova/llm/: {sorted(str(p) for p in unexpected)}. "
            "If this is a genuine LLM call, route it through create_provider() instead. "
            "If it is legitimate non-LLM HTTP (e.g. a REST integration), add it to "
            "_ALLOWED_HTTP_IMPORT_USERS deliberately (#924)."
        )
        missing = allowed - actual
        assert missing == set(), f"Allow-listed httpx users no longer import httpx, remove stale entries: {missing}"

    def test_provider_classes_only_constructed_inside_llm_or_audited_sites(self) -> None:
        offenders = [
            f"{path.relative_to(REPO_ROOT)} (constructs {sorted(hit)})"
            for path, tree in _modules_outside_llm().items()
            if path not in _ALLOWED_PROVIDER_CONSTRUCTION_USERS
            for hit in [_constructed_class_names(tree) & _PROVIDER_CLASS_NAMES]
            if hit
        ]
        assert offenders == [], (
            f"A concrete LLMProvider subclass was constructed directly outside sova/llm/: {offenders}. "
            "Route through sova.llm.provider.create_provider() instead, or add the call site to "
            "_ALLOWED_PROVIDER_CONSTRUCTION_USERS deliberately after confirming why it must construct "
            "a provider itself (#924)."
        )
        missing = {
            path
            for path in _ALLOWED_PROVIDER_CONSTRUCTION_USERS
            if path in _modules_outside_llm()
            and not (_constructed_class_names(_modules_outside_llm()[path]) & _PROVIDER_CLASS_NAMES)
        }
        assert missing == set(), (
            f"Allow-listed provider-construction users no longer do so, remove stale entries: {missing}"
        )

    def test_llm_suggestion_service_does_not_import_httpx(self) -> None:
        """The concrete bypass this issue fixed: previously built raw Anthropic/Vertex
        requests with httpx. Must now go through sova.llm.client.invoke()."""
        path = SOVA_ROOT / "dashboard" / "services" / "llm_suggestion_service.py"
        text = path.read_text(encoding="utf-8")
        assert "httpx" not in text
        assert "llm_invoke" in text

    def test_rebase_uses_llm_provider_invoke_not_raw_http(self) -> None:
        path = SOVA_ROOT / "git" / "rebase.py"
        text = path.read_text(encoding="utf-8")
        assert "httpx" not in text
        assert "import anthropic" not in text


class TestIsAnthropicModelId:
    @pytest.mark.parametrize("tier", ["opus", "sonnet", "haiku", "fast", "smart", "cheap"])
    def test_bare_tier_names_are_anthropic(self, tier: str) -> None:
        assert is_anthropic_model_id(tier) is True

    def test_claude_prefixed_id_is_anthropic(self) -> None:
        assert is_anthropic_model_id("claude-sonnet-4-6") is True

    def test_bedrock_dialect_ids_are_anthropic(self) -> None:
        assert is_anthropic_model_id("us.anthropic.claude-sonnet-4-5-20250929-v1:0") is True
        assert is_anthropic_model_id("anthropic.claude-haiku-4-5-20251001-v1:0") is True

    @pytest.mark.parametrize("region_prefix", ["eu.", "apac."])
    def test_non_us_bedrock_region_prefixed_ids_are_anthropic(self, region_prefix: str) -> None:
        """AWS also publishes "eu."/"apac." cross-region inference profiles, not just "us.";
        missing these let a Bedrock-pinned consensus model survive the rebase.py containment
        gate under a non-Anthropic-capable llm.provider (#924)."""
        assert is_anthropic_model_id(f"{region_prefix}anthropic.claude-sonnet-4-5-20250929-v1:0") is True

    def test_non_anthropic_model_is_not_anthropic(self) -> None:
        assert is_anthropic_model_id("gpt-5") is False
        assert is_anthropic_model_id("gemini-2.5-pro") is False
        assert is_anthropic_model_id("ollama/llama3.1") is False
        assert is_anthropic_model_id("vertex_ai/gemini-2.5-pro") is False

    @pytest.mark.parametrize(
        "model",
        ["anthropic/claude-sonnet-4-6", "vertex_ai/claude-sonnet-4-5@20250929", "bedrock/us.anthropic.claude-opus-4-1"],
    )
    def test_litellm_vendor_prefixed_claude_ids_are_anthropic(self, model: str) -> None:
        """A litellm route names its vendor in the model ID, so matching only the
        unprefixed form would read every correctly-prefixed Claude route as non-Anthropic."""
        assert is_anthropic_model_id(model) is True

    def test_multi_segment_litellm_route_with_anthropic_marker_is_anthropic(self) -> None:
        """A multi-hop litellm route (e.g. a proxy vendor fronting Anthropic) can carry the
        Anthropic marker one segment deeper than the first path component; checking only
        model.split("/", 1)[1] as a whole would miss this and silently reach Anthropic
        through an unrecognized "openrouter"-style route (#924)."""
        assert is_anthropic_model_id("openrouter/anthropic/claude-3-opus") is True

    def test_locally_served_model_named_claude_is_not_anthropic(self) -> None:
        """The "ollama/" prefix is rejected, not stripped: a local model is not Anthropic
        whatever it was named."""
        assert is_anthropic_model_id("ollama/claude-sonnet-4-6") is False

    def test_empty_string_is_not_anthropic(self) -> None:
        assert is_anthropic_model_id("") is False


class TestIsAnthropicCapable:
    def test_claude_code_always_capable(self) -> None:
        assert is_anthropic_capable(LLMConfig(provider="claude-code")) is True

    def test_anthropic_always_capable(self) -> None:
        assert is_anthropic_capable(LLMConfig(provider="anthropic", model="claude-sonnet-4-6")) is True

    def test_vertex_with_claude_model_is_capable(self) -> None:
        assert is_anthropic_capable(LLMConfig(provider="vertex", model="vertex_ai/claude-sonnet-4-5")) is True

    def test_vertex_with_non_anthropic_model_is_not_capable(self) -> None:
        """llm.provider="vertex" is generic LiteLLM Vertex routing, not Claude-only:
        _VENDOR_MODEL_EXAMPLES documents "vertex_ai/gemini-2.5-pro" as its example model."""
        assert is_anthropic_capable(LLMConfig(provider="vertex", model="vertex_ai/gemini-2.5-pro")) is False

    def test_litellm_with_anthropic_model_is_capable(self) -> None:
        assert is_anthropic_capable(LLMConfig(provider="litellm", model="claude-sonnet-4-6")) is True

    def test_hybrid_with_anthropic_model_is_capable(self) -> None:
        assert is_anthropic_capable(LLMConfig(provider="hybrid", model="claude-opus-4-1")) is True

    def test_litellm_with_non_anthropic_model_is_not_capable(self) -> None:
        assert is_anthropic_capable(LLMConfig(provider="litellm", model="gpt-5")) is False

    def test_openai_is_never_capable_even_with_anthropic_model_string(self) -> None:
        """A stale llm.model left over from a provider switch must not leak through:
        openai always forwards to OpenAI regardless of what cfg.model says (#924)."""
        assert is_anthropic_capable(LLMConfig(provider="openai", model="claude-sonnet-4-6")) is False

    def test_ollama_is_never_capable(self) -> None:
        assert is_anthropic_capable(LLMConfig(provider="ollama", model="ollama/claude-sonnet-4-6")) is False
