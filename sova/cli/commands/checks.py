"""CLI commands: sova checks should-skip, record.

Lets a command's full-check instruction (develop.md, simplify.md, review.md,
pr.md) avoid re-running an identical check the autonomous pipeline already ran
earlier in the same cycle. See sova.core.check_cache for the caching contract.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Optional

import typer

app = typer.Typer(name="checks", help="Reuse full check-command results within a pipeline cycle.", no_args_is_help=True)

_CHECK_CMD_ARGUMENT = typer.Argument(help="The full check command, e.g. 'make check'.")
_CWD_OPTION = typer.Option(
    "--cwd", exists=True, file_okay=False, help="Worktree directory to check. Defaults to the current directory."
)


@app.command("should-skip")
def should_skip_cmd(
    check_cmd: Annotated[str, _CHECK_CMD_ARGUMENT],
    cwd: Annotated[Optional[Path], _CWD_OPTION] = None,
) -> None:
    """Exit 0 when an identical full check already passed earlier in this pipeline run, else 1."""
    from sova.core.check_cache import should_skip

    resolved_cwd = cwd if cwd is not None else Path.cwd()
    try:
        skip = asyncio.run(should_skip(resolved_cwd, check_cmd))
    except Exception as exc:  # noqa: BLE001 (any lookup failure degrades to running the real check)
        typer.echo(f"run (cache lookup failed: {exc})")
        raise typer.Exit(code=1) from None
    if skip:
        typer.echo("skip")
        raise typer.Exit(code=0)
    typer.echo("run")
    raise typer.Exit(code=1)


@app.command("record")
def record_cmd(
    check_cmd: Annotated[str, _CHECK_CMD_ARGUMENT],
    passed: Annotated[bool, typer.Option("--passed/--failed", help="Outcome of the check that just ran.")],
    cwd: Annotated[Optional[Path], _CWD_OPTION] = None,
) -> None:
    """Record a full check's outcome so a later call site in this cycle can reuse it.

    Always exits 0. The documented call is chained after the real check
    command (``<check> && sova checks record <check> --passed``), so a
    non-zero exit here would turn a passing check into an apparent failure
    and send the agent into its fix-and-re-run loop over nothing.
    """
    from sova.core.check_cache import record_result

    resolved_cwd = cwd if cwd is not None else Path.cwd()
    try:
        recorded = asyncio.run(record_result(resolved_cwd, check_cmd, passed=passed))
    except Exception as exc:  # noqa: BLE001 (advisory bookkeeping must never fail the check it follows)
        typer.echo(f"not recorded: {exc}")
        return
    typer.echo("recorded" if recorded else "not recorded")
