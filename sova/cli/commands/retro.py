"""CLI commands: sova retro record, backfill, report, top-rules.

Records which recorded rules (bold-labelled bullets in AGENTS.md, CLAUDE.md, .claude/rules/*.md and
common-mistakes.md) each merged PR's review findings repeat, and reports the repeat rate per month (issue #744).
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Optional

import typer
from sqlalchemy.exc import SQLAlchemyError

from sova.knowledge.retro import (
    COMMON_MISTAKES_PATH,
    RetroClassificationError,
    RetroError,
    build_report,
    collect_pr_review_data,
    load_retro_rows,
    load_rule_bullets,
    load_rule_catalog,
    pr_numbers_with_runs,
    record_pr_retro,
    recorded_pr_numbers,
    with_rule_text,
)

app = typer.Typer(
    name="retro",
    help="Record which recorded rules merged PRs' review findings repeat, and report the repeat rate.",
    no_args_is_help=True,
)

_PROJECT_OPTION = typer.Option(
    "--project", "-p", help="Project directory (default: the primary checkout, which holds sova.db)."
)
_GH_MERGED_LIMIT = "5000"


def _fail(message: str) -> typer.Exit:
    typer.echo(message, err=True)
    return typer.Exit(code=1)


async def _project_dir(project: Path | None) -> Path:
    from sova.db.session import init_db
    from sova.git.worktree import get_primary_worktree_root

    project_dir = (project or await get_primary_worktree_root()).resolve()
    await init_db(project_dir)
    return project_dir


async def _project_context(project: Path | None) -> tuple[Path, str, dict[str, str] | None, str]:
    """``(project dir, GitHub repo, gh env, classification model)`` for record and backfill."""
    from sova.config.loader import load_config
    from sova.llm.routing import resolve_extraction_model
    from sova.utils.gh import resolve_gh_env

    project_dir = await _project_dir(project)
    config = load_config(project_dir)
    if config.task_source.type != "github" or not config.github_repo:
        raise _fail("sova retro reads merged PRs from GitHub: set github_repo on a GitHub project.")
    env = await resolve_gh_env(config.github_user)
    return project_dir, config.github_repo, env, resolve_extraction_model(config.llm)


def _parse_merged_at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def _merged_at(pr: int, repo: str, env: dict[str, str] | None) -> datetime | None:
    """The PR's merge time, None when it is not merged. Raises ``RetroError`` when gh cannot tell."""
    from sova.utils.shell import run

    result = await run("gh", "pr", "view", str(pr), "--repo", repo, "--json", "state,mergedAt", env=env)
    if not result.success:
        raise RetroError(f"gh pr view {pr} failed: {result.stderr.strip() or 'no error output'}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RetroError(f"gh pr view {pr} returned unreadable output") from exc
    if data.get("state") != "MERGED" or not data.get("mergedAt"):
        return None
    return _parse_merged_at(data["mergedAt"])


async def _merged_prs(repo: str, env: dict[str, str] | None) -> dict[int, datetime]:
    """Every merged PR's merge time. Raises ``RetroError`` when gh fails."""
    from sova.utils.shell import run

    result = await run(
        "gh", "pr", "list", "--repo", repo, "--state", "merged", "--limit", _GH_MERGED_LIMIT,
        "--json", "number,mergedAt", env=env, timeout=120,
    )  # fmt: skip
    if not result.success:
        raise RetroError(f"gh pr list failed: {result.stderr.strip() or 'no error output'}")
    try:
        items = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RetroError("gh pr list returned unreadable output") from exc
    if len(items) >= int(_GH_MERGED_LIMIT):
        raise RetroError(f"gh pr list hit its {_GH_MERGED_LIMIT} limit: the merged list may be incomplete")
    return {int(item["number"]): _parse_merged_at(item["mergedAt"]) for item in items if item.get("mergedAt")}


@app.command("record")
def record_cmd(
    pr: Annotated[int, typer.Argument(help="Merged PR number.")],
    force: Annotated[bool, typer.Option("--force", help="Re-record a PR that already has a row.")] = False,
    project: Annotated[Optional[Path], _PROJECT_OPTION] = None,
) -> None:
    """Record one merged PR. Exits 1 when the PR is not merged or its classification fails."""
    asyncio.run(_record(pr=pr, force=force, project=project))


async def _record(*, pr: int, force: bool, project: Path | None) -> None:
    project_dir, repo, env, model = await _project_context(project)
    try:
        merged_at = await _merged_at(pr, repo, env)
        if merged_at is None:
            raise _fail(f"PR #{pr} is not merged: nothing recorded.")
        catalog = await load_rule_catalog(project_dir)
        outcome = await record_pr_retro(project_dir, pr, merged_at=merged_at, catalog=catalog, model=model, force=force)
    except RetroClassificationError as exc:
        raise _fail(f"PR #{pr}: retro not recorded, classification failed (cost ${exc.cost_usd:.4f}): {exc}") from exc
    except RetroError as exc:
        raise _fail(f"PR #{pr}: retro not recorded: {exc}") from exc

    if outcome.status == "skipped":
        typer.echo(f"PR #{pr}: already recorded, skipped (use --force to re-record).")
    elif outcome.status == "no_data":
        typer.echo(f"PR #{pr}: no SOVA review on record, nothing recorded.")
    else:
        typer.echo(
            f"PR #{pr}: recorded {outcome.findings} finding(s), {outcome.repeats} repeat a recorded rule "
            f"(classification ${outcome.cost_usd:.4f})."
        )


@app.command("backfill")
def backfill_cmd(
    limit: Annotated[Optional[int], typer.Option("--limit", help="Classify at most this many PRs.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="List the PRs that would be classified.")] = False,
    force: Annotated[bool, typer.Option("--force", help="Re-record PRs that already have a row.")] = False,
    concurrency: Annotated[int, typer.Option("--concurrency", min=1, help="Classification calls at once.")] = 4,
    project: Annotated[Optional[Path], _PROJECT_OPTION] = None,
) -> None:
    """Record every merged PR that has SOVA review data and no row yet. Exits 1 if any PR failed."""
    asyncio.run(_backfill(project=project, limit=limit, dry_run=dry_run, force=force, concurrency=concurrency))


async def _backfill(
    *, project: Path | None, limit: int | None, dry_run: bool, force: bool = False, concurrency: int = 1
) -> None:
    from sova.db.session import get_session

    project_dir, repo, env, model = await _project_context(project)
    try:
        merged = await _merged_prs(repo, env)
        catalog = await load_rule_catalog(project_dir)
    except RetroError as exc:
        raise _fail(f"backfill not started: {exc}") from exc

    async with await get_session(project_dir) as session:
        recorded = await recorded_pr_numbers(session)
        candidates = sorted(n for n in await pr_numbers_with_runs(session) if n in merged)
        if not force:
            candidates = [n for n in candidates if n not in recorded]
        reviewed = []
        for number in candidates:
            data = await collect_pr_review_data(session, number)
            if data.has_review_data:
                reviewed.append((number, len(data.findings)))
    if limit is not None:
        reviewed = reviewed[:limit]

    if dry_run:
        for number, findings in reviewed:
            typer.echo(f"PR #{number}: {findings} finding(s)")
        typer.echo(f"{len(reviewed)} PR(s) would be classified with {model} against {len(catalog.bullets)} rule(s).")
        return

    semaphore = asyncio.Semaphore(concurrency)
    counts = {"recorded": 0, "skipped": 0, "no_data": 0, "failed": 0}
    total_cost = Decimal("0")

    async def one(number: int) -> None:
        nonlocal total_cost
        async with semaphore:
            try:
                outcome = await record_pr_retro(
                    project_dir, number, merged_at=merged[number], catalog=catalog, model=model, force=force
                )
            except RetroClassificationError as exc:
                counts["failed"] += 1
                total_cost += exc.cost_usd
                typer.echo(f"PR #{number}: FAILED, classification: {exc}", err=True)
                return
            except Exception as exc:  # noqa: BLE001 (one PR's failure must not stop the rest of the backfill)
                counts["failed"] += 1
                typer.echo(f"PR #{number}: FAILED: {type(exc).__name__}: {exc}", err=True)
                return
            counts[outcome.status] += 1
            total_cost += outcome.cost_usd
            if outcome.status == "recorded":
                typer.echo(f"PR #{number}: {outcome.findings} finding(s), {outcome.repeats} repeat(s)")

    await asyncio.gather(*(one(number) for number, _ in reviewed))

    classified = counts["recorded"] + counts["failed"]
    per_pr = total_cost / classified if classified else Decimal("0")
    typer.echo(
        f"Backfill: recorded {counts['recorded']}, skipped {counts['skipped']}, failed {counts['failed']}, "
        f"no review data {counts['no_data']}. Classification cost ${total_cost:.4f} (${per_pr:.4f} per PR)."
    )
    if counts["failed"]:
        raise typer.Exit(code=1)


def _check_since(since: str | None) -> str | None:
    """Option callback shared by ``report`` and ``top-rules``: no value or a ``YYYY-MM`` month, else a usage error."""
    if since is not None and re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", since, flags=re.ASCII) is None:
        raise typer.BadParameter(f"takes a month as YYYY-MM, got {since!r}.", param_hint="--since")
    return since


_SINCE_OPTION = typer.Option("--since", help="First month to include, YYYY-MM.", callback=_check_since)


@app.command("report")
def report_cmd(
    since: Annotated[Optional[str], _SINCE_OPTION] = None,
    project: Annotated[Optional[Path], _PROJECT_OPTION] = None,
) -> None:
    """Print the monthly repeat rate of recorded rules and the most repeated rules."""
    asyncio.run(_report(since=since, project=project))


def _share(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "n/a"


async def _report(*, since: str | None, project: Path | None) -> None:
    from sova.db.session import get_session

    project_dir = await _project_dir(project)
    async with await get_session(project_dir) as session:
        report = build_report(await load_retro_rows(session), since=since)
    if not report.months:
        typer.echo("No PR recorded yet: run `sova retro backfill` or `sova retro record <pr>`.")
        return

    typer.echo(
        f"{'Month':<8} {'PRs':>4} {'Med+':>5} {'Repeats':>8} {'Share':>7} {'Rounds':>7} {'Cycles':>7} {'Cost/PR':>9}"
    )
    for m in report.months:
        typer.echo(
            f"{m.month:<8} {m.prs:>4} {m.medium_plus:>5} {m.repeats:>8} {_share(m.repeats, m.medium_plus):>7} "
            f"{m.mean_rounds:>7.1f} {m.mean_cycles:>7.1f} {'$' + format(m.mean_cost, '.2f'):>9}"
        )
    typer.echo("")
    typer.echo(
        f"Medium-or-worse findings repeating a recorded rule: {report.repeats} of {report.medium_plus} "
        f"({_share(report.repeats, report.medium_plus)}); tracked rule files only, without {COMMON_MISTAKES_PATH}: "
        f"{report.tracked_repeats} ({_share(report.tracked_repeats, report.medium_plus)})."
    )
    typer.echo(
        f"Rule lists matched against: {report.digests} distinct. Classification cost ${report.classify_cost:.4f}."
    )
    if report.top_rules:
        typer.echo("Most repeated rules (medium-or-worse findings, re-raises counted once):")
        for rule in report.top_rules:
            prs = ", ".join(f"#{n}" for n in rule.prs)
            typer.echo(f"  {rule.count:>3}  {rule.file}: {rule.label} (PRs {prs})")


@app.command("top-rules")
def top_rules_cmd(
    limit: Annotated[int, typer.Option("--limit", min=1, help="Most repeated rules to return.")] = 3,
    since: Annotated[Optional[str], _SINCE_OPTION] = None,
    project: Annotated[Optional[Path], _PROJECT_OPTION] = None,
) -> None:
    """Print the most repeated recorded rules as JSON, with each rule's text. `[]` means none recorded.

    Exits 1 (message on stderr) when the retro data or the rule files cannot be read, so an unreadable
    database never reads as `[]`; exits 2 on a malformed --since or a --limit below 1.
    """
    try:
        asyncio.run(_top_rules(limit=limit, since=since, project=project))
    except (RetroError, SQLAlchemyError, OSError) as exc:
        raise _fail(f"could not read the repeated rules: {type(exc).__name__}: {exc}") from exc


async def _top_rules(*, limit: int, since: str | None, project: Path | None) -> None:
    from sova.db.session import get_session

    project_dir = await _project_dir(project)
    async with await get_session(project_dir) as session:
        rows = await load_retro_rows(session)
    ranked = build_report(rows, since=since, top=limit).top_rules
    # The bullets are read (never dated through git) only once a rule ranks: with nothing recorded there is no text.
    bullets = await load_rule_bullets(project_dir) if ranked else []
    rules = with_rule_text(ranked, bullets)
    typer.echo(json.dumps([asdict(rule) for rule in rules], indent=2))
