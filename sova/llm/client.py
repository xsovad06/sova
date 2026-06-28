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
from sova.llm.models import LLMResult, StreamEvent
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
    cwd: Path | str | None = None,
    max_budget_usd: Decimal | None = None,
    timeout: float | None = 600,
) -> LLMResult:
    """Run a prompt via the active LLM provider."""
    from sova.llm.guard import guard_prompt

    guard_prompt(prompt)
    return await get_provider().invoke(prompt, model=model, cwd=cwd, max_budget_usd=max_budget_usd, timeout=timeout)


async def invoke_command(
    command: str,
    args: str = "",
    *,
    model: str | None = None,
    cwd: Path | str | None = None,
    max_budget_usd: Decimal | None = None,
    timeout: float | None = 600,
) -> LLMResult:
    """Run a slash command via the active LLM provider."""
    return await get_provider().invoke_command(
        command, args, model=model, cwd=cwd, max_budget_usd=max_budget_usd, timeout=timeout
    )


async def invoke_streaming(
    prompt: str,
    *,
    model: str | None = None,
    cwd: Path | str | None = None,
    max_budget_usd: Decimal | None = None,
) -> AsyncIterator[StreamEvent]:
    """Stream output from the active LLM provider."""
    from sova.llm.guard import guard_prompt

    guard_prompt(prompt)
    async for event in get_provider().invoke_streaming(prompt, model=model, cwd=cwd, max_budget_usd=max_budget_usd):
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
) -> str | None:
    """Resolve the model for a given agent role.

    Priority: role-specific config > complexity-based routing > None.

    Args:
        role: Agent role name (e.g., "researcher", "triage", "developer").
        roles_config: The roles configuration section.
        complexity: Task complexity tier for model routing fallback.
        llm_config: LLM configuration with optional routing overrides.

    Returns:
        Model alias string, or None if no model is resolved.
    """
    field_name = _ROLE_MODEL_FIELDS.get(role)
    if field_name:
        value = getattr(roles_config, field_name, None)
        if value:
            return value

    if complexity is not None:
        from sova.llm.routing import route_model

        return route_model(complexity, llm_config=llm_config)

    return None
