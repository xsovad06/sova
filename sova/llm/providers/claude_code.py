"""Claude Code CLI provider -- the default SOVA LLM backend."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncIterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

# Aliased to the provider's historical name: imported as `_build_args` by
# tests/test_llm.py and tests/test_model_fallback_cli.py.
from sova.llm.cli_args import build_claude_cli_args as _build_args
from sova.llm.cli_args import write_system_prompt_file
from sova.llm.client import cached_enumeration, get_availability_cache
from sova.llm.egress import scan_and_redact
from sova.llm.errors import LLMInvocationError, classify_error
from sova.llm.models import CURATED_MODELS, CostSource, LLMResult, ModelInfo, StreamEvent
from sova.llm.provider import LLMProvider, ProviderCapabilities
from sova.utils.env import configured_passthrough, scrub_agent_env
from sova.utils.logging import get_logger
from sova.utils.shell import ShellResult, run, write_stdin_and_close

log = get_logger(component="llm.provider.claude_code")

# Auth status is a local keychain read; a slow one means something is wrong.
_AUTH_CHECK_TIMEOUT = 15.0

# A probe is a real (minimal) LLM turn, not a local status read, so it gets a
# longer allowance than _AUTH_CHECK_TIMEOUT, but still short enough that a
# hung model never stalls enumeration for long.
_MODEL_PROBE_TIMEOUT = 20.0

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
        system_prompt_path = write_system_prompt_file(system_prompt) if system_prompt else None
        try:
            args = _build_args(
                model=model,
                fallback_model=fallback_model,
                max_budget_usd=max_budget_usd,
                output_format="json",
                system_prompt_file=system_prompt_path,
            )

            log.info("llm.invoke", model=model, prompt_len=len(prompt))

            result = await run(
                *args,
                cwd=cwd,
                timeout=timeout,
                env=scrub_agent_env(passthrough=configured_passthrough()),
                stdin=prompt,
            )
        finally:
            if system_prompt_path is not None:
                system_prompt_path.unlink(missing_ok=True)

        # Try to parse output first - Claude CLI may exit 1 for fallback warnings
        # but still produce valid JSON output.
        partial = _partial_success_payload(result)
        if partial is not None:
            try:
                parsed = _parse_result(partial)
            except (RuntimeError, KeyError):
                parsed = None
            if parsed is not None:
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

    async def list_available_models(self, *, allow_probe: bool = True) -> list[ModelInfo]:
        """Probe the curated candidate list and return the ones that actually work.

        There is no free model-listing endpoint on the CLI, so the only way to
        learn the account's working model set is a minimal ``claude -p`` turn
        per candidate. That cost is bounded: at most one probe per
        ``(identity, model)`` per process (cached), only over the curated
        list, only when ``allow_probe`` is True, and skipped entirely when the
        account is not authenticated.
        """
        if not allow_probe:
            return list(CURATED_MODELS)

        # Probed fresh rather than via get_auth_details(): that method reuses
        # whatever check_available() last cached on this instance, which can
        # be stale if the CLI account was switched afterward. Enumeration
        # must key its cache (and its probe results) off the account that is
        # actually active right now, not a possibly-stale prior identity, so
        # it never stores or serves another account's catalog.
        auth = await _probe_auth()
        if auth is None or auth.get("loggedIn") is not True:
            return list(CURATED_MODELS)

        identity = _claude_code_enumeration_identity(auth)
        return await cached_enumeration(identity, lambda: _probe_curated_models(identity))

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


def _claude_code_enumeration_identity(auth: dict) -> str:
    """Return the enumeration cache key for an authenticated account.

    Scoped by email (not the whole auth payload) so two deployments logged in
    as different accounts in the same process never share a probed model set.
    """
    email = str(auth.get("email") or "").strip()
    return f"claude-code:{email or 'unknown'}"


async def _probe_model(model_id: str) -> bool:
    """Run a minimal claude -p turn with *model_id* and report whether it worked.

    A timeout (run() returns ShellResult(timed_out=True) rather than raising)
    or a nonzero exit both count as unavailable for this model only. An
    OSError (the CLI binary disappearing mid-process) is caught here rather
    than propagated, matching _probe_auth()'s fail-open contract.

    "Worked" is resolved through the same _partial_success_payload() guard
    invoke() uses, not a bare exit-code check: the CLI exits nonzero on a
    fallback warning while still emitting a clean JSON result, and a probe
    stricter than the call path it predicts would drop a model invoke() would
    have accepted.

    Isolated from workspace configuration: ``--safe-mode`` disables CLAUDE.md
    auto-discovery, hooks, MCP servers, and custom commands/agents for this
    one invocation (auth, model selection, and permissions still work
    normally, unlike ``--bare``, which additionally forces API-key-only auth
    and would break subscription/OAuth accounts), and ``--tools ""`` disables
    every built-in tool. Without both, a repository under enumeration (e.g.
    a fork PR's contributor-controlled worktree) could execute commands with
    the authenticated user's privileges merely by being probed for model
    availability, since bypassPermissions mode otherwise loads whatever
    project hooks and MCP config sit in the inherited cwd.
    """
    try:
        result = await run(
            *_build_args(model=model_id, output_format="json"),
            "--safe-mode",
            "--tools",
            "",
            timeout=_MODEL_PROBE_TIMEOUT,
            env=scrub_agent_env(passthrough=configured_passthrough()),
            stdin="hi",
        )
    except OSError:
        log.debug("llm.model_probe.failed", model=model_id, exc_info=True)
        return False
    return result.success or _partial_success_payload(result) is not None


async def _probe_curated_models(identity: str) -> list[ModelInfo] | None:
    """Probe every curated candidate on *identity* and return the working ones.

    Per-model outcomes are cached separately from the whole-list result, so a
    later enumeration after the list TTL expires re-uses the probe answers
    instead of paying for another full pass.

    Returns ``None``, never ``[]``, when no candidate worked: a whole-list
    wipeout is an account- or CLI-level outage rather than a real "this
    account can reach no models" fact, and caching it would leave every
    caller with an empty model list for the full enumeration TTL.
    ``cached_enumeration()`` turns ``None`` into the curated fallback without
    caching it, matching _enumerate_all_backends()'s handling of the same case.
    """
    cache = get_availability_cache()
    working: list[ModelInfo] = []
    for candidate in CURATED_MODELS:
        outcome = cache.get_probe_outcome(identity, candidate.id)
        if outcome is None:
            outcome = await _probe_model(candidate.id)
            cache.set_probe_outcome(identity, candidate.id, outcome)
        if outcome:
            working.append(replace(candidate, source="probed"))
    return working or None


def _partial_success_payload(result: ShellResult) -> dict | None:
    """Return the JSON payload of a nonzero exit that still succeeded, else None.

    The Claude CLI exits nonzero for incidental conditions (a fallback
    warning) while still writing a complete, non-error JSON result to stdout,
    so a bare exit-code check would discard a usable turn. Only stdout that is
    a JSON *object* qualifies: ``json.loads`` accepts a bare scalar or array
    too, and calling ``.get()`` on one raises an AttributeError that no caller
    here guards against.

    Shared by invoke() and _probe_model() so the probe cannot end up stricter
    than the invocation path whose behavior it is meant to predict.
    """
    if result.success or not result.stdout.strip() or result.stderr.strip():
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:  # JSONDecodeError subclasses ValueError
        return None
    if not isinstance(data, dict) or data.get("is_error") or data.get("terminal_reason"):
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
    args = _build_args(model=model, max_budget_usd=max_budget_usd, output_format="stream-json")

    log.info("llm.stream", model=model, prompt_len=len(prompt))

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=scrub_agent_env(passthrough=configured_passthrough()),
    )
    await write_stdin_and_close(proc, prompt)
    return proc
