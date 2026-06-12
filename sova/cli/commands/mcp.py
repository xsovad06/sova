"""CLI commands: sova mcp serve."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import click
import typer
from rich.console import Console

app = typer.Typer(
    name="mcp",
    help="SOVA MCP server (provider-agnostic agent tools).",
    no_args_is_help=True,
)

console = Console(stderr=True)

_VALID_TRANSPORTS = ["stdio", "sse"]


@app.command()
def serve(
    transport: Annotated[
        str,
        typer.Option(
            "--transport",
            "-t",
            help="Transport protocol: stdio or sse.",
            click_type=click.Choice(_VALID_TRANSPORTS, case_sensitive=False),
        ),
    ] = "stdio",
    project: Annotated[
        str,
        typer.Option("--project", "-p", help="Path to the project directory to bind the server to."),
    ] = "",
) -> None:
    """Start the SOVA MCP server.

    Exposes SOVA tools (develop, review, test, simplify, etc.) via MCP
    so any compliant agent runtime can discover and invoke them.

    Default transport is stdio (for local tool integration).
    """
    import asyncio

    from sova.mcp.server import create_server

    project_dir: Path | None = None
    if project:
        project_dir = Path(project).resolve()
        if not project_dir.is_dir():
            console.print(f"[red]Project directory not found: {project_dir}[/red]")
            raise typer.Exit(code=1)

    server = create_server(project_dir=project_dir)

    console.print(f"[cyan]Starting SOVA MCP server (transport={transport})[/cyan]")

    try:
        if transport == "stdio":
            asyncio.run(server.run_stdio_async())
        else:
            asyncio.run(server.run_sse_async())
    except KeyboardInterrupt:
        console.print("[yellow]Server stopped by user[/yellow]")
