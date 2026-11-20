"""Behavioural tests for `sova run`, `sova watch` and `sova parallel` (`sova.cli.commands.run`), via `CliRunner`.

Each test runs the real command against a real temporary git project and its own SQLite file (`run_project` points
`SOVA_DATABASE_URL` at it: the command's `init_db()` builds a fresh engine, which for the shared in-memory test
database would be a fresh empty database, so rows seeded before the command ran would be gone). Only the
process boundary is faked: a scripted `gh` on PATH (`fake_gh`), which is how the GitHub adapter reads an issue.
The role that runs is `triage` in `dry_run` mode, which reads the issue and writes nothing, so a run completes
in about a second and still goes through the real dispatcher, role, `TaskRun` persistence and exit-code mapping.
Nothing inside `sova/cli/commands/` is patched. The Rich console writes to stderr, so a command's messages are
asserted on `result.stderr`.
"""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from sova.cli.app import app
from sova.db.models import StepExecution, TaskRun
from sova.db.session import get_session, init_db
from tests.process_fakes import FakeGh, send_signal_when

pytestmark = pytest.mark.usefixtures("no_cli_provider_init", "quiet_logs")

runner = CliRunner()


def _issue(number: int, *, state: str = "OPEN", labels: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": "Do the thing.\n\n## Acceptance Criteria\n- it works",
        "state": state,
        "labels": [{"name": name} for name in labels],
        "assignees": [],
        "milestone": None,
        "url": f"https://github.com/owner/repo/issues/{number}",
        "createdAt": "2026-01-01T00:00:00Z",
    }


@pytest.fixture
def run_project(
    git_project: Path, fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch, _dispose_project_engines: None
) -> Path:
    """A git project for `owner/repo` whose default role is a dry-run triage, with issue 42 readable on GitHub."""
    monkeypatch.setenv("SOVA_DATABASE_URL", f"sqlite+aiosqlite:///{git_project.parent / 'run-test.db'}")
    monkeypatch.setenv("SOVA_GITHUB_REPO", "owner/repo")
    monkeypatch.setenv("SOVA_GITHUB_USER", "")
    monkeypatch.setenv("SOVA_TRIAGE_MODE", "dry_run")
    monkeypatch.setenv("SOVA_ROLES_DEFAULT", "triage")
    fake_gh.respond_json("issue", "view", "42", payload=_issue(42))
    return git_project


def _run(*args: str, project: Path) -> Any:
    return runner.invoke(app, ["run", *args, "--project", str(project)])


async def _task_runs(project: Path) -> list[TaskRun]:
    await init_db(project, run_migrations=False)
    async with await get_session(project) as session:
        return list((await session.execute(select(TaskRun).order_by(TaskRun.id))).scalars())


async def _seed_run(
    project: Path, *, issue_number: str, status: str, role: str = "triage", steps: tuple[str, ...] = ()
) -> int:
    await init_db(project)
    async with await get_session(project) as session, session.begin():
        run = TaskRun(issue_number=issue_number, role=role, status=status, current_step="sync")
        session.add(run)
        await session.flush()
        for name in steps:
            session.add(StepExecution(task_run_id=run.id, step_name=name, status="passed"))
        return run.id


class TestRunIssue:
    def test_needs_an_issue_or_a_role(self, run_project: Path) -> None:
        result = _run(project=run_project)

        assert result.exit_code == 2
        assert "Either an issue number or --role is required." in result.stderr

    def test_a_completed_run_is_recorded_as_a_task_run(self, run_project: Path) -> None:
        result = _run("42", "--role", "triage", project=run_project)

        assert result.exit_code == 0, result.stderr
        assert "Starting workflow for #42" in result.stderr
        assert "Workflow completed (triage): Issue #42 assessed as ready" in result.stderr
        (task_run,) = asyncio.run(_task_runs(run_project))
        assert (task_run.issue_number, task_run.role) == ("42", "triage")

    def test_a_role_that_refuses_the_issue_exits_1_with_its_reason(self, run_project: Path, fake_gh: FakeGh) -> None:
        fake_gh.respond_json("issue", "view", "43", payload=_issue(43, state="CLOSED"))

        result = _run("43", "--role", "triage", project=run_project)

        assert result.exit_code == 1
        assert "Workflow failed (triage): Precondition failed" in result.stderr

    def test_an_unreadable_issue_fails_the_run(self, run_project: Path, fake_gh: FakeGh) -> None:
        fake_gh.respond("issue", "view", "44", stderr="HTTP 404", returncode=1)

        result = _run("44", "--role", "triage", project=run_project)

        assert result.exit_code == 1
        assert "Workflow completed" not in result.stderr

    def test_a_human_only_issue_is_refused_before_the_workflow_starts(
        self, run_project: Path, fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The default role is what the tracker state dispatches to, and triage is never refused.
        monkeypatch.delenv("SOVA_ROLES_DEFAULT")
        fake_gh.respond_json("issue", "view", "45", payload=_issue(45, labels=("agent:human-only", "agent:researched")))

        result = _run("45", project=run_project)

        assert result.exit_code == 1
        assert "Pass --override-suitability to run it anyway." in result.stderr
        assert "Starting workflow" not in result.stderr

    def test_override_suitability_starts_the_workflow_on_a_human_only_issue(
        self, run_project: Path, fake_gh: FakeGh
    ) -> None:
        fake_gh.respond_json("issue", "view", "48", payload=_issue(48, labels=("agent:human-only",)))

        result = _run("48", "--role", "triage", "--override-suitability", project=run_project)

        # The gate is passed; triage itself then declines a human-only issue, which is its own refusal.
        assert "Starting workflow for #48" in result.stderr
        assert "Pass --override-suitability" not in result.stderr
        assert "Workflow failed (triage): Precondition failed" in result.stderr

    def test_override_suitability_without_a_readable_issue_exits_1_instead_of_guessing_a_role(
        self, run_project: Path, fake_gh: FakeGh
    ) -> None:
        fake_gh.respond("issue", "view", "49", stderr="HTTP 404", returncode=1)

        result = _run("49", "--override-suitability", project=run_project)

        assert result.exit_code == 1
        assert "Starting workflow" not in result.stderr

    def test_a_project_scope_role_runs_without_an_issue(self, run_project: Path) -> None:
        result = _run("--role", "triage", project=run_project)

        assert "Starting workflow for" in result.stderr
        assert "triage-" in result.stderr
        assert "Workflow completed" not in result.stderr

    def test_sigterm_ends_the_run_with_the_signal_exit_code(self, run_project: Path, fake_gh: FakeGh) -> None:
        # The issue read blocks, so the run is mid-dispatch (its SIGTERM handler installed) when the signal comes.
        fake_gh.respond_json("issue", "view", "46", payload=_issue(46))
        fake_gh.respond("issue", "view", "47", delay=30)
        previous = signal.getsignal(signal.SIGTERM)
        cancel = send_signal_when(
            signal.SIGTERM,
            lambda: fake_gh.called_with("issue", "view", "47") and signal.getsignal(signal.SIGTERM) is not previous,
        )

        result = _run("47", "--role", "triage", project=run_project)
        cancel.set()

        assert result.exit_code == 128 + signal.SIGTERM
        assert "Workflow terminated (SIGTERM)" in result.stderr


class TestRunResume:
    def test_an_unknown_run_cannot_be_resumed(self, run_project: Path) -> None:
        result = _run("42", "--resume", "999", project=run_project)

        assert result.exit_code == 1
        assert "Cannot resume: Run #999 not found" in result.stderr

    def test_a_run_of_another_issue_cannot_be_resumed(self, run_project: Path) -> None:
        run_id = asyncio.run(_seed_run(run_project, issue_number="7", status="failed"))

        result = _run("42", "--resume", str(run_id), project=run_project)

        assert result.exit_code == 1
        assert f"Cannot resume: Run #{run_id} is for issue #7, not #42" in result.stderr

    def test_a_running_run_cannot_be_resumed(self, run_project: Path) -> None:
        run_id = asyncio.run(_seed_run(run_project, issue_number="42", status="running"))

        result = _run("42", "--resume", str(run_id), project=run_project)

        assert result.exit_code == 1
        assert f"Run #{run_id} has status 'running'" in result.stderr

    def test_an_issue_less_run_cannot_be_resumed_with_an_issue(self, run_project: Path) -> None:
        run_id = asyncio.run(_seed_run(run_project, issue_number="", status="failed"))

        result = _run("42", "--resume", str(run_id), project=run_project)

        assert result.exit_code == 1
        assert "is an issue-less run, cannot resume with issue #42" in result.stderr

    def test_a_failed_run_resumes_past_its_completed_steps(self, run_project: Path) -> None:
        run_id = asyncio.run(_seed_run(run_project, issue_number="42", status="failed", steps=("sync", "assess")))

        result = _run("42", "--resume", str(run_id), project=run_project)

        assert result.exit_code == 0, result.stderr
        assert f"Resuming from run #{run_id} (skipping 2 completed steps)" in result.stderr
        assert f"Resuming workflow for #42 from run #{run_id}" in result.stderr


class TestParallel:
    def test_reports_one_result_row_per_issue(self, run_project: Path, fake_gh: FakeGh) -> None:
        fake_gh.respond_json("issue", "view", "41", payload=_issue(41, state="CLOSED"))

        result = runner.invoke(app, ["parallel", "41", "42", "--project", str(run_project)])

        assert result.exit_code == 0, result.stderr
        assert "Processing 2 issues" in result.stderr
        assert "#42: OK: Issue #42 assessed as ready" in result.stderr
        assert "#41: FAIL:" in result.stderr


class TestWatch:
    def test_processes_an_actionable_issue_and_stops_on_interrupt(
        self, run_project: Path, fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SOVA_WATCH_INTERVAL_ACTIVE", "1")
        fake_gh.respond_json("issue", "list", payload=[_issue(42)])
        # A second listing means the first cycle, including the dispatch, has finished.
        cancel = send_signal_when(signal.SIGINT, lambda: fake_gh.call_count("issue", "list") >= 2)

        result = runner.invoke(app, ["watch", "--project", str(run_project)])
        cancel.set()

        assert "Watch mode started" in result.stderr
        assert "Processing #42: Issue 42" in result.stderr
        assert "Done: Issue #42 assessed as ready" in result.stderr

    def test_idles_when_nothing_is_actionable(
        self, run_project: Path, fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SOVA_WATCH_INTERVAL_IDLE", "1")
        fake_gh.respond_json("issue", "list", payload=[])
        cancel = send_signal_when(signal.SIGINT, lambda: fake_gh.call_count("issue", "list") >= 2)

        result = runner.invoke(app, ["watch", "--project", str(run_project)])
        cancel.set()

        assert "No actionable issues." in result.stderr
