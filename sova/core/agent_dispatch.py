"""Dispatch boundary between the text-only LLMProvider path and AgentRuntime.

A step that only needs a text response (triage, research, review) goes
through ``sova.llm.client.invoke_command()``, which calls the configured
``LLMProvider``. A step that needs to actually edit files
(``BaseStep.requires_tools = True``, e.g. ``DevelopStep``) must instead go
through the configured ``AgentRuntime`` (``sova.ipc.runtime.get_runtime()``):
some ``LLMProvider`` implementations (LiteLLM, the Anthropic API) are pure
text-completion backends with no mechanism to edit a working tree at all, so
routing a tool-using step to one of them does not fail, it just silently
produces a chat response with zero file changes, discovered only later by
``validate_output()``'s diff check (wasting the request in the meantime).

``dispatch_command()`` (for a slash command) and ``dispatch_prompt()`` (for a
step-assembled prompt) are the chokepoints every tool-using step must call
instead of ``invoke_command()``/``invoke()`` directly. Neither ever falls back
from AgentRuntime to LLMProvider: when the configured runtime's CLI is missing
or unauthenticated, they raise immediately rather than silently degrading to a
text-only call that cannot do the work (Design Decision 3 in issue #1125, the
no-implicit-fallback rule already established for model selection in
docs/model-selection-architecture.md).

Both tool paths run the same pre-flight the ``invoke*`` entry points run
(``sova.llm.client.prepare_invocation()``), so bypassing the LLMProvider does
not also bypass the runaway call guard, ``llm.routing`` task-type routing,
alias resolution, or the configured timeout default.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from sova.commands.catalog import parse_frontmatter
from sova.ipc.runtime import AgentRuntime, get_runtime
from sova.llm import client
from sova.llm.egress import scan_and_redact
from sova.llm.errors import LLMTimeoutError, classify_error
from sova.llm.models import LLMResult
from sova.llm.provider import _assert_command_exists
from sova.utils.files import read_text_or_none
from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.core.steps.base import BaseStep

log = get_logger(component="core.agent_dispatch")

# The Claude Code CLI's own argument placeholder, as it appears in a rendered
# command body under .claude/commands/. See _assemble_prompt().
_ARGUMENTS_PLACEHOLDER = "$ARGUMENTS"


class AgentRuntimeUnavailableError(RuntimeError):
    """Raised when a tool-using step has no usable AgentRuntime to dispatch to.

    Distinct from a generic RuntimeError so callers that want to tell this
    configuration failure apart from an in-flight agent error (a timeout, a
    bad exit code) can do so without string matching.
    """


def _resolve_command_body(command: str, cwd: Path) -> str:
    """Read the canonical markdown body for *command* out of ``cwd``.

    Strips YAML frontmatter when present, since that is metadata for command
    discovery/rendering, not instructions for the agent. A runtime with no
    slash-command concept of its own (Codex) needs the literal body text, not
    a bare ``"/develop"`` reference it cannot resolve on its own.

    Locating and validating the file is delegated to the same
    ``_assert_command_exists()`` the LLMProvider path uses, so the tool path
    gets its name validation and worktree ``.claude/`` restore attempt rather
    than a second copy of the ``.claude/commands/`` layout.
    """
    cmd_path = _assert_command_exists(command, cwd)
    content = read_text_or_none(cmd_path)
    if content is None:
        raise RuntimeError(f"Command file {cmd_path} exists but could not be read as UTF-8 text")
    parsed = parse_frontmatter(content)
    return parsed[1].strip() if parsed is not None else content.strip()


def _assemble_prompt(body: str, args: str) -> str:
    """Substitute *args* into *body* the way the Claude Code CLI would.

    A rendered command body carries the CLI's own ``$ARGUMENTS`` placeholder at
    the point the task belongs (``.claude/commands/develop.md`` puts it under
    "**Task to develop**:", and later steps refer back to it), and the CLI
    substitutes it when it expands a ``/command`` reference. The AgentRuntime
    path spawns the body as a literal prompt, so it has to do that
    substitution itself: appending the args instead would leave the agent
    reading a bare ``$ARGUMENTS`` where its task should be, and every
    instruction referring back to that placeholder would dangle.

    A body with no placeholder (not every command takes arguments inline)
    keeps the appended form, which is what the CLI's own prompt assembly
    produces for those.
    """
    if _ARGUMENTS_PLACEHOLDER in body:
        return body.replace(_ARGUMENTS_PLACEHOLDER, args).strip()
    return f"{body}\n\n{args}".strip() if args else body


async def _require_runtime(step: BaseStep) -> AgentRuntime:
    """Return the configured runtime, or raise if it cannot actually run.

    The no-implicit-fallback rule: a tool-using step whose runtime is missing
    or unauthenticated fails here, rather than degrading to an LLMProvider
    that has no way to edit files.
    """
    runtime = get_runtime()
    available, detail = await runtime.check_available()
    if not available:
        raise AgentRuntimeUnavailableError(
            f"Step {step.name!r} requires tool execution but agent runtime {runtime.name!r} is "
            f"unavailable: {detail}. Configure 'agent.runtime' to a working coding-agent CLI; a "
            "text-only LLM provider cannot execute this step's file edits."
        )
    return runtime


async def _run_agent_process(
    runtime: AgentRuntime,
    prompt: str,
    *,
    cwd: Path,
    model: str | None,
    fallback_model: str | None,
    max_budget_usd: Decimal | None,
    timeout: float | None,
) -> LLMResult:
    """Spawn *prompt* on *runtime* and wait for its terminal result.

    Mirrors ``LLMProvider.invoke()``'s contract: raises a classified
    ``LLMError`` subclass on failure (so existing ``except RuntimeError``
    fix-loop handling in step code needs no AgentRuntime-specific branch),
    and raises ``LLMTimeoutError`` distinctly on timeout (matching the
    "fix_llm_timeout" vs "fix_llm_failed" marker convention in
    ``sova.llm.errors.format_fix_llm_failure``). Cancellation (e.g. the
    step's own outer ``asyncio.timeout`` in ``WorkflowEngine``) always stops
    the spawned process before propagating, so a cancelled dispatch never
    leaves an orphaned agent subprocess behind.
    """
    proc = await runtime.spawn(
        prompt,
        cwd,
        model=model,
        fallback_model=fallback_model,
        max_budget_usd=max_budget_usd,
    )
    parser = runtime.create_stream_parser()
    parse_line = parser.parse_line if parser is not None else runtime.parse_output
    result: LLMResult | None = None
    stderr_lines: list[str] = []

    async def _drain_stdout() -> None:
        nonlocal result
        async for line in proc.stdout_lines():
            event = parse_line(line)
            if event is not None and event.type == "result" and event.result is not None:
                result = event.result

    async def _drain_stderr() -> None:
        async for line in proc.stderr_lines():
            stderr_lines.append(line)

    try:
        async with asyncio.timeout(timeout):
            await asyncio.gather(_drain_stdout(), _drain_stderr())
            await proc.wait()
    except TimeoutError as exc:
        await proc.stop(cause="agent_dispatch_timeout", requester="agent_dispatch")
        raise LLMTimeoutError(f"Agent runtime {runtime.name!r} timed out after {timeout}s") from exc
    except asyncio.CancelledError:
        await proc.stop(cause="agent_dispatch_cancelled", requester="agent_dispatch")
        raise

    if result is not None:
        return result

    detail = "\n".join(stderr_lines).strip() or "agent process exited without a terminal result event"
    detail = scan_and_redact(detail).redacted_text[:500]
    message = f"Agent runtime {runtime.name!r} failed (exit {proc.returncode}): {detail}"
    raise classify_error(detail)(message)


async def dispatch_command(
    step: BaseStep,
    command: str,
    args: str = "",
    *,
    model: str | None = None,
    fallback_model: str | None = None,
    task_type: str | None = None,
    cwd: Path | str | None = None,
    max_budget_usd: Decimal | None = None,
    timeout: float | None = None,
) -> LLMResult:
    """Run a slash command, routing through AgentRuntime when *step* needs tools.

    ``step.requires_tools`` is the capability flag (``BaseStep``'s own
    semantics: does this step edit files), so a text-only step keeps calling
    ``sova.llm.client.invoke_command()`` through the configured
    ``LLMProvider`` unchanged. A tool-using step always goes through
    ``sova.ipc.runtime.get_runtime()`` instead, regardless of which
    ``LLMProvider`` the project configured, and fails immediately (no silent
    fallback) when that runtime is not actually usable.
    """
    if not step.requires_tools:
        return await client.invoke_command(
            command,
            args,
            model=model,
            fallback_model=fallback_model,
            task_type=task_type,
            cwd=cwd,
            max_budget_usd=max_budget_usd,
            timeout=timeout,
        )

    runtime = await _require_runtime(step)
    resolved_cwd = Path(cwd) if cwd is not None else Path.cwd()
    body = _resolve_command_body(command, resolved_cwd)

    cfg, resolved_model, resolved_timeout = await client.prepare_invocation(
        model=model, task_type=task_type, timeout=timeout, cwd=cwd
    )
    if args:
        from sova.llm.guard import guard_prompt

        # Guarded and compressed exactly as invoke_command() does it, so moving
        # a step to the tool path does not drop the injection check or restore
        # the uncompressed payload (develop's args carry a whole spec section).
        guard_prompt(f"{command} {args}".strip())
        args = client.maybe_compress(args, cwd, cfg=cfg)

    prompt = _assemble_prompt(body, args)
    log.info("agent_dispatch.invoke", step=step.name, command=command, runtime=runtime.name)
    return await _run_agent_process(
        runtime,
        prompt,
        cwd=resolved_cwd,
        model=resolved_model,
        fallback_model=fallback_model,
        max_budget_usd=max_budget_usd,
        timeout=resolved_timeout,
    )


async def dispatch_prompt(
    step: BaseStep,
    prompt: str,
    *,
    model: str | None = None,
    fallback_model: str | None = None,
    task_type: str | None = None,
    cwd: Path | str | None = None,
    max_budget_usd: Decimal | None = None,
    timeout: float | None = None,
) -> LLMResult:
    """Run a raw *prompt*, routing through AgentRuntime when *step* needs tools.

    The prompt-level sibling of ``dispatch_command()``, for a tool-using step
    that assembles its own prompt rather than naming a slash command: today
    ``DevelopStep``'s inner fix loop, which asks the agent to edit source
    files until the project's check command passes. That loop is as
    tool-dependent as the ``/develop`` invocation it follows, so leaving it on
    ``sova.llm.client.invoke()`` would reintroduce the exact silent
    degradation ``requires_tools`` exists to prevent, one call later.
    """
    if not step.requires_tools:
        return await client.invoke(
            prompt,
            model=model,
            fallback_model=fallback_model,
            task_type=task_type,
            cwd=cwd,
            max_budget_usd=max_budget_usd,
            timeout=timeout,
        )

    runtime = await _require_runtime(step)
    cfg, resolved_model, resolved_timeout = await client.prepare_invocation(
        model=model, task_type=task_type, timeout=timeout, cwd=cwd
    )

    from sova.llm.guard import guard_prompt

    guard_prompt(prompt)
    prompt = client.maybe_compress(prompt, cwd, cfg=cfg)

    log.info("agent_dispatch.invoke_prompt", step=step.name, runtime=runtime.name, prompt_len=len(prompt))
    return await _run_agent_process(
        runtime,
        prompt,
        cwd=Path(cwd) if cwd is not None else Path.cwd(),
        model=resolved_model,
        fallback_model=fallback_model,
        max_budget_usd=max_budget_usd,
        timeout=resolved_timeout,
    )
