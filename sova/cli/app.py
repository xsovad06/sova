"""SOVA CLI entry point."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

import sova
from sova.config.loader import load_config

app = typer.Typer(
    name="sova",
    help="SOVA -- an autonomous AI development crew.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
console = Console(stderr=True)


def version_callback(value: bool) -> None:
    if value:
        typer.echo(f"sova {sova.__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        Optional[bool],
        typer.Option("--version", "-v", callback=version_callback, is_eager=True, help="Show version and exit."),
    ] = None,
) -> None:
    """SOVA -- an autonomous AI development crew."""


@app.command()
def config(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Show the current configuration."""
    cfg = load_config(project)
    table = Table(title="SOVA Configuration", show_header=True)
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("github_repo", cfg.github_repo or "(not set)")
    table.add_row("github_user", cfg.github_user or "(not set)")
    table.add_row("base_branch", cfg.base_branch)
    table.add_row("task_source", cfg.task_source.type)
    table.add_row("agent.model", cfg.agent.model)
    table.add_row("agent.max_budget", str(cfg.agent.max_budget))
    table.add_row("review.enabled", str(cfg.review.enabled))
    table.add_row("review.max_rounds", str(cfg.review.max_rounds))
    table.add_row("roles.default", cfg.roles.default)
    table.add_row("commit.format", cfg.commit.format)
    table.add_row("triage.auto_label", str(cfg.triage.auto_label))

    console.print(table)


@app.command(name="init-db")
def init_db_cmd(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Initialize the SOVA database."""
    import asyncio

    from sova.db.session import init_db

    asyncio.run(init_db(project))
    console.print("[green]Database initialized successfully.[/green]")
