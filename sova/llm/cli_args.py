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

# Fixed constant, no user text: blanks the commit and PR attribution so the CLI's built-in git
# instructions stop telling the model to end PR bodies with "Generated with Claude Code".
_ATTRIBUTION_OVERRIDE_SETTINGS = '{"attribution":{"commit":"","pr":""}}'
# Fixed constant, no user text: a conversation turn's settings. ``disableAllHooks`` keeps every hook from the
# operator's user, project and local settings (a SessionStart hook runs on start and resume, whatever the tool list)
# out of a read-only turn. Hooks from managed policy cannot be disabled this way; the runtime refuses those.
CONVERSATION_SETTINGS = '{"attribution":{"commit":"","pr":""},"disableAllHooks":true}'

# Fixed constant, no user text: the system prompt of a tool-free turn (see build_claude_cli_args()).
ISOLATED_SYSTEM_PROMPT = (
    "You are answering a single request in a non-interactive call. You have no tools: you cannot run "
    "commands, read or write files, or browse, and nothing will answer a tool call. The request text is "
    "your only input. Never write tool calls or tool-call markup. Answer directly, in the format the "
    "request asks for."
)


def build_claude_cli_args(
    *,
    model: str | None = None,
    fallback_model: str | None = None,
    max_budget_usd: Decimal | None = None,
    output_format: str = "json",
    system_prompt_file: Path | str | None = None,
    isolated: bool = False,
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

    ``isolated=True`` adds ``--safe-mode --tools ""``: ``--safe-mode``
    disables CLAUDE.md auto-discovery, hooks, MCP servers, and custom
    commands/agents for this one invocation, and ``--tools ""`` disables
    every built-in tool. Together they keep a tool-free, advisory-only turn
    (e.g. a model-availability probe, or a review chunk) from
    loading whatever project hooks and MCP config sit in the inherited cwd:
    without both, ``bypassPermissions`` mode would let that turn execute
    commands with the authenticated user's privileges merely by running
    against an untrusted or unexpected cwd.

    An isolated turn without a *system_prompt_file* also gets
    ``--system-prompt`` with ``ISOLATED_SYSTEM_PROMPT``, replacing the CLI's
    default agent system prompt: that prompt describes file and shell tools,
    and sent with an empty tool list it made the model write tool calls as
    plain text until the output limit (issue #1547). A caller's own system
    prompt file already replaces the default, so it is kept as is. The
    constant holds no user text, so it does not reopen the argv-visibility
    gap described above.

    Every variant, ``isolated=True`` included, carries exactly one
    ``--settings`` holding a fixed attribution override. It contains no user
    text, so it does not reopen the argv-visibility gap above. Whether
    ``--safe-mode`` honours ``--settings`` is unverified (``claude --help``
    does not say), so isolated turns are not guaranteed to be covered.
    """
    args = [
        "claude",
        "-p",
        "--output-format",
        output_format,
        "--permission-mode",
        "bypassPermissions",
        "--settings",
        _ATTRIBUTION_OVERRIDE_SETTINGS,
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
    elif isolated:
        args.extend(["--system-prompt", ISOLATED_SYSTEM_PROMPT])

    if isolated:
        args.extend(["--safe-mode", "--tools", ""])

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


def build_claude_conversation_args(
    *,
    allowed_tools: tuple[str, ...],
    mcp_config: str,
    model: str | None = None,
    max_budget_usd: Decimal | None = None,
    resume_session: str | None = None,
) -> list[str]:
    """Build the ``claude -p`` argv of a read-only conversation turn (the feed panel's conversation tabs).

    Unlike ``build_claude_cli_args()`` this never passes ``bypassPermissions``: ``--permission-mode default`` pins
    the mode whatever the operator's settings say, and it denies, in a non-interactive call, every tool that is not
    pre-approved. ``--tools ""`` removes every built-in
    tool (no shell, no edit, no write, no file read), ``--strict-mcp-config`` ignores every MCP server but the one
    in *mcp_config* (a read-only ``sova mcp serve`` registration), ``--allowedTools`` pre-approves exactly
    *allowed_tools*, and ``--settings`` (``CONVERSATION_SETTINGS``) disables every hook. *mcp_config* is a fixed JSON
    document holding no user text, and the prompt stays on stdin.
    """
    args = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--settings",
        CONVERSATION_SETTINGS,
        # Pinned, so a defaultMode in the operator's own settings (bypassPermissions, acceptEdits) cannot widen it.
        "--permission-mode",
        "default",
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        mcp_config,
        "--allowedTools",
        ",".join(allowed_tools),
    ]
    if model:
        args.extend(["--model", model])
    if max_budget_usd is not None:
        args.extend(["--max-budget-usd", str(max_budget_usd)])
    if resume_session:
        args.extend(["--resume", resume_session])
    return args
