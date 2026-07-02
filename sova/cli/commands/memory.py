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
    semantic: Annotated[bool, typer.Option("--semantic", "-s", help="Use semantic similarity search.")] = False,
) -> None:
    """Search agent memory entries."""
    asyncio.run(_search(query=query, category=category, tier=tier, project_dir=project, semantic=semantic))


async def _search(
    *,
    query: str,
    category: str | None,
    tier: str | None,
    project_dir: Path | None,
    semantic: bool = False,
) -> None:
    from sova.db.session import init_db

    resolved_dir = project_dir or Path.cwd()
    await init_db(resolved_dir)

    if semantic:
        scored_results = await memory_store.semantic_search(query=query, category=category, tier=tier)
        if not scored_results:
            console.print("[yellow]No memories found.[/yellow]")
            return

        table = Table(title=f"Semantic Search: {query}", show_header=True)
        table.add_column("ID", style="cyan", width=5)
        table.add_column("Score", style="yellow", width=6)
        table.add_column("Category", style="green")
        table.add_column("Title", style="white")
        table.add_column("Tier", style="magenta")

        for mem, score in scored_results:
            table.add_row(str(mem.id), f"{score:.3f}", mem.category, mem.title[:60], mem.tier)

        console.print(table)
        console.print(f"\n[bold]{len(scored_results)} result(s).[/bold]")
        return

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


@app.command(name="backfill-embeddings")
def backfill_embeddings(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Compute embeddings for all memories that don't have one yet.

    Requires the sentence-transformers package (~80MB model download on first run).
    """
    asyncio.run(_backfill_embeddings(project_dir=project))


async def _backfill_embeddings(*, project_dir: Path | None) -> None:
    from sova.db.session import init_db

    resolved_dir = project_dir or Path.cwd()
    await init_db(resolved_dir)

    from sova.knowledge.embeddings import embed_text, is_available

    if not is_available():
        console.print("[red]sentence-transformers is not installed.[/red]")
        console.print("Install with: pip install sentence-transformers")
        raise typer.Exit(1)

    from sqlalchemy import select

    from sova.db.models import Memory
    from sova.db.session import get_session

    async with await get_session() as session:
        async with session.begin():
            result = await session.execute(select(Memory).where(Memory.embedding.is_(None)))
            memories = list(result.scalars().all())

    if not memories:
        console.print("[green]All memories already have embeddings.[/green]")
        return

    console.print(f"Backfilling embeddings for {len(memories)} memories...")

    # Compute all embeddings, then batch-write to DB
    updates: list[tuple[int, list[float]]] = []
    for mem in memories:
        vector = embed_text(f"{mem.title} {mem.content}")
        if vector is not None:
            updates.append((mem.id, vector))

    if updates:
        from sqlalchemy import update

        from sova.db.models import Memory as MemoryModel
        from sova.db.session import get_session

        async with await get_session() as session:
            async with session.begin():
                for mem_id, vector in updates:
                    await session.execute(update(MemoryModel).where(MemoryModel.id == mem_id).values(embedding=vector))

    console.print(f"[green]Updated {len(updates)}/{len(memories)} memories with embeddings.[/green]")


@app.command()
def health(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Compute and display health scores for all active memories."""
    asyncio.run(_health(project_dir=project))


async def _health(*, project_dir: Path | None) -> None:
    from sova.db.session import init_db
    from sova.knowledge.lifecycle import compute_health_scores

    resolved_dir = project_dir or Path.cwd()
    await init_db(resolved_dir)

    result = await compute_health_scores()
    console.print(f"[green]Computed health scores for {result.updated}/{result.total} memories.[/green]")

    # Show top/bottom memories by score
    from sqlalchemy import select

    from sova.db.models import Memory
    from sova.db.session import get_session

    async with await get_session() as session:
        async with session.begin():
            stmt = (
                select(Memory)
                .where(Memory.superseded_by.is_(None), Memory.archived.is_(False), Memory.health_score.isnot(None))
                .order_by(Memory.health_score.desc())
            )
            rows = await session.execute(stmt)
            memories = list(rows.scalars().all())

    if not memories:
        console.print("[yellow]No scored memories found.[/yellow]")
        return

    table = Table(title="Memory Health Scores", show_header=True)
    table.add_column("ID", style="cyan", width=5)
    table.add_column("Score", style="yellow", width=7)
    table.add_column("Category", style="green")
    table.add_column("Title", style="white")
    table.add_column("Retrievals", style="magenta", width=10)

    for mem in memories[:20]:
        score_str = f"{float(mem.health_score):.4f}" if mem.health_score is not None else "N/A"
        table.add_row(str(mem.id), score_str, mem.category, mem.title[:50], str(mem.retrieval_count or 0))

    console.print(table)
    console.print(f"\n[bold]{len(memories)} total scored memories.[/bold]")


@app.command()
def consolidate(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show candidates without merging.")] = False,
) -> None:
    """Find and merge duplicate memory clusters."""
    asyncio.run(_consolidate(project_dir=project, dry_run=dry_run))


async def _consolidate(*, project_dir: Path | None, dry_run: bool) -> None:
    from sova.db.session import init_db
    from sova.knowledge.lifecycle import consolidate_cluster, find_consolidation_candidates

    resolved_dir = project_dir or Path.cwd()
    await init_db(resolved_dir)

    clusters = await find_consolidation_candidates()

    if not clusters:
        console.print("[green]No consolidation candidates found.[/green]")
        return

    if dry_run:
        console.print(f"[yellow]Found {len(clusters)} cluster(s) to consolidate:[/yellow]")
        for i, cluster in enumerate(clusters, 1):
            console.print(f"\n  Cluster {i} ({len(cluster.member_ids)} members):")
            for title in cluster.titles:
                console.print(f"    - {title[:70]}")
        return

    merged = 0
    for cluster in clusters:
        new_id = await consolidate_cluster(cluster, cwd=resolved_dir)
        if new_id is not None:
            console.print(f"[green]Merged {len(cluster.member_ids)} memories into #{new_id}[/green]")
            merged += 1

    console.print(f"\n[bold]Consolidated {merged}/{len(clusters)} clusters.[/bold]")


@app.command()
def archive(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    days: Annotated[
        int, typer.Option("--days", "-d", help="Archive memories older than N days with 0 confirmations.")
    ] = 30,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be archived.")] = False,
) -> None:
    """Archive low-value memories (soft-delete)."""
    asyncio.run(_archive(project_dir=project, archive_days=days, dry_run=dry_run))


async def _archive(*, project_dir: Path | None, archive_days: int, dry_run: bool) -> None:
    from sova.db.session import init_db
    from sova.knowledge.lifecycle import auto_archive, find_archive_candidates, flag_stale_memories

    resolved_dir = project_dir or Path.cwd()
    await init_db(resolved_dir)

    stale_ids = await flag_stale_memories()

    if dry_run:
        console.print(f"[yellow]Found {len(stale_ids)} stale memories (unretrieved for 60+ days).[/yellow]")

        candidates = await find_archive_candidates(archive_days=archive_days)
        msg = f"Would archive {len(candidates)} memories (0 confirmations, >{archive_days} days old):"
        console.print(f"[yellow]{msg}[/yellow]")
        for mem in candidates[:20]:
            console.print(f"  - [{mem.id}] {mem.title[:60]}")
        if len(candidates) > 20:
            console.print(f"  ... and {len(candidates) - 20} more")
        return

    archived = await auto_archive(archive_days=archive_days)
    console.print(f"[green]Archived {archived} low-value memories.[/green]")
    if stale_ids:
        console.print(f"[dim]{len(stale_ids)} stale memories detected (use --dry-run to inspect).[/dim]")


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
