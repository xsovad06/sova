"""Per-phase model-call, token and time tally for a ``command:*`` agent run (issue #1336).

A command run is one long Claude Code session. Its markdown command prints a
marker line (``echo "::sova-phase::<name>"``) at the start of each phase; the
dashboard's stream reader feeds every stream-json event to a ``PhaseTally``,
which charges each model call and its context tokens to the phase in effect
when that call's first event arrived.

A model call is one distinct assistant ``message.id``: one API request, which
re-reads the whole context, so calls times context size is what drives input
spend. The CLI repeats a message across one event per content block, so
counting events would overcount (run 2921: 96 assistant events, 51 ids).
Model calls are not the CLI's ``num_turns``, which counts user-side messages
(each tool result separately, plus the prompt and any injected message): a
reply that runs three tools in parallel is one call but three turns, so across
63 recorded runs ``num_turns`` exceeded the call count by 0 to 14. The two are
stored side by side and never derived from each other. A subagent's messages
(``parent_tool_use_id`` set) are model calls too, charged to the phase they
ran in. Output tokens are not tallied: the per-message figures are partial
(2921's sum to 305 against 15,108 in the result event).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass

UNMARKED_PHASE = "unmarked"

# The phase names each command prints, in file order. This is a contract:
# reports compare these names across runs, so renaming one breaks every
# comparison that spans the rename. tests/test_command_phase_markers.py checks
# each command file carries exactly these markers.
COMMAND_PHASES: dict[str, tuple[str, ...]] = {
    "integrate-pr": (
        "identify",
        "mergeability",
        "update",
        "revert-gate",
        "docs",
        "ci-wait",
        "merge",
        "cleanup",
        "report",
    ),
    "address-pr": ("fix", "squash-rebase", "push-ci-wait", "threads-summary"),
}

# An `echo` of the marker as a command of its own: at a line start or after
# `;` or `&&`, and ending at the line end or the next `;`, `&&` or `||`.
# Agents chain the marker with the phase's first command
# (`echo "::sova-phase::fix"; sed -n 1,5p f`, issue #1376), while a command
# that merely mentions one (a grep for it, prose inside a longer echo, a pipe
# into another command) never switches the phase.
_MARKER_LINE = re.compile(
    r"""(?:^|;|&&)\s*echo\s+(["']?)::sova-phase::([a-z][a-z0-9-]*)\1\s*(?=$|;|&&|\|\|)""", re.MULTILINE
)


def find_phase_marker(command: str) -> str | None:
    """Return the phase a shell command switches to (the last marker echo in it), or None."""
    matches = _MARKER_LINE.findall(command)
    return matches[-1][1] if matches else None


@dataclass
class _PhaseBucket:
    model_calls: int = 0
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    closed_seconds: float = 0.0


class PhaseTally:
    """Accumulate per-phase model calls, context tokens and wall time from stream-json events."""

    def __init__(self, *, started_at: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._current = UNMARKED_PHASE
        self._phase_started_at = started_at
        self._buckets: dict[str, _PhaseBucket] = {UNMARKED_PHASE: _PhaseBucket()}
        self._seen_message_ids: set[str] = set()
        self._reported_usage = False

    def observe(self, event: dict) -> None:
        """Feed one parsed stream-json event; anything but an assistant message is ignored."""
        if event.get("type") != "assistant":
            return
        message = event.get("message")
        if not isinstance(message, dict):
            return

        # Count the call before reading its blocks: a marker switches the phase
        # from the next message on, so the call that prints it stays in the old one.
        message_id = message.get("id")
        if isinstance(message_id, str) and message_id not in self._seen_message_ids:
            self._seen_message_ids.add(message_id)
            self._count_call(message.get("usage"))

        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use" or block.get("name") != "Bash":
                continue
            tool_input = block.get("input")
            command = tool_input.get("command") if isinstance(tool_input, dict) else None
            phase = find_phase_marker(command) if isinstance(command, str) else None
            if phase is not None:
                self._switch_to(phase)

    def snapshot(self) -> list[dict] | None:
        """The breakdown in first-seen order, or None when no message carried usage (Codex).

        Read-only: the open phase's duration runs to now without closing it,
        so a snapshot taken mid-run does not shift later figures.
        """
        if not self._reported_usage:
            return None
        open_seconds = max(self._clock() - self._phase_started_at, 0.0)
        rows = []
        for phase, bucket in self._buckets.items():
            seconds = bucket.closed_seconds + (open_seconds if phase == self._current else 0.0)
            rows.append(
                {
                    "phase": phase,
                    "model_calls": bucket.model_calls,
                    "input_tokens": bucket.input_tokens,
                    "cache_read_tokens": bucket.cache_read_tokens,
                    "cache_write_tokens": bucket.cache_write_tokens,
                    "context_tokens": bucket.input_tokens + bucket.cache_read_tokens + bucket.cache_write_tokens,
                    "duration_ms": round(seconds * 1000),
                }
            )
        return rows

    def _count_call(self, usage: object) -> None:
        bucket = self._buckets[self._current]
        bucket.model_calls += 1
        if not isinstance(usage, dict):
            return
        self._reported_usage = True
        bucket.input_tokens += _token_count(usage, "input_tokens")
        bucket.cache_read_tokens += _token_count(usage, "cache_read_input_tokens")
        bucket.cache_write_tokens += _token_count(usage, "cache_creation_input_tokens")

    def _switch_to(self, phase: str) -> None:
        now = self._clock()
        self._buckets[self._current].closed_seconds += max(now - self._phase_started_at, 0.0)
        self._current = phase
        self._phase_started_at = now
        self._buckets.setdefault(phase, _PhaseBucket())


def _token_count(usage: dict, key: str) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
