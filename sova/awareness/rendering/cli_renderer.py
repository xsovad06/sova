"""Rich terminal renderer for awareness briefings."""

from __future__ import annotations

from datetime import datetime

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from sova.awareness.rendering.models import Briefing


def render_briefing_cli(briefing: Briefing, console: Console, quiet: bool = False) -> None:
    """Render a Briefing to the terminal using Rich."""
    if not briefing.provider_statuses:
        console.print("[dim]No awareness providers configured.[/dim]")
        console.print("[dim]Enable in sova.toml under [awareness].[/dim]")
        return

    if not quiet:
        _render_greeting(briefing, console)

    _render_attention_items(briefing, console)

    if not quiet:
        _render_schedule(briefing, console)
        _render_informational(briefing, console)
        _render_project_pulses(briefing, console)
        _render_provider_status(briefing, console)


def _render_greeting(briefing: Briefing, console: Console) -> None:
    """Render greeting header with date."""
    date_str = briefing.generated_at.strftime("%A, %B %d, %Y")
    time_str = briefing.generated_at.strftime("%H:%M")

    hour = briefing.generated_at.hour
    if hour < 12:
        greeting = "Good morning"
    elif hour < 18:
        greeting = "Good afternoon"
    else:
        greeting = "Good evening"

    console.print(f"\n[bold]{greeting}[/bold] [dim]{date_str} at {time_str}[/dim]\n")


def _render_attention_items(briefing: Briefing, console: Console) -> None:
    """Render needs-attention items with urgency markers."""
    if not briefing.attention_items:
        return

    count = len(briefing.attention_items)
    plural = "items" if count > 1 else "item"
    console.print(f"[bold yellow]{count} {plural} need your attention[/bold yellow]\n")

    for item in briefing.attention_items:
        urgency_marker = _urgency_marker(item.urgency)
        title_line = f"{urgency_marker} [bold]{escape(item.title)}[/bold]"

        if item.body:
            body_text = escape(item.body[:200])
            ellipsis = "..." if len(item.body) > 200 else ""
            lines = [title_line, f"[dim]{body_text}{ellipsis}[/dim]"]
        else:
            lines = [title_line]

        if item.action_hint:
            lines.append(f"[cyan]{escape(item.action_hint)}[/cyan]")

        if item.timestamp:
            time_ago = _format_time_ago(item.timestamp)
            lines.append(f"[dim]{time_ago} • {escape(item.provider)}[/dim]")
        else:
            lines.append(f"[dim]{escape(item.provider)}[/dim]")

        panel_content = "\n".join(lines)
        console.print(Panel(panel_content, expand=False, border_style="yellow"))


def _render_schedule(briefing: Briefing, console: Console) -> None:
    """Render schedule items."""
    if not briefing.schedule:
        return

    console.print("\n[bold]Today's Schedule[/bold]\n")

    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("Time", style="cyan", width=10)
    table.add_column("Event", style="white")

    for item in briefing.schedule:
        if item.timestamp:
            time_str = item.timestamp.strftime("%H:%M")
            table.add_row(time_str, escape(item.title))
        else:
            table.add_row("--:--", f"[dim]{escape(item.title)}[/dim]")

    console.print(table)


def _render_informational(briefing: Briefing, console: Console) -> None:
    """Render informational items."""
    if not briefing.informational_items:
        return

    console.print("\n[bold]Recent Activity[/bold]\n")

    for item in briefing.informational_items:
        if item.timestamp:
            time_ago = _format_time_ago(item.timestamp)
            console.print(f"  • {escape(item.title)} [dim]({time_ago})[/dim]")
        else:
            console.print(f"  • {escape(item.title)}")


def _render_project_pulses(briefing: Briefing, console: Console) -> None:
    """Render project pulse summaries."""
    if not briefing.project_pulses:
        return

    console.print("\n[bold]Project Status[/bold]\n")

    table = Table(show_header=True, box=None, padding=(0, 1))
    table.add_column("Project", style="cyan")
    table.add_column("PRs", style="white", justify="right")
    table.add_column("Agent", style="white")
    table.add_column("CI", style="white")

    for pulse in briefing.project_pulses:
        ci_style = "green" if pulse.last_ci == "passing" else "red" if pulse.last_ci == "failing" else "dim"
        table.add_row(
            escape(pulse.project_slug),
            str(pulse.open_prs),
            escape(pulse.agent_status),
            f"[{ci_style}]{escape(pulse.last_ci)}[/{ci_style}]",
        )

    console.print(table)


def _render_provider_status(briefing: Briefing, console: Console) -> None:
    """Render provider fetch status."""
    if not briefing.provider_statuses:
        return

    failed = [s for s in briefing.provider_statuses if not s.ok]
    if not failed:
        return

    console.print("\n[bold red]Provider Errors[/bold red]\n")
    for status in failed:
        console.print(f"  [red]×[/red] {escape(status.name)}: {escape(status.message)}")


def _urgency_marker(urgency: int) -> str:
    """Return urgency marker text + color."""
    if urgency >= 2:
        return "[red]!!![/red]"
    if urgency == 1:
        return "[yellow]!![/yellow]"
    return "[dim]![/dim]"


def _format_time_ago(timestamp: datetime) -> str:
    """Format timestamp as relative time (e.g., '2h ago', '30m ago')."""
    if timestamp > datetime.now():
        return timestamp.strftime("%Y-%m-%d %H:%M")

    delta = datetime.now() - timestamp
    seconds = delta.total_seconds()

    if seconds < 60:
        return "just now"
    if seconds < 3600:
        minutes = int(seconds // 60)
        return f"{minutes}m ago"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours}h ago"

    days = int(seconds // 86400)
    return f"{days}d ago"
