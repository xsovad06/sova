"""Claude Code CLI provider -- the default SOVA LLM backend."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

from sova.llm.models import LLMResult, StreamEvent
from sova.llm.provider import LLMProvider
from sova.utils.logging import get_logger
from sova.utils.shell import ShellResult, run

log = get_logger(component="llm.provider.claude_code")

# Generic tier -> Claude model ID mapping
_MODEL_ALIASES: dict[str, str] = {
    "fast": "sonnet",
    "smart": "opus",
    "cheap": "haiku",
}


class ClaudeCodeProvider(LLMProvider):
    """LLM provider that wraps the Claude Code CLI (``claude -p``)."""

    async def invoke(
        self,
        prompt: str,
        *,
        model: str | None = None,
        cwd: Path | str | None = None,
        max_budget_usd: Decimal | None = None,
        timeout: float | None = 600,
    ) -> LLMResult:
        args = _build_args(prompt, model=model, max_budget_usd=max_budget_usd, output_format="json")

        log.info("llm.invoke", model=model, prompt_len=len(prompt))

        result = await run(*args, cwd=cwd, timeout=timeout)
        if not result.success:
            detail = _extract_failure_detail(result)
            raise RuntimeError(f"Claude CLI failed (exit {result.returncode}): {detail}")

        return _parse_json_output(result.stdout)

    async def invoke_streaming(
        self,
        prompt: str,
        *,
        model: str | None = None,
        cwd: Path | str | None = None,
        max_budget_usd: Decimal | None = None,
    ) -> AsyncIterator[StreamEvent]:
        proc = await _start_streaming_process(prompt, model=model, cwd=cwd, max_budget_usd=max_budget_usd)

        previous_text = ""
        got_result = False
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
                    got_result = True
                    break

                if data.get("type") == "assistant":
                    content_blocks = data.get("message", {}).get("content", [])
                    full_text = ""
                    for block in content_blocks:
                        if block.get("type") == "text":
                            full_text += block.get("text", "")

                    if full_text and full_text != previous_text:
                        delta = full_text[len(previous_text) :]
                        previous_text = full_text
                        yield StreamEvent(type="content", text=delta)
        finally:
            stderr_bytes = await proc.stderr.read() if proc.stderr else b""
            await proc.wait()
            if not got_result and proc.returncode and proc.returncode != 0:
                stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"Claude CLI streaming failed (exit {proc.returncode}): {stderr_text[:500]}")

    def normalize_model_name(self, model: str) -> str:
        return _MODEL_ALIASES.get(model, model)

    async def check_available(self) -> tuple[bool, str]:
        claude_path = shutil.which("claude")
        if not claude_path:
            return False, "claude CLI not found -- install: https://docs.anthropic.com/en/docs/claude-code"
        result = await run("claude", "--version")
        if result.success:
            version = result.stdout.strip().split("\n")[0]
            return True, version
        return False, "claude CLI found but --version failed"


# ---------------------------------------------------------------------------
# Internal helpers (moved from client.py)
# ---------------------------------------------------------------------------


def _extract_failure_detail(result: ShellResult) -> str:
    """Extract the best available error detail from a failed Claude CLI run.

    Claude CLI with --output-format json writes error info to stdout as JSON
    (is_error, result, terminal_reason), while stderr is often empty.
    """
    if result.stderr.strip():
        return result.stderr[:500]

    if result.stdout.strip():
        try:
            data = json.loads(result.stdout)
            parts: list[str] = []
            if data.get("terminal_reason"):
                parts.append(f"terminal_reason={data['terminal_reason']}")
            if data.get("is_error"):
                parts.append("is_error=true")
            if data.get("result"):
                parts.append(str(data["result"])[:300])
            if parts:
                return "; ".join(parts)
        except (json.JSONDecodeError, KeyError):
            return result.stdout[:500]

    return "(no error detail captured)"


def _build_args(
    prompt: str,
    *,
    model: str | None = None,
    max_budget_usd: Decimal | None = None,
    output_format: str = "json",
) -> list[str]:
    args = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        output_format,
        "--permission-mode",
        "auto",
    ]

    if model:
        args.extend(["--model", model])

    if max_budget_usd is not None:
        args.extend(["--max-budget-usd", str(max_budget_usd)])

    return args


def _parse_json_output(stdout: str) -> LLMResult:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse Claude CLI JSON output: {exc}") from exc
    return _parse_result(data)


def _parse_result(data: dict) -> LLMResult:
    usage = data.get("usage", {})
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
    args = _build_args(prompt, model=model, max_budget_usd=max_budget_usd, output_format="stream-json")

    log.info("llm.stream", model=model, prompt_len=len(prompt))

    return await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
