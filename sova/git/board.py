"""Project board membership: add issue URLs to a GitHub Projects (v2) board with gh.

Lives below the CLI and the dashboard so both ``sova board add`` and a dashboard service can add a freshly filed
issue to the board without an upward import. Every gh failure becomes a failed outcome with a reason, never an
exception and never a silent success.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from sova.utils.shell import ShellResult, run

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
# A project lookup failing with one of these will fail the same way for every URL (an org-owned board or a
# wrong project number), unlike a timeout or HTTP 5xx; matched ignoring case. "empty result" is a null project.
_DETERMINISTIC_PROJECT_FAILURES = ("could not resolve to a", "empty result")
# gh's owner-type lookup (which needs read:org) can fail with only this text, naming no scope; matched ignoring case.
_UNKNOWN_OWNER_TYPE = "unknown owner type"
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
class BoardOutcome:
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
        self._project_failure: str | None = None
        self.title = ""

    async def add(self, url: str, issue_owner: str, issue_repo: str, issue_number: int) -> BoardOutcome:
        """Add the issue the URL names, looked up in the URL's own repository, to the board owner's board.

        A resolved project id and title are reused. A failed lookup is retried by the next URL only when it may be
        transient; a deterministic one (project not found, null project) is reused, so it costs no further calls.
        """
        if self._project_failure is not None:
            return BoardOutcome(url, "failed", self._project_failure)
        if self._project_id is None:
            project, project_failure = await _graphql_id(
                _PROJECT_ID_QUERY, _PROJECT_JQ, "-f", f"owner={self._board_owner}", "-F", f"number={self._number}"
            )
            if project is None:
                detail = (
                    f"could not resolve project id for {self._board_owner}#{self._number}: {project_failure}; "
                    "the board may be org-owned, which needs a token with read:org, or the project number may be wrong"
                )
                if any(marker in project_failure.lower() for marker in _DETERMINISTIC_PROJECT_FAILURES):
                    self._project_failure = detail
                return BoardOutcome(url, "failed", detail)
            self._project_id, _, self.title = project.partition("\t")
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
            return BoardOutcome(url, "failed", f"could not resolve issue id: {issue_failure}")
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
            return BoardOutcome(url, "failed", f"graphql add failed: {_failure_reason(added)}")
        return BoardOutcome(url, "added")


async def _resolve_repo() -> tuple[str, str] | None:
    result = await _gh("repo", "view", "--json", "owner,name")
    if not result.success:
        return None
    try:
        data = json.loads(result.stdout)
        return str(data["owner"]["login"]), str(data["name"])
    except (ValueError, KeyError, TypeError):
        return None


async def _discover_board(repo_owner: str) -> tuple[int | None, BoardOutcome]:
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
        return None, BoardOutcome("", "failed", f"could not list boards for {repo_owner}: {reason}")
    try:
        numbers = [int(project["number"]) for project in json.loads(result.stdout)["projects"]]
    except (ValueError, KeyError, TypeError):
        return None, BoardOutcome("", "failed", f"could not read the board list for {repo_owner}")
    if len(numbers) == 1:
        return numbers[0], BoardOutcome("", "board", f"{repo_owner}#{numbers[0]} (discovered, none configured)")
    found = "no project board found" if not numbers else f"{len(numbers)} project boards found"
    reason = f"{found} for {repo_owner} and task_source.github_project_number is 0; set it to choose a board"
    return None, BoardOutcome("", "skip", reason)


def issue_url_owner(url: str) -> str:
    """The repository owner an issue URL names, or "" when it is not an issue URL."""
    match = _ISSUE_URL_RE.search(url)
    return match.group("owner") if match else ""


@dataclass
class _BatchState:
    """What an earlier URL of the batch learned about the token, so later URLs skip a call bound to fail."""

    project_scope_blocked: bool = False
    read_org_blocked: bool = False


async def _add_after_item_add_failure(
    url: str,
    result: ShellResult,
    issue: tuple[str, str, int],
    target: tuple[str, int],
    fallback: _GraphqlFallback,
    state: _BatchState,
) -> BoardOutcome:
    """The outcome of one URL whose ``gh project item-add`` failed: a scope remedy, or the GraphQL fallback."""
    scopes = _missing_scopes(result)
    if scopes & _PROJECT_SCOPES:
        # The fallback mutation needs this scope too, so it is not attempted.
        state.project_scope_blocked = True
        return BoardOutcome(url, "failed", _project_scope_remedy())
    if "read:org" in scopes or _UNKNOWN_OWNER_TYPE in result.stderr.lower():
        # gh's owner-type lookup fails for the token, not the URL, so every later item-add would too.
        state.read_org_blocked = True
        return await fallback.add(url, *issue)
    # The cause is unknown and may be specific to this URL, so later URLs still try item-add first.
    outcome = await fallback.add(url, *issue)
    if not outcome.failed:
        return outcome
    repo_owner, number = target
    gh_detail = f"could not add to board: owner {repo_owner}, project {number}: {_first_line(result.stderr)}"
    return BoardOutcome(url, "failed", f"{gh_detail}; fallback: {outcome.detail}")


async def _add_one(url: str, target: tuple[str, int], fallback: _GraphqlFallback, state: _BatchState) -> BoardOutcome:
    """Add one issue URL to board ``target`` (owner, number); never raises for a malformed URL."""
    match = _ISSUE_URL_RE.search(url)
    if match is None:
        return BoardOutcome(url, "failed", "not an issue URL")
    if state.project_scope_blocked:
        return BoardOutcome(url, "failed", _project_scope_remedy())
    try:
        issue = (match.group("owner"), match.group("repo"), int(match.group("number")))
    except ValueError:  # a number past int()'s digit limit; the rest of the batch still runs
        return BoardOutcome(url, "failed", "the issue number is not a usable number")
    if state.read_org_blocked:
        # item-add would fail the same way (and wait out a timeout) for every later URL.
        return await fallback.add(url, *issue)
    repo_owner, number = target
    result = await _gh("project", "item-add", str(number), "--owner", repo_owner, "--url", url)
    if result.success:
        return BoardOutcome(url, "added")
    return await _add_after_item_add_failure(url, result, issue, target, fallback, state)


async def add_issues_to_board(urls: list[str], number: int, *, repo_owner: str = "") -> list[BoardOutcome]:
    """Add issue URLs to project board ``number`` (0 discovers the repo owner's only board).

    ``repo_owner`` names the board's owner for a caller whose working directory is not the repository (a dashboard
    service in a multi-project server); empty resolves it with ``gh repo view`` in the working directory.
    One outcome per URL, preceded by a url-less note when the board was discovered, or alone when skipped."""
    if not repo_owner:
        repo = await _resolve_repo()
        if repo is None:
            reason = "could not resolve the repository owner with gh repo view"
            return [BoardOutcome(url, "failed", reason) for url in urls]
        repo_owner = repo[0]
    discovered_note: BoardOutcome | None = None
    if number == 0:
        discovered, note = await _discover_board(repo_owner)
        if discovered is None:
            return [note]
        number, discovered_note = discovered, note
    # The board belongs to the repo owner gh resolves, not github_user: the same owner item-add
    # and Phase D's membership check use, so the fallback cannot add to a different board.
    fallback = _GraphqlFallback(repo_owner, number)
    state = _BatchState()
    outcomes = [await _add_one(url, (repo_owner, number), fallback, state) for url in urls]
    if fallback.title:
        # Phase D matches membership by title; only the fallback has it, since item-add prints none.
        suffix = " (discovered, none configured)" if discovered_note else ""
        return [BoardOutcome("", "board", f'{repo_owner}#{number} "{fallback.title}"{suffix}'), *outcomes]
    return [discovered_note, *outcomes] if discovered_note else outcomes
