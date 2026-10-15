"""CLI commands: sova assess set, get, clear.

Manually record, read, or clear a local suitability assessment for a ticket
(`task_assessments` table), used by the supervisor's human-involvement gate as
a fallback when the tracker has no `agent:` suitability label.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

console = Console(stderr=True)

app = typer.Typer(name="assess", help="Manage local suitability assessments.", no_args_is_help=True)

_PROJECT_OPTION = typer.Option("--project", "-p", exists=True, file_okay=False, help="Project directory.")
_ISSUE_ARGUMENT = typer.Argument(help="Issue number or key, e.g. 1234, #1234, RHCLOUD-1234.")
_SUITABILITY_CHOICES = "ready, interactive, human-only"


def _is_installed_project(project_dir: Path) -> bool:
    """Return True if ``project_dir`` already looks like an installed SOVA project.

    A mistyped or non-SOVA path must not silently create a brand new database:
    ``init_db`` runs every migration and happily creates `.claude/sova.db` for
    any writable directory, reporting success for a record no supervisor will
    ever read. Accepts an existing project DB, a not-yet-migrated `sova.toml`
    (the legacy config format `load_config` auto-migrates on first read), or a
    registry entry (a project registered but not yet given its first write).
    """
    from sova.config.registry import find_slug_for_path

    if (project_dir / ".claude" / "sova.db").exists():
        return True
    if (project_dir / "sova.toml").exists():
        return True
    return find_slug_for_path(project_dir) is not None


def _expected_tracker_prefix(task_source, github_repo: str) -> str:
    """Return this project's tracker key prefix, or '' when not configured.

    Jira tickets are keyed by the configured project key; GitHub issues by
    convention use the repo name (the part of ``owner/repo`` after the slash).
    """
    if task_source.type == "jira":
        return task_source.jira_project_key
    return github_repo.rsplit("/", 1)[-1]


def _normalize_issue_number(raw: str, *, expected_prefix: str = "") -> str:
    """Strip a leading '#' and any tracker key prefix (e.g. 'RHCLOUD-1234' -> '1234').

    Handles any project prefix, not just one tracker's convention: everything
    up to and including the last '-' before the numeric suffix is dropped.
    When ``expected_prefix`` is given, a present prefix is additionally
    validated against it, so a foreign ticket key (e.g. 'ABC-1234' pasted
    into a project configured for 'RHCLOUD') is never silently misattributed
    to a local issue of the same number.

    Raises:
        typer.Exit: when the result is not a bare issue number, or when a
            tracker prefix does not match the configured project. The
            supervisor gate looks the record up by the issue's integer id, so
            a row keyed by anything else can never be read: accepting it
            would store a write that silently does nothing (or, worse,
            attributes a foreign ticket's verdict to a local one).
    """
    stripped = raw.strip().lstrip("#")
    prefix, _, suffix = stripped.rpartition("-")
    normalized = suffix if prefix else stripped
    if not normalized.isdigit():
        typer.echo(f"Unrecognized issue number: '{raw}'. Expected 1234, #1234 or PROJ-1234.", err=True)
        raise typer.Exit(code=1)

    if prefix and expected_prefix and prefix.lower() != expected_prefix.lower():
        typer.echo(
            f"'{raw}' does not belong to this project's configured tracker "
            f"(expected prefix '{expected_prefix}', got '{prefix}').",
            err=True,
        )
        raise typer.Exit(code=1)
    return normalized


async def _resolve_target(project_dir: Path | None, issue: str) -> tuple[Path, str, str]:
    """Initialize the project DB and return (project_dir, project_slug, issue_number).

    With no explicit ``--project``, resolves the primary worktree root rather
    than the bare cwd: ``.claude/sova.db`` lives in the primary checkout, so a
    command run from a linked worktree would otherwise create and write to a
    throwaway DB the supervisor never reads.
    """
    from sova.config.loader import load_config
    from sova.db.session import init_db
    from sova.git.worktree import get_primary_worktree_root

    resolved_dir = (project_dir or await get_primary_worktree_root()).resolve()
    if not _is_installed_project(resolved_dir):
        typer.echo(
            f"'{resolved_dir}' does not look like an installed SOVA project "
            "(no .claude/sova.db, sova.toml, or registry entry). Run `sova install` first.",
            err=True,
        )
        raise typer.Exit(code=1)

    await init_db(resolved_dir)
    cfg = load_config(resolved_dir)
    expected_prefix = _expected_tracker_prefix(cfg.task_source, cfg.github_repo)
    issue_number = _normalize_issue_number(issue, expected_prefix=expected_prefix)
    return resolved_dir, cfg.github_repo, issue_number


@app.command("set")
def set_cmd(
    issue: Annotated[str, _ISSUE_ARGUMENT],
    suitability: Annotated[str, typer.Argument(help=f"One of: {_SUITABILITY_CHOICES}.")],
    project: Annotated[Optional[Path], _PROJECT_OPTION] = None,
) -> None:
    """Record a local suitability assessment for an issue."""
    asyncio.run(_set(issue=issue, suitability=suitability, project_dir=project))


async def _set(*, issue: str, suitability: str, project_dir: Path | None) -> None:
    from sova.db.session import get_session
    from sova.knowledge.assessments import resolve_suitability, set_assessment

    resolved = resolve_suitability(suitability)
    if resolved is None:
        typer.echo(f"Unknown suitability: '{suitability}'. Use one of: {_SUITABILITY_CHOICES}.", err=True)
        raise typer.Exit(code=1)

    resolved_dir, project_slug, issue_number = await _resolve_target(project_dir, issue)

    async with await get_session(resolved_dir) as session:
        await set_assessment(
            session,
            issue_number=issue_number,
            project_slug=project_slug,
            suitability=resolved,
            reasoning="Manually set via `sova assess set`.",
        )

    console.print(f"[green]Set #{issue_number} suitability to '{suitability}'.[/green]")


@app.command("get")
def get_cmd(
    issue: Annotated[str, _ISSUE_ARGUMENT],
    project: Annotated[Optional[Path], _PROJECT_OPTION] = None,
) -> None:
    """Print the local suitability assessment for an issue, or 'none'."""
    asyncio.run(_get(issue=issue, project_dir=project))


async def _get(*, issue: str, project_dir: Path | None) -> None:
    from sova.db.session import get_session
    from sova.knowledge.assessments import get_latest_assessment, shorthand_for

    resolved_dir, project_slug, issue_number = await _resolve_target(project_dir, issue)

    async with await get_session(resolved_dir) as session:
        record = await get_latest_assessment(session, issue_number=issue_number, project_slug=project_slug)

    if record is None:
        typer.echo("none")
        return
    typer.echo(shorthand_for(record.suitability))


@app.command("clear")
def clear_cmd(
    issue: Annotated[str, _ISSUE_ARGUMENT],
    project: Annotated[Optional[Path], _PROJECT_OPTION] = None,
) -> None:
    """Delete the local suitability assessment for an issue, if any."""
    asyncio.run(_clear(issue=issue, project_dir=project))


async def _clear(*, issue: str, project_dir: Path | None) -> None:
    from sova.db.session import get_session
    from sova.knowledge.assessments import clear_assessment

    resolved_dir, project_slug, issue_number = await _resolve_target(project_dir, issue)

    async with await get_session(resolved_dir) as session:
        count = await clear_assessment(session, issue_number=issue_number, project_slug=project_slug)

    if count:
        console.print(f"[green]Cleared local suitability for #{issue_number}.[/green]")
    else:
        console.print(f"[yellow]No local suitability record for #{issue_number}.[/yellow]")
