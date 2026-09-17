"""Codex CLI stream event parser.

Maps documented ``codex exec --json`` lifecycle events onto SOVA's existing
``StreamEvent`` / ``LLMResult`` models (see ``sova/llm/models.py``), the same
models ``ClaudeCodeRuntime.parse_output()`` targets. User-visible agent
messages and concise command/file-change progress become ``content`` events;
``turn.completed`` becomes exactly one terminal ``result`` event carrying the
thread ID and token usage; ``turn.failed`` / ``error`` become one terminal
``result`` with ``stop_reason="error"``. Reasoning items produce no output at
all, and unknown event or item types are ignored (logged at debug) so a
future Codex schema addition cannot terminate stream processing.

``CodexStreamParser`` is stateful: it remembers the thread ID reported by
``thread.started`` for a later ``turn.completed``, and latches after the
first terminal event so a duplicate ``turn.completed`` / ``turn.failed`` /
``error`` cannot emit a second result for the same turn. One instance
therefore belongs to exactly one Codex process's output stream.

``CodexRuntime`` does **not** satisfy that today and cannot, because
``AgentRuntime.parse_output()`` takes only a line with no process identity
and the runtime itself is a module-level singleton (``get_runtime()``): its
convenience parser is shared by every concurrent Codex agent, so their
thread IDs and terminal latches would interleave. That is latent, not live:
``AgentConfig.runtime`` does not offer ``codex`` and nothing calls
``parse_output()`` in production yet. Whoever wires Codex into the
dashboard's stream tailer (``sova/dashboard/services/agent_output.py``)
must construct a ``CodexStreamParser`` per spawned process there rather
than reuse the runtime's; that wiring is a separate epic #940 follow-up.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from sova.llm.egress import scan_and_redact
from sova.llm.models import CostSource, LLMResult, StreamEvent
from sova.utils.logging import get_logger

log = get_logger(component="ipc.codex")

# Cap applied before redaction scanning, so an unbounded aggregated command
# output string (can approach the 10 MB subprocess line limit) never turns
# the regex scan into an expensive full-buffer walk.
_MAX_RENDERED_CHARS = 4000
# Cap applied to the final, already-redacted content text.
_MAX_CONTENT_CHARS = 2000
_MAX_COMMAND_CHARS = 200
# Head (not tail) of a command's aggregated output: redaction runs before
# truncation, so slicing from the front keeps the scan and the cut aligned.
_MAX_OUTPUT_HEAD_CHARS = 500
_MAX_FILE_CHANGE_PATHS = 10

_NO_OP_EVENT_TYPES = frozenset({"turn.started", "item.updated"})


def _redact_and_truncate(text: str, max_chars: int = _MAX_CONTENT_CHARS) -> str:
    """Redact secrets, then truncate.

    Order matters: truncating first could slice a secret in half below the
    redaction regex's match width, leaving a mangled fragment that no
    longer matches. The pre-scan cap keeps the scan itself bounded.
    """
    if not text:
        return text
    capped = text[:_MAX_RENDERED_CHARS]
    redacted = scan_and_redact(capped).redacted_text
    return redacted[:max_chars]


def _coerce_usage_int(usage: Any, key: str) -> int:
    """Defensively coerce a usage field to a non-negative int, else 0.

    Missing, null, non-dict ``usage``, non-integer, negative, or
    string-encoded values all coerce to 0 rather than being parsed:
    a bad usage payload must never suppress the terminal result.

    A key that is present but unusable is logged at debug. Without that,
    an upstream schema change (ints becoming strings, counts moving into
    a nested object) degrades to silent zeros in every token column with
    nothing in the run log to point at the cause.
    """
    if not isinstance(usage, dict):
        return 0
    if key not in usage:
        return 0
    value = usage[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        log.debug("codex.unusable_usage_value", key=key, value_type=type(value).__name__)
        return 0
    return value


def _coerce_reasoning_tokens(usage: Any) -> int:
    """Read the reasoning-token count from either shape Codex may report.

    ``codex exec --json`` reports usage flat, but the OpenAI Responses API
    that backs it nests the same breakdown under
    ``output_tokens_details.reasoning_tokens``. Accepting both mirrors the
    ``item_type`` / ``type`` tolerance elsewhere in this parser: whichever
    shape arrives, the count is captured, and a payload carrying neither
    still reports 0.
    """
    flat = _coerce_usage_int(usage, "reasoning_output_tokens")
    if flat:
        return flat
    details = usage.get("output_tokens_details") if isinstance(usage, dict) else None
    return _coerce_usage_int(details, "reasoning_tokens")


class CodexStreamParser:
    """Stateful parser mapping Codex JSONL events to StreamEvent/LLMResult."""

    def __init__(self) -> None:
        self._thread_id = ""
        self._last_message_text = ""
        self._terminal_emitted = False

    def parse_line(self, line: str) -> StreamEvent | None:
        """Parse a single line of Codex ``exec --json`` output.

        Returns None for blank lines, no-op lifecycle events, unknown
        event/item types, and suppressed duplicate terminal events.
        """
        stripped = line.strip()
        if not stripped:
            return None

        try:
            data = json.loads(stripped)
        except ValueError:
            data = None
        # Not JSON at all, or valid JSON that is not an object (a bare scalar
        # or list): surface the line rather than dropping it. Uses the stripped
        # form so a stray line renders like every other runtime's content event
        # instead of carrying its trailing newline and indentation through.
        if not isinstance(data, dict):
            return StreamEvent(type="content", text=_redact_and_truncate(stripped))

        event_type = data.get("type", "")

        if event_type == "thread.started":
            return self._on_thread_started(data)
        if event_type == "item.started":
            return self._on_item_started(data)
        if event_type == "item.completed":
            return self._on_item_completed(data)
        if event_type == "turn.completed":
            return self._on_turn_completed(data)
        if event_type in ("turn.failed", "error"):
            return self._on_terminal_failure(data, event_type)
        if event_type in _NO_OP_EVENT_TYPES:
            return None

        log.debug("codex.unknown_event", event_type=event_type)
        return None

    def _on_thread_started(self, data: dict[str, Any]) -> None:
        # A reused parser instance may see a new thread after a prior turn's
        # terminal event; reset the latch and thread id for the next turn.
        self._thread_id = str(data.get("thread_id") or "")
        self._terminal_emitted = False
        self._last_message_text = ""
        return None

    @staticmethod
    def _item_payload(data: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
        item = data.get("item")
        if not isinstance(item, dict):
            return None
        item_type = item.get("item_type") or item.get("type") or ""
        return item, str(item_type)

    def _on_item_started(self, data: dict[str, Any]) -> StreamEvent | None:
        parsed = self._item_payload(data)
        if parsed is None:
            return None
        item, item_type = parsed
        if item_type != "command_execution":
            return None
        command = _redact_and_truncate(str(item.get("command", "")), _MAX_COMMAND_CHARS)
        if not command:
            return None
        return StreamEvent(type="content", text=f"$ {command}")

    def _on_item_completed(self, data: dict[str, Any]) -> StreamEvent | None:
        parsed = self._item_payload(data)
        if parsed is None:
            return None
        item, item_type = parsed

        if item_type == "reasoning":
            return None
        if item_type == "agent_message":
            return self._render_agent_message(item)
        if item_type == "command_execution":
            return self._render_command_completed(item)
        if item_type == "file_change":
            return self._render_file_change(item)

        log.debug("codex.unknown_item_type", item_type=item_type)
        return None

    def _render_agent_message(self, item: dict[str, Any]) -> StreamEvent | None:
        rendered = _redact_and_truncate(str(item.get("text", "")))
        if not rendered:
            return None
        self._last_message_text = rendered
        return StreamEvent(type="content", text=rendered)

    def _render_command_completed(self, item: dict[str, Any]) -> StreamEvent:
        command = _redact_and_truncate(str(item.get("command", "")), _MAX_COMMAND_CHARS)
        output = _redact_and_truncate(str(item.get("aggregated_output", "")), _MAX_OUTPUT_HEAD_CHARS)
        exit_code = item.get("exit_code")

        parts = [f"$ {command}" if command else "$ (command)"]
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            parts.append(f"exit {exit_code}")
        if output:
            parts.append(output)
        return StreamEvent(type="content", text=" | ".join(parts))

    def _render_file_change(self, item: dict[str, Any]) -> StreamEvent | None:
        changes = item.get("changes")
        if not isinstance(changes, list):
            return None
        paths = [str(c["path"]) for c in changes if isinstance(c, dict) and c.get("path")]
        if not paths:
            return None

        shown = paths[:_MAX_FILE_CHANGE_PATHS]
        summary = f"{len(paths)} file(s) changed: {', '.join(shown)}"
        if len(paths) > _MAX_FILE_CHANGE_PATHS:
            summary += f" (+{len(paths) - _MAX_FILE_CHANGE_PATHS} more)"
        return StreamEvent(type="content", text=_redact_and_truncate(summary))

    def _on_turn_completed(self, data: dict[str, Any]) -> StreamEvent | None:
        if self._terminal_emitted:
            log.debug("codex.duplicate_terminal_event", event_type="turn.completed")
            return None
        self._terminal_emitted = True

        usage = data.get("usage")
        result = LLMResult(
            text=self._last_message_text,
            model="",
            cost_usd=Decimal("0"),
            cost_source=CostSource.UNKNOWN,
            input_tokens=_coerce_usage_int(usage, "input_tokens"),
            output_tokens=_coerce_usage_int(usage, "output_tokens"),
            cache_read_tokens=_coerce_usage_int(usage, "cached_input_tokens"),
            reasoning_output_tokens=_coerce_reasoning_tokens(usage),
            session_id=self._thread_id,
            stop_reason="end_turn",
        )
        return StreamEvent(type="result", text=result.text, result=result)

    def _on_terminal_failure(self, data: dict[str, Any], event_type: str) -> StreamEvent | None:
        if self._terminal_emitted:
            log.debug("codex.duplicate_terminal_event", event_type=event_type)
            return None
        self._terminal_emitted = True

        error = data.get("error")
        if isinstance(error, dict):
            raw_message = str(error.get("message", ""))
        else:
            raw_message = str(data.get("message", ""))
        message = _redact_and_truncate(raw_message) or self._last_message_text

        result = LLMResult(
            text=message,
            model="",
            cost_usd=Decimal("0"),
            cost_source=CostSource.UNKNOWN,
            session_id=self._thread_id,
            stop_reason="error",
        )
        return StreamEvent(type="result", text=result.text, result=result)
