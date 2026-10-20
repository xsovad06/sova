"""Tests for sova.core.check_cache: pipeline-scoped reuse of a full check run."""

from __future__ import annotations

import json
from pathlib import Path

from sova.commands.catalog import get_canonical_dir
from sova.core import check_cache
from sova.core.check_cache import (
    PIPELINE_STAGE_ENV_KEY,
    clear_marker,
    is_pipeline_dispatch,
    record_result,
    should_skip,
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


class TestClearMarker:
    async def test_removes_existing_marker(self, tmp_path: Path) -> None:
        await _init_repo(tmp_path)
        await record_result(tmp_path, CHECK_CMD, passed=True)
        marker_path = tmp_path / ".claude" / "agent-control" / "check-state.json"
        assert marker_path.is_file()

        clear_marker(tmp_path)

        assert not marker_path.exists()

    def test_noop_when_no_marker(self, tmp_path: Path) -> None:
        clear_marker(tmp_path)  # must not raise


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

        clear_marker(tmp_path)  # must not raise


class TestCanonicalCommandCallSites:
    """The reuse contract lives in markdown, so the markdown is what has to hold it."""

    CALL_SITES = ("develop", "simplify", "review", "pr")
    GUARDED_BLOCK = (
        'if sova checks should-skip "{{ check_cmd }}"; then',
        '{{ check_cmd }} && { sova checks record "{{ check_cmd }}" --passed || true; }',
    )

    @staticmethod
    def _body(name: str) -> str:
        return (get_canonical_dir() / f"{name}.md").read_text(encoding="utf-8")

    def test_every_full_check_call_site_is_guarded(self) -> None:
        for name in self.CALL_SITES:
            body = self._body(name)
            for fragment in self.GUARDED_BLOCK:
                assert fragment in body, f"{name}.md must run its full check through the reuse guard"

    def test_no_call_site_records_a_pass_outside_the_chain(self) -> None:
        """A fix loop usually re-runs only the failing tests, so a record issued
        outside the `<check> && record` chain would key a full-check pass to a
        worktree state the full check never ran against."""
        for name in self.CALL_SITES:
            for line in self._body(name).splitlines():
                if "sova checks record" not in line:
                    continue
                assert line.strip().startswith("{{ check_cmd }} &&") or "Never run `sova checks record" in line, (
                    f"{name}.md records a check pass outside the full-check chain: {line.strip()}"
                )
