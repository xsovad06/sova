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

from sova.config.models import RolesConfig
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


async def invoke(
    prompt: str,
    *,
    model: str | None = None,
    cwd: Path | str | None = None,
    max_budget_usd: Decimal | None = None,
    timeout: float | None = 600,
) -> LLMResult:
    """Run a prompt via the active LLM provider."""
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
    async for event in get_provider().invoke_streaming(prompt, model=model, cwd=cwd, max_budget_usd=max_budget_usd):
        yield event


def resolve_model(role: str, roles_config: RolesConfig) -> str | None:
    """Resolve the model for a given agent role.

    Args:
        role: Agent role name (e.g., "researcher", "triage", "developer").
        roles_config: The roles configuration section.

    Returns:
        Model alias string, or None if no role-specific model is configured.
    """
    role_model_fields = {
        "researcher": "researcher_model",
        "triage": "triage_model",
    }

    field_name = role_model_fields.get(role)
    if field_name:
        return getattr(roles_config, field_name, None)

    return None
