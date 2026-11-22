"""Readable output lines from Claude Code ``stream-json`` events.

Both consumers of the Claude CLI stream (the dashboard's agent output reader and
``ClaudeCodeRuntime.parse_output()`` for pipeline runs) turn an event into display
lines with ``claude_event_lines()``, so a tool call looks the same in both.

A line's kind is told by its text alone: a tool call starts with ``[Name]`` and an errored tool result with
``[tool error]``, and a text block is one row that may span several lines. A renderer that styles by prefix
must treat agent prose that happens to start with ``[`` as text, so it matches these two forms only.

The real event shape nests blocks under ``message.content``: an ``assistant`` event
carries ``text`` and ``tool_use`` blocks, and a ``user`` event carries the
``tool_result`` blocks answering them.
"""

from __future__ import annotations

import json
import re
from typing import Any

from sova.llm.egress import scan_and_redact

_MAX_SUMMARY_CHARS = 160
# Only this much of a raw argument is redacted: a Bash heredoc or a serialised input can be hundreds of KB, and the
# redactor would run over all of it on the dashboard's event loop. It is above the 10000-character window of the
# longest credential pattern (a private key block), so no credential can straddle the cut.
_REDACTION_WINDOW = _MAX_SUMMARY_CHARS + 12000
# Claude Code wraps most errored tool results in this tag pair.
_TOOL_ERROR_WRAPPER = re.compile(r"^\s*<tool_use_error>(.*?)(?:</tool_use_error>)?\s*$", re.DOTALL)
# The input field that says what a tool call is about, first match wins. A tool with
# none of them (or an unknown tool) is summarised by its name alone.
_MAIN_ARGUMENT_KEYS = ("command", "file_path", "pattern", "path", "url", "query", "description", "prompt")


def _one_line(value: Any, limit: int = _MAX_SUMMARY_CHARS) -> str:
    """Render *value*, credentials redacted, as one line of at most *limit* characters (``...`` when cut)."""
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    # Redacted on the raw text (patterns may anchor on line boundaries, as a PEM block does), then flattened,
    # then cut, so neither the newlines nor the boundary can hide a credential from the pattern. Only a bounded
    # prefix is redacted, since the rest is cut away anyway.
    text = " ".join(scan_and_redact(text[:_REDACTION_WINDOW]).redacted_text.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def summarize_tool_use(name: str, tool_input: Any) -> str:
    """One line for a ``tool_use`` block: ``[Name] main argument``, or ``[Name]`` when it has none."""
    label = f"[{name or 'tool'}]"
    if not isinstance(tool_input, dict):
        return label
    for key in _MAIN_ARGUMENT_KEYS:
        value = tool_input.get(key)
        if value not in (None, "", [], {}):
            summary = _one_line(value)
            return f"{label} {summary}" if summary else label
    return label


def summarize_tool_error(content: Any) -> str:
    """One line for an errored ``tool_result``: ``[tool error] first line of the detail``."""
    if isinstance(content, list):
        content = " ".join(str(block.get("text", "")) if isinstance(block, dict) else str(block) for block in content)
    if isinstance(content, str) and (wrapped := _TOOL_ERROR_WRAPPER.match(content)):
        content = wrapped.group(1)
    detail = _one_line(content) if isinstance(content, str) else ""
    return f"[tool error] {detail}" if detail else "[tool error]"


def _block_line(event_type: Any, block: Any) -> str | None:
    """The display line for one content block of an event, or ``None`` when it shows nothing."""
    if isinstance(block, str):
        block = {"type": "text", "text": block}
    elif not isinstance(block, dict):
        return None
    kind = block.get("type")
    if event_type == "assistant" and kind == "text":
        text = block.get("text")
        return text if isinstance(text, str) and text.strip() else None
    if event_type == "assistant" and kind == "tool_use":
        return summarize_tool_use(block.get("name") or "", block.get("input"))
    if event_type == "user" and kind == "tool_result" and block.get("is_error"):
        return summarize_tool_error(block.get("content"))
    return None


def claude_event_lines(data: dict[str, Any]) -> list[str]:
    """Display lines for one decoded stream-json event, in block order.

    Text blocks keep their text (empty ones are skipped), each ``tool_use`` becomes a summary line and
    each errored ``tool_result`` an error line. Any other event, and a successful ``tool_result`` (its
    body is the tool's raw output, too large to log), yields nothing.
    """
    message = data.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    event_type = data.get("type")
    return [line for block in content if (line := _block_line(event_type, block)) is not None]
