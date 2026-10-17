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

# PrivateTmp=true in the generated systemd unit gives it its own /tmp, isolated
# from these host roots (see install_service()). A module-level tuple, not an
# inline literal, so tests can patch it: pytest's own tmp_path fixture lives
# under the host's real /tmp on Linux, which would otherwise make this check
# fire on unrelated test fixtures that have nothing to do with it.
# S5443 flags any literal /tmp or /var/tmp reference as an unsafe write to a
# publicly writable directory; these two are read-only comparison roots this
# module never writes through, so nothing here is subject to that risk.
_PRIVATE_TMP_ROOTS = (Path("/tmp").resolve(), Path("/var/tmp").resolve())  # NOSONAR


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
    from sova.scheduler.server import StopResult, _is_port_listening, stop_server

    resolved_dir = project or Path.cwd()
    config = load_config(resolved_dir)

    result = stop_server(config, project_dir=resolved_dir)
    if result is StopResult.STOPPED:
        console.print("[green]Server stopped.[/green]")
    elif result is StopResult.FAILED:
        console.print("[red]Server did not stop; the process may still be alive.[/red]")
        raise typer.Exit(code=1)
    elif _is_port_listening(config.server.host, config.server.port):
        console.print(f"[red]Server did not stop; still listening on {config.server.host}:{config.server.port}.[/red]")
        raise typer.Exit(code=1)
    else:
        console.print("[yellow]Server is not running.[/yellow]")


@app.command()
def status(
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Show the SOVA server status."""
    from sova.config.loader import load_config
    from sova.scheduler.server import _is_port_listening, read_pid_file

    resolved_dir = project or Path.cwd()
    config = load_config(resolved_dir)
    pid = read_pid_file(config, project_dir=resolved_dir)

    if pid is not None:
        console.print(f"[green]Server is running (PID {pid}).[/green]")
    elif _is_port_listening(config.server.host, config.server.port):
        console.print(
            f"[yellow]Server appears to be running on {config.server.host}:{config.server.port}, "
            "but no PID file was found (orphaned).[/yellow]"
        )
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
    from sova.scheduler.server import StopResult, _is_port_listening, stop_server

    resolved_dir = project or Path.cwd()
    config = load_config(resolved_dir)

    result = stop_server(config, project_dir=resolved_dir)
    if result is StopResult.STOPPED:
        console.print("[yellow]Stopped running server.[/yellow]")
    elif result is StopResult.FAILED:
        console.print("[red]Could not stop the running server; the process may still be alive. Aborting restart.[/red]")
        raise typer.Exit(code=1)
    elif _is_port_listening(host, port):
        console.print(
            f"[red]Could not stop the running server; still listening on {host}:{port}. Aborting restart.[/red]"
        )
        raise typer.Exit(code=1)
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


def _generate_newsyslog_config(log_dir: Path, project_dir: Path) -> None:
    """Write a path-substituted newsyslog.d config for the launchd service's log files.

    launchd does not rotate StandardOutPath/StandardErrorPath on its own, and
    holds those descriptors open across a newsyslog rotation: the service
    keeps writing into the rotated-away inode until launchd actually restarts
    the job and reopens the paths fresh. Generate the real config (see
    deploy/com.sova.server.newsyslog.conf for the reference template) but
    never write into /etc/newsyslog.d/ ourselves: that is a system-wide change
    the operator must opt into.

    Each entry's [/pid_file sig_num] fields point newsyslog at the server's own
    PID file and SIGUSR2 (uncaught by uvicorn, so the default disposition
    terminates the process). KeepAlive's SuccessfulExit=false then has launchd
    restart the job, which is what actually reopens the log paths. SIGUSR2
    rather than SIGTERM keeps this distinct from `sova server stop`, which
    still relies on a clean SIGTERM exit(0) to NOT be restarted. Signal
    numbers are read from Python's signal module rather than hardcoded,
    since the launchd branch only ever runs on the macOS host it generates
    the config for, and Darwin's SIGUSR2 (31) differs from Linux's (12).

    newsyslog.conf is whitespace-delimited with no quoting or escaping mechanism
    at all, so a home directory containing a space (/Users/Jane Doe) can only
    produce an entry newsyslog silently ignores. Say so instead of writing one,
    the same way the systemd branch of install_service() refuses.
    """
    import signal

    from sova.config.loader import load_config
    from sova.scheduler.server import _resolve_pid_path

    newsyslog_dir = Path.home() / ".config" / "sova"
    newsyslog_file = newsyslog_dir / "sova-server.newsyslog.conf"
    pid_path = _resolve_pid_path(load_config(project_dir), project_dir)
    if any(c.isspace() for c in str(log_dir)) or any(c.isspace() for c in str(pid_path)):
        console.print(f"\n[yellow]Log or PID path contains whitespace: {log_dir} / {pid_path}[/yellow]")
        console.print("[yellow]newsyslog.conf cannot quote paths, so no rotation config was generated.[/yellow]")
        console.print("[yellow]Move the log directory or rotate these files another way.[/yellow]")
        return

    sig_usr2 = signal.SIGUSR2.value
    try:
        newsyslog_dir.mkdir(parents=True, exist_ok=True)
        newsyslog_file.write_text(
            "# logfilename                [owner:group]  mode  count  size(KB)  when  flags  [pid_file]  [sig_num]\n"
            f"{log_dir}/sova-server.log   644   5   10240   *   J   {pid_path}   {sig_usr2}\n"
            f"{log_dir}/sova-server.err   644   5   10240   *   J   {pid_path}   {sig_usr2}\n",
            encoding="utf-8",
        )
    except OSError as exc:
        # The service itself is already installed; losing the optional rotation
        # helper must not fail the whole command.
        console.print(f"[yellow]Could not write {newsyslog_file}: {exc}[/yellow]")
        console.print("[yellow]See deploy/com.sova.server.newsyslog.conf to install rotation by hand.[/yellow]")
        return

    console.print("\n[cyan]Log rotation (newsyslog):[/cyan]")
    console.print(f"  Generated: {newsyslog_file}")
    console.print(f"  sudo cp {newsyslog_file} /etc/newsyslog.d/sova-server.conf")
    console.print(
        "  [dim]Rotation sends SIGUSR2 to the PID in "
        f"{pid_path}; launchd's KeepAlive restarts the service and reopens the log files.[/dim]"
    )


@app.command()
def install_service(
    service_type: Annotated[str, typer.Option("--type", help="Service type (systemd|launchd).")],
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite existing service file.")] = False,
) -> None:
    """Install SOVA server as a system service (systemd or launchd)."""
    import html
    import shutil

    resolved_dir = (project or Path.cwd()).resolve()
    if not resolved_dir.is_dir():
        console.print(f"[red]Error: project directory does not exist: {resolved_dir}[/red]")
        raise typer.Exit(code=1)

    # Find sova binary location
    sova_bin = shutil.which("sova")
    if not sova_bin:
        console.print("[red]Error: sova binary not found in PATH.[/red]")
        raise typer.Exit(code=1)

    if service_type == "systemd":
        # Environment=, ExecStart= and ReadWritePaths= are all whitespace-split by
        # systemd, so a path with a space in it renders a unit that silently fails
        # at start (ReadWritePaths would name two nonexistent paths). Refuse here
        # rather than write one: the launchd plist is unaffected, since every path
        # there is its own XML <string>.
        # sova_bin is interpolated into the same whitespace-split ExecStart=/ExecStop=
        # lines, so it has to clear the same bar as the project path.
        # Whitespace is not the only character systemd reads as syntax in these
        # lines: a trailing backslash continues the value onto the next line
        # (and any backslash starts an escape sequence), and a percent sign
        # introduces a specifier such as the %h this unit already relies on. A
        # path carrying either renders a unit that fails at start, or one that
        # quietly points somewhere other than the directory asked for.
        unsafe_chars = (
            ("\\", "a backslash (systemd line continuation or escape sequence)"),
            ("%", "a percent sign (systemd specifier expansion)"),
        )
        for label, candidate in (("project path", str(resolved_dir)), ("sova binary path", str(sova_bin))):
            if any(c.isspace() for c in candidate):
                reason = "whitespace"
            else:
                reason = next((desc for char, desc in unsafe_chars if char in candidate), "")
            if not reason:
                continue
            console.print(f"[red]Error: {label} contains {reason}: {candidate}[/red]")
            console.print(
                "[yellow]systemd splits Environment=/ExecStart=/ReadWritePaths= on whitespace and "
                "interprets backslashes and percent signs as syntax.[/yellow]"
            )
            console.print("[yellow]Adapt deploy/sova-server.service by hand with quoted paths instead.[/yellow]")
            raise typer.Exit(code=1)

        # PrivateTmp=true below gives this unit its own /tmp mount namespace, detached
        # from the host's. ReadWritePaths= does not restore visibility into the host's
        # /tmp from inside that namespace, so a project or sova binary living there
        # would install successfully but be unreachable once the service starts.
        for label, candidate in (("project path", resolved_dir), ("sova binary path", Path(sova_bin).resolve())):
            if any(candidate.is_relative_to(root) for root in _PRIVATE_TMP_ROOTS):
                console.print(f"[red]Error: {label} is under /tmp: {candidate}[/red]")
                console.print(
                    "[yellow]PrivateTmp=true isolates this unit's /tmp from the host's, so a path "
                    "under /tmp is unreachable once the service starts.[/yellow]"
                )
                console.print(
                    "[yellow]Move it outside /tmp, or adapt deploy/sova-server.service by hand "
                    "without PrivateTmp=true.[/yellow]"
                )
                raise typer.Exit(code=1)

        target_dir = Path.home() / ".config" / "systemd" / "user"
        target_file = target_dir / "sova-server.service"

        # The whole project directory must be writable, not just its .claude/:
        # agents commit from linked worktrees, so git writes {project}/.git as
        # well as {project}/.claude. %h/.claude stays listed too, since the
        # spawned coding-agent CLI keeps its own state there and
        # knowledge.shared_knowledge_dir defaults under it.
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
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=%h/.config/sova %h/.claude {resolved_dir}

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
    target_file.write_text(template, encoding="utf-8")
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

        _generate_newsyslog_config(log_dir, resolved_dir)
