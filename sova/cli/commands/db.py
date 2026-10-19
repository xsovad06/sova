"""CLI commands: sova db status/backup."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(name="db", help="Inspect and back up the SOVA database.", no_args_is_help=True)

console = Console(stderr=True)


@app.command()
def status(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Show the current migration revision, the head revision, and any schema drift."""
    asyncio.run(_status(project_dir=project))


async def _status(*, project_dir: Path | None) -> None:
    from sova.db import session as db_session

    inspection = await db_session.inspect_schema(project_dir or Path.cwd())

    if inspection.state == "no_file":
        console.print("[yellow]Database is not initialized (no database file found).[/yellow]")
        return
    if inspection.state == "no_tables":
        console.print("[yellow]Database is not initialized (no database tables found).[/yellow]")
        return

    if inspection.legacy:
        console.print(
            "[yellow]Database has application tables but no alembic_version table "
            "(legacy, unversioned database). Continuing with diagnostics.[/yellow]"
        )

    table = Table(title="SOVA Database Status", show_header=True)
    table.add_column("Field", style="cyan")
    table.add_column("Value", style="white")
    current_label = "[red]unversioned (legacy)[/red]" if inspection.legacy else "[red]unknown[/red]"
    table.add_row("Current revision", inspection.current or current_label)
    table.add_row("Head revision", inspection.head or "[red]unknown[/red]")
    table.add_row("At head", "[green]yes[/green]" if inspection.at_head else "[yellow]no[/yellow]")
    table.add_row("Missing ORM columns", str(len(inspection.missing)) if inspection.missing else "none")
    console.print(table)

    if inspection.missing:
        console.print("[red]Schema drift: the database is missing columns the ORM declares:[/red]")
        for item in inspection.missing:
            console.print(f"  - {item}")


@app.command()
def backup(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Write a WAL-consistent backup of the SQLite database."""
    asyncio.run(_backup(project_dir=project))


async def _backup(*, project_dir: Path | None) -> None:
    from sova.db import session as db_session

    resolved_dir = project_dir or Path.cwd()
    url = db_session._get_database_url(resolved_dir, create_dirs=False)

    db_path = db_session._get_db_path_from_url(url)
    if db_path is None:
        console.print(
            "[yellow]Backup is only supported for SQLite; this project's database uses a different backend.[/yellow]"
        )
        raise typer.Exit(1)

    # Check existence before acquiring the migration lock: lock acquisition
    # creates the lock file's parent directory, which must not happen for a
    # project that has no database to back up yet.
    if not db_path.exists() or db_path.stat().st_size == 0:
        console.print("[yellow]Nothing to back up: no database file exists yet, or it is empty.[/yellow]")
        raise typer.Exit(1)

    async with db_session._migration_lock(url):
        path = await db_session._backup_db(url)
    if path is None:
        console.print("[yellow]Nothing to back up: no database file exists yet, or it is empty.[/yellow]")
        raise typer.Exit(1)

    console.print(f"[green]Backup written to {path}[/green]")
