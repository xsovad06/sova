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


def _start_server(project: Path | None, host: str, port: int, no_scheduler: bool, multi: bool | None) -> None:
    """Common server startup logic."""
    from sova.config.loader import load_config
    from sova.config.registry import has_projects
    from sova.scheduler.server import SOVAServer

    resolved_dir = project or Path.cwd()
    config = load_config(resolved_dir)

    if multi is None:
        multi = project is None and has_projects()

    if no_scheduler:
        config.server.scheduler_enabled = False

    config.server.host = host
    config.server.port = port

    console.print(f"[cyan]Starting SOVA server at http://{host}:{port}[/cyan]")
    if multi:
        console.print("[cyan]Mode: multi-project[/cyan]")
    if config.server.scheduler_enabled and not multi:
        console.print("[cyan]Scheduler: enabled[/cyan]")
    elif not multi:
        console.print("[dim]Scheduler: disabled[/dim]")

    server = SOVAServer(
        config=config,
        project_dir=resolved_dir if not multi else None,
        host=host,
        port=port,
        multi_project=multi,
    )
    server.run()


@app.command()
def start(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    host: Annotated[str, typer.Option("--host", help="Host to bind to.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port to serve on.")] = 8111,
    no_scheduler: Annotated[bool, typer.Option("--no-scheduler", help="Start dashboard only, no watch loop.")] = False,
    multi: Annotated[
        Optional[bool],
        typer.Option("--multi/--no-multi", help="Multi-project mode. Auto-detected when omitted."),
    ] = None,
) -> None:
    """Start the SOVA server (dashboard + scheduler)."""
    from sova.config.loader import load_config
    from sova.scheduler.server import read_pid_file

    resolved_dir = project or Path.cwd()
    config = load_config(resolved_dir)

    existing_pid = read_pid_file(config, project_dir=resolved_dir)
    if existing_pid is not None:
        console.print(f"[yellow]Server already running (PID {existing_pid}).[/yellow]")
        raise typer.Exit(code=1)

    _start_server(project, host, port, no_scheduler, multi)


@app.command()
def stop(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Stop the running SOVA server."""
    from sova.config.loader import load_config
    from sova.scheduler.server import stop_server

    resolved_dir = project or Path.cwd()
    config = load_config(resolved_dir)

    if stop_server(config, project_dir=resolved_dir):
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

    resolved_dir = project or Path.cwd()
    config = load_config(resolved_dir)
    pid = read_pid_file(config, project_dir=resolved_dir)

    if pid is not None:
        console.print(f"[green]Server is running (PID {pid}).[/green]")
    else:
        console.print("[dim]Server is not running.[/dim]")


@app.command()
def restart(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    host: Annotated[str, typer.Option("--host", help="Host to bind to.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port to serve on.")] = 8111,
    no_scheduler: Annotated[bool, typer.Option("--no-scheduler", help="Start dashboard only, no watch loop.")] = False,
    multi: Annotated[
        Optional[bool],
        typer.Option("--multi/--no-multi", help="Multi-project mode. Auto-detected when omitted."),
    ] = None,
) -> None:
    """Restart the SOVA server (stop, then start)."""
    from sova.config.loader import load_config
    from sova.scheduler.server import stop_server

    resolved_dir = project or Path.cwd()
    config = load_config(resolved_dir)

    if stop_server(config, project_dir=resolved_dir):
        console.print("[yellow]Stopped running server.[/yellow]")
    else:
        console.print("[dim]No server was running.[/dim]")

    _start_server(project, host, port, no_scheduler, multi)


@app.command()
def digest(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    hours: Annotated[int, typer.Option("--hours", help="Time window in hours.")] = 24,
) -> None:
    """Print a summary of task activity and costs."""
    import asyncio
    from datetime import UTC, datetime, timedelta

    from sova.db.session import get_session
    from sova.scheduler.server import query_digest_stats

    resolved_dir = project or Path.cwd()
    cutoff = datetime.now(UTC) - timedelta(hours=hours)

    async def fetch_digest() -> dict:
        session = await get_session(resolved_dir)
        try:
            completed, failed, in_progress, total_cost = await query_digest_stats(session, cutoff)
            return {
                "completed": completed,
                "failed": failed,
                "in_progress": in_progress,
                "cost": round(total_cost, 2),
            }
        finally:
            await session.close()

    data = asyncio.run(fetch_digest())

    console.print(f"[bold]SOVA Digest (last {hours}h)[/bold]")
    console.print(f"  Tasks completed: {data['completed']}")
    console.print(f"  Tasks failed: {data['failed']}")
    console.print(f"  Tasks in progress: {data['in_progress']}")
    console.print(f"  Total cost: ${data['cost']:.2f}")


@app.command()
def install_service(
    service_type: Annotated[str, typer.Option("--type", help="Service type (systemd|launchd).")],
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite existing service file.")] = False,
) -> None:
    """Install SOVA server as a system service (systemd or launchd)."""
    import html
    import shutil

    resolved_dir = project or Path.cwd()

    # Find sova binary location
    sova_bin = shutil.which("sova")
    if not sova_bin:
        console.print("[red]Error: sova binary not found in PATH.[/red]")
        raise typer.Exit(code=1)

    if service_type == "systemd":
        target_dir = Path.home() / ".config" / "systemd" / "user"
        target_file = target_dir / "sova-server.service"

        template = f"""[Unit]
Description=SOVA Server (Dashboard + Scheduler)
Documentation=https://github.com/xsovad06/sova
After=network.target

[Service]
Type=simple
Environment=HOME=%h
Environment=SOVA_PROJECT_DIR={resolved_dir}
ExecStart={sova_bin} server start --host 127.0.0.1 --port 8111 --project {resolved_dir}
ExecStop={sova_bin} server stop --project {resolved_dir}
StandardOutput=journal
StandardError=journal
Restart=on-failure
RestartSec=10

NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=%h/.config/sova %h/.claude

[Install]
WantedBy=default.target
"""

    elif service_type == "launchd":
        target_dir = Path.home() / "Library" / "LaunchAgents"
        target_file = target_dir / "com.sova.server.plist"
        log_dir = Path.home() / "Library" / "Logs" / "sova"

        # XML-escape paths to handle special characters
        sova_bin_escaped = html.escape(str(sova_bin))
        resolved_dir_escaped = html.escape(str(resolved_dir))
        log_dir_escaped = html.escape(str(log_dir))

        template = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.sova.server</string>

    <key>ProgramArguments</key>
    <array>
        <string>{sova_bin_escaped}</string>
        <string>server</string>
        <string>start</string>
        <string>--host</string>
        <string>127.0.0.1</string>
        <string>--port</string>
        <string>8111</string>
        <string>--project</string>
        <string>{resolved_dir_escaped}</string>
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <key>SOVA_PROJECT_DIR</key>
        <string>{resolved_dir_escaped}</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>StandardOutPath</key>
    <string>{log_dir_escaped}/sova-server.log</string>

    <key>StandardErrorPath</key>
    <string>{log_dir_escaped}/sova-server.err</string>

    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
"""

    else:
        console.print(f"[red]Error: Unknown service type '{service_type}'. Use 'systemd' or 'launchd'.[/red]")
        raise typer.Exit(code=1)

    # Check if file exists
    if target_file.exists() and not force:
        console.print(f"[yellow]Service file already exists: {target_file}[/yellow]")
        console.print("[yellow]Use --force to overwrite.[/yellow]")
        raise typer.Exit(code=1)

    # Create target directory
    target_dir.mkdir(parents=True, exist_ok=True)

    # Create log directory for launchd
    if service_type == "launchd":
        log_dir.mkdir(parents=True, exist_ok=True)

    # Write service file
    target_file.write_text(template)
    console.print(f"[green]Service file installed: {target_file}[/green]")

    # Print post-install instructions
    if service_type == "systemd":
        console.print("\n[cyan]Next steps:[/cyan]")
        console.print("  systemctl --user enable sova-server")
        console.print("  systemctl --user start sova-server")
        console.print("  systemctl --user status sova-server")
    elif service_type == "launchd":
        console.print("\n[cyan]Next steps:[/cyan]")
        console.print(f"  launchctl load {target_file}")
        console.print("  launchctl start com.sova.server")
        console.print("  launchctl list | grep sova")
