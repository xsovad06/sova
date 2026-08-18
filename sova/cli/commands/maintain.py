"""CLI command: sova maintain -- sweep and auto-merge Dependabot PRs."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from sova.utils.logging import get_logger

console = Console(stderr=True)
log = get_logger(component="cli.maintain")

_ProjectOpt = Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")]
_DryRunOpt = Annotated[bool, typer.Option("--dry-run", help="Show what would happen without merging.")]


def maintain(project: _ProjectOpt = None, dry_run: _DryRunOpt = False) -> None:
    """Sweep open Dependabot PRs: auto-merge if CI passes, close if CI fails."""
    asyncio.run(_maintain(project_dir=project, dry_run=dry_run))


async def _maintain(*, project_dir: Path | None, dry_run: bool) -> None:
    from sova.config.loader import load_config
    from sova.supervisor.dependabot import (
        MergeResult,
        classify_dependabot_prs,
        sweep_dependabot_prs,
    )

    resolved_dir = (project_dir or Path.cwd()).resolve()
    config = load_config(resolved_dir)

    if not config.github_repo:
        console.print("[red]No github_repo configured in sova.toml[/red]")
        raise typer.Exit(code=1)

    if dry_run:
        console.print(f"[bold]Inspecting Dependabot PRs for {config.github_repo}...[/bold]")

        from sova.git.pr import list_open_prs

        try:
            all_prs = await list_open_prs(repo=config.github_repo, github_user=config.github_user)
        except OSError:
            console.print("[red]Failed to reach GitHub CLI (gh not found or not executable).[/red]")
            raise typer.Exit(code=1) from None
        except Exception:
            log.warning("dependabot.list_prs.error", exc_info=True)
            console.print("[red]Failed to fetch PRs from GitHub.[/red]")
            raise typer.Exit(code=1) from None

        classified = classify_dependabot_prs(all_prs, config.dependabot)

        if not classified:
            console.print("No open Dependabot PRs found.")
            return

        table = Table(title="Dependabot PRs (dry run)")
        table.add_column("PR", style="cyan")
        table.add_column("Title")
        table.add_column("Group", style="yellow")
        table.add_column("Action", style="green")

        for dpr, skip_reason in classified:
            if skip_reason:
                action = f"skip: {skip_reason}"
                style = "dim"
            else:
                action = "would process (check CI -> merge/close)"
                style = ""
            table.add_row(
                f"#{dpr.number}",
                dpr.title,
                dpr.group or "(ungrouped)",
                action,
                style=style,
            )

        console.print(table)
        return

    console.print(f"[bold]Sweeping Dependabot PRs for {config.github_repo}...[/bold]")

    results: list[MergeResult] = await sweep_dependabot_prs(
        project_dir=resolved_dir,
        repo=config.github_repo,
        github_user=config.github_user,
        config=config.dependabot,
        notification_config=config.notification,
    )

    if not results:
        console.print("No open Dependabot PRs found.")
        return

    table = Table(title="Dependabot Sweep Results")
    table.add_column("PR", style="cyan")
    table.add_column("Title")
    table.add_column("Result", style="green")
    table.add_column("Detail")

    _ACTION_STYLES = {
        "merged": "green",
        "closed": "yellow",
        "skipped": "dim",
        "waiting": "blue",
        "error": "red",
    }

    for r in results:
        style = _ACTION_STYLES.get(r.action, "")
        table.add_row(
            f"#{r.pr_number}",
            r.title,
            r.action,
            r.reason,
            style=style,
        )

    console.print(table)

    merged = sum(1 for r in results if r.action == "merged")
    closed = sum(1 for r in results if r.action == "closed")
    skipped = sum(1 for r in results if r.action == "skipped")
    waiting = sum(1 for r in results if r.action == "waiting")
    errors = sum(1 for r in results if r.action == "error")

    console.print(
        f"\n[bold]Summary:[/bold] {merged} merged, {closed} closed, {skipped} skipped,"
        f" {waiting} waiting, {errors} errors"
    )

    if errors:
        raise typer.Exit(code=1)
