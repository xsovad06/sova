"""Tests for the batch provider with Vertex AI and Anthropic direct backends."""

from __future__ import annotations

import asyncio
import json
import os
from decimal import Decimal
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from sova.llm.models import BatchRequest, BatchResult, BatchTimeoutError, LLMResult
from sova.llm.provider import LLMProvider
from sova.llm.providers.anthropic_batch import (
    _DEFAULT_MODEL,
    BatchProvider,
    create_batch_provider,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(custom_id: str = "req-1", prompt: str = "Hello", model: str = "") -> BatchRequest:
    return BatchRequest(custom_id=custom_id, prompt=prompt, model=model)


def _vertex_success_response(custom_id: str, text: str = "Result text") -> dict:
    return {
        "instance": {"custom_id": custom_id},
        "status": "",
        "response": {
            "content": [{"type": "text", "text": text}],
            "model": "claude-sonnet-4-6",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        },
    }


def _anthropic_success_result(custom_id: str, text: str = "Result text") -> dict:
    return {
        "custom_id": custom_id,
        "result": {
            "type": "succeeded",
            "message": {
                "content": [{"type": "text", "text": text}],
                "model": "claude-sonnet-4-6",
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        },
    }


def _anthropic_error_result(custom_id: str) -> dict:
    return {
        "custom_id": custom_id,
        "result": {
            "type": "errored",
            "error": {"type": "server_error", "message": "Internal error"},
        },
    }


# ---------------------------------------------------------------------------
# BatchProvider -- shared behavior
# ---------------------------------------------------------------------------


class TestBatchProviderShared:
    @pytest.mark.asyncio
    async def test_empty_batch_returns_empty(self) -> None:
        provider = BatchProvider("anthropic", api_key="test-key")
        results = await provider.invoke_batch([])
        assert results == []

    @pytest.mark.asyncio
    async def test_invoke_raises_not_implemented(self) -> None:
        provider = BatchProvider("anthropic", api_key="test-key")
        with pytest.raises(NotImplementedError, match="batch-only"):
            await provider.invoke("test")

    @pytest.mark.asyncio
    async def test_invoke_streaming_raises_not_implemented(self) -> None:
        provider = BatchProvider("anthropic", api_key="test-key")
        with pytest.raises(NotImplementedError, match="batch-only"):
            await provider.invoke_streaming("test")

    def test_parse_message_response(self) -> None:
        provider = BatchProvider("anthropic", api_key="test-key")
        message = {
            "content": [{"type": "text", "text": "Hello world"}],
            "model": "claude-sonnet-4-6",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
        result = provider._parse_message_response(message)
        assert result.text == "Hello world"
        assert result.model == "claude-sonnet-4-6"
        assert result.input_tokens == 100
        assert result.output_tokens == 50
        assert result.cost_usd == Decimal("0")
        assert result.stop_reason == "end_turn"

    def test_resolve_model_uses_first_request(self) -> None:
        provider = BatchProvider("anthropic", api_key="test-key")
        reqs = [
            _make_request("r1", model="claude-opus-4-6"),
            _make_request("r2", model="claude-sonnet-4-6"),
        ]
        assert provider._resolve_model(reqs) == "claude-opus-4-6"

    def test_resolve_model_defaults(self) -> None:
        provider = BatchProvider("anthropic", api_key="test-key")
        reqs = [_make_request("r1")]
        assert provider._resolve_model(reqs) == _DEFAULT_MODEL


# ---------------------------------------------------------------------------
# Anthropic direct backend
# ---------------------------------------------------------------------------


class TestAnthropicBackend:
    @pytest.mark.asyncio
    @respx.mock
    async def test_submit_poll_retrieve(self) -> None:
        provider = BatchProvider("anthropic", api_key="test-key")
        requests = [_make_request("r1", "Hello"), _make_request("r2", "World")]

        respx.post("https://api.anthropic.com/v1/messages/batches").mock(
            return_value=httpx.Response(200, json={"id": "batch-123"})
        )
        respx.get("https://api.anthropic.com/v1/messages/batches/batch-123").mock(
            return_value=httpx.Response(200, json={"processing_status": "ended"})
        )
        results_jsonl = "\n".join(
            [
                json.dumps(_anthropic_success_result("r1", "Answer 1")),
                json.dumps(_anthropic_success_result("r2", "Answer 2")),
            ]
        )
        respx.get("https://api.anthropic.com/v1/messages/batches/batch-123/results").mock(
            return_value=httpx.Response(200, text=results_jsonl)
        )

        results = await provider.invoke_batch(requests, poll_interval=0, timeout=10)
        assert len(results) == 2
        assert results[0].succeeded
        assert results[0].result.text == "Answer 1"
        assert results[1].succeeded
        assert results[1].result.text == "Answer 2"

    @pytest.mark.asyncio
    @respx.mock
    async def test_partial_failure(self) -> None:
        provider = BatchProvider("anthropic", api_key="test-key")
        requests = [_make_request("r1"), _make_request("r2"), _make_request("r3")]

        respx.post("https://api.anthropic.com/v1/messages/batches").mock(
            return_value=httpx.Response(200, json={"id": "batch-456"})
        )
        respx.get("https://api.anthropic.com/v1/messages/batches/batch-456").mock(
            return_value=httpx.Response(200, json={"processing_status": "ended"})
        )
        results_jsonl = "\n".join(
            [
                json.dumps(_anthropic_success_result("r1")),
                json.dumps(_anthropic_error_result("r2")),
                json.dumps(_anthropic_success_result("r3")),
            ]
        )
        respx.get("https://api.anthropic.com/v1/messages/batches/batch-456/results").mock(
            return_value=httpx.Response(200, text=results_jsonl)
        )

        results = await provider.invoke_batch(requests, poll_interval=0, timeout=10)
        assert len(results) == 3
        assert results[0].succeeded
        assert not results[1].succeeded
        assert "Internal error" in results[1].error
        assert results[2].succeeded

    @pytest.mark.asyncio
    @respx.mock
    async def test_result_reordering(self) -> None:
        provider = BatchProvider("anthropic", api_key="test-key")
        requests = [_make_request("a"), _make_request("b"), _make_request("c")]

        respx.post("https://api.anthropic.com/v1/messages/batches").mock(
            return_value=httpx.Response(200, json={"id": "batch-789"})
        )
        respx.get("https://api.anthropic.com/v1/messages/batches/batch-789").mock(
            return_value=httpx.Response(200, json={"processing_status": "ended"})
        )
        results_jsonl = "\n".join(
            [
                json.dumps(_anthropic_success_result("c", "Third")),
                json.dumps(_anthropic_success_result("a", "First")),
                json.dumps(_anthropic_success_result("b", "Second")),
            ]
        )
        respx.get("https://api.anthropic.com/v1/messages/batches/batch-789/results").mock(
            return_value=httpx.Response(200, text=results_jsonl)
        )

        results = await provider.invoke_batch(requests, poll_interval=0, timeout=10)
        assert results[0].result.text == "First"
        assert results[1].result.text == "Second"
        assert results[2].result.text == "Third"

    @pytest.mark.asyncio
    @respx.mock
    async def test_timeout(self) -> None:
        provider = BatchProvider("anthropic", api_key="test-key")

        respx.post("https://api.anthropic.com/v1/messages/batches").mock(
            return_value=httpx.Response(200, json={"id": "batch-slow"})
        )
        respx.get("https://api.anthropic.com/v1/messages/batches/batch-slow").mock(
            return_value=httpx.Response(200, json={"processing_status": "in_progress"})
        )
        cancel_route = respx.post("https://api.anthropic.com/v1/messages/batches/batch-slow/cancel").mock(
            return_value=httpx.Response(200, json={"id": "batch-slow", "processing_status": "canceling"})
        )

        with pytest.raises(BatchTimeoutError, match="timed out"):
            await provider.invoke_batch([_make_request("r1")], poll_interval=0, timeout=0)

        assert cancel_route.called

    @pytest.mark.asyncio
    @respx.mock
    async def test_429_retry_on_submit(self) -> None:
        provider = BatchProvider("anthropic", api_key="test-key")

        route = respx.post("https://api.anthropic.com/v1/messages/batches")
        route.side_effect = [
            httpx.Response(429, text="Rate limited"),
            httpx.Response(200, json={"id": "batch-ok"}),
        ]
        respx.get("https://api.anthropic.com/v1/messages/batches/batch-ok").mock(
            return_value=httpx.Response(200, json={"processing_status": "ended"})
        )
        respx.get("https://api.anthropic.com/v1/messages/batches/batch-ok/results").mock(
            return_value=httpx.Response(200, text=json.dumps(_anthropic_success_result("r1")))
        )

        results = await provider.invoke_batch([_make_request("r1")], poll_interval=0, timeout=10)
        assert len(results) == 1
        assert results[0].succeeded

    @pytest.mark.asyncio
    @respx.mock
    async def test_batch_canceled(self) -> None:
        provider = BatchProvider("anthropic", api_key="test-key")

        respx.post("https://api.anthropic.com/v1/messages/batches").mock(
            return_value=httpx.Response(200, json={"id": "batch-cancel"})
        )
        respx.get("https://api.anthropic.com/v1/messages/batches/batch-cancel").mock(
            return_value=httpx.Response(200, json={"processing_status": "canceled"})
        )

        with pytest.raises(BatchTimeoutError, match="canceled"):
            await provider.invoke_batch([_make_request("r1")], poll_interval=0, timeout=10)

    @pytest.mark.asyncio
    @respx.mock
    async def test_check_available_success(self) -> None:
        provider = BatchProvider("anthropic", api_key="test-key")
        respx.get("https://api.anthropic.com/v1/messages/batches").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        available, msg = await provider.check_available()
        assert available
        assert "available" in msg.lower()

    @pytest.mark.asyncio
    @respx.mock
    async def test_check_available_failure(self) -> None:
        provider = BatchProvider("anthropic", api_key="bad-key")
        respx.get("https://api.anthropic.com/v1/messages/batches").mock(
            return_value=httpx.Response(401, text="Unauthorized")
        )
        available, msg = await provider.check_available()
        assert not available


# ---------------------------------------------------------------------------
# Vertex AI backend
# ---------------------------------------------------------------------------


class TestVertexBackend:
    def _make_vertex_provider(self) -> BatchProvider:
        return BatchProvider(
            "vertex",
            project_id="test-project",
            region="us-east5",
            gcs_bucket="test-bucket",
            gcs_prefix="sova-batch",
        )

    @pytest.mark.asyncio
    @respx.mock
    async def test_submit_poll_retrieve(self) -> None:
        provider = self._make_vertex_provider()
        requests = [_make_request("r1", "Hello"), _make_request("r2", "World")]

        mock_creds = MagicMock()
        mock_creds.token = "fake-token"
        mock_creds.expired = False

        with patch("sova.llm.providers.anthropic_batch.BatchProvider._get_vertex_token", return_value="fake-token"):
            respx.post("https://storage.googleapis.com/upload/storage/v1/b/test-bucket/o").mock(
                return_value=httpx.Response(200, json={"name": "uploaded"})
            )

            respx.post(
                "https://us-east5-aiplatform.googleapis.com/v1/projects/test-project/"
                "locations/us-east5/batchPredictionJobs"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "name": "projects/test-project/locations/us-east5/batchPredictionJobs/123",
                    },
                )
            )

            respx.get(
                "https://us-east5-aiplatform.googleapis.com/v1/"
                "projects/test-project/locations/us-east5/batchPredictionJobs/123"
            ).mock(return_value=httpx.Response(200, json={"state": "JOB_STATE_SUCCEEDED"}))

            results_jsonl = "\n".join(
                [
                    json.dumps(_vertex_success_response("r1", "Answer 1")),
                    json.dumps(_vertex_success_response("r2", "Answer 2")),
                ]
            )
            respx.get("https://storage.googleapis.com/storage/v1/b/test-bucket/o").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "items": [{"name": "sova-batch/output-abc/results.jsonl"}],
                    },
                )
            )
            respx.get(
                "https://storage.googleapis.com/storage/v1/b/test-bucket/o/sova-batch/output-abc/results.jsonl"
            ).mock(return_value=httpx.Response(200, text=results_jsonl))

            respx.delete(url__regex=r".*storage.*test-bucket.*").mock(return_value=httpx.Response(204))

            results = await provider.invoke_batch(requests, poll_interval=0, timeout=10)

        assert len(results) == 2
        assert results[0].succeeded
        assert results[0].result.text == "Answer 1"
        assert results[1].succeeded
        assert results[1].result.text == "Answer 2"

    @pytest.mark.asyncio
    @respx.mock
    async def test_vertex_job_failed(self) -> None:
        provider = self._make_vertex_provider()

        with patch("sova.llm.providers.anthropic_batch.BatchProvider._get_vertex_token", return_value="fake-token"):
            respx.post("https://storage.googleapis.com/upload/storage/v1/b/test-bucket/o").mock(
                return_value=httpx.Response(200, json={"name": "uploaded"})
            )
            respx.post(url__regex=r".*batchPredictionJobs$").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "name": "projects/test-project/locations/us-east5/batchPredictionJobs/456",
                    },
                )
            )
            respx.get(url__regex=r".*batchPredictionJobs/456$").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "state": "JOB_STATE_FAILED",
                        "error": {"message": "Quota exceeded"},
                    },
                )
            )

            with pytest.raises(BatchTimeoutError, match="JOB_STATE_FAILED"):
                await provider.invoke_batch([_make_request("r1")], poll_interval=0, timeout=10)

    @pytest.mark.asyncio
    @respx.mock
    async def test_vertex_timeout(self) -> None:
        provider = self._make_vertex_provider()

        with patch("sova.llm.providers.anthropic_batch.BatchProvider._get_vertex_token", return_value="fake-token"):
            respx.post("https://storage.googleapis.com/upload/storage/v1/b/test-bucket/o").mock(
                return_value=httpx.Response(200, json={"name": "uploaded"})
            )
            respx.post(url__regex=r".*batchPredictionJobs$").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "name": "projects/test-project/locations/us-east5/batchPredictionJobs/789",
                    },
                )
            )
            respx.get(url__regex=r".*batchPredictionJobs/789$").mock(
                return_value=httpx.Response(200, json={"state": "JOB_STATE_RUNNING"})
            )

            with pytest.raises(BatchTimeoutError, match="timed out"):
                await provider.invoke_batch([_make_request("r1")], poll_interval=0, timeout=0)

    @pytest.mark.asyncio
    @respx.mock
    async def test_vertex_partial_failure(self) -> None:
        provider = self._make_vertex_provider()
        requests = [_make_request("r1"), _make_request("r2")]

        with patch("sova.llm.providers.anthropic_batch.BatchProvider._get_vertex_token", return_value="fake-token"):
            respx.post("https://storage.googleapis.com/upload/storage/v1/b/test-bucket/o").mock(
                return_value=httpx.Response(200, json={"name": "uploaded"})
            )
            respx.post(url__regex=r".*batchPredictionJobs$").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "name": "projects/test-project/locations/us-east5/batchPredictionJobs/111",
                    },
                )
            )
            respx.get(url__regex=r".*batchPredictionJobs/111$").mock(
                return_value=httpx.Response(200, json={"state": "JOB_STATE_SUCCEEDED"})
            )

            results_jsonl = "\n".join(
                [
                    json.dumps(_vertex_success_response("r1", "OK")),
                    json.dumps(
                        {
                            "instance": {"custom_id": "r2"},
                            "status": {"code": 500, "message": "Internal error"},
                            "response": {},
                        }
                    ),
                ]
            )
            respx.get("https://storage.googleapis.com/storage/v1/b/test-bucket/o").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "items": [{"name": "sova-batch/output-xyz/results.jsonl"}],
                    },
                )
            )
            respx.get(
                "https://storage.googleapis.com/storage/v1/b/test-bucket/o/sova-batch/output-xyz/results.jsonl"
            ).mock(return_value=httpx.Response(200, text=results_jsonl))

            respx.delete(url__regex=r".*storage.*test-bucket.*").mock(return_value=httpx.Response(204))

            results = await provider.invoke_batch(requests, poll_interval=0, timeout=10)

        assert len(results) == 2
        assert results[0].succeeded
        assert results[0].result.text == "OK"
        assert not results[1].succeeded
        assert "failed" in results[1].error.lower()

    def test_build_vertex_jsonl(self) -> None:
        provider = self._make_vertex_provider()
        requests = [
            BatchRequest(custom_id="r1", prompt="Hello", model="claude-sonnet-4-6", system="Be helpful"),
            BatchRequest(custom_id="r2", prompt="World"),
        ]
        lines = provider._build_vertex_jsonl(requests)
        assert len(lines) == 2

        parsed_1 = json.loads(lines[0])
        assert parsed_1["custom_id"] == "r1"
        assert parsed_1["request"]["anthropic_version"] == "vertex-2023-10-16"
        assert parsed_1["request"]["messages"] == [{"role": "user", "content": "Hello"}]
        assert parsed_1["request"]["system"] == "Be helpful"

        parsed_2 = json.loads(lines[1])
        assert "system" not in parsed_2["request"]

    @pytest.mark.asyncio
    async def test_gcs_upload_failure(self) -> None:
        provider = self._make_vertex_provider()

        with patch("sova.llm.providers.anthropic_batch.BatchProvider._get_vertex_token", return_value="fake-token"):
            with respx.mock:
                respx.post("https://storage.googleapis.com/upload/storage/v1/b/test-bucket/o").mock(
                    return_value=httpx.Response(403, text="Forbidden")
                )
                with pytest.raises(RuntimeError, match="GCS upload failed"):
                    await provider.invoke_batch([_make_request("r1")], poll_interval=0, timeout=10)


# ---------------------------------------------------------------------------
# create_batch_provider auto-detection
# ---------------------------------------------------------------------------


class TestCreateBatchProvider:
    def test_vertex_detected(self) -> None:
        mock_google_auth = MagicMock()
        env = {"ANTHROPIC_VERTEX_PROJECT_ID": "proj", "CLOUD_ML_REGION": "us-east5"}
        with (
            patch.dict(os.environ, env, clear=False),
            patch.dict("sys.modules", {"google": MagicMock(), "google.auth": mock_google_auth}),
        ):
            provider = create_batch_provider(gcs_bucket="my-bucket")
            assert provider is not None
            assert provider._backend == "vertex"

    def test_anthropic_detected(self) -> None:
        env_clean = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_VERTEX_PROJECT_ID"}
        env_clean["ANTHROPIC_API_KEY"] = "sk-test"
        with patch.dict(os.environ, env_clean, clear=True):
            provider = create_batch_provider(gcs_bucket="")
            assert provider is not None
            assert provider._backend == "anthropic"

    def test_none_when_no_credentials(self) -> None:
        env_clean = {
            k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_VERTEX_PROJECT_ID", "ANTHROPIC_API_KEY")
        }
        with patch.dict(os.environ, env_clean, clear=True):
            provider = create_batch_provider(gcs_bucket="")
            assert provider is None

    def test_vertex_needs_gcs_bucket(self) -> None:
        env_clean = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        env_clean["ANTHROPIC_VERTEX_PROJECT_ID"] = "proj"
        with patch.dict(os.environ, env_clean, clear=True):
            provider = create_batch_provider(gcs_bucket="")
            assert provider is None


# ---------------------------------------------------------------------------
# LLMProvider ABC sequential fallback
# ---------------------------------------------------------------------------


class TestABCFallback:
    @pytest.mark.asyncio
    async def test_sequential_fallback(self) -> None:
        """Default invoke_batch() on LLMProvider calls invoke() N times."""

        class MinimalProvider(LLMProvider):
            async def invoke(self, prompt, **kwargs):
                return LLMResult(text=f"echo:{prompt}", model="test")

            async def invoke_streaming(self, prompt, **kwargs):
                yield  # pragma: no cover

            async def check_available(self):
                return True, "ok"

        provider = MinimalProvider()
        requests = [_make_request("a", "p1"), _make_request("b", "p2")]
        results = await provider.invoke_batch(requests)

        assert len(results) == 2
        assert results[0].succeeded
        assert results[0].result.text == "echo:p1"
        assert results[1].succeeded
        assert results[1].result.text == "echo:p2"

    @pytest.mark.asyncio
    async def test_sequential_fallback_with_errors(self) -> None:
        """Sequential fallback captures per-item errors."""

        class FailingProvider(LLMProvider):
            async def invoke(self, prompt, **kwargs):
                if "fail" in prompt:
                    raise RuntimeError("deliberate failure")
                return LLMResult(text="ok", model="test")

            async def invoke_streaming(self, prompt, **kwargs):
                yield  # pragma: no cover

            async def check_available(self):
                return True, "ok"

        provider = FailingProvider()
        requests = [_make_request("a", "good"), _make_request("b", "fail")]
        results = await provider.invoke_batch(requests)

        assert results[0].succeeded
        assert not results[1].succeeded
        assert "deliberate failure" in results[1].error

    @pytest.mark.asyncio
    async def test_sequential_fallback_timeout(self) -> None:
        """Sequential fallback respects deadline-based timeout."""

        class SlowProvider(LLMProvider):
            async def invoke(self, prompt, **kwargs):
                await asyncio.sleep(0.1)
                return LLMResult(text="ok", model="test")

            async def invoke_streaming(self, prompt, **kwargs):
                yield  # pragma: no cover

            async def check_available(self):
                return True, "ok"

        provider = SlowProvider()
        requests = [_make_request(f"r{i}") for i in range(5)]
        results = await provider.invoke_batch(requests, timeout=0)

        exhausted = [r for r in results if r.error and "exhausted" in r.error]
        assert len(exhausted) >= 1


class TestCancelAndCleanup:
    @pytest.mark.asyncio
    @respx.mock
    async def test_cancel_anthropic_batch(self) -> None:
        provider = BatchProvider("anthropic", api_key="test-key")
        cancel_route = respx.post("https://api.anthropic.com/v1/messages/batches/batch-123/cancel").mock(
            return_value=httpx.Response(200, json={"id": "batch-123"})
        )

        async with httpx.AsyncClient() as client:
            await provider._cancel_anthropic_batch(client, provider._anthropic_headers(), "batch-123")

        assert cancel_route.called

    @pytest.mark.asyncio
    @respx.mock
    async def test_cancel_anthropic_batch_failure(self) -> None:
        provider = BatchProvider("anthropic", api_key="test-key")
        respx.post("https://api.anthropic.com/v1/messages/batches/batch-fail/cancel").mock(
            side_effect=httpx.ConnectError("network down")
        )

        async with httpx.AsyncClient() as client:
            await provider._cancel_anthropic_batch(client, provider._anthropic_headers(), "batch-fail")

    @pytest.mark.asyncio
    async def test_gcs_cleanup_prefix(self) -> None:
        provider = BatchProvider("vertex", project_id="p", region="us-east5", gcs_bucket="test-bucket")
        with (
            patch.object(provider, "_get_vertex_token", return_value="tok"),
            respx.mock,
        ):
            respx.get("https://storage.googleapis.com/storage/v1/b/test-bucket/o").mock(
                return_value=httpx.Response(200, json={"items": [{"name": "out/result.jsonl"}]})
            )
            delete_input = respx.delete(url__regex=r".*/input-abc.*").mock(return_value=httpx.Response(204))
            delete_output = respx.delete(url__regex=r".*/out%2Fresult\.jsonl.*").mock(return_value=httpx.Response(204))

            async with httpx.AsyncClient() as client:
                await provider._gcs_cleanup_prefix(client, "input-abc", "out/")

            assert delete_input.called
            assert delete_output.called

    @pytest.mark.asyncio
    async def test_gcs_cleanup_prefix_token_failure(self) -> None:
        provider = BatchProvider("vertex", project_id="p", region="us-east5", gcs_bucket="test-bucket")
        with patch.object(provider, "_get_vertex_token", side_effect=RuntimeError("no creds")):
            async with httpx.AsyncClient() as client:
                await provider._gcs_cleanup_prefix(client, "input", "out/")

    @pytest.mark.asyncio
    async def test_gcs_delete_rejected(self) -> None:
        provider = BatchProvider("vertex", project_id="p", region="us-east5", gcs_bucket="test-bucket")
        with respx.mock:
            respx.delete(url__regex=r".*test-bucket.*").mock(return_value=httpx.Response(403, text="Forbidden"))
            async with httpx.AsyncClient() as client:
                await provider._gcs_delete(client, "tok", "some-file")

    def test_vertex_error_guard_non_dict(self) -> None:
        provider = BatchProvider("vertex", project_id="p", region="us-east5", gcs_bucket="b")
        requests = [_make_request("r1")]
        raw = [{"instance": {"custom_id": "r1"}, "response": {}, "status": "FAILED"}]
        results = provider._parse_vertex_results(requests, raw)
        assert not results[0].succeeded
        assert "FAILED" in results[0].error

    def test_vertex_empty_response(self) -> None:
        provider = BatchProvider("vertex", project_id="p", region="us-east5", gcs_bucket="b")
        requests = [_make_request("r1")]
        raw = [{"instance": {"custom_id": "r1"}, "response": {}, "status": ""}]
        results = provider._parse_vertex_results(requests, raw)
        assert not results[0].succeeded
        assert "empty response" in results[0].error

    def test_anthropic_error_guard_non_dict(self) -> None:
        provider = BatchProvider("anthropic", api_key="k")
        requests = [_make_request("r1")]
        raw = [{"custom_id": "r1", "result": {"type": "errored", "error": "plain string error"}}]
        results = provider._parse_anthropic_results(requests, raw)
        assert not results[0].succeeded
        assert "plain string error" in results[0].error

    def test_resolve_model_warns_mixed(self) -> None:
        provider = BatchProvider("anthropic", api_key="k")
        reqs = [
            BatchRequest(custom_id="a", prompt="p", model="model-a"),
            BatchRequest(custom_id="b", prompt="p", model="model-b"),
        ]
        model = provider._resolve_model(reqs)
        assert model == "model-a"

    def test_message_params_with_system(self) -> None:
        provider = BatchProvider("anthropic", api_key="k")
        req = BatchRequest(custom_id="r1", prompt="hi", model="m1", system="be nice")
        params = provider._message_params(req)
        assert params["system"] == "be nice"
        assert params["model"] == "m1"
        assert params["messages"] == [{"role": "user", "content": "hi"}]

    def test_message_params_without_system(self) -> None:
        provider = BatchProvider("anthropic", api_key="k")
        req = BatchRequest(custom_id="r1", prompt="hi")
        params = provider._message_params(req)
        assert "system" not in params

    def test_anthropic_headers_has_beta(self) -> None:
        provider = BatchProvider("anthropic", api_key="test-key")
        headers = provider._anthropic_headers()
        assert headers["anthropic-beta"] == "message-batches-2024-09-24"


# ---------------------------------------------------------------------------
# Client layer invoke_batch
# ---------------------------------------------------------------------------


class TestClientInvokeBatch:
    @pytest.mark.asyncio
    async def test_invoke_batch_empty_returns_empty(self) -> None:
        from sova.llm.client import invoke_batch

        results = await invoke_batch([])
        assert results == []

    @pytest.mark.asyncio
    async def test_invoke_batch_no_backend_uses_global(self) -> None:
        from unittest.mock import AsyncMock

        from sova.llm import client

        mock_provider = MagicMock()
        mock_provider.invoke_batch = AsyncMock(
            return_value=[
                BatchResult(
                    request=_make_request("r1"),
                    result=LLMResult(text="ok", model="test"),
                )
            ]
        )

        with (
            patch.object(client, "get_provider", return_value=mock_provider),
            patch(
                "sova.llm.providers.anthropic_batch.create_batch_provider",
                return_value=None,
            ),
        ):
            results = await client.invoke_batch([_make_request("r1")])

        assert len(results) == 1
        assert results[0].succeeded
        mock_provider.invoke_batch.assert_called_once()

    @pytest.mark.asyncio
    async def test_invoke_batch_with_batch_provider(self) -> None:
        from unittest.mock import AsyncMock

        from sova.llm import client

        mock_batch = MagicMock()
        mock_batch.invoke_batch = AsyncMock(
            return_value=[
                BatchResult(
                    request=_make_request("r1"),
                    result=LLMResult(text="batch-ok", model="test"),
                )
            ]
        )

        with (
            patch.object(client, "get_provider"),
            patch(
                "sova.llm.providers.anthropic_batch.create_batch_provider",
                return_value=mock_batch,
            ),
        ):
            results = await client.invoke_batch([_make_request("r1")], gcs_bucket="bucket")

        assert len(results) == 1
        assert results[0].result.text == "batch-ok"
        mock_batch.invoke_batch.assert_called_once()
