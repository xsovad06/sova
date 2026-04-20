"""CLI commands: sova commands list/diff/update -- command distribution management."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from sova.commands.catalog import get_canonical_dir
from sova.commands.distribution import diff_commands, list_commands, update_commands
from sova.config.loader import load_config

app = typer.Typer(
    name="commands",
    help="Manage SOVA command distribution.",
    no_args_is_help=True,
)

console = Console(stderr=True)


@app.command(name="list")
def list_cmd(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """List installed commands (managed vs local)."""
    project_dir = (project or Path.cwd()).resolve()
    target_dir = project_dir / ".claude" / "commands"

    listing = list_commands(target_dir)

    table = Table(title="Installed Commands", show_header=True)
    table.add_column("Command", style="cyan")
    table.add_column("Type", style="green")

    for entry in listing.managed:
        table.add_row(entry.filename, "managed (SOVA)")
    for entry in listing.local:
        table.add_row(entry.filename, "local (project)")

    console.print(table)
    console.print(f"\n  Managed: {len(listing.managed)}  |  Local: {len(listing.local)}")


@app.command(name="diff")
def diff_cmd(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Show what changed since last install."""
    project_dir = (project or Path.cwd()).resolve()
    target_dir = project_dir / ".claude" / "commands"
    cfg = load_config(project_dir)
    canonical_dir = get_canonical_dir()

    result = diff_commands(canonical_dir, target_dir, cfg)

    if not result.changed and not result.new and not result.removed:
        console.print("[green]All commands are up to date.[/green]")
        return

    if result.new:
        console.print("[bold]New commands available:[/bold]")
        for name in result.new:
            console.print(f"  + {name}", style="green")

    if result.changed:
        console.print("[bold]Changed commands:[/bold]")
        for name in result.changed:
            console.print(f"  ~ {name}", style="yellow")

    if result.removed:
        console.print("[bold]Removed from canonical:[/bold]")
        for name in result.removed:
            console.print(f"  - {name}", style="red")


@app.command(name="update")
def update_cmd(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    include_autonomous: Annotated[bool, typer.Option("--autonomous", help="Include autonomous agent commands.")] = True,
    force: Annotated[bool, typer.Option("--force", help="Overwrite customized commands without prompting.")] = False,
) -> None:
    """Sync commands to latest canonical versions."""
    project_dir = (project or Path.cwd()).resolve()
    target_dir = project_dir / ".claude" / "commands"
    cfg = load_config(project_dir)
    canonical_dir = get_canonical_dir()

    target_dir.mkdir(parents=True, exist_ok=True)

    result = update_commands(canonical_dir, target_dir, cfg, include_autonomous=include_autonomous, force=force)

    console.print(f"[green]Updated: {result.updated}[/green]")
    console.print(f"[dim]Skipped (unchanged): {result.skipped}[/dim]")

    if result.conflicts:
        console.print(f"\n[yellow]Conflicts ({len(result.conflicts)}):[/yellow]")
        for name in result.conflicts:
            console.print(f"  ! {name} -- locally modified, source also changed")
        console.print("[dim]Use --force to overwrite, or manually merge.[/dim]")
