"""Shared Claude CLI stdout/stderr failure-detail parsing.

Both ``sova.llm.providers.claude_code`` (the ``LLMProvider`` path, a batch
``claude --output-format json`` call) and ``sova.core.agent_dispatch`` (the
AgentRuntime path, streaming ``claude --output-format stream-json``) spawn the
same CLI and must apply the same precedence when building a failure message
from a dead process: the CLI writes its real error to stdout as structured
JSON (``is_error``, ``terminal_reason``, ``result``), while stderr often
carries only an incidental warning. Stdout's structured error always wins
when present; stderr is used only as a fallback, with leading warning lines
stripped so a warning never masquerades as the cause.
"""

from __future__ import annotations

import json


def strip_leading_warnings(stderr: str) -> str:
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


def parse_stdout_error(stdout: str) -> tuple[list[str], str | None]:
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


def extract_claude_cli_failure_detail(*, stdout: str, stderr: str) -> str:
    """Return the best available error detail from a failed Claude CLI run.

    Claude CLI writes the real error to stdout as JSON (is_error, result,
    terminal_reason), while stderr often carries only an incidental warning.
    Stdout's structured error always wins when present; stderr is used only
    as a fallback, with leading warning lines stripped so a warning never
    masquerades as the cause.
    """
    stdout_parts, stdout_raw_fallback = parse_stdout_error(stdout)
    if stdout_parts:
        return "; ".join(stdout_parts)[:500]

    stripped_stderr = strip_leading_warnings(stderr)
    if stripped_stderr:
        return stripped_stderr[:500]

    if stdout_raw_fallback is not None:
        return stdout_raw_fallback

    if stderr.strip():
        return "(no error detail captured beyond warnings)"

    return "(no error detail captured)"
