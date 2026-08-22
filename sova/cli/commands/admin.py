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

    async with await get_session() as session:
        async with session.begin():
            result = await session.execute(select(TaskRun).order_by(TaskRun.started_at.desc()).limit(10))
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

    async with await get_session() as session:
        async with session.begin():
            total_result = await session.execute(select(func.sum(CostRecord.cost_usd)))
            total_cost = total_result.scalar() or Decimal("0")

            model_result = await session.execute(
                select(CostRecord.model, func.sum(CostRecord.cost_usd), func.count())
                .group_by(CostRecord.model)
                .order_by(func.sum(CostRecord.cost_usd).desc())
            )
            by_model = list(model_result.all())

            recent_result = await session.execute(select(CostRecord).order_by(CostRecord.recorded_at.desc()).limit(10))
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


def verify_run(
    run_id: Annotated[int, typer.Argument(help="Run ID to verify.")],
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Verify the hash chain integrity of a run journal."""
    from sova.core.journal import RunJournal

    resolved_dir = project or Path.cwd()
    result = RunJournal.verify(resolved_dir, run_id)

    if result.valid:
        console.print(f"[green]Run {run_id}: journal integrity verified ({result.event_count} events).[/green]")
    else:
        console.print(f"[red]Run {run_id}: journal integrity check FAILED.[/red]")
        for error in result.errors:
            console.print(f"  [red]{error}[/red]")
        raise typer.Exit(code=1)


def cleanup(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be cleaned up.")] = False,
    logs: Annotated[bool, typer.Option("--logs", help="Also clean up old agent output lines from the DB.")] = False,
    all_: Annotated[bool, typer.Option("--all", help="Run issue-aware GC (worktrees, branches, stashes).")] = False,
) -> None:
    """Remove stale worktrees, optionally old output logs, and with --all branches for closed issues."""
    asyncio.run(_cleanup(project_dir=project, dry_run=dry_run, clean_logs=logs, run_all=all_))


async def _cleanup(*, project_dir: Path | None, dry_run: bool, clean_logs: bool, run_all: bool = False) -> None:
    from sova.utils.shell import run

    resolved_dir = project_dir or Path.cwd()

    # List worktrees
    result = await run("git", "worktree", "list", "--porcelain", cwd=resolved_dir)
    if not result.success:
        console.print("[red]Failed to list worktrees.[/red]")
        raise typer.Exit(code=1)

    worktrees = _parse_worktree_output(result.stdout)
    stale = _filter_stale_worktrees(worktrees)

    if not stale:
        console.print("[green]No stale worktrees found.[/green]")
    elif dry_run:
        _preview_stale_worktrees(stale)
    else:
        await _remove_worktrees(stale, resolved_dir)

    if run_all:
        from sova.git.worktree import cleanup_by_issue_state

        gc = await cleanup_by_issue_state(project_dir=resolved_dir, dry_run=dry_run)
        if dry_run:
            console.print(
                f"[yellow]Would remove {gc.worktrees_removed} worktree(s)"
                f" and {gc.branches_removed} branch(es) for closed issues.[/yellow]"
            )
        elif gc.worktrees_removed or gc.branches_removed:
            console.print(
                f"[green]Removed {gc.worktrees_removed} worktree(s)"
                f" and {gc.branches_removed} branch(es) for closed issues.[/green]"
            )
        else:
            console.print("[green]No stale worktrees or branches for closed issues.[/green]")
        if gc.stashes_found:
            console.print(f"[yellow]Found {len(gc.stashes_found)} stash(es) (not auto-dropped):[/yellow]")
            for stash in gc.stashes_found:
                console.print(f"  {stash}")
        for err in gc.errors:
            console.print(f"[red]{err}[/red]")
        if gc.errors:
            raise typer.Exit(code=1)

    if clean_logs and not dry_run:
        from sova.config.loader import load_config
        from sova.core.output import cleanup_old_output
        from sova.db.session import init_db

        await init_db(resolved_dir)
        cfg = load_config(resolved_dir)
        deleted = await cleanup_old_output(resolved_dir, cfg.output.retention_days)
        console.print(f"[green]Cleaned up {deleted} old output line(s).[/green]")


def _parse_worktree_output(stdout: str) -> list[dict[str, str]]:
    """Parse `git worktree list --porcelain` output into a list of worktree dicts."""
    worktrees: list[dict[str, str]] = []
    current_wt: dict[str, str] = {}
    for line in stdout.strip().split("\n"):
        if line.startswith("worktree "):
            if current_wt:
                worktrees.append(current_wt)
            current_wt = {"path": line.split(" ", 1)[1]}
        elif line.startswith("branch "):
            current_wt["branch"] = line.split(" ", 1)[1]
        elif not line.strip() and current_wt:
            worktrees.append(current_wt)
            current_wt = {}

    if current_wt:
        worktrees.append(current_wt)

    return worktrees


def _filter_stale_worktrees(worktrees: list[dict[str, str]]) -> list[dict[str, str]]:
    """Filter to SOVA-managed worktrees (branches with feat/, fix/, refactor/ prefixes)."""
    sova_prefixes = ("refs/heads/feat/", "refs/heads/fix/", "refs/heads/refactor/")
    return [wt for wt in worktrees if wt.get("branch", "").startswith(sova_prefixes)]


def _preview_stale_worktrees(stale: list[dict[str, str]]) -> None:
    """Show what would be removed in a dry run."""
    console.print(f"[yellow]Would remove {len(stale)} worktree(s):[/yellow]")
    for wt in stale:
        console.print(f"  - {wt['path']} ({wt.get('branch', 'detached')})")


async def _remove_worktrees(stale: list[dict[str, str]], resolved_dir: Path) -> None:
    """Remove stale worktrees and report results."""
    from sova.utils.shell import run

    removed = 0
    for wt in stale:
        result = await run("git", "worktree", "remove", "--force", wt["path"], cwd=resolved_dir)
        if result.success:
            removed += 1
            console.print(f"  Removed: {wt['path']}")
        else:
            console.print(f"  [red]Failed: {wt['path']} -- {result.stderr.strip()}[/red]")

    console.print(f"\n[green]Removed {removed} worktree(s).[/green]")
