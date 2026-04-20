"""CLI commands: sova server start/stop/status."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

app = typer.Typer(
    name="server",
    help="Manage the SOVA server daemon (dashboard + scheduler).",
    no_args_is_help=True,
)

console = Console(stderr=True)


@app.command()
def start(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    host: Annotated[str, typer.Option("--host", help="Host to bind to.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port to serve on.")] = 8111,
    no_scheduler: Annotated[bool, typer.Option("--no-scheduler", help="Start dashboard only, no watch loop.")] = False,
) -> None:
    """Start the SOVA server (dashboard + scheduler)."""
    from sova.config.loader import load_config
    from sova.scheduler.server import SOVAServer, read_pid_file

    resolved_dir = project or Path.cwd()
    config = load_config(resolved_dir)

    # Check if already running
    existing_pid = read_pid_file(config)
    if existing_pid is not None:
        console.print(f"[yellow]Server already running (PID {existing_pid}).[/yellow]")
        raise typer.Exit(code=1)

    if no_scheduler:
        config.server.scheduler_enabled = False

    config.server.host = host
    config.server.port = port

    console.print(f"[cyan]Starting SOVA server at http://{host}:{port}[/cyan]")
    if config.server.scheduler_enabled:
        console.print("[cyan]Scheduler: enabled[/cyan]")
    else:
        console.print("[dim]Scheduler: disabled[/dim]")

    server = SOVAServer(config=config, project_dir=resolved_dir, host=host, port=port)
    server.run()


@app.command()
def stop(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Stop the running SOVA server."""
    from sova.config.loader import load_config
    from sova.scheduler.server import stop_server

    config = load_config(project) if project else None

    if stop_server(config):
        console.print("[green]Server stopped.[/green]")
    else:
        console.print("[yellow]Server is not running.[/yellow]")


@app.command()
def status(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Show the SOVA server status."""
    from sova.config.loader import load_config
    from sova.scheduler.server import read_pid_file

    config = load_config(project) if project else None
    pid = read_pid_file(config)

    if pid is not None:
        console.print(f"[green]Server is running (PID {pid}).[/green]")
    else:
        console.print("[dim]Server is not running.[/dim]")
