"""CLI commands: sova supervisor status/poll."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

app = typer.Typer(
    name="supervisor",
    help="Query the SOVA supervisor daemon.",
    no_args_is_help=True,
)

console = Console(stderr=True)


@app.command()
def status(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    output_json: Annotated[bool, typer.Option("--json", help="Output as JSON.")] = False,
) -> None:
    """Show supervisor daemon status and recent decisions."""
    from sova.config.loader import load_config

    resolved = (project or Path.cwd()).resolve()
    cfg = load_config(resolved)

    if output_json:
        result = {
            "enabled": cfg.supervisor.enabled,
            "poll_interval_seconds": cfg.supervisor.poll_interval_seconds,
            "log_retention_days": cfg.supervisor.log_retention_days,
            "auto_triage": cfg.supervisor.auto_triage,
            "auto_research": cfg.supervisor.auto_research,
            "auto_develop": cfg.supervisor.auto_develop,
            "auto_integrate": cfg.supervisor.auto_integrate,
            "auto_rebase": cfg.supervisor.auto_rebase,
            "respect_dependencies": cfg.supervisor.respect_dependencies,
        }
        console.print(json.dumps(result, indent=2))
        return

    if not cfg.supervisor.enabled:
        console.print("[dim]Supervisor is disabled.[/dim]")
        console.print("[dim]Enable with: supervisor.enabled = true in sova.toml[/dim]")
        return

    console.print("[green]Supervisor is enabled[/green]")
    console.print(f"  Poll interval: {cfg.supervisor.poll_interval_seconds}s")
    console.print(f"  Log retention: {cfg.supervisor.log_retention_days}d")
    console.print(f"  Auto-triage: {cfg.supervisor.auto_triage}")
    console.print(f"  Auto-research: {cfg.supervisor.auto_research}")
    console.print(f"  Auto-develop: {cfg.supervisor.auto_develop}")
    console.print(f"  Auto-integrate: {cfg.supervisor.auto_integrate}")
    console.print(f"  Auto-rebase: {cfg.supervisor.auto_rebase}")
    console.print(f"  Respect deps: {cfg.supervisor.respect_dependencies}")

    # Show recent decisions
    async def _show_recent() -> None:
        from sova.dashboard.services.supervisor_service import get_recent_decisions
        from sova.db.session import init_db

        await init_db(resolved)
        decisions = await get_recent_decisions(resolved, limit=10)
        if not decisions:
            console.print("\n[dim]No recent decisions.[/dim]")
            return

        console.print(f"\n[cyan]Recent decisions ({len(decisions)}):[/cyan]")
        for d in decisions:
            action_style = "[red]" if d["action"] == "error" else "[cyan]"
            issue = f" #{d['issue_number']}" if d.get("issue_number") else ""
            console.print(
                f"  {d['created_at'][:19]}  {d['component']:12s}  "
                f"{action_style}{d['action']}[/]  {issue}  {d['detail'][:60]}"
            )

    asyncio.run(_show_recent())


@app.command()
def poll(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    output_json: Annotated[bool, typer.Option("--json", help="Output as JSON.")] = False,
) -> None:
    """Run a single supervisor poll cycle."""
    from sova.config.loader import load_config

    resolved = (project or Path.cwd()).resolve()
    cfg = load_config(resolved)

    if not cfg.supervisor.enabled:
        console.print("[yellow]Supervisor is disabled. Enable with supervisor.enabled = true[/yellow]")
        raise typer.Exit(code=1)

    async def _run_poll() -> dict:
        from sova.db.session import get_session_factory, init_db
        from sova.supervisor.daemon import SupervisorDaemon

        await init_db(resolved)
        session_factory = await get_session_factory(resolved)
        daemon = SupervisorDaemon(
            config=cfg,
            project_dir=resolved,
            session_factory=session_factory,
        )
        return await daemon.poll_once()

    result = asyncio.run(_run_poll())

    if output_json:
        console.print(json.dumps(result, indent=2, default=str))
        return

    console.print("[green]Poll cycle completed[/green]")
    for component, data in result.items():
        if isinstance(data, dict):
            if "error" in data:
                console.print(f"  {component}: [red]{data['error']}[/red]")
            else:
                summary = ", ".join(f"{k}={v}" for k, v in data.items())
                console.print(f"  {component}: {summary}")
