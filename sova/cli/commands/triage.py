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

VALID_MODES = ("full", "comment", "dry_run")
_MODE_ALIASES = {"dry-run": "dry_run", "dryrun": "dry_run"}


def _validate_mode(mode: str | None) -> str | None:
    if mode is None:
        return None
    normalized = _MODE_ALIASES.get(mode, mode)
    if normalized not in VALID_MODES:
        console.print(f"[red]Invalid mode: {mode}. Choose from: {', '.join(VALID_MODES)}[/red]")
        raise typer.Exit(1)
    return normalized


def triage(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    issue: Annotated[Optional[str], typer.Option("--issue", "-i", help="Triage a single issue by number.")] = None,
    label: Annotated[Optional[bool], typer.Option("--label/--no-label", help="Apply suitability labels.")] = None,
    mode: Annotated[Optional[str], typer.Option("--mode", "-m", help="Triage mode: full, comment, dry_run.")] = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Bypass state checks.")] = False,
) -> None:
    """Assess backlog issues for agent suitability and classify them."""
    validated_mode = _validate_mode(mode)
    asyncio.run(
        _triage(project_dir=project, issue=issue, label_override=label, mode_override=validated_mode, force=force),
    )


async def _triage(
    *,
    project_dir: Path | None,
    issue: str | None,
    label_override: bool | None,
    mode_override: str | None,
    force: bool,
) -> None:
    from sova.db.session import init_db

    resolved_dir = project_dir or Path.cwd()
    config = load_config(resolved_dir)
    await init_db(resolved_dir)

    adapter = create_adapter(config)
    role = TriageRole()

    triage_cfg = config.triage
    overrides: dict[str, object] = {}
    if mode_override is not None:
        overrides["mode"] = mode_override
    if label_override is not None:
        overrides["auto_label"] = label_override
    if overrides:
        triage_cfg = triage_cfg.model_copy(update=overrides)

    if issue:
        tasks = [await adapter.get_task(issue)]
    else:
        filters = TaskFilters(state="open")
        all_tasks = await adapter.list_tasks(filters)
        tasks = [t for t in all_tasks if t.state == TaskState.BACKLOG]

    if not tasks:
        console.print("[yellow]No backlog issues found to triage.[/yellow]")
        return

    mode_label = f" (mode: {triage_cfg.mode})" if triage_cfg.mode != "full" else ""
    console.print(f"[bold]Triaging {len(tasks)} issue(s){mode_label}...[/bold]\n")

    results = []
    for task in tasks:
        assessment = role.heuristic_assess(task, triage_cfg)

        if triage_cfg.mode != "dry_run":
            if triage_cfg.auto_label:
                label_name = role.resolve_label(assessment.suitability, triage_cfg)
                if label_name:
                    await adapter.add_label(task.id, label_name)

            assessment_section = role._build_assessment_comment(task, assessment)
            if triage_cfg.mode == "comment":
                await adapter.post_comment(task.id, assessment_section)
            elif triage_cfg.write_body:
                updated_body = (task.body or "").rstrip() + "\n\n" + assessment_section
                await adapter.edit_body(task.id, updated_body)

            if triage_cfg.write_transition and not force and task.state in role.allowed_input_states:
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
