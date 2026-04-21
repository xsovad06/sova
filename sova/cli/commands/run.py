"""CLI commands: sova run, sova watch, sova parallel."""

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
    role: Annotated[Optional[str], typer.Option("--role", "-r", help="Force a specific role.")] = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Skip pipeline gate checks.")] = False,
) -> None:
    """Run the agent workflow for a single issue."""
    asyncio.run(_run_workflow(issue, project_dir=project, role_name=role, force=force))


async def _run_workflow(issue: str, *, project_dir: Path | None, role_name: str | None, force: bool) -> None:
    from sova.adapters import create_adapter
    from sova.config.loader import load_config
    from sova.core.context import ExecutionContext
    from sova.db.session import init_db
    from sova.roles.dispatcher import dispatch

    resolved_dir = project_dir or Path.cwd()
    config = load_config(resolved_dir)

    await init_db(resolved_dir)

    adapter = create_adapter(config.task_source.type, config.github_repo, config.github_user)

    ctx = ExecutionContext(
        project_dir=resolved_dir,
        config=config,
        adapter=adapter,
        issue_number=issue,
        role=role_name or config.roles.default,
        force=force,
    )

    console.print(f"[bold]Starting workflow for issue #{issue}[/bold]")

    role, result = await dispatch(ctx, role_name=role_name, config=config.roles)

    if result.success:
        console.print(f"[green]Workflow completed ({role.name}): {result.summary}[/green]")
    else:
        console.print(f"[red]Workflow failed ({role.name}): {result.error}[/red]")
        raise typer.Exit(code=1)


def watch(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Skip pipeline gate checks.")] = False,
) -> None:
    """Continuous autonomous mode -- poll for issues and process them."""
    asyncio.run(_watch(project_dir=project, force=force))


async def _watch(*, project_dir: Path | None, force: bool) -> None:
    from sova.adapters import create_adapter
    from sova.adapters.base import TaskFilters, TaskState
    from sova.config.loader import load_config
    from sova.core.context import ExecutionContext
    from sova.db.session import init_db
    from sova.roles.dispatcher import dispatch

    resolved_dir = project_dir or Path.cwd()
    config = load_config(resolved_dir)

    await init_db(resolved_dir)

    adapter = create_adapter(config.task_source.type, config.github_repo, config.github_user)
    interval = config.watch.interval_active

    console.print(f"[bold]Watch mode started (polling every {interval}s)[/bold]")

    while True:
        try:
            tasks = await adapter.list_tasks(TaskFilters(state="open"))
            actionable = [t for t in tasks if t.state in (TaskState.BACKLOG, TaskState.TRIAGED, TaskState.RESEARCHED)]

            if actionable:
                task = actionable[0]
                console.print(f"\n[bold]Processing #{task.id}: {task.title}[/bold]")

                ctx = ExecutionContext(
                    project_dir=resolved_dir,
                    config=config,
                    adapter=adapter,
                    issue_number=task.id,
                    role=config.roles.default,
                    force=force,
                )

                role, result = await dispatch(ctx, config=config.roles)
                if result.success:
                    console.print(f"[green]Done: {result.summary}[/green]")
                else:
                    console.print(f"[yellow]Issue #{task.id}: {result.error}[/yellow]")
            else:
                console.print(f"[dim]No actionable issues. Sleeping {config.watch.interval_idle}s...[/dim]")
                interval = config.watch.interval_idle

        except KeyboardInterrupt:
            console.print("\n[bold]Watch mode stopped.[/bold]")
            break
        except Exception as exc:
            console.print(f"[red]Error: {exc}[/red]")

        await asyncio.sleep(interval)
        interval = config.watch.interval_active


def parallel(
    issues: Annotated[list[str], typer.Argument(help="Issue numbers to process concurrently.")],
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Skip pipeline gate checks.")] = False,
) -> None:
    """Run multiple issues concurrently."""
    asyncio.run(_parallel(issues=issues, project_dir=project, force=force))


async def _parallel(*, issues: list[str], project_dir: Path | None, force: bool) -> None:
    from sova.adapters import create_adapter
    from sova.config.loader import load_config
    from sova.core.context import ExecutionContext
    from sova.db.session import init_db
    from sova.roles.dispatcher import dispatch

    resolved_dir = project_dir or Path.cwd()
    config = load_config(resolved_dir)

    await init_db(resolved_dir)

    adapter = create_adapter(config.task_source.type, config.github_repo, config.github_user)
    max_concurrent = config.max_parallel_agents

    console.print(f"[bold]Processing {len(issues)} issues (max {max_concurrent} concurrent)[/bold]")

    semaphore = asyncio.Semaphore(max_concurrent)

    async def process_issue(issue: str) -> tuple[str, bool, str]:
        async with semaphore:
            ctx = ExecutionContext(
                project_dir=resolved_dir,
                config=config,
                adapter=adapter,
                issue_number=issue,
                role=config.roles.default,
                force=force,
            )
            try:
                role, result = await dispatch(ctx, config=config.roles)
                return issue, result.success, result.summary
            except Exception as exc:
                return issue, False, str(exc)

    results = await asyncio.gather(*[process_issue(i) for i in issues])

    console.print("\n[bold]Results:[/bold]")
    for issue, success, summary in results:
        status = "[green]OK[/green]" if success else "[red]FAIL[/red]"
        console.print(f"  #{issue}: {status} -- {summary}")
