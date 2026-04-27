"""CLI command: sova triage -- assess issues for agent suitability."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from sova.adapters import create_adapter
from sova.adapters.base import TaskFilters, TaskState
from sova.config.loader import load_config
from sova.roles.triage import TriageRole

console = Console(stderr=True)


def triage(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    issue: Annotated[Optional[str], typer.Option("--issue", "-i", help="Triage a single issue by number.")] = None,
    label: Annotated[bool, typer.Option("--label/--no-label", help="Apply suitability labels.")] = True,
    force: Annotated[bool, typer.Option("--force", "-f", help="Bypass state checks.")] = False,
) -> None:
    """Assess backlog issues for agent suitability and classify them."""
    asyncio.run(_triage(project_dir=project, issue=issue, apply_labels=label, force=force))


async def _triage(
    *,
    project_dir: Path | None,
    issue: str | None,
    apply_labels: bool,
    force: bool,
) -> None:
    from sova.db.session import init_db

    resolved_dir = project_dir or Path.cwd()
    config = load_config(resolved_dir)
    await init_db(resolved_dir)

    ts = config.task_source
    adapter = create_adapter(ts.type, config.github_repo, config.github_user, ts.github_project_number)
    role = TriageRole()

    if issue:
        # Triage a single issue
        tasks = [await adapter.get_task(issue)]
    else:
        # Triage all BACKLOG issues
        filters = TaskFilters(state="open")
        all_tasks = await adapter.list_tasks(filters)
        tasks = [t for t in all_tasks if t.state == TaskState.BACKLOG]

    if not tasks:
        console.print("[yellow]No backlog issues found to triage.[/yellow]")
        return

    console.print(f"[bold]Triaging {len(tasks)} issue(s)...[/bold]\n")

    results = []
    for task in tasks:
        assessment = await role.assess_task(task)

        if apply_labels:
            label_name = role.SUITABILITY_LABELS[assessment.suitability]
            await adapter.add_label(task.id, label_name)

            comment = role._build_assessment_comment(task, assessment)
            await adapter.post_comment(task.id, comment)

            if not force and task.state in role.allowed_input_states:
                await adapter.transition_state(task.id, TaskState.TRIAGED)

        results.append((task, assessment))

    # Display results table
    table = Table(title="Triage Results", show_header=True)
    table.add_column("Issue", style="cyan")
    table.add_column("Title", style="white")
    table.add_column("Suitability", style="green")
    table.add_column("Confidence", style="yellow")
    table.add_column("Complexity", style="magenta")

    for task, assessment in results:
        suitability_style = {
            "ready": "green",
            "needs_spec": "yellow",
            "needs_research": "blue",
            "human_only": "red",
        }.get(assessment.suitability, "white")

        table.add_row(
            f"#{task.id}",
            task.title[:50],
            f"[{suitability_style}]{assessment.suitability}[/{suitability_style}]",
            f"{assessment.confidence:.0%}",
            assessment.estimated_complexity,
        )

    console.print(table)
    console.print(f"\n[bold]Triaged {len(results)} issue(s).[/bold]")
