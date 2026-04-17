"""Claude Code CLI invocation for SOVA."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

from sova.config.models import RolesConfig
from sova.llm.models import LLMResult, StreamEvent
from sova.utils.logging import get_logger
from sova.utils.shell import run

log = get_logger(component="llm.client")


async def invoke(
    prompt: str,
    *,
    model: str | None = None,
    cwd: Path | str | None = None,
    max_budget_usd: Decimal | None = None,
    timeout: float | None = 600,
) -> LLMResult:
    """Run Claude Code CLI with a prompt and return the parsed result.

    Args:
        prompt: The prompt text to send.
        model: Model alias (e.g., "opus", "sonnet") or full ID.
        cwd: Working directory for the Claude process.
        max_budget_usd: Maximum spend for this invocation.
        timeout: Timeout in seconds (default 10 minutes).

    Returns:
        Parsed LLMResult with text, cost, and token data.

    Raises:
        RuntimeError: If the Claude CLI process exits with non-zero code.
    """
    args = _build_args(prompt, model=model, max_budget_usd=max_budget_usd, output_format="json")

    log.info("llm.invoke", model=model, prompt_len=len(prompt))

    result = await run(*args, cwd=cwd, timeout=timeout)
    if not result.success:
        raise RuntimeError(f"Claude CLI failed (exit {result.returncode}): {result.stderr[:500]}")

    parsed = _parse_json_output(result.stdout)
    return parsed


async def invoke_command(
    command: str,
    args: str = "",
    *,
    model: str | None = None,
    cwd: Path | str | None = None,
    max_budget_usd: Decimal | None = None,
    timeout: float | None = 600,
) -> LLMResult:
    """Run a Claude Code slash command.

    This constructs a prompt that triggers the command, similar to how
    the Claude Code CLI processes slash commands in non-interactive mode.

    Args:
        command: Command name (e.g., "/develop", "/review").
        args: Arguments to pass to the command.
        model: Model alias or full ID.
        cwd: Working directory.
        max_budget_usd: Maximum spend.
        timeout: Timeout in seconds.
    """
    prompt = f"{command} {args}".strip() if args else command

    log.info("llm.invoke_command", command=command, args=args, model=model)

    return await invoke(prompt, model=model, cwd=cwd, max_budget_usd=max_budget_usd, timeout=timeout)


async def invoke_streaming(
    prompt: str,
    *,
    model: str | None = None,
    cwd: Path | str | None = None,
    max_budget_usd: Decimal | None = None,
) -> AsyncIterator[StreamEvent]:
    """Run Claude Code CLI with streaming output.

    Yields StreamEvent objects as output arrives, allowing the dashboard
    to display live progress. The final event has type="result" with the
    full LLMResult including costs.

    Args:
        prompt: The prompt text.
        model: Model alias or full ID.
        cwd: Working directory.
        max_budget_usd: Maximum spend.

    Yields:
        StreamEvent with type "content" or "result".
    """
    proc = await _start_streaming_process(prompt, model=model, cwd=cwd, max_budget_usd=max_budget_usd)

    previous_text = ""
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break

            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str:
                continue

            try:
                data = json.loads(line_str)
            except json.JSONDecodeError:
                continue

            if data.get("type") == "result":
                parsed = _parse_result(data)
                yield StreamEvent(type="result", text=parsed.text, result=parsed)
                break

            # Assistant messages contain accumulated text content
            if data.get("type") == "assistant":
                content_blocks = data.get("message", {}).get("content", [])
                full_text = ""
                for block in content_blocks:
                    if block.get("type") == "text":
                        full_text += block.get("text", "")

                if full_text and full_text != previous_text:
                    delta = full_text[len(previous_text):]
                    previous_text = full_text
                    yield StreamEvent(type="content", text=delta)
    finally:
        await proc.wait()


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


def _build_args(
    prompt: str,
    *,
    model: str | None = None,
    max_budget_usd: Decimal | None = None,
    output_format: str = "json",
) -> list[str]:
    """Build the CLI argument list for claude invocation."""
    args = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        output_format,
    ]

    if model:
        args.extend(["--model", model])

    if max_budget_usd is not None:
        args.extend(["--max-budget-usd", str(max_budget_usd)])

    return args


def _parse_json_output(stdout: str) -> LLMResult:
    """Parse the JSON output from claude CLI (--output-format json)."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse Claude CLI JSON output: {exc}") from exc

    return _parse_result(data)


def _parse_result(data: dict) -> LLMResult:
    """Parse a result dict (from JSON or stream-json) into LLMResult."""
    usage = data.get("usage", {})

    # Extract model from modelUsage keys
    model_usage = data.get("modelUsage", {})
    model = next(iter(model_usage), "") if model_usage else ""

    return LLMResult(
        text=data.get("result", ""),
        model=model,
        cost_usd=Decimal(str(data.get("total_cost_usd", 0))),
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cache_read_tokens=usage.get("cache_read_input_tokens", 0),
        cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
        duration_ms=data.get("duration_ms", 0),
        session_id=data.get("session_id", ""),
        stop_reason=data.get("stop_reason", ""),
    )


async def _start_streaming_process(
    prompt: str,
    *,
    model: str | None = None,
    cwd: Path | str | None = None,
    max_budget_usd: Decimal | None = None,
) -> asyncio.subprocess.Process:
    """Start a Claude CLI process in stream-json mode."""
    args = _build_args(prompt, model=model, max_budget_usd=max_budget_usd, output_format="stream-json")

    log.info("llm.stream", model=model, prompt_len=len(prompt))

    return await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
