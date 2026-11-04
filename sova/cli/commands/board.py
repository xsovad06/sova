"""CLI command: sova board add.

Deterministic project-board membership for freshly created issues. The
health-audit command used to carry this as prose and shell snippets an LLM
re-derived on every run; it is now one call that prints one line per issue URL.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy.exc import SQLAlchemyError

from sova.cli.commands.audit import project_root
from sova.utils.logging import get_logger
from sova.utils.shell import ShellResult, run

log = get_logger(component="cli.board")

app = typer.Typer(name="board", help="Project board operations.", no_args_is_help=True)

# Everything load_config can raise from a malformed or unreadable sova.toml or a locked or broken database.
_CONFIG_ERRORS = (RuntimeError, ValueError, OSError, sqlite3.Error, SQLAlchemyError)

_URL_ARGUMENT = typer.Argument(help="Issue URLs to add to the configured project board.")

_GH_TIMEOUT_SECONDS = 60
# Any host: gh item-add works on every host gh is logged in to, GitHub Enterprise included.
_ISSUE_URL_RE = re.compile(r"^https?://[^/]+/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)(?:[/?#].*)?$")
# `gh project` prints "missing required scopes [a b]"; `gh api graphql` passes GitHub's message through,
# "... requires one of the following scopes: ['a', 'b'], but your token has only been granted ...".
_MISSING_SCOPES_RES = (
    re.compile(r"missing required scopes? \[(?P<scopes>[^\]]*)\]", re.IGNORECASE),
    re.compile(r"requires one of the following scopes: \[(?P<scopes>[^\]]*)\]", re.IGNORECASE),
)
# gh's INSUFFICIENT_SCOPES message names read:project when the token has no project scope at all.
_PROJECT_SCOPES = frozenset({"project", "read:project"})
# Any of these supplying auth makes `gh auth refresh` refuse to run.
_TOKEN_ENV_VARS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN")

_PROJECT_ID_QUERY = """
query($owner:String!,$number:Int!){
  user(login:$owner){ projectV2(number:$number){ id title } }
}"""
# The title rides along so Phase D can check membership by title without a `gh project view` (which needs read:org).
_PROJECT_JQ = r'.data.user.projectV2 // empty | "\(.id)\t\(.title)"'
_ISSUE_ID_JQ = ".data.repository.issue.id // empty"
_ISSUE_ID_QUERY = """
query($owner:String!,$name:String!,$num:Int!){
  repository(owner:$owner,name:$name){ issue(number:$num){ id } }
}"""
_ADD_ITEM_MUTATION = """
mutation($project:ID!,$item:ID!){
  addProjectV2ItemById(input:{projectId:$project, contentId:$item}){ item { id } }
}"""


@dataclass(frozen=True)
class _Outcome:
    """Result for one issue URL (``status`` is added or failed), or a url-less board note (board, skip, failed).

    Re-adding an item already on the board succeeds in GitHub's API, so there is no separate "exists" status.
    """

    url: str
    status: str
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.status == "failed"

    def line(self) -> str:
        if not self.url:
            return f"{self.status}: {self.detail}"
        suffix = f" ({self.detail})" if self.detail else ""
        return f"{self.url}: {self.status}{suffix}"


async def _gh(*args: str) -> ShellResult:
    try:
        return await run("gh", *args, timeout=_GH_TIMEOUT_SECONDS)
    except OSError as exc:
        return ShellResult(returncode=127, stdout="", stderr=f"could not run gh: {exc}")


def _first_line(text: str) -> str:
    stripped = text.strip()
    return stripped.splitlines()[0] if stripped else "no output"


def _missing_scopes(result: ShellResult) -> set[str]:
    text = f"{result.stderr}\n{result.stdout}"
    scopes: set[str] = set()
    for pattern in _MISSING_SCOPES_RES:
        match = pattern.search(text)
        if match is not None:
            scopes.update(scope.strip("'\"") for scope in re.split(r"[,\s]+", match.group("scopes")))
    scopes.discard("")
    return scopes


def _project_scope_remedy() -> str:
    for var in _TOKEN_ENV_VARS:
        if os.environ.get(var):
            return f"missing project scope: {var} is set, so gh auth refresh refuses to run; use a token with it"
    return "missing project scope: run gh auth refresh -s project"


def _failure_reason(result: ShellResult) -> str:
    """The remedy when the token lacks the project scope, else gh's first stderr line."""
    if _missing_scopes(result) & _PROJECT_SCOPES:
        return _project_scope_remedy()
    return _first_line(result.stderr)


async def _graphql_id(query: str, jq: str, *fields: str) -> tuple[str | None, str]:
    """Run a GraphQL query whose jq result is one node id: (id, "") or (None, reason), never partial output."""
    result = await _gh("api", "graphql", "-f", f"query={query}", *fields, "--jq", jq)
    if not result.success:
        return None, _failure_reason(result)
    node_id = result.stdout.strip()
    return (node_id, "") if node_id else (None, "empty result")


class _GraphqlFallback:
    """Adds issues through GraphQL, which skips the owner-type lookup that demands read:org."""

    def __init__(self, board_owner: str, number: int) -> None:
        self._board_owner = board_owner
        self._number = number
        self._project_id: str | None = None
        self._project_failure = ""
        self.title = ""

    async def add(self, url: str, issue_owner: str, issue_repo: str, issue_number: int) -> _Outcome:
        """Add the issue the URL names, looked up in the URL's own repository, to the board owner's board."""
        if self._project_id is None and not self._project_failure:
            project, self._project_failure = await _graphql_id(
                _PROJECT_ID_QUERY, _PROJECT_JQ, "-f", f"owner={self._board_owner}", "-F", f"number={self._number}"
            )
            if project is not None:
                self._project_id, _, self.title = project.partition("\t")
        if self._project_id is None:
            detail = (
                f"could not resolve project id for {self._board_owner}#{self._number}: {self._project_failure}; "
                "the board may be org-owned, which needs a token with read:org, or the project number may be wrong"
            )
            return _Outcome(url, "failed", detail)
        issue_id, issue_failure = await _graphql_id(
            _ISSUE_ID_QUERY,
            _ISSUE_ID_JQ,
            "-f",
            f"owner={issue_owner}",
            "-f",
            f"name={issue_repo}",
            "-F",
            f"num={issue_number}",
        )
        if issue_id is None:
            return _Outcome(url, "failed", f"could not resolve issue id: {issue_failure}")
        added = await _gh(
            "api",
            "graphql",
            "-f",
            f"query={_ADD_ITEM_MUTATION}",
            "-F",
            f"project={self._project_id}",
            "-F",
            f"item={issue_id}",
        )
        if not added.success:
            return _Outcome(url, "failed", f"graphql add failed: {_failure_reason(added)}")
        return _Outcome(url, "added")


async def _resolve_repo() -> tuple[str, str] | None:
    result = await _gh("repo", "view", "--json", "owner,name")
    if not result.success:
        return None
    try:
        data = json.loads(result.stdout)
        return str(data["owner"]["login"]), str(data["name"])
    except (ValueError, KeyError, TypeError):
        return None


async def _discover_board(repo_owner: str) -> tuple[int | None, _Outcome]:
    """The repo owner's only project board, with the line saying which board was used or why none was."""
    result = await _gh("project", "list", "--owner", repo_owner, "--limit", "1000", "--format", "json")
    if not result.success:
        scopes = _missing_scopes(result)
        if scopes & _PROJECT_SCOPES:
            reason = _project_scope_remedy()
        elif "read:org" in scopes:
            reason = "missing read:org scope; set task_source.github_project_number or use a token with read:org"
        else:
            reason = _first_line(result.stderr)
        return None, _Outcome("", "failed", f"could not list boards for {repo_owner}: {reason}")
    try:
        numbers = [int(project["number"]) for project in json.loads(result.stdout)["projects"]]
    except (ValueError, KeyError, TypeError):
        return None, _Outcome("", "failed", f"could not read the board list for {repo_owner}")
    if len(numbers) == 1:
        return numbers[0], _Outcome("", "board", f"{repo_owner}#{numbers[0]} (discovered, none configured)")
    found = "no project board found" if not numbers else f"{len(numbers)} project boards found"
    reason = f"{found} for {repo_owner} and task_source.github_project_number is 0; set it to choose a board"
    return None, _Outcome("", "skip", reason)


async def _add_all(urls: list[str], number: int) -> list[_Outcome]:
    """One outcome per URL, preceded by a url-less note when the board was discovered, or alone when skipped."""
    repo = await _resolve_repo()
    if repo is None:
        return [_Outcome(url, "failed", "could not resolve the repository owner with gh repo view") for url in urls]
    repo_owner = repo[0]
    outcomes: list[_Outcome] = []
    discovered_note = False
    if number == 0:
        discovered, note = await _discover_board(repo_owner)
        if discovered is None:
            return [note]
        number = discovered
        outcomes.append(note)
        discovered_note = True
    # The board belongs to the repo owner gh resolves, not github_user: the same owner item-add
    # and Phase D's membership check use, so the fallback cannot add to a different board.
    fallback = _GraphqlFallback(repo_owner, number)
    project_scope_blocked = False
    read_org_blocked = False
    for url in urls:
        match = _ISSUE_URL_RE.search(url)
        if match is None:
            outcomes.append(_Outcome(url, "failed", "not an issue URL"))
            continue
        if project_scope_blocked:
            outcomes.append(_Outcome(url, "failed", _project_scope_remedy()))
            continue
        issue = (match.group("owner"), match.group("repo"), int(match.group("number")))
        if read_org_blocked:
            # item-add would fail the same way (and wait out a timeout) for every later URL.
            outcomes.append(await fallback.add(url, *issue))
            continue
        result = await _gh("project", "item-add", str(number), "--owner", repo_owner, "--url", url)
        if result.success:
            outcomes.append(_Outcome(url, "added"))
            continue
        scopes = _missing_scopes(result)
        if scopes & _PROJECT_SCOPES:
            # The fallback mutation needs this scope too, so it is not attempted.
            project_scope_blocked = True
            outcomes.append(_Outcome(url, "failed", _project_scope_remedy()))
        elif "read:org" in scopes:
            read_org_blocked = True
            outcomes.append(await fallback.add(url, *issue))
        else:
            detail = f"could not add to board: owner {repo_owner}, project {number}: {_first_line(result.stderr)}"
            outcomes.append(_Outcome(url, "failed", detail))
    if fallback.title:
        # Phase D matches membership by title; only the fallback has it, since item-add prints none.
        detail = f'{repo_owner}#{number} "{fallback.title}"' + (
            " (discovered, none configured)" if discovered_note else ""
        )
        outcomes = [_Outcome("", "board", detail), *outcomes[int(discovered_note) :]]
    return outcomes


@app.command("add")
def add_cmd(urls: Annotated[list[str], _URL_ARGUMENT]) -> None:
    """Add issues to the configured project board, printing one line per URL.

    Uses task_source.github_project_number, or the repo owner's only board when it is 0 or the
    configuration cannot be loaded (the load error goes to stderr). When no single board can be
    chosen, one `skip:` line replaces the per-URL lines. A `board:` line names the board used when
    it was discovered or its title is known.
    Exits 0 when every URL was added (or no single board could be chosen), 1 when any URL failed.
    """
    from sova.config.loader import load_config

    try:
        root = asyncio.run(project_root(Path.cwd()))
        number = load_config(root).task_source.github_project_number
    except _CONFIG_ERRORS as exc:
        typer.echo(f"Configuration error: {exc}; discovering the board instead", err=True)
        number = 0
    outcomes = asyncio.run(_add_all(urls, number))
    for outcome in outcomes:
        typer.echo(outcome.line())
    failures = sum(1 for outcome in outcomes if outcome.failed)
    if failures:
        log.warning("board.add_failed", failed=failures, total=len(outcomes))
        raise typer.Exit(code=1)
