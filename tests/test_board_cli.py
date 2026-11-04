"""Tests for `sova board add` (sova.cli.commands.board).

`gh` is replaced by a scripted fake keyed on the subcommand, so each branch of the old
prose (scope remedies, GraphQL fallback, partial failure, existing item) is pinned
without a network call.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import SQLAlchemyError
from typer.testing import CliRunner

from sova.cli.app import app
from sova.cli.commands import audit, board
from sova.utils.shell import ShellResult

runner = CliRunner()

# Captured before the autouse fixture replaces it, so the root-callback test can run the real function.
_REAL_INIT_LLM_PROVIDER = importlib.import_module("sova.cli.app")._init_llm_provider


@pytest.fixture(autouse=True)
def _no_provider_init(monkeypatch: pytest.MonkeyPatch) -> None:
    """The app callback would load the real config and build an LLM provider; these commands never use either."""
    # sova.cli.app names the Typer object, so the module is looked up by import.
    monkeypatch.setattr(importlib.import_module("sova.cli.app"), "_init_llm_provider", lambda: None)


@pytest.fixture(autouse=True)
def _cwd_is_the_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real root lookup runs git, whose debug log line lands in the captured stdout these tests compare."""

    async def cwd_root(cwd: Path) -> Path:
        return cwd

    monkeypatch.setattr(board, "project_root", cwd_root)


URL_1 = "https://github.com/acme/widgets/issues/11"
URL_2 = "https://github.com/acme/widgets/issues/12"
REPO_JSON = json.dumps({"owner": {"login": "acme"}, "name": "widgets"})
READ_ORG = ShellResult(1, "", "error: your authentication token is missing required scopes [read:org]")
PROJECT_SCOPE = ShellResult(1, "", "error: your authentication token is missing required scopes [project]")
# What gh prints for a token with no project scope at all: the server names read:project, not project.
READ_PROJECT_SCOPE = ShellResult(1, "", "error: your authentication token is missing required scopes [read:project]")
READ_ORG_AND_READ_PROJECT = ShellResult(
    1, "", "error: your authentication token is missing required scopes [read:org read:project]"
)
# What `gh api graphql` prints: GitHub's INSUFFICIENT_SCOPES message, unlike the `gh project` wording above.
GRAPHQL_PROJECT_SCOPE = ShellResult(
    1,
    "",
    "gh: Your token has not been granted the required scopes to execute this query. The 'id' field requires "
    "one of the following scopes: ['read:project'], but your token has only been granted the: ['repo'] scopes. "
    "Please modify your token's scopes at: https://github.com/settings/tokens.",
)
GRAPHQL_MUTATION_SCOPE = ShellResult(
    1,
    "",
    "gh: Your token has not been granted the required scopes to execute this query. The 'addProjectV2ItemById' "
    "field requires one of the following scopes: ['project'], but your token has only been granted the: "
    "['read:project', 'repo'] scopes.",
)
_TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN")


class FakeGh:
    """Records every gh call; `item_add` maps the --url argument to a result."""

    def __init__(
        self,
        item_add: Callable[[str], ShellResult],
        graphql: Callable[[str], ShellResult] | None = None,
        project_list: ShellResult | None = None,
    ):
        self._project_list = project_list or ShellResult(1, "", "project list not scripted")
        self.calls: list[tuple[str, ...]] = []
        self._item_add = item_add
        self._graphql = graphql or (lambda _query: ShellResult(0, "NODE_ID\n", ""))

    async def __call__(self, *args: str) -> ShellResult:
        self.calls.append(args)
        if args[:2] == ("repo", "view"):
            return ShellResult(0, REPO_JSON, "")
        if args[:2] == ("project", "list"):
            return self._project_list
        if args[:2] == ("project", "item-add"):
            return self._item_add(args[args.index("--url") + 1])
        if args[:2] == ("api", "graphql"):
            return self._graphql(next(arg for arg in args if arg.startswith("query=")))
        raise AssertionError(f"unexpected gh call {args}")

    def count(self, *prefix: str) -> int:
        return sum(1 for call in self.calls if call[: len(prefix)] == prefix)


def _install(monkeypatch: pytest.MonkeyPatch, fake: FakeGh) -> None:
    monkeypatch.setattr(board, "_gh", fake)


def _run(urls: list[str]) -> list[board._Outcome]:
    return asyncio.run(board._add_all(urls, 7))


class TestAddAll:
    def test_all_added(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, FakeGh(lambda _url: ShellResult(0, "", "")))

        assert [o.line() for o in _run([URL_1, URL_2])] == [f"{URL_1}: added", f"{URL_2}: added"]

    def test_partial_failure_still_reports_every_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def item_add(url: str) -> ShellResult:
            return ShellResult(0, "", "") if url == URL_1 else ShellResult(1, "", "GraphQL: not found")

        _install(monkeypatch, FakeGh(item_add))

        outcomes = _run([URL_1, URL_2])

        assert [o.status for o in outcomes] == ["added", "failed"]
        assert "owner acme, project 7" in outcomes[1].detail

    def test_nonzero_exit_with_partial_stdout_is_a_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, FakeGh(lambda _url: ShellResult(1, '{"id":"PVTI_partial"}', "boom")))

        (outcome,) = _run([URL_1])

        assert outcome.failed

    @pytest.mark.parametrize("stderr", ["item already exists in project", "token already in use"])
    def test_an_item_add_failure_is_never_reported_as_existing(
        self, monkeypatch: pytest.MonkeyPatch, stderr: str
    ) -> None:
        """Re-adding an item succeeds in the API, so error text with "already" is still a failure."""
        _install(monkeypatch, FakeGh(lambda _url: ShellResult(1, "", stderr)))

        (outcome,) = _run([URL_1])

        assert outcome.status == "failed"

    @pytest.mark.parametrize("failure", [PROJECT_SCOPE, READ_PROJECT_SCOPE, READ_ORG_AND_READ_PROJECT])
    def test_project_scope_prints_remedy_and_skips_the_fallback(
        self, monkeypatch: pytest.MonkeyPatch, failure: ShellResult
    ) -> None:
        for var in _TOKEN_VARS:
            monkeypatch.delenv(var, raising=False)
        fake = FakeGh(lambda _url: failure)
        _install(monkeypatch, fake)

        outcomes = _run([URL_1, URL_2])

        assert all(o.failed and "gh auth refresh -s project" in o.detail for o in outcomes)
        assert fake.count("api", "graphql") == 0
        assert fake.count("project", "item-add") == 1

    @pytest.mark.parametrize("var", _TOKEN_VARS)
    def test_project_scope_with_a_token_variable_set_names_it(self, monkeypatch: pytest.MonkeyPatch, var: str) -> None:
        for other in _TOKEN_VARS:
            monkeypatch.delenv(other, raising=False)
        monkeypatch.setenv(var, "x")
        _install(monkeypatch, FakeGh(lambda _url: PROJECT_SCOPE))

        (outcome,) = _run([URL_1])

        assert f"{var} is set" in outcome.detail
        assert "gh auth refresh -s project" not in outcome.detail

    def test_both_scopes_missing_is_treated_as_project(self, monkeypatch: pytest.MonkeyPatch) -> None:
        both = ShellResult(1, "", "missing required scopes [project, read:org]")
        fake = FakeGh(lambda _url: both)
        _install(monkeypatch, fake)

        (outcome,) = _run([URL_1])

        assert outcome.failed
        assert fake.count("api", "graphql") == 0

    def test_read_org_uses_the_graphql_fallback_on_the_repo_owners_board(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The fallback targets the same owner item-add used, never github_user's same-numbered board."""
        fake = FakeGh(lambda _url: READ_ORG)
        _install(monkeypatch, fake)

        outcomes = _run([URL_1, URL_2])

        assert [o.status for o in outcomes] == ["added", "added"]
        project_query = next(call for call in fake.calls if any("projectV2" in arg for arg in call))
        assert "owner=acme" in project_query
        # The project id is resolved once, then one issue lookup and one mutation per URL.
        assert fake.count("api", "graphql") == 1 + 2 * 2
        # Once item-add reports the missing scope, later URLs go straight to the fallback.
        assert fake.count("project", "item-add") == 1

    def test_fallback_looks_the_issue_up_in_the_urls_own_repo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """gh repo view resolves acme/widgets, but the URL names upstream/gadgets: the URL wins."""
        fake = FakeGh(lambda _url: READ_ORG)
        _install(monkeypatch, fake)

        (outcome,) = _run(["https://github.com/upstream/gadgets/issues/5"])

        assert outcome.status == "added"
        issue_query = next(call for call in fake.calls if any("repository(" in arg for arg in call))
        assert "owner=upstream" in issue_query
        assert "name=gadgets" in issue_query
        assert "num=5" in issue_query
        project_query = next(call for call in fake.calls if any("projectV2" in arg for arg in call))
        assert "owner=acme" in project_query

    def test_fallback_project_failure_keeps_gh_error_and_words_the_remedy_conditionally(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, FakeGh(lambda _url: READ_ORG, lambda _query: ShellResult(1, "", "GraphQL: not found")))

        (outcome,) = _run([URL_1])

        assert outcome.failed
        assert "GraphQL: not found" in outcome.detail
        assert "may be org-owned" in outcome.detail
        assert "read:org" in outcome.detail

    def test_fallback_project_failure_is_looked_up_once_for_all_urls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeGh(lambda _url: READ_ORG, lambda _query: ShellResult(1, "", "GraphQL: not found"))
        _install(monkeypatch, fake)

        outcomes = _run([URL_1, URL_2])

        assert [o.failed for o in outcomes] == [True, True]
        assert fake.count("api", "graphql") == 1

    def test_fallback_project_scope_failure_gives_the_refresh_remedy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in _TOKEN_VARS:
            monkeypatch.delenv(var, raising=False)
        _install(monkeypatch, FakeGh(lambda _url: READ_ORG, lambda _query: GRAPHQL_PROJECT_SCOPE))

        (outcome,) = _run([URL_1])

        assert "gh auth refresh -s project" in outcome.detail

    def test_fallback_mutation_scope_failure_gives_the_refresh_remedy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A token with read:project but not project passes both lookups and fails only on the mutation."""
        for var in _TOKEN_VARS:
            monkeypatch.delenv(var, raising=False)

        def graphql(query: str) -> ShellResult:
            return GRAPHQL_MUTATION_SCOPE if "addProjectV2ItemById" in query else ShellResult(0, "NODE_ID\n", "")

        _install(monkeypatch, FakeGh(lambda _url: READ_ORG, graphql))

        (outcome,) = _run([URL_1])

        assert outcome.failed
        assert "gh auth refresh -s project" in outcome.detail

    def test_fallback_names_the_board_title_for_phase_d(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The title comes from the fallback's own project query, so Phase D needs no `gh project view`."""

        def graphql(query: str) -> ShellResult:
            return ShellResult(0, "PVT_1\tSOVA Roadmap\n", "") if "projectV2" in query else ShellResult(0, "NODE\n", "")

        _install(monkeypatch, FakeGh(lambda _url: READ_ORG, graphql))

        outcomes = _run([URL_1])

        assert [o.line() for o in outcomes] == ['board: acme#7 "SOVA Roadmap"', f"{URL_1}: added"]

    def test_fallback_title_joins_the_discovered_board_note(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def graphql(query: str) -> ShellResult:
            return ShellResult(0, "PVT_1\tSOVA Roadmap\n", "") if "projectV2" in query else ShellResult(0, "NODE\n", "")

        _install(monkeypatch, FakeGh(lambda _url: READ_ORG, graphql, project_list=ONE_BOARD))

        outcomes = asyncio.run(board._add_all([URL_1], 0))

        assert [o.line() for o in outcomes] == [
            'board: acme#4 "SOVA Roadmap" (discovered, none configured)',
            f"{URL_1}: added",
        ]

    def test_fallback_failure_is_reported_per_issue(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def graphql(query: str) -> ShellResult:
            if "addProjectV2ItemById" in query:
                return ShellResult(1, '{"errors":[{"message":"nope"}]}', "GraphQL: nope")
            return ShellResult(0, "NODE_ID\n", "")

        _install(monkeypatch, FakeGh(lambda _url: READ_ORG, graphql))

        outcomes = _run([URL_1, URL_2])

        assert [o.failed for o in outcomes] == [True, True]
        assert "nope" in outcomes[0].detail

    def test_fallback_with_empty_id_is_a_failure_not_a_blank_mutation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeGh(lambda _url: READ_ORG, lambda _query: ShellResult(0, "\n", ""))
        _install(monkeypatch, fake)

        (outcome,) = _run([URL_1])

        assert outcome.failed
        assert "project id" in outcome.detail

    def test_fallback_ignores_stdout_of_a_failed_id_lookup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeGh(lambda _url: READ_ORG, lambda _query: ShellResult(1, '{"errors":[]}', "GraphQL error"))
        _install(monkeypatch, fake)

        (outcome,) = _run([URL_1])

        assert outcome.failed
        assert fake.count("api", "graphql") == 1

    def test_github_enterprise_issue_url_reaches_item_add(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ghe_url = "https://github.example.com/acme/widgets/issues/5"
        fake = FakeGh(lambda _url: ShellResult(0, "", ""))
        _install(monkeypatch, fake)

        (outcome,) = _run([ghe_url])

        assert outcome.line() == f"{ghe_url}: added"
        assert fake.count("project", "item-add") == 1

    def test_github_enterprise_issue_url_is_parsed_for_the_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeGh(lambda _url: READ_ORG)
        _install(monkeypatch, fake)

        (outcome,) = _run(["https://ghe.corp.example/upstream/gadgets/issues/5#issuecomment-1"])

        assert outcome.status == "added"
        issue_query = next(call for call in fake.calls if any("repository(" in arg for arg in call))
        assert "owner=upstream" in issue_query
        assert "name=gadgets" in issue_query
        assert "num=5" in issue_query

    def test_non_issue_url_fails_without_calling_gh_for_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeGh(lambda _url: ShellResult(0, "", ""))
        _install(monkeypatch, fake)

        (outcome,) = _run(["https://github.com/acme/widgets/pull/3"])

        assert outcome.failed
        assert fake.count("project", "item-add") == 0

    def test_unresolvable_repo_fails_every_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def broken(*args: str) -> ShellResult:
            return ShellResult(1, "", "not a repo")

        monkeypatch.setattr(board, "_gh", broken)

        assert all(o.failed for o in _run([URL_1, URL_2]))

    def test_fallback_issue_id_failure_skips_the_mutation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def graphql(query: str) -> ShellResult:
            if "repository(" in query:
                return ShellResult(1, "", "GraphQL: Could not resolve to an Issue")
            return ShellResult(0, "NODE_ID\n", "")

        fake = FakeGh(lambda _url: READ_ORG, graphql)
        _install(monkeypatch, fake)

        (outcome,) = _run([URL_1])

        assert outcome.failed
        assert outcome.detail == "could not resolve issue id: GraphQL: Could not resolve to an Issue"
        assert not any(any("addProjectV2ItemById" in arg for arg in call) for call in fake.calls)

    @pytest.mark.parametrize(
        "stdout",
        ["not json", json.dumps({"name": "widgets"}), json.dumps({"owner": None, "name": "widgets"})],
        ids=["invalid-json", "missing-owner", "null-owner"],
    )
    def test_malformed_repo_view_fails_every_url(self, monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
        async def repo_view(*args: str) -> ShellResult:
            assert args[:2] == ("repo", "view")
            return ShellResult(0, stdout, "")

        monkeypatch.setattr(board, "_gh", repo_view)

        outcomes = _run([URL_1, URL_2])

        assert all(o.failed for o in outcomes)
        assert all("gh repo view" in o.detail for o in outcomes)


ONE_BOARD = ShellResult(0, json.dumps({"projects": [{"number": 4}], "totalCount": 1}), "")
NO_BOARD = ShellResult(0, json.dumps({"projects": [], "totalCount": 0}), "")
TWO_BOARDS = ShellResult(0, json.dumps({"projects": [{"number": 4}, {"number": 9}], "totalCount": 2}), "")


class TestBoardDiscovery:
    """With no configured board number, the repo owner's only board is used, as before the #1363 cut."""

    def test_exactly_one_board_is_used_and_named(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = FakeGh(lambda _url: ShellResult(0, "", ""), project_list=ONE_BOARD)
        _install(monkeypatch, fake)

        outcomes = asyncio.run(board._add_all([URL_1], 0))

        assert [o.line() for o in outcomes] == ["board: acme#4 (discovered, none configured)", f"{URL_1}: added"]
        add_call = next(call for call in fake.calls if call[:2] == ("project", "item-add"))
        assert add_call[2] == "4"
        assert ("project", "list", "--owner", "acme", "--limit", "1000", "--format", "json") in fake.calls

    @pytest.mark.parametrize(
        ("listing", "expected"), [(NO_BOARD, "no project board found"), (TWO_BOARDS, "2 project boards")]
    )
    def test_zero_or_many_boards_skip_with_the_reason(
        self, monkeypatch: pytest.MonkeyPatch, listing: ShellResult, expected: str
    ) -> None:
        fake = FakeGh(lambda _url: ShellResult(0, "", ""), project_list=listing)
        _install(monkeypatch, fake)

        (outcome,) = asyncio.run(board._add_all([URL_1, URL_2], 0))

        assert outcome.status == "skip"
        assert not outcome.failed
        assert outcome.line().startswith("skip: ")
        assert expected in outcome.line()
        assert fake.count("project", "item-add") == 0

    def test_a_failed_listing_for_read_org_names_the_scope_and_the_config_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, FakeGh(lambda _url: ShellResult(0, "", ""), project_list=READ_ORG))

        (outcome,) = asyncio.run(board._add_all([URL_1], 0))

        assert outcome.failed
        assert "missing read:org scope" in outcome.detail
        assert "task_source.github_project_number" in outcome.detail

    @pytest.mark.parametrize("listing", [PROJECT_SCOPE, READ_PROJECT_SCOPE, READ_ORG_AND_READ_PROJECT])
    def test_a_listing_blocked_by_project_scope_gives_the_refresh_remedy(
        self, monkeypatch: pytest.MonkeyPatch, listing: ShellResult
    ) -> None:
        for var in _TOKEN_VARS:
            monkeypatch.delenv(var, raising=False)
        _install(monkeypatch, FakeGh(lambda _url: ShellResult(0, "", ""), project_list=listing))

        (outcome,) = asyncio.run(board._add_all([URL_1], 0))

        assert outcome.failed
        assert "gh auth refresh -s project" in outcome.detail

    @pytest.mark.parametrize("listing", [ShellResult(1, "", "boom"), ShellResult(0, "not json", "")])
    def test_a_failed_listing_is_a_failure_not_a_skip(
        self, monkeypatch: pytest.MonkeyPatch, listing: ShellResult
    ) -> None:
        _install(monkeypatch, FakeGh(lambda _url: ShellResult(0, "", ""), project_list=listing))

        (outcome,) = asyncio.run(board._add_all([URL_1], 0))

        assert outcome.failed
        assert outcome.line().startswith("failed: could not")


class TestGh:
    def test_gh_passes_args_and_timeout_to_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, object] = {}

        async def fake_run(*args: str, timeout: int) -> ShellResult:
            seen["args"] = args
            seen["timeout"] = timeout
            return ShellResult(0, "ok", "")

        monkeypatch.setattr(board, "run", fake_run)

        result = asyncio.run(board._gh("repo", "view"))

        assert result.success
        assert seen == {"args": ("gh", "repo", "view"), "timeout": board._GH_TIMEOUT_SECONDS}

    def test_gh_missing_binary_is_a_failed_result_not_an_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def missing(*_args: str, timeout: int) -> ShellResult:
            raise FileNotFoundError("No such file or directory: 'gh'")

        monkeypatch.setattr(board, "run", missing)

        result = asyncio.run(board._gh("repo", "view"))

        assert not result.success
        assert result.returncode == 127
        assert result.stdout == ""
        assert result.stderr.startswith("could not run gh:")


def _config(number: int) -> SimpleNamespace:
    return SimpleNamespace(task_source=SimpleNamespace(github_project_number=number))


class TestAddCommand:
    def test_unconfigured_board_with_no_single_board_skips_with_exit_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeGh(lambda _url: ShellResult(0, "", ""), project_list=TWO_BOARDS)
        _install(monkeypatch, fake)
        monkeypatch.setattr("sova.config.loader.load_config", lambda _root: _config(0))

        result = runner.invoke(app, ["board", "add", URL_1])

        assert result.exit_code == 0
        assert result.stdout.startswith("skip:")
        assert fake.count("project", "item-add") == 0

    def test_failed_discovery_exits_nonzero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, FakeGh(lambda _url: ShellResult(0, "", ""), project_list=ShellResult(1, "", "boom")))
        monkeypatch.setattr("sova.config.loader.load_config", lambda _root: _config(0))

        result = runner.invoke(app, ["board", "add", URL_1])

        assert result.exit_code == 1
        assert result.stdout.startswith("failed:")

    def test_prints_one_line_per_url_and_exits_nonzero_on_a_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def item_add(url: str) -> ShellResult:
            return ShellResult(0, "", "") if url == URL_1 else ShellResult(1, "", "boom")

        _install(monkeypatch, FakeGh(item_add))
        monkeypatch.setattr("sova.config.loader.load_config", lambda _root: _config(7))

        result = runner.invoke(app, ["board", "add", URL_1, URL_2])

        assert result.exit_code == 1
        lines = result.stdout.strip().splitlines()
        assert lines[0] == f"{URL_1}: added"
        assert lines[1].startswith(f"{URL_2}: failed")

    @pytest.mark.parametrize(
        "error",
        [
            RuntimeError("invalid JSON value for key"),
            PermissionError("sova.toml"),
            sqlite3.OperationalError("database is locked"),
            SQLAlchemyError("connection lost"),
        ],
        ids=["runtime", "os", "sqlite", "sqlalchemy"],
    )
    def test_config_load_failure_falls_back_to_discovery_and_says_so(
        self, monkeypatch: pytest.MonkeyPatch, error: Exception
    ) -> None:
        """A broken config row used to drop board membership entirely; discovery still finds the only board."""
        fake = FakeGh(lambda _url: ShellResult(0, "", ""), project_list=ONE_BOARD)
        _install(monkeypatch, fake)

        def broken(_root: object) -> SimpleNamespace:
            raise error

        monkeypatch.setattr("sova.config.loader.load_config", broken)

        result = runner.invoke(app, ["board", "add", URL_1])

        assert result.exit_code == 0
        assert "Configuration error" in result.output
        assert "Traceback" not in result.output
        assert result.stdout.strip().splitlines() == [
            "board: acme#4 (discovered, none configured)",
            f"{URL_1}: added",
        ]
        add_call = next(call for call in fake.calls if call[:2] == ("project", "item-add"))
        assert add_call[2] == "4"

    def test_config_is_read_from_the_git_root_not_the_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """From a subdirectory the configured board must still be found, as `sova audit` judges from the root."""
        subdir = tmp_path / "pkg" / "sub"
        subdir.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        monkeypatch.chdir(subdir)
        monkeypatch.setattr(board, "project_root", audit.project_root)
        seen: list[Path] = []

        def load(root: Path) -> SimpleNamespace:
            seen.append(root)
            return _config(7)

        monkeypatch.setattr("sova.config.loader.load_config", load)
        _install(monkeypatch, FakeGh(lambda _url: ShellResult(0, "", "")))

        result = runner.invoke(app, ["board", "add", URL_1])

        assert result.exit_code == 0
        assert [path.resolve() for path in seen] == [tmp_path.resolve()]


class TestRootCallback:
    def test_unreadable_config_does_not_crash_before_board_add(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The root callback loads config before any subcommand; board add must not die in it on an OSError."""
        monkeypatch.chdir(tmp_path)
        app_module = importlib.import_module("sova.cli.app")

        def unreadable(*_args: object, **_kwargs: object) -> None:
            raise PermissionError("sova.toml")

        monkeypatch.setattr(app_module, "_init_llm_provider", _REAL_INIT_LLM_PROVIDER)
        monkeypatch.setattr(app_module, "load_config", unreadable)
        monkeypatch.setattr("sova.config.loader.load_config", unreadable)
        _install(monkeypatch, FakeGh(lambda _url: ShellResult(0, "", "")))

        result = runner.invoke(app, ["board", "add", URL_1])

        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "Traceback" not in result.output
        assert "Configuration error" in result.output
