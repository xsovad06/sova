"""LLM client -- thin delegation layer to the active provider.

All existing callers (``from sova.llm.client import invoke``) continue
to work unchanged.  The actual implementation lives in the configured
:class:`~sova.llm.provider.LLMProvider` (default: ClaudeCodeProvider).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from sova.config.models import LLMConfig, RolesConfig
from sova.llm.complexity import ComplexityTier
from sova.llm.models import BatchRequest, BatchResult, LLMResult, StreamEvent
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.llm.provider import LLMProvider

log = get_logger(component="llm.client")

_provider: LLMProvider | None = None


def get_provider() -> LLMProvider:
    """Return the active LLM provider, creating a default if needed."""
    global _provider  # noqa: PLW0603
    if _provider is None:
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        _provider = ClaudeCodeProvider()
    return _provider


def set_provider(provider: LLMProvider) -> None:
    """Replace the active LLM provider (e.g., from config at startup)."""
    global _provider  # noqa: PLW0603
    _provider = provider


def reset_provider() -> None:
    """Reset the global provider to None (for testing)."""
    global _provider  # noqa: PLW0603
    _provider = None


async def invoke(
    prompt: str,
    *,
    model: str | None = None,
    fallback_model: str | None = None,
    task_type: str | None = None,
    cwd: Path | str | None = None,
    max_budget_usd: Decimal | None = None,
    timeout: float | None = 600,
) -> LLMResult:
    """Run a prompt via the active LLM provider.

    Args:
        task_type: Routing category (e.g. "triage", "harden", "planner").
            When set and *model* is ``None``, looks up ``llm.routing[task_type]``
            to select a model. Ignored when *model* is explicitly provided.
            Requires ``provider = "litellm"`` or ``"hybrid"``.
    """
    from sova.llm.guard import guard_prompt

    guard_prompt(prompt)
    resolved = _resolve_task_type_model(model, task_type, cwd=cwd)
    return await get_provider().invoke(
        prompt, model=resolved, fallback_model=fallback_model, cwd=cwd, max_budget_usd=max_budget_usd, timeout=timeout
    )


def _resolve_task_type_model(model: str | None, task_type: str | None, *, cwd: Path | str | None = None) -> str | None:
    """Resolve model from task_type routing if no explicit model is provided."""
    if model or not task_type:
        return model

    try:
        from sova.config.loader import load_config

        cfg = load_config(Path(cwd) if cwd else None)
        if not cfg.llm.routing:
            return model

        override = cfg.llm.routing.get(task_type)
        if override is not None:
            log.info("llm.task_type_route", task_type=task_type, model=override)
            return override
    except Exception:
        log.debug("llm.task_type_resolve_failed", task_type=task_type, exc_info=True)

    return model


async def invoke_command(
    command: str,
    args: str = "",
    *,
    model: str | None = None,
    fallback_model: str | None = None,
    cwd: Path | str | None = None,
    max_budget_usd: Decimal | None = None,
    timeout: float | None = 600,
) -> LLMResult:
    """Run a slash command via the active LLM provider."""
    if args:
        from sova.llm.guard import guard_prompt

        assembled = f"{command} {args}".strip()
        guard_prompt(assembled)
    return await get_provider().invoke_command(
        command,
        args,
        model=model,
        fallback_model=fallback_model,
        cwd=cwd,
        max_budget_usd=max_budget_usd,
        timeout=timeout,
    )


async def invoke_batch(
    requests: list[BatchRequest],
    *,
    poll_interval: int = 60,
    timeout: int = 86400,
    gcs_bucket: str = "",
    gcs_prefix: str = "sova-batch",
) -> list[BatchResult]:
    """Submit a batch of prompts. Uses a dedicated batch provider if available,
    otherwise falls back to the global provider's sequential default."""
    if not requests:
        return []

    from sova.llm.guard import guard_prompt

    for req in requests:
        guard_prompt(req.prompt)

    from sova.llm.providers.anthropic_batch import create_batch_provider

    batch_provider = create_batch_provider(gcs_bucket=gcs_bucket, gcs_prefix=gcs_prefix)
    if batch_provider is not None:
        return await batch_provider.invoke_batch(requests, poll_interval=poll_interval, timeout=timeout)

    return await get_provider().invoke_batch(requests, poll_interval=poll_interval, timeout=timeout)


async def invoke_streaming(
    prompt: str,
    *,
    model: str | None = None,
    task_type: str | None = None,
    cwd: Path | str | None = None,
    max_budget_usd: Decimal | None = None,
) -> AsyncIterator[StreamEvent]:
    """Stream output from the active LLM provider."""
    from sova.llm.guard import guard_prompt

    guard_prompt(prompt)
    resolved = _resolve_task_type_model(model, task_type, cwd=cwd)
    async for event in get_provider().invoke_streaming(prompt, model=resolved, cwd=cwd, max_budget_usd=max_budget_usd):
        yield event


_ROLE_MODEL_FIELDS: dict[str, str] = {
    "researcher": "researcher_model",
    "triage": "triage_model",
}


def resolve_model(
    role: str,
    roles_config: RolesConfig,
    *,
    complexity: ComplexityTier | None = None,
    llm_config: LLMConfig | None = None,
) -> tuple[str, str] | None:
    """Resolve the model for a given agent role.

    Priority: role-specific config > complexity-based routing > None.

    Returns:
        (model_alias, reason) tuple, or None if no model is resolved.
    """
    field_name = _ROLE_MODEL_FIELDS.get(role)
    if field_name:
        value = getattr(roles_config, field_name, None)
        if value:
            return value, f"role:{role}->{value}"

    if complexity is not None:
        from sova.llm.routing import route_model

        return route_model(complexity, llm_config=llm_config)

    return None
