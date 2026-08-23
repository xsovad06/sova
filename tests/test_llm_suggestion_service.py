"""Tests for the LLM action suggestion service."""

from __future__ import annotations

import builtins
import json
import os
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cachetools import TTLCache

from sova.dashboard.services.llm_suggestion_service import (
    _PR_ACTION_LABELS,
    _make_cache_key,
    clear_cache,
    get_llm_suggestion,
)


@pytest.fixture(autouse=True)
def reset_cache() -> None:
    clear_cache()
    yield  # type: ignore[misc]
    clear_cache()


@pytest.fixture(autouse=True)
def _reset_module_state() -> None:
    """Reset module-level state between tests."""
    import sova.dashboard.services.llm_suggestion_service as mod

    mod._warned_no_credentials = False
    mod._vertex_credentials = None
    yield  # type: ignore[misc]
    mod._warned_no_credentials = False
    mod._vertex_credentials = None


def _make_httpx_response(action_id: str, reasoning: str = "test reason", status_code: int = 200) -> MagicMock:
    """Build a mock httpx response returning valid Anthropic Messages API JSON."""
    body = {
        "content": [{"type": "text", "text": json.dumps({"action_id": action_id, "reasoning": reasoning})}],
        "model": "claude-haiku-4-5-20251001",
        "stop_reason": "end_turn",
    }
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        import httpx

        resp.raise_for_status.side_effect = httpx.HTTPStatusError("error", request=MagicMock(), response=resp)
    return resp


def _vertex_env(project_id: str = "test-project", region: str = "us-east5") -> dict[str, str]:
    """Env dict for Vertex AI backend (no ANTHROPIC_API_KEY)."""
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["ANTHROPIC_VERTEX_PROJECT_ID"] = project_id
    env["CLOUD_ML_REGION"] = region
    return env


@contextmanager
def _mock_api(mock_client: AsyncMock, api_key: str = "sk-test-key"):
    """Patch env, httpx.AsyncClient, and config for a typical Anthropic direct API test."""
    ctx_manager = AsyncMock()
    ctx_manager.__aenter__ = AsyncMock(return_value=mock_client)
    ctx_manager.__aexit__ = AsyncMock(return_value=False)
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_VERTEX_PROJECT_ID"}
    env["ANTHROPIC_API_KEY"] = api_key
    with (
        patch.dict(os.environ, env, clear=True),
        patch("sova.dashboard.services.llm_suggestion_service.httpx.AsyncClient", return_value=ctx_manager),
        patch("sova.dashboard.services.llm_suggestion_service._is_enabled", return_value=True),
    ):
        yield


@contextmanager
def _mock_vertex_api(mock_client: AsyncMock, project_id: str = "test-project", region: str = "us-east5"):
    """Patch env, httpx.AsyncClient, google.auth, and config for a Vertex AI API test."""
    ctx_manager = AsyncMock()
    ctx_manager.__aenter__ = AsyncMock(return_value=mock_client)
    ctx_manager.__aexit__ = AsyncMock(return_value=False)
    mock_creds = MagicMock()
    mock_creds.token = "fake-gcp-token"
    mock_creds.expired = False
    with (
        patch.dict(os.environ, _vertex_env(project_id, region), clear=True),
        patch("sova.dashboard.services.llm_suggestion_service.httpx.AsyncClient", return_value=ctx_manager),
        patch("sova.dashboard.services.llm_suggestion_service._is_enabled", return_value=True),
        patch("sova.dashboard.services.llm_suggestion_service._get_vertex_token", return_value="fake-gcp-token"),
    ):
        yield


def _kwargs(**overrides: object) -> dict:
    defaults: dict = {
        "pr_number": 378,
        "deterministic_state": "pr_sova_pending",
        "deterministic_action_id": "review_pr",
        "pr_computed_state": "approved_ci_green",
        "has_sova_review": False,
        "sova_verdict": None,
        "mergeable": "MERGEABLE",
        "review_decision": "APPROVED",
        "ci_passed": True,
        "external_reviews_enabled": True,
    }
    defaults.update(overrides)
    return defaults


class TestGetLlmSuggestion:
    async def test_calls_api_and_parses_response(self) -> None:
        mock_resp = _make_httpx_response("integrate", "PR is approved and CI green")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        with _mock_api(mock_client):
            result = await get_llm_suggestion(**_kwargs())

        assert result is not None
        assert result["action_id"] == "integrate"
        assert result["action_label"] == "Integrate PR"
        assert result["reasoning"] == "PR is approved and CI green"
        assert result["disagrees"] is True

        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        assert call_kwargs[0][0] == "https://api.anthropic.com/v1/messages"
        req_body = call_kwargs[1]["json"]
        assert req_body["model"] == "claude-haiku-4-5-20251001"
        assert req_body["max_tokens"] == 200

    async def test_disagrees_false_when_llm_matches_deterministic(self) -> None:
        mock_resp = _make_httpx_response("review_pr")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        with _mock_api(mock_client):
            result = await get_llm_suggestion(**_kwargs(deterministic_action_id="review_pr"))

        assert result is not None
        assert result["disagrees"] is False

    async def test_returns_none_when_config_disabled(self) -> None:
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}),
            patch("sova.dashboard.services.llm_suggestion_service._is_enabled", return_value=False),
        ):
            result = await get_llm_suggestion(**_kwargs())
        assert result is None

    async def test_returns_none_when_no_credentials(self) -> None:
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_VERTEX_PROJECT_ID")}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("sova.dashboard.services.llm_suggestion_service._is_enabled", return_value=True),
        ):
            result = await get_llm_suggestion(**_kwargs())
        assert result is None

    async def test_returns_none_when_api_key_empty_string(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_VERTEX_PROJECT_ID"}
        env["ANTHROPIC_API_KEY"] = ""
        with (
            patch.dict(os.environ, env, clear=True),
            patch("sova.dashboard.services.llm_suggestion_service._is_enabled", return_value=True),
        ):
            result = await get_llm_suggestion(**_kwargs())
        assert result is None

    async def test_warns_once_on_missing_credentials(self) -> None:
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_VERTEX_PROJECT_ID")}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("sova.dashboard.services.llm_suggestion_service._is_enabled", return_value=True),
            patch("sova.dashboard.services.llm_suggestion_service.log") as mock_log,
        ):
            await get_llm_suggestion(**_kwargs())
            await get_llm_suggestion(**_kwargs(pr_number=999))

        assert mock_log.warning.call_count == 1

    async def test_returns_none_on_http_error(self) -> None:
        mock_resp = _make_httpx_response("integrate", status_code=401)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        with _mock_api(mock_client):
            result = await get_llm_suggestion(**_kwargs())
        assert result is None

    async def test_returns_none_on_invalid_action_id(self) -> None:
        mock_resp = _make_httpx_response("not_a_valid_action")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        with _mock_api(mock_client):
            result = await get_llm_suggestion(**_kwargs())
        assert result is None

    async def test_returns_none_on_malformed_json(self) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "content": [{"type": "text", "text": "not json at all"}],
        }
        resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=resp)

        with _mock_api(mock_client):
            result = await get_llm_suggestion(**_kwargs())
        assert result is None

    async def test_strips_markdown_code_fences_from_response(self) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "content": [{"type": "text", "text": '```json\n{"action_id": "integrate", "reasoning": "ready"}\n```'}],
        }
        resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=resp)

        with _mock_api(mock_client):
            result = await get_llm_suggestion(**_kwargs())
        assert result is not None
        assert result["action_id"] == "integrate"

    async def test_caches_result_second_call_skips_api(self) -> None:
        mock_resp = _make_httpx_response("integrate")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        with _mock_api(mock_client):
            result1 = await get_llm_suggestion(**_kwargs())
            result2 = await get_llm_suggestion(**_kwargs())

        assert result1 == result2
        assert mock_client.post.call_count == 1

    async def test_different_pr_numbers_are_cached_separately(self) -> None:
        responses = [
            _make_httpx_response("integrate"),
            _make_httpx_response("review_pr"),
        ]
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=responses)

        with _mock_api(mock_client):
            result_a = await get_llm_suggestion(**_kwargs(pr_number=100))
            result_b = await get_llm_suggestion(**_kwargs(pr_number=200))

        assert result_a["action_id"] == "integrate"
        assert result_b["action_id"] == "review_pr"
        assert mock_client.post.call_count == 2

    async def test_different_states_are_cached_separately(self) -> None:
        responses = [
            _make_httpx_response("integrate"),
            _make_httpx_response("address_pr"),
        ]
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=responses)

        with _mock_api(mock_client):
            result_a = await get_llm_suggestion(**_kwargs(deterministic_state="pr_sova_pending"))
            result_b = await get_llm_suggestion(**_kwargs(deterministic_state="pr_awaiting_review"))

        assert result_a["action_id"] == "integrate"
        assert result_b["action_id"] == "address_pr"

    async def test_expired_cache_entry_triggers_new_call(self) -> None:
        import sova.dashboard.services.llm_suggestion_service as mod

        fake_time = [0.0]
        test_cache: TTLCache[str, dict] = TTLCache(maxsize=100, ttl=mod._CACHE_TTL, timer=lambda: fake_time[0])

        mock_resp = _make_httpx_response("integrate")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        with _mock_api(mock_client):
            original_cache = mod._cache
            mod._cache = test_cache
            try:
                await get_llm_suggestion(**_kwargs())
                assert len(mod._cache) == 1

                fake_time[0] = mod._CACHE_TTL + 1

                await get_llm_suggestion(**_kwargs())
                assert mock_client.post.call_count == 2
            finally:
                mod._cache = original_cache

    async def test_returns_none_on_prompt_format_error(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_VERTEX_PROJECT_ID"}
        env["ANTHROPIC_API_KEY"] = "sk-test-key"
        with (
            patch.dict(os.environ, env, clear=True),
            patch("sova.dashboard.services.llm_suggestion_service._is_enabled", return_value=True),
            patch(
                "sova.dashboard.services.llm_suggestion_service._PROMPT",
                "{missing_key_that_does_not_exist}",
            ),
        ):
            result = await get_llm_suggestion(**_kwargs())
        assert result is None

    async def test_state_change_invalidates_cache(self) -> None:
        responses = [
            _make_httpx_response("review_pr"),
            _make_httpx_response("integrate"),
        ]
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=responses)

        with _mock_api(mock_client):
            result_a = await get_llm_suggestion(**_kwargs(deterministic_state="pr_sova_pending"))
            result_b = await get_llm_suggestion(**_kwargs(deterministic_state="pr_approved"))

        assert result_a["action_id"] == "review_pr"
        assert result_b["action_id"] == "integrate"
        assert mock_client.post.call_count == 2

    async def test_sends_correct_headers_direct_api(self) -> None:
        mock_resp = _make_httpx_response("integrate")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        with _mock_api(mock_client, api_key="sk-test-key-123"):
            await get_llm_suggestion(**_kwargs())

        call_kwargs = mock_client.post.call_args
        headers = call_kwargs[1]["headers"]
        assert headers["x-api-key"] == "sk-test-key-123"
        assert headers["anthropic-version"] == "2023-06-01"
        assert headers["content-type"] == "application/json"

    async def test_returns_none_on_timeout(self) -> None:
        import httpx

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))

        with _mock_api(mock_client):
            result = await get_llm_suggestion(**_kwargs())
        assert result is None


class TestVertexAiBackend:
    async def test_calls_vertex_endpoint_with_bearer_token(self) -> None:
        mock_resp = _make_httpx_response("integrate", "CI green and approved")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        with _mock_vertex_api(mock_client, project_id="my-proj", region="us-east5"):
            result = await get_llm_suggestion(**_kwargs())

        assert result is not None
        assert result["action_id"] == "integrate"
        assert result["disagrees"] is True

        call_kwargs = mock_client.post.call_args
        url = call_kwargs[0][0]
        assert "us-east5-aiplatform.googleapis.com" in url
        assert "my-proj" in url
        assert "rawPredict" in url

        headers = call_kwargs[1]["headers"]
        assert headers["Authorization"] == "Bearer fake-gcp-token"
        assert "x-api-key" not in headers

    async def test_vertex_request_body_includes_anthropic_version(self) -> None:
        mock_resp = _make_httpx_response("review_pr")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        with _mock_vertex_api(mock_client):
            await get_llm_suggestion(**_kwargs())

        call_kwargs = mock_client.post.call_args
        req_body = call_kwargs[1]["json"]
        assert "anthropic_version" in req_body
        assert "model" not in req_body
        assert req_body["max_tokens"] == 200

    async def test_vertex_preferred_over_direct_api(self) -> None:
        """When both ANTHROPIC_VERTEX_PROJECT_ID and ANTHROPIC_API_KEY are set, Vertex wins."""
        mock_resp = _make_httpx_response("integrate")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        ctx_manager = AsyncMock()
        ctx_manager.__aenter__ = AsyncMock(return_value=mock_client)
        ctx_manager.__aexit__ = AsyncMock(return_value=False)

        env = dict(os.environ)
        env["ANTHROPIC_VERTEX_PROJECT_ID"] = "dual-project"
        env["CLOUD_ML_REGION"] = "us-east5"
        env["ANTHROPIC_API_KEY"] = "sk-also-set"
        with (
            patch.dict(os.environ, env, clear=True),
            patch("sova.dashboard.services.llm_suggestion_service.httpx.AsyncClient", return_value=ctx_manager),
            patch("sova.dashboard.services.llm_suggestion_service._is_enabled", return_value=True),
            patch("sova.dashboard.services.llm_suggestion_service._get_vertex_token", return_value="tok"),
        ):
            await get_llm_suggestion(**_kwargs())

        call_kwargs = mock_client.post.call_args
        url = call_kwargs[0][0]
        assert "aiplatform.googleapis.com" in url

    async def test_vertex_returns_none_on_token_error(self) -> None:
        mock_client = AsyncMock()
        ctx_manager = AsyncMock()
        ctx_manager.__aenter__ = AsyncMock(return_value=mock_client)
        ctx_manager.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.dict(os.environ, _vertex_env(), clear=True),
            patch("sova.dashboard.services.llm_suggestion_service.httpx.AsyncClient", return_value=ctx_manager),
            patch("sova.dashboard.services.llm_suggestion_service._is_enabled", return_value=True),
            patch(
                "sova.dashboard.services.llm_suggestion_service._get_vertex_token",
                side_effect=RuntimeError("ADC failed"),
            ),
        ):
            result = await get_llm_suggestion(**_kwargs())
        assert result is None

    async def test_vertex_default_region(self) -> None:
        mock_resp = _make_httpx_response("integrate")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        ctx_manager = AsyncMock()
        ctx_manager.__aenter__ = AsyncMock(return_value=mock_client)
        ctx_manager.__aexit__ = AsyncMock(return_value=False)

        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "CLOUD_ML_REGION")}
        env["ANTHROPIC_VERTEX_PROJECT_ID"] = "proj"
        with (
            patch.dict(os.environ, env, clear=True),
            patch("sova.dashboard.services.llm_suggestion_service.httpx.AsyncClient", return_value=ctx_manager),
            patch("sova.dashboard.services.llm_suggestion_service._is_enabled", return_value=True),
            patch("sova.dashboard.services.llm_suggestion_service._get_vertex_token", return_value="tok"),
        ):
            await get_llm_suggestion(**_kwargs())

        url = mock_client.post.call_args[0][0]
        assert "us-east5-aiplatform.googleapis.com" in url


class TestGetVertexToken:
    async def test_returns_token_from_cached_credentials(self) -> None:
        import sova.dashboard.services.llm_suggestion_service as mod

        mock_creds = MagicMock()
        mock_creds.token = "cached-token-123"
        mock_creds.expired = False
        mod._vertex_credentials = mock_creds

        try:
            token = await mod._get_vertex_token()
            assert token == "cached-token-123"
        finally:
            mod._vertex_credentials = None

    async def test_passes_cloud_platform_scope_to_adc(self) -> None:
        import sys
        import types

        import sova.dashboard.services.llm_suggestion_service as mod

        mod._vertex_credentials = None
        mock_creds = MagicMock()
        mock_creds.token = "scoped-token"
        mock_creds.expired = False

        mock_auth = types.ModuleType("google.auth")
        mock_auth.default = MagicMock()  # type: ignore[attr-defined]
        saved_google = sys.modules.get("google")
        saved_google_auth = sys.modules.get("google.auth")
        if "google" not in sys.modules:
            sys.modules["google"] = types.ModuleType("google")
        _sentinel = object()
        saved_auth_attr = getattr(sys.modules["google"], "auth", _sentinel)
        sys.modules["google"].auth = mock_auth  # type: ignore[attr-defined]
        sys.modules["google.auth"] = mock_auth

        async def _capture_to_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
            _capture_to_thread.captured_kwargs = kwargs  # type: ignore[attr-defined]
            _capture_to_thread.captured_fn = fn  # type: ignore[attr-defined]
            return (mock_creds, "project-id")

        try:
            target = "sova.dashboard.services.llm_suggestion_service.asyncio.to_thread"
            with patch(target, side_effect=_capture_to_thread):
                await mod._get_vertex_token()
                assert _capture_to_thread.captured_fn is mock_auth.default  # type: ignore[attr-defined]
                assert _capture_to_thread.captured_kwargs["scopes"] == [  # type: ignore[attr-defined]
                    "https://www.googleapis.com/auth/cloud-platform"
                ]
        finally:
            mod._vertex_credentials = None
            if saved_google_auth is None:
                sys.modules.pop("google.auth", None)
            else:
                sys.modules["google.auth"] = saved_google_auth
            if saved_google is None:
                sys.modules.pop("google", None)
            else:
                sys.modules["google"] = saved_google
                if saved_auth_attr is _sentinel:
                    if hasattr(sys.modules["google"], "auth"):
                        delattr(sys.modules["google"], "auth")
                else:
                    sys.modules["google"].auth = saved_auth_attr  # type: ignore[attr-defined]

    async def test_raises_import_error_when_google_auth_missing(self) -> None:
        import sova.dashboard.services.llm_suggestion_service as mod

        mod._vertex_credentials = None
        real_import = builtins.__import__

        def _block_google_auth(name: str, *a: Any, **kw: Any) -> Any:
            if name == "google.auth" or name.startswith("google.auth."):
                raise ImportError("No module named 'google.auth'")
            return real_import(name, *a, **kw)

        try:
            with patch.object(builtins, "__import__", side_effect=_block_google_auth):
                with pytest.raises(ImportError, match="google-auth"):
                    await mod._get_vertex_token()
        finally:
            mod._vertex_credentials = None


class TestMakeCacheKey:
    def test_unique_by_pr_number(self) -> None:
        k1 = _make_cache_key(1, "pr_sova_pending", "approved_ci_green")
        k2 = _make_cache_key(2, "pr_sova_pending", "approved_ci_green")
        assert k1 != k2

    def test_unique_by_deterministic_state(self) -> None:
        k1 = _make_cache_key(1, "pr_sova_pending", "approved_ci_green")
        k2 = _make_cache_key(1, "pr_awaiting_review", "approved_ci_green")
        assert k1 != k2

    def test_unique_by_computed_state(self) -> None:
        k1 = _make_cache_key(1, "pr_sova_pending", "approved_ci_green")
        k2 = _make_cache_key(1, "pr_sova_pending", "approved")
        assert k1 != k2

    def test_same_args_same_key(self) -> None:
        k1 = _make_cache_key(42, "pr_approved", "approved_ci_green")
        k2 = _make_cache_key(42, "pr_approved", "approved_ci_green")
        assert k1 == k2


class TestPrActionLabels:
    def test_all_pr_actions_have_labels(self) -> None:
        expected = {"review_pr", "address_review", "address_pr", "integrate"}
        assert set(_PR_ACTION_LABELS.keys()) == expected

    def test_labels_are_non_empty_strings(self) -> None:
        for action_id, label in _PR_ACTION_LABELS.items():
            assert isinstance(label, str) and label, f"{action_id} has empty label"


class TestDetectBackend:
    def test_returns_vertex_when_project_id_set(self) -> None:
        from sova.dashboard.services.llm_suggestion_service import _detect_backend

        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        env["ANTHROPIC_VERTEX_PROJECT_ID"] = "my-project"
        with patch.dict(os.environ, env, clear=True):
            assert _detect_backend() == "vertex"

    def test_returns_anthropic_when_api_key_set(self) -> None:
        from sova.dashboard.services.llm_suggestion_service import _detect_backend

        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_VERTEX_PROJECT_ID"}
        env["ANTHROPIC_API_KEY"] = "sk-test"
        with patch.dict(os.environ, env, clear=True):
            assert _detect_backend() == "anthropic"

    def test_vertex_preferred_over_direct(self) -> None:
        from sova.dashboard.services.llm_suggestion_service import _detect_backend

        with patch.dict(os.environ, {"ANTHROPIC_VERTEX_PROJECT_ID": "proj", "ANTHROPIC_API_KEY": "sk-test"}):
            assert _detect_backend() == "vertex"

    def test_returns_none_when_nothing_set(self) -> None:
        from sova.dashboard.services.llm_suggestion_service import _detect_backend

        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_VERTEX_PROJECT_ID", "ANTHROPIC_API_KEY")}
        with patch.dict(os.environ, env, clear=True):
            assert _detect_backend() is None

    def test_ignores_empty_project_id(self) -> None:
        from sova.dashboard.services.llm_suggestion_service import _detect_backend

        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        env["ANTHROPIC_VERTEX_PROJECT_ID"] = "  "
        with patch.dict(os.environ, env, clear=True):
            assert _detect_backend() is None


class TestIsEnabled:
    async def test_returns_true_by_default(self) -> None:
        from sova.dashboard.services.llm_suggestion_service import _is_enabled

        with patch("sova.dashboard.services.llm_suggestion_service.asyncio.to_thread") as mock_to_thread:
            mock_cfg = MagicMock()
            mock_cfg.dashboard.llm_suggestions = True
            mock_to_thread.return_value = mock_cfg
            assert await _is_enabled() is True

    async def test_returns_false_when_config_disables(self) -> None:
        from sova.dashboard.services.llm_suggestion_service import _is_enabled

        with patch("sova.dashboard.services.llm_suggestion_service.asyncio.to_thread") as mock_to_thread:
            mock_cfg = MagicMock()
            mock_cfg.dashboard.llm_suggestions = False
            mock_to_thread.return_value = mock_cfg
            assert await _is_enabled() is False

    async def test_defaults_to_true_on_config_error(self) -> None:
        from sova.dashboard.services.llm_suggestion_service import _is_enabled

        with patch(
            "sova.dashboard.services.llm_suggestion_service.asyncio.to_thread",
            side_effect=RuntimeError("config error"),
        ):
            assert await _is_enabled() is True
