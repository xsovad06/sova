"""Agent Runtime abstraction layer.

Defines the abstract interface that all coding agent backends must implement.
The default runtime (ClaudeCodeRuntime) wraps the Claude Code CLI.
Alternative runtimes (Aider, etc.) map to the same AgentProcess interface.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from abc import ABC, abstractmethod
from decimal import Decimal
from pathlib import Path

from sova.ipc.control import AgentProcess
from sova.llm.models import StreamEvent
from sova.utils.logging import get_logger

log = get_logger(component="ipc.runtime")


async def _check_cli_available(cli_name: str, install_hint: str) -> tuple[bool, str]:
    """Check if a CLI tool is installed and return its version.

    Shared helper for runtime ``check_available()`` implementations.
    """
    path = shutil.which(cli_name)
    if not path:
        return False, f"{cli_name} not found -- install: {install_hint}"
    try:
        proc = await asyncio.create_subprocess_exec(
            cli_name,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        version = stdout.decode().strip().split("\n")[0] if stdout else "unknown"
        return True, version
    except Exception as exc:
        return False, f"error checking version: {exc}"


class AgentRuntime(ABC):
    """Abstract interface for coding agent backends.

    Each runtime knows how to spawn a coding agent process that reads files,
    edits code, runs tests, and creates commits. Different from the LLM
    provider layer (which handles prompt -> response), this is a full
    autonomous coding agent.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable runtime name (e.g., 'claude-code', 'aider')."""
        ...

    @abstractmethod
    async def spawn(
        self,
        prompt: str,
        cwd: str | Path,
        *,
        env: dict[str, str] | None = None,
        model: str | None = None,
        max_budget_usd: Decimal | None = None,
    ) -> AgentProcess:
        """Spawn a coding agent process.

        Args:
            prompt: The task prompt.
            cwd: Working directory.
            env: Environment variables (None inherits parent).
            model: Optional model override.
            max_budget_usd: Optional budget cap.

        Returns:
            An AgentProcess wrapping the subprocess.
        """
        ...

    @abstractmethod
    def parse_output(self, line: str) -> StreamEvent | None:
        """Parse a line of agent stdout into a StreamEvent.

        Returns None for empty/whitespace lines only.
        """
        ...

    @abstractmethod
    async def check_available(self) -> tuple[bool, str]:
        """Check if this runtime's CLI tool is installed.

        Returns:
            Tuple of (available, detail_message).
        """
        ...


class ClaudeCodeRuntime(AgentRuntime):
    """Runtime that spawns Claude Code CLI processes."""

    @property
    def name(self) -> str:
        return "claude-code"

    async def spawn(
        self,
        prompt: str,
        cwd: str | Path,
        *,
        env: dict[str, str] | None = None,
        model: str | None = None,
        max_budget_usd: Decimal | None = None,
    ) -> AgentProcess:
        return await AgentProcess.spawn(
            prompt=prompt,
            cwd=cwd,
            model=model,
            max_budget_usd=max_budget_usd,
            env=env,
        )

    def parse_output(self, line: str) -> StreamEvent | None:
        stripped = line.strip()
        if not stripped:
            return None
        try:
            data = json.loads(stripped)
        except ValueError:
            return StreamEvent(type="content", text=line)

        event_type = data.get("type", "")
        if event_type == "assistant":
            content = data.get("content", "")
            if isinstance(content, list):
                parts = [b.get("text", "") for b in content if b.get("type") == "text"]
                text = "".join(parts)
            elif isinstance(content, str):
                text = content
            else:
                text = str(content)
            return StreamEvent(type="content", text=text) if text else None

        if event_type == "result":
            return StreamEvent(type="result", text=data.get("result", ""))

        return None

    async def check_available(self) -> tuple[bool, str]:
        return await _check_cli_available(
            "claude", "https://docs.anthropic.com/en/docs/claude-code"
        )


class AiderRuntime(AgentRuntime):
    """Runtime that spawns Aider CLI processes.

    Aider (https://aider.chat) is an open-source AI pair programming tool.
    It supports multiple LLM backends and edits code via git commits.
    """

    @property
    def name(self) -> str:
        return "aider"

    async def spawn(
        self,
        prompt: str,
        cwd: str | Path,
        *,
        env: dict[str, str] | None = None,
        model: str | None = None,
        max_budget_usd: Decimal | None = None,
    ) -> AgentProcess:
        args: list[str] = [
            "aider",
            "--message",
            prompt,
            "--yes-always",
            "--no-pretty",
            "--no-suggest-shell-commands",
        ]

        if model:
            args.extend(["--model", model])

        if max_budget_usd is not None:
            log.warning("aider.budget_not_enforced", budget=str(max_budget_usd))

        log.info("aider.spawn", cwd=str(cwd), model=model, prompt_len=len(prompt))

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
        return AgentProcess(proc)

    def parse_output(self, line: str) -> StreamEvent | None:
        stripped = line.strip()
        if not stripped:
            return None
        return StreamEvent(type="content", text=stripped)

    async def check_available(self) -> tuple[bool, str]:
        return await _check_cli_available("aider", "pip install aider-chat")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_RUNTIMES: dict[str, type[AgentRuntime]] = {
    "claude-code": ClaudeCodeRuntime,
    "aider": AiderRuntime,
}


def create_runtime(runtime_type: str = "claude-code") -> AgentRuntime:
    """Create an AgentRuntime instance by type name.

    Args:
        runtime_type: Runtime identifier (e.g., "claude-code", "aider").

    Returns:
        An AgentRuntime instance.

    Raises:
        ValueError: If the runtime type is unknown.
    """
    cls = _RUNTIMES.get(runtime_type)
    if cls is None:
        available = ", ".join(sorted(_RUNTIMES))
        raise ValueError(f"Unknown agent runtime: {runtime_type!r}. Available: {available}")
    return cls()


# ---------------------------------------------------------------------------
# Module-level singleton (mirrors sova.llm.client pattern)
# ---------------------------------------------------------------------------

_runtime: AgentRuntime | None = None


def get_runtime() -> AgentRuntime:
    """Get the current agent runtime (defaults to ClaudeCodeRuntime)."""
    global _runtime  # noqa: PLW0603
    if _runtime is None:
        _runtime = ClaudeCodeRuntime()
    return _runtime


def set_runtime(runtime: AgentRuntime) -> None:
    """Set the module-level agent runtime."""
    global _runtime  # noqa: PLW0603
    _runtime = runtime
