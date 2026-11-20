"""Tests for sova.core.check_cache: pipeline-scoped reuse of a full check run."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from sova.commands.catalog import get_canonical_dir
from sova.core import check_cache
from sova.core.check_cache import (
    PIPELINE_STAGE_ENV_KEY,
    clear_marker,
    has_recorded_pass,
    is_pipeline_dispatch,
    record_result,
    seed_outputs,
    should_skip,
    snapshot_untracked,
)
from sova.utils.shell import ShellResult, run

CHECK_CMD = "make check"


async def _init_repo(tmp_path: Path) -> None:
    for argv in (
        ("git", "init"),
        ("git", "config", "user.email", "test@example.com"),
        ("git", "config", "user.name", "Test User"),
        ("git", "config", "commit.gpgsign", "false"),
    ):
        assert (await run(*argv, cwd=tmp_path)).success, argv
    (tmp_path / "file.txt").write_text("initial")
    assert (await run("git", "add", "file.txt", cwd=tmp_path)).success
    assert (await run("git", "commit", "-m", "initial", cwd=tmp_path)).success


async def _set_origin_main(tmp_path: Path) -> None:
    """Give the fixture repo a base-branch ref so _diff_base() resolves a merge base."""
    await run("git", "update-ref", "refs/remotes/origin/main", "HEAD", cwd=tmp_path)


class TestIsPipelineDispatch:
    def test_false_when_unset(self, monkeypatch) -> None:
        monkeypatch.delenv(PIPELINE_STAGE_ENV_KEY, raising=False)
        assert is_pipeline_dispatch() is False

    def test_false_for_other_value(self, monkeypatch) -> None:
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "true")
        assert is_pipeline_dispatch() is False

    def test_true_when_set_to_one(self, monkeypatch) -> None:
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        assert is_pipeline_dispatch() is True


class TestShouldSkip:
    async def test_false_outside_pipeline_even_with_matching_marker(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        await record_result(tmp_path, CHECK_CMD, passed=True)
        monkeypatch.delenv(PIPELINE_STAGE_ENV_KEY, raising=False)

        assert await should_skip(tmp_path, CHECK_CMD) is False

    async def test_false_with_no_marker(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")

        assert await should_skip(tmp_path, CHECK_CMD) is False

    async def test_true_when_marker_matches_unchanged_state(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        await record_result(tmp_path, CHECK_CMD, passed=True)

        assert await should_skip(tmp_path, CHECK_CMD) is True

    async def test_false_when_marker_recorded_failed(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        await record_result(tmp_path, CHECK_CMD, passed=False)

        assert await should_skip(tmp_path, CHECK_CMD) is False

    async def test_false_after_unstaged_edit(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        await record_result(tmp_path, CHECK_CMD, passed=True)

        (tmp_path / "file.txt").write_text("modified")

        assert await should_skip(tmp_path, CHECK_CMD) is False

    async def test_false_after_new_commit(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        await record_result(tmp_path, CHECK_CMD, passed=True)

        (tmp_path / "file.txt").write_text("modified")
        await run("git", "commit", "-am", "second", cwd=tmp_path)

        assert await should_skip(tmp_path, CHECK_CMD) is False

    async def test_false_after_untracked_file_added(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        await record_result(tmp_path, CHECK_CMD, passed=True)

        (tmp_path / "new.txt").write_text("new")

        assert await should_skip(tmp_path, CHECK_CMD) is False

    async def test_false_after_untracked_file_edited_in_place(self, tmp_path: Path, monkeypatch) -> None:
        """A new file an agent creates stays untracked across develop -> simplify ->
        self_review, so editing it changes nothing about the path listing: the
        fingerprint has to cover untracked content, not just untracked names."""
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        (tmp_path / "new.txt").write_text("first")
        await record_result(tmp_path, CHECK_CMD, passed=True)

        (tmp_path / "new.txt").write_text("second")

        assert await should_skip(tmp_path, CHECK_CMD) is False

    async def test_true_when_only_the_marker_itself_was_written(self, tmp_path: Path, monkeypatch) -> None:
        """Recording the marker creates an untracked file inside the worktree, which
        must not invalidate the hash it was just recorded under (this fixture repo has
        no .gitignore, so nothing else excludes it)."""
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        await record_result(tmp_path, CHECK_CMD, passed=True)

        assert await should_skip(tmp_path, CHECK_CMD) is True

    async def test_false_for_different_check_cmd(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        await record_result(tmp_path, CHECK_CMD, passed=True)

        assert await should_skip(tmp_path, "make lint") is False

    async def test_true_after_committing_already_checked_changes(self, tmp_path: Path, monkeypatch) -> None:
        """CommitStep runs between review.md's call site and pr.md's, and committing
        work the check already covered leaves the files byte-identical: keying on the
        merge-base diff rather than on HEAD is what keeps that pass reusable."""
        await _init_repo(tmp_path)
        await _set_origin_main(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        (tmp_path / "file.txt").write_text("modified")
        await record_result(tmp_path, CHECK_CMD, passed=True)

        assert (await run("git", "commit", "-am", "second", cwd=tmp_path)).success

        assert await should_skip(tmp_path, CHECK_CMD) is True

    @pytest.mark.parametrize("stage", ["add", "intent_to_add", "commit"])
    async def test_true_after_a_new_file_moves_from_untracked_to_git_bookkeeping(
        self, tmp_path: Path, monkeypatch, stage: str
    ) -> None:
        """A new file is untracked when develop.md records, staged when CommitStep runs
        `git add`, and committed for pr.md. The bytes never change, so neither may the key."""
        await _init_repo(tmp_path)
        await _set_origin_main(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        (tmp_path / "new.txt").write_text("new")
        await record_result(tmp_path, CHECK_CMD, passed=True)

        argv = {
            "add": ("git", "add", "new.txt"),
            "intent_to_add": ("git", "add", "-N", "new.txt"),
            "commit": ("git", "add", "-A"),
        }[stage]
        assert (await run(*argv, cwd=tmp_path)).success
        if stage == "commit":
            assert (await run("git", "commit", "-m", "second", cwd=tmp_path)).success

        assert await should_skip(tmp_path, CHECK_CMD) is True

    async def test_false_after_a_checked_file_is_deleted(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        await _set_origin_main(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        await record_result(tmp_path, CHECK_CMD, passed=True)

        (tmp_path / "file.txt").unlink()

        assert await should_skip(tmp_path, CHECK_CMD) is False

    async def test_false_after_an_edit_below_the_repo_root(self, tmp_path: Path, monkeypatch) -> None:
        """A cwd below the root must still see its changed files' content, not read them as absent."""
        await _init_repo(tmp_path)
        await _set_origin_main(tmp_path)
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "app.py").write_text("x = 1\n")
        assert (await run("git", "add", ".", cwd=tmp_path)).success
        assert (await run("git", "commit", "-m", "sub", cwd=tmp_path)).success
        await _set_origin_main(tmp_path)
        (sub / "app.py").write_text("x = 2\n")
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        await record_result(sub, CHECK_CMD, passed=True)

        (sub / "app.py").write_text("x = 3\n")

        assert await should_skip(sub, CHECK_CMD) is False

    async def test_false_after_a_changed_file_loses_its_executable_bit(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        await _set_origin_main(tmp_path)
        script = tmp_path / "file.txt"
        script.write_text("edited")
        script.chmod(0o755)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        await record_result(tmp_path, CHECK_CMD, passed=True)

        script.chmod(0o644)

        assert await should_skip(tmp_path, CHECK_CMD) is False

    async def test_true_when_origin_main_moves_but_the_merge_base_does_not(self, tmp_path: Path, monkeypatch) -> None:
        """Only the merge base is part of the key, never where origin/main points."""
        await _init_repo(tmp_path)
        await _set_origin_main(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        await record_result(tmp_path, CHECK_CMD, passed=True)

        assert (await run("git", "checkout", "-q", "-b", "other", "HEAD", cwd=tmp_path)).success
        (tmp_path / "other.txt").write_text("other")
        assert (await run("git", "add", "other.txt", cwd=tmp_path)).success
        assert (await run("git", "commit", "-m", "other", cwd=tmp_path)).success
        await run("git", "update-ref", "refs/remotes/origin/main", "HEAD", cwd=tmp_path)
        assert (await run("git", "checkout", "-q", "-", cwd=tmp_path)).success

        assert await should_skip(tmp_path, CHECK_CMD) is True

    async def test_false_after_edit_following_a_commit(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        await _set_origin_main(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        (tmp_path / "file.txt").write_text("modified")
        await record_result(tmp_path, CHECK_CMD, passed=True)

        assert (await run("git", "commit", "-am", "second", cwd=tmp_path)).success
        (tmp_path / "file.txt").write_text("modified again")

        assert await should_skip(tmp_path, CHECK_CMD) is False

    async def test_false_when_git_cannot_report_state(self, tmp_path: Path, monkeypatch) -> None:
        """An unhashable worktree must not read as "the state matches": a hash built
        from a failed git call's empty output can collide with another worktree's."""
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        marker_path = tmp_path / ".claude" / "agent-control" / "check-state.json"
        marker_path.parent.mkdir(parents=True)
        marker_path.write_text(json.dumps({"hash": "deadbeef", "passed": True}), encoding="utf-8")

        assert await should_skip(tmp_path, CHECK_CMD) is False


class TestRecordResult:
    async def test_writes_marker_file(self, tmp_path: Path) -> None:
        await _init_repo(tmp_path)

        await record_result(tmp_path, CHECK_CMD, passed=True)

        marker_path = tmp_path / ".claude" / "agent-control" / "check-state.json"
        assert marker_path.is_file()
        payload = json.loads(marker_path.read_text())
        assert payload["passed"] is True
        assert payload["check_cmd"] == CHECK_CMD
        assert "hash" in payload
        assert "recorded_at" in payload

    async def test_returns_false_and_writes_nothing_outside_a_git_repo(self, tmp_path: Path) -> None:
        """Recording is advisory: an unhashable worktree costs a later call site its
        reuse and nothing else, so no marker is written and nothing is raised."""
        assert await record_result(tmp_path, CHECK_CMD, passed=True) is False
        assert not (tmp_path / ".claude" / "agent-control" / "check-state.json").exists()

    async def test_overwrites_existing_marker(self, tmp_path: Path) -> None:
        await _init_repo(tmp_path)

        await record_result(tmp_path, CHECK_CMD, passed=False)
        await record_result(tmp_path, CHECK_CMD, passed=True)

        marker_path = tmp_path / ".claude" / "agent-control" / "check-state.json"
        payload = json.loads(marker_path.read_text())
        assert payload["passed"] is True

    async def test_a_later_record_for_another_command_keeps_the_earlier_pass(self, tmp_path: Path, monkeypatch) -> None:
        """The inner loop records the full check, then simplify and review record the fast one."""
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")

        await record_result(tmp_path, CHECK_CMD, passed=True)
        await record_result(tmp_path, "make ci-fast", passed=True)

        assert await should_skip(tmp_path, CHECK_CMD) is True
        assert await should_skip(tmp_path, "make ci-fast") is True

    async def test_a_later_failed_record_for_the_same_command_supersedes_the_pass(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")

        await record_result(tmp_path, CHECK_CMD, passed=True)
        await record_result(tmp_path, CHECK_CMD, passed=False)

        assert await should_skip(tmp_path, CHECK_CMD) is False

    async def test_keeps_only_a_bounded_number_of_earlier_records(self, tmp_path: Path) -> None:
        await _init_repo(tmp_path)

        for i in range(check_cache.EARLIER_RECORDS_KEPT + 3):
            await record_result(tmp_path, f"check-{i}", passed=True)

        payload = json.loads((tmp_path / ".claude" / "agent-control" / "check-state.json").read_text())
        kept = check_cache.EARLIER_RECORDS_KEPT
        assert payload["check_cmd"] == f"check-{kept + 2}"
        assert [e["check_cmd"] for e in payload["earlier"]] == [f"check-{i}" for i in range(kept + 1, 1, -1)]

    async def test_a_full_pass_survives_a_burst_of_later_fast_records(self, tmp_path: Path) -> None:
        await _init_repo(tmp_path)
        await record_result(tmp_path, CHECK_CMD, passed=True)

        for i in range(check_cache.EARLIER_RECORDS_KEPT):
            await record_result(tmp_path, f"fast-{i}", passed=True)

        assert await has_recorded_pass(tmp_path, CHECK_CMD) is True


class TestClearMarker:
    async def test_removes_existing_marker(self, tmp_path: Path) -> None:
        await _init_repo(tmp_path)
        await record_result(tmp_path, CHECK_CMD, passed=True)
        marker_path = tmp_path / ".claude" / "agent-control" / "check-state.json"
        assert marker_path.is_file()

        clear_marker(tmp_path)

        assert not marker_path.exists()

    def test_noop_when_no_marker(self, tmp_path: Path) -> None:
        with capture_logs() as logs:
            clear_marker(tmp_path)

        assert [e["event"] for e in logs if e["log_level"] == "warning"] == []
        assert list(tmp_path.iterdir()) == []


class TestHashFailureModes:
    """Every git call feeding the state hash must fail closed (see _compute_hash)."""

    @staticmethod
    def _failing_run(failing_argv: tuple[str, ...], **overrides):
        """Wrap the real shell runner, failing only the call starting with failing_argv."""
        real_run = check_cache.run

        async def fake_run(*args: str, **kwargs):
            if args[: len(failing_argv)] == failing_argv:
                return ShellResult(returncode=1, stdout=overrides.get("stdout", ""), stderr="boom")
            return await real_run(*args, **kwargs)

        return fake_run

    async def test_false_when_untracked_listing_fails(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        await record_result(tmp_path, CHECK_CMD, passed=True)
        monkeypatch.setattr(check_cache, "run", self._failing_run(("git", "ls-files")))

        assert await should_skip(tmp_path, CHECK_CMD) is False

    async def test_false_when_untracked_hashing_fails(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        (tmp_path / "new.txt").write_text("untracked")
        await record_result(tmp_path, CHECK_CMD, passed=True)
        monkeypatch.setattr(check_cache, "run", self._failing_run(("git", "hash-object")))

        assert await should_skip(tmp_path, CHECK_CMD) is False

    async def test_false_when_untracked_hashing_returns_fewer_blobs_than_paths(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A short blob list means git hashed only some paths: a payload built from
        it would describe a worktree state that was never observed."""
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        await record_result(tmp_path, CHECK_CMD, passed=True)

        real_run = check_cache.run

        async def fake_run(*args: str, **kwargs):
            if args[:2] == ("git", "hash-object"):
                return ShellResult(returncode=0, stdout="0000000000000000000000000000000000000000\n", stderr="")
            return await real_run(*args, **kwargs)

        monkeypatch.setattr(check_cache, "run", fake_run)

        assert await should_skip(tmp_path, CHECK_CMD) is False

    async def test_false_when_worktree_diff_fails(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        await record_result(tmp_path, CHECK_CMD, passed=True)
        monkeypatch.setattr(check_cache, "run", self._failing_run(("git", "diff")))

        assert await should_skip(tmp_path, CHECK_CMD) is False

    async def test_true_when_base_resolves_through_origin_head_symref(self, tmp_path: Path, monkeypatch) -> None:
        """origin/HEAD is preferred over the hardcoded origin/main fallback, so a
        project whose base branch is neither main nor master still gets reuse."""
        await _init_repo(tmp_path)
        await run("git", "update-ref", "refs/remotes/origin/trunk", "HEAD", cwd=tmp_path)
        await run("git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk", cwd=tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")

        await record_result(tmp_path, CHECK_CMD, passed=True)

        assert await should_skip(tmp_path, CHECK_CMD) is True


class TestMarkerReadFailureModes:
    async def test_false_when_marker_is_not_valid_json(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        marker_path = tmp_path / ".claude" / "agent-control" / "check-state.json"
        marker_path.parent.mkdir(parents=True)
        marker_path.write_text("{not json", encoding="utf-8")

        assert await should_skip(tmp_path, CHECK_CMD) is False

    async def test_false_when_marker_is_json_but_not_an_object(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        marker_path = tmp_path / ".claude" / "agent-control" / "check-state.json"
        marker_path.parent.mkdir(parents=True)
        marker_path.write_text(json.dumps(["passed"]), encoding="utf-8")

        assert await should_skip(tmp_path, CHECK_CMD) is False


class TestRecordResultWriteFailure:
    async def test_returns_false_when_marker_path_is_unwritable(self, tmp_path: Path) -> None:
        """Recording is advisory: an unwritable marker path costs a later call site
        its reuse, and must not raise into the check command that chained it."""
        await _init_repo(tmp_path)
        (tmp_path / ".claude").write_text("a file where the directory should be", encoding="utf-8")

        assert await record_result(tmp_path, CHECK_CMD, passed=True) is False


class TestClearMarkerFailure:
    def test_swallows_an_unlink_error(self, tmp_path: Path, monkeypatch) -> None:
        """Both call sites are pipeline steps whose own job must not fail because an
        advisory marker could not be deleted."""

        def boom(self, missing_ok: bool = False) -> None:
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "unlink", boom)

        with capture_logs() as logs:
            clear_marker(tmp_path)

        assert [e["event"] for e in logs if e["log_level"] == "warning"] == ["check_cache.clear_failed"]


class TestPathStates:
    """Every changed or untracked path gets an explicit state: a file, a link, a repository, or absent."""

    async def _hit_after(self, tmp_path: Path, monkeypatch, mutate) -> bool:
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        assert await record_result(tmp_path, CHECK_CMD, passed=True) is True
        mutate()
        return await should_skip(tmp_path, CHECK_CMD)

    async def test_a_symlink_to_a_directory_still_hashes_and_is_reused(self, tmp_path: Path, monkeypatch) -> None:
        """hash-object follows a link and fails on a directory, which would disable reuse for the worktree."""
        await _init_repo(tmp_path)
        (tmp_path / "pkg").mkdir()
        (tmp_path / "venv-link").symlink_to("pkg")

        assert await self._hit_after(tmp_path, monkeypatch, lambda: None) is True

    async def test_retargeting_a_symlink_to_identical_bytes_is_a_miss(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        (tmp_path / "a.txt").write_text("same")
        (tmp_path / "b.txt").write_text("same")
        link = tmp_path / "current"
        link.symlink_to("a.txt")

        def retarget() -> None:
            link.unlink()
            link.symlink_to("b.txt")

        assert await self._hit_after(tmp_path, monkeypatch, retarget) is False

    async def test_a_new_commit_in_an_untracked_nested_repository_is_a_miss(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        nested = tmp_path / "vendor"
        nested.mkdir()
        await _init_repo(nested)

        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        assert await record_result(tmp_path, CHECK_CMD, passed=True) is True
        assert await should_skip(tmp_path, CHECK_CMD) is True
        (nested / "file.txt").write_text("bumped")
        assert (await run("git", "commit", "-am", "bump", cwd=nested)).success

        assert await should_skip(tmp_path, CHECK_CMD) is False

    async def test_a_submodule_pointer_bump_is_a_miss(self, tmp_path: Path, monkeypatch) -> None:
        """A staged gitlink lists only the directory in `git diff --name-only`; its commit has to key the hash."""
        await _init_repo(tmp_path)
        await _set_origin_main(tmp_path)
        sub = tmp_path / "sub"
        sub.mkdir()
        await _init_repo(sub)
        assert (await run("git", "add", "sub", cwd=tmp_path)).success
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        assert await record_result(tmp_path, CHECK_CMD, passed=True) is True
        assert await should_skip(tmp_path, CHECK_CMD) is True

        (sub / "file.txt").write_text("bumped")
        assert (await run("git", "commit", "-am", "bump", cwd=sub)).success
        assert (await run("git", "add", "sub", cwd=tmp_path)).success

        assert await should_skip(tmp_path, CHECK_CMD) is False

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs fifo support")
    async def test_false_for_an_unreadable_path_type(self, tmp_path: Path, monkeypatch) -> None:
        """A tracked file swapped for a fifo cannot be hashed, and must not read like an absent path."""
        await _init_repo(tmp_path)
        (tmp_path / "file.txt").unlink()
        os.mkfifo(tmp_path / "file.txt")
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")

        assert await record_result(tmp_path, CHECK_CMD, passed=True) is False


class TestCheckOutputsAreNotContent:
    """A check's own untracked outputs must not make a later hash differ from the recorded one."""

    async def test_a_rewritten_output_still_hits(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        assert await should_skip(tmp_path, CHECK_CMD) is False  # miss: notes the pre-check untracked paths
        (tmp_path / "coverage.xml").write_text("<coverage ts='1'/>")  # the check runs and writes its output
        await record_result(tmp_path, CHECK_CMD, passed=True)

        (tmp_path / "coverage.xml").write_text("<coverage ts='2'/>")  # a later check rewrites it

        assert await should_skip(tmp_path, CHECK_CMD) is True

    async def test_snapshot_before_the_run_gives_the_same_result(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        before = await snapshot_untracked(tmp_path)
        (tmp_path / "build.stamp").write_text("1")
        await record_result(tmp_path, CHECK_CMD, passed=True, pre_untracked=before)

        (tmp_path / "build.stamp").write_text("2")

        assert await should_skip(tmp_path, CHECK_CMD) is True

    async def test_a_file_untracked_before_the_check_is_still_content(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        (tmp_path / "module.py").write_text("v1")
        assert await should_skip(tmp_path, CHECK_CMD) is False
        (tmp_path / "coverage.xml").write_text("x")
        await record_result(tmp_path, CHECK_CMD, passed=True)

        (tmp_path / "module.py").write_text("v2")

        assert await should_skip(tmp_path, CHECK_CMD) is False

    async def test_a_source_file_the_check_rewrites_is_still_content(self, tmp_path: Path, monkeypatch) -> None:
        """A formatter that rewrites an untracked file the agent created must not take it out of the key."""
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        (tmp_path / "new.py").write_text("x=1")
        assert await should_skip(tmp_path, CHECK_CMD) is False
        (tmp_path / "new.py").write_text("x = 1\n")  # `ruff format` / `prettier --write`
        await record_result(tmp_path, CHECK_CMD, passed=True)

        (tmp_path / "new.py").write_text("x = 2\n")

        assert await should_skip(tmp_path, CHECK_CMD) is False

    async def test_a_file_created_after_the_record_is_a_miss(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        assert await should_skip(tmp_path, CHECK_CMD) is False
        (tmp_path / "coverage.xml").write_text("x")
        await record_result(tmp_path, CHECK_CMD, passed=True)

        (tmp_path / "new_module.py").write_text("print()")

        assert await should_skip(tmp_path, CHECK_CMD) is False

    async def test_without_a_pre_check_note_nothing_is_excluded(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        (tmp_path / "coverage.xml").write_text("1")
        await record_result(tmp_path, CHECK_CMD, passed=True)

        (tmp_path / "coverage.xml").write_text("2")

        assert await should_skip(tmp_path, CHECK_CMD) is False

    async def test_the_pending_note_is_single_use(self, tmp_path: Path, monkeypatch) -> None:
        """A record without its own should-skip must not inherit an old note and exclude an agent's new file."""
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        await should_skip(tmp_path, CHECK_CMD)
        await record_result(tmp_path, CHECK_CMD, passed=True)
        (tmp_path / "agent_file.py").write_text("v1")
        await record_result(tmp_path, CHECK_CMD, passed=True)

        (tmp_path / "agent_file.py").write_text("v2")

        assert await should_skip(tmp_path, CHECK_CMD) is False

    async def test_a_note_for_another_command_is_ignored(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        await should_skip(tmp_path, "make other")
        (tmp_path / "coverage.xml").write_text("1")
        await record_result(tmp_path, CHECK_CMD, passed=True)

        (tmp_path / "coverage.xml").write_text("2")

        assert await should_skip(tmp_path, CHECK_CMD) is False

    async def test_clear_marker_removes_the_pending_note(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        await should_skip(tmp_path, CHECK_CMD)
        pending = tmp_path / ".claude" / "agent-control" / check_cache.PENDING_FILENAME
        assert pending.exists()

        clear_marker(tmp_path)

        assert not pending.exists()


class TestPendingNoteBinding:
    """The pending note describes one run: it is dropped once the tracked state moved on."""

    async def test_a_note_from_a_failed_run_is_not_used_after_a_code_fix(self, tmp_path: Path, monkeypatch) -> None:
        """should-skip misses, the check fails (no record), the agent fixes code and re-runs the chain alone."""
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        assert await should_skip(tmp_path, CHECK_CMD) is False
        (tmp_path / "file.txt").write_text("fixed")  # the code fix to a tracked file
        (tmp_path / "agent_new.py").write_text("v1")  # and a new source file
        await record_result(tmp_path, CHECK_CMD, passed=True)

        (tmp_path / "agent_new.py").write_text("v2")

        assert await should_skip(tmp_path, CHECK_CMD) is False, "the stale note must not exclude agent_new.py"

    async def test_should_skip_drops_an_older_note(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        assert await should_skip(tmp_path, CHECK_CMD) is False
        pending = tmp_path / ".claude" / "agent-control" / check_cache.PENDING_FILENAME
        assert pending.exists()
        await record_result(tmp_path, CHECK_CMD, passed=True, use_pending=False)  # leaves the note in place
        assert pending.exists()

        assert await should_skip(tmp_path, CHECK_CMD) is True

        assert not pending.exists()

    async def test_a_note_still_applies_while_the_tracked_state_is_unchanged(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        assert await should_skip(tmp_path, CHECK_CMD) is False
        (tmp_path / "coverage.xml").write_text("1")
        await record_result(tmp_path, CHECK_CMD, passed=True)
        (tmp_path / "coverage.xml").write_text("2")

        assert await should_skip(tmp_path, CHECK_CMD) is True


class TestRepositoryWideKey:
    """The key covers the whole repository, whichever directory the check is recorded from."""

    async def test_an_edit_outside_the_cwd_subtree_is_a_miss(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        await _set_origin_main(tmp_path)
        sub = tmp_path / "sub"
        sub.mkdir()
        (tmp_path / "pyproject.toml").write_text("v1\n")
        (sub / "app.py").write_text("x = 1\n")
        assert (await run("git", "add", ".", cwd=tmp_path)).success
        assert (await run("git", "commit", "-m", "files", cwd=tmp_path)).success
        await _set_origin_main(tmp_path)
        (tmp_path / "pyproject.toml").write_text("v2\n")
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        assert await record_result(sub, CHECK_CMD, passed=True) is True
        assert await should_skip(sub, CHECK_CMD) is True

        (tmp_path / "pyproject.toml").write_text("v3\n")

        assert await should_skip(sub, CHECK_CMD) is False

    async def test_a_new_untracked_file_outside_the_cwd_subtree_is_a_miss(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        sub = tmp_path / "sub"
        sub.mkdir()
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        assert await record_result(sub, CHECK_CMD, passed=True, use_pending=False) is True

        (tmp_path / "shared.py").write_text("x")

        assert await should_skip(sub, CHECK_CMD) is False

    async def test_a_marker_written_from_a_subdirectory_does_not_change_the_root_key(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        await _init_repo(tmp_path)
        sub = tmp_path / "sub"
        sub.mkdir()
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        assert await record_result(tmp_path, CHECK_CMD, passed=True, use_pending=False) is True

        assert await record_result(sub, "make other", passed=True, use_pending=False) is True  # writes sub/.claude/...

        assert (sub / ".claude" / "agent-control" / check_cache.MARKER_FILENAME).exists()
        assert await has_recorded_pass(tmp_path, CHECK_CMD) is True

    async def test_the_marker_written_below_the_root_is_not_content(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        sub = tmp_path / "sub"
        sub.mkdir()
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        assert await record_result(sub, CHECK_CMD, passed=True, use_pending=False) is True

        assert await should_skip(sub, CHECK_CMD) is True


class TestDirtyNestedRepository:
    async def test_an_uncommitted_edit_in_a_nested_repository_is_a_miss(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        nested = tmp_path / "vendor"
        nested.mkdir()
        await _init_repo(nested)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        assert await record_result(tmp_path, CHECK_CMD, passed=True, use_pending=False) is True
        assert await should_skip(tmp_path, CHECK_CMD) is True

        (nested / "file.txt").write_text("edited, not committed")

        assert await should_skip(tmp_path, CHECK_CMD) is False

    async def test_a_new_file_in_a_nested_repository_is_a_miss(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        nested = tmp_path / "vendor"
        nested.mkdir()
        await _init_repo(nested)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        assert await record_result(tmp_path, CHECK_CMD, passed=True, use_pending=False) is True

        (nested / "new.py").write_text("x")

        assert await should_skip(tmp_path, CHECK_CMD) is False

    async def test_a_dirty_nested_repository_is_not_recorded(self, tmp_path: Path) -> None:
        await _init_repo(tmp_path)
        nested = tmp_path / "vendor"
        nested.mkdir()
        await _init_repo(nested)
        (nested / "file.txt").write_text("dirty")

        assert await record_result(tmp_path, CHECK_CMD, passed=True, use_pending=False) is False


class TestOutputsAcrossRecords:
    """The pipeline order: a fast check, the full check, then more fast checks, all rewriting one output."""

    async def test_a_full_record_after_a_fast_record_survives_a_later_rewrite(self, tmp_path: Path) -> None:
        await _init_repo(tmp_path)
        full, fast = CHECK_CMD, "make ci-fast"
        out = tmp_path / "coverage.xml"

        before = await snapshot_untracked(tmp_path)
        out.write_text("<c ts='1'/>")
        await record_result(tmp_path, fast, passed=True, pre_untracked=before, use_pending=False)

        before = await snapshot_untracked(tmp_path)
        out.write_text("<c ts='2'/>")  # the full check rewrites the output the fast check left
        await record_result(tmp_path, full, passed=True, pre_untracked=before, use_pending=False)

        out.write_text("<c ts='3'/>")  # a later fast check rewrites it again

        assert await has_recorded_pass(tmp_path, full) is True

    async def test_an_output_the_full_check_leaves_unchanged_is_still_an_output(self, tmp_path: Path) -> None:
        await _init_repo(tmp_path)
        full, fast = CHECK_CMD, "make ci-fast"
        out = tmp_path / "coverage.xml"

        before = await snapshot_untracked(tmp_path)
        out.write_text("<c ts='1'/>")
        await record_result(tmp_path, fast, passed=True, pre_untracked=before, use_pending=False)
        before = await snapshot_untracked(tmp_path)  # the full check does not touch the file
        await record_result(tmp_path, full, passed=True, pre_untracked=before, use_pending=False)

        out.write_text("<c ts='2'/>")

        assert await has_recorded_pass(tmp_path, full) is True

    async def test_an_untracked_source_file_is_never_carried_as_an_output(self, tmp_path: Path) -> None:
        await _init_repo(tmp_path)
        (tmp_path / "module.py").write_text("v1")
        before = await snapshot_untracked(tmp_path)
        (tmp_path / "coverage.xml").write_text("1")
        await record_result(tmp_path, "make ci-fast", passed=True, pre_untracked=before, use_pending=False)
        before = await snapshot_untracked(tmp_path)
        await record_result(tmp_path, CHECK_CMD, passed=True, pre_untracked=before, use_pending=False)

        (tmp_path / "module.py").write_text("v2")

        assert await has_recorded_pass(tmp_path, CHECK_CMD) is False

    async def test_an_excluded_output_that_is_committed_and_edited_is_a_miss(self, tmp_path: Path) -> None:
        """Once an output is tracked it is content again: a check can read a committed snapshot or generated file."""
        await _init_repo(tmp_path)
        await _set_origin_main(tmp_path)
        before = await snapshot_untracked(tmp_path)
        (tmp_path / "generated.py").write_text("v1")
        await record_result(tmp_path, CHECK_CMD, passed=True, pre_untracked=before, use_pending=False)
        assert (await run("git", "add", "generated.py", cwd=tmp_path)).success
        assert (await run("git", "commit", "-m", "gen", cwd=tmp_path)).success
        assert await has_recorded_pass(tmp_path, CHECK_CMD) is False, "tracked now, so hashed by content"
        await record_result(tmp_path, CHECK_CMD, passed=True, use_pending=False)

        (tmp_path / "generated.py").write_text("v2")

        assert await has_recorded_pass(tmp_path, CHECK_CMD) is False

    async def test_a_use_pending_false_record_never_consumes_the_note(self, tmp_path: Path, monkeypatch) -> None:
        await _init_repo(tmp_path)
        monkeypatch.setenv(PIPELINE_STAGE_ENV_KEY, "1")
        await should_skip(tmp_path, CHECK_CMD)  # the agent's note
        (tmp_path / "agent_file.py").write_text("v1")

        await record_result(tmp_path, CHECK_CMD, passed=True, pre_untracked=None, use_pending=False)

        assert (tmp_path / ".claude" / "agent-control" / check_cache.PENDING_FILENAME).exists()
        (tmp_path / "agent_file.py").write_text("v2")
        assert await has_recorded_pass(tmp_path, CHECK_CMD) is False, "nothing was excluded"


class TestLatestRecordWins:
    async def test_a_failure_supersedes_an_earlier_pass_recorded_with_other_exclusions(self, tmp_path: Path) -> None:
        await _init_repo(tmp_path)
        before = await snapshot_untracked(tmp_path)
        (tmp_path / "coverage.xml").write_text("1")
        await record_result(tmp_path, CHECK_CMD, passed=True, pre_untracked=before, use_pending=False)

        # Same content and command, but no snapshot: a different exclusion set, hence a different hash.
        await record_result(tmp_path, CHECK_CMD, passed=False, use_pending=False)

        assert await has_recorded_pass(tmp_path, CHECK_CMD) is False


class TestSeededOutputs:
    """An output the baseline run left predates every record, yet it is still a check output."""

    async def test_an_output_left_by_the_baseline_run_stays_out_of_every_key(self, tmp_path: Path) -> None:
        await _init_repo(tmp_path)
        out = tmp_path / "coverage.xml"
        before = await snapshot_untracked(tmp_path)
        out.write_text("<c ts='0'/>")  # the baseline test run
        assert await seed_outputs(tmp_path, before) is True

        before = await snapshot_untracked(tmp_path)
        out.write_text("<c ts='1'/>")
        await record_result(tmp_path, "make ci-fast", passed=True, pre_untracked=before, use_pending=False)
        before = await snapshot_untracked(tmp_path)
        out.write_text("<c ts='2'/>")
        await record_result(tmp_path, CHECK_CMD, passed=True, pre_untracked=before, use_pending=False)
        out.write_text("<c ts='3'/>")  # simplify's fast check rewrites it again

        assert await has_recorded_pass(tmp_path, CHECK_CMD) is True

    async def test_without_a_seed_an_output_that_predates_the_first_record_is_content(self, tmp_path: Path) -> None:
        await _init_repo(tmp_path)
        out = tmp_path / "coverage.xml"
        out.write_text("<c ts='0'/>")
        before = await snapshot_untracked(tmp_path)
        out.write_text("<c ts='1'/>")
        await record_result(tmp_path, CHECK_CMD, passed=True, pre_untracked=before, use_pending=False)

        out.write_text("<c ts='2'/>")

        assert await has_recorded_pass(tmp_path, CHECK_CMD) is False

    async def test_a_seed_is_kept_across_records_and_holds_no_record_itself(self, tmp_path: Path) -> None:
        await _init_repo(tmp_path)
        before = await snapshot_untracked(tmp_path)
        (tmp_path / "coverage.xml").write_text("0")
        await seed_outputs(tmp_path, before)

        assert await has_recorded_pass(tmp_path, CHECK_CMD) is False
        await record_result(tmp_path, CHECK_CMD, passed=True, pre_untracked=await snapshot_untracked(tmp_path))

        payload = json.loads((tmp_path / ".claude" / "agent-control" / "check-state.json").read_text())
        assert payload["seeded"] == ["coverage.xml"]
        assert payload["earlier"] == []

    async def test_nothing_is_seeded_without_a_snapshot_or_a_new_path(self, tmp_path: Path) -> None:
        await _init_repo(tmp_path)

        assert await seed_outputs(tmp_path, None) is False
        assert await seed_outputs(tmp_path, await snapshot_untracked(tmp_path)) is False
        assert not (tmp_path / ".claude" / "agent-control" / "check-state.json").exists()


class TestCanonicalCommandCallSites:
    """The reuse contract lives in markdown, so the markdown is what has to hold it."""

    # The three agent-facing commands run the configured fast check; pr.md keeps the full one.
    CALL_SITES = {
        "develop": "fast_check_cmd",
        "simplify": "fast_check_cmd",
        "review": "fast_check_cmd",
        "pr": "check_cmd",
    }
    GUARDED_BLOCK = (
        'if sova checks should-skip "{{ %s }}"; then',
        '{{ %s }} && { sova checks record "{{ %s }}" --passed || true; }',
    )

    @staticmethod
    def _body(name: str) -> str:
        return (get_canonical_dir() / f"{name}.md").read_text(encoding="utf-8")

    def test_every_full_check_call_site_is_guarded(self) -> None:
        for name, var in self.CALL_SITES.items():
            body = self._body(name)
            for fragment in self.GUARDED_BLOCK:
                fragment = fragment.replace("%s", var)
                assert fragment in body, f"{name}.md must run its check through the reuse guard"

    def test_every_fast_check_call_site_says_never_to_run_the_full_check(self) -> None:
        """The prompt names both commands, so the agent can tell whether the two differ."""
        for name, var in self.CALL_SITES.items():
            if var != "fast_check_cmd":
                continue
            body = self._body(name)
            assert (
                "If `{{ fast_check_cmd }}` differs from `{{ check_cmd }}`, run only `{{ fast_check_cmd }}`; "
                "the autonomous pipeline runs `{{ check_cmd }}` once in its own gate."
            ) in body, f"{name}.md must tell the agent never to run the full check"

    def test_fast_check_call_sites_never_run_or_guard_the_full_check(self) -> None:
        """A leftover full-check chain next to the fast one would keep the full suite running per call site."""
        for name, var in self.CALL_SITES.items():
            if var != "fast_check_cmd":
                continue
            body = self._body(name)
            assert 'should-skip "{{ check_cmd }}"' not in body, f"{name}.md guards the full check"
            for line in body.splitlines():
                assert not line.strip().startswith("{{ check_cmd }} &&"), f"{name}.md runs the full check: {line}"

    def test_no_call_site_records_a_pass_outside_the_chain(self) -> None:
        """A fix loop usually re-runs only the failing tests, so a record issued
        outside the `<check> && record` chain would key a full-check pass to a
        worktree state the full check never ran against."""
        for name in self.CALL_SITES:
            for line in self._body(name).splitlines():
                if "sova checks record" not in line:
                    continue
                assert (
                    line.strip().startswith(f"{{{{ {self.CALL_SITES[name]} }}}} &&")
                    or "Never run `sova checks record" in line
                ), f"{name}.md records a check pass outside the full-check chain: {line.strip()}"
