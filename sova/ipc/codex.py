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
``error`` cannot emit a second result for the same turn. ``turn.started``
and ``thread.started`` lift that latch, so a thread running several turns
reports each one rather than only the first. One instance therefore
belongs to exactly one Codex process's output stream.

``CodexRuntime`` does **not** satisfy that today and cannot, because
``AgentRuntime.parse_output()`` takes only a line with no process identity
and the runtime itself is a module-level singleton (``get_runtime()``): its
convenience parser is shared by every concurrent Codex agent, so their
thread IDs and terminal latches would interleave. That is latent, not live:
``AgentConfig.runtime`` does offer ``codex``, but nothing calls
``parse_output()`` in production yet. Whoever wires Codex into the
dashboard's stream tailer (``sova/dashboard/services/agent_output.py``)
must construct a ``CodexStreamParser`` per spawned process there rather
than reuse the runtime's; that wiring is a separate epic #940 follow-up.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

from sova.llm.egress import scan_and_redact
from sova.llm.models import CostSource, LLMResult, StreamEvent
from sova.utils.logging import get_logger

log = get_logger(component="ipc.codex")

# Margin applied to the scan window beyond the caller's own cap, so an
# unbounded aggregated command output string (can approach the 10 MB
# subprocess line limit) never turns the regex scan into an expensive
# full-buffer walk, while still leaving room to move the cut off a secret
# and onto a whitespace boundary.
#
# Sizing, and what it does and does not bound. Strategy 2 in
# ``_redact_and_truncate`` drops this many characters from the tail of the
# redacted window, so it bounds a sliced secret only when the pattern's
# *value* sits at the end of its match and has a fixed minimum width. Every
# such pattern in ``_SENSITIVE_PATTERNS`` is covered: the widest requirement
# is ``github_pat_\w{82,}``, whose value can fall at most 81 characters short
# of matching, so 256 is comfortable headroom.
#
# It does not bound a pattern whose match needs trailing context after an
# unbounded value, because the sliced fragment is then as long as the value:
# a JWT (third segment ``{10,}``) and a connection string (password up to
# ``@``). For those, strategy 2 can leave the front of the match visible,
# which for a JWT is its header and payload segments (the signature is always
# inside the dropped tail, so the token stays unusable) and for a connection
# string longer than the margin is the leading characters of the password.
# That residual is accepted rather than fixed: only a whitespace cut
# (strategies 1 and 3) is provably safe, and strategy 2 exists precisely to
# avoid collapsing an unbroken blob back to a whitespace cut. Weigh it before
# adding a pattern with an unbounded value plus trailing context.
# ``TestBoundaryMarginResidual`` in ``tests/test_codex_parser.py`` pins the
# behaviour so it cannot regress silently in either direction.
_BOUNDARY_MARGIN_CHARS = 256
# Cap applied to the final, already-redacted content text.
_MAX_CONTENT_CHARS = 2000
_MAX_COMMAND_CHARS = 200
# Head (not tail) of a command's aggregated output: redaction runs before
# truncation, so slicing from the front keeps the scan and the cut aligned.
_MAX_OUTPUT_HEAD_CHARS = 500
_MAX_FILE_CHANGE_PATHS = 10

_NO_OP_EVENT_TYPES = frozenset({"item.updated"})


_WHITESPACE_RE = re.compile(r"\s")


def _last_whitespace_in(text: str, start: int, stop: int) -> int | None:
    """Index of the last whitespace character in ``text[start:stop]``, else None."""
    matches = list(_WHITESPACE_RE.finditer(text, start, stop))
    return matches[-1].start() if matches else None


def _redact_and_truncate(text: str, max_chars: int = _MAX_CONTENT_CHARS) -> str:
    """Redact secrets, then truncate.

    Order matters: truncating first could slice a secret in half below the
    redaction regex's match width, leaving a mangled fragment that no longer
    matches. Bounding the scan window is itself a truncation and carries the
    same hazard, so the window cut is moved back onto a whitespace boundary
    before anything is scanned: no value matched by ``_SENSITIVE_PATTERNS``
    holds whitespace, so a secret straddling the cut is either scanned whole
    (and redacted) or dropped whole. Once that holds, the final cut to
    ``max_chars`` only ever slices already-redacted text.

    Three cut strategies are tried in order, all of them safe; they differ
    only in how much text survives.

    1. The last whitespace inside the margin region (``[max_chars, window)``).
       This is the only strategy that both cuts on whitespace and keeps the
       full ``max_chars`` the caller asked for.
    2. Redact the whole window and drop the margin region afterwards. Only
       the window's tail can hold a boundary-sliced fragment, because
       redaction never reorders text, and the margin covers every pattern
       whose secret value ends its match at a fixed minimum width. It does
       not cover a pattern needing trailing context after an unbounded value
       (JWT, connection string); see ``_BOUNDARY_MARGIN_CHARS`` for what can
       still surface there and why that is accepted. Used when the margin region
       holds no whitespace at all (an unbroken token spanning the cut), where
       strategy 1 would otherwise have to back the cut up far below
       ``max_chars`` and throw away most of the caller's budget: a command
       printing a 3 KB compact-JSON blob after a short header collapsed to
       just the header.
    3. The last whitespace anywhere before ``max_chars``. Strategy 2 drops a
       fixed character count from text that redaction may have shrunk, so on
       a secret-dense window it can consume the whole string: a ``curl``
       carrying a bearer token redacts down to 32 characters and vanishes
       from the stream entirely. Falling back to an early whitespace cut
       keeps such a line visible, truncated, rather than dropping it.

    A window with no whitespace anywhere that also redacts away to nothing
    has no cut point left and yields an empty string; the caller then emits
    no event for it.
    """
    if not text:
        return text
    window = max_chars + _BOUNDARY_MARGIN_CHARS
    if len(text) <= window:
        return scan_and_redact(text).redacted_text[:max_chars]

    boundary = _last_whitespace_in(text, max_chars, window)
    if boundary is not None:
        return scan_and_redact(text[:boundary]).redacted_text[:max_chars]

    redacted = scan_and_redact(text[:window]).redacted_text
    trimmed = redacted[:-_BOUNDARY_MARGIN_CHARS][:max_chars]
    if trimmed:
        return trimmed

    early = _last_whitespace_in(text, 0, max_chars)
    if early is None:
        return ""
    return scan_and_redact(text[:early]).redacted_text[:max_chars]


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
    if not isinstance(usage, dict):
        return 0
    if "reasoning_output_tokens" in usage:
        return _coerce_usage_int(usage, "reasoning_output_tokens")
    return _coerce_usage_int(usage.get("output_tokens_details"), "reasoning_tokens")


def _str_field(obj: Any, key: str) -> str:
    """Read a string field, mapping missing, ``None``, and non-string values to ``""``.

    ``str(obj.get(key, ""))`` only falls back on a missing key: an explicit
    JSON ``null`` still reaches ``str()`` and renders as the literal text
    ``"None"``, which then gets stored and displayed as if it were real
    content.
    """
    if not isinstance(obj, dict):
        return ""
    value = obj.get(key)
    return value if isinstance(value, str) else ""


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

        raw_type = data.get("type")
        event_type = raw_type if isinstance(raw_type, str) else ""

        if event_type == "thread.started":
            self._reset_for_thread(data)
            return None
        if event_type == "turn.started":
            self._reset_for_turn()
            return None
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

    def _reset_for_thread(self, data: dict[str, Any]) -> None:
        # A reused parser instance may see a new thread after a prior turn's
        # terminal event; adopt the new thread id and start its first turn.
        raw_thread_id = data.get("thread_id")
        self._thread_id = "" if raw_thread_id is None else str(raw_thread_id)
        self._reset_for_turn()

    def _reset_for_turn(self) -> None:
        # The terminal latch suppresses a duplicate terminal event for the
        # turn that emitted it, so it has to lift when the next turn opens.
        # A thread that runs several turns would otherwise report only the
        # first one's usage and drop every later result. The thread id is
        # deliberately kept: it spans the whole thread, not one turn.
        self._terminal_emitted = False
        self._last_message_text = ""

    @staticmethod
    def _item_payload(data: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """The event's item and its type, both empty when the payload is missing or malformed."""
        item = data.get("item")
        if not isinstance(item, dict):
            return {}, ""
        return item, str(item.get("item_type") or item.get("type") or "")

    def _on_item_started(self, data: dict[str, Any]) -> StreamEvent | None:
        item, item_type = self._item_payload(data)
        if item_type != "command_execution":
            return None
        command = _redact_and_truncate(_str_field(item, "command"), _MAX_COMMAND_CHARS)
        if not command:
            return None
        return StreamEvent(type="content", text=f"$ {command}")

    def _on_item_completed(self, data: dict[str, Any]) -> StreamEvent | None:
        item, item_type = self._item_payload(data)
        if not item_type:
            return None

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
        rendered = _redact_and_truncate(_str_field(item, "text"))
        if not rendered:
            return None
        self._last_message_text = rendered
        return StreamEvent(type="content", text=rendered)

    def _render_command_completed(self, item: dict[str, Any]) -> StreamEvent:
        command = _redact_and_truncate(_str_field(item, "command"), _MAX_COMMAND_CHARS)
        output = _redact_and_truncate(_str_field(item, "aggregated_output"), _MAX_OUTPUT_HEAD_CHARS)
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
        # ``text`` is the last agent message as rendered for the stream, so it
        # is redacted and capped at ``_MAX_CONTENT_CHARS``. That diverges from
        # ``ClaudeCodeRuntime.parse_output()``, which passes its result text
        # through verbatim. Harmless while this feeds a display stream; a
        # consumer that treats ``LLMResult.text`` as the turn's full answer
        # needs an untruncated copy kept alongside it.
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
        # ``error`` is treated as terminal for the turn. If Codex ever emits a
        # non-fatal ``error``, the latch set here swallows the real
        # ``turn.completed``, so the turn reports failure with no usage.
        # Deliberate: reporting a failure that succeeded is recoverable, while
        # reporting success on a failed turn is not, and lifting the latch for
        # a later ``turn.completed`` would emit two terminal results for one
        # turn. Revisit against the Codex event schema under epic #940.
        if self._terminal_emitted:
            log.debug("codex.duplicate_terminal_event", event_type=event_type)
            return None
        self._terminal_emitted = True

        error = data.get("error")
        if isinstance(error, dict):
            raw_message = _str_field(error, "message")
        elif error:
            # A bare scalar under "error" still carries the cause; rendering
            # it beats falling through to the unrelated last agent message.
            raw_message = str(error)
        else:
            raw_message = _str_field(data, "message")
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
