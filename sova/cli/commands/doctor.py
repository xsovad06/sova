"""CLI command: sova doctor (validate prerequisites and environment)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from sova.utils.diagnostics import DiagnosticCheck, run_all_checks

console = Console(stderr=True)


def doctor(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Check prerequisites and environment health."""
    asyncio.run(_doctor(project))


async def _doctor(project: Path | None) -> None:
    project_dir = (project or Path.cwd()).resolve()
    checks = await run_all_checks(project_dir)
    _render_results(checks)


def _render_results(checks: list[DiagnosticCheck]) -> None:
    """Render check results as a Rich table and exit appropriately."""
    table = Table(title="SOVA Doctor", show_header=True)
    table.add_column("Check", style="cyan")
    table.add_column("Status")
    table.add_column("Detail", style="dim")

    has_required_failure = False
    for check in checks:
        if check.passed:
            status = "[green]OK[/green]"
        elif check.required:
            status = "[red]FAIL[/red]"
            has_required_failure = True
        else:
            status = "[yellow]WARN[/yellow]"
        table.add_row(check.name, status, check.detail)

    console.print(table)

    if has_required_failure:
        console.print("\n[red]Some required checks failed. Fix the issues above before using SOVA.[/red]")
        raise typer.Exit(code=1)
    else:
        console.print("\n[green]All required checks passed.[/green]")
