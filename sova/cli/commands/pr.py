"""CLI commands: sova address-pr, maintain-pr, review-pr, linked-issues, pr-verdict, refresh-pr-body, learn-from-pr."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Optional

import typer
from rich.console import Console

if TYPE_CHECKING:
    from sova.config.models import ProjectConfig

console = Console(stderr=True)

# `sova refresh-pr-body` exit codes. 0 is a clean re-judge or nothing to do; the rest tell /integrate-pr
# whether the merge may still go ahead (1 and 4: the body is unchanged, it stays "Part of") or must not (3).
REFRESH_STILL_UNPARSEABLE = 1
REFRESH_USAGE = 2
REFRESH_HEAD_MOVED = 3
REFRESH_INFRASTRUCTURE = 4

_REFRESH_REF_PREFIX = "refs/sova/refresh-pr-body/"
_HEAD_SHA_RE = re.compile(r"[0-9a-f]{7,40}")


def address_pr(
    pr: Annotated[int, typer.Argument(help="PR number to address.")],
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Address review comments on a pull request."""
    asyncio.run(_address_pr(pr=pr, project_dir=project))


async def _address_pr(*, pr: int, project_dir: Path | None) -> None:
    from sova.config.loader import load_config
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
    from sova.config.loader import load_config
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
    from sova.config.loader import load_config
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
            help="After the merge: move each issue the PR closes to integration.post_merge_state, "
            "and comment on each issue it is only part of.",
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

    Without a flag, prints {"closes": [...], "part_of": [...]} as JSON. Only a closing keyword
    (Closes #N, Fixes #N, Resolves #N and their other forms) closes an issue: "Part of #N" leaves
    it open, and a bare #N is a mention, not a link.
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
    from sova.config.loader import load_config
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


def refresh_pr_body(
    pr: Annotated[int, typer.Argument(help="PR number.")],
    only_if_unparseable: Annotated[
        bool,
        typer.Option(
            "--only-if-unparseable",
            help="Do nothing unless the body carries the unparseable-verdicts note (no LLM call otherwise).",
        ),
    ] = False,
    head: Annotated[
        Optional[str],
        typer.Option("--head", help="Judge only if the PR head is this commit (the SHA a merge gate checked)."),
    ] = None,
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Re-judge a PR's acceptance criteria and rewrite only its "## Issue" closing section.

    Exit codes: 0 re-judged (or nothing to re-judge); 1 the verdicts still could not be obtained, or the
    PR links no issue, so it stays "Part of #N"; 2 usage error (non-GitHub project, malformed --head);
    3 the PR head is no longer the --head commit, so the gated commit is not what would merge; 4 the
    re-judge could not run (body unreadable, git fetch or worktree failed) and the body is unchanged.

    A re-judge takes up to two LLM calls of commit.pr_body_timeout each: give the calling shell a budget
    of at least 2 x that plus 60 seconds.
    """
    if head is not None:
        head = head.strip().lower()
        if not _HEAD_SHA_RE.fullmatch(head):
            console.print("[red]--head must be a commit SHA of 7 to 40 hex characters.[/red]")
            raise typer.Exit(code=REFRESH_USAGE)
    asyncio.run(
        _refresh_pr_body(pr=pr, only_if_unparseable=only_if_unparseable, expected_head=head, project_dir=project)
    )


async def _refresh_pr_body(
    *, pr: int, only_if_unparseable: bool, expected_head: str | None, project_dir: Path | None
) -> None:
    import shutil
    import tempfile
    import uuid

    from sova.adapters import create_adapter
    from sova.config.loader import load_config
    from sova.core.context import ExecutionContext
    from sova.core.steps._pr_body import UNPARSEABLE_NOTE
    from sova.core.steps.refresh_pr_body import RefreshPrBodyStep
    from sova.git.pr import get_pr_body
    from sova.git.worktree import cleanup_worktree, get_primary_worktree_root
    from sova.utils.gh import parse_issue_links
    from sova.utils.markdown import extract_section
    from sova.utils.shell import run

    root = project_dir or await get_primary_worktree_root()
    config = load_config(root)
    if config.task_source.type != "github":
        console.print("[red]refresh-pr-body judges GitHub acceptance criteria; a Jira PR has no closing keyword.[/red]")
        raise typer.Exit(code=REFRESH_USAGE)

    async def read_body() -> str:
        try:
            return await get_pr_body(pr, repo=config.github_repo, github_user=config.github_user)
        except RuntimeError as exc:
            console.print(f"[red]Could not read the body of PR #{pr}: {exc}[/red]")
            raise typer.Exit(code=REFRESH_INFRASTRUCTURE) from exc

    body = await read_body()
    if only_if_unparseable and UNPARSEABLE_NOTE not in body:
        typer.echo(f"PR #{pr} carries no unparseable-verdicts note: not re-judged.")
        return
    # The unparseable note is only ever rendered inside "## Issue", so that section names the issue that was
    # judged. A closing keyword a human added elsewhere in the body must not redirect the re-judge to another issue.
    section_links = parse_issue_links(extract_section(body, "Issue"))
    links = parse_issue_links(body)
    issues = [*section_links.closes, *section_links.part_of] or [*links.closes, *links.part_of]
    if not issues:
        console.print(f"[red]PR #{pr} links no issue: nothing to judge.[/red]")
        raise typer.Exit(code=REFRESH_STILL_UNPARSEABLE)

    # The step diffs HEAD against origin/<base>, so judge from a detached checkout of the PR head.
    # The head goes to a private ref, not FETCH_HEAD: the primary checkout's FETCH_HEAD is shared
    # with every other SOVA process fetching there, and a concurrent fetch would swap the commit judged.
    # The ref is unique per invocation, so two concurrent runs for one PR neither clobber nor delete each other's.
    await _drop_stale_refresh_refs(root, pr)
    head_ref = f"{_REFRESH_REF_PREFIX}{pr}-{uuid.uuid4().hex[:8]}"
    fetched = await run("git", "fetch", "origin", config.base_branch, f"+pull/{pr}/head:{head_ref}", cwd=root)
    if not fetched.success:
        console.print(f"[red]git fetch of PR #{pr} and {config.base_branch} failed: {fetched.stderr.strip()}[/red]")
        raise typer.Exit(code=REFRESH_INFRASTRUCTURE)
    if expected_head:
        resolved = await run("git", "rev-parse", "--verify", f"{head_ref}^{{commit}}", cwd=root)
        fetched_sha = resolved.stdout.strip()
        if not resolved.success or not fetched_sha:
            await run("git", "update-ref", "-d", head_ref, cwd=root)
            console.print(f"[red]Could not read the fetched head of PR #{pr}: {resolved.stderr.strip()}[/red]")
            raise typer.Exit(code=REFRESH_INFRASTRUCTURE)
        if not fetched_sha.lower().startswith(expected_head):
            await run("git", "update-ref", "-d", head_ref, cwd=root)
            console.print(
                f"[red]PR #{pr} head is {fetched_sha[:12]}, not the expected {expected_head}: not re-judged.[/red]"
            )
            raise typer.Exit(code=REFRESH_HEAD_MOVED)
    with tempfile.TemporaryDirectory(prefix=f"sova-pr-{pr}-") as tmp:
        worktree = Path(tmp) / "head"
        added = await run("git", "worktree", "add", "--detach", str(worktree), head_ref, cwd=root)
        if not added.success:
            await run("git", "update-ref", "-d", head_ref, cwd=root)
            console.print(f"[red]Could not check out PR #{pr}: {added.stderr.strip()}[/red]")
            raise typer.Exit(code=REFRESH_INFRASTRUCTURE)
        try:
            ctx = ExecutionContext(
                project_dir=root,
                config=config,
                adapter=create_adapter(config),
                issue_number=issues[0],
                pr_number=pr,
                worktree_dir=worktree,
            )
            result = await RefreshPrBodyStep(closing_only=True).execute(ctx)
        finally:
            await cleanup_worktree(worktree, cwd=root)
            # TemporaryDirectory removes the directory next: drop any worktree metadata cleanup left behind.
            shutil.rmtree(worktree, ignore_errors=True)
            await run("git", "worktree", "prune", cwd=root)
            await run("git", "update-ref", "-d", head_ref, cwd=root)

    typer.echo(result.summary)
    if UNPARSEABLE_NOTE in await read_body():
        console.print(f"[red]PR #{pr} still has no per-criterion verdicts: it stays 'Part of #{issues[0]}'.[/red]")
        raise typer.Exit(code=REFRESH_STILL_UNPARSEABLE)


async def _drop_stale_refresh_refs(root: Path, pr: int) -> None:
    """Delete private head refs and worktree registrations a killed earlier run of this command left behind.

    The refs carry a unique suffix, so nothing else ever reuses or deletes one, and a run killed before
    its ``finally`` (a shell tool timeout) leaks its ref and a worktree registration pointing into a
    removed temp dir. Best effort: a failure here must not stop the re-judge. A detached worktree does not
    need its ref once checked out, so dropping a concurrent run's ref after its checkout is harmless.
    """
    from sova.utils.shell import run

    listed = await run("git", "for-each-ref", "--format=%(refname)", f"{_REFRESH_REF_PREFIX}{pr}-*", cwd=root)
    if listed.success:
        for ref in listed.stdout.split():
            await run("git", "update-ref", "-d", ref, cwd=root)
    await run("git", "worktree", "prune", cwd=root)


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
    from sova.config.loader import load_config
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
    for line in merged.describe(post_merge_state) or [f"PR #{pr} closes no issue and is part of none."]:
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


def learn_from_pr(
    pr: Annotated[int, typer.Argument(help="PR number to learn from.")],
    project: Annotated[Optional[Path], typer.Option("--project", "-p", help="Project directory.")] = None,
) -> None:
    """Ingest PR review feedback into agent memory."""
    asyncio.run(_learn_from_pr(pr=pr, project_dir=project))


async def _learn_from_pr(*, pr: int, project_dir: Path | None) -> None:
    from sova.config.loader import load_config
    from sova.db.session import init_db
    from sova.knowledge.review_patterns import record_review_finding
    from sova.utils.gh import resolve_gh_env
    from sova.utils.shell import run

    resolved_dir = project_dir or Path.cwd()
    config = load_config(resolved_dir)
    await init_db(resolved_dir)

    console.print(f"[bold]Learning from PR #{pr}...[/bold]")

    # Fetch PR reviews
    env = await resolve_gh_env(config.github_user)
    result = await run(
        "gh",
        "api",
        f"repos/{config.github_repo}/pulls/{pr}/reviews",
        "--jq",
        ".[].body",
        cwd=resolved_dir,
        env=env,
    )

    if not result.success:
        console.print(f"[red]Failed to fetch reviews: {result.stderr}[/red]")
        raise typer.Exit(code=1)

    reviews = [r.strip() for r in result.stdout.strip().split("\n") if r.strip()]

    if not reviews:
        console.print("[yellow]No review comments found.[/yellow]")
        return

    for review_body in reviews:
        await record_review_finding(
            session=None,
            category="pr_feedback",
            pattern=review_body[:500],
            source_pr=f"#{pr}",
        )

    console.print(f"[green]Ingested {len(reviews)} review(s) into memory.[/green]")
