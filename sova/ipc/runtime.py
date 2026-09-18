"""Agent Runtime abstraction layer.

Defines the abstract interface that all coding agent backends must implement.
The default runtime (ClaudeCodeRuntime) wraps the Claude Code CLI.
Alternative runtimes (Aider, etc.) map to the same AgentProcess interface.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from decimal import Decimal
from pathlib import Path

from sova.config.models import CodexConfig, ProjectConfig
from sova.ipc.control import AgentProcess, FileAgentProcess
from sova.llm.cli_args import build_claude_cli_args
from sova.llm.models import LLMResult, StreamEvent
from sova.utils.env import ANTHROPIC_CREDENTIAL_VARS, configured_passthrough, scrub_agent_env
from sova.utils.logging import get_logger
from sova.utils.shell import run

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
    "NO BACKGROUND WAITING: this session ends the moment you produce a "
    "turn with no tool call, even if you believe a background task, poll "
    "loop, or notification will resume you later. Headless mode has no "
    "such resumption mechanism. Never launch a long-running or blocking "
    "operation (e.g., polling CI checks) as a background task and then "
    "stop to 'wait' for it. Run it as a single synchronous foreground "
    "command that blocks until it finishes, and keep issuing tool calls "
    "until the entire task is actually complete.\n\n"
    "CONTEXT MANAGEMENT: Monitor your context window usage. When "
    "context grows large (after reading many files or long outputs), "
    "use /compact proactively to free space. Prefer reading specific "
    "file sections (line ranges) over entire files. Summarize long "
    "command outputs before continuing.\n\n"
    "Execute the following instruction exactly as specified:\n\n"
)


_SOVA_AGENT_ENV_KEY = "SOVA_AGENT_RUN"


def _inject_agent_marker(
    env: dict[str, str] | None,
    *,
    extra_scrub: Iterable[str] = (),
    extra_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the child environment for a spawned agent.

    Scrubs inherited provider-routing and parent-session variables (see
    ``sova.utils.env``) and sets SOVA_AGENT_RUN=1 so benchmark hooks skip
    logging. Every spawn path in this module routes through here.

    ``extra_scrub`` removes additional variables for this spawn only (e.g. a
    cross-provider credential that must never reach this particular runtime).
    ``extra_env`` is merged in after scrubbing, so it can re-admit a variable
    this spawn explicitly wants (e.g. ``CodexRuntime`` re-injecting
    ``CODEX_API_KEY``, which ``SCRUBBED_VARS`` strips by default) without
    reopening it for every other runtime.
    """
    merged = scrub_agent_env(env, passthrough=configured_passthrough(), extra_scrub=extra_scrub)
    merged[_SOVA_AGENT_ENV_KEY] = "1"
    if extra_env:
        merged.update(extra_env)
    return merged


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
    except Exception as exc:  # noqa: BLE001 (availability probe must never raise; any failure means "unavailable")
        log.debug("runtime.version_probe_failed", cli=cli_name, exc_info=True)
        return False, f"error checking version: {exc}"


async def _spawn_agent_process(
    args: list[str],
    cwd: str | Path,
    env: dict[str, str] | None,
    output_dir: Path | None,
    run_label: str | None,
    *,
    extra_scrub: Iterable[str] = (),
    extra_env: Mapping[str, str] | None = None,
) -> AgentProcess | FileAgentProcess:
    """Spawn ``args`` in the scrubbed agent environment.

    Redirects stdout/stderr to files when ``output_dir`` is set, otherwise
    pipes them for streaming. Every runtime spawn path funnels through here,
    so ``start_new_session=True`` (see the subprocess isolation note in
    ``.claude/rules/architecture.md``) cannot be omitted by one of them.

    ``extra_scrub``/``extra_env`` are per-spawn overrides layered on top of
    the shared scrub; see ``_inject_agent_marker()``.
    """
    agent_env = _inject_agent_marker(env, extra_scrub=extra_scrub, extra_env=extra_env)

    if output_dir is not None:
        return await _spawn_with_file_output(args, cwd, agent_env, output_dir, run_label)

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=agent_env,
        limit=_SUBPROCESS_LINE_LIMIT,
        start_new_session=True,
    )
    return AgentProcess(proc)


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
        """Human-readable runtime name (e.g., 'claude-code', 'aider', 'codex')."""
        ...

    @abstractmethod
    async def spawn(
        self,
        prompt: str,
        cwd: str | Path,
        *,
        env: dict[str, str] | None = None,
        model: str | None = None,
        fallback_model: str | None = None,
        max_budget_usd: Decimal | None = None,
        output_dir: Path | None = None,
        run_label: str | None = None,
    ) -> AgentProcess | FileAgentProcess:
        """Spawn a coding agent process.

        Args:
            prompt: The task prompt.
            cwd: Working directory.
            env: Environment variables (None inherits parent).
            model: Optional model override.
            fallback_model: Optional fallback model for billing/rate-limit resilience.
            max_budget_usd: Optional budget cap.
            output_dir: When set, redirect stdout/stderr to files in this
                directory and return a FileAgentProcess. When None, use
                pipes and return AgentProcess (backward compat).
            run_label: Filename prefix for output files (e.g., the run ID).
                Required when output_dir is set.

        Returns:
            An AgentProcess (pipe-based) or FileAgentProcess (file-based).
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
        fallback_model: str | None = None,
        max_budget_usd: Decimal | None = None,
        output_dir: Path | None = None,
        run_label: str | None = None,
    ) -> AgentProcess | FileAgentProcess:
        args = build_claude_cli_args(
            _HEADLESS_PREAMBLE + prompt,
            model=model,
            fallback_model=fallback_model,
            max_budget_usd=max_budget_usd,
            output_format="stream-json",
        )

        log.info("process.spawn", cwd=str(cwd), model=model, prompt_len=len(prompt))

        return await _spawn_agent_process(args, cwd, env, output_dir, run_label)

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
            except (ArithmeticError, ValueError, TypeError):
                log.debug("runtime.cost_parse_failed", raw=data.get("total_cost_usd"), exc_info=True)
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
        fallback_model: str | None = None,
        max_budget_usd: Decimal | None = None,
        output_dir: Path | None = None,
        run_label: str | None = None,
    ) -> AgentProcess | FileAgentProcess:
        transformed = self.transform_prompt(prompt)

        # If the prompt was a sova CLI command, execute it directly
        # instead of passing to Aider (which cannot run shell commands).
        if transformed != prompt and transformed.startswith("sova "):
            log.info("aider.exec_sova_cmd", cwd=str(cwd), cmd=transformed)
            import shlex as _shlex

            return await _spawn_agent_process(_shlex.split(transformed), cwd, env, output_dir, run_label)

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

        return await _spawn_agent_process(args, cwd, env, output_dir, run_label)

    def parse_output(self, line: str) -> StreamEvent | None:
        stripped = line.strip()
        if not stripped:
            return None
        return StreamEvent(type="content", text=stripped)

    async def check_available(self) -> tuple[bool, str]:
        return await _check_cli_available("aider", "pip install aider-chat")


# codex login status is a local keychain/credential-store read, matching the
# rationale for _AUTH_CHECK_TIMEOUT in the Claude Code provider.
_CODEX_AUTH_PROBE_TIMEOUT = _VERSION_CHECK_TIMEOUT

_AUTH_UNKNOWN_DETAIL = "auth state unknown"


def _resolve_codex_api_key(env: Mapping[str, str] | None = None) -> str | None:
    """Read ``CODEX_API_KEY`` from ``env``, falling back to the process environment.

    ``env`` is the caller-supplied child environment (``spawn(env=...)``), so a
    caller that curated its own environment gets its own key rather than the
    server's: a per-project key must not be silently replaced by whatever
    happens to be in ``os.environ``. ``None`` means "no curated env", which is
    the probe path and the default spawn path.

    A blank or whitespace-only value is treated as absent: SOVA must not inject
    or report an empty credential as present.
    """
    source = os.environ if env is None else env
    value = source.get("CODEX_API_KEY", "").strip()
    return value or None


def _codex_extra_env(env: Mapping[str, str] | None = None) -> dict[str, str] | None:
    """Build the ``extra_env`` override that re-admits ``CODEX_API_KEY``.

    ``CODEX_API_KEY`` is in ``SCRUBBED_VARS`` by default (see
    ``sova.utils.env``), so it is absent from the ``env`` a ``CodexRuntime``
    spawn would otherwise receive. This re-injects it after scrubbing, scoped
    to this one spawn/probe: no other runtime calls this function, so the key
    never reaches a Claude Code or Aider child.
    """
    api_key = _resolve_codex_api_key(env)
    return {"CODEX_API_KEY": api_key} if api_key else None


async def _probe_codex_auth() -> str | None:
    """Run ``codex login status`` once and return its combined output.

    A local credential-store read only (never a model request or any other
    billable API call). Uses the same environment a real Codex spawn would see
    (shared scrub, cross-provider credentials removed, ``CODEX_API_KEY``
    re-injected if set) so the probe's answer matches what agents actually
    experience.

    Returns ``None`` on subprocess spawn failure or timeout, mirroring
    ``_probe_auth()`` in the Claude Code provider (which runs the equivalent
    ``claude auth status`` probe through the same ``shell.run()`` helper).
    ``codex login status`` has no documented machine-readable output contract,
    so stdout and stderr are returned as one blob for the caller to interpret
    defensively rather than assumed to be JSON. The exit code is deliberately
    not returned: its meaning is undocumented too, so only the text is worth
    reading.
    """
    probe_env = _inject_agent_marker(None, extra_scrub=ANTHROPIC_CREDENTIAL_VARS, extra_env=_codex_extra_env())
    try:
        result = await run("codex", "login", "status", env=probe_env, timeout=_CODEX_AUTH_PROBE_TIMEOUT)
    except OSError:
        log.debug("codex.auth_probe_failed", exc_info=True)
        return None
    if result.timed_out:
        return None
    return f"{result.stdout}\n{result.stderr}".strip()


def _interpret_codex_auth_probe(text: str | None) -> tuple[bool | None, str]:
    """Interpret ``codex login status`` output.

    Returns ``(authenticated, detail)``:

    - ``True``: the CLI reports an authenticated session.
    - ``False``: the CLI reports it is logged out.
    - ``None``: the output could not be confidently interpreted (unknown CLI
      build, unexpected format, probe never ran). Callers fail open on this
      case, matching ``_interpret_auth_probe()`` in the Claude Code provider:
      a CLI build variation must never block agent spawning.

    ``codex login status`` has no documented machine-readable contract, so
    text is scanned defensively rather than assumed to be JSON. A JSON
    payload is still attempted first in case a future CLI version emits one;
    a bare scalar (valid JSON, but not an object, the same trap documented
    for ``_parse_log_file()``) carries no fields to read and falls through to
    the plain-text scan below, matching ``_extract_auth_detail()`` in
    ``doctor.py`` for ``gh auth status``.
    """
    if not text:
        return None, _AUTH_UNKNOWN_DETAIL

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        for key in ("loggedIn", "logged_in", "authenticated"):
            # Strict identity, not truthiness: a null or string value is a
            # shape this code does not understand, and must fall through to
            # "unknown" (fail open) rather than be read as a definite logout.
            if data.get(key) is True:
                return True, "authenticated"
            if data.get(key) is False:
                return False, "not authenticated (run: codex login)"
        # A dict this code could not read is unknown, full stop. Falling back
        # to the phrase scan here would scan the JSON source itself, where a
        # key name ("authenticated") matches regardless of its value.
        return None, _AUTH_UNKNOWN_DETAIL

    lowered = text.lower()
    if any(phrase in lowered for phrase in ("not logged in", "not authenticated", "logged out", "no credentials")):
        return False, "not authenticated (run: codex login)"
    if any(phrase in lowered for phrase in ("logged in", "authenticated")):
        return True, "authenticated"

    return None, _AUTH_UNKNOWN_DETAIL


class CodexRuntime(AgentRuntime):
    """Runtime that spawns Codex CLI processes.

    Codex (https://developers.openai.com/codex/) is OpenAI's coding agent
    CLI. Invoked non-interactively via ``codex exec`` with JSON Lines
    output and an explicit ``workspace-write`` sandbox.

    Credential handling: local setup should rely on Codex's own keyring
    storage (``codex login`` / ``cli_auth_credentials_store = "keyring"``);
    SOVA never reads, copies, or persists that credential. An optional
    ``CODEX_API_KEY`` in SOVA's own process environment is forwarded only to
    the Codex child that needs it (see ``_codex_extra_env()``), and is
    scrubbed from every other spawned runtime by default (``SCRUBBED_VARS``
    in ``sova.utils.env``). ``check_available()`` uses ``codex login status``
    to distinguish missing CLI, logged-out CLI, and authenticated CLI without
    ever making a model request.

    Parity gaps tracked by epic #940, not yet closed here: the prompt does
    not carry ``_HEADLESS_PREAMBLE`` (which is written for Claude Code and
    references its ``/compact`` command), so SOVA's pipeline-boundary
    guardrails are absent, and ``parse_output()`` returns each JSONL line
    verbatim instead of mapping it to content and result events.

    Model and sandbox policy come from ``CodexConfig`` (``[codex]`` in
    ``sova.toml``), passed in at construction via ``create_runtime(codex=...)``.
    The caller-supplied ``model`` argument to ``spawn()`` is a Claude model id
    resolved from ``agent.model`` and is never forwarded to ``codex exec``.
    """

    def __init__(self, config: CodexConfig | None = None) -> None:
        self._config = config if config is not None else CodexConfig()

    @property
    def name(self) -> str:
        return "codex"

    async def spawn(
        self,
        prompt: str,
        cwd: str | Path,
        *,
        env: dict[str, str] | None = None,
        model: str | None = None,
        fallback_model: str | None = None,
        max_budget_usd: Decimal | None = None,
        output_dir: Path | None = None,
        run_label: str | None = None,
    ) -> AgentProcess | FileAgentProcess:
        if fallback_model is not None:
            log.warning(
                "codex.fallback_model_not_supported",
                fallback_model=fallback_model,
                hint="Codex CLI does not support a fallback model; the input is ignored",
            )

        if max_budget_usd is not None:
            log.warning(
                "codex.budget_not_enforced",
                budget=str(max_budget_usd),
                hint="Codex CLI does not support budget caps; cost is not limited",
            )

        args: list[str] = ["codex", "exec", "--json", "--sandbox", self._config.sandbox]

        codex_model = self._config.model
        if codex_model:
            args.extend(["--model", codex_model])

        # "--" is required: codex exec takes PROMPT as a positional argument
        # (clap-based parser), so a prompt starting with "-" would otherwise
        # be misread as an unrecognized option.
        args.extend(["--", prompt])

        log.info("codex.spawn", cwd=str(cwd), model=codex_model, prompt_len=len(prompt))

        return await _spawn_agent_process(
            args,
            cwd,
            env,
            output_dir,
            run_label,
            extra_scrub=ANTHROPIC_CREDENTIAL_VARS,
            extra_env=_codex_extra_env(env),
        )

    def parse_output(self, line: str) -> StreamEvent | None:
        stripped = line.strip()
        if not stripped:
            return None
        return StreamEvent(type="content", text=stripped)

    async def check_available(self) -> tuple[bool, str]:
        available, version_detail = await _check_cli_available("codex", "npm install -g @openai/codex")
        if not available:
            return False, version_detail

        probe = await _probe_codex_auth()
        authenticated, auth_detail = _interpret_codex_auth_probe(probe)
        if _resolve_codex_api_key() is not None:
            auth_detail = f"{auth_detail}, CODEX_API_KEY set"

        if authenticated is False:
            return False, f"{version_detail} but {auth_detail}"
        return True, f"{version_detail} ({auth_detail})"


# ---------------------------------------------------------------------------
# Direct subprocess spawn (bypasses Claude Code intermediary)
# ---------------------------------------------------------------------------

_PIPELINE_ROLES = frozenset({"developer", "researcher", "planner"})


async def spawn_direct(
    cmd_parts: list[str],
    cwd: str | Path,
    *,
    env: dict[str, str] | None = None,
    output_dir: Path | None = None,
    run_label: str | None = None,
) -> AgentProcess | FileAgentProcess:
    """Spawn a CLI command directly as a subprocess (approved ``shell.py`` exception).

    Used for pipeline roles (developer, researcher, planner) where the
    dashboard previously spawned Claude Code as an intermediary just to
    run ``sova run``. Direct spawning eliminates the 600s Bash tool
    timeout and saves the ~$0.50 wrapper agent cost.

    This intentionally bypasses ``sova/utils/shell.py`` because it returns
    a live process handle for streaming output, which the shared runner's
    fire-and-wait model cannot support. Timeout and lifecycle management
    are handled by ``_wait_and_finalize()`` in ``agent_lifecycle.py``.
    """
    log.info("process.spawn_direct", cwd=str(cwd), cmd=cmd_parts[0:3])

    return await _spawn_agent_process(cmd_parts, cwd, env, output_dir, run_label)


# ---------------------------------------------------------------------------
# File-based output helper
# ---------------------------------------------------------------------------


async def _spawn_with_file_output(
    args: list[str],
    cwd: str | Path,
    env: dict[str, str] | None,
    output_dir: Path,
    run_label: str,
) -> FileAgentProcess:
    """Spawn a subprocess with stdout/stderr redirected to files.

    Creates ``{output_dir}/{run_label}.stdout`` and ``.stderr``, opens them
    for writing, and passes the file descriptors to the subprocess. Returns
    a ``FileAgentProcess`` that tails these files for streaming.
    """
    if not run_label:
        raise ValueError("run_label is required when output_dir is set")
    stdout_path = output_dir / f"{run_label}.stdout"
    stderr_path = output_dir / f"{run_label}.stderr"

    with open(stdout_path, "wb") as stdout_fh, open(stderr_path, "wb") as stderr_fh:  # NOSONAR
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=stdout_fh,
            stderr=stderr_fh,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )

    return FileAgentProcess(proc, stdout_path=stdout_path, stderr_path=stderr_path)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_RUNTIMES: dict[str, type[AgentRuntime]] = {
    "claude-code": ClaudeCodeRuntime,
    "aider": AiderRuntime,
    "codex": CodexRuntime,
}


def create_runtime(runtime_type: str = "claude-code", *, codex: CodexConfig | None = None) -> AgentRuntime:
    """Create an AgentRuntime instance by type name.

    Args:
        runtime_type: Runtime identifier (e.g., "claude-code", "aider", "codex").
        codex: Codex-specific config, forwarded to ``CodexRuntime`` when
            ``runtime_type == "codex"``. Ignored for every other runtime type.

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

    if cls is CodexRuntime:
        return CodexRuntime(config=codex)
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


def reload_runtime(cfg: ProjectConfig) -> None:
    """Recreate the global agent runtime from fresh config.

    Python's GIL ensures the reference swap is atomic. In-flight spawns
    hold their own reference to the old runtime via the returned process.
    """
    set_runtime(create_runtime(cfg.agent.runtime, codex=cfg.codex))
