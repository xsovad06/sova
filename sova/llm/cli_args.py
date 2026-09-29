"""Shared Claude CLI argument builder.

Imported by both the IPC runtime spawn path (``sova.ipc.runtime``) and the
LLM provider invoke/stream paths (``sova.llm.providers.claude_code``). It
has no intra-project imports of its own, so it can never be the module that
closes an import cycle between those two layers. Note this does not make the
import free: ``import sova.llm.cli_args`` still executes ``sova/llm/__init__``,
which pulls in ``client`` and ``provider``.
"""

from __future__ import annotations

import os
import tempfile
from decimal import Decimal
from pathlib import Path


def build_claude_cli_args(
    *,
    model: str | None = None,
    fallback_model: str | None = None,
    max_budget_usd: Decimal | None = None,
    output_format: str = "json",
    system_prompt_file: Path | str | None = None,
) -> list[str]:
    """Build the ``claude -p`` argv shared by every CLI invocation path.

    The user prompt is never on this argv: ``-p`` takes no positional value
    here, so the Claude CLI reads it from stdin instead (the default
    ``--input-format text`` mode supports this). Callers must write the
    prompt to the spawned process's stdin themselves; an unrelated
    ``pkill -f <pattern>`` can no longer match on user prompt content that
    happened to appear in a process's argv.

    The system prompt, when present, is also kept off this argv: callers
    pass a path (see ``write_system_prompt_file()``) rather than the text
    itself, and ``--system-prompt-file`` is used instead of ``--system-prompt
    <text>``. Without this, a caller passing unbounded user-authored text as
    a system prompt (e.g. the supervisor planner's operations persona) would
    reopen the same argv-visibility gap the user prompt was moved off of.

    ``--verbose`` is added automatically whenever *output_format* is
    ``"stream-json"``: the Claude CLI requires it alongside ``-p`` plus
    ``--output-format stream-json``, and every streaming call site wants it
    whenever it requests that format.
    """
    args = [
        "claude",
        "-p",
        "--output-format",
        output_format,
        "--permission-mode",
        "bypassPermissions",
    ]

    if output_format == "stream-json":
        args.append("--verbose")

    if model:
        args.extend(["--model", model])

    if fallback_model:
        args.extend(["--fallback-model", fallback_model])

    if max_budget_usd is not None:
        args.extend(["--max-budget-usd", str(max_budget_usd)])

    if system_prompt_file:
        args.extend(["--system-prompt-file", str(system_prompt_file)])

    return args


def write_system_prompt_file(system_prompt: str) -> Path:
    """Write *system_prompt* to a private temp file and return its path.

    The Claude CLI has no stdin channel for ``--system-prompt`` (stdin is
    reserved for the user prompt), so a private, caller-owned file is the
    argv-avoiding equivalent: ``tempfile.mkstemp`` creates it mode 0600, and
    the caller is responsible for deleting it once the subprocess that reads
    it has exited (e.g. in a ``finally`` block).
    """
    fd, path_str = tempfile.mkstemp(prefix="sova-system-prompt-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(system_prompt)
    except BaseException:
        os.unlink(path_str)
        raise
    return Path(path_str)
