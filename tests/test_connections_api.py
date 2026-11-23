"""Connections, auth status, reconnect flows and available-models API routes.

Split from tests/test_dashboard.py; see docs/testing-guidelines.md, "Test File Layout".
"""

from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from sova.llm.errors import ComponentMisconfiguredError
from tests.conftest import csrf_request_headers as _csrf_request_headers
from tests.dashboard_helpers import NO_CSRF

# Uses the shared per-worker in-memory DB, emptied before each test
# (see ``tests/conftest.py:reset_db_rows``). A test here that leaves a
# background task touching the DB when its event loop closes (the real
# ``start_agent()`` calls in ``TestDuplicateAgentPrevention``) can invalidate the
# shared connection; the next test's sweep recreates the schema.
pytestmark = pytest.mark.usefixtures("reset_db_rows")


# ---------------------------------------------------------------------------
# Auth status API
# ---------------------------------------------------------------------------


class _FakeAuthProvider:
    """Fake claude-code-shaped provider: exposes get_auth_details()."""

    def __init__(self, authenticated: bool, detail: str, account: dict | None) -> None:
        self._authenticated = authenticated
        self._detail = detail
        self._account = account

    async def check_available(self):
        return self._authenticated, self._detail

    async def get_auth_details(self):
        return self._account


class _FakeNoAuthDetailsProvider:
    """Fake non-claude-code provider: has no get_auth_details() at all."""

    def __init__(self, authenticated: bool, detail: str) -> None:
        self._authenticated = authenticated
        self._detail = detail

    async def check_available(self):
        return self._authenticated, self._detail


def _fake_cfg(provider: str = "claude-code", api_key: str = ""):
    from types import SimpleNamespace

    from sova.config.models import LLMConfig

    return SimpleNamespace(llm=LLMConfig(provider=provider, api_key=api_key, model="claude-sonnet-4-6"))


def _fake_full_cfg(llm_provider: str = "claude-code", agent_runtime: str = "claude-code"):
    """Like _fake_cfg, plus the `agent`/`codex` sections the Connections
    catalog and runtime candidates read (cfg.agent.runtime, cfg.codex)."""
    from types import SimpleNamespace

    from sova.config.models import LLMConfig

    return SimpleNamespace(
        llm=LLMConfig(provider=llm_provider, api_key="", model="claude-sonnet-4-6"),
        agent=SimpleNamespace(runtime=agent_runtime),
        codex=None,
    )


class TestBuildLLMCandidateProviderLegacyFallbackScoping:
    """_build_llm_candidate_provider()'s legacy/db-value fallback must only
    apply to the currently-active provider, never an inactive candidate
    being probed (issue #1148): the legacy unscoped keyring entry and the
    plaintext cfg.llm.api_key database row both belong to whichever
    provider was active when they were written, not to every candidate."""

    def test_inactive_anthropic_candidate_does_not_use_legacy_fallback(self, tmp_path: Path) -> None:
        from sova.dashboard.services.setup_service import _build_llm_candidate_provider
        from sova.llm import keyring_store
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        cfg = _fake_cfg(provider="openai", api_key="sk-openai-active-db-value")
        with (
            patch.object(keyring_store, "resolve_secret", wraps=keyring_store.resolve_secret) as mock_resolve,
            patch.object(AnthropicAPIProvider, "__init__", return_value=None),
        ):
            _build_llm_candidate_provider("anthropic", cfg, tmp_path)

        # "anthropic" is not cfg.llm.provider ("openai" is active), so
        # neither the legacy unscoped entry nor the active provider's own
        # database value may be consulted for this probe.
        mock_resolve.assert_called_once()
        _name, db_value = mock_resolve.call_args[0]
        assert db_value is None
        assert mock_resolve.call_args.kwargs.get("legacy_name") is None

    def test_active_anthropic_candidate_still_uses_legacy_fallback(self, tmp_path: Path) -> None:
        from sova.dashboard.services.setup_service import _build_llm_candidate_provider
        from sova.llm import keyring_store
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        cfg = _fake_cfg(provider="anthropic", api_key="sk-anthropic-active-db-value")
        with (
            patch.object(keyring_store, "resolve_secret", wraps=keyring_store.resolve_secret) as mock_resolve,
            patch.object(AnthropicAPIProvider, "__init__", return_value=None),
        ):
            _build_llm_candidate_provider("anthropic", cfg, tmp_path)

        mock_resolve.assert_called_once()
        _name, db_value = mock_resolve.call_args[0]
        assert db_value == "sk-anthropic-active-db-value"
        assert mock_resolve.call_args.kwargs.get("legacy_name") == "llm.api_key"

    def test_inactive_openai_candidate_does_not_use_legacy_fallback(self, tmp_path: Path) -> None:
        from sova.dashboard.services.setup_service import _build_llm_candidate_provider
        from sova.llm import keyring_store
        from sova.llm.litellm_provider import LiteLLMProvider

        cfg = _fake_cfg(provider="anthropic", api_key="sk-anthropic-active-db-value")
        with (
            patch.object(keyring_store, "resolve_secret", wraps=keyring_store.resolve_secret) as mock_resolve,
            patch.object(LiteLLMProvider, "__init__", return_value=None),
        ):
            _build_llm_candidate_provider("openai", cfg, tmp_path)

        mock_resolve.assert_called_once()
        _name, db_value = mock_resolve.call_args[0]
        assert db_value is None
        assert mock_resolve.call_args.kwargs.get("legacy_name") is None


class TestAuthStatusAPI:
    @pytest.fixture(autouse=True)
    def _clear_auth_cache(self):
        from sova.dashboard.services.setup_service import _auth_status_cache

        _auth_status_cache.clear()
        yield
        _auth_status_cache.clear()

    async def test_authenticated_claude_code_response_shape(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        account = {
            "loggedIn": True,
            "email": "user@example.com",
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
            "orgId": "org_123",
            "orgName": "Example Org",
            "subscriptionType": "max",
        }
        provider = _FakeAuthProvider(True, "2.1.259 (user@example.com, max)", account)
        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=_fake_cfg()),
            patch("sova.llm.provider.create_provider", return_value=provider),
        ):
            resp = await client.get("/api/auth/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["provider"] == "claude-code"
        assert data["authenticated"] is True
        assert data["detail"] == "2.1.259 (user@example.com, max)"
        assert data["account"]["email"] == "user@example.com"
        assert "loggedIn" not in data["account"]
        assert data["api_key_configured"] is False
        assert data["routing_warnings"] == []
        assert data["cached"] is False
        assert "checked_at" in data

    async def test_unauthenticated_response_has_no_invented_account(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        provider = _FakeAuthProvider(False, "not authenticated (run: claude auth login)", None)
        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=_fake_cfg()),
            patch("sova.llm.provider.create_provider", return_value=provider),
        ):
            resp = await client.get("/api/auth/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is False
        assert data["account"] is None
        assert data["detail"] == "not authenticated (run: claude auth login)"

    async def test_fail_open_availability_is_not_reported_as_authenticated(self, client: AsyncClient) -> None:
        """check_available() fails open (reports available with "auth state
        unknown") so a CLI build without the auth subcommand doesn't block
        agent spawning. The widget must not read that fail-open availability
        as a confirmed login: only a get_auth_details() payload counts here."""
        from unittest.mock import patch

        provider = _FakeAuthProvider(True, "2.1.259 (auth state unknown)", None)
        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=_fake_cfg()),
            patch("sova.llm.provider.create_provider", return_value=provider),
        ):
            resp = await client.get("/api/auth/status")
        data = resp.json()
        assert data["authenticated"] is False
        assert data["account"] is None
        assert "auth state unknown" in data["detail"]

    async def test_non_claude_code_provider_has_no_account_field(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        provider = _FakeNoAuthDetailsProvider(True, "ollama available")
        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=_fake_cfg(provider="ollama")),
            patch("sova.llm.provider.create_provider", return_value=provider),
        ):
            resp = await client.get("/api/auth/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["provider"] == "ollama"
        assert data["authenticated"] is True
        assert data["account"] is None

    async def test_cache_hit_returns_cached_true(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        provider = _FakeAuthProvider(True, "2.1.259 (user@example.com, max)", None)
        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=_fake_cfg()) as mock_load,
            patch("sova.llm.provider.create_provider", return_value=provider),
        ):
            first = await client.get("/api/auth/status")
            second = await client.get("/api/auth/status")
        assert first.json()["cached"] is False
        assert second.json()["cached"] is True
        assert second.json()["checked_at"] == first.json()["checked_at"]
        assert mock_load.call_count == 1

    async def test_concurrent_uncached_requests_share_one_probe(self, client: AsyncClient) -> None:
        """Two concurrent requests racing a cold/expired cache must not each
        spawn their own probe: the single-flight lock (mirroring
        coderabbit_quota.py's per-repo lock) makes the second one wait for and
        reuse the first's result instead of duplicating CLI subprocess calls."""
        import asyncio
        from unittest.mock import patch

        probe_count = 0

        class _SlowAuthProvider:
            async def check_available(self):
                nonlocal probe_count
                probe_count += 1
                await asyncio.sleep(0.05)
                return True, "2.1.259 (user@example.com, max)"

        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=_fake_cfg()),
            patch("sova.llm.provider.create_provider", return_value=_SlowAuthProvider()),
        ):
            first, second = await asyncio.gather(
                client.get("/api/auth/status"),
                client.get("/api/auth/status"),
            )

        assert first.status_code == 200
        assert second.status_code == 200
        assert probe_count == 1
        assert first.json()["authenticated"] is True
        assert second.json()["authenticated"] is True

    async def test_routing_variable_warning_detected(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        provider = _FakeAuthProvider(True, "2.1.259 (user@example.com, max)", None)
        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=_fake_cfg()),
            patch("sova.llm.provider.create_provider", return_value=provider),
            patch.dict(os.environ, {"ANTHROPIC_VERTEX_PROJECT_ID": "some-project"}),
        ):
            resp = await client.get("/api/auth/status")
        data = resp.json()
        assert "ANTHROPIC_VERTEX_PROJECT_ID" in data["routing_warnings"]

    async def test_misconfigured_provider_returns_200(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=_fake_cfg(provider="claude-code")),
            patch("sova.llm.provider.create_provider", side_effect=ValueError("Unknown LLM provider")),
        ):
            resp = await client.get("/api/auth/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["authenticated"] is False
        assert data["account"] is None
        assert "Unknown LLM provider" in data["detail"]

    async def test_config_load_failure_returns_503(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        with patch("sova.dashboard.services.setup_service.load_config", side_effect=RuntimeError("config unreadable")):
            resp = await client.get("/api/auth/status")
        assert resp.status_code == 503
        assert "config unreadable" not in resp.text

    async def test_no_expires_at_or_models_fields(self, client: AsyncClient) -> None:
        """Neither the envelope nor the account carries expires_at/models.

        The account payload is whatever the CLI printed, so the fields that
        must never appear have to be asserted against an account that actually
        contains them, not against an envelope that never had them.
        """
        from unittest.mock import patch

        account = {"loggedIn": True, "email": "u@e.com", "expires_at": "2026-01-01", "models": ["opus"]}
        provider = _FakeAuthProvider(True, "2.1.259 (user@example.com, max)", account)
        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=_fake_cfg()),
            patch("sova.llm.provider.create_provider", return_value=provider),
        ):
            resp = await client.get("/api/auth/status")
        data = resp.json()
        assert "expires_at" not in data
        assert "models" not in data
        assert "expires_at" not in data["account"]
        assert "models" not in data["account"]
        assert data["account"] == {"email": "u@e.com"}

    async def test_account_drops_unknown_fields(self, client: AsyncClient) -> None:
        """A field outside the verified contract is never echoed to the client."""
        from unittest.mock import patch

        account = {"loggedIn": True, "email": "u@e.com", "accessToken": "sk-ant-secret"}
        provider = _FakeAuthProvider(True, "2.1.259", account)
        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=_fake_cfg()),
            patch("sova.llm.provider.create_provider", return_value=provider),
        ):
            resp = await client.get("/api/auth/status")
        body = resp.text
        assert "sk-ant-secret" not in body
        assert resp.json()["account"] == {"email": "u@e.com"}


class _FakeCatalogProvider:
    """Fake LLM candidate that is available and enumerates a fixed catalog."""

    def __init__(self, models: list) -> None:
        self._models = models

    async def check_available(self):
        return True, "ok"

    async def list_available_models(self, *, allow_probe: bool = True):
        return list(self._models)


class _FakeProcess:
    """Fake asyncio subprocess for reconnect tests: `.wait()` blocks on an Event."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.killed = False
        self.terminated = False
        self._event = asyncio.Event()

    async def wait(self) -> int:
        await self._event.wait()
        return self.returncode

    def kill(self) -> None:
        # SIGKILL cannot be ignored, so a killed process always exits; without
        # this, a watcher awaiting wait() after kill() would hang forever.
        self.killed = True
        self.finish(-9)

    def terminate(self) -> None:
        self.terminated = True

    def finish(self, code: int = 0) -> None:
        self.returncode = code
        self._event.set()


class TestConnectionsAPI:
    """Tests for GET/POST /api/connections* (the Connections page backend)."""

    @pytest.fixture(autouse=True)
    def _clear_connection_state(self):
        from sova.dashboard.services.setup_service import _auth_status_cache, _reconnect_sessions

        _auth_status_cache.clear()
        _reconnect_sessions.clear()
        yield
        _auth_status_cache.clear()
        _reconnect_sessions.clear()

    async def test_catalog_lists_every_provider_and_runtime(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=_fake_full_cfg()),
        ):
            resp = await client.get("/api/connections")
        assert resp.status_code == 200
        data = resp.json()
        llm_ids = {c["id"] for c in data["llm_providers"]}
        assert llm_ids == {"claude-code", "anthropic", "openai", "ollama", "vertex", "litellm", "hybrid"}
        runtime_ids = {c["id"] for c in data["runtimes"]}
        assert runtime_ids == {"claude-code", "aider", "codex"}
        active = next(c for c in data["llm_providers"] if c["id"] == "claude-code")
        assert active["active"] is True
        assert "keyring_available" in data
        assert "checked_at" in data

    async def test_catalog_candidate_failure_does_not_break_others(self, client: AsyncClient) -> None:
        """A candidate whose check_available() raises reports unavailable, not a 503."""
        from unittest.mock import patch

        def _boom(_provider_id, _cfg, _project_dir, **_kwargs):
            raise RuntimeError("detonated")

        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=_fake_full_cfg()),
            patch("sova.dashboard.services.setup_service._build_llm_candidate_provider", side_effect=_boom),
        ):
            resp = await client.get("/api/connections")
        assert resp.status_code == 200
        for candidate in resp.json()["llm_providers"]:
            assert candidate["available"] is False
            assert "detonated" in candidate["detail"]

    def test_catalog_tables_match_config(self) -> None:
        """The Connections catalog re-lists provider/runtime ids that are owned
        elsewhere; pin all three tables so adding one there cannot leave the
        page silently missing it (or offering an optional model field for a
        provider whose model is mandatory)."""
        from typing import get_args

        from sova.config.models import _VENDOR_MODEL_EXAMPLES, AgentConfig, LLMConfig
        from sova.dashboard.services.setup_service import (
            _LLM_MODEL_REQUIRED,
            _LLM_PROVIDER_LABELS,
            _RUNTIME_LABELS,
        )

        assert set(_LLM_PROVIDER_LABELS) == set(get_args(LLMConfig.model_fields["provider"].annotation))
        assert set(_RUNTIME_LABELS) == set(get_args(AgentConfig.model_fields["runtime"].annotation))
        assert _LLM_MODEL_REQUIRED == frozenset(_VENDOR_MODEL_EXAMPLES)

    @pytest.mark.parametrize(
        ("url", "body"),
        [
            # The probe reaches a caller-supplied api_base and spawns provider CLIs, so it is guarded even though it
            # persists nothing.
            pytest.param("/api/connections/llm/validate", {"provider": "claude-code"}, id="validate-llm"),
            pytest.param("/api/connections/runtime/validate", {"runtime": "aider"}, id="validate-runtime"),
            pytest.param("/api/connections/llm/activate", {"provider": "claude-code"}, id="activate-llm"),
            pytest.param("/api/connections/llm/test", {"provider": "claude-code"}, id="test-llm"),
            pytest.param("/api/connections/runtime/activate", {"runtime": "aider"}, id="activate-runtime"),
            pytest.param("/api/connections/reconnect/start", None, id="reconnect-start"),
            pytest.param("/api/connections/reconnect/cancel", None, id="reconnect-cancel"),
        ],
    )
    async def test_requires_csrf(self, client: AsyncClient, url: str, body: dict[str, str] | None) -> None:
        resp = await client.post(url, json=body, headers=NO_CSRF)
        assert resp.status_code == 403

    async def test_catalog_reports_active_api_base_for_prefill(self, client: AsyncClient) -> None:
        """Only the active candidate carries configured_api_base; the page
        prefills it so Validate probes the configured host, not the default."""
        from types import SimpleNamespace
        from unittest.mock import patch

        from sova.config.models import LLMConfig

        cfg = SimpleNamespace(
            llm=LLMConfig(provider="ollama", model="ollama/llama3", api_base="http://box:9999"),
            agent=SimpleNamespace(runtime="claude-code"),
            codex=None,
        )
        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=cfg),
        ):
            resp = await client.get("/api/connections")
        by_id = {c["id"]: c for c in resp.json()["llm_providers"]}
        assert by_id["ollama"]["configured_api_base"] == "http://box:9999"
        assert by_id["openai"]["configured_api_base"] == ""

    async def test_activate_llm_persist_failure_detail_passes_through(self, client: AsyncClient) -> None:
        """update_config_many's error detail reaches the API response verbatim."""
        from unittest.mock import AsyncMock, patch

        provider = _FakeNoAuthDetailsProvider(True, "ok")
        mock_update_many = AsyncMock(return_value={"error": "db is locked"})
        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=_fake_cfg()),
            patch("sova.dashboard.services.setup_service._build_llm_candidate_provider", return_value=provider),
            patch("sova.dashboard.services.settings_service.update_config_many", mock_update_many),
        ):
            resp = await client.post(
                "/api/connections/llm/activate",
                json={"provider": "openai", "model": "gpt-5"},
                headers=_csrf_request_headers(client),
            )
        data = resp.json()
        assert data["status"] == "error"
        assert data["reason"] == "persist_failed"
        assert data["detail"] == "db is locked"

    async def test_validate_llm_missing_model_is_rejected(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/connections/llm/validate",
            json={"provider": "openai"},
            headers=_csrf_request_headers(client),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert data["reason"] == "missing_model"

    async def test_validate_llm_unknown_provider_is_rejected(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/connections/llm/validate",
            json={"provider": "not-a-provider"},
            headers=_csrf_request_headers(client),
        )
        data = resp.json()
        assert data["ok"] is False
        assert data["reason"] == "unknown_provider"

    async def test_validate_llm_success(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        provider = _FakeNoAuthDetailsProvider(True, "ok")
        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=_fake_cfg()),
            patch("sova.dashboard.services.setup_service._build_llm_candidate_provider", return_value=provider),
        ):
            resp = await client.post(
                "/api/connections/llm/validate",
                json={"provider": "claude-code"},
                headers=_csrf_request_headers(client),
            )
        data = resp.json()
        assert data == {"ok": True, "detail": "ok"}

    async def test_validate_llm_not_authenticated(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        provider = _FakeNoAuthDetailsProvider(False, "not logged in")
        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=_fake_cfg()),
            patch("sova.dashboard.services.setup_service._build_llm_candidate_provider", return_value=provider),
        ):
            resp = await client.post(
                "/api/connections/llm/validate",
                json={"provider": "claude-code"},
                headers=_csrf_request_headers(client),
            )
        data = resp.json()
        assert data == {"ok": False, "reason": "not_authenticated", "detail": "not logged in"}

    async def _validate_with_catalog(self, client: AsyncClient, *, provider: str, model: str, catalog: list) -> dict:
        """Validate *model* against a candidate whose enumeration returns *catalog*."""
        from unittest.mock import patch

        candidate = _FakeCatalogProvider(catalog)
        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=_fake_cfg()),
            patch("sova.dashboard.services.setup_service._build_llm_candidate_provider", return_value=candidate),
        ):
            resp = await client.post(
                "/api/connections/llm/validate",
                json={"provider": provider, "model": model},
                headers=_csrf_request_headers(client),
            )
        return resp.json()

    async def test_validate_llm_curated_fallback_never_rejects_the_model(self, client: AsyncClient) -> None:
        """list_available_models() falls back to CURATED_MODELS (four Anthropic
        ids) when no vendor source is configured, which is the normal case for
        plain OpenAI with no api_base. Treating that as the vendor's catalog
        rejected every documented openai/ollama/vertex config outright."""
        from sova.llm.models import CURATED_MODELS

        data = await self._validate_with_catalog(client, provider="openai", model="gpt-5", catalog=list(CURATED_MODELS))
        assert data == {"ok": True, "detail": "ok"}

    async def test_validate_llm_unknown_model_is_advisory_not_a_rejection(self, client: AsyncClient) -> None:
        data = await self._validate_with_catalog(client, provider="openai", model="gpt-5", catalog=[_model("gpt-4o")])
        assert data["ok"] is True
        assert "may be a typo" in data["detail"]

    async def test_validate_llm_vertex_prefix_matches_bare_catalog_id(self, client: AsyncClient) -> None:
        """The Vertex publisher catalog reports bare ids while llm.model carries
        litellm's `vertex_ai/` prefix; the two must still compare equal."""
        data = await self._validate_with_catalog(
            client, provider="vertex", model="vertex_ai/gemini-2.5-pro", catalog=[_model("gemini-2.5-pro")]
        )
        assert data == {"ok": True, "detail": "ok"}

    async def test_validate_llm_ollama_untagged_model_matches_latest_tag(self, client: AsyncClient) -> None:
        """`ollama pull llama3.1` is reported by /api/tags as `llama3.1:latest`."""
        data = await self._validate_with_catalog(
            client, provider="ollama", model="ollama/llama3.1", catalog=[_model("ollama/llama3.1:latest")]
        )
        assert data == {"ok": True, "detail": "ok"}

    async def test_activate_llm_persists_after_validation_passes(self, client: AsyncClient) -> None:
        """All three keys are written in a single atomic call, including the
        clear of the previous vendor's llm.model/llm.api_base (issue #1148:
        update_config_many validates the combined resulting config at once,
        so there is no write-order dependency)."""
        from unittest.mock import AsyncMock, patch

        provider = _FakeNoAuthDetailsProvider(True, "ok")
        mock_update_many = AsyncMock(return_value={"status": "ok"})
        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=_fake_cfg()),
            patch("sova.dashboard.services.setup_service._build_llm_candidate_provider", return_value=provider),
            patch("sova.dashboard.services.settings_service.update_config_many", mock_update_many),
        ):
            resp = await client.post(
                "/api/connections/llm/activate",
                json={"provider": "claude-code"},
                headers=_csrf_request_headers(client),
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"status": "ok", "provider": "claude-code"}
        mock_update_many.assert_awaited_once()
        updates = mock_update_many.await_args.args[1]
        assert updates == {"llm.provider": "claude-code", "llm.model": "", "llm.api_base": ""}

    async def test_activate_llm_reloads_in_process_provider(self, client: AsyncClient) -> None:
        """Unlike POST /settings/config, this path writes through
        update_config_many() directly and must reload the in-process
        provider itself, or the running server keeps using the old one
        until an unrelated llm.* settings write happens to trigger a
        reload (issue #1148)."""
        from unittest.mock import AsyncMock, patch

        provider = _FakeNoAuthDetailsProvider(True, "ok")
        mock_reload = AsyncMock()
        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=_fake_cfg()),
            patch("sova.dashboard.services.setup_service._build_llm_candidate_provider", return_value=provider),
            patch(
                "sova.dashboard.services.settings_service.update_config_many",
                AsyncMock(return_value={"status": "ok"}),
            ),
            patch("sova.llm.client.reload_provider_async", mock_reload),
        ):
            resp = await client.post(
                "/api/connections/llm/activate",
                json={"provider": "claude-code"},
                headers=_csrf_request_headers(client),
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        mock_reload.assert_awaited_once()

    async def test_activate_llm_model_required_writes_model_and_provider_together(self, client: AsyncClient) -> None:
        """A model-required target is validated as one combined write, not a
        hand-ordered sequence of single-key writes."""
        from unittest.mock import AsyncMock, patch

        provider = _FakeNoAuthDetailsProvider(True, "ok")
        mock_update_many = AsyncMock(return_value={"status": "ok"})
        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=_fake_cfg()),
            patch("sova.dashboard.services.setup_service._build_llm_candidate_provider", return_value=provider),
            patch("sova.dashboard.services.settings_service.update_config_many", mock_update_many),
        ):
            resp = await client.post(
                "/api/connections/llm/activate",
                json={"provider": "openai", "model": "gpt-5"},
                headers=_csrf_request_headers(client),
            )
        assert resp.json() == {"status": "ok", "provider": "openai"}
        updates = mock_update_many.await_args.args[1]
        assert updates == {"llm.provider": "openai", "llm.model": "gpt-5", "llm.api_base": ""}

    async def test_activate_llm_blocked_when_validation_fails(self, client: AsyncClient) -> None:
        """A failing candidate must never reach settings_service.update_config_many."""
        from unittest.mock import AsyncMock, patch

        mock_update_many = AsyncMock(return_value={"status": "ok"})
        with patch("sova.dashboard.services.settings_service.update_config_many", mock_update_many):
            resp = await client.post(
                "/api/connections/llm/activate",
                json={"provider": "openai"},
                headers=_csrf_request_headers(client),
            )
        data = resp.json()
        assert data["status"] == "error"
        assert data["reason"] == "missing_model"
        mock_update_many.assert_not_called()

    async def test_activate_llm_persist_failure_is_reported(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        provider = _FakeNoAuthDetailsProvider(True, "ok")
        mock_update_many = AsyncMock(return_value={"error": "'llm.provider' rejected: llm.model: required"})
        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=_fake_cfg()),
            patch("sova.dashboard.services.setup_service._build_llm_candidate_provider", return_value=provider),
            patch("sova.dashboard.services.settings_service.update_config_many", mock_update_many),
        ):
            resp = await client.post(
                "/api/connections/llm/activate",
                json={"provider": "claude-code"},
                headers=_csrf_request_headers(client),
            )
        data = resp.json()
        assert data["status"] == "error"
        assert data["reason"] == "persist_failed"

    async def test_activate_llm_db_failure_reports_persist_failed(self, client: AsyncClient) -> None:
        """A mid-transaction DB failure surfaces as persist_failed; the
        all-or-nothing rollback behavior itself is covered directly against a
        real database in tests/test_settings_service.py::TestUpdateConfigMany."""
        from unittest.mock import AsyncMock, patch

        provider = _FakeNoAuthDetailsProvider(True, "ok")
        mock_update_many = AsyncMock(return_value={"error": "Failed to persist settings (DB unavailable)"})
        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=_fake_cfg()),
            patch("sova.dashboard.services.setup_service._build_llm_candidate_provider", return_value=provider),
            patch("sova.dashboard.services.settings_service.update_config_many", mock_update_many),
        ):
            resp = await client.post(
                "/api/connections/llm/activate",
                json={"provider": "openai", "model": "gpt-5"},
                headers=_csrf_request_headers(client),
            )
        data = resp.json()
        assert data["status"] == "error"
        assert data["reason"] == "persist_failed"

    async def test_test_llm_runs_real_invoke(self, client: AsyncClient) -> None:
        from decimal import Decimal
        from unittest.mock import AsyncMock, patch

        from sova.llm.models import LLMResult

        provider = _FakeNoAuthDetailsProvider(True, "ok")
        provider.invoke = AsyncMock(
            return_value=LLMResult(text="pong", model="claude-sonnet-4-6", cost_usd=Decimal("0.001"))
        )
        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=_fake_cfg()),
            patch("sova.dashboard.services.setup_service._build_llm_candidate_provider", return_value=provider),
        ):
            resp = await client.post(
                "/api/connections/llm/test",
                json={"provider": "claude-code"},
                headers=_csrf_request_headers(client),
            )
        data = resp.json()
        assert data == {"ok": True, "text": "pong", "model": "claude-sonnet-4-6", "cost_usd": "0.001"}

    async def test_test_llm_unknown_provider_never_reaches_invoke(self, client: AsyncClient) -> None:
        """Without the catalog check, an unknown id falls through to the LiteLLM
        branch and spends a real invoke() against the submitted api_base."""
        from unittest.mock import patch

        with patch("sova.dashboard.services.setup_service._build_llm_candidate_provider") as mock_build:
            resp = await client.post(
                "/api/connections/llm/test",
                json={"provider": "not-a-provider", "model": "x", "api_base": "http://10.0.0.1:8080"},
                headers=_csrf_request_headers(client),
            )
        data = resp.json()
        assert data["ok"] is False
        assert data["reason"] == "unknown_provider"
        mock_build.assert_not_called()

    async def test_catalog_routing_backend_ignores_a_scrubbed_routing_var(self, client: AsyncClient) -> None:
        """CLAUDE_CODE_USE_VERTEX set on the server but not in agent.env_passthrough
        is stripped from the spawned CLI's environment, so the page must not
        report Vertex routing (and hide the `claude auth login` button) for it."""
        from unittest.mock import patch

        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=_fake_full_cfg()),
            patch.dict(os.environ, {"CLAUDE_CODE_USE_VERTEX": "1"}),
            patch("sova.dashboard.services.setup_service.configured_passthrough", return_value=()),
        ):
            resp = await client.get("/api/connections")
        claude_code = next(c for c in resp.json()["llm_providers"] if c["id"] == "claude-code")
        assert claude_code["routing_backend"] == "firstparty"

    async def test_catalog_routing_backend_honors_a_passed_through_routing_var(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=_fake_full_cfg()),
            patch.dict(os.environ, {"CLAUDE_CODE_USE_VERTEX": "1"}),
            patch(
                "sova.dashboard.services.setup_service.configured_passthrough",
                return_value=("CLAUDE_CODE_USE_VERTEX",),
            ),
        ):
            resp = await client.get("/api/connections")
        claude_code = next(c for c in resp.json()["llm_providers"] if c["id"] == "claude-code")
        assert claude_code["routing_backend"] == "vertex"

    async def test_test_llm_invoke_failure_is_reported(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        provider = _FakeNoAuthDetailsProvider(True, "ok")
        provider.invoke = AsyncMock(side_effect=RuntimeError("rate limited"))
        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=_fake_cfg()),
            patch("sova.dashboard.services.setup_service._build_llm_candidate_provider", return_value=provider),
        ):
            resp = await client.post(
                "/api/connections/llm/test",
                json={"provider": "claude-code"},
                headers=_csrf_request_headers(client),
            )
        data = resp.json()
        assert data["ok"] is False
        assert data["reason"] == "invoke_failed"
        assert "rate limited" in data["detail"]

    async def test_validate_runtime_unknown_is_rejected(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/connections/runtime/validate",
            json={"runtime": "not-a-runtime"},
            headers=_csrf_request_headers(client),
        )
        data = resp.json()
        assert data["ok"] is False
        assert data["reason"] == "unknown_runtime"

    async def test_activate_runtime_persists_after_validation_passes(self, client: AsyncClient) -> None:
        from unittest.mock import ANY, AsyncMock, patch

        runtime = _FakeNoAuthDetailsProvider(True, "ok")
        mock_update = AsyncMock(return_value={"status": "ok"})
        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=_fake_full_cfg()),
            patch("sova.ipc.runtime.create_runtime", return_value=runtime),
            patch("sova.dashboard.services.settings_service.update_config", mock_update),
        ):
            resp = await client.post(
                "/api/connections/runtime/activate",
                json={"runtime": "aider"},
                headers=_csrf_request_headers(client),
            )
        data = resp.json()
        assert data == {"status": "ok", "runtime": "aider"}
        mock_update.assert_awaited_once_with(ANY, key="agent.runtime", value="aider")

    async def test_activate_runtime_reloads_in_process_runtime(self, client: AsyncClient) -> None:
        """Mirrors test_activate_llm_reloads_in_process_provider: this path
        writes through update_config() directly, bypassing POST
        /settings/config's _reload_all_configs() dispatch, so it must
        reload the in-process runtime itself (issue #1148)."""
        from unittest.mock import AsyncMock, patch

        runtime = _FakeNoAuthDetailsProvider(True, "ok")
        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=_fake_full_cfg()),
            patch("sova.ipc.runtime.create_runtime", return_value=runtime),
            patch("sova.dashboard.services.settings_service.update_config", AsyncMock(return_value={"status": "ok"})),
            patch("sova.ipc.runtime.reload_runtime") as mock_reload,
        ):
            resp = await client.post(
                "/api/connections/runtime/activate",
                json={"runtime": "aider"},
                headers=_csrf_request_headers(client),
            )
        assert resp.json()["status"] == "ok"
        mock_reload.assert_called_once()

    async def test_reconnect_status_idle_by_default(self, client: AsyncClient) -> None:
        resp = await client.get("/api/connections/reconnect/status")
        assert resp.json() == {"status": "idle"}

    async def test_reconnect_cli_missing(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        with (
            patch("shutil.which", return_value=None),
            patch("sova.dashboard.services.setup_service._is_headless_environment", return_value=False),
        ):
            resp = await client.post("/api/connections/reconnect/start", headers=_csrf_request_headers(client))
        data = resp.json()
        assert data == {"status": "error", "reason": "cli_missing", "detail": "claude CLI not found"}

    async def test_reconnect_second_start_while_running_is_rejected(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        process = _FakeProcess()
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)),
            patch("sova.dashboard.services.setup_service._is_headless_environment", return_value=False),
        ):
            first = await client.post("/api/connections/reconnect/start", headers=_csrf_request_headers(client))
            assert first.json() == {"status": "ok"}

            status = await client.get("/api/connections/reconnect/status")
            assert status.json()["status"] == "running"

            second = await client.post("/api/connections/reconnect/start", headers=_csrf_request_headers(client))
            assert second.json() == {
                "status": "error",
                "reason": "already_in_progress",
                "detail": "A reconnect is already in progress",
            }
        process.finish(0)

    async def test_reconnect_completes_successfully(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        process = _FakeProcess()
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)),
            patch("sova.dashboard.services.setup_service._is_headless_environment", return_value=False),
        ):
            await client.post("/api/connections/reconnect/start", headers=_csrf_request_headers(client))
            process.finish(0)
            await _wait_for_reconnect_watchers()  # the watcher polls returncode, so wait for it to finish
            status = await client.get("/api/connections/reconnect/status")
        assert status.json()["status"] == "completed"

    async def test_reconnect_failure_is_reported(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        process = _FakeProcess()
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)),
            patch("sova.dashboard.services.setup_service._is_headless_environment", return_value=False),
        ):
            await client.post("/api/connections/reconnect/start", headers=_csrf_request_headers(client))
            process.finish(1)
            await _wait_for_reconnect_watchers()
            status = await client.get("/api/connections/reconnect/status")
        assert status.json()["status"] == "failed"
        assert "exited with code 1" in status.json()["detail"]

    async def test_reconnect_hard_timeout_kills_process(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        process = _FakeProcess()
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)),
            patch("sova.dashboard.services.setup_service._RECONNECT_TIMEOUT", 0.05),
            patch("sova.dashboard.services.setup_service._is_headless_environment", return_value=False),
        ):
            await client.post("/api/connections/reconnect/start", headers=_csrf_request_headers(client))
            await _wait_for_reconnect_watchers()
            status = await client.get("/api/connections/reconnect/status")
        assert status.json()["status"] == "timeout"
        assert process.killed is True

    async def test_cancel_reconnect_with_no_session_is_an_error(self, client: AsyncClient) -> None:
        resp = await client.post("/api/connections/reconnect/cancel", headers=_csrf_request_headers(client))
        data = resp.json()
        assert data["status"] == "error"

    async def test_cancel_reconnect_terminates_process(self, client: AsyncClient) -> None:
        from unittest.mock import AsyncMock, patch

        process = _FakeProcess()
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)),
            patch("sova.dashboard.services.setup_service._is_headless_environment", return_value=False),
        ):
            await client.post("/api/connections/reconnect/start", headers=_csrf_request_headers(client))
            cancel_resp = await client.post("/api/connections/reconnect/cancel", headers=_csrf_request_headers(client))
            assert cancel_resp.json() == {"status": "ok"}
            assert process.terminated is True
            process.finish(-15)
            await asyncio.sleep(0)
            status = await client.get("/api/connections/reconnect/status")
        assert status.json()["status"] == "cancelled"

    async def test_cancel_reconnect_escalates_to_kill_after_grace_period(self, client: AsyncClient) -> None:
        """A process that ignores terminate() must be killed once the grace period elapses."""
        from unittest.mock import AsyncMock, patch

        process = _FakeProcess()
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)),
            patch("sova.dashboard.services.setup_service._CANCEL_GRACE_PERIOD", 0.05),
            patch("sova.dashboard.services.setup_service._is_headless_environment", return_value=False),
        ):
            await client.post("/api/connections/reconnect/start", headers=_csrf_request_headers(client))
            cancel_resp = await client.post("/api/connections/reconnect/cancel", headers=_csrf_request_headers(client))
            assert cancel_resp.json() == {"status": "ok"}
            assert process.terminated is True
            assert process.killed is True

    async def test_watch_reconnect_does_not_overwrite_cancelled_status_on_timeout(self, client: AsyncClient) -> None:
        """If the hard timeout fires after cancellation, the status must stay 'cancelled', not flip to 'timeout'."""
        from unittest.mock import AsyncMock, patch

        process = _FakeProcess()
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)),
            patch("sova.dashboard.services.setup_service._RECONNECT_TIMEOUT", 0.05),
            patch("sova.dashboard.services.setup_service._CANCEL_GRACE_PERIOD", 0.05),
            patch("sova.dashboard.services.setup_service._is_headless_environment", return_value=False),
        ):
            await client.post("/api/connections/reconnect/start", headers=_csrf_request_headers(client))
            cancel_resp = await client.post("/api/connections/reconnect/cancel", headers=_csrf_request_headers(client))
            assert cancel_resp.json() == {"status": "ok"}
            await _wait_for_reconnect_watchers()
            status = await client.get("/api/connections/reconnect/status")
        assert status.json()["status"] == "cancelled"


async def _wait_for_reconnect_watchers() -> None:
    """Wait for every reconnect watcher task to finish, instead of sleeping past its patched timeout."""
    from sova.dashboard.services import setup_service

    watchers = [s.watcher for s in setup_service._reconnect_sessions.values() if s.watcher is not None]
    await asyncio.wait_for(asyncio.gather(*watchers, return_exceptions=True), timeout=5)


async def test_watcher_ends_when_the_child_exits_even_if_a_grandchild_holds_the_pipe(tmp_path: Path) -> None:
    """A browser gh launched inherits its stdout: `Process.wait()` would block on it, `returncode` does not."""
    import signal
    import time

    from sova.dashboard.services import setup_service

    flow = setup_service._FLOWS["claude"]
    process = await asyncio.create_subprocess_exec(
        "sh", "-c", "sleep 30 & echo $!; exit 0", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    assert process.stdout is not None
    grandchild = int((await asyncio.wait_for(process.stdout.readline(), timeout=3)).decode())
    session = setup_service._ReconnectSession(process=process, code="ABCD-1234")
    started = time.monotonic()
    try:
        await asyncio.wait_for(setup_service._watch_reconnect(session, tmp_path, flow), timeout=3)
        assert time.monotonic() - started < 3
        assert session.status == "completed"
        assert session.code == ""
    finally:
        # Release the pipe so the subprocess transport closes cleanly instead of leaking into a later test.
        os.kill(grandchild, signal.SIGKILL)
        # `wait()` returns on exit, before the pipe's EOF lets the transport close; drain to EOF and yield so it closes
        # here, not in a GC pass during a later test (py3.14 turns that ResourceWarning into a failure).
        await asyncio.wait_for(process.stdout.read(), timeout=3)
        await asyncio.wait_for(process.wait(), timeout=3)
        for _ in range(3):
            await asyncio.sleep(0)


class _FakeStream:
    """Fake child output pipe: `.read()` blocks until text is fed, and returns b"" once closed."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()

    def feed(self, text: str) -> None:
        self._queue.put_nowait(text.encode())

    async def read(self, _size: int) -> bytes:
        return await self._queue.get()


class _FakeOutputProcess(_FakeProcess):
    def __init__(self) -> None:
        super().__init__()
        self.stdout = _FakeStream()


def _spawn_patches(process: _FakeProcess):
    """Patch the spawn so a flow starts against `process`, on a machine that has a display and every CLI."""
    from contextlib import ExitStack
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    stack = ExitStack()
    spawn = stack.enter_context(patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)))
    stack.enter_context(patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}"))
    stack.enter_context(patch("sova.dashboard.services.setup_service._is_headless_environment", return_value=False))
    # gh is not logged in until a test says so: the start probes `gh auth status` before spawning.
    stack.enter_context(
        patch("sova.dashboard.services.setup_service.run", AsyncMock(return_value=SimpleNamespace(success=False)))
    )
    return stack, spawn


async def _wait_for(predicate, *, timeout: float = 2.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), timeout=timeout)


class TestReconnectFlows:
    """The gh and google flows on the flow-keyed reconnect runner (issue #1121)."""

    @pytest.fixture(autouse=True)
    def _clear_sessions(self):
        from sova.dashboard.services.setup_service import _reconnect_sessions

        _reconnect_sessions.clear()
        yield
        _reconnect_sessions.clear()

    @pytest.fixture(autouse=True)
    def _no_ambient_gh_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sova.dashboard.services.setup_service import _GH_TOKEN_VARS

        for var in _GH_TOKEN_VARS:
            monkeypatch.delenv(var, raising=False)

    @pytest.fixture
    def google_ready(self, tmp_path: Path):
        """A deployment where the Google control can run: provider configured, extra installed, credentials present."""
        from types import SimpleNamespace
        from unittest.mock import patch

        from sova.awareness.auth import google_oauth

        credentials = tmp_path / "google_credentials.json"
        credentials.write_text("{}", encoding="utf-8")
        cfg = SimpleNamespace(awareness=SimpleNamespace(enabled=True, providers=["gmail"], gmail_token_path=""))
        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=cfg),
            patch.object(google_oauth, "InstalledAppFlow", object()),
            patch.object(google_oauth, "DEFAULT_CREDENTIALS_PATH", credentials),
        ):
            yield cfg

    @pytest.mark.parametrize("flow", ["gh", "google"])
    async def test_non_loopback_is_rejected_without_spawning(self, flow: str, tmp_path: Path) -> None:
        from sova.dashboard.services.setup_service import start_reconnect

        process = _FakeProcess()
        stack, spawn = _spawn_patches(process)
        with stack:
            result = await start_reconnect(tmp_path, is_loopback_bind=False, flow=flow)
        assert result["reason"] == "non_loopback"
        spawn.assert_not_called()

    async def test_gh_missing_cli_is_cli_missing(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.setup_service import start_reconnect

        with (
            patch("shutil.which", return_value=None),
            patch("sova.dashboard.services.setup_service._is_headless_environment", return_value=False),
        ):
            result = await start_reconnect(tmp_path, flow="gh")
        assert result == {"status": "error", "reason": "cli_missing", "detail": "gh CLI not found"}

    async def test_gh_is_not_blocked_by_a_vertex_routed_claude(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.setup_service import reconnect_availability
        from sova.llm.backends import Backend

        with (
            patch("sova.dashboard.services.setup_service._is_headless_environment", return_value=False),
            patch("sova.dashboard.services.setup_service._claude_cli_backend", return_value=Backend.VERTEX),
        ):
            assert reconnect_availability(is_loopback_bind=True)["reason"] == "vertex_routed"
            assert reconnect_availability(is_loopback_bind=True, flow="gh")["can_reconnect"] is True

    async def test_gh_surfaces_the_code_while_running_and_clears_it_when_done(self, client: AsyncClient) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        process = _FakeOutputProcess()
        stack, spawn = _spawn_patches(process)
        not_yet, logged_in = SimpleNamespace(success=False), SimpleNamespace(success=True)
        with (
            stack,
            patch("sova.dashboard.services.setup_service.run", AsyncMock(side_effect=[not_yet, logged_in])),
            patch("sova.dashboard.services.setup_service.invalidate_health_checks_cache") as invalidate,
        ):
            started = await client.post("/api/connections/reconnect/gh/start", headers=_csrf_request_headers(client))
            assert started.json() == {"status": "ok"}
            argv = spawn.call_args.args
            assert argv[1:] == ("auth", "login", "--web")

            process.stdout.feed("\n! One-time code (B991-E7E6) copied to clipboard\nOpen this URL: https://x\n")
            await _wait_for(lambda: _gh_status_code() == "B991-E7E6")
            running = (await client.get("/api/connections/reconnect/gh/status")).json()
            assert running["status"] == "running"
            assert running["code"] == "B991-E7E6"

            process.finish(0)
            await _wait_for_reconnect_watchers()
            done = (await client.get("/api/connections/reconnect/gh/status")).json()
        assert done["status"] == "completed"
        assert done["code"] == ""
        invalidate.assert_called_once()

    @pytest.mark.parametrize("failure", ["raises", "hangs"])
    async def test_gh_start_survives_a_raising_or_hanging_precheck_and_releases_the_lock(
        self, failure: str, tmp_path: Path
    ) -> None:
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services import setup_service

        async def hang(*_args, **_kwargs):
            await asyncio.sleep(3600)

        process = _FakeOutputProcess()
        stack, spawn = _spawn_patches(process)
        probe = AsyncMock(side_effect=FileNotFoundError("gh")) if failure == "raises" else hang
        with (
            stack,
            patch.object(setup_service, "run", probe),
            patch.object(setup_service, "_POST_CHECK_TIMEOUT", 0.05),
        ):
            result = await setup_service.start_reconnect(tmp_path, flow="gh")
            assert result == {"status": "ok"}
            spawn.assert_called_once()
            assert not setup_service._flow_start_locks["gh"].locked()
            assert setup_service.get_reconnect_status(tmp_path, "gh")["status"] == "running"
            process.finish(1)
            await _wait_for_reconnect_watchers()

    @pytest.mark.parametrize("var", ["GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"])
    async def test_gh_is_rejected_while_a_token_env_var_overrides_any_login(
        self, var: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sova.dashboard.services.setup_service import start_reconnect

        monkeypatch.setenv(var, "ghp_expired")
        stack, spawn = _spawn_patches(_FakeProcess())
        with stack:
            result = await start_reconnect(tmp_path, flow="gh")
        assert result["reason"] == "env_token"
        assert "ghp_expired" not in result["detail"]
        spawn.assert_not_called()

    async def test_gh_already_logged_in_completes_without_spawning(self, tmp_path: Path) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from sova.dashboard.services.setup_service import get_reconnect_status, start_reconnect

        stack, spawn = _spawn_patches(_FakeProcess())
        with (
            stack,
            patch("sova.dashboard.services.setup_service.run", AsyncMock(return_value=SimpleNamespace(success=True))),
            patch("sova.dashboard.services.setup_service.invalidate_health_checks_cache") as invalidate,
        ):
            assert await start_reconnect(tmp_path, flow="gh") == {"status": "ok"}
        spawn.assert_not_called()
        invalidate.assert_called_once_with(tmp_path)
        status = get_reconnect_status(tmp_path, "gh")
        assert status["status"] == "completed"
        assert status["code"] == ""

    async def test_gh_without_a_code_line_stays_running_with_an_empty_code(self, client: AsyncClient) -> None:
        process = _FakeOutputProcess()
        stack, _ = _spawn_patches(process)
        with stack:
            await client.post("/api/connections/reconnect/gh/start", headers=_csrf_request_headers(client))
            process.stdout.feed("Something else entirely\n")
            await asyncio.sleep(0.02)
            status = (await client.get("/api/connections/reconnect/gh/status")).json()
            process.finish(1)
            await _wait_for_reconnect_watchers()
        assert status["status"] == "running"
        assert status["code"] == ""

    async def test_gh_exit_zero_without_a_passing_auth_status_is_failed(self, client: AsyncClient) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        process = _FakeOutputProcess()
        stack, _ = _spawn_patches(process)
        with (
            stack,
            patch("sova.dashboard.services.setup_service.run", AsyncMock(return_value=SimpleNamespace(success=False))),
        ):
            await client.post("/api/connections/reconnect/gh/start", headers=_csrf_request_headers(client))
            process.finish(0)
            await _wait_for_reconnect_watchers()
            status = (await client.get("/api/connections/reconnect/gh/status")).json()
        assert status["status"] == "failed"

    async def test_gh_timeout_clears_the_code(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        process = _FakeOutputProcess()
        stack, _ = _spawn_patches(process)
        with stack, patch("sova.dashboard.services.setup_service._RECONNECT_TIMEOUT", 0.5):
            await client.post("/api/connections/reconnect/gh/start", headers=_csrf_request_headers(client))
            process.stdout.feed("one-time code: ABCD-1234\n")
            await _wait_for(lambda: _gh_status_code() == "ABCD-1234")
            await _wait_for_reconnect_watchers()
            status = (await client.get("/api/connections/reconnect/gh/status")).json()
        assert status["status"] == "timeout"
        assert status["code"] == ""
        assert process.killed is True

    async def test_cancel_gh_terminates_and_clears_the_code(self, client: AsyncClient) -> None:
        process = _FakeOutputProcess()
        stack, _ = _spawn_patches(process)
        with stack:
            await client.post("/api/connections/reconnect/gh/start", headers=_csrf_request_headers(client))
            process.stdout.feed("one-time code: ABCD-1234\n")
            await _wait_for(lambda: _gh_status_code() == "ABCD-1234")
            cancelled = await client.post("/api/connections/reconnect/gh/cancel", headers=_csrf_request_headers(client))
            process.finish(-15)
            await _wait_for_reconnect_watchers()
            status = (await client.get("/api/connections/reconnect/gh/status")).json()
        assert cancelled.json() == {"status": "ok"}
        assert process.terminated is True
        assert status["status"] == "cancelled"
        assert status["code"] == ""

    async def test_flows_are_single_flight_per_flow_not_across_flows(self, client: AsyncClient) -> None:
        gh_process, claude_process = _FakeOutputProcess(), _FakeProcess()
        stack, _ = _spawn_patches(gh_process)
        with stack:
            first = await client.post("/api/connections/reconnect/gh/start", headers=_csrf_request_headers(client))
            second = await client.post("/api/connections/reconnect/gh/start", headers=_csrf_request_headers(client))
            with _spawn_patches(claude_process)[0]:
                other = await client.post("/api/connections/reconnect/start", headers=_csrf_request_headers(client))
            gh_process.finish(1)
            claude_process.finish(1)
            await _wait_for_reconnect_watchers()
        assert first.json() == {"status": "ok"}
        assert second.json()["reason"] == "already_in_progress"
        assert other.json() == {"status": "ok"}

    async def test_claude_status_payload_has_no_code_field(self, client: AsyncClient) -> None:
        process = _FakeProcess()
        stack, _ = _spawn_patches(process)
        with stack:
            await client.post("/api/connections/reconnect/claude/start", headers=_csrf_request_headers(client))
            status = (await client.get("/api/connections/reconnect/status")).json()
            process.finish(1)
            await _wait_for_reconnect_watchers()
        assert set(status) == {"status", "detail", "elapsed_seconds"}

    async def test_unknown_flow_is_404(self, client: AsyncClient) -> None:
        resp = await client.post("/api/connections/reconnect/nope/start", headers=_csrf_request_headers(client))
        assert resp.status_code == 404

    async def test_flow_routes_stay_csrf_guarded(self, dashboard_app) -> None:
        transport = ASGITransport(app=dashboard_app)
        async with AsyncClient(transport=transport, base_url="http://test") as bare:
            resp = await bare.post("/api/connections/reconnect/gh/start")
        assert resp.status_code == 403

    @pytest.mark.parametrize(
        ("missing", "reason"),
        [
            ("providers", "not_configured"),
            ("disabled", "not_configured"),
            ("extra", "extra_missing"),
            ("credentials", "credentials_missing"),
        ],
    )
    async def test_google_gating_rejects_without_spawning(
        self, missing: str, reason: str, google_ready, tmp_path: Path
    ) -> None:
        from unittest.mock import patch

        from sova.awareness.auth import google_oauth
        from sova.dashboard.services.setup_service import start_reconnect

        stack, spawn = _spawn_patches(_FakeProcess())
        with stack:
            if missing == "providers":
                google_ready.awareness.providers = ["reminders"]
                result = await start_reconnect(tmp_path, flow="google")
            elif missing == "disabled":
                google_ready.awareness.enabled = False
                result = await start_reconnect(tmp_path, flow="google")
            elif missing == "extra":
                with patch.object(google_oauth, "InstalledAppFlow", None):
                    result = await start_reconnect(tmp_path, flow="google")
            else:
                with patch.object(google_oauth, "DEFAULT_CREDENTIALS_PATH", tmp_path / "absent.json"):
                    result = await start_reconnect(tmp_path, flow="google")
        assert result["reason"] == reason
        spawn.assert_not_called()

    async def test_google_runs_the_authorize_child_and_rechecks_the_token(self, google_ready, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sova.awareness.auth import google_oauth
        from sova.dashboard.services.setup_service import get_reconnect_status, start_reconnect

        process = _FakeProcess()
        stack, spawn = _spawn_patches(process)
        with (
            stack,
            patch.object(google_oauth, "forget_cached_credentials") as forget,
            patch.object(google_oauth, "authenticate_google") as authenticate,
            patch("sova.dashboard.services.setup_service.invalidate_health_checks_cache") as invalidate,
        ):
            assert await start_reconnect(tmp_path, flow="google") == {"status": "ok"}
            # -P keeps the working directory off the child's sys.path.
            assert spawn.call_args.args[1:] == ("-P", "-m", "sova.awareness.auth.authorize", str(tmp_path))
            process.finish(0)
            await _wait_for_reconnect_watchers()
        assert get_reconnect_status(tmp_path, "google")["status"] == "completed"
        forget.assert_called_once()
        assert authenticate.call_args.kwargs == {"interactive": False}
        invalidate.assert_called_once_with(tmp_path)

    @pytest.mark.parametrize(
        ("exit_code", "recheck_fails", "expected_detail"),
        [(1, False, "Google authorization exited with code 1"), (0, True, "no usable token")],
    )
    async def test_google_failure_detail_is_generic(
        self, exit_code: int, recheck_fails: bool, expected_detail: str, google_ready, tmp_path: Path
    ) -> None:
        from unittest.mock import patch

        from sova.awareness.auth import google_oauth
        from sova.dashboard.services.setup_service import get_reconnect_status, start_reconnect

        process = _FakeProcess()
        stack, _ = _spawn_patches(process)
        with (
            stack,
            patch.object(google_oauth, "forget_cached_credentials"),
            patch.object(
                google_oauth,
                "authenticate_google",
                side_effect=FileNotFoundError("/secret/path") if recheck_fails else None,
            ),
        ):
            await start_reconnect(tmp_path, flow="google")
            process.finish(exit_code)
            await _wait_for_reconnect_watchers()
        status = get_reconnect_status(tmp_path, "google")
        assert status["status"] == "failed"
        assert expected_detail in status["detail"]
        assert "/secret/path" not in status["detail"]

    async def test_google_config_load_failure_is_a_reason_coded_rejection(self, google_ready, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.setup_service import start_reconnect

        stack, spawn = _spawn_patches(_FakeProcess())
        with (
            stack,
            patch("sova.dashboard.services.setup_service.load_config", side_effect=ValueError("/secret/config")),
        ):
            result = await start_reconnect(tmp_path, flow="google")
        assert result["reason"] == "config_error"
        assert "/secret" not in result["detail"]
        spawn.assert_not_called()

    async def test_one_flow_check_failing_does_not_hide_the_others(self, google_ready, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sova.dashboard.services.setup_service import get_reconnect_flow_availability

        stack, _ = _spawn_patches(_FakeProcess())
        with stack, patch("sova.dashboard.services.setup_service._cli_availability", side_effect=OSError("boom")):
            flows = await get_reconnect_flow_availability(tmp_path, is_loopback_bind=True)
        assert flows["gh"]["reason"] == "check_failed"
        assert "boom" not in flows["gh"]["detail"]
        assert flows["claude"]["reason"] == "check_failed"
        assert flows["google"]["can_reconnect"] is True

    async def test_availability_endpoint_reports_every_flow(self, client: AsyncClient) -> None:
        from unittest.mock import patch

        with (
            patch("shutil.which", return_value=None),
            patch("sova.dashboard.services.setup_service._is_headless_environment", return_value=False),
        ):
            data = (await client.get("/api/connections/reconnect/availability")).json()
        assert set(data["flows"]) == {"claude", "gh", "google"}
        assert data["flows"]["gh"]["reason"] == "cli_missing"

    async def test_read_flow_code_without_stdout_returns_immediately(self) -> None:
        from sova.dashboard.services.setup_service import _GH_CODE_PATTERN, _read_flow_code, _ReconnectSession

        process = _FakeProcess()
        process.stdout = None
        session = _ReconnectSession(process=process)
        await asyncio.wait_for(_read_flow_code(session, _GH_CODE_PATTERN), timeout=1)
        assert session.code == ""

    async def test_read_flow_code_keeps_draining_after_the_code_is_found(self) -> None:
        from sova.dashboard.services.setup_service import _GH_CODE_PATTERN, _read_flow_code, _ReconnectSession

        process = _FakeOutputProcess()
        session = _ReconnectSession(process=process)
        process.stdout.feed("! First copy your one-time code: ABCD-1234\n")
        process.stdout.feed("one-time code: WXYZ-9876\n")
        process.stdout.feed("")
        await asyncio.wait_for(_read_flow_code(session, _GH_CODE_PATTERN), timeout=1)
        assert session.code == "ABCD-1234"

    async def test_read_flow_code_swallows_a_pipe_read_error(self) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.setup_service import _GH_CODE_PATTERN, _read_flow_code, _ReconnectSession

        process = _FakeOutputProcess()
        process.stdout.read = AsyncMock(side_effect=OSError("pipe closed"))
        session = _ReconnectSession(process=process)
        await asyncio.wait_for(_read_flow_code(session, _GH_CODE_PATTERN), timeout=1)
        assert session.code == ""
        assert session.status == "running"

    @pytest.mark.parametrize("failure", ["raises", "hangs"])
    async def test_a_broken_post_check_ends_the_session_and_frees_single_flight(
        self, failure: str, client: AsyncClient, tmp_path: Path
    ) -> None:
        from dataclasses import replace
        from unittest.mock import patch

        from sova.dashboard.services import setup_service

        async def broken(_project_dir: Path) -> str:
            if failure == "raises":
                raise FileNotFoundError("/secret/gh")
            await asyncio.sleep(60)
            return ""

        process = _FakeProcess()
        stack, _ = _spawn_patches(process)
        flows = {**setup_service._FLOWS, "claude": replace(setup_service._FLOWS["claude"], on_success=broken)}
        with (
            stack,
            patch.object(setup_service, "_FLOWS", flows),
            patch.object(setup_service, "_POST_CHECK_TIMEOUT", 0.05),
        ):
            await client.post("/api/connections/reconnect/start", headers=_csrf_request_headers(client))
            process.finish(0)
            await _wait_for_reconnect_watchers()
            status = (await client.get("/api/connections/reconnect/status")).json()
            again = await client.post("/api/connections/reconnect/start", headers=_csrf_request_headers(client))
            process.finish(0)
            await _wait_for_reconnect_watchers()
        assert status["status"] == "failed"
        assert "post-check could not run" in status["detail"]
        assert "/secret" not in status["detail"]
        assert again.json() == {"status": "ok"}

    async def test_the_reader_keeps_draining_after_the_child_exits(self, tmp_path: Path) -> None:
        """A browser gh launched can hold the pipe open past gh's exit: it must never block on a full pipe."""
        from sova.dashboard.services import setup_service

        process = _FakeOutputProcess()
        stack, _ = _spawn_patches(process)
        with stack:
            await setup_service.start_reconnect(tmp_path, flow="gh")
            session = setup_service._reconnect_sessions[setup_service._session_key("gh", tmp_path)]
            process.finish(1)
            await _wait_for_reconnect_watchers()
            assert session.status == "failed"
            assert session.reader is not None
            assert not session.reader.done()
            # The stream stays open after the exit: later writes are still consumed, and EOF ends the reader.
            process.stdout.feed("browser console noise\n")
            await _wait_for(process.stdout._queue.empty)
            assert not session.reader.done()
            process.stdout.feed("")
            await asyncio.wait_for(session.reader, timeout=1)

    async def test_the_watcher_never_leaves_a_session_running(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from sova.dashboard.services import setup_service

        process = _FakeProcess()
        session = setup_service._ReconnectSession(process=process)
        with patch.object(setup_service, "_await_reconnect_outcome", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                await setup_service._watch_reconnect(session, tmp_path, setup_service._FLOWS["gh"])
        assert session.status == "failed"

    async def test_await_reconnect_outcome_keeps_a_cancel_during_the_post_check(self, tmp_path: Path) -> None:
        from dataclasses import replace

        from sova.dashboard.services.setup_service import _FLOWS, _await_reconnect_outcome, _ReconnectSession

        process = _FakeProcess()
        process.finish(0)
        session = _ReconnectSession(process=process)

        async def cancel_during_check(_project_dir: Path) -> str:
            session.status = "cancelled"
            return "post-check failure that must not overwrite the cancel"

        flow = replace(_FLOWS["gh"], on_success=cancel_during_check)
        await _await_reconnect_outcome(session, tmp_path, flow)
        assert session.status == "cancelled"
        assert session.detail == ""


def _gh_status_code() -> str:
    """The gh session's in-memory code, read without a request so a poll loop stays synchronous."""
    from sova.dashboard.services import setup_service

    return next((s.code for (flow, _), s in setup_service._reconnect_sessions.items() if flow == "gh"), "")


class TestAuthorizeGoogleChild:
    """The thin entry point the google flow runs as a child process."""

    def test_exit_codes(self, tmp_path: Path) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch

        from sova.awareness.auth import authorize

        cfg = SimpleNamespace(
            awareness=SimpleNamespace(), server=SimpleNamespace(log_max_bytes=1024, log_backup_count=2)
        )
        with (
            patch("sova.awareness.auth.authorize.load_config", return_value=cfg),
            patch("sova.utils.logging.setup_logging") as setup_logging,
        ):
            with patch("sova.awareness.auth.google_oauth.authenticate_google") as authenticate:
                assert authorize.main([str(tmp_path)]) == 0
                authenticate.assert_called_once_with(cfg.awareness)
            with patch("sova.awareness.auth.google_oauth.authenticate_google", side_effect=ValueError("bad")):
                assert authorize.main([str(tmp_path)]) == 1
            # The parent discards stderr, so the child must log to the project's shared file.
            assert setup_logging.call_args.kwargs["log_file"] == tmp_path / ".claude" / "sova.log"
        assert authorize.main([]) == 2

    def test_forget_cached_credentials_drops_only_the_configured_token(self, tmp_path: Path) -> None:
        from types import SimpleNamespace

        from sova.awareness.auth import google_oauth

        token = tmp_path / "token.pickle"
        other = tmp_path / "other.pickle"
        google_oauth._set_cached_creds(token, object())
        google_oauth._set_cached_creds(other, object())
        google_oauth.forget_cached_credentials(SimpleNamespace(gmail_token_path=str(token)))
        assert google_oauth._get_cached_creds(token) is None
        assert google_oauth._get_cached_creds(other) is not None
        google_oauth._creds_cache.clear()


class _FakeModelsProvider:
    """Fake provider exposing only ``list_available_models()``."""

    def __init__(self, models: list, *, delay: float = 0.0) -> None:
        self._models = models
        self._delay = delay
        self.call_count = 0

    async def list_available_models(self, *, allow_probe: bool = True):
        self.call_count += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        return list(self._models)


def _model(model_id: str = "m1", display_name: str = "M1", *, tier: str = "smart", source: str = "probed"):
    """Build one ModelInfo; every field a test does not care about has a default."""
    from sova.llm.models import ModelFamily, ModelInfo

    return ModelInfo(id=model_id, family=ModelFamily.ANTHROPIC, tier=tier, display_name=display_name, source=source)


class _FakeRuntimeForModels:
    """Fake AgentRuntime exposing the two members layer=runtime enumeration reads."""

    def __init__(self, model_ids, *, delay: float = 0.0, honours_model_override: bool = True) -> None:
        self._model_ids = model_ids
        self._delay = delay
        self.honours_model_override = honours_model_override

    async def list_available_models(self):
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._model_ids


@contextmanager
def _patch_models_env(provider, cfg=None):
    """Patch the config load and provider construction every test here needs."""
    with (
        patch(
            "sova.dashboard.services.models_service.load_config", return_value=cfg if cfg is not None else _fake_cfg()
        ),
        patch("sova.llm.provider.create_provider", return_value=provider),
    ):
        yield


@contextmanager
def _patch_runtime_env(runtime):
    """_patch_models_env's layer=runtime counterpart: config load + runtime construction."""
    with (
        patch("sova.dashboard.services.models_service.load_config", return_value=_fake_full_cfg()),
        patch("sova.ipc.runtime.create_runtime", return_value=runtime),
    ):
        yield


class TestReconnectAvailability:
    """reconnect_availability()/start_reconnect(): environment-aware reconnect gating (issue #1148).

    A browser-based `claude auth login` cannot complete from a non-loopback
    dashboard session, nor is it how a Vertex/Bedrock-routed CLI authenticates.
    """

    def test_non_loopback_is_blocked(self) -> None:
        from sova.dashboard.services.setup_service import reconnect_availability

        result = reconnect_availability(is_loopback_bind=False)
        assert result["can_reconnect"] is False
        assert result["reason"] == "non_loopback"

    def test_loopback_firstparty_can_reconnect(self) -> None:
        from sova.dashboard.services.setup_service import reconnect_availability
        from sova.llm.backends import Backend

        with (
            patch("sova.dashboard.services.setup_service._claude_cli_backend", return_value=Backend.FIRSTPARTY),
            patch("sova.dashboard.services.setup_service._is_headless_environment", return_value=False),
        ):
            result = reconnect_availability(is_loopback_bind=True)
        assert result == {"can_reconnect": True, "reason": "", "detail": ""}

    def test_vertex_routed_is_blocked(self) -> None:
        from sova.dashboard.services.setup_service import reconnect_availability
        from sova.llm.backends import Backend

        with (
            patch("sova.dashboard.services.setup_service._claude_cli_backend", return_value=Backend.VERTEX),
            patch("sova.dashboard.services.setup_service._is_headless_environment", return_value=False),
        ):
            result = reconnect_availability(is_loopback_bind=True)
        assert result["can_reconnect"] is False
        assert result["reason"] == "vertex_routed"
        assert "Vertex" in result["detail"]

    def test_bedrock_routed_is_blocked(self) -> None:
        from sova.dashboard.services.setup_service import reconnect_availability
        from sova.llm.backends import Backend

        with (
            patch("sova.dashboard.services.setup_service._claude_cli_backend", return_value=Backend.BEDROCK),
            patch("sova.dashboard.services.setup_service._is_headless_environment", return_value=False),
        ):
            result = reconnect_availability(is_loopback_bind=True)
        assert result["can_reconnect"] is False
        assert result["reason"] == "bedrock_routed"
        assert "Bedrock" in result["detail"]

    def test_non_loopback_checked_before_backend(self) -> None:
        """A non-loopback session is rejected without even probing the CLI backend."""
        from sova.dashboard.services.setup_service import reconnect_availability

        with patch("sova.dashboard.services.setup_service._claude_cli_backend") as mock_backend:
            result = reconnect_availability(is_loopback_bind=False)
        assert result["reason"] == "non_loopback"
        mock_backend.assert_not_called()

    def test_headless_is_blocked(self) -> None:
        """A loopback-bound session on a display-less server process (SSH
        session, container) must not offer a button that can never
        complete (edge case named in the spec)."""
        from sova.dashboard.services.setup_service import reconnect_availability
        from sova.llm.backends import Backend

        with (
            patch("sova.dashboard.services.setup_service._claude_cli_backend", return_value=Backend.FIRSTPARTY),
            patch("sova.dashboard.services.setup_service._is_headless_environment", return_value=True),
        ):
            result = reconnect_availability(is_loopback_bind=True)
        assert result["can_reconnect"] is False
        assert result["reason"] == "headless"

    def test_headless_checked_before_backend(self) -> None:
        """A headless session is rejected without even probing the CLI backend."""
        from sova.dashboard.services.setup_service import reconnect_availability

        with (
            patch("sova.dashboard.services.setup_service._is_headless_environment", return_value=True),
            patch("sova.dashboard.services.setup_service._claude_cli_backend") as mock_backend,
        ):
            result = reconnect_availability(is_loopback_bind=True)
        assert result["reason"] == "headless"
        mock_backend.assert_not_called()

    async def test_start_reconnect_blocked_non_loopback_never_spawns(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.setup_service import start_reconnect

        with patch("asyncio.create_subprocess_exec", AsyncMock()) as mock_spawn:
            result = await start_reconnect(tmp_path / "some-project", is_loopback_bind=False)

        assert result == {
            "status": "error",
            "reason": "non_loopback",
            "detail": "The dashboard is not bound to loopback; reconnect cannot be completed from this session.",
        }
        mock_spawn.assert_not_called()

    async def test_start_reconnect_blocked_vertex_routed_never_spawns(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.setup_service import start_reconnect
        from sova.llm.backends import Backend

        with (
            patch("sova.dashboard.services.setup_service._claude_cli_backend", return_value=Backend.VERTEX),
            patch("sova.dashboard.services.setup_service._is_headless_environment", return_value=False),
            patch("asyncio.create_subprocess_exec", AsyncMock()) as mock_spawn,
        ):
            result = await start_reconnect(tmp_path / "some-project", is_loopback_bind=True)

        assert result["status"] == "error"
        assert result["reason"] == "vertex_routed"
        mock_spawn.assert_not_called()

    async def test_start_reconnect_allowed_when_loopback_firstparty(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.setup_service import _reconnect_sessions, start_reconnect
        from sova.llm.backends import Backend

        process = _FakeProcess()
        with (
            patch("sova.dashboard.services.setup_service._claude_cli_backend", return_value=Backend.FIRSTPARTY),
            patch("sova.dashboard.services.setup_service._is_headless_environment", return_value=False),
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)),
        ):
            try:
                result = await start_reconnect(tmp_path / "some-project", is_loopback_bind=True)
            finally:
                _reconnect_sessions.clear()
                process.finish(0)

        assert result == {"status": "ok"}

    async def test_catalog_reports_reconnect_field(self, client: AsyncClient) -> None:
        """GET /api/connections surfaces the reconnect gate so the page can
        suppress/replace the login button for this session."""
        from sova.llm.backends import Backend

        with (
            patch("sova.dashboard.services.setup_service.load_config", return_value=_fake_full_cfg()),
            patch("sova.dashboard.services.setup_service._claude_cli_backend", return_value=Backend.VERTEX),
            patch("sova.dashboard.services.setup_service._is_headless_environment", return_value=False),
        ):
            resp = await client.get("/api/connections")
        data = resp.json()
        assert data["reconnect"]["can_reconnect"] is False
        assert data["reconnect"]["reason"] == "vertex_routed"

    async def test_reconnect_start_blocked_via_http_when_vertex_routed(self, client: AsyncClient) -> None:
        """The router wires request.app.state.is_loopback_bind through to
        start_reconnect(), and a Vertex-routed backend blocks it even on a
        loopback-bound dashboard (this client's default bind)."""
        from sova.llm.backends import Backend

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("sova.dashboard.services.setup_service._claude_cli_backend", return_value=Backend.VERTEX),
            patch("sova.dashboard.services.setup_service._is_headless_environment", return_value=False),
        ):
            resp = await client.post("/api/connections/reconnect/start", headers=_csrf_request_headers(client))
        data = resp.json()
        assert data["status"] == "error"
        assert data["reason"] == "vertex_routed"


class TestAvailableModelsAPI:
    @pytest.fixture(autouse=True)
    def _clear_models_cache(self):
        from sova.dashboard.services import models_service

        models_service._models_cache.clear()
        models_service._models_locks.clear()
        models_service._last_refresh_at.clear()
        yield
        models_service._models_cache.clear()
        models_service._models_locks.clear()
        models_service._last_refresh_at.clear()

    async def test_default_provider_returns_curated_response_shape(self, client: AsyncClient) -> None:
        from sova.llm.models import CURATED_MODELS

        provider = _FakeModelsProvider(list(CURATED_MODELS))
        with _patch_models_env(provider):
            resp = await client.get("/api/models/available", params={"layer": "llm"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["layer"] == "llm"
        assert data["provider_identity"] == "claude-code:firstparty"
        assert data["source"] == "curated"
        assert data["detail"] == ""
        assert data["cached"] is False
        assert "cached_at" in data
        assert len(data["models"]) == len(CURATED_MODELS)
        assert set(data["models"][0].keys()) == {"id", "family", "display_name", "tier"}

    async def test_empty_enumeration_is_a_valid_200(self, client: AsyncClient) -> None:
        provider = _FakeModelsProvider([])
        with _patch_models_env(provider):
            resp = await client.get("/api/models/available", params={"layer": "llm"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["models"] == []
        assert data["source"] == "unknown"

    async def test_empty_enumeration_is_never_cached(self, client: AsyncClient) -> None:
        """No in-tree provider returns ``[]`` (an outage is reported as ``None``,
        which becomes the curated fallback), so a zero-model result is a shape
        nothing here can vouch is stable and must not be pinned for the TTL."""
        provider = _FakeModelsProvider([])
        with _patch_models_env(provider):
            first = await client.get("/api/models/available", params={"layer": "llm"})
            second = await client.get("/api/models/available", params={"layer": "llm"})
        assert first.json()["cached"] is False
        assert second.json()["cached"] is False
        assert provider.call_count == 2

    async def test_real_enumeration_serializes_only_allowlisted_fields(self, client: AsyncClient) -> None:
        provider = _FakeModelsProvider([_model("claude-sonnet-5", "Sonnet")])
        with _patch_models_env(provider):
            resp = await client.get("/api/models/available", params={"layer": "llm"})
        data = resp.json()
        assert data["source"] == "probed"
        assert data["models"] == [
            {"id": "claude-sonnet-5", "family": "anthropic", "display_name": "Sonnet", "tier": "smart"}
        ]

    async def test_mixed_sources_report_mixed(self, client: AsyncClient) -> None:
        provider = _FakeModelsProvider([_model(), _model("m2", "M2", tier="fast", source="curated")])
        with _patch_models_env(provider):
            resp = await client.get("/api/models/available", params={"layer": "llm"})
        assert resp.json()["source"] == "mixed"

    async def test_no_secret_material_in_response_body(self, client: AsyncClient) -> None:
        provider = _FakeModelsProvider([_model("claude-x", "X")])
        with _patch_models_env(provider, _fake_cfg(api_key="sk-ant-secret-token")):
            resp = await client.get("/api/models/available", params={"layer": "llm"})
        assert "sk-ant-secret-token" not in resp.text
        assert "api_base" not in resp.text
        assert "api_key" not in resp.text
        assert "email" not in resp.text

    async def test_cache_hit_returns_cached_true(self, client: AsyncClient) -> None:
        provider = _FakeModelsProvider([_model()])
        with (
            patch("sova.dashboard.services.models_service.load_config", return_value=_fake_cfg()) as mock_load,
            patch("sova.llm.provider.create_provider", return_value=provider),
        ):
            first = await client.get("/api/models/available", params={"layer": "llm"})
            second = await client.get("/api/models/available", params={"layer": "llm"})
        assert first.json()["cached"] is False
        assert second.json()["cached"] is True
        assert second.json()["cached_at"] == first.json()["cached_at"]
        assert mock_load.call_count == 1
        assert provider.call_count == 1

    async def test_curated_result_is_never_cached(self, client: AsyncClient) -> None:
        """A curated fallback means enumeration did not produce a real answer
        (unconfigured provider, not authenticated, misconfig, timeout). It
        must not be pinned at the service layer: the next poll retries."""
        from sova.llm.models import CURATED_MODELS

        provider = _FakeModelsProvider(list(CURATED_MODELS))
        with _patch_models_env(provider):
            first = await client.get("/api/models/available", params={"layer": "llm"})
            second = await client.get("/api/models/available", params={"layer": "llm"})
        assert first.json()["cached"] is False
        assert second.json()["cached"] is False
        assert provider.call_count == 2

    async def test_concurrent_cold_requests_share_one_enumeration_pass(self, client: AsyncClient) -> None:
        provider = _FakeModelsProvider([_model()], delay=0.05)
        with _patch_models_env(provider):
            first, second = await asyncio.gather(
                client.get("/api/models/available", params={"layer": "llm"}),
                client.get("/api/models/available", params={"layer": "llm"}),
            )
        assert first.status_code == 200
        assert second.status_code == 200
        assert provider.call_count == 1

    async def test_misconfigured_provider_returns_200_curated(self, client: AsyncClient) -> None:
        with (
            patch("sova.dashboard.services.models_service.load_config", return_value=_fake_cfg()),
            patch(
                "sova.llm.provider.create_provider",
                side_effect=ComponentMisconfiguredError("Unknown LLM provider: 'nope'"),
            ),
        ):
            resp = await client.get("/api/models/available", params={"layer": "llm"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "curated"
        assert data["detail"] == "Unknown LLM provider: 'nope'"
        assert data["cached"] is False

    async def test_a_provider_failure_that_is_not_author_written_never_reaches_the_detail(
        self, client: AsyncClient
    ) -> None:
        with (
            patch("sova.dashboard.services.models_service.load_config", return_value=_fake_cfg()),
            patch("sova.llm.provider.create_provider", side_effect=ValueError("bad key sk-SECRET")),
        ):
            resp = await client.get("/api/models/available", params={"layer": "llm"})
        assert resp.status_code == 200
        assert resp.json()["detail"] == "LLM provider is misconfigured or its SDK is not installed"
        assert "sk-SECRET" not in resp.text

    async def test_enumeration_timeout_falls_back_to_curated_and_is_not_cached(self, client: AsyncClient) -> None:
        provider = _FakeModelsProvider([_model()], delay=0.2)
        with (
            _patch_models_env(provider),
            patch("sova.dashboard.services.models_service._ENUMERATION_TIMEOUT_SECONDS", 0.01),
        ):
            resp = await client.get("/api/models/available", params={"layer": "llm"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "curated"
        assert data["cached"] is False
        assert "timed out" in data["detail"].lower()

    async def test_config_load_failure_returns_503(self, client: AsyncClient) -> None:
        with patch("sova.dashboard.services.models_service.load_config", side_effect=RuntimeError("config unreadable")):
            resp = await client.get("/api/models/available", params={"layer": "llm"})
        assert resp.status_code == 503
        assert "config unreadable" not in resp.text

    async def test_layer_runtime_reachable_returns_runtime_reported_models(self, client: AsyncClient) -> None:
        runtime = _FakeRuntimeForModels(["claude-sonnet-5-5"])
        with _patch_runtime_env(runtime):
            resp = await client.get("/api/models/available", params={"layer": "runtime"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["layer"] == "runtime"
        assert data["provider_identity"] == "claude-code"
        assert data["source"] == "runtime"
        # display_name is the curated label for an ID SOVA already names, so the
        # same model does not read differently in the two layers' dropdowns.
        assert data["models"] == [
            {"id": "claude-sonnet-5-5", "family": "anthropic", "display_name": "Claude Sonnet 5.5", "tier": "fast"}
        ]
        assert data["honours_model_override"] is True

    async def test_layer_runtime_unnamed_model_id_keeps_itself_as_its_label(self, client: AsyncClient) -> None:
        """An ID with no curated entry must still be offered, labelled by itself."""
        runtime = _FakeRuntimeForModels(["some-local-model"])
        with _patch_runtime_env(runtime):
            resp = await client.get("/api/models/available", params={"layer": "runtime"})
        assert resp.json()["models"][0]["display_name"] == "some-local-model"

    async def test_layer_runtime_reports_model_override_capability(self, client: AsyncClient) -> None:
        """A runtime that drops a caller-supplied model says so in the payload:
        both model selectors hide themselves on this flag rather than on a
        hardcoded runtime name."""
        runtime = _FakeRuntimeForModels(["gpt-5-codex"], honours_model_override=False)
        with _patch_runtime_env(runtime):
            resp = await client.get("/api/models/available", params={"layer": "runtime"})
        assert resp.json()["honours_model_override"] is False

    async def test_layer_runtime_unavailable_returns_200_empty(self, client: AsyncClient) -> None:
        """``None`` (CLI missing/unauthenticated) is reported as a real 200
        with an empty catalog and a distinct source, not a 503: a dropdown
        consuming this must hide, not error out."""
        runtime = _FakeRuntimeForModels(None)
        with _patch_runtime_env(runtime):
            resp = await client.get("/api/models/available", params={"layer": "runtime"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["models"] == []
        assert data["source"] == "unavailable"

    async def test_layer_runtime_reachable_empty_is_distinct_from_unavailable(self, client: AsyncClient) -> None:
        """A reachable runtime with no catalog of its own (``[]``) must not be
        reported the same way as an unreachable one (``None``)."""
        runtime = _FakeRuntimeForModels([])
        with _patch_runtime_env(runtime):
            resp = await client.get("/api/models/available", params={"layer": "runtime"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["models"] == []
        assert data["source"] == "reachable"

    async def test_layer_runtime_timeout_returns_200_unavailable(self, client: AsyncClient) -> None:
        runtime = _FakeRuntimeForModels(["m1"], delay=0.2)
        with (
            _patch_runtime_env(runtime),
            patch("sova.dashboard.services.models_service._ENUMERATION_TIMEOUT_SECONDS", 0.01),
        ):
            resp = await client.get("/api/models/available", params={"layer": "runtime"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "unavailable"
        assert "timed out" in data["detail"].lower()

    async def test_layer_runtime_unknown_runtime_type_returns_200_unavailable(self, client: AsyncClient) -> None:
        with patch(
            "sova.dashboard.services.models_service.load_config", return_value=_fake_full_cfg(agent_runtime="bogus")
        ):
            resp = await client.get("/api/models/available", params={"layer": "runtime"})
        assert resp.status_code == 200
        assert resp.json()["source"] == "unavailable"

    async def test_llm_and_runtime_layers_do_not_share_a_cache_entry(self, client: AsyncClient) -> None:
        """The cache key must be layer-aware: enumerating one layer must not
        serve a cached answer meant for the other layer of the same project."""
        llm_provider = _FakeModelsProvider([_model("claude-llm-model")])
        runtime = _FakeRuntimeForModels(["claude-runtime-model"])
        with (
            _patch_runtime_env(runtime),
            patch("sova.llm.provider.create_provider", return_value=llm_provider),
        ):
            llm_resp = await client.get("/api/models/available", params={"layer": "llm"})
            runtime_resp = await client.get("/api/models/available", params={"layer": "runtime"})
        assert llm_resp.json()["models"][0]["id"] == "claude-llm-model"
        assert runtime_resp.json()["models"][0]["id"] == "claude-runtime-model"

    async def test_missing_layer_returns_422(self, client: AsyncClient) -> None:
        resp = await client.get("/api/models/available")
        assert resp.status_code == 422

    async def test_invalid_layer_returns_422(self, client: AsyncClient) -> None:
        resp = await client.get("/api/models/available", params={"layer": "bogus"})
        assert resp.status_code == 422

    async def test_refresh_clears_provider_level_enumeration_cache(self, client: AsyncClient) -> None:
        provider = _FakeModelsProvider([_model()])
        with (
            _patch_models_env(provider),
            patch("sova.llm.client.clear_enumeration") as mock_clear,
        ):
            resp = await client.get("/api/models/available", params={"layer": "llm", "refresh": "1"})
        assert resp.status_code == 200
        mock_clear.assert_called_once()

    async def test_refresh_does_not_wipe_the_global_cache_when_the_provider_is_unusable(
        self, client: AsyncClient
    ) -> None:
        """clear_enumeration() wipes every identity process-wide, so a refresh
        that cannot enumerate anything must not charge that cost to every other
        project sharing the process."""
        with (
            patch("sova.dashboard.services.models_service.load_config", return_value=_fake_cfg()),
            patch("sova.llm.provider.create_provider", side_effect=ValueError("Unknown LLM provider")),
            patch("sova.llm.client.clear_enumeration") as mock_clear,
        ):
            resp = await client.get("/api/models/available", params={"layer": "llm", "refresh": "1"})
        assert resp.status_code == 200
        mock_clear.assert_not_called()

    async def test_refresh_allowance_not_consumed_when_provider_misconfigured(self, client: AsyncClient) -> None:
        """A refresh that never actually ran (create_provider failed) must not
        spend the 60s throttle window: the very next ?refresh=1 should still
        go through rather than getting a 429 for a refresh that never happened."""
        with (
            patch("sova.dashboard.services.models_service.load_config", return_value=_fake_cfg()),
            patch("sova.llm.provider.create_provider", side_effect=ValueError("Unknown LLM provider")),
        ):
            first = await client.get("/api/models/available", params={"layer": "llm", "refresh": "1"})
            second = await client.get("/api/models/available", params={"layer": "llm", "refresh": "1"})
        assert first.status_code == 200
        assert second.status_code == 200

    async def test_refresh_allowance_not_consumed_when_import_error(self, client: AsyncClient) -> None:
        """create_provider() can also raise ImportError (missing optional SDK
        extra for litellm/anthropic providers); that must be handled the same
        as a ValueError misconfiguration, not propagate as a 503."""
        with (
            patch("sova.dashboard.services.models_service.load_config", return_value=_fake_cfg()),
            patch("sova.llm.provider.create_provider", side_effect=ImportError("litellm not installed")),
        ):
            resp = await client.get("/api/models/available", params={"layer": "llm"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["source"] == "curated"
        assert data["detail"] == "LLM provider is misconfigured or its SDK is not installed"
        assert "litellm not installed" not in str(data)

    async def test_refresh_allowance_not_consumed_when_config_load_fails(self, client: AsyncClient) -> None:
        """A refresh that fails before create_provider is even reached (the
        config load itself errors) must not spend the throttle window either."""
        with patch("sova.dashboard.services.models_service.load_config", side_effect=RuntimeError("config unreadable")):
            first = await client.get("/api/models/available", params={"layer": "llm", "refresh": "1"})
        assert first.status_code == 503

        provider = _FakeModelsProvider([_model()])
        with _patch_models_env(provider):
            second = await client.get("/api/models/available", params={"layer": "llm", "refresh": "1"})
        assert second.status_code == 200

    async def test_refresh_rate_limited_returns_429_with_retry_after(self, client: AsyncClient) -> None:
        provider = _FakeModelsProvider([_model()])
        with _patch_models_env(provider):
            first = await client.get("/api/models/available", params={"layer": "llm", "refresh": "1"})
            second = await client.get("/api/models/available", params={"layer": "llm", "refresh": "1"})
        assert first.status_code == 200
        assert second.status_code == 429
        assert "Retry-After" in second.headers
        assert int(second.headers["Retry-After"]) > 0

    async def test_plain_get_is_never_rate_limited(self, client: AsyncClient) -> None:
        provider = _FakeModelsProvider([_model()])
        with _patch_models_env(provider):
            first = await client.get("/api/models/available", params={"layer": "llm"})
            second = await client.get("/api/models/available", params={"layer": "llm"})
        assert first.status_code == 200
        assert second.status_code == 200

    def test_enumeration_timeout_exceeds_claude_code_probe_bounds(self) -> None:
        """A budget below one auth probe plus one model probe cancels mid-probe,
        banks no outcome, and turns the "next poll retries" contract into a
        permanent curated fallback. Pins the floor against drift on either side."""
        from sova.dashboard.services import models_service
        from sova.llm.providers.claude_code import _AUTH_CHECK_TIMEOUT, _MODEL_PROBE_TIMEOUT

        assert models_service._ENUMERATION_TIMEOUT_SECONDS > _AUTH_CHECK_TIMEOUT + _MODEL_PROBE_TIMEOUT

    async def test_project_isolation_separate_cache_entries(self, tmp_path: Path) -> None:
        from sova.dashboard.services import models_service

        project_a = tmp_path / "a"
        project_b = tmp_path / "b"
        project_a.mkdir()
        project_b.mkdir()

        provider_a = _FakeModelsProvider([_model("model-a", "A")])
        provider_b = _FakeModelsProvider([_model("model-b", "B")])

        def _load_config(path):
            return _fake_cfg(provider="claude-code" if path == project_a else "ollama")

        def _create_provider(llm_cfg, _project_dir=None):
            return provider_a if llm_cfg.provider == "claude-code" else provider_b

        with (
            patch("sova.dashboard.services.models_service.load_config", side_effect=_load_config),
            patch("sova.llm.provider.create_provider", side_effect=_create_provider),
        ):
            result_a = await models_service.get_available_models(project_a)
            result_b = await models_service.get_available_models(project_b)
            # Second call for project_a must hit its own cache, not project_b's.
            result_a_again = await models_service.get_available_models(project_a)

        assert result_a["models"][0]["id"] == "model-a"
        assert result_b["models"][0]["id"] == "model-b"
        assert result_a_again["cached"] is True
        assert provider_a.call_count == 1
        assert provider_b.call_count == 1

    async def test_stale_project_entries_are_pruned(self, tmp_path: Path) -> None:
        """A project dir untouched for far longer than the cache TTL must not
        accumulate forever: its cache/lock/refresh entries are dropped on a
        later, unrelated request rather than held for the life of the
        process."""
        import time

        from sova.dashboard.services import models_service

        stale_dir = tmp_path / "stale"
        fresh_dir = tmp_path / "fresh"
        stale_dir.mkdir()
        fresh_dir.mkdir()

        # time.monotonic()'s reference point is undefined (CPython docs) and
        # can be small on a freshly started process/container, so a literal
        # 0.0 is not reliably "long ago": it must be relative to the clock
        # actually in use, or this flakes on exactly that kind of runner.
        stale_timestamp = time.monotonic() - (models_service._PRUNE_STALE_AFTER_SECONDS * 2)
        stale_key = str(stale_dir.resolve())
        models_service._models_cache[stale_key] = (stale_timestamp, {"fake": "entry"})
        models_service._last_refresh_at[stale_key] = stale_timestamp
        models_service._models_locks[stale_key] = asyncio.Lock()

        provider = _FakeModelsProvider([_model()])
        with _patch_models_env(provider):
            await models_service.get_available_models(fresh_dir)

        assert stale_key not in models_service._models_cache
        assert stale_key not in models_service._last_refresh_at
        assert stale_key not in models_service._models_locks

    async def test_held_lock_on_stale_entry_is_not_pruned(self, tmp_path: Path) -> None:
        """A lock currently held must survive pruning even if its cache/refresh
        entries look stale, since deleting it out from under a waiter would let
        a concurrent setdefault() hand out a second Lock for the same key."""
        import time

        from sova.dashboard.services import models_service

        busy_dir = tmp_path / "busy"
        busy_dir.mkdir()
        busy_key = str(busy_dir.resolve())

        lock = asyncio.Lock()
        models_service._models_locks[busy_key] = lock

        async with lock:
            models_service._prune_stale_entries(time.monotonic() + models_service._PRUNE_STALE_AFTER_SECONDS * 2)
            assert busy_key in models_service._models_locks
            assert models_service._models_locks[busy_key] is lock
