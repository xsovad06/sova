"""CLI commands: sova status, sova costs, sova cleanup."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import func, select

console = Console(stderr=True)


def status(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Show agent status and recent task runs."""
    asyncio.run(_status(project_dir=project))


async def _status(*, project_dir: Path | None) -> None:
    from sova.db.models import TaskRun
    from sova.db.session import get_session, init_db

    resolved_dir = project_dir or Path.cwd()
    await init_db(resolved_dir)

    session = await get_session()
    async with session.begin():
        result = await session.execute(
            select(TaskRun).order_by(TaskRun.started_at.desc()).limit(10)
        )
        runs = list(result.scalars().all())

    if not runs:
        console.print("[yellow]No task runs found.[/yellow]")
        return

    table = Table(title="Recent Task Runs", show_header=True)
    table.add_column("ID", style="cyan", width=5)
    table.add_column("Issue", style="white")
    table.add_column("Role", style="green")
    table.add_column("Status", style="yellow")
    table.add_column("Cost", style="magenta")
    table.add_column("Started", style="dim")

    for run in runs:
        status_style = {
            "done": "green",
            "failed": "red",
            "pending": "yellow",
            "running": "blue",
        }.get(run.status, "white")

        table.add_row(
            str(run.id),
            f"#{run.issue_number}",
            run.role,
            f"[{status_style}]{run.status}[/{status_style}]",
            f"${run.total_cost_usd:.4f}",
            run.started_at.strftime("%Y-%m-%d %H:%M") if run.started_at else "",
        )

    console.print(table)


def costs(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Show cost tracking summary."""
    asyncio.run(_costs(project_dir=project))


async def _costs(*, project_dir: Path | None) -> None:
    from sova.db.models import CostRecord
    from sova.db.session import get_session, init_db

    resolved_dir = project_dir or Path.cwd()
    await init_db(resolved_dir)

    session = await get_session()
    async with session.begin():
        # Total cost
        total_result = await session.execute(select(func.sum(CostRecord.cost_usd)))
        total_cost = total_result.scalar() or Decimal("0")

        # Cost by model
        model_result = await session.execute(
            select(CostRecord.model, func.sum(CostRecord.cost_usd), func.count())
            .group_by(CostRecord.model)
            .order_by(func.sum(CostRecord.cost_usd).desc())
        )
        by_model = list(model_result.all())

        # Recent costs
        recent_result = await session.execute(
            select(CostRecord).order_by(CostRecord.recorded_at.desc()).limit(10)
        )
        recent = list(recent_result.scalars().all())

    console.print(f"\n[bold]Total cost: ${total_cost:.4f}[/bold]\n")

    if by_model:
        model_table = Table(title="Cost by Model", show_header=True)
        model_table.add_column("Model", style="cyan")
        model_table.add_column("Total Cost", style="green")
        model_table.add_column("Invocations", style="yellow")

        for model, cost, count in by_model:
            model_table.add_row(model, f"${cost:.4f}", str(count))

        console.print(model_table)

    if recent:
        recent_table = Table(title="\nRecent Invocations", show_header=True)
        recent_table.add_column("Phase", style="cyan")
        recent_table.add_column("Issue", style="white")
        recent_table.add_column("Model", style="green")
        recent_table.add_column("Cost", style="magenta")
        recent_table.add_column("Tokens", style="dim")

        for rec in recent:
            tokens = f"{rec.input_tokens}in/{rec.output_tokens}out"
            recent_table.add_row(rec.phase, f"#{rec.issue}", rec.model, f"${rec.cost_usd:.4f}", tokens)

        console.print(recent_table)


def cleanup(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be cleaned up.")] = False,
) -> None:
    """Remove stale worktrees."""
    asyncio.run(_cleanup(project_dir=project, dry_run=dry_run))


async def _cleanup(*, project_dir: Path | None, dry_run: bool) -> None:
    from sova.utils.shell import run

    resolved_dir = project_dir or Path.cwd()

    # List worktrees
    result = await run("git", "worktree", "list", "--porcelain", cwd=resolved_dir)
    if not result.success:
        console.print("[red]Failed to list worktrees.[/red]")
        raise typer.Exit(code=1)

    worktrees = []
    current_wt: dict[str, str] = {}
    for line in result.stdout.strip().split("\n"):
        if line.startswith("worktree "):
            if current_wt:
                worktrees.append(current_wt)
            current_wt = {"path": line.split(" ", 1)[1]}
        elif line.startswith("branch "):
            current_wt["branch"] = line.split(" ", 1)[1]
        elif not line.strip():
            if current_wt:
                worktrees.append(current_wt)
                current_wt = {}

    if current_wt:
        worktrees.append(current_wt)

    # Filter to SOVA-managed worktrees (branches with feat/, fix/, refactor/ prefixes)
    sova_prefixes = ("refs/heads/feat/", "refs/heads/fix/", "refs/heads/refactor/")
    stale = [wt for wt in worktrees if wt.get("branch", "").startswith(sova_prefixes)]

    if not stale:
        console.print("[green]No stale worktrees found.[/green]")
        return

    if dry_run:
        console.print(f"[yellow]Would remove {len(stale)} worktree(s):[/yellow]")
        for wt in stale:
            console.print(f"  - {wt['path']} ({wt.get('branch', 'detached')})")
        return

    removed = 0
    for wt in stale:
        result = await run("git", "worktree", "remove", "--force", wt["path"], cwd=resolved_dir)
        if result.success:
            removed += 1
            console.print(f"  Removed: {wt['path']}")
        else:
            console.print(f"  [red]Failed: {wt['path']} -- {result.stderr.strip()}[/red]")

    console.print(f"\n[green]Removed {removed} worktree(s).[/green]")
