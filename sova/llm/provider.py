"""LLM Provider abstraction layer.

Defines the abstract interface that all LLM backends must implement.
The default provider (ClaudeCodeProvider) wraps the Claude Code CLI.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from sova.llm.models import BatchRequest, BatchResult, LLMResult, StreamEvent
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.config.models import LLMConfig

log = get_logger(component="llm.provider")


def _resolve_primary_root(cwd: Path) -> Path | None:
    """Resolve the primary worktree root synchronously (for fallback restoration)."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        common = Path(result.stdout.strip())
        if not common.is_absolute():
            common = (cwd / common).resolve()
        root = common.parent if common.name == ".git" else common.parent.parent
        if root == cwd:
            return None
        return root
    except Exception:  # noqa: BLE001 (subprocess+path ops, silent fallback)
        log.debug("llm.resolve_primary_root_failed", exc_info=True)
        return None


def _assert_command_exists(command: str, cwd: Path) -> None:
    """Fail fast if a slash command file is missing from the target project.

    When running inside a worktree, attempts to restore ``.claude/`` artifacts
    from the primary checkout before raising, since rebase stash operations
    can destroy them.
    """
    name = command.lstrip("/")
    if not name or "/" in name or "\\" in name or ".." in name:
        raise RuntimeError(f"Invalid slash command: {command!r}")
    cmd_path = cwd / ".claude" / "commands" / f"{name}.md"
    if not cmd_path.is_file():
        project_root = _resolve_primary_root(cwd)
        if project_root:
            try:
                from sova.git.worktree import ensure_claude_artifacts

                ensure_claude_artifacts(project_root, cwd)
                if cmd_path.is_file():
                    log.info("llm.command_restored", command=command, cwd=str(cwd))
                    return
            except (RuntimeError, OSError):
                log.debug("llm.command_restore_failed", command=command, cwd=str(cwd), exc_info=True)
        raise RuntimeError(
            f"Command {command} not found at {cmd_path}. Run 'sova commands update --project {cwd}' to install it."
        )


@dataclass(frozen=True)
class ProviderCapabilities:
    """Feature flags a caller can introspect instead of assuming Claude-CLI behavior.

    Every field defaults to ``False`` so a provider subclass that does not
    override :attr:`LLMProvider.capabilities` is never assumed more capable
    than it has proven to be (fail toward "assume unreliable"), rather than the
    fallback and budget layers assuming CLI-only features (native
    ``--fallback-model``, native ``--max-budget-usd`` enforcement, trustworthy
    cost reporting, open-ended model enumeration) are universal across
    providers.
    """

    supports_cli_fallback: bool = False
    supports_budget_cap: bool = False
    reports_cost: bool = False
    dynamic_models: bool = False


class LLMProvider(ABC):
    """Abstract interface for LLM providers.

    All LLM interactions in SOVA flow through this interface, enabling
    provider-agnostic code in workflow steps and roles.
    """

    @abstractmethod
    async def invoke(
        self,
        prompt: str,
        *,
        model: str | None = None,
        fallback_model: str | None = None,
        cwd: Path | str | None = None,
        max_budget_usd: Decimal | None = None,
        timeout: float | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        """Run a prompt and return the parsed result."""
        ...

    @abstractmethod
    async def invoke_streaming(
        self,
        prompt: str,
        *,
        model: str | None = None,
        cwd: Path | str | None = None,
        max_budget_usd: Decimal | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Run a prompt with streaming output, yielding events."""
        ...
        yield  # pragma: no cover -- required for async generator typing

    async def invoke_command(
        self,
        command: str,
        args: str = "",
        *,
        model: str | None = None,
        fallback_model: str | None = None,
        cwd: Path | str | None = None,
        max_budget_usd: Decimal | None = None,
        timeout: float | None = None,
    ) -> LLMResult:
        """Run a slash command (e.g., /develop, /review).

        Default implementation constructs a prompt and delegates to invoke().
        Providers that support native command dispatch can override this.
        """
        if command.startswith("/") and cwd:
            _assert_command_exists(command, Path(cwd))
        prompt = f"{command} {args}".strip() if args else command
        log.info("llm.invoke_command", command=command, args_len=len(args), model=model)
        return await self.invoke(
            prompt, model=model, fallback_model=fallback_model, cwd=cwd, max_budget_usd=max_budget_usd, timeout=timeout
        )

    async def invoke_batch(
        self,
        requests: list[BatchRequest],
        *,
        poll_interval: int = 60,
        timeout: int = 86400,
    ) -> list[BatchResult]:
        """Submit a batch of prompts and return results in input order.

        Default implementation calls invoke() sequentially.
        Batch-capable providers override this.
        """
        results: list[BatchResult] = []
        deadline = time.monotonic() + timeout
        for req in requests:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                results.append(BatchResult(request=req, error=f"Batch timeout of {timeout}s exhausted"))
                continue
            try:
                r = await self.invoke(
                    req.prompt,
                    model=req.model or None,
                    timeout=remaining,
                    system_prompt=req.system or None,
                    max_tokens=req.max_tokens,
                )
                results.append(BatchResult(request=req, result=r))
            except Exception as exc:  # noqa: BLE001 (arbitrary provider error)
                log.warning("llm.batch_item_failed", custom_id=req.custom_id, error=str(exc), exc_info=True)
                results.append(BatchResult(request=req, error=str(exc)))
        return results

    def normalize_model_name(self, model: str) -> str:
        """Map generic model tiers (fast/smart/cheap) to provider-specific IDs.

        Default implementation returns the model unchanged. Providers can
        override to map generic tiers to provider-specific identifiers.
        """
        return model

    @abstractmethod
    async def check_available(self) -> tuple[bool, str]:
        """Check if this provider is available and configured.

        Returns:
            Tuple of (available, detail_message).
        """
        ...

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Declare what this provider can reliably guarantee.

        Concrete default (not abstract) so existing and third-party provider
        subclasses do not break. Providers override this to opt into stronger
        guarantees as they earn them; the conservative default means an
        unaudited provider is treated as unable to cap budget or report cost.
        """
        return ProviderCapabilities()


def create_provider(cfg: LLMConfig) -> LLMProvider:
    """Create an LLM provider instance from the whole ``llm`` config section.

    Takes the entire :class:`~sova.config.models.LLMConfig` rather than a
    hand-picked set of keyword arguments so a newly added field cannot silently
    no-op at a call site that was never updated to forward it (the failure mode
    that made ``reload_provider`` drop fields after a settings hot-reload; see
    docs/model-selection-risk-assessment.md, R12).

    Args:
        cfg: The project's ``llm`` config section. ``cfg.provider`` selects the
            backend; the remaining fields are read per backend (``model``,
            ``fallback_model`` and ``api_base`` for LiteLLM; ``model`` and
            ``api_key`` for the Anthropic API, which falls back to the
            ``ANTHROPIC_API_KEY`` env var when the key is empty). ``model`` and
            ``fallback_model`` are resolved through ``cfg.model_aliases`` first,
            so a deployment can point a generic tier name (e.g. ``"smart"``) at
            these fields exactly as it can at a per-call ``model=`` argument.

    Returns:
        An LLMProvider instance.

    Raises:
        ValueError: If the provider type is unknown.
        ImportError: If litellm or anthropic SDK is not installed.
    """
    if cfg.provider == "claude-code":
        from sova.llm.providers.claude_code import ClaudeCodeProvider

        return ClaudeCodeProvider()

    if cfg.provider in ("litellm", "hybrid", "openai", "ollama", "vertex"):
        from sova.llm.client import resolve_alias
        from sova.llm.litellm_provider import LiteLLMProvider

        # The vendor-specific types are LiteLLM under a more discoverable name.
        # LLMConfig._default_model_for_litellm guarantees cfg.model is set for
        # them, so the Claude default below only ever applies to litellm/hybrid.
        model = resolve_alias(cfg.model, cfg.model_aliases) if cfg.model else cfg.model
        fallback_model = (
            resolve_alias(cfg.fallback_model, cfg.model_aliases) if cfg.fallback_model else cfg.fallback_model
        )
        return LiteLLMProvider(
            model=model or "claude-sonnet-4-6",
            fallback_model=fallback_model or None,
            api_base=cfg.api_base or None,
        )

    if cfg.provider == "anthropic":
        from sova.llm.client import resolve_alias
        from sova.llm.providers.anthropic_api import AnthropicAPIProvider

        model = resolve_alias(cfg.model, cfg.model_aliases) if cfg.model else cfg.model
        return AnthropicAPIProvider(model=model or "", api_key=cfg.api_key)

    available = ["claude-code", "litellm", "hybrid", "anthropic", "openai", "ollama", "vertex"]
    raise ValueError(f"Unknown LLM provider: {cfg.provider!r}. Available: {', '.join(available)}")


def _measure_ms(start: float) -> int:
    """Convert a time.monotonic() start to elapsed milliseconds."""
    return int((time.monotonic() - start) * 1000)
