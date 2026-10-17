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
    from alembic.config import Config
    from sqlalchemy import inspect, text
    from sqlalchemy.ext.asyncio import create_async_engine

    from sova.db import session as db_session

    resolved_dir = project_dir or Path.cwd()
    url = db_session._get_database_url(resolved_dir, create_dirs=False)
    db_path = db_session._get_db_path_from_url(url)
    if db_path is not None and not db_path.exists():
        console.print("[yellow]Database is not initialized (no database file found).[/yellow]")
        return

    connect_args = {"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {}
    engine = create_async_engine(url, connect_args=connect_args)

    try:
        async with engine.connect() as conn:
            table_names = await conn.run_sync(lambda c: inspect(c).get_table_names())

        if not table_names:
            console.print("[yellow]Database is not initialized (no database tables found).[/yellow]")
            return

        legacy = "alembic_version" not in table_names
        current: str | None = None
        if legacy:
            console.print(
                "[yellow]Database has application tables but no alembic_version table "
                "(legacy, unversioned database). Continuing with diagnostics.[/yellow]"
            )
        else:
            async with engine.begin() as conn:
                row = await conn.run_sync(
                    lambda c: c.execute(text("SELECT version_num FROM alembic_version")).fetchone()
                )
            current = row[0] if row else None

        alembic_cfg = Config(str(Path(db_session.__file__).parent / "alembic.ini"))
        head = db_session._get_alembic_head(alembic_cfg)

        async with engine.connect() as conn:
            missing = await conn.run_sync(db_session._find_missing_columns)
    finally:
        await engine.dispose()

    at_head = current is not None and head is not None and current == head

    table = Table(title="SOVA Database Status", show_header=True)
    table.add_column("Field", style="cyan")
    table.add_column("Value", style="white")
    current_label = "[red]unversioned (legacy)[/red]" if legacy else "[red]unknown[/red]"
    table.add_row("Current revision", current or current_label)
    table.add_row("Head revision", head or "[red]unknown[/red]")
    table.add_row("At head", "[green]yes[/green]" if at_head else "[yellow]no[/yellow]")
    table.add_row("Missing ORM columns", str(len(missing)) if missing else "none")
    console.print(table)

    if missing:
        console.print("[red]Schema drift: the database is missing columns the ORM declares:[/red]")
        for item in missing:
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
