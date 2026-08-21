"""Batch provider with Vertex AI and Anthropic direct backends.

Submits batches of LLM requests for async processing at 50% cost discount.
Auto-detects backend: Vertex AI (GCS + batchPredictionJobs) when GCP env vars
and a GCS bucket are configured, Anthropic direct (Message Batches API) when
ANTHROPIC_API_KEY is set.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

import httpx

from sova.llm.models import (
    BatchRequest,
    BatchResult,
    BatchTimeoutError,
    LLMResult,
    StreamEvent,
)
from sova.llm.provider import LLMProvider
from sova.utils.logging import get_logger

log = get_logger(component="llm.batch")

_DEFAULT_MODEL = "claude-sonnet-4-6"
_ANTHROPIC_API_URL = "https://api.anthropic.com/v1"
_ANTHROPIC_VERSION = "2023-06-01"
_VERTEX_ANTHROPIC_VERSION = "vertex-2023-10-16"
_MAX_SUBMIT_RETRIES = 3
_MAX_POLL_FAILURES = 3

_warned_no_backend = False


class BatchProvider(LLMProvider):
    """Batch-only LLM provider. invoke() and invoke_streaming() are not supported."""

    def __init__(
        self,
        backend: str,
        *,
        api_key: str = "",
        project_id: str = "",
        region: str = "",
        gcs_bucket: str = "",
        gcs_prefix: str = "sova-batch",
    ) -> None:
        self._backend = backend
        self._api_key = api_key
        self._project_id = project_id
        self._region = region
        self._gcs_bucket = gcs_bucket
        self._gcs_prefix = gcs_prefix
        self._credentials: object | None = None

    async def invoke(
        self,
        prompt: str,
        *,
        model: str | None = None,
        cwd: Path | str | None = None,
        max_budget_usd: Decimal | None = None,
        timeout: float | None = None,
    ) -> LLMResult:
        raise NotImplementedError("BatchProvider is batch-only; use invoke_batch()")

    async def invoke_streaming(  # type: ignore[override]
        self,
        prompt: str,
        *,
        model: str | None = None,
        cwd: Path | str | None = None,
        max_budget_usd: Decimal | None = None,
    ) -> AsyncIterator[StreamEvent]:
        raise NotImplementedError("BatchProvider is batch-only; use invoke_batch()")

    async def check_available(self) -> tuple[bool, str]:
        if self._backend == "vertex":
            return await self._check_vertex_available()
        return await self._check_anthropic_available()

    async def invoke_batch(
        self,
        requests: list[BatchRequest],
        *,
        poll_interval: int = 60,
        timeout: int = 86400,
    ) -> list[BatchResult]:
        if not requests:
            return []

        if self._backend == "vertex":
            return await self._submit_vertex(requests, poll_interval=poll_interval, timeout=timeout)
        return await self._submit_anthropic(requests, poll_interval=poll_interval, timeout=timeout)

    # -- Vertex AI backend --

    async def _submit_vertex(
        self,
        requests: list[BatchRequest],
        *,
        poll_interval: int,
        timeout: int,
    ) -> list[BatchResult]:
        batch_id = uuid.uuid4().hex[:12]
        input_name = f"{self._gcs_prefix}/input-{batch_id}.jsonl"
        output_prefix = f"{self._gcs_prefix}/output-{batch_id}"

        token = await self._get_vertex_token()
        jsonl_lines = self._build_vertex_jsonl(requests)
        model = self._resolve_model(requests)

        async with httpx.AsyncClient(timeout=30.0) as client:
            await self._gcs_upload(client, token, input_name, "\n".join(jsonl_lines))

            try:
                job_name = await self._create_batch_job(
                    client,
                    token,
                    model,
                    input_name,
                    output_prefix,
                    batch_id,
                )

                await self._poll_vertex_job(client, job_name, poll_interval, timeout)

                token = await self._get_vertex_token()
                raw_results = await self._gcs_download_results(client, token, output_prefix)
            finally:
                await self._gcs_cleanup_prefix(client, input_name, output_prefix)

        return self._parse_vertex_results(requests, raw_results)

    def _build_vertex_jsonl(self, requests: list[BatchRequest]) -> list[str]:
        lines = []
        for req in requests:
            body: dict = {
                "custom_id": req.custom_id,
                "request": {"anthropic_version": _VERTEX_ANTHROPIC_VERSION, **self._message_params(req)},
            }
            lines.append(json.dumps(body, separators=(",", ":")))
        return lines

    async def _get_vertex_token(self) -> str:
        try:
            import google.auth
            import google.auth.transport.requests
        except ImportError:
            raise ImportError(
                "google-auth is required for the Vertex AI batch backend. Install it with: pip install google-auth"
            ) from None

        if self._credentials is None:
            self._credentials, _ = await asyncio.to_thread(google.auth.default)

        creds = self._credentials
        if not getattr(creds, "token", "") or getattr(creds, "expired", False):
            await asyncio.to_thread(creds.refresh, google.auth.transport.requests.Request())

        return str(creds.token)

    async def _gcs_upload(self, client: httpx.AsyncClient, token: str, name: str, content: str) -> None:
        url = f"https://storage.googleapis.com/upload/storage/v1/b/{self._gcs_bucket}/o"
        resp = await client.post(
            url,
            params={"uploadType": "media", "name": name},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/jsonl"},
            content=content.encode(),
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"GCS upload failed ({resp.status_code}): {resp.text[:200]}. "
                f"Bucket: {self._gcs_bucket}, required permissions: "
                "storage.objects.create, storage.objects.get, storage.objects.list, storage.objects.delete"
            )

    async def _create_batch_job(
        self,
        client: httpx.AsyncClient,
        token: str,
        model: str,
        input_name: str,
        output_prefix: str,
        batch_id: str,
    ) -> str:
        url = (
            f"https://{self._region}-aiplatform.googleapis.com/v1/"
            f"projects/{self._project_id}/locations/{self._region}/batchPredictionJobs"
        )
        body = {
            "displayName": f"sova-batch-{batch_id}",
            "model": f"publishers/anthropic/models/{model}",
            "inputConfig": {
                "instancesFormat": "jsonl",
                "gcsSource": {"uris": [f"gs://{self._gcs_bucket}/{input_name}"]},
            },
            "outputConfig": {
                "predictionsFormat": "jsonl",
                "gcsDestination": {"outputUriPrefix": f"gs://{self._gcs_bucket}/{output_prefix}/"},
            },
        }

        resp = await self._post_with_retry(
            client,
            url,
            body,
            headers={"Authorization": f"Bearer {token}"},
        )
        data = resp.json()
        job_name = data.get("name", "")
        if not job_name:
            raise RuntimeError(f"Vertex AI batch job creation returned no job name: {str(data)[:200]}")
        log.info("batch.vertex.job_created", job_name=job_name, batch_id=batch_id)
        return job_name

    async def _poll_vertex_job(
        self,
        client: httpx.AsyncClient,
        job_name: str,
        poll_interval: int,
        timeout: int,
    ) -> None:
        url = f"https://{self._region}-aiplatform.googleapis.com/v1/{job_name}"
        elapsed = 0
        consecutive_failures = 0
        current_interval = poll_interval

        while elapsed < timeout:
            await asyncio.sleep(current_interval)
            elapsed += max(current_interval, 1)

            try:
                token = await self._get_vertex_token()
                resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
                resp.raise_for_status()
                consecutive_failures = 0
                current_interval = poll_interval
            except httpx.HTTPStatusError as exc:
                consecutive_failures += 1
                if exc.response.status_code == 429:
                    current_interval = min(current_interval * 2, 300)
                    log.warning("batch.vertex.poll_429", interval=current_interval)
                    continue
                if consecutive_failures >= _MAX_POLL_FAILURES:
                    raise BatchTimeoutError(f"Polling failed {_MAX_POLL_FAILURES} times: {exc}") from exc
                continue

            state = resp.json().get("state", "")
            log.debug("batch.vertex.poll", state=state, elapsed=elapsed)

            if state == "JOB_STATE_SUCCEEDED":
                return
            if state in ("JOB_STATE_FAILED", "JOB_STATE_CANCELLED"):
                err = resp.json().get("error", {})
                error_msg = err.get("message", state) if isinstance(err, dict) else str(err or state)
                raise BatchTimeoutError(f"Vertex AI batch job {state}: {error_msg}")

        raise BatchTimeoutError(f"Vertex AI batch job timed out after {timeout}s")

    async def _gcs_download_results(
        self,
        client: httpx.AsyncClient,
        token: str,
        output_prefix: str,
    ) -> list[dict]:
        list_url = f"https://storage.googleapis.com/storage/v1/b/{self._gcs_bucket}/o"
        resp = await client.get(
            list_url,
            params={"prefix": output_prefix},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])

        results: list[dict] = []
        for item in items:
            name = item.get("name", "")
            if not name.endswith(".jsonl"):
                continue
            dl_url = f"https://storage.googleapis.com/storage/v1/b/{self._gcs_bucket}/o/{quote(name, safe='')}"
            dl_resp = await client.get(
                dl_url,
                params={"alt": "media"},
                headers={"Authorization": f"Bearer {token}"},
            )
            dl_resp.raise_for_status()
            for line in dl_resp.text.strip().split("\n"):
                line = line.strip()
                if line:
                    try:
                        results.append(json.loads(line))
                    except json.JSONDecodeError:
                        log.warning("batch.vertex.jsonl_parse_error", line_length=len(line))
        return results

    async def _gcs_delete(self, client: httpx.AsyncClient, token: str, name: str) -> None:
        url = f"https://storage.googleapis.com/storage/v1/b/{self._gcs_bucket}/o/{quote(name, safe='')}"
        try:
            resp = await client.delete(url, headers={"Authorization": f"Bearer {token}"})
            if resp.status_code < 400:
                log.debug("batch.gcs.cleanup", name=name)
            else:
                log.warning("batch.gcs.cleanup_rejected", name=name, status=resp.status_code)
        except Exception:
            log.warning("batch.gcs.cleanup_failed", name=name, exc_info=True)

    async def _gcs_cleanup_prefix(
        self,
        client: httpx.AsyncClient,
        input_name: str,
        output_prefix: str,
    ) -> None:
        try:
            token = await self._get_vertex_token()
        except Exception:
            log.warning("batch.gcs.cleanup_token_failed", exc_info=True)
            return

        names = [input_name]
        try:
            resp = await client.get(
                f"https://storage.googleapis.com/storage/v1/b/{self._gcs_bucket}/o",
                params={"prefix": output_prefix},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            names.extend(item["name"] for item in resp.json().get("items", []) if item.get("name"))
        except Exception:
            log.warning("batch.gcs.cleanup_list_failed", prefix=output_prefix, exc_info=True)

        for name in names:
            await self._gcs_delete(client, token, name)

    def _parse_vertex_results(
        self,
        requests: list[BatchRequest],
        raw_results: list[dict],
    ) -> list[BatchResult]:
        index_map = {req.custom_id: i for i, req in enumerate(requests)}
        results: list[BatchResult | None] = [None] * len(requests)

        for entry in raw_results:
            response = entry.get("response", {})
            status = entry.get("status", "")

            custom_id = self._extract_custom_id_vertex(entry)
            idx = index_map.get(custom_id)
            if idx is None:
                log.warning("batch.vertex.unknown_custom_id", custom_id=custom_id)
                continue

            if status:
                error_detail = status if isinstance(status, str) else json.dumps(status)
                results[idx] = BatchResult(
                    request=requests[idx],
                    error=f"Vertex batch item failed: {error_detail}",
                )
                continue

            if not response:
                results[idx] = BatchResult(
                    request=requests[idx],
                    error="Vertex batch item returned an empty response",
                )
                continue

            try:
                llm_result = self._parse_message_response(response)
                results[idx] = BatchResult(request=requests[idx], result=llm_result)
            except Exception as exc:
                results[idx] = BatchResult(request=requests[idx], error=f"Parse error: {exc}")

        for i, r in enumerate(results):
            if r is None:
                results[i] = BatchResult(request=requests[i], error="No result returned from batch")

        return results  # type: ignore[return-value]

    def _extract_custom_id_vertex(self, entry: dict) -> str:
        instance = entry.get("instance", {})
        if isinstance(instance, dict):
            return instance.get("custom_id", "")
        return entry.get("custom_id", "")

    async def _check_vertex_available(self) -> tuple[bool, str]:
        try:
            token = await self._get_vertex_token()
            async with httpx.AsyncClient(timeout=10.0) as client:
                url = (
                    f"https://{self._region}-aiplatform.googleapis.com/v1/"
                    f"projects/{self._project_id}/locations/{self._region}/batchPredictionJobs"
                    "?pageSize=1"
                )
                resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
                if resp.status_code < 400:
                    return True, f"Vertex AI batch available (project={self._project_id}, region={self._region})"
                return False, f"Vertex AI returned {resp.status_code}: {resp.text[:200]}"
        except Exception as exc:
            return False, f"Vertex AI unavailable: {exc}"

    # -- Anthropic direct backend --

    async def _submit_anthropic(
        self,
        requests: list[BatchRequest],
        *,
        poll_interval: int,
        timeout: int,
    ) -> list[BatchResult]:
        api_requests = self._build_anthropic_requests(requests)

        async with httpx.AsyncClient(timeout=30.0) as client:
            headers = self._anthropic_headers()

            resp = await self._post_with_retry(
                client,
                f"{_ANTHROPIC_API_URL}/messages/batches",
                {"requests": api_requests},
                headers=headers,
            )
            batch_data = resp.json()
            batch_id = batch_data["id"]
            log.info("batch.anthropic.submitted", batch_id=batch_id, count=len(requests))

            try:
                await self._poll_anthropic_batch(client, headers, batch_id, poll_interval, timeout)
            except BatchTimeoutError:
                await self._cancel_anthropic_batch(client, headers, batch_id)
                raise

            raw_results = await self._retrieve_anthropic_results(client, headers, batch_id)

        return self._parse_anthropic_results(requests, raw_results)

    def _message_params(self, req: BatchRequest) -> dict:
        params: dict = {
            "model": req.model or _DEFAULT_MODEL,
            "max_tokens": req.max_tokens,
            "messages": [{"role": "user", "content": req.prompt}],
        }
        if req.system:
            params["system"] = req.system
        return params

    def _build_anthropic_requests(self, requests: list[BatchRequest]) -> list[dict]:
        return [{"custom_id": req.custom_id, "params": self._message_params(req)} for req in requests]

    def _anthropic_headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "anthropic-beta": "message-batches-2024-09-24",
            "content-type": "application/json",
        }

    async def _poll_anthropic_batch(
        self,
        client: httpx.AsyncClient,
        headers: dict,
        batch_id: str,
        poll_interval: int,
        timeout: int,
    ) -> None:
        url = f"{_ANTHROPIC_API_URL}/messages/batches/{batch_id}"
        elapsed = 0
        consecutive_failures = 0
        current_interval = poll_interval

        while elapsed < timeout:
            await asyncio.sleep(current_interval)
            elapsed += max(current_interval, 1)

            try:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                consecutive_failures = 0
                current_interval = poll_interval
            except httpx.HTTPStatusError as exc:
                consecutive_failures += 1
                if exc.response.status_code == 429:
                    current_interval = min(current_interval * 2, 300)
                    log.warning("batch.anthropic.poll_429", interval=current_interval)
                    continue
                if consecutive_failures >= _MAX_POLL_FAILURES:
                    raise BatchTimeoutError(f"Polling failed {_MAX_POLL_FAILURES} times: {exc}") from exc
                continue

            data = resp.json()
            status = data.get("processing_status", "")
            log.debug("batch.anthropic.poll", status=status, elapsed=elapsed)

            if status == "ended":
                return
            if status in ("canceled", "canceling"):
                raise BatchTimeoutError("Anthropic batch was canceled")

        raise BatchTimeoutError(f"Anthropic batch timed out after {timeout}s")

    async def _retrieve_anthropic_results(
        self,
        client: httpx.AsyncClient,
        headers: dict,
        batch_id: str,
    ) -> list[dict]:
        url = f"{_ANTHROPIC_API_URL}/messages/batches/{batch_id}/results"
        resp = await client.get(url, headers=headers, follow_redirects=True, timeout=300.0)
        resp.raise_for_status()

        results = []
        for line in resp.text.strip().split("\n"):
            line = line.strip()
            if line:
                try:
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    log.warning("batch.anthropic.jsonl_parse_error", line_length=len(line))
        return results

    def _parse_anthropic_results(
        self,
        requests: list[BatchRequest],
        raw_results: list[dict],
    ) -> list[BatchResult]:
        index_map = {req.custom_id: i for i, req in enumerate(requests)}
        results: list[BatchResult | None] = [None] * len(requests)

        for entry in raw_results:
            custom_id = entry.get("custom_id", "")
            idx = index_map.get(custom_id)
            if idx is None:
                log.warning("batch.anthropic.unknown_custom_id", custom_id=custom_id)
                continue

            result_data = entry.get("result", {})
            result_type = result_data.get("type", "")

            if result_type == "succeeded":
                try:
                    message = result_data.get("message", {})
                    llm_result = self._parse_message_response(message)
                    results[idx] = BatchResult(request=requests[idx], result=llm_result)
                except Exception as exc:
                    results[idx] = BatchResult(request=requests[idx], error=f"Parse error: {exc}")
            else:
                err = result_data.get("error")
                error_detail = err.get("message", result_type) if isinstance(err, dict) else str(err or result_type)
                results[idx] = BatchResult(
                    request=requests[idx],
                    error=f"Batch item {result_type}: {error_detail}",
                )

        for i, r in enumerate(results):
            if r is None:
                results[i] = BatchResult(request=requests[i], error="No result returned from batch")

        return results  # type: ignore[return-value]

    async def _cancel_anthropic_batch(
        self,
        client: httpx.AsyncClient,
        headers: dict,
        batch_id: str,
    ) -> None:
        try:
            resp = await client.post(
                f"{_ANTHROPIC_API_URL}/messages/batches/{batch_id}/cancel",
                headers=headers,
            )
            log.info("batch.anthropic.cancel_requested", batch_id=batch_id, status=resp.status_code)
        except Exception:
            log.warning("batch.anthropic.cancel_failed", batch_id=batch_id, exc_info=True)

    async def _check_anthropic_available(self) -> tuple[bool, str]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{_ANTHROPIC_API_URL}/messages/batches?limit=1",
                    headers=self._anthropic_headers(),
                )
                if resp.status_code < 400:
                    return True, "Anthropic Batch API available"
                return False, f"Anthropic API returned {resp.status_code}: {resp.text[:200]}"
        except Exception as exc:
            return False, f"Anthropic API unavailable: {exc}"

    # -- Shared helpers --

    def _parse_message_response(self, message: dict) -> LLMResult:
        content_blocks = message.get("content", [])
        text = ""
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                text += block.get("text", "")

        usage = message.get("usage", {})
        return LLMResult(
            text=text,
            model=message.get("model", ""),
            cost_usd=Decimal("0"),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            stop_reason=message.get("stop_reason", ""),
        )

    def _resolve_model(self, requests: list[BatchRequest]) -> str:
        models = {req.model for req in requests if req.model}
        if len(models) > 1:
            log.warning("batch.vertex.mixed_models", models=sorted(models))
        for req in requests:
            if req.model:
                return req.model
        return _DEFAULT_MODEL

    async def _post_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        body: dict,
        headers: dict,
    ) -> httpx.Response:
        delay = 1.0
        for attempt in range(_MAX_SUBMIT_RETRIES):
            resp = await client.post(url, json=body, headers={**headers, "content-type": "application/json"})
            if resp.status_code == 429 and attempt < _MAX_SUBMIT_RETRIES - 1:
                log.warning("batch.submit_429", attempt=attempt + 1, delay=delay)
                await asyncio.sleep(delay)
                delay *= 2
                continue
            resp.raise_for_status()
            return resp
        raise RuntimeError("Unreachable")  # pragma: no cover


def create_batch_provider(
    *,
    gcs_bucket: str = "",
    gcs_prefix: str = "sova-batch",
) -> BatchProvider | None:
    """Create a batch provider using auto-detection. Returns None if no backend available."""
    global _warned_no_backend  # noqa: PLW0603

    project_id = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "")
    region = os.environ.get("CLOUD_ML_REGION", "us-east5")

    if project_id and gcs_bucket:
        try:
            import google.auth  # noqa: F401

            log.info("batch.backend_selected", backend="vertex", project=project_id, region=region)
            return BatchProvider(
                "vertex",
                project_id=project_id,
                region=region,
                gcs_bucket=gcs_bucket,
                gcs_prefix=gcs_prefix,
            )
        except ImportError:
            log.warning("batch.vertex_unavailable", reason="google-auth not installed")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        log.info("batch.backend_selected", backend="anthropic")
        return BatchProvider("anthropic", api_key=api_key)

    if not _warned_no_backend:
        log.info(
            "batch.no_backend",
            reason="neither Vertex AI (ANTHROPIC_VERTEX_PROJECT_ID + batch_gcs_bucket) "
            "nor Anthropic direct (ANTHROPIC_API_KEY) is configured",
        )
        _warned_no_backend = True
    return None
