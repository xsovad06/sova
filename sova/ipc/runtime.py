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
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from sova.config.models import RUNTIME_MODEL_PIN_ROLES, CodexConfig, ProjectConfig
from sova.ipc.control import AgentProcess, FileAgentProcess
from sova.llm.cli_args import build_claude_cli_args
from sova.llm.errors import ComponentMisconfiguredError
from sova.llm.models import CURATED_MODELS, StreamEvent, build_claude_cli_result
from sova.utils.env import (
    configured_passthrough,
    scrub_agent_env,
)
from sova.utils.logging import get_logger
from sova.utils.shell import write_stdin_and_close

if TYPE_CHECKING:
    from sova.ipc.codex_runtime import CodexRuntime

log = get_logger(component="ipc.runtime")


_VERSION_CHECK_TIMEOUT = 5.0
_SUBPROCESS_LINE_LIMIT = 10 * 1024 * 1024  # 10 MB: agent JSON lines can exceed 64 KB default

# Provider-neutral guardrails shared by every headless runtime: pipeline
# boundary, literal command execution, worktree conflict recovery, and the
# no-background-waiting contract. None of this text assumes a specific CLI.
_HEADLESS_PREAMBLE_CORE = (
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
    "```bash``` code block with a CLI command, "
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
)

# Claude Code-specific: references /compact, a Claude Code CLI command with
# no equivalent in other runtimes. Only ClaudeCodeRuntime uses this.
_HEADLESS_PREAMBLE_CLAUDE_TAIL = (
    "CONTEXT MANAGEMENT: Monitor your context window usage. When "
    "context grows large (after reading many files or long outputs), "
    "use /compact proactively to free space. Prefer reading specific "
    "file sections (line ranges) over entire files. Summarize long "
    "command outputs before continuing.\n\n"
)

_HEADLESS_PREAMBLE_EXECUTE_SUFFIX = "Execute the following instruction exactly as specified:\n\n"

# Byte-identical to the pre-split constant: ClaudeCodeRuntime.spawn() must
# see no behavioral change from this split.
_HEADLESS_PREAMBLE = _HEADLESS_PREAMBLE_CORE + _HEADLESS_PREAMBLE_CLAUDE_TAIL + _HEADLESS_PREAMBLE_EXECUTE_SUFFIX

# Intra-package API for sova.ipc.codex_runtime (underscore-prefixed on purpose, not public):
# _HEADLESS_PREAMBLE_CORE, _HEADLESS_PREAMBLE_EXECUTE_SUFFIX, _VERSION_CHECK_TIMEOUT, _check_cli_available,
# _inject_agent_marker and _spawn_agent_process. Rename or change one and update codex_runtime.py in the same
# change. The Codex preamble itself (core plus suffix, no Claude-specific tail) lives in codex_runtime.py.


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
        return False, f"{cli_name} not found, install: {install_hint}"
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
    stdin_payload: str | None = None,
) -> AgentProcess | FileAgentProcess:
    """Spawn ``args`` in the scrubbed agent environment.

    Redirects stdout/stderr to files when ``output_dir`` is set, otherwise
    pipes them for streaming. Every runtime spawn path funnels through here,
    so ``start_new_session=True`` (see the subprocess isolation note in
    ``.claude/rules/architecture.md``) cannot be omitted by one of them.

    ``extra_scrub``/``extra_env`` are per-spawn overrides layered on top of
    the shared scrub; see ``_inject_agent_marker()``.

    ``stdin_payload``, when set, is written to the child's stdin and the
    pipe is then closed (EOF), instead of the default of inheriting this
    process's stdin. ``ClaudeCodeRuntime.spawn()`` always sets it (the
    prompt, since the Claude CLI reads it from stdin rather than argv);
    ``CodexRuntime.spawn()`` sets it only when
    ``_probe_codex_stdin_prompt_support()`` confirms the installed CLI reads
    an omitted PROMPT from stdin, and otherwise leaves it ``None`` to keep
    the prompt on argv. Every other caller leaves it ``None`` and keeps
    inheriting stdin unchanged.
    """
    agent_env = _inject_agent_marker(env, extra_scrub=extra_scrub, extra_env=extra_env)

    if output_dir is not None:
        return await _spawn_with_file_output(args, cwd, agent_env, output_dir, run_label, stdin_payload=stdin_payload)

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if stdin_payload is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=agent_env,
        limit=_SUBPROCESS_LINE_LIMIT,
        start_new_session=True,
    )
    if stdin_payload is not None:
        await write_stdin_and_close(proc, stdin_payload)
    return AgentProcess(proc)


# Matches _ENUMERATION_TTL_SECONDS in sova/llm/client.py: a runtime's model
# catalog is a stable positive fact for the life of a deployment, not
# something that needs reactive-TTL treatment the way a negative probe does.
_RUNTIME_MODEL_CACHE_TTL = 1800.0

# Matches _UNAVAILABLE_TTL_SECONDS in sova/llm/client.py, and for the same
# reason: "unavailable" is a reactive negative, not a stable fact about the
# deployment, so it has to expire fast enough that a `claude login` or a CLI
# install done mid-session is picked up without restarting the process.
# There is no clear_enumeration() equivalent for this per-instance cache, so
# a negative pinned for the positive TTL would be unfixable for half an hour.
_RUNTIME_MODEL_UNAVAILABLE_TTL = 300.0


class _RuntimeModelCache:
    """Per-instance, single-flighted cache for ``AgentRuntime.list_available_models()``.

    Mirrors ``sova.llm.client.cached_enumeration()``'s check-cache /
    take-lock / re-check / compute / store sequence, scoped to one runtime
    instance rather than a module-level identity map: every ``AgentRuntime``
    concrete class is already a process-wide singleton via ``get_runtime()``,
    so caching on ``self`` gives the same effect as keying a shared cache by
    runtime identity, without introducing a second global registry.

    Caches ``None`` (unavailable) too, not just a real answer, so a missing
    or unauthenticated CLI is not re-probed on every call, but only for the
    much shorter ``_RUNTIME_MODEL_UNAVAILABLE_TTL``: pinning it for the full
    positive TTL would reproduce exactly what ``cached_enumeration()``
    refuses to do when its own ``compute`` returns ``None`` (serve the
    fallback without caching it, so the next call retries).
    """

    def __init__(self) -> None:
        # (expiry_monotonic, value); the expiry is stamped after compute()
        # returns, so a slow probe does not eat into its own TTL.
        self._entry: tuple[float, list[str] | None] | None = None
        self._lock = asyncio.Lock()

    def peek(self) -> list[str] | None:
        """Return the live cached catalog, or ``None`` for a miss, an expired entry, or a cached unavailable answer.

        The read-only, never-computing view ``AgentRuntime.cached_models()``
        needs. Public so that caller does not reach into ``_cached()``: a
        ``# noqa`` for the private access there was itself rejected by
        SonarCloud's suppression-comment syntax rule (S7632).
        """
        _hit, value = self._cached()
        return value

    def _cached(self) -> tuple[bool, list[str] | None]:
        """Return ``(hit, value)``, dropping and reporting a miss for an expired entry."""
        entry = self._entry
        if entry is None:
            return False, None
        expiry, value = entry
        if time.monotonic() >= expiry:
            self._entry = None
            return False, None
        return True, list(value) if value is not None else None

    async def get_or_compute(self, compute: Callable[[], Awaitable[list[str] | None]]) -> list[str] | None:
        hit, value = self._cached()
        if hit:
            return value

        async with self._lock:
            hit, value = self._cached()
            if hit:
                return value

            computed = await compute()
            ttl = _RUNTIME_MODEL_CACHE_TTL if computed is not None else _RUNTIME_MODEL_UNAVAILABLE_TTL
            self._entry = (time.monotonic() + ttl, computed)
            return list(computed) if computed is not None else None


class StreamParser(Protocol):
    """A stateful parser scoped to one spawned process's output stream.

    Matches ``CodexStreamParser.parse_line``'s shape exactly, so that class
    satisfies this protocol without any adapter. A runtime whose output
    format needs per-process state (a thread/session id, a terminal-event
    latch) implements ``AgentRuntime.create_stream_parser()`` to hand the
    caller a fresh instance instead of parsing through a shared one.
    """

    def parse_line(self, line: str) -> StreamEvent | None: ...


class AgentRuntime(ABC):
    """Abstract interface for coding agent backends.

    Each runtime knows how to spawn a coding agent process that reads files,
    edits code, runs tests, and creates commits. Different from the LLM
    provider layer (which handles prompt -> response), this is a full
    autonomous coding agent.
    """

    def __init__(self) -> None:
        self._model_cache: _RuntimeModelCache | None = None

    def _models_cache(self) -> _RuntimeModelCache:
        """Return this instance's model cache, creating it on first use.

        Lazy rather than relying on ``__init__`` alone: this ABC had no
        ``__init__`` before model enumeration existed, so a subclass that
        defines its own without calling ``super().__init__()`` (every
        in-repo one had to be amended for exactly this, and ``set_runtime()``
        accepts out-of-tree runtimes) would otherwise raise
        ``AttributeError`` the first time anything asked it for models.
        """
        cache = getattr(self, "_model_cache", None)
        if cache is None:
            cache = _RuntimeModelCache()
            self._model_cache = cache
        return cache

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable runtime name (e.g., 'claude-code', 'aider', 'codex')."""
        ...

    @property
    def enforces_budget_cap(self) -> bool:
        """Whether this runtime's CLI enforces ``spawn(max_budget_usd=...)`` itself.

        The runtime-side counterpart of
        ``ProviderCapabilities.supports_budget_cap``: a caller that passes a
        remaining-budget figure (every pipeline step passes ``max_budget -
        ctx.cost_usd``) needs to know whether the spawned process will honour
        it or merely log that it cannot. Defaults to False, the conservative
        answer, so a new runtime is treated as unable to cap until it proves
        otherwise; ``sova.core.agent_dispatch`` refuses to spawn such a
        runtime once the remaining budget is non-positive, which is the only
        enforcement point left for it.

        ``CodexRuntime`` layers a second, stricter gate on top of this one
        rather than through it: it rejects *any* non-``None``
        ``max_budget_usd`` (not just an already-exhausted one) unless
        ``codex.allow_unenforced_budget`` is explicitly set, inside its own
        ``spawn()``. That extra gate and this property's False default must
        not drift apart: if a future runtime needs the same "opt in or
        refuse" behavior, prefer adding it here (and to
        ``_raise_if_budget_cap_unsupported()``) rather than duplicating
        another one-off check inside that runtime's own ``spawn()``.
        """
        return False

    @property
    def enforces_read_only(self) -> bool:
        """Whether ``spawn(read_only=True)`` is enforced by the runtime rather than only the prompt.

        Defaults to False: Claude Code and Aider treat the flag as advisory. ``CodexRuntime``
        overrides it to True because its sandbox denies writes to the whole process tree.
        ``dispatch_runtime_prompt()`` warns when a read-only request lands on a runtime without it.
        """
        return False

    @property
    def honours_model_override(self) -> bool:
        """Whether ``spawn(model=...)`` actually selects the model used.

        Defaults to True. ``CodexRuntime`` overrides this to False: its model
        comes from ``CodexConfig`` alone and the caller-supplied ``model``
        argument is never forwarded to ``codex exec`` (see its class
        docstring). ``sova.core.agent_dispatch`` uses this to skip walking
        ``agent.fallback_models`` for such a runtime: every hop would spawn a
        byte-identical agent invocation for a model that never changes,
        paying a full agent run per candidate for no benefit.
        """
        return True

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
        read_only: bool = False,
        network_access: bool = False,
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
            read_only: When True, the runtime must prevent the process from
                modifying the working directory, if it has a mechanism to do
                so. A runtime with no such mechanism (ClaudeCodeRuntime,
                AiderRuntime today) accepts the flag and no-ops: the caller's
                prompt-level guardrails are the only enforcement for those.
                CodexRuntime maps this onto ``codex exec --sandbox
                read-only``, which the process cannot be talked out of.

                Caller beware: a sandbox is blunter than "do not edit source
                files". Codex's ``read-only`` profile denies every write and
                disables network access for the whole process tree, so a
                prompt that runs ``sova run`` under it cannot write
                ``.claude/sova.db``, write a handoff file, or reach the
                GitHub API. Pass True only for a prompt that genuinely needs
                nothing but reads.

                No production caller passes this today: the reviewer role
                (the one role that used to request it) now runs as a plain
                trusted subprocess via ``spawn_direct()``, outside any
                ``AgentRuntime`` entirely. The parameter and its three
                implementations are kept available for a future non-pipeline
                role that needs a sandboxed read-only spawn; there is
                currently no live enforcement path exercising it outside
                tests.
            network_access: Whether this run needs external services. Codex
                enables network within its workspace-write sandbox only for
                these runs; pipeline roles leave it disabled by default.

        Returns:
            An AgentProcess (pipe-based) or FileAgentProcess (file-based).
        """
        ...

    @abstractmethod
    def parse_output(self, line: str) -> StreamEvent | None:
        """Parse a line of agent stdout into a StreamEvent.

        Returns None for any line that carries nothing worth surfacing:
        always for empty/whitespace lines, and for whatever else a given
        runtime treats as internal (Codex, for instance, drops reasoning
        items, no-op lifecycle events and duplicate terminal events).
        Callers must skip a None rather than read it as end-of-stream.
        """
        ...

    def create_stream_parser(self) -> StreamParser | None:
        """Create a fresh stateful parser scoped to one spawned process.

        The caller (the dashboard's output reader) invokes this once per
        spawn and keeps the returned parser on that process's own state,
        never sharing it with another concurrent agent. Returning ``None``
        (the default) means this runtime's output format carries no
        per-process state, so a caller falls back to ``parse_output()``
        (or its own inline parsing) for every runtime that does not
        override this.
        """
        return None

    def stream_reader_drain_timeout(self) -> float | None:
        """Seconds to wait for the output reader to drain a per-process parser.

        Only meaningful for a runtime that overrides ``create_stream_parser()``:
        such a runtime's terminal failure may arrive as a stream event rather
        than an exit code, so the caller needs to know how long to wait for
        that event to be read before giving up. Returning ``None`` (the
        default) means this runtime has no tuning of its own and the caller
        should fall back to its own default. Resolved once at spawn time and
        cached on the caller's process state, not re-read on every finalize.
        """
        return None

    def transform_prompt(self, prompt: str) -> str:
        """Rewrite a caller-supplied prompt into the form this runtime needs.

        The default implementation returns the prompt unchanged. The two
        current overrides use it for unrelated purposes: ``AiderRuntime``
        detects a shell-command-formatted prompt and extracts the bare
        command (Aider cannot execute shell commands), while ``CodexRuntime``
        prepends its provider-neutral headless guardrail preamble.

        Each runtime's own ``spawn()`` is the only caller: a prompt reaching
        ``spawn()`` is always the untransformed one. Never call this and then
        pass the result to ``spawn()``, or a transformation that prepends
        (``CodexRuntime``) is applied twice.
        """
        return prompt

    @abstractmethod
    async def check_available(self) -> tuple[bool, str]:
        """Check if this runtime's CLI tool is installed.

        Returns:
            Tuple of (available, detail_message).
        """
        ...

    async def list_available_models(self) -> list[str] | None:
        """Return the model IDs this runtime's CLI can reach, or ``None`` if unavailable.

        Layer-1 (runtime) counterpart of ``LLMProvider.list_available_models()``
        (layer-2): ``None`` is the "unavailable" sentinel (CLI not installed,
        not authenticated, or probe failure), kept distinct from ``[]``
        (available, but this runtime knows of zero reachable models), the
        same shape ``get_model_capabilities()`` in ``sova.llm.provider``
        composes (not to be confused with ``LLMProvider.capabilities``, the
        unrelated budget/cost capability declaration).

        Cached per instance and single-flighted (``_RuntimeModelCache``), so
        a caller polling ``get_model_capabilities()`` repeatedly, or multiple
        concurrent callers, never re-run the underlying probe more than once
        per TTL window: ``_RUNTIME_MODEL_CACHE_TTL`` for a real answer, the
        much shorter ``_RUNTIME_MODEL_UNAVAILABLE_TTL`` for ``None``. Never
        raises: subclasses override ``_probe_models()``, not this method, and
        must keep that same fail-open contract.
        """
        return await self._models_cache().get_or_compute(self._enumerate_models)

    def cached_models(self) -> list[str] | None:
        """Return this runtime's model catalog without probing, or ``None`` if unknown.

        Unlike ``list_available_models()``, never calls ``_enumerate_models()``:
        a caller that must not pay a probe's cost on this path
        (``sova.core.agent_dispatch``'s task-type model compatibility check,
        see ``tests/test_ipc.py::TestNoModelProbeOnSpawnHotPath``) gets the
        warm cache entry when there is one, else whatever the runtime can
        state statically (``declared_models()``), else ``None`` so the caller
        fails open. A confirmed-unavailable cache entry (``None``) falls
        through to the static declaration on purpose, rather than reporting
        "no catalog" until that negative entry expires: the question this
        check asks is which model dialect the runtime's CLI parses, which an
        uninstalled or unauthenticated CLI does not change. An empty list (a
        reachable runtime with no catalog of its own) is returned as-is,
        since that is a confirmed answer and the caller fails open on it too.
        """
        cached = self._models_cache().peek()
        return cached if cached is not None else self.declared_models()

    def declared_models(self) -> list[str] | None:
        """Return the model IDs this runtime's CLI accepts, known without any probe.

        The fallback behind ``cached_models()``, and the only source of a
        catalog inside a ``sova run`` pipeline process: nothing there ever
        calls ``list_available_models()`` (the dashboard's own enumeration
        builds a separate project-scoped instance via ``create_runtime()``),
        so the per-instance cache is permanently cold and a compatibility
        check keyed to it alone would never fire.

        ``None`` (the default) means this runtime declares nothing statically,
        so such a check must fail open. Only a runtime whose accepted model
        dialect is a fixed, locally-known set overrides this; it is a
        statement about dialect, not about availability, so it stays correct
        for an uninstalled or unauthenticated CLI (which ``check_available()``
        and the spawn itself report) and needs no I/O.
        """
        return None

    async def _enumerate_models(self) -> list[str] | None:
        """Apply the availability gate once for every runtime, then delegate to ``_probe_models()``.

        Kept here rather than in each ``_probe_models()`` override so no
        subclass has to repeat (or can forget) the "CLI missing or
        unauthenticated means ``None``" half of the contract.

        Also the one place the "never raises" half is actually enforced:
        ``check_available()`` has no such contract of its own (it shells out
        to a CLI), and an override that lets a probe error escape would
        otherwise break every caller of ``list_available_models()``.
        """
        try:
            available, _ = await self.check_available()
            if not available:
                return None
            return await self._probe_models()
        except Exception:  # noqa: BLE001 (any probe failure means "unavailable", never a raise)
            log.debug("runtime.model_enumeration_failed", runtime=self.name, exc_info=True)
            return None

    async def _probe_models(self) -> list[str] | None:
        """Return this runtime's own model catalog, its CLI already known to be usable.

        Called only once ``_enumerate_models()``'s availability gate passes.
        Default implementation: available, but with no catalog of its own
        (``[]``). Overrides must never raise; any probe exception, timeout,
        or auth failure must be caught and translated to ``None``.
        """
        return []


def _claude_result_stop_reason(data: dict) -> str:
    """Return ``"error"`` when a Claude CLI result event reports a failed turn.

    The CLI marks a failed turn with ``is_error: true`` and a ``subtype`` of
    ``error_during_execution``/``error_max_turns``; a successful one carries
    ``subtype: "success"``. A missing subtype is not treated as a failure:
    older/partial payloads omit it, and inventing an error from an absent field
    would fail a turn that actually succeeded.
    """
    subtype = str(data.get("subtype", "") or "")
    if data.get("is_error") or (subtype and subtype != "success"):
        return "error"
    return ""


class ClaudeCodeRuntime(AgentRuntime):
    """Runtime that spawns Claude Code CLI processes."""

    @property
    def name(self) -> str:
        return "claude-code"

    @property
    def enforces_budget_cap(self) -> bool:
        # spawn() passes max_budget_usd to the CLI's own --max-budget-usd.
        return True

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
        read_only: bool = False,
        network_access: bool = False,
    ) -> AgentProcess | FileAgentProcess:
        # Claude Code has no sandboxed read-only mode; read_only is advisory
        # only (the caller's prompt is the sole enforcement).
        args = build_claude_cli_args(
            model=model,
            fallback_model=fallback_model,
            max_budget_usd=max_budget_usd,
            output_format="stream-json",
        )

        log.info("process.spawn", cwd=str(cwd), model=model, prompt_len=len(prompt))

        return await _spawn_agent_process(
            args,
            cwd,
            env,
            output_dir,
            run_label,
            stdin_payload=_HEADLESS_PREAMBLE + prompt,
        )

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
            # build_claude_cli_result() is the shared parser with
            # _parse_result() in sova/llm/providers/claude_code.py, so the
            # two field-name sets (cache_read_input_tokens, modelUsage) and
            # cost_source cannot drift apart again. stop_reason is computed
            # here, not by the shared helper: a terminal result event carries
            # the turn's own verdict (``is_error``, or a ``subtype`` other
            # than "success"), and mapping it onto stop_reason="error" is
            # what makes the failure visible to a caller holding only the
            # LLMResult (``LLMResult.is_error``, which the Codex parser
            # already sets the same way); dropping it made a failed turn
            # indistinguishable from a successful one.
            llm_result = build_claude_cli_result(data, stop_reason=_claude_result_stop_reason(data))
            return StreamEvent(type="result", text=result_text, result=llm_result)

        return None

    async def check_available(self) -> tuple[bool, str]:
        return await _check_cli_available("claude", "https://docs.anthropic.com/en/docs/claude-code")

    async def _probe_models(self) -> list[str] | None:
        """Report the curated Anthropic model IDs.

        Reuses ``CURATED_MODELS`` (the same static list layer-2's
        ``LLMProvider`` default returns) rather than hand-rolling a second
        catalog: Claude Code runs whatever Anthropic model it is passed, so
        the reachable set is the same one SOVA already curates.
        """
        return self.declared_models()

    def declared_models(self) -> list[str]:
        """The curated Anthropic IDs, the one catalog knowable without a probe.

        ``claude --model`` accepts exactly this dialect whether or not the CLI
        is installed or authenticated, which is what lets the task-type
        compatibility check in ``sova.core.agent_dispatch`` work inside a
        pipeline process with a cold enumeration cache.
        """
        return [m.id for m in CURATED_MODELS]


class AiderRuntime(AgentRuntime):
    """Runtime that spawns Aider CLI processes.

    Aider (https://aider.chat) is an open-source AI pair programming tool.
    It supports multiple LLM backends and edits code via git commits.

    Deliberately keeps ``AgentRuntime._probe_models()``'s default (``[]``,
    "reachable, no catalog of its own") rather than overriding it: Aider's
    ``--model`` is an arbitrary litellm-style string and the CLI has no
    discovery endpoint, so there is nothing to enumerate. Delegating to the
    configured layer-2 provider instead would fuse the two axes that
    ``get_model_capabilities()`` exists to keep apart: the same catalog would
    be reported under both ``layer1`` and ``layer2``, and a layer-2
    credential failure would report layer 1 unavailable even though the
    Aider CLI itself is installed and usable. Issue #1038.
    """

    @property
    def name(self) -> str:
        return "aider"

    # Pattern matching shell-command prompts from start_agent() / start_command().
    # Format: "Run the following command...\n```bash\nsova run 28\n```"
    _SHELL_CMD_RE = re.compile(r"```(?:bash|sh)[^\S\n]*\n((?:[^`\n]*\n)+)```")

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
        read_only: bool = False,
        network_access: bool = False,
    ) -> AgentProcess | FileAgentProcess:
        # Aider has no sandboxed read-only mode; read_only is advisory only
        # (the caller's prompt is the sole enforcement).
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

    # No _probe_models() override: see the class docstring. Aider reports the
    # ABC default ([] when the CLI is installed, None when it is not), never a
    # borrowed layer-2 catalog.


def _openai_extra_env(env: Mapping[str, str] | None = None) -> dict[str, str] | None:
    """Build the ``extra_env`` override that re-admits ``OPENAI_API_KEY`` for ``spawn_direct()``.

    ``OPENAI_API_KEY`` is in ``SCRUBBED_VARS`` by default (see
    ``sova.utils.env``), so it is otherwise absent from every spawned child.
    ``spawn_direct()`` is the one caller that needs it back: it launches the
    ``sova run N`` subprocess for the developer/researcher/planner pipeline
    roles, and that subprocess's own ``WorkflowEngine`` may read
    ``OPENAI_API_KEY`` in-process (``sova/llm/litellm_provider.py``) when the
    project's ``llm.provider`` is an OpenAI-backed litellm model. Mirrors
    ``_codex_extra_env()``: scoped to this one call site, so
    ``ClaudeCodeRuntime``/``AiderRuntime``/``CodexRuntime`` spawns never
    receive it.
    """
    source = os.environ if env is None else env
    api_key = source.get("OPENAI_API_KEY", "").strip()
    return {"OPENAI_API_KEY": api_key} if api_key else None


# ---------------------------------------------------------------------------
# Direct subprocess spawn (bypasses Claude Code intermediary)
# ---------------------------------------------------------------------------
#
# Role launch-path classification (issue #946, revised by #1126), intentional,
# not incidental:
#   developer, researcher, planner -> spawn_direct(). Each runs `sova run` as
#       a plain subprocess; the WorkflowEngine inside that subprocess resolves
#       its own LLM provider calls, so wrapping it in an AgentRuntime would
#       add a redundant second agent process, a second bill, and reintroduce
#       the 600s-timeout class of failure spawn_direct() exists to eliminate.
#   reviewer -> also spawn_direct(), via a local extension of this set in
#       agent_lifecycle.py (``_DIRECT_SPAWN_ROLES``), not by adding it here.
#       The reviewer never needed interactive coding-agent tool-use (it only
#       makes a model call over a diff plus GitHub/DB writes), so routing it
#       through an AgentRuntime sandbox was a redundant wrapper, not a
#       protection it depended on. It is kept out of ``_PIPELINE_ROLES``
#       itself because this constant names the WorkflowEngine-driven
#       step-pipeline roles, a shape ReviewerRole bypasses entirely (it has
#       no WorkflowEngine and no step pipeline). Note that ``agent_db.py``
#       and ``work_service.py`` do NOT read this constant: each defines its
#       own independent local ``_PIPELINE_ROLES`` frozenset for its own
#       purpose (``agent_db.py``'s gates ``_validate_pipeline_outcome()``,
#       pipeline-bypass detection at run exit; ``work_service.py``'s gates
#       step-count display), so adding "reviewer" here would not have
#       affected either of them, and editing this set does not propagate
#       to theirs.
#   custom, command:* -> runtime.spawn(), via the configured AgentRuntime
#       (Claude Code, Aider, or Codex). These are not pipeline-driven:
#       custom-role runs and every dashboard-triggered slash command execute
#       inside a single autonomous agent session.
_PIPELINE_ROLES = frozenset({"developer", "researcher", "planner"})


async def spawn_direct(
    cmd_parts: list[str],
    cwd: str | Path,
    *,
    env: dict[str, str] | None = None,
    output_dir: Path | None = None,
    run_label: str | None = None,
    extra_env: Mapping[str, str] | None = None,
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

    ``extra_env`` re-admits variables the shared scrub strips by default
    (see ``_inject_agent_marker()``), e.g. ``_codex_extra_env()`` for the
    reviewer role's optional read-only Codex repo-context sub-call.
    ``OPENAI_API_KEY`` is always re-admitted on top of that (see
    ``_openai_extra_env()``): the spawned ``sova run N`` subprocess is the
    one trusted pipeline child whose own in-process litellm calls may need
    it, unlike the AgentRuntime spawns below which never receive it.
    """
    log.info("process.spawn_direct", cwd=str(cwd), cmd=cmd_parts[0:3])

    combined_extra_env = {**(_openai_extra_env(env) or {}), **(extra_env or {})}
    return await _spawn_agent_process(cmd_parts, cwd, env, output_dir, run_label, extra_env=combined_extra_env or None)


# ---------------------------------------------------------------------------
# File-based output helper
# ---------------------------------------------------------------------------


async def _spawn_with_file_output(
    args: list[str],
    cwd: str | Path,
    env: dict[str, str] | None,
    output_dir: Path,
    run_label: str,
    *,
    stdin_payload: str | None = None,
) -> FileAgentProcess:
    """Spawn a subprocess with stdout/stderr redirected to files.

    Creates ``{output_dir}/{run_label}.stdout`` and ``.stderr``, opens them
    for writing, and passes the file descriptors to the subprocess. Returns
    a ``FileAgentProcess`` that tails these files for streaming.

    ``stdin_payload``, when set, is written after the process is created and
    the stdout/stderr file descriptors are already closed (the ``with``
    block above only guards those two files; ``proc.stdin`` stays valid
    afterwards).
    """
    if not run_label:
        raise ValueError("run_label is required when output_dir is set")
    stdout_path = output_dir / f"{run_label}.stdout"
    stderr_path = output_dir / f"{run_label}.stderr"

    with open(stdout_path, "wb") as stdout_fh, open(stderr_path, "wb") as stderr_fh:  # NOSONAR
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if stdin_payload is not None else None,
            stdout=stdout_fh,
            stderr=stderr_fh,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )

    if stdin_payload is not None:
        await write_stdin_and_close(proc, stdin_payload)

    return FileAgentProcess(proc, stdout_path=stdout_path, stderr_path=stderr_path)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _load_codex_runtime() -> type[CodexRuntime]:
    # CodexRuntime lives in sova.ipc.codex_runtime, which imports this module, so a module-level
    # import here would be a cycle. The loader defers it until create_runtime() needs it.
    from sova.ipc.codex_runtime import CodexRuntime

    return CodexRuntime


# Every value is a factory taking the Codex config (ignored by every runtime but Codex), so the
# registry never holds a placeholder, still lists every available runtime, and needs no per-name branch.
_RUNTIMES: dict[str, Callable[[CodexConfig | None], AgentRuntime]] = {
    "claude-code": lambda _codex: ClaudeCodeRuntime(),
    "aider": lambda _codex: AiderRuntime(),
    "codex": lambda codex: _load_codex_runtime()(config=codex),
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

    factory = _RUNTIMES.get(runtime_type)
    if factory is None:
        available = ", ".join(sorted([*_RUNTIMES, "mock"]))
        raise ComponentMisconfiguredError(f"Unknown agent runtime: {runtime_type!r}. Available: {available}")

    return factory(codex)


# ---------------------------------------------------------------------------
# Runtime-model pin (issue #1039)
# ---------------------------------------------------------------------------

# The launch modal's per-run Model choice for a pipeline role. Like
# sova.llm.client.RUN_MODEL_OVERRIDE_ENV, the dashboard sets it only in the
# spawned `sova run` child's own env, never in config, so concurrent runs
# cannot see each other's choice and nothing is persisted.
RUN_RUNTIME_MODEL_OVERRIDE_ENV = "SOVA_RUNTIME_MODEL_OVERRIDE"


def resolve_runtime_model_pin(runtime: AgentRuntime, cfg: ProjectConfig | None, role: str) -> tuple[str, str] | None:
    """Return ``(model, reason)`` pinning *runtime*'s model for *role*, or None.

    The per-run override wins over ``roles.<role>_runtime_model``. Either one
    replaces the routed model for a tool-using step, and only for a role in
    ``RUNTIME_MODEL_PIN_ROLES``. None when neither is set
    or when *runtime* ignores a caller-supplied model (Codex): pinning it
    there would change nothing the agent runs, only what the step reports.
    """
    if not runtime.honours_model_override:
        return None
    override = os.environ.get(RUN_RUNTIME_MODEL_OVERRIDE_ENV, "").strip()
    if override and role in RUNTIME_MODEL_PIN_ROLES:
        return override, f"run override->{override}"
    pinned = cfg.roles.runtime_model_for(role) if cfg is not None else ""
    if pinned:
        return pinned, f"role:{role} runtime pin->{pinned}"
    return None


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
