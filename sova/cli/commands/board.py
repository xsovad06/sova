"""CLI command: sova board add.

Deterministic project-board membership for freshly created issues. The
health-audit command used to carry this as prose and shell snippets an LLM
re-derived on every run; it is now one call that prints one line per issue URL.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy.exc import SQLAlchemyError

from sova.cli.commands.audit import project_root
from sova.config.loader import load_config
from sova.git.board import add_issues_to_board
from sova.utils.logging import get_logger

log = get_logger(component="cli.board")

app = typer.Typer(name="board", help="Project board operations.", no_args_is_help=True)

# Everything load_config can raise from a malformed or unreadable sova.toml or a locked or broken database.
_CONFIG_ERRORS = (RuntimeError, ValueError, OSError, sqlite3.Error, SQLAlchemyError)

_URL_ARGUMENT = typer.Argument(help="Issue URLs to add to the configured project board.")


@app.command("add")
def add_cmd(urls: Annotated[list[str], _URL_ARGUMENT]) -> None:
    """Add issues to the configured project board, printing one line per URL.

    Uses task_source.github_project_number, or the repo owner's only board when it is 0 or the
    configuration cannot be loaded (the load error goes to stderr). When no single board can be
    chosen, one `skip:` line replaces the per-URL lines. A `board:` line names the board used when
    it was discovered or its title is known.
    Exits 0 when every URL was added (or no single board could be chosen), 1 when any URL failed.
    """
    try:
        root = asyncio.run(project_root(Path.cwd()))
        number = load_config(root).task_source.github_project_number
    except _CONFIG_ERRORS as exc:
        typer.echo(f"Configuration error: {exc}; discovering the board instead", err=True)
        number = 0
    outcomes = asyncio.run(add_issues_to_board(urls, number))
    for outcome in outcomes:
        typer.echo(outcome.line())
    failures = sum(1 for outcome in outcomes if outcome.failed)
    if failures:
        log.warning("board.add_failed", failed=failures, total=len(outcomes))
        raise typer.Exit(code=1)
