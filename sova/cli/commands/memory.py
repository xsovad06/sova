"""CLI commands: sova memory search, sova memory prune."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from sova.knowledge import memory as memory_store

console = Console(stderr=True)

app = typer.Typer(name="memory", help="Manage agent memory.", no_args_is_help=True)


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Text to search for.")],
    category: Annotated[Optional[str], typer.Option("--category", "-c", help="Filter by category.")] = None,
    tier: Annotated[Optional[str], typer.Option("--tier", "-t", help="Filter by tier.")] = None,
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Search agent memory entries."""
    asyncio.run(_search(query=query, category=category, tier=tier, project_dir=project))


async def _search(
    *,
    query: str,
    category: str | None,
    tier: str | None,
    project_dir: Path | None,
) -> None:
    from sova.db.session import init_db

    resolved_dir = project_dir or Path.cwd()
    await init_db(resolved_dir)

    results = await memory_store.search(query=query, category=category, tier=tier)

    if not results:
        console.print("[yellow]No memories found.[/yellow]")
        return

    table = Table(title=f"Memory Search: {query}", show_header=True)
    table.add_column("ID", style="cyan", width=5)
    table.add_column("Category", style="green")
    table.add_column("Title", style="white")
    table.add_column("Tier", style="magenta")
    table.add_column("Tags", style="dim")

    for mem in results:
        table.add_row(str(mem.id), mem.category, mem.title[:60], mem.tier, mem.tags[:30])

    console.print(table)
    console.print(f"\n[bold]{len(results)} result(s).[/bold]")


@app.command()
def prune(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be pruned.")] = False,
) -> None:
    """Remove superseded and stale memory entries."""
    asyncio.run(_prune(project_dir=project, dry_run=dry_run))


async def _prune(*, project_dir: Path | None, dry_run: bool) -> None:
    from sova.db.session import init_db

    resolved_dir = project_dir or Path.cwd()
    await init_db(resolved_dir)

    # Find superseded entries
    superseded = await memory_store.search(include_superseded=True)
    to_prune = [m for m in superseded if m.superseded_by is not None]

    if not to_prune:
        console.print("[green]No stale memories to prune.[/green]")
        return

    if dry_run:
        console.print(f"[yellow]Would prune {len(to_prune)} superseded entries:[/yellow]")
        for mem in to_prune:
            console.print(f"  - [{mem.id}] {mem.title} (superseded by #{mem.superseded_by})")
        return

    pruned = 0
    for mem in to_prune:
        if await memory_store.delete(mem.id):
            pruned += 1

    console.print(f"[green]Pruned {pruned} superseded memory entries.[/green]")
