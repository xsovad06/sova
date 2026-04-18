"""CLI command: sova run <issue> -- trigger the Developer workflow."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

console = Console(stderr=True)


def run_issue(
    issue: Annotated[str, typer.Argument(help="Issue number to develop.")],
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Skip pipeline gate checks.")] = False,
) -> None:
    """Run the full developer workflow for a single issue."""
    asyncio.run(_run_workflow(issue, project_dir=project, force=force))


async def _run_workflow(issue: str, *, project_dir: Path | None, force: bool) -> None:
    from sova.adapters import create_adapter
    from sova.config.loader import load_config
    from sova.core.context import ExecutionContext
    from sova.core.workflow import WorkflowEngine, build_pipeline
    from sova.db.session import init_db

    resolved_dir = project_dir or Path.cwd()
    config = load_config(resolved_dir)

    await init_db(resolved_dir)

    adapter = create_adapter(config.task_source.type, config.github_repo)

    ctx = ExecutionContext(
        project_dir=resolved_dir,
        config=config,
        adapter=adapter,
        issue_number=issue,
        role="developer",
        force=force,
    )

    console.print(f"[bold]Starting developer workflow for issue #{issue}[/bold]")

    steps = build_pipeline()
    engine = WorkflowEngine(steps=steps, ctx=ctx)
    result = await engine.run()

    if result.success:
        console.print(f"[green]Workflow completed successfully. PR: #{ctx.pr_number}[/green]")
    else:
        console.print(f"[red]Workflow ended: {result.final_status} -- {result.error}[/red]")
        raise typer.Exit(code=1)
