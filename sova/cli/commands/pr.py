"""CLI commands: sova address-pr, maintain-pr, review-pr, learn-from-pr."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

console = Console(stderr=True)


def address_pr(
    pr: Annotated[int, typer.Argument(help="PR number to address.")],
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Address review comments on a pull request."""
    asyncio.run(_address_pr(pr=pr, project_dir=project))


async def _address_pr(*, pr: int, project_dir: Path | None) -> None:
    from sova.config.loader import load_config
    from sova.llm.client import invoke_command

    resolved_dir = project_dir or Path.cwd()
    config = load_config(resolved_dir)

    console.print(f"[bold]Addressing review comments on PR #{pr}...[/bold]")

    result = await invoke_command(
        "/develop",
        f"Address all review comments on PR #{pr}",
        cwd=resolved_dir,
        model=config.agent.model,
        max_budget_usd=config.agent.max_budget,
    )

    console.print(f"[green]Done. Cost: ${result.cost_usd:.4f}[/green]")


def maintain_pr(
    pr: Annotated[int, typer.Argument(help="PR number to maintain.")],
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Rebase a PR on main and sync the description."""
    asyncio.run(_maintain_pr(pr=pr, project_dir=project))


async def _maintain_pr(*, pr: int, project_dir: Path | None) -> None:
    from sova.utils.shell import run

    resolved_dir = project_dir or Path.cwd()

    console.print(f"[bold]Maintaining PR #{pr}...[/bold]")

    # Fetch PR branch
    result = await run("gh", "pr", "checkout", str(pr), cwd=resolved_dir)
    if not result.success:
        console.print(f"[red]Failed to checkout PR: {result.stderr}[/red]")
        raise typer.Exit(code=1)

    # Rebase on main
    result = await run("git", "fetch", "origin", "main", cwd=resolved_dir)
    if not result.success:
        console.print(f"[red]Failed to fetch main: {result.stderr}[/red]")
        raise typer.Exit(code=1)

    result = await run("git", "rebase", "origin/main", cwd=resolved_dir)
    if not result.success:
        console.print("[red]Rebase failed. Resolve conflicts manually.[/red]")
        raise typer.Exit(code=1)

    # Force-push
    result = await run("git", "push", "--force-with-lease", cwd=resolved_dir)
    if not result.success:
        console.print(f"[red]Push failed: {result.stderr}[/red]")
        raise typer.Exit(code=1)

    console.print(f"[green]PR #{pr} rebased and pushed.[/green]")


def review_pr(
    pr: Annotated[int, typer.Argument(help="PR number to review.")],
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Run the automated reviewer on a pull request."""
    asyncio.run(_review_pr(pr=pr, project_dir=project))


async def _review_pr(*, pr: int, project_dir: Path | None) -> None:
    from sova.adapters import create_adapter
    from sova.config.loader import load_config
    from sova.core.context import ExecutionContext
    from sova.db.session import init_db
    from sova.roles.reviewer import ReviewerRole

    resolved_dir = project_dir or Path.cwd()
    config = load_config(resolved_dir)
    await init_db(resolved_dir)

    adapter = create_adapter(config.task_source.type, config.github_repo)

    console.print(f"[bold]Reviewing PR #{pr}...[/bold]")

    ctx = ExecutionContext(
        project_dir=resolved_dir,
        config=config,
        adapter=adapter,
        issue_number=str(pr),
        role="reviewer",
        pr_number=pr,
    )

    role = ReviewerRole()
    result = await role.execute(ctx)

    if result.success:
        console.print(f"[green]Review complete: {result.summary}[/green]")
    else:
        console.print(f"[red]Review failed: {result.error}[/red]")
        raise typer.Exit(code=1)


def learn_from_pr(
    pr: Annotated[int, typer.Argument(help="PR number to learn from.")],
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Ingest PR review feedback into agent memory."""
    asyncio.run(_learn_from_pr(pr=pr, project_dir=project))


async def _learn_from_pr(*, pr: int, project_dir: Path | None) -> None:
    from sova.config.loader import load_config
    from sova.db.session import init_db
    from sova.knowledge.review_patterns import record_review_finding
    from sova.utils.shell import run

    resolved_dir = project_dir or Path.cwd()
    config = load_config(resolved_dir)
    await init_db(resolved_dir)

    console.print(f"[bold]Learning from PR #{pr}...[/bold]")

    # Fetch PR reviews
    result = await run(
        "gh",
        "api",
        f"repos/{config.github_repo}/pulls/{pr}/reviews",
        "--jq",
        ".[].body",
        cwd=resolved_dir,
    )

    if not result.success:
        console.print(f"[red]Failed to fetch reviews: {result.stderr}[/red]")
        raise typer.Exit(code=1)

    reviews = [r.strip() for r in result.stdout.strip().split("\n") if r.strip()]

    if not reviews:
        console.print("[yellow]No review comments found.[/yellow]")
        return

    for review_body in reviews:
        await record_review_finding(
            session=None,
            category="pr_feedback",
            pattern=review_body[:500],
            source_pr=f"#{pr}",
        )

    console.print(f"[green]Ingested {len(reviews)} review(s) into memory.[/green]")
