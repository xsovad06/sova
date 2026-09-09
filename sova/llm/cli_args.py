"""Shared Claude CLI argument builder.

Imported by both the IPC runtime spawn path (``sova.ipc.runtime``) and the
LLM provider invoke/stream paths (``sova.llm.providers.claude_code``). It
has no intra-project imports of its own, so it can never be the module that
closes an import cycle between those two layers. Note this does not make the
import free: ``import sova.llm.cli_args`` still executes ``sova/llm/__init__``,
which pulls in ``client`` and ``provider``.
"""

from __future__ import annotations

from decimal import Decimal


def build_claude_cli_args(
    prompt: str,
    *,
    model: str | None = None,
    fallback_model: str | None = None,
    max_budget_usd: Decimal | None = None,
    output_format: str = "json",
    system_prompt: str | None = None,
) -> list[str]:
    """Build the ``claude -p`` argv shared by every CLI invocation path.

    ``--verbose`` is added automatically whenever *output_format* is
    ``"stream-json"``: the Claude CLI requires it alongside ``-p`` plus
    ``--output-format stream-json``, and every streaming call site wants it
    whenever it requests that format.
    """
    args = [
        "claude",
        "-p",
        prompt,
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

    if system_prompt:
        args.extend(["--system-prompt", system_prompt])

    return args
