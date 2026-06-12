"""CLI commands: sova mcp serve."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console

app = typer.Typer(
    name="mcp",
    help="SOVA MCP server (provider-agnostic agent tools).",
    no_args_is_help=True,
)

console = Console(stderr=True)


@app.command()
def serve(
    transport: Annotated[str, typer.Option("--transport", "-t", help="Transport protocol: stdio or sse.")] = "stdio",
) -> None:
    """Start the SOVA MCP server.

    Exposes SOVA tools (develop, review, test, simplify, etc.) via MCP
    so any compliant agent runtime can discover and invoke them.

    Default transport is stdio (for local tool integration).
    """
    import asyncio

    from sova.mcp.server import create_server

    server = create_server()

    console.print(f"[cyan]Starting SOVA MCP server (transport={transport})[/cyan]")

    if transport == "stdio":
        asyncio.run(server.run_stdio_async())
    elif transport == "sse":
        asyncio.run(server.run_sse_async())
    else:
        console.print(f"[red]Unknown transport: {transport}. Use 'stdio' or 'sse'.[/red]")
        raise typer.Exit(code=1)
