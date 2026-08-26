"""LLM-based PR action suggestion service for the dual-evaluation experiment.

Calls the Claude API directly via httpx to suggest a next action for a PR,
then compares it against the deterministic model's choice. Supports two
backends: Vertex AI (Google Cloud ADC) and Anthropic direct (API key).
Vertex AI is preferred when ANTHROPIC_VERTEX_PROJECT_ID is set.

Results are cached server-side for 5 minutes per (pr_number,
deterministic_state, pr_computed_state) triple.

All results (agreements and disagreements) are cached and returned to the UI.
The UI shows a comparison widget when the LLM disagrees, and a standalone
"State is wrong" button on all PR-stage cards for user feedback.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx
from cachetools import TTLCache

from sova.config.loader import load_config
from sova.utils.logging import get_logger

log = get_logger(component="dashboard.llm_suggestion")

_MODEL = "claude-haiku-4-5-20251001"
_VERTEX_MODEL = "claude-haiku-4-5"
_MAX_TOKENS = 200
_CACHE_TTL = 300  # 5 minutes
_ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_API_VERSION = "2023-06-01"
_VERTEX_ANTHROPIC_VERSION = "vertex-2023-10-16"
_HTTP_TIMEOUT = 10.0

_cache: TTLCache[str, dict] = TTLCache(maxsize=100, ttl=_CACHE_TTL)
_warned_no_credentials: bool = False
_vertex_credentials: Any = None
_vertex_lock = asyncio.Lock()

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
- review_decision: {review_decision}
- ci_passed: {ci_passed}
- external_reviews_enabled: {external_reviews_enabled}

The rule-based system chose: {deterministic_action_id} ("{deterministic_action_label}")

Return ONLY valid JSON, no other text:
{{"action_id": "one_of_the_above", "reasoning": "one sentence why"}}"""


async def _is_enabled() -> bool:
    """Check whether the llm_suggestions config toggle is on."""
    try:
        cfg = await asyncio.to_thread(load_config)
    except Exception:
        log.warning("llm_suggestion.config_load_failed", exc_info=True)
        return True
    return cfg.dashboard.llm_suggestions


def _make_cache_key(pr_number: int, deterministic_state: str, pr_computed_state: str) -> str:
    return f"{pr_number}|{deterministic_state}|{pr_computed_state}"


def _detect_backend() -> str | None:
    """Detect which API backend is available. Vertex AI takes priority."""
    if os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "").strip():
        return "vertex"
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return "anthropic"
    return None


async def _get_vertex_token() -> str:
    """Get a Google Cloud ADC bearer token for Vertex AI."""
    global _vertex_credentials

    async with _vertex_lock:
        if _vertex_credentials is None:
            try:
                import google.auth
            except ImportError:
                raise ImportError(
                    "google-auth is required for Vertex AI LLM suggestions. Install: pip install google-auth"
                ) from None
            _vertex_credentials, _ = await asyncio.to_thread(
                google.auth.default,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )

        creds = _vertex_credentials
        if not getattr(creds, "token", "") or getattr(creds, "expired", False):
            try:
                import google.auth.transport.requests
            except ImportError:
                raise ImportError(
                    "google-auth is required for Vertex AI LLM suggestions. Install: pip install google-auth"
                ) from None
            await asyncio.to_thread(creds.refresh, google.auth.transport.requests.Request())

    return str(creds.token)


async def _build_request(backend: str, prompt: str) -> tuple[str, dict[str, str], dict]:
    """Build (url, headers, body) for the detected backend."""
    messages = [{"role": "user", "content": prompt}]

    if backend == "vertex":
        project_id = os.environ["ANTHROPIC_VERTEX_PROJECT_ID"]
        region = os.environ.get("CLOUD_ML_REGION", "us-east5")
        token = await _get_vertex_token()
        # Global region uses base domain without region prefix
        domain = "aiplatform.googleapis.com" if region == "global" else f"{region}-aiplatform.googleapis.com"
        url = (
            f"https://{domain}/v1/"
            f"projects/{project_id}/locations/{region}/"
            f"publishers/anthropic/models/{_VERTEX_MODEL}:rawPredict"
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "content-type": "application/json",
        }
        body: dict = {
            "max_tokens": _MAX_TOKENS,
            "messages": messages,
            "anthropic_version": _VERTEX_ANTHROPIC_VERSION,
        }
        return url, headers, body

    api_key = os.environ["ANTHROPIC_API_KEY"]
    headers = {
        "x-api-key": api_key,
        "anthropic-version": _ANTHROPIC_API_VERSION,
        "content-type": "application/json",
    }
    body = {
        "model": _MODEL,
        "max_tokens": _MAX_TOKENS,
        "messages": messages,
    }
    return _ANTHROPIC_API_URL, headers, body


async def get_llm_suggestion(
    *,
    pr_number: int,
    deterministic_state: str,
    deterministic_action_id: str,
    pr_computed_state: str,
    has_sova_review: bool,
    sova_verdict: str | None,
    mergeable: str,
    review_decision: str | None,
    ci_passed: bool,
    external_reviews_enabled: bool = True,
) -> dict | None:
    """Ask the LLM to suggest a PR action. Returns None on any error.

    Result shape when non-None:
        {action_id, action_label, reasoning, disagrees}
    """
    global _warned_no_credentials

    cache_key = _make_cache_key(pr_number, deterministic_state, pr_computed_state)
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    if not await _is_enabled():
        return None

    backend = _detect_backend()
    if backend is None:
        if not _warned_no_credentials:
            log.warning(
                "llm_suggestion.no_credentials",
                hint="Set ANTHROPIC_VERTEX_PROJECT_ID (Vertex AI) or ANTHROPIC_API_KEY (direct) "
                "to enable LLM action suggestions",
            )
            _warned_no_credentials = True
        return None

    try:
        prompt = _PROMPT.format(
            pr_computed_state=pr_computed_state or "unknown",
            has_sova_review=has_sova_review,
            sova_verdict=sova_verdict or "none",
            mergeable=mergeable or "unknown",
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
        url, headers, body = await _build_request(backend, prompt)
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            resp_body = resp.json()
        text = resp_body["content"][0]["text"].strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        parsed = json.loads(text)
    except Exception:
        log.warning("llm_suggestion.call_failed", pr=pr_number, exc_info=True)
        return None

    action_id = parsed.get("action_id", "")
    if action_id not in _PR_ACTION_LABELS:
        log.warning("llm_suggestion.invalid_action_id", pr=pr_number, action_id=action_id)
        return None

    result: dict = {
        "action_id": action_id,
        "action_label": _PR_ACTION_LABELS[action_id],
        "reasoning": str(parsed.get("reasoning", "")),
        "disagrees": action_id != deterministic_action_id,
    }
    _cache[cache_key] = result
    return result


def clear_cache() -> None:
    """Clear the suggestion cache. Intended for testing."""
    _cache.clear()
