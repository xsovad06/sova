"""SOVA CLI entry point."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

import sova
from sova.cli.commands.admin import cleanup, costs, status
from sova.cli.commands.commands import app as commands_app
from sova.cli.commands.harden import harden
from sova.cli.commands.memory import app as memory_app
from sova.cli.commands.migrate import app as migrate_app
from sova.cli.commands.pr import address_pr, learn_from_pr, maintain_pr, review_pr
from sova.cli.commands.project import install, setup
from sova.cli.commands.run import parallel, run_issue, watch
from sova.cli.commands.server import app as server_app
from sova.cli.commands.triage import triage
from sova.config.loader import load_config

app = typer.Typer(
    name="sova",
    help="SOVA -- an autonomous AI development crew.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

# Core workflow
app.command(name="run")(run_issue)
app.command(name="watch")(watch)
app.command(name="parallel")(parallel)

# Triage
app.command(name="triage")(triage)
app.command(name="harden")(harden)

# Project
app.command(name="install")(install)
app.command(name="setup")(setup)

# PR
app.command(name="address-pr")(address_pr)
app.command(name="maintain-pr")(maintain_pr)
app.command(name="review-pr")(review_pr)
app.command(name="learn-from-pr")(learn_from_pr)

# Admin
app.command(name="status")(status)
app.command(name="costs")(costs)
app.command(name="cleanup")(cleanup)

# Memory (subcommand group)
app.add_typer(memory_app)

# Commands (subcommand group)
app.add_typer(commands_app)

# Migrate (subcommand group)
app.add_typer(migrate_app)

# Server (subcommand group)
app.add_typer(server_app)

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


@app.command()
def dashboard(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    host: Annotated[str, typer.Option("--host", help="Host to bind to.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port to serve on.")] = 8111,
    reload: Annotated[bool, typer.Option("--reload", help="Auto-reload on source changes.")] = False,
) -> None:
    """Start the SOVA dashboard web UI."""
    import uvicorn

    console.print(f"[cyan]Starting SOVA dashboard at http://{host}:{port}[/cyan]")

    if reload:
        import os

        if project:
            os.environ["SOVA_DASHBOARD_PROJECT"] = str(project.resolve())
        reload_dir = str(Path(__file__).resolve().parent.parent / "dashboard")
        console.print(f"[dim]Watching {reload_dir} for changes[/dim]")
        uvicorn.run(
            "sova.dashboard.app:create_app",
            factory=True,
            host=host,
            port=port,
            log_level="info",
            reload=True,
            reload_dirs=[reload_dir],
        )
    else:
        from sova.dashboard.app import create_app

        app = create_app(project_dir=project)
        uvicorn.run(app, host=host, port=port, log_level="info")
