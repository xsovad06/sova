"""analyze-failures command: Display accurate failure rate breakdown."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Optional

import typer

from sova.dashboard.services import failure_analysis_service
from sova.db.session import get_session, init_db

app = typer.Typer(help="Analyze failure rates with accurate categorization")


@app.command()
def main(
    project_slug: Annotated[Optional[str], typer.Option("--project", "-p", help="Project slug to analyze.")] = None,
) -> None:
    """Display accurate failure rate breakdown.

    Excludes operational failures (user dismissals, stale recovery)
    from the true pipeline failure rate.
    """
    asyncio.run(_analyze(project_slug))


async def _analyze(project_slug: str | None) -> None:
    """Run failure analysis and display results."""
    await init_db(Path.cwd())
    async with await get_session() as session:
        breakdown = await failure_analysis_service.analyze_failures(session, project_slug)
        categories = await failure_analysis_service.get_failure_category_counts(session, project_slug)

    # Display summary
    typer.echo("\n=== Failure Rate Analysis ===\n")
    typer.echo(f"Total runs: {breakdown.total_runs}")
    if breakdown.total_runs:
        typer.echo(f"Done: {breakdown.done_runs} ({breakdown.done_runs / breakdown.total_runs * 100:.1f}%)")
        typer.echo(f"Failed (all): {breakdown.failed_runs} ({breakdown.failed_runs / breakdown.total_runs * 100:.1f}%)")
    else:
        typer.echo(f"Done: {breakdown.done_runs}")
        typer.echo(f"Failed (all): {breakdown.failed_runs}")
    typer.echo(f"Interrupted: {breakdown.interrupted_runs}")
    typer.echo(f"Rejected: {breakdown.rejected_runs}")
    typer.echo()

    # Operational vs pipeline failures
    typer.echo(f"Operational failures (dismissed, stale): {breakdown.operational_failures}")
    typer.echo(f"True pipeline failures: {breakdown.true_pipeline_failures} ({breakdown.pipeline_failure_rate:.1f}%)")
    typer.echo()

    # Failure categories
    typer.echo("=== Failure Categories ===\n")
    for category, count in categories.items():
        if count > 0:
            label = category.replace("_", " ").title()
            typer.echo(f"{label}: {count}")
    typer.echo()

    # Top failing steps
    if breakdown.top_step_failures:
        typer.echo("=== Top Failing Steps ===\n")
        for step, count in breakdown.top_step_failures[:5]:
            typer.echo(f"{step}: {count}")
        typer.echo()

    # Top error patterns
    if breakdown.top_error_patterns:
        typer.echo("=== Top Error Patterns ===\n")
        for error, count in breakdown.top_error_patterns[:5]:
            display_error = error[:80] + "..." if len(error) > 80 else error
            typer.echo(f"[{count}x] {display_error}")
