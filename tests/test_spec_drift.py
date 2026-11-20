"""Tests for the spec code-drift check (#1420): base-commit stamp, path extraction, git comparison."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sova.config.models import ProjectConfig
from sova.core.context import ExecutionContext
from sova.core.spec_utils import (
    SpecDrift,
    SpecDriftResult,
    check_spec_code_drift,
    compute_base_commit,
    extract_spec_paths,
    read_spec,
    spec_drift_caveat,
)
from sova.core.steps.develop import _read_approved_spec, _spec_drift_caveat
from sova.core.steps.spec import _stamp_issue_revision

_UNKNOWN_SHA = "a" * 40


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, files: dict[str, str | None]) -> str:
    """Write (or delete, for None) *files*, commit, point origin/main at the commit, return its SHA."""
    for name, content in files.items():
        path = repo / name
        if content is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "change")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", sha)
    return sha


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "main")
    _commit(tmp_path, {"sova/core/a.py": "a\n", "docs/x.md": "x\n", "README.md": "r\n"})
    return tmp_path


def _spec(sha: str | None, body: str) -> str:
    stamp = f"**Base commit**: {sha}\n" if sha is not None else ""
    return f"# Spec: T\n\n**Status**: draft\n{stamp}\n## Solution\n{body}\n"


class TestExtractSpecPaths:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("`sova/core/a.py`", {"sova/core/a.py"}),
            ("`sova/core/a.py:42` and `sova/core/b.py:10-20`", {"sova/core/a.py", "sova/core/b.py"}),
            ("`sova/core/a.py::Klass.method`", {"sova/core/a.py"}),
            ("`./sova/core/a.py` and `README.md`", {"sova/core/a.py", "README.md"}),
            # Prose words are kept too: the intersection with the changed files discards them.
            ("`edit sova/core/a.py now`", {"edit", "sova/core/a.py", "now"}),
            ("`sova/core/` and `sova/utils`", {"sova/core/", "sova/utils"}),
            ("`1.2.3` `https://example.com/a.py` `/etc/passwd` `42` `...`", set()),
            ("`Makefile` and `Dockerfile`", {"Makefile", "Dockerfile"}),
            ("`foo()`", {"foo"}),
            ("`sova/core/a.py:12:5`", {"sova/core/a.py"}),
            ("`sova/core/a.py:12-14:3`", {"sova/core/a.py"}),
            (
                "`sova/core/a.py,` `sova/core/b.py.` `sova/core/c.py;`",
                {"sova/core/a.py", "sova/core/b.py", "sova/core/c.py"},
            ),
            ("`sova/core/a.py:12,` `sova/core/b.py:3.`", {"sova/core/a.py", "sova/core/b.py"}),
            ("`sova/core/a.py()`", {"sova/core/a.py"}),
            ("`(see sova/core/a.py)`", {"see", "sova/core/a.py"}),
            ("`(sova/core/a.py`", {"sova/core/a.py"}),
            ('`"sova/core/a.py"`', {"sova/core/a.py"}),
            ("`[sova/core/a.py]` `'sova/core/b.py'`", {"sova/core/a.py", "sova/core/b.py"}),
            ("`(sova/core/a.py:12)`", {"sova/core/a.py"}),
            ("```\n`sova/core/fenced.py`\n```\n", set()),
            ("sova/core/unticked.py", set()),
        ],
    )
    def test_extraction(self, text: str, expected: set[str]) -> None:
        assert extract_spec_paths(text) == expected


class TestCheckSpecDrift:
    def test_current_when_base_has_not_moved(self, repo: Path) -> None:
        sha = _git(repo, "rev-parse", "HEAD")
        result = check_spec_code_drift(_spec(sha, "Edit `sova/core/a.py`."), repo, "main")
        assert result == SpecDriftResult(SpecDrift.CURRENT)

    def test_current_when_only_unnamed_files_changed(self, repo: Path) -> None:
        sha = _git(repo, "rev-parse", "HEAD")
        _commit(repo, {"other.py": "o\n"})
        assert check_spec_code_drift(_spec(sha, "Edit `sova/core/a.py`."), repo, "main").status is SpecDrift.CURRENT

    def test_drifted_lists_only_touched_named_files(self, repo: Path) -> None:
        sha = _git(repo, "rev-parse", "HEAD")
        _commit(repo, {"sova/core/a.py": "a2\n", "other.py": "o\n"})
        result = check_spec_code_drift(_spec(sha, "Edit `sova/core/a.py:3` and `docs/x.md`."), repo, "main")
        assert result == SpecDriftResult(SpecDrift.DRIFTED, ("sova/core/a.py",))

    def test_ref_behind_the_stamp_is_not_drift(self, repo: Path) -> None:
        old = _git(repo, "rev-parse", "HEAD")
        sha = _commit(repo, {"sova/core/a.py": "a2\n"})
        _git(repo, "update-ref", "refs/remotes/origin/main", old)
        assert check_spec_code_drift(_spec(sha, "`sova/core/a.py`"), repo, "main").status is SpecDrift.CURRENT

    def test_deleted_named_file_counts_as_drift(self, repo: Path) -> None:
        sha = _git(repo, "rev-parse", "HEAD")
        _commit(repo, {"docs/x.md": None})
        result = check_spec_code_drift(_spec(sha, "See `docs/x.md`."), repo, "main")
        assert result.files == ("docs/x.md",)

    def test_renamed_named_file_counts_as_drift_for_its_old_path(self, repo: Path) -> None:
        sha = _git(repo, "rev-parse", "HEAD")
        _git(repo, "mv", "sova/core/a.py", "sova/core/b.py")
        _commit(repo, {})
        result = check_spec_code_drift(_spec(sha, "Edit `sova/core/a.py`."), repo, "main")
        assert result == SpecDriftResult(SpecDrift.DRIFTED, ("sova/core/a.py",))

    def test_rename_detection_config_does_not_hide_the_old_path(self, repo: Path) -> None:
        sha = _git(repo, "rev-parse", "HEAD")
        _git(repo, "mv", "sova/core/a.py", "sova/core/b.py")
        _commit(repo, {})
        _git(repo, "config", "diff.renames", "copies")
        result = check_spec_code_drift(_spec(sha, "Edit `sova/core/a.py`."), repo, "main")
        assert result.files == ("sova/core/a.py",)

    def test_root_extensionless_file_counts_as_drift(self, repo: Path) -> None:
        _commit(repo, {"Makefile": "all:\n"})
        sha = _git(repo, "rev-parse", "HEAD")
        _commit(repo, {"Makefile": "all:\n\t@true\n"})
        result = check_spec_code_drift(_spec(sha, "Edit `Makefile`."), repo, "main")
        assert result == SpecDriftResult(SpecDrift.DRIFTED, ("Makefile",))

    @pytest.mark.parametrize(
        ("named", "drifted"),
        [("`sova/core/`", True), ("`sova/core`", False), ("`sova/`", True), ("`sova/utils/`", False)],
    )
    def test_directory_prefix_needs_trailing_slash(self, repo: Path, named: str, drifted: bool) -> None:
        sha = _git(repo, "rev-parse", "HEAD")
        _commit(repo, {"sova/core/a.py": "a2\n"})
        status = check_spec_code_drift(_spec(sha, named), repo, "main").status
        assert status is (SpecDrift.DRIFTED if drifted else SpecDrift.CURRENT)

    @pytest.mark.parametrize(
        "stamp",
        [None, "", "not-a-sha", "abc123", "--output=/tmp/x", _UNKNOWN_SHA],
        ids=["absent", "empty", "garbage", "short", "option-like", "unknown-commit"],
    )
    def test_unverifiable_stamps(self, repo: Path, stamp: str | None) -> None:
        _commit(repo, {"sova/core/a.py": "a2\n"})
        assert check_spec_code_drift(_spec(stamp, "`sova/core/a.py`"), repo, "main").status is SpecDrift.UNVERIFIABLE

    def test_stamp_quoted_in_body_is_ignored(self, repo: Path) -> None:
        sha = _git(repo, "rev-parse", "HEAD")
        text = f"# Spec: T\n\n**Status**: draft\n\n## Solution\n**Base commit**: {sha}\n`sova/core/a.py`\n"
        assert check_spec_code_drift(text, repo, "main").status is SpecDrift.UNVERIFIABLE

    def test_inline_marker_before_the_real_stamp_line_is_ignored(self, repo: Path) -> None:
        sha = _git(repo, "rev-parse", "HEAD")
        _commit(repo, {"sova/core/a.py": "a2\n"})
        text = (
            "# Spec: T\n\n**Status**: draft\n"
            f"Note: write **Base commit**: {_UNKNOWN_SHA} on its own line.\n"
            f"**Base commit**: {sha}\n\n## Solution\n`sova/core/a.py`\n"
        )
        assert check_spec_code_drift(text, repo, "main") == SpecDriftResult(SpecDrift.DRIFTED, ("sova/core/a.py",))

    def test_paths_are_relative_to_a_project_in_a_repo_subdirectory(self, repo: Path) -> None:
        sha = _git(repo, "rev-parse", "HEAD")
        _commit(repo, {"sova/core/a.py": "a2\n", "other/y.py": "y\n"})
        project = repo / "sova"
        # Named relative to the project (core/a.py), as a spec in a monorepo subproject would name it.
        result = check_spec_code_drift(_spec(sha, "`core/a.py` and `other/y.py`"), project, "main")
        assert result == SpecDriftResult(SpecDrift.DRIFTED, ("core/a.py",))

    def test_unverifiable_without_remote_ref(self, repo: Path) -> None:
        sha = _git(repo, "rev-parse", "HEAD")
        assert check_spec_code_drift(_spec(sha, "`sova/core/a.py`"), repo, "develop").status is SpecDrift.UNVERIFIABLE

    def test_unverifiable_when_git_cannot_run(self, tmp_path: Path) -> None:
        text = _spec(_UNKNOWN_SHA, "`sova/core/a.py`")
        assert check_spec_code_drift(text, tmp_path / "missing", "main").status is SpecDrift.UNVERIFIABLE

    def test_never_raises(self, repo: Path) -> None:
        sha = _git(repo, "rev-parse", "HEAD")
        with patch("sova.core.spec_utils.extract_spec_paths", side_effect=RuntimeError("boom")):
            assert check_spec_code_drift(_spec(sha, "`a.py`"), repo, "main").status is SpecDrift.UNVERIFIABLE

    def test_large_diff_is_handled_by_set_intersection(self, repo: Path) -> None:
        sha = _git(repo, "rev-parse", "HEAD")
        _commit(repo, {f"bulk/f{i}.txt": "x\n" for i in range(2000)})
        assert check_spec_code_drift(_spec(sha, "`bulk/f1999.txt`"), repo, "main").files == ("bulk/f1999.txt",)


class TestComputeBaseCommit:
    def test_merge_base_ignores_local_commits(self, repo: Path) -> None:
        base = _git(repo, "rev-parse", "HEAD")
        _git(repo, "checkout", "-b", "work")
        (repo / "local.txt").write_text("l\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "local")
        assert compute_base_commit(repo, "main") == base

    def test_none_without_remote_ref(self, repo: Path) -> None:
        assert compute_base_commit(repo, "develop") is None


class TestSpecDriftCaveat:
    def test_empty_unless_drifted(self) -> None:
        assert spec_drift_caveat(SpecDriftResult(SpecDrift.CURRENT)) == ""
        assert spec_drift_caveat(SpecDriftResult(SpecDrift.UNVERIFIABLE)) == ""

    def test_lists_files_and_caps_the_list(self) -> None:
        files = tuple(f"f{i:02d}.py" for i in range(13))
        caveat = spec_drift_caveat(SpecDriftResult(SpecDrift.DRIFTED, files))
        assert "f09.py" in caveat
        assert "f10.py" not in caveat
        assert "(and 3 more)" in caveat


class TestStamp:
    def test_base_commit_follows_revision_line_in_frontmatter(self) -> None:
        content = "# Spec: T\n\n**Status**: draft\n**Complexity**: simple\n\n## Solution\nx\n"
        stamped = _stamp_issue_revision(content, "T", "B", _UNKNOWN_SHA)
        lines = stamped.splitlines()
        revision = next(i for i, line in enumerate(lines) if line.startswith("**Issue revision**"))
        assert lines[revision + 1] == f"**Base commit**: {_UNKNOWN_SHA}"
        assert revision < lines.index("## Solution")

    def test_omitted_without_base_commit(self) -> None:
        assert "Base commit" not in _stamp_issue_revision("# Spec: T\n\n## Solution\nx\n", "T", "B", None)

    def test_stamp_round_trips_through_the_checker(self, repo: Path) -> None:
        sha = compute_base_commit(repo, "main")
        stamped = _stamp_issue_revision(_spec(None, "`sova/core/a.py`"), "T", "B", sha)
        assert check_spec_code_drift(stamped, repo, "main").status is SpecDrift.CURRENT
        _commit(repo, {"sova/core/a.py": "a2\n"})
        assert check_spec_code_drift(stamped, repo, "main").status is SpecDrift.DRIFTED


class TestStampEchoedLines:
    _ECHOED = (
        "# Spec: T\n\n**Status**: draft\n**Complexity**: simple\n"
        f"**Issue revision**: oldoldoldold\n**Base commit**: {_UNKNOWN_SHA}\n\n## Solution\nx\n"
    )

    def test_echoed_stamp_is_removed_when_there_is_no_new_base_commit(self) -> None:
        stamped = _stamp_issue_revision(self._ECHOED, "T", "B", None)
        assert "Base commit" not in stamped
        assert "oldoldoldold" not in stamped
        assert stamped.count("**Issue revision**") == 1

    def test_echoed_stamp_is_replaced_by_the_new_base_commit(self) -> None:
        new_sha = "b" * 40
        stamped = _stamp_issue_revision(self._ECHOED, "T", "B", new_sha)
        assert stamped.count("**Base commit**") == 1
        assert f"**Base commit**: {new_sha}" in stamped
        assert _UNKNOWN_SHA not in stamped

    def test_quoted_marker_in_a_fenced_example_is_left_alone(self) -> None:
        content = "# Spec: T\n\n**Complexity**: simple\n\n## Solution\n```\n**Base commit**: x\n```\n"
        assert "**Base commit**: x" in _stamp_issue_revision(content, "T", "B", None)


class TestSpecStepStampsFromWorkingDir:
    def test_stamp_is_the_merge_base_of_the_checkout_the_researcher_reads(self, repo: Path, tmp_path: Path) -> None:
        old = _git(repo, "rev-parse", "HEAD")
        newer = _commit(repo, {"sova/core/a.py": "a2\n"})
        worktree = tmp_path / "wt"
        _git(repo, "worktree", "add", "--detach", str(worktree), newer)
        # The project dir is on an older ancestor of origin/main; the worktree is at origin/main.
        _git(repo, "checkout", "--detach", old)
        assert compute_base_commit(repo, "main") == old
        assert compute_base_commit(worktree, "main") == newer

    def test_a_checkout_behind_origin_logs_a_warning(self, repo: Path) -> None:
        old = _git(repo, "rev-parse", "HEAD")
        _commit(repo, {"sova/core/a.py": "a2\n"})
        _git(repo, "checkout", "--detach", old)
        with patch("sova.core.spec_utils.log") as log:
            assert compute_base_commit(repo, "main") == old
        assert log.warning.call_args.args[0] == "spec.base_commit_behind_origin"

    def test_a_synced_checkout_logs_nothing(self, repo: Path) -> None:
        with patch("sova.core.spec_utils.log") as log:
            assert compute_base_commit(repo, "main") == _git(repo, "rev-parse", "HEAD")
        log.warning.assert_not_called()

    async def test_spec_step_uses_the_working_dir(self, repo: Path, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock

        from sova.core.steps.spec import SpecStep
        from sova.llm.models import LLMResult

        project = tmp_path / "project"
        worktree = tmp_path / "wt"
        (project / ".claude" / "agent-control").mkdir(parents=True)
        worktree.mkdir()
        adapter = AsyncMock()
        adapter.get_task.return_value = MagicMock(title="Test issue", body="body\n\n**Complexity**: simple")
        ctx = ExecutionContext(
            project_dir=project,
            config=ProjectConfig(),
            adapter=adapter,
            issue_number="42",
            role="researcher",
            worktree_dir=worktree,
        )
        assert ctx.working_dir == worktree
        spec_text = "# Spec: T\n\n**Status**: draft\n**Complexity**: simple\n\n## Solution\n\nx\n"
        llm_result = LLMResult(text=spec_text, model="sonnet", cost_usd=0, input_tokens=1, output_tokens=1)
        with (
            patch("sova.core.steps.spec.invoke", new_callable=AsyncMock, return_value=llm_result),
            patch("sova.core.steps.spec.compute_base_commit", return_value=None) as spy,
        ):
            await SpecStep().execute(ctx)
        assert spy.call_args.args[0] == worktree
        assert spy.call_args.args[0] != project


class TestReadApprovedSpecFailure:
    def test_non_oserror_failure_is_swallowed_and_logged_at_warning(self, repo: Path) -> None:
        ctx = ExecutionContext(
            project_dir=repo, config=ProjectConfig(), adapter=MagicMock(), issue_number="42", role="developer"
        )
        with (
            patch("sova.core.spec_utils.read_spec", side_effect=KeyError("boom")),
            patch("sova.core.steps.develop.log") as log,
        ):
            assert _read_approved_spec(ctx) is None
        log.warning.assert_called_once()
        assert log.warning.call_args.args[0] == "step.develop.spec_load_failed"

    async def test_develop_still_dispatches_when_the_spec_read_raises(self, repo: Path) -> None:
        from unittest.mock import AsyncMock

        from sova.core.steps.develop import _build_develop_args

        ctx = ExecutionContext(
            project_dir=repo, config=ProjectConfig(), adapter=MagicMock(), issue_number="42", role="developer"
        )
        with (
            patch("sova.core.spec_utils.read_spec", side_effect=KeyError("boom")),
            patch("sova.core.steps.develop._issue_context_for_develop", new=AsyncMock(return_value="")),
        ):
            assert await _build_develop_args(ctx) == ("42", "")


class TestDevelopDriftCaveat:
    def _ctx(self, repo: Path) -> ExecutionContext:
        return ExecutionContext(
            project_dir=repo,
            config=ProjectConfig(),
            adapter=MagicMock(),
            issue_number="42",
            role="developer",
        )

    def _write_spec(self, repo: Path, sha: str, status: str = "approved") -> None:
        specs = repo / ".claude" / "specs"
        specs.mkdir(parents=True, exist_ok=True)
        text = _spec(sha, "Edit `sova/core/a.py`.").replace("draft", status)
        (specs / "42-t.md").write_text(text, encoding="utf-8")

    async def test_drift_returns_caveat_and_emits_one_warning_event_per_call(self, repo: Path) -> None:
        sha = _git(repo, "rev-parse", "HEAD")
        self._write_spec(repo, sha)
        _commit(repo, {"sova/core/a.py": "a2\n"})
        with patch("sova.dashboard.services.feed_service.emit_safe") as emit:
            ctx = self._ctx(repo)
            first = await _spec_drift_caveat(ctx, _read_approved_spec(ctx))
            second = await _spec_drift_caveat(ctx, _read_approved_spec(ctx))
        assert "sova/core/a.py" in first
        assert first == second
        assert emit.call_count == 2
        assert emit.call_args.kwargs["metadata"]["drifted_files"] == ["sova/core/a.py"]

    async def test_no_drift_is_silent(self, repo: Path) -> None:
        sha = _git(repo, "rev-parse", "HEAD")
        self._write_spec(repo, sha)
        with patch("sova.dashboard.services.feed_service.emit_safe") as emit:
            ctx = self._ctx(repo)
            assert await _spec_drift_caveat(ctx, _read_approved_spec(ctx)) == ""
        emit.assert_not_called()

    async def test_unapproved_spec_is_not_checked(self, repo: Path) -> None:
        sha = _git(repo, "rev-parse", "HEAD")
        self._write_spec(repo, sha, "draft")
        _commit(repo, {"sova/core/a.py": "a2\n"})
        with patch("sova.dashboard.services.feed_service.emit_safe") as emit:
            ctx = self._ctx(repo)
            assert await _spec_drift_caveat(ctx, _read_approved_spec(ctx)) == ""
        emit.assert_not_called()

    async def test_unexpected_failure_is_swallowed_so_develop_still_runs(self, repo: Path) -> None:
        ctx = self._ctx(repo)
        spec = {"status": "approved", "raw_content": _spec(_UNKNOWN_SHA, "`a.py`")}
        with patch("sova.core.spec_utils.check_spec_code_drift", side_effect=RuntimeError("boom")):
            assert await _spec_drift_caveat(ctx, spec) == ""

    async def test_spec_is_read_once_for_context_and_drift(self, repo: Path) -> None:
        from unittest.mock import AsyncMock

        from sova.core.steps.develop import _build_develop_args

        sha = _git(repo, "rev-parse", "HEAD")
        self._write_spec(repo, sha)
        _commit(repo, {"sova/core/a.py": "a2\n"})
        ctx = self._ctx(repo)
        ctx.task = MagicMock()
        with (
            patch("sova.core.spec_utils.read_spec", wraps=read_spec) as read,
            patch("sova.dashboard.services.feed_service.emit_safe"),
            patch("sova.core.steps.develop._issue_context_for_develop", new=AsyncMock()),
        ):
            args, _ = await _build_develop_args(ctx)
        assert read.call_count == 1
        assert "may be out of date" in args


class TestGitNonzeroLogging:
    def test_nonzero_exit_is_logged_and_unverifiable(self, repo: Path) -> None:
        with patch("sova.core.spec_utils.log") as log:
            result = check_spec_code_drift(_spec(_UNKNOWN_SHA, "`a.py`"), repo, "main")
        assert result.status is SpecDrift.UNVERIFIABLE
        events = [call.args[0] for call in log.info.call_args_list]
        assert "spec.drift_git_nonzero" in events


class TestSpecDriftService:
    async def test_reports_drift_for_the_project_checkout(self, repo: Path) -> None:
        from sova.dashboard.services.spec_service import spec_drift

        sha = _git(repo, "rev-parse", "HEAD")
        _commit(repo, {"sova/core/a.py": "a2\n"})
        result = await spec_drift(_spec(sha, "`sova/core/a.py`"), project_dir=repo)
        assert result == {"status": "drifted", "files": ["sova/core/a.py"]}

    @pytest.mark.parametrize("status", ["rejected", "implemented"])
    async def test_spec_that_cannot_guide_work_is_not_checked(self, repo: Path, status: str) -> None:
        from sova.dashboard.services.spec_service import spec_drift

        sha = _git(repo, "rev-parse", "HEAD")
        _commit(repo, {"sova/core/a.py": "a2\n"})
        result = await spec_drift(_spec(sha, "`sova/core/a.py`"), status, repo)
        assert result == {"status": "unverifiable", "files": []}

    async def test_spec_whose_developer_run_opened_a_pr_is_not_checked(self, repo: Path) -> None:
        """No Implementation Notes (optional), but the issue's own merged PR changed the named file."""
        from sova.dashboard.services.spec_service import spec_drift
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        sha = _git(repo, "rev-parse", "HEAD")
        _commit(repo, {"sova/core/a.py": "a2\n"})
        text = _spec(sha, "`sova/core/a.py`").replace("draft", "approved")
        assert await spec_drift(text, "approved", repo, "42") == {"status": "drifted", "files": ["sova/core/a.py"]}
        async with await get_session(project_dir=repo) as session:
            session.add(TaskRun(issue_number="42", role="developer", status="done", pr_number=7))
            await session.commit()
        assert await spec_drift(text, "approved", repo, "42") == {"status": "unverifiable", "files": []}
        # Another issue's run does not hide this spec's drift.
        assert (await spec_drift(text, "approved", repo, "43"))["status"] == "drifted"

    async def test_pr_run_older_than_the_spec_does_not_hide_drift(self, repo: Path) -> None:
        """A PR closed unmerged before a re-research consumed an older spec: the new spec still warns."""
        import os
        from datetime import datetime, timedelta, timezone

        from sova.dashboard.services.spec_service import spec_drift
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        sha = _git(repo, "rev-parse", "HEAD")
        _commit(repo, {"sova/core/a.py": "a2\n"})
        text = _spec(sha, "`sova/core/a.py`").replace("draft", "approved")
        spec_dir = repo / ".claude" / "specs"
        spec_dir.mkdir(parents=True)
        spec_file = spec_dir / "42-spec.md"
        spec_file.write_text(text, encoding="utf-8")
        now = datetime.now(timezone.utc)
        async with await get_session(project_dir=repo) as session:
            session.add(
                TaskRun(
                    issue_number="42", role="developer", status="done", pr_number=7, started_at=now - timedelta(hours=2)
                )
            )
            await session.commit()
        os.utime(spec_file, (now.timestamp(), now.timestamp()))
        with patch("sova.dashboard.services.spec_service.find_spec_file", return_value=spec_file):
            assert (await spec_drift(text, "approved", repo, "42"))["status"] == "drifted"
            # A PR run started after the spec was written did consume it.
            os.utime(spec_file, (now.timestamp() - 7200 * 2, now.timestamp() - 7200 * 2))
            assert (await spec_drift(text, "approved", repo, "42"))["status"] == "unverifiable"

    async def test_spec_with_an_active_developer_run_is_not_checked(self, repo: Path) -> None:
        from sova.dashboard.services.spec_service import spec_drift
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        sha = _git(repo, "rev-parse", "HEAD")
        _commit(repo, {"sova/core/a.py": "a2\n"})
        async with await get_session(project_dir=repo) as session:
            session.add(TaskRun(issue_number="42", role="developer", status="running"))
            session.add(TaskRun(issue_number="43", role="developer", status="failed"))
            await session.commit()
        text = _spec(sha, "`sova/core/a.py`")
        assert (await spec_drift(text, "approved", repo, "42"))["status"] == "unverifiable"
        # A failed run with no PR has not consumed the spec: the warning stays.
        assert (await spec_drift(text, "approved", repo, "43"))["status"] == "drifted"

    async def test_run_lookup_failure_is_unverifiable_not_checked(self, repo: Path) -> None:
        from sqlalchemy.exc import OperationalError

        from sova.dashboard.services.spec_service import spec_drift

        sha = _git(repo, "rev-parse", "HEAD")
        _commit(repo, {"sova/core/a.py": "a2\n"})
        with patch(
            "sova.dashboard.services.spec_service.get_session", side_effect=OperationalError("s", {}, Exception("db"))
        ):
            result = await spec_drift(_spec(sha, "`sova/core/a.py`"), "approved", repo, "42")
        assert result == {"status": "unverifiable", "files": []}

    async def test_implemented_spec_is_not_checked(self, repo: Path) -> None:
        from sova.dashboard.services.spec_service import spec_drift

        sha = _git(repo, "rev-parse", "HEAD")
        _commit(repo, {"sova/core/a.py": "a2\n"})
        text = _spec(sha, "`sova/core/a.py`") + "\n## Implementation Notes\nDone.\n"
        assert await spec_drift(text, "approved", repo) == {"status": "unverifiable", "files": []}

    async def test_config_failure_is_unverifiable_not_current(self, repo: Path) -> None:
        from sova.dashboard.services.spec_service import spec_drift

        with patch("sova.dashboard.services.spec_service.load_config", side_effect=ValueError("bad config")):
            result = await spec_drift(_spec(_UNKNOWN_SHA, "`a.py`"), project_dir=repo)
        assert result == {"status": "unverifiable", "files": []}


class TestDevelopPromptWiring:
    async def test_drift_caveat_leads_the_spec_context_passed_to_develop(self, tmp_path: Path) -> None:
        from decimal import Decimal
        from unittest.mock import AsyncMock

        from sova.core.steps.develop import DevelopStep

        ctx = ExecutionContext(
            project_dir=tmp_path, config=ProjectConfig(), adapter=MagicMock(), issue_number="42", role="developer"
        )
        ctx.task = MagicMock()
        dispatched = MagicMock(cost_usd=Decimal("1.00"), total_tokens=10, session_id="s")
        step = DevelopStep()
        with (
            patch("sova.core.steps.develop._load_spec_for_develop", return_value=("SPEC BODY", "")),
            patch("sova.core.steps.develop._spec_drift_caveat", new=AsyncMock(return_value="DRIFT CAVEAT")),
            patch("sova.core.steps.develop.dispatch_command", new=AsyncMock(return_value=dispatched)) as dispatch,
            patch("sova.core.steps.develop._append_implementation_notes", new=AsyncMock()),
            patch.object(DevelopStep, "_run_inner_check_loop", new=AsyncMock(return_value=(True, ""))),
        ):
            await step.execute(ctx)
        args = dispatch.call_args.kwargs["args"]
        assert args.index("DRIFT CAVEAT") < args.index("SPEC BODY")


class TestBuildDevelopArgs:
    def _ctx(self, tmp_path: Path) -> ExecutionContext:
        return ExecutionContext(
            project_dir=tmp_path, config=ProjectConfig(), adapter=MagicMock(), issue_number="42", role="developer"
        )

    async def test_build_develop_args_prefixes_spec_with_drift_caveat(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock

        from sova.core.steps.develop import _build_develop_args

        with (
            patch("sova.core.steps.develop._load_spec_for_develop", return_value=("SPEC BODY", "note")),
            patch("sova.core.steps.develop._spec_drift_caveat", new=AsyncMock(return_value="DRIFT CAVEAT")),
            patch("sova.core.steps.develop._issue_context_for_develop", new=AsyncMock()) as issue_ctx,
        ):
            args, note = await _build_develop_args(self._ctx(tmp_path))
        assert args.startswith("42\n\n## Spec Context")
        assert args.endswith("DRIFT CAVEAT\n\nSPEC BODY")
        assert note == "note"
        issue_ctx.assert_not_awaited()

    async def test_build_develop_args_spec_without_drift_has_no_caveat(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock

        from sova.core.steps.develop import _build_develop_args

        with (
            patch("sova.core.steps.develop._load_spec_for_develop", return_value=("SPEC BODY", "")),
            patch("sova.core.steps.develop._spec_drift_caveat", new=AsyncMock(return_value="")),
        ):
            args, note = await _build_develop_args(self._ctx(tmp_path))
        assert args.endswith("do NOT re-fetch the issue body)\n\nSPEC BODY")
        assert note == ""

    async def test_build_develop_args_falls_back_to_issue_context(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock

        from sova.core.steps.develop import _build_develop_args

        with (
            patch("sova.core.steps.develop._load_spec_for_develop", return_value=("", "")),
            patch("sova.core.steps.develop._spec_drift_caveat", new=AsyncMock()) as drift,
            patch("sova.core.steps.develop._issue_context_for_develop", new=AsyncMock(return_value="ISSUE BODY")),
        ):
            args, note = await _build_develop_args(self._ctx(tmp_path))
        assert args.startswith("42\n\n## Issue Context")
        assert args.endswith("ISSUE BODY")
        assert note == ""
        drift.assert_not_awaited()

    async def test_build_develop_args_bare_issue_number_without_context(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock

        from sova.core.steps.develop import _build_develop_args

        with (
            patch("sova.core.steps.develop._load_spec_for_develop", return_value=("", "")),
            patch("sova.core.steps.develop._issue_context_for_develop", new=AsyncMock(return_value="")),
        ):
            args, note = await _build_develop_args(self._ctx(tmp_path))
        assert (args, note) == ("42", "")


class TestSpecRouterDrift:
    @pytest.fixture
    def client_factory(self):
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient

        from sova.dashboard.routers.spec import router as spec_router

        app = FastAPI()
        app.include_router(spec_router, prefix="/api")
        return lambda: AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def test_get_spec_includes_drift(self, client_factory) -> None:
        spec = {"issue_number": "42", "status": "approved", "raw_content": "`a.py`"}
        drift = {"status": "drifted", "files": ["a.py"]}
        with (
            patch("sova.dashboard.services.spec_service.read_spec", return_value=spec),
            patch("sova.dashboard.services.spec_service.spec_drift", return_value=drift),
        ):
            async with client_factory() as client:
                resp = await client.get("/api/spec/42")
        assert resp.json()["drift"] == drift

    async def test_get_spec_of_a_rejected_spec_has_no_drifted_payload(self, client_factory, repo: Path) -> None:
        sha = _git(repo, "rev-parse", "HEAD")
        _commit(repo, {"sova/core/a.py": "a2\n"})
        spec = {
            "issue_number": "42",
            "status": "rejected",
            "raw_content": _spec(sha, "`sova/core/a.py`").replace("draft", "rejected"),
        }
        with (
            patch("sova.dashboard.services.spec_service.read_spec", return_value=spec),
            patch("sova.dashboard.services.spec_service.get_project_dir", return_value=repo),
        ):
            async with client_factory() as client:
                resp = await client.get("/api/spec/42")
        assert resp.json()["drift"] == {"status": "unverifiable", "files": []}

    async def test_revise_is_refused_while_another_agent_is_active_for_the_issue(self, client_factory) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services import control_service, handoff_service

        conflict = {"error": "Issue #42 already has an active agent (run 9)", "existing_run_id": 9}
        with (
            patch.object(control_service, "complete_awaiting_approval_by_issue", new=AsyncMock(return_value=None)),
            patch.object(control_service, "start_agent", new=AsyncMock(return_value=conflict)),
            patch.object(handoff_service, "clear_handoff") as clear,
        ):
            async with client_factory() as client:
                resp = await client.post("/api/spec/42/revise")
        assert resp.status_code == 409
        assert "already has an active agent" in resp.json()["detail"]
        clear.assert_not_called()

    @pytest.mark.parametrize("status", ["draft", "approved"])
    async def test_revise_starts_researcher_for_draft_and_approved_specs(self, client_factory, status: str) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services import control_service, handoff_service

        spec = {"issue_number": "42", "status": status, "raw_content": "x"}
        with (
            patch("sova.dashboard.services.spec_service.read_spec", return_value=spec),
            patch.object(control_service, "complete_awaiting_approval_by_issue", new=AsyncMock(return_value=None)),
            patch.object(control_service, "start_agent", new=AsyncMock(return_value={"pid": 1})) as start,
            patch.object(handoff_service, "clear_handoff"),
        ):
            async with client_factory() as client:
                resp = await client.post("/api/spec/42/revise")
        assert resp.status_code == 200
        assert resp.json()["status"] == "revision_started"
        start.assert_awaited_once_with("42", role="researcher")


class TestSpecPageDriftWiring:
    """spec.html: the warning and the Re-research button are keyed on a drifted status."""

    @pytest.fixture
    def html(self) -> str:
        path = Path(__file__).resolve().parent.parent / "sova" / "dashboard" / "templates" / "spec.html"
        return path.read_text(encoding="utf-8")

    def test_drift_section_hidden_by_default_and_rendered_from_the_api_payload(self, html: str) -> None:
        assert 'id="drift-section"' in html
        assert 'id="drift-section" class="' in html and " hidden" in html.split('id="drift-section"', 1)[1][:200]
        assert "renderDrift(data.drift)" in html

    def test_render_drift_shows_only_when_status_is_drifted(self, html: str) -> None:
        body = html.split("function renderDrift(drift)", 1)[1].split("function renderSpec", 1)[0]
        assert "drift.status === 'drifted'" in body
        assert "classList.add('hidden')" in body

    def test_re_research_button_calls_revise_without_feedback_input(self, html: str) -> None:
        section = html.split('id="drift-section"', 1)[1].split("<!-- Open Questions", 1)[0]
        assert 'onclick="reviseSpec()"' in section and "Re-research" in section
        revise = html.split("async function reviseSpec()", 1)[1].split("async function skipSpec", 1)[0]
        assert "/revise" in revise and "feedback" not in revise
