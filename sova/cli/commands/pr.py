"""CLI commands: sova address-pr, maintain-pr, review-pr, linked-issues, pr-verdict, merge-subject."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Optional

import typer
from rich.console import Console
from rich.markup import escape

from sova.config.loader import load_config

if TYPE_CHECKING:
    from sova.config.models import ProjectConfig

console = Console(stderr=True)


def address_pr(
    pr: Annotated[int, typer.Argument(help="PR number to address.")],
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Address review comments on a pull request."""
    asyncio.run(_address_pr(pr=pr, project_dir=project))


async def _address_pr(*, pr: int, project_dir: Path | None) -> None:
    from sova.llm.client import invoke_command

    resolved_dir = project_dir or Path.cwd()
    config = load_config(resolved_dir)

    console.print(f"[bold]Addressing review comments on PR #{pr}...[/bold]")

    # No task_type: this is a dynamic user-invoked CLI command, not a pipeline
    # step, so it inherits config.agent.model and client-level fallback only.
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
    from sova.utils.env import scrub_check_env
    from sova.utils.gh import resolve_gh_env
    from sova.utils.shell import run

    resolved_dir = project_dir or Path.cwd()
    config = load_config(resolved_dir)

    console.print(f"[bold]Maintaining PR #{pr}...[/bold]")

    # Fetch PR branch
    env = await resolve_gh_env(config.github_user)
    result = await run("gh", "pr", "checkout", str(pr), cwd=resolved_dir, env=env)
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
    result = await run(
        "git", "push", "--force-with-lease", cwd=resolved_dir, env=scrub_check_env(config=config, quiet=True)
    )
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
    from sova.core.context import ExecutionContext
    from sova.db.session import init_db
    from sova.roles.reviewer import ReviewerRole
    from sova.utils.gh import resolve_linked_issue

    resolved_dir = project_dir or Path.cwd()
    config = load_config(resolved_dir)
    await init_db(resolved_dir)

    adapter = create_adapter(config)

    console.print(f"[bold]Reviewing PR #{pr}...[/bold]")

    try:
        issue_number = await resolve_linked_issue(
            pr,
            repo=config.github_repo,
            github_user=config.github_user,
        )
    except RuntimeError as exc:
        console.print(f"[red]Could not resolve the linked issue of PR #{pr}: {exc}[/red]")
        raise typer.Exit(code=1) from exc
    if not issue_number:
        console.print(f"[yellow]PR #{pr} has no linked issue, reviewing the PR on its own.[/yellow]")
        issue_number = ""

    ctx = ExecutionContext(
        project_dir=resolved_dir,
        config=config,
        adapter=adapter,
        issue_number=issue_number,
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


def linked_issues(
    pr: Annotated[int, typer.Argument(help="PR number.")],
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="After the merge: move each issue the PR closes to integration.post_merge_state; "
            "an issue it is only part of is left unchanged.",
        ),
    ] = False,
    mark_in_review: Annotated[
        bool,
        typer.Option(
            "--mark-in-review",
            help="Once the PR is open: move each issue it closes or is part of to in_review, "
            "from backlog, triaged, researched or in_progress only.",
        ),
    ] = False,
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Print the issues a PR closes and the ones it is only part of, or act on them.

    Without a flag, prints {"closes": [...], "part_of": [...]} as JSON.
    Only a closing keyword (Closes #N, Fixes #N, Resolves #N and their other forms) closes an issue:
    "Part of #N" leaves it open, and a bare #N is a mention, not a link.
    """
    asyncio.run(_linked_issues(pr=pr, apply=apply, mark_in_review=mark_in_review, project_dir=project))


def pr_verdict(
    pr: Annotated[int, typer.Argument(help="PR number.")],
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Print SOVA's verdict for a PR's current head: approve, revise, block, none or unknown.

    The same answer the dashboard and the PR monitor use: GitHub's latest sova-review marker for the
    current head, else the local reviewer handoff naming the same PR and head (a verdict whose post
    failed). Always prints exactly one word on stdout and exits 0; "unknown" means the verdict could
    not be read (GitHub unreachable, no repo configured, or the head could not be resolved).
    """
    typer.echo(asyncio.run(_pr_verdict(pr=pr, project_dir=project)))


async def _pr_verdict(*, pr: int, project_dir: Path | None) -> str:
    """The verdict word for *pr*; any failure to read it prints its reason on stderr and returns "unknown"."""
    try:
        return await _read_pr_verdict(pr=pr, project_dir=project_dir)
    except (RuntimeError, OSError, ValueError) as exc:
        # gh failures surface as RuntimeError; VERDICT=$(sova pr-verdict N) needs the word, not a traceback.
        console.print(f"[red]Could not read the verdict of PR #{pr}: {exc}[/red]")
        return "unknown"


async def _read_pr_verdict(*, pr: int, project_dir: Path | None) -> str:
    from sova.dashboard.services.pr_service import resolve_head_verdict
    from sova.git.pr import get_pr_head_sha, get_pr_review_data
    from sova.git.worktree import get_primary_worktree_root
    from sova.ipc.handoff import read_all_handoff_files
    from sova.utils.gh import get_active_gh_user

    # A linked worktree has no sova.db and its handoff files live in the primary checkout.
    root = project_dir or await get_primary_worktree_root()
    config = load_config(root)
    if not config.github_repo:
        console.print("[red]No github_repo configured: cannot read the PR verdict.[/red]")
        return "unknown"
    head_sha = await get_pr_head_sha(pr, repo=config.github_repo, github_user=config.github_user)
    if not head_sha:
        console.print(f"[red]Could not read the head commit of PR #{pr}.[/red]")
        return "unknown"
    data = await get_pr_review_data([pr], repo=config.github_repo, github_user=config.github_user)
    return resolve_head_verdict(
        data.get(pr),
        pr_number=pr,
        head_sha=head_sha,
        trusted_login=config.github_user or await get_active_gh_user() or "",
        handoffs=read_all_handoff_files(root),
    )


# `sova merge-subject` exits 4 for an invalid title (the user edits it) and 3 when the title could not be
# checked at all (gh, config, PR state, parse failure or any unexpected exception), which needs a different
# recovery. Typer owns 2, and 1 is what an uncaught traceback exits with, so neither means "invalid title".
MERGE_SUBJECT_INVALID_TITLE = 4
MERGE_SUBJECT_CHECK_FAILED = 3


def merge_subject(
    pr: Annotated[int, typer.Argument(help="PR number.")],
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
    validate_only: Annotated[
        bool,
        typer.Option("--validate-only", help="Only validate the title (the merge-queue path takes no --subject)."),
    ] = False,
) -> None:
    """Validate a PR title against commit.types and commit.scopes, and print its squash-merge subject.

    Exits 4 naming the failing part and the allowed values when the title is not a valid
    conventional-commit line, and 3 when the title could not be checked (gh, config, an unreadable stored
    commit convention, a PR that is not open, an unreadable allowed-methods lookup under "auto", or an
    unexpected error), so /integrate-pr stops before the merge and tells the two apart (a GitHub project
    with commit.pr_title_format "freeform" skips that check, as create_pr does). A merge that lands no
    PR-title subject (merge_method "rebase" or "merge", or "auto" on a repository allowing several
    methods) is not checked at all. When the merge will be a squash (merge_method "squash", or "auto" on a
    repository allowing only squash) prints "<title> (#N)" for `gh pr merge --subject`; otherwise prints
    nothing, and the reason goes to stderr. With --validate-only it prints nothing, skips the
    allowed-methods lookup and always checks the title (the queue's method is unknown, and it takes no
    --subject).
    """
    try:
        subject = asyncio.run(_merge_subject(pr=pr, project_dir=project, validate_only=validate_only))
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 (an unexpected error is no verdict on the title)
        console.print(f"[red]Could not check the title of PR #{pr}: {escape(str(exc))}[/red]")
        raise typer.Exit(code=MERGE_SUBJECT_CHECK_FAILED) from None
    if subject:
        typer.echo(subject)


async def _merge_subject(*, pr: int, project_dir: Path | None, validate_only: bool = False) -> str:
    """The validated squash subject for *pr*, or "" when the merge method takes no subject.

    An invalid title exits 4; any other failure to reach a verdict exits 3. The title is checked only when a
    squash lands it. With *validate_only* (merge queue, method unknown) the method resolution is skipped, the
    title is always checked and "" is returned once it is valid.
    """
    from sova.config.db_loader import db_read_failed
    from sova.git.merge import squash_is_sole_merge_method
    from sova.git.worktree import get_primary_worktree_root
    from sova.utils.gh import resolve_gh_env
    from sova.utils.shell import run

    root = project_dir or await get_primary_worktree_root()
    try:
        config = load_config(root)
    except Exception as exc:  # noqa: BLE001 (any load failure means no verdict, which must not read as a bad title)
        console.print(f"[red]Could not load the SOVA config: {escape(str(exc))}[/red]")
        raise typer.Exit(code=MERGE_SUBJECT_CHECK_FAILED) from None
    if db_read_failed():
        # load_config then holds the TOML or default commit convention, not the stored one: a title the stored
        # convention rejects could pass and become the squash subject.
        console.print("[red]Stored settings unreadable: the commit convention is unverified.[/red]")
        raise typer.Exit(code=MERGE_SUBJECT_CHECK_FAILED)
    if not config.github_repo:
        console.print("[red]No github_repo configured: cannot read the PR title.[/red]")
        raise typer.Exit(code=MERGE_SUBJECT_CHECK_FAILED)
    env = await resolve_gh_env(config.github_user)
    view = await run("gh", "pr", "view", str(pr), "--repo", config.github_repo, "--json", "title,state", env=env)
    if not view.success:
        console.print(f"[red]gh pr view {pr} failed: {escape(view.stderr.strip()) or 'no error output'}[/red]")
        raise typer.Exit(code=MERGE_SUBJECT_CHECK_FAILED)
    try:
        data = json.loads(view.stdout)
        title, state = str(data["title"]), str(data["state"])
    except (json.JSONDecodeError, KeyError, TypeError):
        console.print(f"[red]Could not read the title of PR #{pr}.[/red]")
        raise typer.Exit(code=MERGE_SUBJECT_CHECK_FAILED) from None
    if state != "OPEN":
        console.print(f"[red]PR #{pr} is not open ({state}): nothing to merge.[/red]")
        raise typer.Exit(code=MERGE_SUBJECT_CHECK_FAILED)

    method = config.integration.merge_method
    # A rebase or merge-commit merge lands no PR-title subject, so its title is not judged. The merge-queue path
    # (--validate-only) cannot tell which method the queue uses, so it always validates.
    lands_squash = True
    if not validate_only:
        if method == "auto":
            sole_squash = await squash_is_sole_merge_method(repo=config.github_repo, github_user=config.github_user)
            if sole_squash is None:
                # Not knowing the allowed methods must not read as "several methods": the merge would then take the
                # repository default's title at merge time, which is the edit window --subject exists to close.
                console.print(
                    f"[red]Could not read the allowed merge methods of {escape(config.github_repo)}: "
                    "merge_method is auto, so it is unknown whether the merge is a squash.[/red]"
                )
                raise typer.Exit(code=MERGE_SUBJECT_CHECK_FAILED)
            lands_squash = sole_squash
            if not lands_squash:
                console.print(
                    "merge_method is auto and the repository allows more than one method: the title is not "
                    "checked and there is no --subject, the repository default decides the landed subject."
                )
                return ""
        elif method != "squash":
            console.print(f"merge_method is {method}, which lands no PR-title subject: the title is not checked.")
            return ""

    subject = _validated_subject(config, title, pr)
    return "" if validate_only or not lands_squash else subject


def _validated_subject(config: ProjectConfig, title: str, pr: int) -> str:
    """The squash subject for *title*, or exit 4 naming the failing part and the allowed values."""
    from sova.core.steps.commit_convention import SCOPELESS_TYPE, build_merge_subject

    commit = config.commit
    # create_pr's rule: a Jira title is always "[KEY-N] type: subject", whatever pr_title_format says.
    conventional = commit.pr_title_format != "freeform" or config.task_source.is_jira
    try:
        return build_merge_subject(
            title,
            pr,
            types=commit.types,
            scopes=commit.scopes,
            conventional=conventional,
            jira=config.task_source.is_jira,
        )
    except ValueError as exc:
        if config.task_source.is_jira:
            scope_note = " (a scope is optional only after a [KEY-N] ticket key)"
        elif SCOPELESS_TYPE in commit.types:
            scope_note = f" (a scope is optional on {SCOPELESS_TYPE})"
        else:
            scope_note = ""
        console.print(
            f"[red]PR #{pr} title is not a valid commit line: {escape(str(exc))}.[/red]\n"
            f"Allowed types: {', '.join(commit.types)}\n"
            f"Allowed scopes: {', '.join(commit.scopes)}{scope_note}"
        )
        raise typer.Exit(code=MERGE_SUBJECT_INVALID_TITLE) from None


async def _pr_state(pr: int, repo: str, env: dict[str, str] | None) -> str:
    """The PR's GitHub state (OPEN, MERGED, CLOSED), or "" when it cannot be read (the gh error is printed)."""
    from sova.utils.shell import run

    state = await run("gh", "pr", "view", str(pr), "--repo", repo, "--json", "state", "--jq", ".state", env=env)
    if not state.success:
        # An auth or repository-access error must not read the same as an unreadable PR.
        console.print(f"[red]gh pr view {pr} failed: {state.stderr.strip() or 'no error output'}[/red]")
        return ""
    return state.stdout.strip()


async def _linked_issues(*, pr: int, apply: bool, mark_in_review: bool, project_dir: Path | None) -> None:
    from sova.git.merge import handle_merged_pr_issues, read_issue_links
    from sova.git.worktree import get_primary_worktree_root
    from sova.utils.gh import resolve_gh_env

    if apply and mark_in_review:
        console.print("[red]--apply (after the merge) and --mark-in-review (once open) are separate steps.[/red]")
        raise typer.Exit(code=2)
    # A linked worktree has no sova.db, so its own config has no repo: read the primary checkout's, as `sova run` does.
    root = project_dir or await get_primary_worktree_root()
    config = load_config(root)
    repo = config.github_repo
    env = await resolve_gh_env(config.github_user)
    if mark_in_review:
        await _mark_linked_issues_in_review(pr, config, env)
        return
    if not apply:
        links = await read_issue_links(pr, repo=repo, env=env)
        if links is None:
            console.print(f"[red]Could not read the body of PR #{pr}.[/red]")
            raise typer.Exit(code=1)
        typer.echo(json.dumps({"closes": list(links.closes), "part_of": list(links.part_of)}))
        return

    if config.task_source.type != "github":
        console.print("[red]--apply reads GitHub closing keywords; move a Jira ticket through its own workflow.[/red]")
        raise typer.Exit(code=2)
    pr_state = await _pr_state(pr, repo, env)
    if pr_state != "MERGED":
        console.print(f"[red]PR #{pr} is not merged ({pr_state or 'state unknown'}): no issue updated.[/red]")
        raise typer.Exit(code=1)

    from sova.adapters import create_adapter

    post_merge_state = config.integration.post_merge_state
    merged = await handle_merged_pr_issues(
        pr,
        post_merge_state=post_merge_state,
        repo=repo,
        github_user=config.github_user,
        adapter=create_adapter(config),
        project_dir=root,
    )
    if merged is None:
        console.print(f"[red]Could not read the body of PR #{pr}: no issue updated.[/red]")
        raise typer.Exit(code=1)
    for line in merged.describe(post_merge_state) or [f"PR #{pr} closes no issue: no issue updated."]:
        typer.echo(line)
    if merged.failed:
        raise typer.Exit(code=1)


async def _mark_linked_issues_in_review(pr: int, config: ProjectConfig, env: dict[str, str] | None) -> None:
    """Move the issues an open PR links to in_review, as the pipeline's create_pr step does (see /pr step 11)."""
    from sova.adapters import create_adapter
    from sova.git.merge import mark_pr_issues_in_review

    if config.task_source.type != "github":
        # /pr runs this on every project, so a Jira one must not fail the command.
        typer.echo("--mark-in-review reads GitHub issue links: no Jira ticket moved.")
        return
    pr_state = await _pr_state(pr, config.github_repo, env)
    if pr_state != "OPEN":
        console.print(f"[red]PR #{pr} is not open ({pr_state or 'state unknown'}): no issue updated.[/red]")
        raise typer.Exit(code=1)
    opened = await mark_pr_issues_in_review(
        pr, repo=config.github_repo, github_user=config.github_user, adapter=create_adapter(config)
    )
    if opened is None:
        console.print(f"[red]Could not read the body of PR #{pr}: no issue updated.[/red]")
        raise typer.Exit(code=1)
    for line in opened.describe() or [f"PR #{pr} closes no issue and is part of none."]:
        typer.echo(line)
    if opened.failed:
        raise typer.Exit(code=1)
