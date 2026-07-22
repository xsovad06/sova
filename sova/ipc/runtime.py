"""Agent Runtime abstraction layer.

Defines the abstract interface that all coding agent backends must implement.
The default runtime (ClaudeCodeRuntime) wraps the Claude Code CLI.
Alternative runtimes (Aider, etc.) map to the same AgentProcess interface.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from abc import ABC, abstractmethod
from decimal import Decimal
from pathlib import Path

from sova.ipc.control import AgentProcess
from sova.llm.models import LLMResult, StreamEvent
from sova.utils.logging import get_logger

log = get_logger(component="ipc.runtime")


_VERSION_CHECK_TIMEOUT = 5.0
_SUBPROCESS_LINE_LIMIT = 10 * 1024 * 1024  # 10 MB -- agent JSON lines can exceed 64 KB default

_HEADLESS_PREAMBLE = (
    "[HEADLESS MODE] You are running as an autonomous agent with no "
    "human operator. Do not ask for confirmation or pose questions. "
    "Proceed with file edits, test runs, and any other actions "
    "required by the task.\n\n"
    "PIPELINE BOUNDARY: You are executing a single step inside SOVA's "
    "workflow pipeline. Do NOT create pull requests, do NOT push to "
    "remote, do NOT commit changes, and do NOT run sova CLI commands "
    "unless the step instructions explicitly tell you to. If your "
    "step fails, exit immediately so the pipeline can handle retries. "
    "Never attempt to complete remaining pipeline steps on your own.\n\n"
    "COMMAND INTERPRETATION: When the instruction below contains a "
    "```bash``` code block with a CLI command (e.g., `sova run 42`), "
    "you MUST execute that exact command in your bash shell. Do NOT "
    "interpret the command as a natural language task description. "
    "Do NOT try to implement the work yourself. The command is a "
    "literal shell invocation that must be run as-is.\n\n"
    "WORKTREE CONFLICT RECOVERY: If you encounter worktree conflicts, "
    "the git operations will resolve them automatically. Do not attempt "
    "manual worktree removal.\n\n"
    "CONTEXT MANAGEMENT: Monitor your context window usage. When "
    "context grows large (after reading many files or long outputs), "
    "use /compact proactively to free space. Prefer reading specific "
    "file sections (line ranges) over entire files. Summarize long "
    "command outputs before continuing.\n\n"
    "Execute the following instruction exactly as specified:\n\n"
)


async def _check_cli_available(cli_name: str, install_hint: str) -> tuple[bool, str]:
    """Check if a CLI tool is installed and return its version.

    Shared helper for runtime ``check_available()`` implementations.
    """
    path = shutil.which(cli_name)
    if not path:
        return False, f"{cli_name} not found -- install: {install_hint}"
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                cli_name,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=_VERSION_CHECK_TIMEOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_VERSION_CHECK_TIMEOUT)
        if proc.returncode != 0:
            return False, f"{cli_name} --version exited with code {proc.returncode}"
        version = stdout.decode().strip().split("\n")[0] if stdout else "unknown"
        return True, version
    except asyncio.TimeoutError:
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
        return False, f"{cli_name} --version timed out"
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

    def transform_prompt(self, prompt: str) -> str:
        """Transform a prompt before passing to the runtime.

        The default implementation returns the prompt unchanged. Runtimes
        that cannot execute shell commands (e.g., Aider) should override
        this to detect shell-command-formatted prompts and extract the
        task description.
        """
        return prompt

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
        args: list[str] = [
            "claude",
            "-p",
            _HEADLESS_PREAMBLE + prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "auto",
        ]

        if model:
            args.extend(["--model", model])

        if max_budget_usd is not None:
            args.extend(["--max-budget-usd", str(max_budget_usd)])

        log.info("process.spawn", cwd=str(cwd), model=model, prompt_len=len(prompt))

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            limit=_SUBPROCESS_LINE_LIMIT,
        )

        return AgentProcess(proc)

    def parse_output(self, line: str) -> StreamEvent | None:
        stripped = line.strip()
        if not stripped:
            return None
        try:
            data = json.loads(stripped)
            if not isinstance(data, dict):
                return StreamEvent(type="content", text=line)
        except ValueError:
            return StreamEvent(type="content", text=line)

        event_type = data.get("type", "")
        if event_type == "assistant":
            content = data.get("content", "")
            if isinstance(content, list):
                parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                text = "".join(parts)
            elif isinstance(content, str):
                text = content
            else:
                text = str(content)
            return StreamEvent(type="content", text=text) if text else None

        if event_type == "result":
            result_text = str(data.get("result", ""))
            try:
                cost_usd = Decimal(str(data.get("total_cost_usd", 0)))
            except Exception:
                cost_usd = Decimal(0)
            usage = data.get("usage", {})
            if not isinstance(usage, dict):
                usage = {}
            llm_result = LLMResult(
                text=result_text,
                model=str(data.get("model", "")),
                cost_usd=cost_usd,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cache_read_tokens=usage.get("cache_read_tokens", 0),
                cache_creation_tokens=usage.get("cache_creation_tokens", 0),
                duration_ms=data.get("duration_ms", 0),
                session_id=str(data.get("session_id", "")),
            )
            return StreamEvent(type="result", text=result_text, result=llm_result)

        return None

    async def check_available(self) -> tuple[bool, str]:
        return await _check_cli_available("claude", "https://docs.anthropic.com/en/docs/claude-code")


class AiderRuntime(AgentRuntime):
    """Runtime that spawns Aider CLI processes.

    Aider (https://aider.chat) is an open-source AI pair programming tool.
    It supports multiple LLM backends and edits code via git commits.
    """

    @property
    def name(self) -> str:
        return "aider"

    # Pattern matching shell-command prompts from start_agent() / start_command().
    # Format: "Run the following command...\n```bash\nsova run 28\n```"
    _SHELL_CMD_RE = re.compile(r"```(?:bash|sh)\s*\n([^`]+)\n```")

    def transform_prompt(self, prompt: str) -> str:
        """Detect shell-command-formatted prompts and extract the sova command.

        Aider cannot execute shell commands. When the prompt contains a
        fenced bash block with a ``sova`` CLI invocation, it must be run
        via subprocess rather than passed as an Aider ``--message``.
        """
        match = self._SHELL_CMD_RE.search(prompt)
        if match:
            cmd = match.group(1).strip()
            if cmd.startswith("sova "):
                log.warning(
                    "aider.shell_prompt_detected",
                    hint="Aider cannot execute shell commands; the sova command "
                    "will be executed via subprocess instead of aider --message",
                    cmd=cmd,
                )
                return cmd
        return prompt

    async def spawn(
        self,
        prompt: str,
        cwd: str | Path,
        *,
        env: dict[str, str] | None = None,
        model: str | None = None,
        max_budget_usd: Decimal | None = None,
    ) -> AgentProcess:
        transformed = self.transform_prompt(prompt)

        # If the prompt was a sova CLI command, execute it directly
        # instead of passing to Aider (which cannot run shell commands).
        if transformed != prompt and transformed.startswith("sova "):
            log.info("aider.exec_sova_cmd", cwd=str(cwd), cmd=transformed)
            import shlex as _shlex

            cmd_parts = _shlex.split(transformed)
            proc = await asyncio.create_subprocess_exec(
                *cmd_parts,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                limit=_SUBPROCESS_LINE_LIMIT,
            )
            return AgentProcess(proc)

        args: list[str] = [
            "aider",
            "--message",
            transformed,
            "--yes-always",
            "--no-pretty",
            "--no-suggest-shell-commands",
        ]

        if model:
            args.extend(["--model", model])

        if max_budget_usd is not None:
            log.warning(
                "aider.budget_not_enforced",
                budget=str(max_budget_usd),
                hint="Aider does not support budget caps; cost is not limited",
            )

        log.info("aider.spawn", cwd=str(cwd), model=model, prompt_len=len(transformed))

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            limit=_SUBPROCESS_LINE_LIMIT,
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
    if runtime_type == "mock":
        from sova.ipc.testing import MockRuntime

        return MockRuntime()

    cls = _RUNTIMES.get(runtime_type)
    if cls is None:
        available = ", ".join(sorted([*_RUNTIMES, "mock"]))
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
