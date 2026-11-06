"""Configured model diagnostics use discovery without making config loading depend on it."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.config.models import ProjectConfig
from sova.dashboard.services import health_service, models_service
from sova.llm.client import clear_enumeration
from sova.llm.models import ModelFamily, ModelInfo
from sova.utils import diagnostics


def _model(model_id: str, source: str = "anthropic_api") -> ModelInfo:
    return ModelInfo(model_id, ModelFamily.ANTHROPIC, "", model_id, source)


def _unchecked(rows: list[diagnostics.DiagnosticCheck] | dict[str, diagnostics.DiagnosticCheck]) -> set[str]:
    """Fields the shared informational row lists as not checked; asserts that row never warns."""
    rows = list(rows.values()) if isinstance(rows, dict) else rows
    summary = [row for row in rows if row.name == "configured models"]
    if not summary:
        return set()
    (row,) = summary
    assert row.passed and not row.required
    fields: set[str] = set()
    for layer in row.detail.removeprefix("not checked (catalog unknown) for ").split("; "):
        fields.update(layer.partition(": ")[2].split(", "))
    return fields


def _only_unchecked(rows: list[diagnostics.DiagnosticCheck]) -> bool:
    """True when every row is the informational one: nothing was settled, so nothing warns."""
    return [row.name for row in rows] == ["configured models"] and bool(_unchecked(rows))


@pytest.fixture(autouse=True)
def isolate_catalogs() -> Iterator[None]:
    clear_enumeration()
    models_service._models_cache.clear()
    models_service._models_locks.clear()
    models_service._last_refresh_at.clear()
    health_service._health_checks_cache.clear()
    health_service._health_checks_locks.clear()
    yield
    clear_enumeration()
    models_service._models_cache.clear()
    models_service._models_locks.clear()
    models_service._last_refresh_at.clear()
    health_service._health_checks_cache.clear()
    health_service._health_checks_locks.clear()


@pytest.fixture
def discovery() -> Iterator[tuple[ProjectConfig, MagicMock, AsyncMock]]:
    cfg = ProjectConfig()
    cfg.llm.provider = "anthropic"
    cfg.llm.model = "claude-present"
    cfg.agent.runtime = "aider"
    provider = MagicMock()
    provider.list_available_models = AsyncMock(return_value=[_model("claude-present")])
    runtime = AsyncMock(return_value={"source": "runtime", "models": [{"id": "runtime-present"}]})
    # These collaborators are imported inside the diagnostic, so patch their source modules.
    with (
        patch("sova.config.loader.load_config", return_value=cfg),
        patch("sova.llm.provider.create_provider", return_value=provider),
        patch.object(models_service, "get_available_models", runtime),
    ):
        yield cfg, provider, runtime


class TestConfiguredModels:
    async def test_valid_and_stale_ids(self, tmp_path: Path, discovery: tuple) -> None:
        cfg, provider, _ = discovery
        cfg.llm.fallback_model = "claude-removed"
        rows = {row.name: row for row in await diagnostics._check_configured_models(tmp_path)}
        assert rows["model: llm.model"].passed
        stale = rows["model: llm.fallback_model"]
        assert not stale.passed and not stale.required
        assert "not found" in stale.detail and "anthropic" in stale.detail and "claude-removed" in stale.detail
        assert all(not row.required for row in rows.values())
        provider.list_available_models.assert_awaited_once_with(allow_probe=True)

    async def test_all_fields_and_aliases(self, tmp_path: Path, discovery: tuple) -> None:
        cfg, _, _ = discovery
        cfg.llm.model_aliases = {"chosen": "claude-present"}
        cfg.llm.model = cfg.llm.fallback_model = "chosen"
        cfg.llm.routing = {"triage": "chosen", "complex": "chosen"}
        cfg.agent.model = "chosen"
        cfg.agent.fallback_models = ["chosen", "claude-removed"]
        cfg.oversight.analysis_model = "chosen"
        cfg.confidence.model = "chosen"
        cfg.review.panel.dimension_models = {"security": "chosen"}
        # Rebase consensus builds its own provider per entry, so llm.provider's catalog cannot judge it.
        cfg.conflict_resolution.models = ["claude-removed"]
        role_fields = [name for name in type(cfg.roles).model_fields if name.endswith("_model")]
        for field in role_fields:
            setattr(cfg.roles, field, "runtime-present" if field.endswith("_runtime_model") else "chosen")
        rows = {row.name: row for row in await diagnostics._check_configured_models(tmp_path)}
        expected = {
            "llm.model",
            "llm.fallback_model",
            "llm.routing.triage",
            "llm.routing.complex",
            "llm.model_aliases.chosen",
            "agent.model",
            "agent.fallback_models[0]",
            "oversight.analysis_model",
            "confidence.model",
            "review.panel.dimension_models.security",
            *(f"roles.{field}" for field in role_fields),
        }
        assert all(rows[f"model: {field}"].passed for field in expected)
        assert not rows["model: agent.fallback_models[1]"].passed
        assert "aider" in rows["model: roles.developer_runtime_model"].detail
        assert "model: codex.model" not in rows
        assert not any(name.startswith("model: conflict_resolution") for name in rows)

    async def test_backend_scoped_aliases(self, tmp_path: Path, discovery: tuple) -> None:
        cfg, _, _ = discovery
        cfg.llm.model_aliases = {"firstparty:chosen": "claude-present", "vertex:chosen": "unavailable-elsewhere"}
        cfg.llm.model = "chosen"
        rows = {row.name: row for row in await diagnostics._check_configured_models(tmp_path)}
        assert rows["model: llm.model"].passed
        assert rows["model: llm.model_aliases.firstparty:chosen"].passed
        # Never used on this backend, so neither a warning nor a "not checked" entry.
        assert "model: llm.model_aliases.vertex:chosen" not in rows
        assert "llm.model_aliases.vertex:chosen" not in _unchecked(rows)

    async def test_alias_to_bare_tier_is_normalized_like_the_provider(self, tmp_path: Path, discovery: tuple) -> None:
        cfg, provider, _ = discovery
        cfg.llm.model_aliases = {"chosen": "sonnet"}
        cfg.llm.model = "chosen"
        provider.list_available_models.return_value = [_model("claude-sonnet-5-5")]
        rows = {row.name: row for row in await diagnostics._check_configured_models(tmp_path)}
        assert rows["model: llm.model"].passed
        assert rows["model: llm.model_aliases.chosen"].passed

    async def test_bare_tier_without_tier_resolution_is_normalized(self, tmp_path: Path, discovery: tuple) -> None:
        cfg, provider, _ = discovery
        cfg.llm.resolve_tier_aliases = False
        cfg.llm.model = "sonnet"
        provider.list_available_models.return_value = [_model("claude-sonnet-5-5")]
        rows = {row.name: row for row in await diagnostics._check_configured_models(tmp_path)}
        assert rows["model: llm.model"].passed

    async def test_scoped_alias_shadows_bare_key(self, tmp_path: Path, discovery: tuple) -> None:
        cfg, _, _ = discovery
        cfg.llm.model_aliases = {"chosen": "shadowed-bare", "firstparty:chosen": "claude-present"}
        cfg.llm.model = "chosen"
        rows = {row.name: row for row in await diagnostics._check_configured_models(tmp_path)}
        assert rows["model: llm.model_aliases.firstparty:chosen"].passed
        assert "model: llm.model_aliases.chosen" not in rows

    async def test_undated_anthropic_alias_matches_dated_catalog_id(self, tmp_path: Path, discovery: tuple) -> None:
        cfg, provider, _ = discovery
        provider.list_available_models.return_value = [_model("claude-sonnet-4-5-20250929")]
        cfg.llm.model = "claude-sonnet-4-5"
        cfg.llm.fallback_model = "claude-sonnet-4"
        rows = {row.name: row for row in await diagnostics._check_configured_models(tmp_path)}
        assert rows["model: llm.model"].passed
        assert not rows["model: llm.fallback_model"].passed

    async def test_floating_alias_does_not_match_another_model_line(self, tmp_path: Path, discovery: tuple) -> None:
        cfg, provider, _ = discovery
        provider.list_available_models.return_value = [_model("claude-sonnet-4-5-20250929")]
        cfg.llm.model = "claude-sonnet-4-0"
        rows = {row.name: row for row in await diagnostics._check_configured_models(tmp_path)}
        assert not rows["model: llm.model"].passed

    async def test_chained_alias_target_is_not_resolved_a_second_time(self, tmp_path: Path, discovery: tuple) -> None:
        cfg, _, _ = discovery
        cfg.llm.model_aliases = {"a": "b", "b": "claude-present"}
        cfg.llm.model = "a"
        rows = {row.name: row for row in await diagnostics._check_configured_models(tmp_path)}
        # A field set to "a" sends "b", which the catalog lacks; so does the "a" row.
        assert not rows["model: llm.model"].passed
        assert not rows["model: llm.model_aliases.a"].passed
        assert rows["model: llm.model_aliases.b"].passed

    async def test_reviewer_tier_follows_the_reviewers_provider_fallback(
        self, tmp_path: Path, discovery: tuple
    ) -> None:
        cfg, provider, _ = discovery
        cfg.llm.provider = "openai"
        cfg.llm.model = "gpt-present"
        cfg.roles.reviewer_model = "sonnet"
        provider.list_available_models.return_value = [_model("gpt-present", "openai_compatible")]
        rows = {row.name: row for row in await diagnostics._check_configured_models(tmp_path)}
        # ReviewerRole sends llm.model for a bare tier on this provider, never "sonnet".
        assert rows["model: roles.reviewer_model"].passed, rows["model: roles.reviewer_model"].detail
        assert "gpt-present" in rows["model: roles.reviewer_model"].detail

    async def test_unscoped_alias_key_with_a_colon_is_checked(self, tmp_path: Path, discovery: tuple) -> None:
        cfg, _, _ = discovery
        cfg.llm.model_aliases = {"qwen3:8b": "claude-present"}
        rows = {row.name: row for row in await diagnostics._check_configured_models(tmp_path)}
        assert rows["model: llm.model_aliases.qwen3:8b"].passed

    @pytest.mark.parametrize("catalog", [[], [_model("claude-present", "curated")]])
    async def test_curated_and_empty_catalogs_are_unknown(
        self, tmp_path: Path, discovery: tuple, catalog: list
    ) -> None:
        _, provider, _ = discovery
        provider.list_available_models.return_value = catalog
        rows = await diagnostics._check_configured_models(tmp_path)
        assert _only_unchecked(rows)
        assert "llm.model" in _unchecked(rows)
        assert all("not found" not in row.detail for row in rows)

    async def test_default_config_reports_one_informational_row(self, tmp_path: Path) -> None:
        # The default claude-code provider never probes, so nothing is settled: no per-field warnings.
        with patch("sova.config.loader.load_config", return_value=ProjectConfig()):
            rows = await diagnostics._check_configured_models(tmp_path)
        assert _only_unchecked(rows)
        assert {"agent.model", "roles.reviewer_model", "oversight.analysis_model"} <= _unchecked(rows)

    async def test_unloadable_config_yields_no_rows(self, tmp_path: Path) -> None:
        with patch("sova.config.loader.load_config", side_effect=ValueError("bad config")):
            assert await diagnostics._check_configured_models(tmp_path) == []

    async def test_failures_are_isolated_and_redacted(self, tmp_path: Path, discovery: tuple) -> None:
        cfg, provider, _ = discovery
        provider.list_available_models.side_effect = RuntimeError("secret-api-key=https://secret.invalid")
        cfg.roles.developer_runtime_model = "runtime-present"
        rows = {row.name: row for row in await diagnostics._check_configured_models(tmp_path)}
        assert "llm.model" in _unchecked(rows)
        assert "secret" not in str(rows)
        assert rows["model: roles.developer_runtime_model"].passed

    @pytest.mark.parametrize("source", ["unavailable", "reachable"])
    async def test_runtime_unknown_does_not_hide_provider(self, tmp_path: Path, discovery: tuple, source: str) -> None:
        cfg, _, runtime = discovery
        cfg.roles.developer_runtime_model = "runtime-missing"
        runtime.return_value = {"source": source, "models": []}
        rows = {row.name: row for row in await diagnostics._check_configured_models(tmp_path)}
        assert rows["model: llm.model"].passed
        assert "roles.developer_runtime_model" in _unchecked(rows)

    async def test_runtime_pin_is_compared_verbatim(self, tmp_path: Path, discovery: tuple) -> None:
        cfg, _, _ = discovery
        cfg.roles.developer_runtime_model = "runtime-present"
        rows = {row.name: row for row in await diagnostics._check_configured_models(tmp_path)}
        assert rows["model: roles.developer_runtime_model"].passed
        cfg.roles.developer_runtime_model = "runtime-typo"
        rows = {row.name: row for row in await diagnostics._check_configured_models(tmp_path)}
        row = rows["model: roles.developer_runtime_model"]
        assert not row.passed and not row.required and "not found" in row.detail and "aider" in row.detail
        # resolve_runtime_model_pin() hands the runtime the raw pin, so an llm.model_aliases
        # entry must not make it look valid: the runtime would still receive "runtime-alias".
        cfg.llm.model_aliases = {"runtime-alias": "runtime-present"}
        cfg.roles.developer_runtime_model = "runtime-alias"
        rows = {row.name: row for row in await diagnostics._check_configured_models(tmp_path)}
        assert not rows["model: roles.developer_runtime_model"].passed

    async def test_codex_echo_is_not_validation(self, tmp_path: Path, discovery: tuple) -> None:
        cfg, _, runtime = discovery
        cfg.agent.runtime = "codex"
        cfg.codex.model = "codex-typo"
        cfg.roles.developer_runtime_model = "ignored-pin"
        runtime.return_value = {"source": "runtime", "models": [{"id": "codex-typo"}]}
        rows = {row.name: row for row in await diagnostics._check_configured_models(tmp_path)}
        assert {"codex.model", "roles.developer_runtime_model"} <= _unchecked(rows)
        assert "model: codex.model" not in rows

    async def test_curated_runtime_is_unknown(self, tmp_path: Path, discovery: tuple) -> None:
        cfg, _, _ = discovery
        cfg.agent.runtime = "claude-code"
        cfg.roles.developer_runtime_model = "claude-future"
        rows = {row.name: row for row in await diagnostics._check_configured_models(tmp_path)}
        assert "roles.developer_runtime_model" in _unchecked(rows)

    async def test_claude_provider_never_runs_paid_probes(self, tmp_path: Path, discovery: tuple) -> None:
        cfg, provider, _ = discovery
        cfg.llm.provider = "claude-code"
        provider.list_available_models.return_value = [_model("claude-present", "curated")]
        await diagnostics._check_configured_models(tmp_path)
        provider.list_available_models.assert_awaited_once_with(allow_probe=False)

    async def test_incomplete_catalog_does_not_reject_other_backends(self, tmp_path: Path, discovery: tuple) -> None:
        cfg, provider, _ = discovery
        cfg.llm.provider = "litellm"
        cfg.llm.model = "ollama/local"
        cfg.llm.fallback_model = "vertex_ai/claude-missing"
        provider.list_available_models.return_value = [_model("ollama/local:latest", "ollama")]
        rows = {row.name: row for row in await diagnostics._check_configured_models(tmp_path)}
        assert rows["model: llm.model"].passed
        assert "llm.fallback_model" in _unchecked(rows)

    @pytest.mark.parametrize(
        ("configured", "installed", "available"),
        [
            ("ollama/registry.local:5000/team/model", "ollama/registry.local:5000/team/model:latest", True),
            ("ollama/team/model:70b", "ollama/team/model:latest", False),
            ("ollama/team/model:70b", "ollama/team/model:70b", True),
        ],
    )
    async def test_ollama_default_tag_preserves_explicit_tags(
        self, tmp_path: Path, discovery: tuple, configured: str, installed: str, available: bool
    ) -> None:
        cfg, provider, _ = discovery
        cfg.llm.provider = "ollama"
        cfg.llm.model = configured
        provider.list_available_models.return_value = [_model(installed, "ollama")]
        rows = {row.name: row for row in await diagnostics._check_configured_models(tmp_path)}
        assert rows["model: llm.model"].passed is available

    async def test_builtin_tier_alias_is_resolved(self, tmp_path: Path, discovery: tuple) -> None:
        from sova.llm.models import resolve_model_alias

        cfg, provider, _ = discovery
        cfg.llm.model = "sonnet"
        provider.list_available_models.return_value = [_model(resolve_model_alias("sonnet"))]
        rows = {row.name: row for row in await diagnostics._check_configured_models(tmp_path)}
        assert rows["model: llm.model"].passed
        assert resolve_model_alias("sonnet") in rows["model: llm.model"].detail

    async def test_layers_do_not_validate_each_others_models(self, tmp_path: Path, discovery: tuple) -> None:
        cfg, _, _ = discovery
        cfg.llm.model = "runtime-present"
        cfg.roles.developer_runtime_model = "claude-present"
        rows = {row.name: row for row in await diagnostics._check_configured_models(tmp_path)}
        for field in ("llm.model", "roles.developer_runtime_model"):
            row = rows[f"model: {field}"]
            assert not row.passed
            assert not row.required
            assert "not found" in row.detail

    @pytest.mark.parametrize("catalog_id", ["gpt-present", "openai/gpt-present"])
    async def test_openai_prefix_preserves_exact_catalog_ids(
        self, tmp_path: Path, discovery: tuple, catalog_id: str
    ) -> None:
        cfg, provider, _ = discovery
        cfg.llm.provider = "openai"
        cfg.llm.model = "openai/gpt-present"
        provider.list_available_models.return_value = [_model(catalog_id, "openai_compatible")]
        rows = {row.name: row for row in await diagnostics._check_configured_models(tmp_path)}
        assert rows["model: llm.model"].passed

    async def test_curated_entry_in_mixed_catalog_is_unknown(self, tmp_path: Path, discovery: tuple) -> None:
        cfg, provider, _ = discovery
        cfg.llm.model = "claude-curated"
        provider.list_available_models.return_value = [
            _model("claude-present"),
            _model("claude-curated", "curated"),
        ]
        rows = {row.name: row for row in await diagnostics._check_configured_models(tmp_path)}
        assert "model: llm.model" not in rows
        assert "llm.model" in _unchecked(rows)

    async def test_vertex_catalog_is_scoped_to_publisher(self, tmp_path: Path, discovery: tuple) -> None:
        cfg, provider, _ = discovery
        cfg.llm.provider = "vertex"
        cfg.llm.model = "vertex_ai/claude-present"
        cfg.llm.fallback_model = "vertex_ai/gemini-missing"
        provider.list_available_models.return_value = [_model("claude-present", "vertex")]
        rows = {row.name: row for row in await diagnostics._check_configured_models(tmp_path)}
        assert rows["model: llm.model"].passed
        assert "llm.fallback_model" in _unchecked(rows)

    @pytest.mark.parametrize(
        ("provider_name", "configured", "listed", "source"),
        [
            ("openai", "openai/gpt-6.1-sol", "gpt-6.1-sol", "openai_compatible"),
            ("openai", "gpt-6.1-sol", "gpt-6.1-sol", "openai_compatible"),
            ("anthropic", "claude-sonnet-5-5", "claude-sonnet-5-5", "anthropic_api"),
            # The documented floating aliases resolve to a dated catalog ID.
            ("anthropic", "claude-sonnet-4-0", "claude-sonnet-4-20250514", "anthropic_api"),
            ("anthropic", "claude-3-5-haiku-latest", "claude-3-5-haiku-20241022", "anthropic_api"),
            # A Vertex publisher catalog lists the model, not its @-pinned snapshot.
            ("vertex", "claude-sonnet-4-5@20250929", "claude-sonnet-4-5", "vertex"),
            ("vertex", "vertex_ai/claude-sonnet-4-5@20250929", "claude-sonnet-4-5", "vertex"),
        ],
    )
    async def test_real_model_ids_are_not_flagged(
        self, tmp_path: Path, discovery: tuple, provider_name: str, configured: str, listed: str, source: str
    ) -> None:
        cfg, provider, _ = discovery
        cfg.llm.provider = provider_name
        cfg.llm.model = configured
        provider.list_available_models.return_value = [_model(listed, source)]
        rows = {row.name: row for row in await diagnostics._check_configured_models(tmp_path)}
        assert rows["model: llm.model"].passed, rows["model: llm.model"].detail

    async def test_transient_failure_retries(self, tmp_path: Path, discovery: tuple) -> None:
        _, provider, _ = discovery
        provider.list_available_models.side_effect = [TimeoutError(), [_model("claude-present")]]
        first = {row.name: row for row in await diagnostics._check_configured_models(tmp_path)}
        second = {row.name: row for row in await diagnostics._check_configured_models(tmp_path)}
        assert "llm.model" in _unchecked(first)
        assert second["model: llm.model"].passed

    async def test_guard_registration(self, tmp_path: Path) -> None:
        with patch.object(diagnostics, "_check_configured_models", side_effect=RuntimeError("broken")) as check:
            rows = await diagnostics.run_all_checks(tmp_path)
        check.assert_called_once_with(tmp_path)
        row = next(row for row in rows if row.name == "configured models")
        assert not row.passed and not row.required

    async def test_cancellation_propagates(self, tmp_path: Path, discovery: tuple) -> None:
        _, provider, _ = discovery
        provider.list_available_models.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await diagnostics._check_configured_models(tmp_path)

    async def test_timeout_is_unknown(self, tmp_path: Path, discovery: tuple) -> None:
        _, provider, _ = discovery

        async def stalled_catalog(**kwargs: object) -> list[ModelInfo]:
            await asyncio.Event().wait()
            return []

        provider.list_available_models.side_effect = stalled_catalog
        # The timeout constant is imported inside the catalog helper.
        with patch("sova.llm.provider.CAPABILITY_PROBE_TIMEOUT", 0.01):
            rows = await diagnostics._check_configured_models(tmp_path)
        assert _only_unchecked(rows)

    @pytest.mark.parametrize("error", [ValueError("bad provider"), ImportError("missing SDK")])
    async def test_provider_construction_failure_is_unknown(
        self, tmp_path: Path, discovery: tuple, error: Exception
    ) -> None:
        # Factory import is local to the catalog helper.
        with patch("sova.llm.provider.create_provider", side_effect=error):
            rows = await diagnostics._check_configured_models(tmp_path)
        assert _only_unchecked(rows)

    async def test_runtime_failure_is_unknown(self, tmp_path: Path, discovery: tuple) -> None:
        cfg, _, runtime = discovery
        cfg.roles.developer_runtime_model = "runtime-present"
        runtime.side_effect = RuntimeError("credential-must-not-leak")
        rows = {row.name: row for row in await diagnostics._check_configured_models(tmp_path)}
        assert rows["model: llm.model"].passed
        assert "roles.developer_runtime_model" in _unchecked(rows)
        assert "credential-must-not-leak" not in str(rows)


class TestDiscoveryIntegration:
    async def test_offline_ollama_is_unknown(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import sova.llm.litellm_provider as litellm_provider
        from sova.llm.litellm_provider import LiteLLMProvider

        # CI does not install the optional litellm extra; every enumeration call below is patched.
        monkeypatch.setattr(litellm_provider, "_HAS_LITELLM", True)
        cfg = ProjectConfig()
        cfg.llm.provider = "ollama"
        cfg.llm.model = "ollama/local-typo"
        provider = LiteLLMProvider(model=cfg.llm.model, vendor="ollama")
        # Factory/config are imported locally; retain the provider's real fallback path.
        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch("sova.llm.provider.create_provider", return_value=provider),
            patch.object(provider, "_enumerate_vertex", AsyncMock(return_value=None)),
            patch.object(provider, "_enumerate_ollama", AsyncMock(return_value=[])),
            patch.object(provider, "_enumerate_openai_compatible", AsyncMock(return_value=None)),
        ):
            rows = await diagnostics._check_configured_models(tmp_path)
        assert _only_unchecked(rows)

    async def test_repeated_health_loads_share_provider_and_runtime_caches(self, tmp_path: Path) -> None:
        from sova.ipc.runtime import AiderRuntime
        from sova.llm.providers import anthropic_api

        probe = AsyncMock(return_value=["runtime-present"])

        class CatalogRuntime(AiderRuntime):
            """Hypothetical runtime with an independent catalog: no shipped runtime exposes one today."""

            async def check_available(self) -> tuple[bool, str]:
                return True, "available"

            async def _probe_models(self) -> list[str] | None:
                return await probe()

        cfg = ProjectConfig()
        cfg.llm.provider = "anthropic"
        cfg.llm.api_key = "doctor-test-key"
        cfg.llm.model = "claude-present"
        cfg.agent.runtime = "aider"
        cfg.roles.developer_runtime_model = "runtime-present"
        fetch = AsyncMock(return_value=[_model("claude-present")])
        # The factories/config are imported locally by diagnostics and models_service.
        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch.object(anthropic_api, "_HAS_ANTHROPIC", True),
            patch.object(anthropic_api.AnthropicAPIProvider, "_fetch_sdk_models", fetch),
            patch("sova.ipc.runtime.create_runtime", return_value=CatalogRuntime()),
            patch.object(health_service, "run_all_checks", side_effect=diagnostics._check_configured_models) as checks,
        ):
            first = await health_service.get_health_checks(tmp_path)
            second = await health_service.get_health_checks(tmp_path)
            # The health TTL expires before the model enumeration TTLs.
            health_service._health_checks_cache.clear()
            third = await health_service.get_health_checks(tmp_path)
        assert first["checks"] == second["checks"] == third["checks"]
        assert second["cached"]
        assert not third["cached"]
        assert checks.await_count == 2
        rows = {row["name"]: row for row in third["checks"]}
        assert rows["model: llm.model"]["passed"]
        assert rows["model: roles.developer_runtime_model"]["passed"]
        fetch.assert_awaited_once()
        probe.assert_awaited_once()

    async def test_runtime_none_is_unknown(self, tmp_path: Path) -> None:
        from sova.ipc.runtime import AiderRuntime

        cfg = ProjectConfig()
        cfg.agent.runtime = "aider"
        cfg.roles.developer_runtime_model = "runtime-present"
        # Runtime construction is local to models_service; use its real enumeration/cache path.
        with (
            patch("sova.config.loader.load_config", return_value=cfg),
            patch.object(AiderRuntime, "check_available", AsyncMock(return_value=(False, "unauthenticated"))),
        ):
            rows = await diagnostics._check_configured_models(tmp_path)
        assert "roles.developer_runtime_model" in _unchecked(rows)
        assert not any(row.name == "model: roles.developer_runtime_model" for row in rows)

    @pytest.mark.parametrize("provider_name", ["anthropic", "ollama", "openai", "claude-code"])
    def test_config_loading_never_enumerates(self, tmp_path: Path, seed_config, provider_name: str) -> None:
        from sova.config.loader import load_config

        seed_config(tmp_path, {"llm.provider": provider_name, "llm.model": "model-typo"})
        # These factories would precede any catalog I/O, and must never run during config load.
        with (
            patch("sova.llm.provider.create_provider") as provider,
            patch("sova.ipc.runtime.create_runtime") as runtime,
        ):
            cfg = load_config(tmp_path)
        assert cfg.llm.model == "model-typo"
        provider.assert_not_called()
        runtime.assert_not_called()
