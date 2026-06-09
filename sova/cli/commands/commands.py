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
from sova.config.registry import list_projects

app = typer.Typer(
    name="commands",
    help="Manage SOVA command distribution.",
    no_args_is_help=True,
)

console = Console(stderr=True)

_COMMANDS_SUBDIR = Path(".claude") / "commands"


@app.command(name="list")
def list_cmd(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """List installed commands (managed vs local)."""
    project_dir = (project or Path.cwd()).resolve()
    target_dir = project_dir / _COMMANDS_SUBDIR

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
    target_dir = project_dir / _COMMANDS_SUBDIR
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
    target_dir = project_dir / _COMMANDS_SUBDIR
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


@app.command(name="sync")
def sync_cmd(
    include_autonomous: Annotated[bool, typer.Option("--autonomous", help="Include autonomous agent commands.")] = True,
    force: Annotated[bool, typer.Option("--force", help="Overwrite customized commands.")] = False,
) -> None:
    """Sync commands across all registered projects."""
    projects = list_projects()
    if not projects:
        console.print("[yellow]No projects registered. Run 'sova install' first.[/yellow]")
        return

    canonical_dir = get_canonical_dir()
    total_updated = 0
    total_skipped = 0
    all_conflicts: list[tuple[str, str]] = []

    for slug, path_str in projects.items():
        project_dir = Path(path_str)
        if not project_dir.is_dir():
            console.print(f"  [red]{slug}[/red]: directory not found ({path_str})")
            continue

        target_dir = project_dir / _COMMANDS_SUBDIR
        try:
            cfg = load_config(project_dir)
        except Exception:
            console.print(f"  [red]{slug}[/red]: failed to load config")
            continue

        target_dir.mkdir(parents=True, exist_ok=True)
        result = update_commands(
            canonical_dir,
            target_dir,
            cfg,
            include_autonomous=include_autonomous,
            force=force,
        )

        total_updated += result.updated
        total_skipped += result.skipped
        for name in result.conflicts:
            all_conflicts.append((slug, name))

        status = f"[green]+{result.updated}[/green]" if result.updated else "[dim]+0[/dim]"
        console.print(f"  {slug}: {status} updated, {result.skipped} unchanged")

    console.print(
        f"\n[bold]Summary[/bold]: {total_updated} updated, {total_skipped} unchanged across {len(projects)} project(s)"
    )
    if all_conflicts:
        console.print(f"[yellow]Conflicts ({len(all_conflicts)}):[/yellow]")
        for slug, name in all_conflicts:
            console.print(f"  ! {slug}/{name}")
