"""LLM-based PR action suggestion service for the dual-evaluation experiment.

Calls the Anthropic Messages API to suggest a next action for a PR, then compares
it against the deterministic model's choice. Results are cached server-side for
5 minutes per (pr_number, deterministic_state, pr_computed_state) triple.

Only suggestions that DISAGREE with the deterministic model are surfaced to users.
"""

from __future__ import annotations

import json
import os
import time

import httpx

from sova.utils.logging import get_logger

log = get_logger(component="dashboard.llm_suggestion")

_ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
_MODEL = "claude-haiku-4-5-20251001"
_CACHE_TTL = 300  # 5 minutes

_cache: dict[str, tuple[float, dict]] = {}

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


def _make_cache_key(pr_number: int, deterministic_state: str, pr_computed_state: str) -> str:
    return f"{pr_number}|{deterministic_state}|{pr_computed_state}"


def _get_cached(key: str) -> dict | None:
    entry = _cache.get(key)
    if entry and (time.monotonic() - entry[0]) < _CACHE_TTL:
        return entry[1]
    if entry:
        del _cache[key]
    return None


def _set_cached(key: str, result: dict) -> None:
    _cache[key] = (time.monotonic(), result)


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
    """Ask the LLM to suggest a PR action. Returns None if no API key or on any error.

    Result shape when non-None:
        {action_id, action_label, reasoning, disagrees}

    The caller should only surface the widget when disagrees=True.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.debug("llm_suggestion.no_api_key")
        return None

    cache_key = _make_cache_key(pr_number, deterministic_state, pr_computed_state)
    cached = _get_cached(cache_key)
    if cached is not None:
        return cached

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
    except Exception:
        log.warning("llm_suggestion.prompt_format_failed", pr=pr_number, exc_info=True)
        return None

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                _ANTHROPIC_API_URL,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": _MODEL,
                    "max_tokens": 200,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=10.0,
            )
            response.raise_for_status()
            data = response.json()
            raw_text = data["content"][0]["text"]
            parsed = json.loads(raw_text)
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
    _set_cached(cache_key, result)
    return result


def clear_cache() -> None:
    """Clear the suggestion cache. Intended for testing."""
    _cache.clear()
