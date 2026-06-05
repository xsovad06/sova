"""CLI commands: sova memory search, prune, export, import, shared."""

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


@app.command(name="export")
def export_cmd(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be exported.")] = False,
) -> None:
    """Export shareable memories to shared knowledge directory."""
    asyncio.run(_export(project_dir=project, dry_run=dry_run))


async def _export(*, project_dir: Path | None, dry_run: bool) -> None:
    from sova.config.loader import load_config
    from sova.db.session import init_db
    from sova.knowledge.sharing import export_memories

    resolved_dir = project_dir or Path.cwd()
    await init_db(resolved_dir)
    cfg = load_config(resolved_dir)

    result = await export_memories(
        cfg.shared_knowledge_path,
        dry_run=dry_run,
        categories=cfg.shared_knowledge_categories,
        repo=cfg.github_repo,
    )

    if dry_run:
        console.print(f"[yellow]Would export {result.exported} entries (skipped {result.skipped}):[/yellow]")
        for title in result.entries:
            console.print(f"  - {title}")
    else:
        console.print(f"[green]Exported {result.exported} entries to {cfg.shared_knowledge_path}[/green]")
        if result.skipped:
            console.print(f"[dim]Skipped {result.skipped} entries (unconfirmed or ineligible).[/dim]")


@app.command(name="import")
def import_cmd(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be imported.")] = False,
) -> None:
    """Import new entries from shared knowledge directory."""
    asyncio.run(_import(project_dir=project, dry_run=dry_run))


async def _import(*, project_dir: Path | None, dry_run: bool) -> None:
    from sova.config.loader import load_config
    from sova.db.session import init_db
    from sova.knowledge.sharing import import_memories

    resolved_dir = project_dir or Path.cwd()
    await init_db(resolved_dir)
    cfg = load_config(resolved_dir)

    result = await import_memories(
        cfg.shared_knowledge_path,
        dry_run=dry_run,
        ignored_hashes=cfg.ignored_shared_hashes,
    )

    if dry_run:
        msg = f"Would import {result.imported} entries (skipped {result.skipped}, ignored {result.ignored}):"
        console.print(f"[yellow]{msg}[/yellow]")
        for title in result.entries:
            console.print(f"  - {title}")
    else:
        console.print(f"[green]Imported {result.imported} entries from {cfg.shared_knowledge_path}[/green]")
        if result.skipped:
            console.print(f"[dim]Skipped {result.skipped} (already present).[/dim]")
        if result.ignored:
            console.print(f"[dim]Ignored {result.ignored} (in ignored_shared_hashes).[/dim]")


@app.command()
def shared(
    category: Annotated[Optional[str], typer.Option("--category", "-c", help="Filter by category.")] = None,
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """List shared knowledge entries."""
    asyncio.run(_shared(category=category, project_dir=project))


async def _shared(*, category: str | None, project_dir: Path | None) -> None:
    from sova.db.session import init_db

    resolved_dir = project_dir or Path.cwd()
    await init_db(resolved_dir)

    results = await memory_store.search(tier="shared", category=category)

    if not results:
        console.print("[yellow]No shared memories found.[/yellow]")
        return

    table = Table(title="Shared Knowledge", show_header=True)
    table.add_column("ID", style="cyan", width=5)
    table.add_column("Category", style="green")
    table.add_column("Title", style="white")
    table.add_column("Tags", style="dim")
    table.add_column("Repo", style="magenta")

    for mem in results:
        table.add_row(str(mem.id), mem.category, mem.title[:60], mem.tags[:30], mem.repo[:30])

    console.print(table)
    console.print(f"\n[bold]{len(results)} shared entry/entries.[/bold]")
