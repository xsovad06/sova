"""Tests for post-resolution conflict verification in sova/git/rebase.py.

Covers issue #965: the single-model resolution path trusted the LLM's own
report that it had resolved and staged every conflicted file. Git-level checks
alone cannot catch a file that was staged with conflict markers still inside
it, so a bad resolution could be committed and reported as a successful rebase.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from sova.git.rebase import (
    _get_conflicted_files,
    _has_unresolved_conflict,
    _unresolved_paths,
    rebase_with_conflict_resolution,
)
from sova.utils.shell import run

_MARKED_FILE = "line1\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> feature\nline3\n"


def _shell(success: bool = True, stdout: str = "", stderr: str = "") -> MagicMock:
    return MagicMock(success=success, stdout=stdout, stderr=stderr)


def _git_side_effect(
    conflict_checks: list[str | None],
    *,
    continue_ok: bool = True,
) -> Callable[..., Coroutine[Any, Any, MagicMock]]:
    """Build a run() side effect keyed on the git subcommand.

    ``conflict_checks`` supplies stdout for each successive conflict check;
    ``None`` makes that check fail. The last entry repeats once exhausted.
    """
    state = {"n": 0}

    async def side_effect(*args: str, **kwargs: object) -> MagicMock:
        cmd = " ".join(args)
        if "fetch" in cmd:
            return _shell()
        if "stash" in cmd:
            return _shell(stdout="No local changes to save")
        if "diff" in cmd and "--diff-filter=U" in cmd:
            idx = min(state["n"], len(conflict_checks) - 1)
            state["n"] += 1
            output = conflict_checks[idx]
            if output is None:
                return _shell(success=False, stderr="fatal: unable to read index")
            return _shell(stdout=output)
        if "--continue" in cmd:
            stdout = "" if continue_ok else "You must edit all merge conflicts"
            return _shell(success=continue_ok, stdout=stdout)
        if "--abort" in cmd:
            return _shell()
        if "rebase" in cmd:
            return _shell(success=False, stderr="CONFLICT")
        return _shell()

    return side_effect


class TestHasUnresolvedConflict:
    """On-disk marker detection, deliberately narrower than _has_conflict_markers."""

    def test_detects_start_marker(self) -> None:
        assert _has_unresolved_conflict("code\n<<<<<<< HEAD\nours\n") is True

    def test_detects_end_marker(self) -> None:
        assert _has_unresolved_conflict("code\n>>>>>>> feature\n") is True

    def test_detects_bare_start_marker_without_label(self) -> None:
        assert _has_unresolved_conflict("code\n<<<<<<<\n") is True

    def test_ignores_bare_separator_line(self) -> None:
        """A lone `=======` is a valid markdown setext underline, not a conflict."""
        assert _has_unresolved_conflict("Title\n=======\n\nbody text\n") is False

    def test_ignores_clean_content(self) -> None:
        assert _has_unresolved_conflict("def f():\n    return 1\n") is False

    def test_ignores_empty(self) -> None:
        assert _has_unresolved_conflict("") is False

    def test_ignores_marker_like_text_mid_line(self) -> None:
        assert _has_unresolved_conflict("print('<<<<<<< not a marker')\n") is False


class TestGetConflictedFiles:
    """The check must distinguish 'check failed' from 'no conflicts remain'."""

    async def test_returns_conflicted_paths(self) -> None:
        with patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell(stdout="a.py\nb.py\n")
            assert await _get_conflicted_files(cwd=Path("/repo")) == ["a.py", "b.py"]

    async def test_returns_empty_list_when_no_conflicts(self) -> None:
        with patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell(stdout="")
            assert await _get_conflicted_files(cwd=Path("/repo")) == []

    async def test_returns_none_when_check_fails(self) -> None:
        """A failed check is unknown state, never 'resolved'."""
        with patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell(success=False, stderr="not a git repository")
            assert await _get_conflicted_files(cwd=Path("/repo")) is None


class TestUnresolvedPaths:
    """Shared verification helper used by both resolution paths."""

    async def test_empty_when_staged_and_marker_free(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("clean\n")
        with patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell(stdout="")
            assert await _unresolved_paths(["a.py"], cwd=tmp_path) == []

    async def test_reports_paths_git_still_calls_unmerged(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("clean\n")
        with patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell(stdout="a.py\n")
            assert await _unresolved_paths(["a.py"], cwd=tmp_path) == ["a.py"]

    async def test_reports_markers_left_in_staged_file(self, tmp_path: Path) -> None:
        """Staging a file with markers makes git report clean: content must be checked."""
        (tmp_path / "a.py").write_text(_MARKED_FILE)
        with patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell(stdout="")
            assert await _unresolved_paths(["a.py"], cwd=tmp_path) == ["a.py"]

    async def test_does_not_duplicate_a_path_flagged_by_both_checks(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text(_MARKED_FILE)
        with patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell(stdout="a.py\n")
            assert await _unresolved_paths(["a.py"], cwd=tmp_path) == ["a.py"]

    async def test_returns_none_when_git_check_fails(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("clean\n")
        with patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell(success=False, stderr="index lock")
            assert await _unresolved_paths(["a.py"], cwd=tmp_path) is None

    async def test_missing_file_is_not_flagged(self, tmp_path: Path) -> None:
        """Deleting a file is a legitimate conflict resolution."""
        with patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell(stdout="")
            assert await _unresolved_paths(["gone.py"], cwd=tmp_path) == []

    async def test_binary_file_is_not_flagged(self, tmp_path: Path) -> None:
        """Binary conflicts cannot be marker-scanned: the git check governs."""
        (tmp_path / "img.bin").write_bytes(b"\x00\xff\xfe\x01binary")
        with patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell(stdout="")
            assert await _unresolved_paths(["img.bin"], cwd=tmp_path) == []

    async def test_reports_unmerged_path_outside_the_requested_set(self, tmp_path: Path) -> None:
        """A conflict git reports is unresolved even if the caller did not list it."""
        (tmp_path / "a.py").write_text("clean\n")
        with patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _shell(stdout="other.py\n")
            assert await _unresolved_paths(["a.py"], cwd=tmp_path) == ["other.py"]


class TestAgainstRealGit:
    """Exercises the real git behaviour the mocked tests are modelled on."""

    @staticmethod
    async def _build_conflicting_repo(tmp_path: Path) -> Path:
        """Create a repo whose feature branch conflicts with origin/main."""
        origin = tmp_path / "origin.git"
        repo = tmp_path / "repo"
        no_hooks = tmp_path / "no-hooks"
        origin.mkdir()
        repo.mkdir()
        no_hooks.mkdir()
        await run("git", "init", "-q", "--bare", "-b", "main", cwd=origin)
        await run("git", "init", "-q", "-b", "main", cwd=repo)
        await run("git", "config", "user.email", "test@example.com", cwd=repo)
        await run("git", "config", "user.name", "Test User", cwd=repo)
        # A global commit.gpgsign or core.hooksPath would otherwise reach in and
        # block the fixture's commits and `rebase --continue`.
        await run("git", "config", "commit.gpgsign", "false", cwd=repo)
        await run("git", "config", "core.hooksPath", str(no_hooks), cwd=repo)
        await run("git", "remote", "add", "origin", str(origin), cwd=repo)

        (repo / "f.txt").write_text("line1\nline2\nline3\n")
        await run("git", "add", "f.txt", cwd=repo)
        await run("git", "commit", "-q", "-m", "base", cwd=repo)
        await run("git", "push", "-q", "origin", "main", cwd=repo)

        await run("git", "checkout", "-q", "-b", "feature", cwd=repo)
        (repo / "f.txt").write_text("line1\nFEATURE\nline3\n")
        await run("git", "commit", "-q", "-am", "feature", cwd=repo)

        await run("git", "checkout", "-q", "main", cwd=repo)
        (repo / "f.txt").write_text("line1\nMAIN\nline3\n")
        await run("git", "commit", "-q", "-am", "main", cwd=repo)
        await run("git", "push", "-q", "origin", "main", cwd=repo)
        await run("git", "checkout", "-q", "feature", cwd=repo)
        return repo

    async def test_staging_markers_does_not_produce_a_successful_rebase(self, tmp_path: Path) -> None:
        """Git accepts a staged file with markers and would commit them: we must not.

        `git add` on a conflicted file clears its unmerged index entry whether or
        not the markers were removed, so every git-level check reports clean and
        `rebase --continue` succeeds. Only the content check catches this.
        """
        repo = await self._build_conflicting_repo(tmp_path)

        async def stage_without_resolving(*_args: object, **kwargs: object) -> MagicMock:
            await run("git", "add", "f.txt", cwd=repo)
            return MagicMock(text="staged", cost_usd=Decimal("0.01"))

        with (
            patch("sova.git.rebase._load_consensus_config", return_value=([], 0.66, {}, None)),
            patch("sova.git.rebase.invoke_command", side_effect=stage_without_resolving),
        ):
            result, _cost = await rebase_with_conflict_resolution("main", cwd=repo, max_attempts=1)

        assert result.success is False
        assert "f.txt" in result.error

        head = await run("git", "show", "HEAD:f.txt", cwd=repo)
        assert not _has_unresolved_conflict(head.stdout), "conflict markers must never be committed"
        assert not (repo / ".git" / "rebase-merge").exists(), "rebase must be aborted on failure"

    async def test_real_resolution_completes_the_rebase(self, tmp_path: Path) -> None:
        """The same path succeeds when the conflict is genuinely resolved."""
        repo = await self._build_conflicting_repo(tmp_path)

        async def resolve_properly(*_args: object, **kwargs: object) -> MagicMock:
            (repo / "f.txt").write_text("line1\nMERGED\nline3\n")
            await run("git", "add", "f.txt", cwd=repo)
            return MagicMock(text="resolved", cost_usd=Decimal("0.01"))

        with (
            patch("sova.git.rebase._load_consensus_config", return_value=([], 0.66, {}, None)),
            patch("sova.git.rebase.invoke_command", side_effect=resolve_properly),
        ):
            result, _cost = await rebase_with_conflict_resolution("main", cwd=repo, max_attempts=1)

        assert result.success is True
        head = await run("git", "show", "HEAD:f.txt", cwd=repo)
        assert "MERGED" in head.stdout
        assert not _has_unresolved_conflict(head.stdout)


class TestRebaseVerificationIntegration:
    """End-to-end behaviour of rebase_with_conflict_resolution() with verification."""

    async def test_markers_left_in_staged_file_do_not_count_as_resolved(self, tmp_path: Path) -> None:
        """The corruption case: git reports clean, but markers would be committed."""
        (tmp_path / "file.py").write_text(_MARKED_FILE)

        with (
            patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.rebase._load_consensus_config", return_value=([], 0.66, {}, None)),
            patch("sova.git.rebase.invoke_command", new_callable=AsyncMock) as mock_llm,
        ):
            mock_run.side_effect = _git_side_effect(["file.py\n", ""])
            mock_llm.return_value = MagicMock(text="done", cost_usd=Decimal("0.01"))

            result, _cost = await rebase_with_conflict_resolution("main", cwd=tmp_path, max_attempts=2)

        assert result.success is False
        assert "file.py" in result.error
        assert mock_llm.await_count == 2

    async def test_failed_conflict_check_is_not_treated_as_resolved(self, tmp_path: Path) -> None:
        """An unverifiable state aborts: a resolution pass costs real money and cannot fix it."""
        (tmp_path / "file.py").write_text("clean\n")

        with (
            patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.rebase._load_consensus_config", return_value=([], 0.66, {}, None)),
            patch("sova.git.rebase.invoke_command", new_callable=AsyncMock) as mock_llm,
        ):
            mock_run.side_effect = _git_side_effect(["file.py\n", None])
            mock_llm.return_value = MagicMock(text="done", cost_usd=Decimal("0.01"))

            result, _cost = await rebase_with_conflict_resolution("main", cwd=tmp_path, max_attempts=3)

        assert result.success is False
        assert "verify" in result.error
        assert mock_llm.await_count == 1, "a failed check must not pay for more resolution passes"
        aborts = [c for c in mock_run.call_args_list if "--abort" in c[0]]
        assert len(aborts) == 1, "the worktree must be left clean, and aborted exactly once"

    async def test_continue_failure_after_resolution_is_logged(self, tmp_path: Path) -> None:
        """The post-resolution --continue was silent, hiding the real failure point."""
        (tmp_path / "file.py").write_text("clean\n")

        with (
            patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.rebase._load_consensus_config", return_value=([], 0.66, {}, None)),
            patch("sova.git.rebase.invoke_command", new_callable=AsyncMock) as mock_llm,
            patch("sova.git.rebase.log") as mock_log,
        ):
            mock_run.side_effect = _git_side_effect(["file.py\n", ""], continue_ok=False)
            mock_llm.return_value = MagicMock(text="done", cost_usd=Decimal("0.01"))

            result, _cost = await rebase_with_conflict_resolution("main", cwd=tmp_path)

        assert result.success is False
        first_commit_warnings = [
            call
            for call in mock_log.warning.call_args_list
            if call[0][0] == "git.rebase.continue_failed" and call[1].get("commit") == 1
        ]
        assert first_commit_warnings, "the --continue that follows resolution must be logged"
        assert "unresolved" in first_commit_warnings[0][1], "the log must name what is still unresolved"

    async def test_check_failing_after_continue_aborts_rather_than_guessing(self, tmp_path: Path) -> None:
        """A failed check after `--continue` must not collapse to "nothing conflicted".

        Reading it as an empty list sends the next iteration into a blind
        `--continue` and reports the generic error instead of naming the cause.
        """
        (tmp_path / "file.py").write_text("clean\n")

        with (
            patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.rebase._load_consensus_config", return_value=([], 0.66, {}, None)),
            patch("sova.git.rebase.invoke_command", new_callable=AsyncMock) as mock_llm,
        ):
            # conflicted, resolved, then the post-continue check fails
            mock_run.side_effect = _git_side_effect(["file.py\n", "", None], continue_ok=False)
            mock_llm.return_value = MagicMock(text="done", cost_usd=Decimal("0.01"))

            result, _cost = await rebase_with_conflict_resolution("main", cwd=tmp_path)

        assert result.success is False
        assert "verify" in result.error
        continues = [c for c in mock_run.call_args_list if "--continue" in c[0]]
        assert len(continues) == 1, "must not blindly retry --continue on an unknown state"

    async def test_consensus_aborts_when_verification_check_fails(self, tmp_path: Path) -> None:
        """Falling through to the paid single-model path cannot fix a broken check."""
        (tmp_path / "file.py").write_text("clean\n")

        with (
            patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.rebase._load_consensus_config", return_value=(["m1", "m2"], 0.66, {}, None)),
            patch("sova.git.rebase._create_providers", return_value={"m1": AsyncMock(), "m2": AsyncMock()}),
            patch("sova.git.rebase._configure_diff3", new_callable=AsyncMock),
            patch("sova.git.rebase._try_consensus_resolution", new_callable=AsyncMock) as mock_consensus,
            patch("sova.git.rebase.invoke_command", new_callable=AsyncMock) as mock_llm,
        ):
            mock_run.side_effect = _git_side_effect(["file.py\n", None])
            mock_consensus.return_value = (True, Decimal("0.05"))
            mock_llm.return_value = MagicMock(text="done", cost_usd=Decimal("0.01"))

            result, _cost = await rebase_with_conflict_resolution("main", cwd=tmp_path)

        assert result.success is False
        assert "verify" in result.error
        assert mock_llm.await_count == 0, "must not fall through to a paid resolution pass"

    async def test_consensus_path_aborts_when_post_continue_check_fails(self, tmp_path: Path) -> None:
        """Same invariant on the consensus path's post-`--continue` check."""
        (tmp_path / "file.py").write_text("clean\n")

        with (
            patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.rebase._load_consensus_config", return_value=(["m1", "m2"], 0.66, {}, None)),
            patch("sova.git.rebase._create_providers", return_value={"m1": AsyncMock(), "m2": AsyncMock()}),
            patch("sova.git.rebase._configure_diff3", new_callable=AsyncMock),
            patch("sova.git.rebase._try_consensus_resolution", new_callable=AsyncMock) as mock_consensus,
            patch("sova.git.rebase.invoke_command", new_callable=AsyncMock) as mock_llm,
        ):
            # conflicted, consensus verifies clean, then the post-continue check fails
            mock_run.side_effect = _git_side_effect(["file.py\n", "", None], continue_ok=False)
            mock_consensus.return_value = (True, Decimal("0.05"))
            mock_llm.return_value = MagicMock(text="done", cost_usd=Decimal("0.01"))

            result, _cost = await rebase_with_conflict_resolution("main", cwd=tmp_path)

        assert result.success is False
        assert "verify" in result.error
        assert mock_llm.await_count == 0, "must not fall through to a paid resolution pass"

    async def test_clean_resolution_still_succeeds(self, tmp_path: Path) -> None:
        """Verification must not block a genuinely resolved conflict."""
        (tmp_path / "file.py").write_text("resolved content\n")

        with (
            patch("sova.git.rebase.run", new_callable=AsyncMock) as mock_run,
            patch("sova.git.rebase._load_consensus_config", return_value=([], 0.66, {}, None)),
            patch("sova.git.rebase.invoke_command", new_callable=AsyncMock) as mock_llm,
        ):
            mock_run.side_effect = _git_side_effect(["file.py\n", ""])
            mock_llm.return_value = MagicMock(text="done", cost_usd=Decimal("0.01"))

            result, cost = await rebase_with_conflict_resolution("main", cwd=tmp_path)

        assert result.success is True
        assert result.conflicts_resolved == 1
        assert cost == Decimal("0.01")
        assert mock_llm.await_count == 1
