"""Claude Code CLI provider -- the default SOVA LLM backend."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

# Aliased to the provider's historical name: imported as `_build_args` by
# tests/test_llm.py and tests/test_model_fallback_cli.py.
from sova.llm.cli_args import build_claude_cli_args as _build_args
from sova.llm.egress import scan_and_redact
from sova.llm.errors import LLMInvocationError, classify_error
from sova.llm.models import CostSource, LLMResult, StreamEvent
from sova.llm.provider import LLMProvider, ProviderCapabilities
from sova.utils.env import configured_passthrough, scrub_agent_env
from sova.utils.logging import get_logger
from sova.utils.shell import ShellResult, run

log = get_logger(component="llm.provider.claude_code")

# Auth status is a local keychain read; a slow one means something is wrong.
_AUTH_CHECK_TIMEOUT = 15.0

# Generic tier -> Claude model ID mapping
_MODEL_ALIASES: dict[str, str] = {
    "fast": "sonnet",
    "smart": "opus",
    "cheap": "haiku",
}


class ClaudeCodeProvider(LLMProvider):
    """LLM provider that wraps the Claude Code CLI (``claude -p``)."""

    def __init__(self) -> None:
        # Set unconditionally at the top of every check_available() call
        # (including its early-return paths for a missing/broken CLI), so a
        # same-instance get_auth_details() call (the setup_service.get_auth_status()
        # flow) reuses that outcome instead of spawning a second CLI process.
        # `_probed` distinguishes "check_available() never ran" (probe fresh)
        # from "it ran and found no auth data" (data genuinely absent, do not
        # re-probe); `_last_auth_probe` alone can't carry that distinction
        # since a completed probe can also legitimately be None.
        self._probed = False
        self._last_auth_probe: dict | None = None

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_cli_fallback=True,
            supports_budget_cap=True,
            reports_cost=True,
            dynamic_models=True,
        )

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
        args = _build_args(
            prompt,
            model=model,
            fallback_model=fallback_model,
            max_budget_usd=max_budget_usd,
            output_format="json",
            system_prompt=system_prompt,
        )

        log.info("llm.invoke", model=model, prompt_len=len(prompt))

        result = await run(*args, cwd=cwd, timeout=timeout, env=scrub_agent_env(passthrough=configured_passthrough()))

        # Try to parse output first - Claude CLI may exit 1 for fallback warnings
        # but still produce valid JSON output. Only attempt this when stderr is empty
        # (if stderr has content, it's a real error and we should raise).
        if result.stdout.strip() and not result.success and not result.stderr.strip():
            try:
                data = json.loads(result.stdout)
                if not data.get("is_error") and not data.get("terminal_reason"):
                    parsed = _parse_result(data)
                    log.warning(
                        "llm.invoke.exit_code_nonzero_but_output_valid",
                        exit_code=result.returncode,
                    )
                    log.info(
                        "llm.invoke.completed",
                        model=parsed.model or model,
                        cost_usd=str(parsed.cost_usd),
                        input_tokens=parsed.input_tokens,
                        output_tokens=parsed.output_tokens,
                        duration_ms=parsed.duration_ms,
                    )
                    return parsed
            except (json.JSONDecodeError, RuntimeError, KeyError):
                pass

        # Handle normal success case
        if result.success and result.stdout.strip():
            try:
                parsed = _parse_json_output(result.stdout)
            except RuntimeError as exc:
                log.error("llm.invoke.failed", exit_code=result.returncode, detail=str(exc)[:200])
                raise
            log.info(
                "llm.invoke.completed",
                model=parsed.model or model,
                cost_usd=str(parsed.cost_usd),
                input_tokens=parsed.input_tokens,
                output_tokens=parsed.output_tokens,
                duration_ms=parsed.duration_ms,
            )
            return parsed

        # No valid output - raise error
        if not result.success:
            detail = _extract_failure_detail(result)
            redacted_detail = scan_and_redact(detail).redacted_text[:200]
            log.error("llm.invoke.failed", exit_code=result.returncode, detail=redacted_detail)
            message = f"Claude CLI failed (exit {result.returncode}): {redacted_detail}"
            raise classify_error(redacted_detail)(message)

        # Success but empty output - shouldn't happen. A structural provider
        # fault, never a transient capacity or billing condition, so it is typed
        # directly rather than run through classify_error.
        log.error("llm.invoke.failed", exit_code=result.returncode, detail="succeeded with no output")
        raise LLMInvocationError("Claude CLI succeeded but produced no output")

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
                redacted_stderr = scan_and_redact(stderr_text).redacted_text[:500]
                message = f"Claude CLI streaming failed (exit {proc.returncode}): {redacted_stderr}"
                raise classify_error(redacted_stderr)(message)

    def normalize_model_name(self, model: str) -> str:
        return _MODEL_ALIASES.get(model, model)

    async def check_available(self) -> tuple[bool, str]:
        # Cleared unconditionally, including every early-return path below, so
        # a later failing call always invalidates a cached successful probe
        # from an earlier one: get_auth_details() must never serve stale
        # account data for the current auth state.
        self._probed = True
        self._last_auth_probe = None

        claude_path = shutil.which("claude")
        if not claude_path:
            return False, "claude CLI not found -- install: https://docs.anthropic.com/en/docs/claude-code"
        result = await run(
            "claude",
            "--version",
            env=scrub_agent_env(passthrough=configured_passthrough()),
            timeout=_AUTH_CHECK_TIMEOUT,
        )
        if not result.success:
            return False, "claude CLI found but --version failed"
        version = result.stdout.strip().split("\n")[0]
        self._last_auth_probe = await _probe_auth()
        authenticated, auth_detail = _interpret_auth_probe(self._last_auth_probe)
        if not authenticated:
            return False, f"{version} but {auth_detail}"
        return True, f"{version} ({auth_detail})"

    async def get_auth_details(self) -> dict | None:
        """Return the parsed ``claude auth status --json`` payload, or ``None``.

        Reuses the outcome of a preceding ``check_available()`` call on this
        same instance, whatever it was (including "CLI missing" or "--version
        failed", both of which mean no auth data was ever fetched), instead of
        spawning a second CLI process. Probes fresh only when this instance has
        never had ``check_available()`` called on it.

        ``None`` covers a probe never reached, a probe failure, unparseable
        output, and any ``loggedIn`` value other than ``True``: callers must
        never build an account identity from a partial or failed probe.
        """
        data = self._last_auth_probe if self._probed else await _probe_auth()
        if data is None or data.get("loggedIn") is not True:
            return None
        return data


# ---------------------------------------------------------------------------
# Internal helpers (moved from client.py)
# ---------------------------------------------------------------------------


async def _probe_auth() -> dict | None:
    """Run ``claude auth status --json`` once and return the parsed payload.

    Returns ``None`` on subprocess failure, unparseable output, or output that
    doesn't parse to a JSON object. Callers derive their own meaning (fails
    open vs. explicitly logged out) from the returned ``loggedIn`` field, so
    this function does not interpret it.
    """
    try:
        result = await run(
            "claude",
            "auth",
            "status",
            "--json",
            env=scrub_agent_env(passthrough=configured_passthrough()),
            timeout=_AUTH_CHECK_TIMEOUT,
        )
    except OSError:
        # A fresh get_auth_details() call (no preceding check_available()) can
        # reach here with the CLI missing: create_subprocess_exec raises
        # FileNotFoundError before a ShellResult exists to report failure.
        log.debug("llm.auth_probe.failed", exc_info=True)
        return None
    if not result.success:
        return None
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _interpret_auth_probe(data: dict | None) -> tuple[bool, str]:
    """Interpret an already-fetched ``claude auth status --json`` payload.

    An installed but logged-out CLI passes ``--version`` and then fails every
    agent run at invocation time, so installation alone is not readiness.

    Fails open: CLI builds without the ``auth`` subcommand, or output this does
    not understand, report as available with an unknown auth state rather than
    blocking an otherwise working setup.
    """
    if data is None:
        return True, "auth state unknown"
    logged_in = data.get("loggedIn")
    if logged_in is False:
        return False, "not authenticated (run: claude auth login)"
    if logged_in is not True:
        return True, "auth state unknown"

    email = str(data.get("email") or "").strip()
    subscription = str(data.get("subscriptionType") or "").strip()
    if email and subscription:
        return True, f"{email}, {subscription}"
    return True, email or subscription or "authenticated"


def _strip_leading_warnings(stderr: str) -> str:
    """Strip a contiguous prefix of ``Warning:`` lines from stderr.

    Claude CLI writes incidental warnings (e.g. model-availability notices)
    to stderr regardless of the configured model. Only the leading run of
    warning lines is removed, so a real error following a warning is kept.
    """
    lines = stderr.splitlines()
    idx = 0
    while idx < len(lines) and lines[idx].strip().lower().startswith("warning:"):
        idx += 1
    return "\n".join(lines[idx:]).strip()


def _parse_stdout_error(stdout: str) -> tuple[list[str], str | None]:
    """Parse stdout for a structured Claude CLI error payload.

    Returns (parts, raw_fallback): ``parts`` is non-empty only when stdout
    carries a structured is_error/terminal_reason/result payload; otherwise
    ``raw_fallback`` holds the truncated raw stdout text (or None if stdout
    is empty) to fall back on once stderr has been exhausted.
    """
    if not stdout.strip():
        return [], None

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        data = None

    if not isinstance(data, dict):
        return [], stdout[:500]

    parts: list[str] = []
    if data.get("terminal_reason"):
        parts.append(f"terminal_reason={data['terminal_reason']}")
    if data.get("is_error"):
        parts.append("is_error=true")
    if data.get("result"):
        parts.append(str(data["result"])[:300])
    return parts, stdout[:500]


def _extract_failure_detail(result: ShellResult) -> str:
    """Extract the best available error detail from a failed Claude CLI run.

    Claude CLI with --output-format json writes the real error to stdout as
    JSON (is_error, result, terminal_reason), while stderr often carries only
    an incidental warning. Stdout's structured error always wins when present;
    stderr is used only as a fallback, with leading warning lines stripped so
    a warning never masquerades as the cause.
    """
    stdout_parts, stdout_raw_fallback = _parse_stdout_error(result.stdout)
    if stdout_parts:
        return "; ".join(stdout_parts)[:500]

    stripped_stderr = _strip_leading_warnings(result.stderr)
    if stripped_stderr:
        return stripped_stderr[:500]

    if stdout_raw_fallback is not None:
        return stdout_raw_fallback

    if result.stderr.strip():
        return "(no error detail captured beyond warnings)"

    return "(no error detail captured)"


def _parse_json_output(stdout: str) -> LLMResult:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise LLMInvocationError(f"Failed to parse Claude CLI JSON output: {exc}") from exc
    return _parse_result(data)


def _parse_result(data: dict) -> LLMResult:
    raw_usage = data.get("usage")
    usage = raw_usage if isinstance(raw_usage, dict) else {}
    raw_model_usage = data.get("modelUsage")
    model_usage = raw_model_usage if isinstance(raw_model_usage, dict) else {}
    model = next(iter(model_usage), "") if model_usage else ""

    return LLMResult(
        text=data.get("result", ""),
        model=model,
        cost_usd=Decimal(str(data.get("total_cost_usd", 0))),
        cost_source=CostSource.PRICED,
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
        env=scrub_agent_env(passthrough=configured_passthrough()),
    )
