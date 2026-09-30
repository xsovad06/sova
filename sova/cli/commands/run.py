"""CLI commands: sova run, sova watch, sova parallel."""

from __future__ import annotations

import asyncio
import signal
import time
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Optional

import typer
from rich.console import Console

from sova.utils.logging import get_logger

if TYPE_CHECKING:
    from sova.core.context import ExecutionContext

console = Console(stderr=True)
log = get_logger(component="cli.run")


def run_issue(
    issue: Annotated[Optional[str], typer.Argument(help="Issue number (optional for project-scope roles).")] = None,
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    role: Annotated[Optional[str], typer.Option("--role", "-r", help="Force a specific role.")] = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Skip pipeline gate checks.")] = False,
    budget_override: Annotated[
        bool, typer.Option("--budget-override", help="Explicitly confirm bypassing the per-issue budget check.")
    ] = False,
    resume: Annotated[Optional[int], typer.Option("--resume", help="Resume from a previous run ID.")] = None,
    pr: Annotated[Optional[int], typer.Option("--pr", help="PR number (skips PR discovery).")] = None,
    run_id: Annotated[Optional[int], typer.Option("--run-id", help="Reuse an existing TaskRun.")] = None,
) -> None:
    """Run the agent workflow for a single issue or a project-scope role."""
    if not issue and not role:
        console.print("[red]Either an issue number or --role is required.[/red]")
        raise typer.Exit(code=2)
    try:
        asyncio.run(
            _run_workflow(
                issue or "",
                project_dir=project,
                role_name=role,
                force=force,
                budget_override=budget_override,
                resume_run_id=resume,
                pr_number=pr,
                task_run_id=run_id,
            )
        )
    except asyncio.CancelledError:
        # Terminated by SIGTERM (see _install_sigterm_handler). Exit with the
        # conventional 128+signal code so a parent process observes a
        # signal-shaped exit even though the handler intercepted the signal
        # itself (see normalize_signal_exit() in sova.ipc.control).
        console.print("[yellow]Workflow terminated (SIGTERM)[/yellow]")
        raise typer.Exit(code=128 + signal.SIGTERM) from None


async def _run_workflow(
    issue: str,
    *,
    project_dir: Path | None,
    role_name: str | None,
    force: bool,
    budget_override: bool = False,
    resume_run_id: int | None = None,
    pr_number: int | None = None,
    task_run_id: int | None = None,
) -> None:
    from sova.adapters import create_adapter
    from sova.config.loader import load_config
    from sova.core.context import ExecutionContext
    from sova.db.session import init_db
    from sova.git.worktree import get_primary_worktree_root
    from sova.roles.dispatcher import dispatch
    from sova.utils.logging import setup_logging

    # When called from inside a linked git worktree (e.g. a dashboard-spawned
    # agent running in .claude/worktrees/<id>), Path.cwd() is the worktree
    # directory, not the project root.  Resolve to the primary worktree root so
    # that config, DB, and pipeline operations all use the correct base path.
    resolved_dir = project_dir or await get_primary_worktree_root()
    config = load_config(resolved_dir)

    setup_logging(log_file=resolved_dir / ".claude" / "sova.log")
    await init_db(resolved_dir)

    adapter = create_adapter(config)

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

    actual_role = role_name or checkpoint.get("role") or config.roles.default

    # Generate a run_label for issue-less runs
    run_label = ""
    if not issue:
        run_label = f"{actual_role}-{int(time.time())}"

    ctx = ExecutionContext(
        project_dir=resolved_dir,
        config=config,
        adapter=adapter,
        issue_number=issue,
        role=actual_role,
        run_label=run_label,
        force=force or bool(resume_run_id),
        budget_override=budget_override,
        resume_run_id=resume_run_id,
        completed_steps=frozenset(checkpoint.get("completed_steps", set())),
        branch_name=checkpoint.get("branch_name", ""),
        worktree_dir=checkpoint.get("worktree_dir"),
        pr_number=pr_number or checkpoint.get("pr_number"),
        cost_usd=checkpoint.get("cost_usd", Decimal("0")),
        task_run_id=task_run_id,
        confidence_score=checkpoint.get("confidence_score"),
        confidence_details=checkpoint.get("confidence_details"),
    )

    if resume_run_id:
        console.print(f"[bold]Resuming workflow for {ctx.display_label} from run #{resume_run_id}[/bold]")
    else:
        console.print(f"[bold]Starting workflow for {ctx.display_label}[/bold]")

    _install_sigterm_handler(task_run_id)

    try:
        role, result = await dispatch(ctx, role_name=role_name, config=config.roles)
    except asyncio.CancelledError:
        await asyncio.shield(_handle_sigterm_shutdown(ctx, task_run_id))
        raise

    if result.success:
        console.print(f"[green]Workflow completed ({role.name}): {result.summary}[/green]")
    elif result.awaiting_approval:
        console.print(f"[yellow]Workflow paused ({role.name}): {result.summary}[/yellow]")
    else:
        console.print(f"[red]Workflow failed ({role.name}): {result.error}[/red]")
        raise typer.Exit(code=1)


async def _handle_sigterm_shutdown(ctx: ExecutionContext, task_run_id: int | None) -> None:
    """Best-effort cleanup during the SIGTERM->SIGKILL grace period.

    Commits any staged partial work so a terminated process does not lose
    in-progress edits, and (when this run is DB-tracked via --run-id) records
    elapsed cost so a delayed or absent external finalizer still sees
    accurate numbers. Status is deliberately left untouched here: whichever
    process classifies the final exit (the dashboard's _finalize_task_run,
    or the liveness sweep for a standalone run with no dashboard) decides
    "stopped" vs "interrupted" vs "failed" from the exit code and any
    TerminationRecord, not this handler.

    Only commits when ctx.worktree_dir is already set. Before create_worktree
    runs (e.g. during sync/assess, or for roles that never create a
    worktree), ctx.working_dir falls back to project_dir, the primary
    checkout; committing there could capture the operator's own uncommitted
    edits on whatever branch they have checked out.
    """
    from sova.git.worktree import commit_partial_work

    if ctx.worktree_dir is not None:
        await commit_partial_work(ctx.worktree_dir, "sigterm")

    if task_run_id is None:
        return

    try:
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session(ctx.project_dir) as session, session.begin():
            task_run = await session.get(TaskRun, task_run_id)
            if task_run is not None:
                task_run.total_cost_usd = ctx.cost_usd
    except Exception:  # noqa: BLE001 (best-effort cleanup during shutdown must not raise)
        log.warning("run.sigterm_db_update_failed", run_id=task_run_id, exc_info=True)


def _install_sigterm_handler(task_run_id: int | None) -> None:
    """Cancel the running workflow task when SIGTERM arrives.

    Lets the caller catch asyncio.CancelledError around dispatch() and run
    _handle_sigterm_shutdown() within the grace period before the parent
    (or OS) escalates to SIGKILL. add_signal_handler is POSIX-only; on a
    platform where it is unavailable, the signal falls back to the default
    disposition (immediate termination), same as before this feature existed.
    """
    try:
        loop = asyncio.get_running_loop()
        main_task = asyncio.current_task()
    except RuntimeError:
        return

    def _on_sigterm() -> None:
        log.warning("run.sigterm_received", run_id=task_run_id)
        if main_task is not None:
            main_task.cancel()

    try:
        loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
    except (NotImplementedError, RuntimeError):
        log.debug("run.sigterm_handler_unavailable")


async def _load_checkpoint(run_id: int, issue: str) -> dict:
    """Load checkpoint data from a previous TaskRun for resume."""
    from sqlalchemy import select

    from sova.db.models import StepExecution, TaskRun
    from sova.db.session import get_session

    async with await get_session() as session:
        async with session.begin():
            task_run = await session.get(TaskRun, run_id)
            if task_run is None:
                return {"error": f"Run #{run_id} not found"}

            if issue and not task_run.issue_number:
                return {"error": f"Run #{run_id} is an issue-less run, cannot resume with issue #{issue}"}
            if not issue and task_run.issue_number:
                return {"error": f"Run #{run_id} is for issue #{task_run.issue_number}, cannot resume without an issue"}
            if issue and task_run.issue_number and task_run.issue_number != issue.lstrip("#").strip():
                return {"error": f"Run #{run_id} is for issue #{task_run.issue_number}, not #{issue}"}

            resumable = {"paused", "failed", "interrupted", "done", "awaiting_approval", "stopped"}
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

            assessment = task_run.assessment_json or {}

            return {
                "completed_steps": completed_steps,
                "branch_name": task_run.branch_name or "",
                "worktree_dir": worktree_dir,
                "pr_number": task_run.pr_number,
                "cost_usd": task_run.total_cost_usd or Decimal("0"),
                "role": task_run.role,
                "confidence_score": assessment.get("confidence_score"),
                "confidence_details": assessment.get("confidence_details"),
            }


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

    adapter = create_adapter(config)
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

                _, result = await dispatch(ctx, config=config.roles)
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
        except Exception as exc:  # noqa: BLE001 (watch loop must survive any single-cycle error)
            log.warning("watch.cycle_failed", exc_info=True)
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

    adapter = create_adapter(config)
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
                _, result = await dispatch(ctx, config=config.roles)
                return issue, result.success, result.summary
            except Exception as exc:  # noqa: BLE001 (one issue must not abort the batch; the error lands in the result row)
                return issue, False, str(exc)

    results = await asyncio.gather(*[process_issue(i) for i in issues])

    console.print("\n[bold]Results:[/bold]")
    for issue, success, summary in results:
        status = "[green]OK[/green]" if success else "[red]FAIL[/red]"
        console.print(f"  #{issue}: {status} -- {summary}")
