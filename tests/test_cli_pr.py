"""Behavioural tests for the `sova` PR commands (`sova.cli.commands.pr`), driven through `CliRunner`.

Each test runs the real command against a real temporary git project and the shared test database, and fakes only
the process boundary: a scripted `gh` on PATH (`fake_gh`) and the LLM provider (`fake_provider`). The
`no_cli_provider_init` fixture keeps the root callback from starting a real provider, and `quiet_logs` keeps log
lines out of stdout, which is the contract of `linked-issues` and `pr-verdict` (stderr carries the diagnostics).
Nothing inside `sova/cli/commands/` is patched. Configuration comes from `SOVA_*` environment variables, which
`monkeypatch` scopes to the test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sova.cli.app import app
from sova.ipc.handoff import DashboardHandoff, write_handoff_file
from tests.process_fakes import FakeAgentRuntime, FakeGh, FakeLLMProvider, add_origin, git

pytestmark = pytest.mark.usefixtures("reset_db_rows", "no_cli_provider_init", "quiet_logs")

runner = CliRunner()


@pytest.fixture
def pr_project(git_project: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real git project whose config names the GitHub repository `owner/repo`, with no GitHub user."""
    monkeypatch.setenv("SOVA_GITHUB_REPO", "owner/repo")
    monkeypatch.setenv("SOVA_GITHUB_USER", "")
    return git_project


def _invoke(*args: str, project: Path) -> object:
    return runner.invoke(app, [*args, "--project", str(project)])


# ---------------------------------------------------------------------------
# address-pr
# ---------------------------------------------------------------------------


class TestAddressPr:
    def test_runs_the_develop_command_and_reports_its_cost(
        self, pr_project: Path, fake_provider: FakeLLMProvider
    ) -> None:
        commands = pr_project / ".claude" / "commands"
        commands.mkdir(parents=True)
        (commands / "develop.md").write_text("---\nname: develop\n---\nDevelop.\n", encoding="utf-8")

        result = _invoke("address-pr", "10", project=pr_project)

        assert result.exit_code == 0
        assert "Addressing review comments on PR #10" in result.output
        assert "Done. Cost: $0.0123" in result.output
        (call,) = fake_provider.calls
        assert call["prompt"] == "/develop Address all review comments on PR #10"
        assert call["cwd"] == pr_project

    def test_a_missing_develop_command_fails_before_the_provider_is_called(
        self, pr_project: Path, fake_provider: FakeLLMProvider
    ) -> None:
        result = _invoke("address-pr", "10", project=pr_project)

        assert result.exit_code != 0
        assert fake_provider.calls == []


# ---------------------------------------------------------------------------
# maintain-pr
# ---------------------------------------------------------------------------


@pytest.fixture
def pr_checkout(pr_project: Path, tmp_path: Path, fake_gh: FakeGh) -> Path:
    """A project with a bare `origin`, checked out on the PR branch `feat/pr-10` that `gh pr checkout` would leave."""
    add_origin(pr_project, tmp_path / "origin.git")
    git(pr_project, "checkout", "-q", "-b", "feat/pr-10")
    (pr_project / "feature.txt").write_text("feature\n", encoding="utf-8")
    git(pr_project, "add", "feature.txt")
    git(pr_project, "commit", "-q", "-m", "feat(cli): add the feature")
    git(pr_project, "push", "-q", "-u", "origin", "feat/pr-10")
    fake_gh.respond("pr", "checkout", "10")
    return pr_project


def _advance_main(project: Path, tmp_path: Path, filename: str) -> None:
    """Commit *filename* to origin's `main` from a second clone, so the PR branch falls behind."""
    other = tmp_path / "other-clone"
    git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), str(other))
    (other / filename).write_text("main moved\n", encoding="utf-8")
    git(other, "add", filename)
    git(other, "commit", "-q", "-m", "chore(docs): move main")
    git(other, "push", "-q", "origin", "main")


class TestMaintainPr:
    def test_rebases_the_pr_branch_on_main_and_force_pushes_it(
        self, pr_checkout: Path, tmp_path: Path, fake_gh: FakeGh
    ) -> None:
        _advance_main(pr_checkout, tmp_path, "main-only.txt")

        result = _invoke("maintain-pr", "10", project=pr_checkout)

        assert result.exit_code == 0
        assert "PR #10 rebased and pushed." in result.output
        git(pr_checkout, "fetch", "-q", "origin")
        assert git(pr_checkout, "merge-base", "--is-ancestor", "origin/main", "HEAD") == ""
        assert git(pr_checkout, "rev-parse", "HEAD") == git(pr_checkout, "rev-parse", "origin/feat/pr-10")
        assert fake_gh.called_with("pr", "checkout", "10")

    def test_a_failed_checkout_exits_1_with_gh_stderr(self, pr_project: Path, fake_gh: FakeGh) -> None:
        fake_gh.respond("pr", "checkout", stderr="no pull request found", returncode=1)

        result = _invoke("maintain-pr", "10", project=pr_project)

        assert result.exit_code == 1
        assert "Failed to checkout PR: no pull request found" in result.output

    def test_a_missing_origin_exits_1_at_the_fetch(self, pr_project: Path, fake_gh: FakeGh) -> None:
        fake_gh.respond("pr", "checkout", "10")

        result = _invoke("maintain-pr", "10", project=pr_project)

        assert result.exit_code == 1
        assert "Failed to fetch main" in result.output

    def test_a_conflicting_rebase_exits_1_and_asks_for_manual_resolution(
        self, pr_checkout: Path, tmp_path: Path
    ) -> None:
        _advance_main(pr_checkout, tmp_path, "feature.txt")

        result = _invoke("maintain-pr", "10", project=pr_checkout)

        assert result.exit_code == 1
        assert "Rebase failed. Resolve conflicts manually." in result.output
        git(pr_checkout, "rebase", "--abort")

    def test_a_rejected_push_exits_1(self, pr_checkout: Path, tmp_path: Path) -> None:
        _advance_main(pr_checkout, tmp_path, "main-only.txt")
        hook = tmp_path / "origin.git" / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'push rejected by policy' >&2\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)

        result = _invoke("maintain-pr", "10", project=pr_checkout)

        assert result.exit_code == 1
        assert "Push failed" in result.output
        assert "push rejected by policy" in result.output


# ---------------------------------------------------------------------------
# review-pr
# ---------------------------------------------------------------------------


class TestReviewPr:
    def test_an_unreadable_linked_issue_exits_1_without_reviewing(self, pr_project: Path, fake_gh: FakeGh) -> None:
        fake_gh.respond("pr", "view", stderr="HTTP 502", returncode=1)

        result = _invoke("review-pr", "10", project=pr_project)

        assert result.exit_code == 1
        assert "Could not resolve the linked issue of PR #10" in result.output

    def test_a_pr_with_no_linked_issue_is_reviewed_on_its_own_and_a_failed_review_exits_1(
        self, pr_project: Path, fake_gh: FakeGh, fake_runtime: FakeAgentRuntime
    ) -> None:
        fake_gh.respond_json("pr", "view", payload={"body": "", "headRefName": "feat/x"})

        result = _invoke("review-pr", "10", project=pr_project)

        assert result.exit_code == 1
        assert "PR #10 has no linked issue, reviewing the PR on its own." in result.stderr
        assert "Review failed" in result.stderr
        assert fake_gh.called_with("pr", "diff", "10")


# ---------------------------------------------------------------------------
# linked-issues
# ---------------------------------------------------------------------------


def _pr_body(fake_gh: FakeGh, body: str) -> None:
    fake_gh.respond_json("pr", "view", "--json", "body", payload={"body": body})


class TestLinkedIssues:
    def test_prints_closes_and_part_of_as_json_on_stdout(self, pr_project: Path, fake_gh: FakeGh) -> None:
        _pr_body(fake_gh, "Closes #12\n\nPart of #30\n\nSee also #99")

        result = _invoke("linked-issues", "10", project=pr_project)

        assert result.exit_code == 0
        assert json.loads(result.stdout) == {"closes": ["12"], "part_of": ["30"]}

    def test_an_unreadable_body_exits_1_and_prints_nothing_on_stdout(self, pr_project: Path, fake_gh: FakeGh) -> None:
        fake_gh.respond("pr", "view", stderr="HTTP 404", returncode=1)

        result = _invoke("linked-issues", "10", project=pr_project)

        assert result.exit_code == 1
        assert result.stdout == ""
        assert "Could not read the body of PR #10." in result.stderr

    def test_apply_and_mark_in_review_are_mutually_exclusive(self, pr_project: Path, fake_gh: FakeGh) -> None:
        result = _invoke("linked-issues", "10", "--apply", "--mark-in-review", project=pr_project)

        assert result.exit_code == 2
        assert "are separate steps" in result.stderr
        assert fake_gh.calls == []

    def test_apply_refuses_a_pr_that_is_not_merged(self, pr_project: Path, fake_gh: FakeGh) -> None:
        fake_gh.respond("pr", "view", "--jq", ".state", stdout="OPEN\n")

        result = _invoke("linked-issues", "10", "--apply", project=pr_project)

        assert result.exit_code == 1
        assert "PR #10 is not merged (OPEN): no issue updated." in result.stderr

    def test_apply_reports_an_unreadable_state_as_unknown_not_as_open(self, pr_project: Path, fake_gh: FakeGh) -> None:
        fake_gh.respond("pr", "view", stderr="bad credentials", returncode=1)

        result = _invoke("linked-issues", "10", "--apply", project=pr_project)

        assert result.exit_code == 1
        assert "gh pr view 10 failed: bad credentials" in result.stderr
        assert "state unknown" in result.stderr

    def test_apply_on_a_jira_project_exits_2(
        self, pr_project: Path, fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SOVA_TASK_TYPE", "jira")

        result = _invoke("linked-issues", "10", "--apply", project=pr_project)

        assert result.exit_code == 2
        assert "--apply reads GitHub closing keywords" in result.stderr

    def test_apply_closes_the_issue_a_merged_pr_closes_and_leaves_a_part_of_issue(
        self, pr_project: Path, fake_gh: FakeGh
    ) -> None:
        _pr_body(fake_gh, "Closes #12\n\nPart of #30")
        fake_gh.respond("pr", "view", "--jq", ".state", stdout="MERGED\n")
        fake_gh.respond("issue", "close", "12")

        result = _invoke("linked-issues", "10", "--apply", project=pr_project)

        assert result.exit_code == 0
        assert result.stdout.splitlines() == ["issue #12 moved to done", "issue #30 left open (Part of): unchanged"]
        assert fake_gh.called_with("issue", "close", "12")
        assert not fake_gh.called_with("issue", "close", "30")

    def test_apply_exits_1_when_an_issue_could_not_be_moved(self, pr_project: Path, fake_gh: FakeGh) -> None:
        _pr_body(fake_gh, "Closes #12")
        fake_gh.respond("pr", "view", "--jq", ".state", stdout="MERGED\n")
        fake_gh.respond("issue", "close", "12", stderr="HTTP 403", returncode=1)

        result = _invoke("linked-issues", "10", "--apply", project=pr_project)

        assert result.exit_code == 1
        assert "issue #12 not updated" in result.stdout

    def test_apply_on_a_merged_pr_that_closes_nothing_says_so(self, pr_project: Path, fake_gh: FakeGh) -> None:
        _pr_body(fake_gh, "Just a refactor.")
        fake_gh.respond("pr", "view", "--jq", ".state", stdout="MERGED\n")

        result = _invoke("linked-issues", "10", "--apply", project=pr_project)

        assert result.exit_code == 0
        assert result.stdout == "PR #10 closes no issue: no issue updated.\n"

    def test_mark_in_review_moves_every_linked_issue_of_an_open_pr(self, pr_project: Path, fake_gh: FakeGh) -> None:
        _pr_body(fake_gh, "Closes #12\n\nPart of #30")
        fake_gh.respond("pr", "view", "--jq", ".state", stdout="OPEN\n")
        fake_gh.respond_json("issue", "view", payload={"state": "OPEN", "labels": [{"name": "agent:in-progress"}]})
        fake_gh.respond("api", "--method", "PATCH")

        result = _invoke("linked-issues", "10", "--mark-in-review", project=pr_project)

        assert result.exit_code == 0
        assert result.stdout.splitlines() == ["issue #12 moved to in_review", "issue #30 moved to in_review"]
        assert fake_gh.called_with("api", "repos/owner/repo/issues/12", "PATCH")
        assert fake_gh.called_with("api", "repos/owner/repo/issues/30", "PATCH")

    def test_mark_in_review_refuses_a_pr_that_is_not_open(self, pr_project: Path, fake_gh: FakeGh) -> None:
        fake_gh.respond("pr", "view", "--jq", ".state", stdout="MERGED\n")

        result = _invoke("linked-issues", "10", "--mark-in-review", project=pr_project)

        assert result.exit_code == 1
        assert "PR #10 is not open (MERGED): no issue updated." in result.stderr

    def test_mark_in_review_on_a_jira_project_moves_nothing_and_succeeds(
        self, pr_project: Path, fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SOVA_TASK_TYPE", "jira")

        result = _invoke("linked-issues", "10", "--mark-in-review", project=pr_project)

        assert result.exit_code == 0
        assert "no Jira ticket moved" in result.stdout


# ---------------------------------------------------------------------------
# pr-verdict
# ---------------------------------------------------------------------------


def _review_data(fake_gh: FakeGh, *, pr: int, head: str, history: list[dict[str, object]]) -> None:
    fake_gh.respond_json("pr", "view", "--json", "headRefOid", payload={"headRefOid": head})
    fake_gh.respond_json(
        "api",
        "graphql",
        payload={
            "data": {
                "repository": {
                    f"pr{pr}": {
                        "headRefOid": head,
                        "reviewThreads": {"totalCount": 0, "pageInfo": {"hasNextPage": False}, "nodes": []},
                        "reviews": {"pageInfo": {"hasPreviousPage": False}, "nodes": []},
                        "history": {"pageInfo": {"hasPreviousPage": False}, "nodes": history},
                    }
                }
            }
        },
    )


def _marker_review(verdict: str, head: str) -> dict[str, object]:
    return {
        "state": "COMMENTED",
        "body": f"<!-- sova-review: {verdict} sha={head} -->",
        "submittedAt": "2026-11-20T10:00:00Z",
        "author": {"login": "sova-bot"},
    }


class TestPrVerdict:
    @pytest.fixture(autouse=True)
    def _trusted_login(self, pr_project: Path, monkeypatch: pytest.MonkeyPatch, fake_gh: FakeGh) -> None:
        monkeypatch.setenv("SOVA_GITHUB_USER", "sova-bot")
        fake_gh.respond("auth", "token", stdout="token\n")

    @pytest.mark.parametrize("verdict", ["approve", "revise", "block"])
    def test_prints_the_github_marker_verdict_for_the_current_head(
        self, pr_project: Path, fake_gh: FakeGh, verdict: str
    ) -> None:
        _review_data(fake_gh, pr=7, head="abc1234", history=[_marker_review(verdict, "abc1234")])

        result = _invoke("pr-verdict", "7", project=pr_project)

        assert result.exit_code == 0
        assert result.stdout == f"{verdict}\n"

    def test_a_marker_for_an_older_head_does_not_count(self, pr_project: Path, fake_gh: FakeGh) -> None:
        _review_data(fake_gh, pr=7, head="abc1234", history=[_marker_review("block", "0000000")])

        result = _invoke("pr-verdict", "7", project=pr_project)

        assert result.stdout == "none\n"

    def test_a_local_reviewer_handoff_covers_a_verdict_whose_post_failed(
        self, pr_project: Path, fake_gh: FakeGh
    ) -> None:
        _review_data(fake_gh, pr=7, head="abc1234", history=[])
        write_handoff_file(
            pr_project,
            DashboardHandoff(
                source="reviewer",
                status="awaiting_action",
                pr_number=7,
                summary="review",
                details={"verdict": "block", "review_head_sha": "abc1234"},
            ),
        )

        result = _invoke("pr-verdict", "7", project=pr_project)

        assert result.stdout == "block\n"

    def test_an_unreadable_head_prints_unknown_on_stdout_and_the_reason_on_stderr(
        self, pr_project: Path, fake_gh: FakeGh
    ) -> None:
        fake_gh.respond("pr", "view", "headRefOid", stderr="HTTP 502", returncode=1)

        result = _invoke("pr-verdict", "7", project=pr_project)

        assert result.exit_code == 0
        assert result.stdout == "unknown\n"
        assert "Could not read the head commit of PR #7." in result.stderr

    def test_no_configured_repository_prints_unknown(self, pr_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOVA_GITHUB_REPO", "")

        result = _invoke("pr-verdict", "7", project=pr_project)

        assert result.exit_code == 0
        assert result.stdout == "unknown\n"
        assert "No github_repo configured" in result.stderr
