"""CLI commands: sova run, sova watch, sova parallel."""

from __future__ import annotations

import asyncio
from decimal import Decimal
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
    resume: Annotated[Optional[int], typer.Option("--resume", help="Resume from a previous run ID.")] = None,
    pr: Annotated[Optional[int], typer.Option("--pr", help="PR number (skips PR discovery).")] = None,
) -> None:
    """Run the agent workflow for a single issue."""
    asyncio.run(
        _run_workflow(issue, project_dir=project, role_name=role, force=force, resume_run_id=resume, pr_number=pr)
    )


async def _run_workflow(
    issue: str,
    *,
    project_dir: Path | None,
    role_name: str | None,
    force: bool,
    resume_run_id: int | None = None,
    pr_number: int | None = None,
) -> None:
    from sova.adapters import create_adapter
    from sova.config.loader import load_config
    from sova.core.context import ExecutionContext
    from sova.db.session import init_db
    from sova.roles.dispatcher import dispatch
    from sova.utils.logging import setup_logging

    resolved_dir = project_dir or Path.cwd()
    config = load_config(resolved_dir)

    setup_logging(log_file=resolved_dir / ".claude" / "sova.log")
    await init_db(resolved_dir)

    ts = config.task_source
    adapter = create_adapter(ts.type, config.github_repo, config.github_user, ts.github_project_number)

    checkpoint = {}
    if resume_run_id is not None:
        checkpoint = await _load_checkpoint(resume_run_id, issue)
        if checkpoint.get("error"):
            console.print(f"[red]Cannot resume: {checkpoint['error']}[/red]")
            raise typer.Exit(code=1)
        console.print(
            f"[bold]Resuming from run #{resume_run_id} "
            f"(skipping {len(checkpoint.get('completed_steps', set()))} completed steps)[/bold]"
        )

    ctx = ExecutionContext(
        project_dir=resolved_dir,
        config=config,
        adapter=adapter,
        issue_number=issue,
        role=role_name or checkpoint.get("role") or config.roles.default,
        force=force or bool(resume_run_id),
        resume_run_id=resume_run_id,
        completed_steps=frozenset(checkpoint.get("completed_steps", set())),
        branch_name=checkpoint.get("branch_name", ""),
        worktree_dir=checkpoint.get("worktree_dir"),
        pr_number=pr_number or checkpoint.get("pr_number"),
        cost_usd=checkpoint.get("cost_usd", Decimal("0")),
    )

    if resume_run_id:
        console.print(f"[bold]Resuming workflow for issue #{issue} from run #{resume_run_id}[/bold]")
    else:
        console.print(f"[bold]Starting workflow for issue #{issue}[/bold]")

    role, result = await dispatch(ctx, role_name=role_name, config=config.roles)

    if result.success:
        console.print(f"[green]Workflow completed ({role.name}): {result.summary}[/green]")
    else:
        console.print(f"[red]Workflow failed ({role.name}): {result.error}[/red]")
        raise typer.Exit(code=1)


async def _load_checkpoint(run_id: int, issue: str) -> dict:
    """Load checkpoint data from a previous TaskRun for resume."""
    from sqlalchemy import select

    from sova.db.models import StepExecution, TaskRun
    from sova.db.session import get_session

    session = await get_session()
    try:
        async with session.begin():
            task_run = await session.get(TaskRun, run_id)
            if task_run is None:
                return {"error": f"Run #{run_id} not found"}

            if task_run.issue_number != issue.lstrip("#").strip():
                return {"error": f"Run #{run_id} is for issue #{task_run.issue_number}, not #{issue}"}

            resumable = {"paused", "failed", "interrupted", "done"}
            if task_run.status not in resumable:
                return {"error": f"Run #{run_id} has status '{task_run.status}' (must be {', '.join(resumable)})"}

            stmt = select(StepExecution).where(StepExecution.task_run_id == run_id)
            result = await session.execute(stmt)
            steps = result.scalars().all()

            completed_steps = {s.step_name for s in steps if s.status in ("passed", "done")}

            worktree_dir = None
            if task_run.worktree_path:
                wt = Path(task_run.worktree_path)
                if wt.exists():
                    worktree_dir = wt

            return {
                "completed_steps": completed_steps,
                "branch_name": task_run.branch_name or "",
                "worktree_dir": worktree_dir,
                "pr_number": task_run.pr_number,
                "cost_usd": task_run.total_cost_usd or Decimal("0"),
                "role": task_run.role,
            }
    finally:
        await session.close()


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

    ts = config.task_source
    adapter = create_adapter(ts.type, config.github_repo, config.github_user, ts.github_project_number)
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

    ts = config.task_source
    adapter = create_adapter(ts.type, config.github_repo, config.github_user, ts.github_project_number)
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
