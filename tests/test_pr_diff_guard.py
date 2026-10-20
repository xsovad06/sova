"""Tests for sova.core.pr_diff_guard."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from sova.core.pr_diff_guard import (
    GuardResult,
    check_pin_downgrades,
    check_silent_revert,
    detect_reverted_files,
    diff_file_set,
    get_merge_base,
)
from sova.utils.shell import ShellResult


def _ok(stdout: str = "", stderr: str = "") -> ShellResult:
    return ShellResult(returncode=0, stdout=stdout, stderr=stderr)


def _fail(stderr: str = "error") -> ShellResult:
    return ShellResult(returncode=1, stdout="", stderr=stderr)


class TestGetMergeBase:
    async def test_returns_stripped_sha(self) -> None:
        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _ok(stdout="abc123\n")
            sha = await get_merge_base(Path("/repo"), "origin/main", "HEAD")
            assert sha == "abc123"

    async def test_returns_none_on_failure(self) -> None:
        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _fail()
            assert await get_merge_base(Path("/repo"), "origin/main") is None

    async def test_returns_none_on_empty_output(self) -> None:
        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _ok(stdout="\n")
            assert await get_merge_base(Path("/repo"), "origin/main") is None


class TestDiffFileSet:
    async def test_parses_file_list(self) -> None:
        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _ok(stdout="a.py\nb/c.py\n")
            files = await diff_file_set(Path("/repo"), "sha1", "sha2")
            assert files == frozenset({"a.py", "b/c.py"})

    async def test_returns_empty_set_for_no_diff(self) -> None:
        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _ok(stdout="")
            assert await diff_file_set(Path("/repo"), "sha1", "sha2") == frozenset()

    async def test_returns_none_on_git_failure(self) -> None:
        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _fail()
            assert await diff_file_set(Path("/repo"), "sha1", "sha2") is None


class TestDetectRevertedFiles:
    def test_flags_new_file_the_base_also_changed(self) -> None:
        reverted = detect_reverted_files(
            pre_run_files=frozenset({"pr_file.py"}),
            post_run_files=frozenset({"pr_file.py", "shared.py"}),
            base_changed_files=frozenset({"shared.py"}),
        )
        assert reverted == frozenset({"shared.py"})

    def test_does_not_flag_files_the_pr_already_owned(self) -> None:
        reverted = detect_reverted_files(
            pre_run_files=frozenset({"shared.py"}),
            post_run_files=frozenset({"shared.py"}),
            base_changed_files=frozenset({"shared.py"}),
        )
        assert reverted == frozenset()

    def test_does_not_flag_files_the_base_never_touched(self) -> None:
        reverted = detect_reverted_files(
            pre_run_files=frozenset({"pr_file.py"}),
            post_run_files=frozenset({"pr_file.py", "new_file.py"}),
            base_changed_files=frozenset(),
        )
        assert reverted == frozenset()

    def test_empty_base_changed_files_trivially_passes(self) -> None:
        reverted = detect_reverted_files(
            pre_run_files=frozenset(),
            post_run_files=frozenset({"anything.py"}),
            base_changed_files=frozenset(),
        )
        assert reverted == frozenset()


class TestCheckSilentRevert:
    async def _patch_calls(
        self,
        mock_run: AsyncMock,
        merge_base_map: dict[str, str],
        diff_map: dict[str, str],
        blob_map: dict[str, str] | None = None,
    ) -> None:
        blob_map = blob_map or {}

        async def side_effect(*args: str, cwd: object = None, **kwargs: object) -> ShellResult:
            if args[:2] == ("git", "fetch"):
                return _ok()
            if args[:2] == ("git", "cat-file"):
                return _ok()
            if args[:2] == ("git", "merge-base"):
                key = f"{args[2]}..{args[3]}"
                return _ok(stdout=merge_base_map[key] + "\n")
            if args[:2] == ("git", "diff"):
                if args[2] == "--name-only":
                    key = f"{args[3]}..{args[4]}"
                else:
                    key = f"{args[2]}..{args[3]}"
                return _ok(stdout=diff_map.get(key, ""))
            if args[:2] == ("git", "rev-parse"):
                spec = args[2]
                if spec in blob_map:
                    return _ok(stdout=blob_map[spec] + "\n")
                return _fail(stderr=f"fatal: invalid object name {spec}")
            raise AssertionError(f"unexpected call: {args}")

        mock_run.side_effect = side_effect

    async def test_passes_when_no_overlap(self) -> None:
        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock) as mock_run:
            await self._patch_calls(
                mock_run,
                merge_base_map={
                    "origin/main..presha": "base_sha",
                    "origin/main..HEAD": "base_sha",
                },
                diff_map={
                    "base_sha..presha": "pr_file.py\n",
                    "base_sha..HEAD": "pr_file.py\n",
                    "base_sha..origin/main": "",
                },
            )
            result = await check_silent_revert(Path("/repo"), "main", "presha")
            assert result == GuardResult(ok=True, pre_merge_base="base_sha", post_merge_base="base_sha")

    async def test_flags_silent_revert(self) -> None:
        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock) as mock_run:
            await self._patch_calls(
                mock_run,
                merge_base_map={
                    "origin/main..presha": "base_sha",
                    "origin/main..HEAD": "base_sha",
                },
                diff_map={
                    "base_sha..presha": "pr_file.py\n",
                    "base_sha..HEAD": "pr_file.py\nshared.py\n",
                    "base_sha..origin/main": "shared.py\n",
                },
                blob_map={
                    "HEAD:shared.py": "blob_old",
                    "base_sha:shared.py": "blob_old",
                    "origin/main:shared.py": "blob_new",
                },
            )
            result = await check_silent_revert(Path("/repo"), "main", "presha")
            assert result.ok is False
            assert "shared.py" in result.files
            assert result.post_merge_base == "base_sha"

    async def test_rebased_pr_still_flags_sibling_dropped_by_the_rebase(self) -> None:
        """address-pr's normal job is to rebase onto the current base, which makes

        post_merge_base equal to the base tip. The sibling's change must still be
        detected from the pre-run fork point, not lost because the post-run diff
        against the base is now empty.
        """
        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock) as mock_run:
            await self._patch_calls(
                mock_run,
                merge_base_map={
                    "origin/main..presha": "old_base_sha",
                    "origin/main..HEAD": "new_base_sha",
                },
                diff_map={
                    "old_base_sha..presha": "pr_file.py\n",
                    "new_base_sha..HEAD": "pr_file.py\nshared.py\n",
                    # The naive (pre-fix) base diff from the post-run fork point
                    # (new_base_sha, i.e. the current base tip) is empty: the
                    # sibling's change only shows up relative to the pre-run
                    # fork point.
                    "new_base_sha..origin/main": "",
                    "old_base_sha..origin/main": "shared.py\n",
                },
                blob_map={
                    "HEAD:shared.py": "blob_old",
                    "old_base_sha:shared.py": "blob_old",
                    "origin/main:shared.py": "blob_new",
                },
            )
            result = await check_silent_revert(Path("/repo"), "main", "presha")
            assert result.ok is False
            assert "shared.py" in result.files

    async def test_does_not_flag_branchs_own_new_edit_to_a_file_the_base_also_touched(self) -> None:
        """Set-identity alone (the pre-fix behavior) would flag this: the branch is new to

        the file and the base also changed it. But the branch's HEAD content differs from
        the pre-sibling content, so this is the branch's own edit, not a revert.
        """
        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock) as mock_run:
            await self._patch_calls(
                mock_run,
                merge_base_map={
                    "origin/main..presha": "base_sha",
                    "origin/main..HEAD": "base_sha",
                },
                diff_map={
                    "base_sha..presha": "pr_file.py\n",
                    "base_sha..HEAD": "pr_file.py\nshared.py\n",
                    "base_sha..origin/main": "shared.py\n",
                },
                blob_map={
                    "HEAD:shared.py": "blob_branch_edit",
                    "base_sha:shared.py": "blob_old",
                    "origin/main:shared.py": "blob_new",
                },
            )
            result = await check_silent_revert(Path("/repo"), "main", "presha")
            assert result.ok is True

    async def test_branch_deleting_a_file_the_base_also_changed_is_inconclusive_not_confirmed(self) -> None:
        """A branch that legitimately deleted a path (e.g. per review request) must not be

        reported as a confirmed silent revert just because the base also changed that path:
        head_blob being unresolvable (the file is gone) while pre_blob resolved (the file
        existed pre-run) is a deliberate deletion, not "the old content survived".
        """
        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock) as mock_run:
            await self._patch_calls(
                mock_run,
                merge_base_map={
                    "origin/main..presha": "base_sha",
                    "origin/main..HEAD": "base_sha",
                },
                diff_map={
                    "base_sha..presha": "pr_file.py\n",
                    "base_sha..HEAD": "pr_file.py\nshared.py\n",
                    "base_sha..origin/main": "shared.py\n",
                },
                blob_map={
                    # No "HEAD:shared.py" entry: the branch deleted the file, so
                    # that rev-parse is unresolvable.
                    "base_sha:shared.py": "blob_old",
                },
            )
            result = await check_silent_revert(Path("/repo"), "main", "presha")
            assert result.ok is False
            assert result.inconclusive is True
            assert "branch deleted shared.py" in result.reason

    async def test_branch_editing_a_file_the_base_added_after_the_fork_is_not_a_revert(self) -> None:
        """The path not existing at the pre-run fork point at all means the base added it

        after the fork; the branch head having content there (typically after rebasing
        onto the base's new file and then editing it, e.g. per a review request) has no
        pre-sibling content to have reverted, so this must not be flagged at all, let
        alone as a confirmed revert.
        """
        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock) as mock_run:
            await self._patch_calls(
                mock_run,
                merge_base_map={
                    "origin/main..presha": "base_sha",
                    "origin/main..HEAD": "base_sha",
                },
                diff_map={
                    "base_sha..presha": "pr_file.py\n",
                    "base_sha..HEAD": "pr_file.py\nshared.py\n",
                    "base_sha..origin/main": "shared.py\n",
                },
                blob_map={
                    "HEAD:shared.py": "blob_x",
                    # No "base_sha:shared.py" entry: the path did not exist at the
                    # fork point, so the base added it afterward.
                },
            )
            result = await check_silent_revert(Path("/repo"), "main", "presha")
            assert result.ok is True

    async def test_confirmed_revert_wins_over_an_unrelated_deleted_candidate(self) -> None:
        """Evaluating every candidate (rather than returning on the first inconclusive one)

        means a run that both legitimately deleted one file and silently reverted another
        reports the confirmed revert, naming the file that actually matters, instead of
        stopping at whichever candidate happens to sort first.
        """
        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock) as mock_run:
            await self._patch_calls(
                mock_run,
                merge_base_map={
                    "origin/main..presha": "base_sha",
                    "origin/main..HEAD": "base_sha",
                },
                diff_map={
                    "base_sha..presha": "pr_file.py\n",
                    "base_sha..HEAD": "pr_file.py\na_deleted.py\nshared.py\n",
                    "base_sha..origin/main": "a_deleted.py\nshared.py\n",
                },
                blob_map={
                    # a_deleted.py: branch legitimately deleted it (no HEAD blob).
                    "base_sha:a_deleted.py": "blob_old",
                    # shared.py: branch head carries the pre-sibling content.
                    "HEAD:shared.py": "blob_old",
                    "base_sha:shared.py": "blob_old",
                    "origin/main:shared.py": "blob_new",
                },
            )
            result = await check_silent_revert(Path("/repo"), "main", "presha")
            assert result.ok is False
            assert result.inconclusive is False
            assert result.files == frozenset({"shared.py"})

    async def test_quiet_base_trivially_passes(self) -> None:
        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock) as mock_run:
            await self._patch_calls(
                mock_run,
                merge_base_map={
                    "origin/main..presha": "base_sha",
                    "origin/main..HEAD": "base_sha",
                },
                diff_map={
                    "base_sha..presha": "pr_file.py\n",
                    "base_sha..HEAD": "pr_file.py\nunrelated_new_file.py\n",
                    "base_sha..origin/main": "",
                },
            )
            result = await check_silent_revert(Path("/repo"), "main", "presha")
            assert result.ok is True

    async def test_fails_closed_on_unresolvable_merge_base(self) -> None:
        async def side_effect(*args: str, cwd: object = None, **kwargs: object) -> ShellResult:
            if args[:2] == ("git", "fetch"):
                return _ok()
            if args[:2] == ("git", "cat-file"):
                return _ok()
            return _fail(stderr="fatal: no merge base")

        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock, side_effect=side_effect):
            result = await check_silent_revert(Path("/repo"), "main", "presha")
            assert result.ok is False
            assert result.inconclusive is True
            assert "fork point" in result.reason

    async def test_fetch_failure_is_inconclusive_not_a_confirmed_revert(self) -> None:
        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _fail(stderr="fatal: unable to access origin")
            result = await check_silent_revert(Path("/repo"), "main", "presha")
            assert result.ok is False
            assert result.inconclusive is True
            assert "fetch" in result.reason

    async def test_post_merge_base_failure_is_inconclusive(self) -> None:
        async def side_effect(*args: str, cwd: object = None, **kwargs: object) -> ShellResult:
            if args[:2] == ("git", "fetch"):
                return _ok()
            if args[:2] == ("git", "cat-file"):
                return _ok()
            if args[:2] == ("git", "merge-base") and args[3] == "presha":
                return _ok(stdout="base_sha\n")
            if args[:2] == ("git", "merge-base") and args[3] == "HEAD":
                return _fail(stderr="fatal: no merge base for HEAD")
            raise AssertionError(f"unexpected call: {args}")

        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock, side_effect=side_effect):
            result = await check_silent_revert(Path("/repo"), "main", "presha")
            assert result.ok is False
            assert result.inconclusive is True
            assert "HEAD" in result.reason

    async def test_pre_run_diff_failure_is_inconclusive(self) -> None:
        async def side_effect(*args: str, cwd: object = None, **kwargs: object) -> ShellResult:
            if args[:2] == ("git", "fetch"):
                return _ok()
            if args[:2] == ("git", "cat-file"):
                return _ok()
            if args[:2] == ("git", "merge-base"):
                return _ok(stdout="base_sha\n")
            if args[:2] == ("git", "diff") and args[4] == "presha":
                return _fail(stderr="fatal: bad revision presha")
            raise AssertionError(f"unexpected call: {args}")

        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock, side_effect=side_effect):
            result = await check_silent_revert(Path("/repo"), "main", "presha")
            assert result.ok is False
            assert result.inconclusive is True
            assert "presha" in result.reason

    async def test_post_run_diff_failure_is_inconclusive(self) -> None:
        async def side_effect(*args: str, cwd: object = None, **kwargs: object) -> ShellResult:
            if args[:2] == ("git", "fetch"):
                return _ok()
            if args[:2] == ("git", "cat-file"):
                return _ok()
            if args[:2] == ("git", "merge-base"):
                return _ok(stdout="base_sha\n")
            if args[:2] == ("git", "diff") and args[4] == "presha":
                return _ok(stdout="pr_file.py\n")
            if args[:2] == ("git", "diff") and args[4] == "HEAD":
                return _fail(stderr="fatal: bad revision HEAD")
            raise AssertionError(f"unexpected call: {args}")

        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock, side_effect=side_effect):
            result = await check_silent_revert(Path("/repo"), "main", "presha")
            assert result.ok is False
            assert result.inconclusive is True
            assert "HEAD" in result.reason

    async def test_base_changed_diff_failure_is_inconclusive(self) -> None:
        async def side_effect(*args: str, cwd: object = None, **kwargs: object) -> ShellResult:
            if args[:2] == ("git", "fetch"):
                return _ok()
            if args[:2] == ("git", "cat-file"):
                return _ok()
            if args[:2] == ("git", "merge-base"):
                return _ok(stdout="base_sha\n")
            if args[:2] == ("git", "diff") and args[4] == "presha":
                return _ok(stdout="pr_file.py\n")
            if args[:2] == ("git", "diff") and args[4] == "HEAD":
                return _ok(stdout="pr_file.py\n")
            if args[:2] == ("git", "diff") and args[4] == "origin/main":
                return _fail(stderr="fatal: bad revision origin/main")
            raise AssertionError(f"unexpected call: {args}")

        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock, side_effect=side_effect):
            result = await check_silent_revert(Path("/repo"), "main", "presha")
            assert result.ok is False
            assert result.inconclusive is True
            assert "origin/main" in result.reason

    async def test_baseline_unavailable_locally_skips_as_cannot_verify(self) -> None:
        """A baseline SHA pushed from elsewhere (web UI, another machine) after this

        worktree's last fetch of the PR branch may not exist locally. That is "cannot
        verify", not "verified bad": the check skips with ok=True rather than treating
        the missing commit as a confirmed revert, even after trying to fetch head_ref.
        """
        calls: list[tuple[str, ...]] = []

        async def side_effect(*args: str, cwd: object = None, **kwargs: object) -> ShellResult:
            calls.append(args)
            if args[:2] == ("git", "fetch"):
                return _ok()
            if args[:2] == ("git", "cat-file"):
                return _fail(stderr="fatal: Not a valid object name presha^{commit}")
            raise AssertionError(f"unexpected call: {args}")

        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock, side_effect=side_effect):
            result = await check_silent_revert(Path("/repo"), "main", "presha", head_ref="feat/x")

        assert result == GuardResult(ok=True)
        assert ("git", "fetch", "origin", "feat/x") in calls

    async def test_baseline_becomes_available_after_fetching_head_ref(self) -> None:
        """When the baseline is missing, fetching head_ref may pull it in (it is typically

        an ancestor of the PR branch); the check must then proceed normally instead of
        giving up after the first cat-file miss.
        """
        cat_file_calls = 0

        async def side_effect(*args: str, cwd: object = None, **kwargs: object) -> ShellResult:
            nonlocal cat_file_calls
            if args[:2] == ("git", "fetch"):
                return _ok()
            if args[:2] == ("git", "cat-file"):
                cat_file_calls += 1
                return _fail() if cat_file_calls == 1 else _ok()
            if args[:2] == ("git", "merge-base"):
                return _ok(stdout="base_sha\n")
            if args[:2] == ("git", "diff") and args[2] == "--name-only":
                return _ok(stdout="")
            raise AssertionError(f"unexpected call: {args}")

        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock, side_effect=side_effect):
            result = await check_silent_revert(Path("/repo"), "main", "presha", head_ref="feat/x")

        assert result.ok is True
        assert cat_file_calls == 2


class TestCheckPinDowngrades:
    async def test_flags_downgraded_python_pin(self) -> None:
        async def side_effect(*args: str, cwd: object = None, **kwargs: object) -> ShellResult:
            if args[:2] == ("git", "diff") and "--" not in args:
                return _ok(stdout="requirements.txt\n")
            if "--" in args:
                return _ok(stdout="-requests==2.31.0\n+requests==2.20.0\n")
            raise AssertionError(f"unexpected call: {args}")

        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock, side_effect=side_effect):
            downgrades, _ = await check_pin_downgrades(Path("/repo"), "base", "head")

        assert len(downgrades) == 1
        assert downgrades[0].package == "requests"
        assert downgrades[0].from_version == "2.31.0"
        assert downgrades[0].to_version == "2.20.0"

    async def test_does_not_flag_upgrade(self) -> None:
        async def side_effect(*args: str, cwd: object = None, **kwargs: object) -> ShellResult:
            if args[:2] == ("git", "diff") and "--" not in args:
                return _ok(stdout="requirements.txt\n")
            if "--" in args:
                return _ok(stdout="-requests==2.20.0\n+requests==2.31.0\n")
            raise AssertionError(f"unexpected call: {args}")

        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock, side_effect=side_effect):
            downgrades, _ = await check_pin_downgrades(Path("/repo"), "base", "head")

        assert downgrades == []

    async def test_does_not_flag_new_dependency(self) -> None:
        async def side_effect(*args: str, cwd: object = None, **kwargs: object) -> ShellResult:
            if args[:2] == ("git", "diff") and "--" not in args:
                return _ok(stdout="requirements.txt\n")
            if "--" in args:
                return _ok(stdout="+newpkg==1.0.0\n")
            raise AssertionError(f"unexpected call: {args}")

        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock, side_effect=side_effect):
            downgrades, _ = await check_pin_downgrades(Path("/repo"), "base", "head")

        assert downgrades == []

    async def test_does_not_flag_removed_dependency(self) -> None:
        async def side_effect(*args: str, cwd: object = None, **kwargs: object) -> ShellResult:
            if args[:2] == ("git", "diff") and "--" not in args:
                return _ok(stdout="requirements.txt\n")
            if "--" in args:
                return _ok(stdout="-oldpkg==1.0.0\n")
            raise AssertionError(f"unexpected call: {args}")

        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock, side_effect=side_effect):
            downgrades, _ = await check_pin_downgrades(Path("/repo"), "base", "head")

        assert downgrades == []

    async def test_pairs_multiple_lockfile_versions_positionally(self) -> None:
        """A lockfile version line carries no package name, so entries must pair by diff order,

        not collapse into one dict key (the bug: ``groups.get("name", "")`` made every
        version line in the file key to the same empty string, so only the last
        removed/added pair survived and unrelated packages' versions were compared).
        """

        async def side_effect(*args: str, cwd: object = None, **kwargs: object) -> ShellResult:
            if args[:2] == ("git", "diff") and "--" not in args:
                return _ok(stdout="package-lock.json\n")
            if "--" in args:
                return _ok(
                    stdout=(
                        '-      "version": "1.2.3",\n'
                        '+      "version": "1.2.0",\n'
                        '-      "version": "4.5.6",\n'
                        '+      "version": "4.5.7",\n'
                    )
                )
            raise AssertionError(f"unexpected call: {args}")

        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock, side_effect=side_effect):
            downgrades, _ = await check_pin_downgrades(Path("/repo"), "base", "head")

        assert len(downgrades) == 1
        assert downgrades[0].from_version == "1.2.3"
        assert downgrades[0].to_version == "1.2.0"

    async def test_unbalanced_added_only_block_does_not_shift_later_pairing(self) -> None:
        """An added-only block (a brand new lockfile entry, no matching removal) must not

        shift index-based pairing for every pair after it: with the old zip-by-index
        pairing, this diff wrongly paired the new entry's "1.0.0" against the unrelated
        "-2.0.0" (reporting a false downgrade), while the real pair "-2.0.0"/"+3.0.0" is
        an upgrade, and the new entry alone is not a downgrade either.
        """

        async def side_effect(*args: str, cwd: object = None, **kwargs: object) -> ShellResult:
            if args[:2] == ("git", "diff") and "--" not in args:
                return _ok(stdout="package-lock.json\n")
            if "--" in args:
                return _ok(
                    stdout=('+      "version": "1.0.0",\n-      "version": "2.0.0",\n+      "version": "3.0.0",\n')
                )
            raise AssertionError(f"unexpected call: {args}")

        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock, side_effect=side_effect):
            downgrades, _ = await check_pin_downgrades(Path("/repo"), "base", "head")

        assert downgrades == []

    async def test_ignores_unrelated_files(self) -> None:
        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _ok(stdout="README.md\n")
            downgrades, _ = await check_pin_downgrades(Path("/repo"), "base", "head")
            assert downgrades == []
            mock_run.assert_called_once()

    async def test_unparseable_version_is_not_flagged(self) -> None:
        async def side_effect(*args: str, cwd: object = None, **kwargs: object) -> ShellResult:
            if args[:2] == ("git", "diff") and "--" not in args:
                return _ok(stdout="requirements.txt\n")
            if "--" in args:
                return _ok(stdout="-weirdpkg==not-a-version\n+weirdpkg==also-not-one\n")
            raise AssertionError(f"unexpected call: {args}")

        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock, side_effect=side_effect):
            downgrades, _ = await check_pin_downgrades(Path("/repo"), "base", "head")

        assert downgrades == []

    async def test_returns_empty_when_no_files_changed(self) -> None:
        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = _ok(stdout="")
            downgrades, _ = await check_pin_downgrades(Path("/repo"), "base", "head")
            assert downgrades == []

    async def test_skips_non_matching_lines_in_lockfile_diff(self) -> None:
        """A lockfile diff also carries unrelated context/hunk lines that must be skipped."""

        async def side_effect(*args: str, cwd: object = None, **kwargs: object) -> ShellResult:
            if args[:2] == ("git", "diff") and "--" not in args:
                return _ok(stdout="package-lock.json\n")
            if "--" in args:
                return _ok(
                    stdout=(
                        "@@ -10,7 +10,7 @@\n"
                        '       "resolved": "https://example.com/pkg.tgz",\n'
                        '-      "version": "1.2.3",\n'
                        '+      "version": "1.2.0",\n'
                    )
                )
            raise AssertionError(f"unexpected call: {args}")

        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock, side_effect=side_effect):
            downgrades, _ = await check_pin_downgrades(Path("/repo"), "base", "head")

        assert len(downgrades) == 1
        assert downgrades[0].from_version == "1.2.3"
        assert downgrades[0].to_version == "1.2.0"

    async def test_skips_non_matching_lines_in_requirements_diff(self) -> None:
        """A requirements.txt diff also carries unchanged context lines that must be skipped."""

        async def side_effect(*args: str, cwd: object = None, **kwargs: object) -> ShellResult:
            if args[:2] == ("git", "diff") and "--" not in args:
                return _ok(stdout="requirements.txt\n")
            if "--" in args:
                return _ok(stdout=" unchanged==1.0.0\n-requests==2.31.0\n+requests==2.20.0\n")
            raise AssertionError(f"unexpected call: {args}")

        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock, side_effect=side_effect):
            downgrades, _ = await check_pin_downgrades(Path("/repo"), "base", "head")

        assert len(downgrades) == 1
        assert downgrades[0].package == "requests"

    async def test_flags_downgraded_pin_in_pyproject_toml(self) -> None:
        """pyproject.toml's dependencies array entries are indented and quoted, unlike requirements.txt."""

        async def side_effect(*args: str, cwd: object = None, **kwargs: object) -> ShellResult:
            if args[:2] == ("git", "diff") and "--" not in args:
                return _ok(stdout="pyproject.toml\n")
            if "--" in args:
                return _ok(stdout='-    "requests==2.31.0",\n+    "requests==2.20.0",\n')
            raise AssertionError(f"unexpected call: {args}")

        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock, side_effect=side_effect):
            downgrades, _ = await check_pin_downgrades(Path("/repo"), "base", "head")

        assert len(downgrades) == 1
        assert downgrades[0].package == "requests"
        assert downgrades[0].from_version == "2.31.0"
        assert downgrades[0].to_version == "2.20.0"

    async def test_does_not_flag_package_json_own_version_field(self) -> None:
        """package.json's own "version" field matches the same quoted-key/digit-value shape as a

        dependency pin, but lowering a project's own version (or narrowing an "engines" range)
        is not a dependency pin downgrade. A real dependency downgrade on another line must
        still be flagged.
        """

        async def side_effect(*args: str, cwd: object = None, **kwargs: object) -> ShellResult:
            if args[:2] == ("git", "diff") and "--" not in args:
                return _ok(stdout="package.json\n")
            if "--" in args:
                return _ok(
                    stdout=(
                        '-  "version": "2.0.0",\n'
                        '+  "version": "1.0.0",\n'
                        '-    "node": "20",\n'
                        '+    "node": "18",\n'
                        '-    "requests": "2.31.0",\n'
                        '+    "requests": "2.20.0",\n'
                    )
                )
            raise AssertionError(f"unexpected call: {args}")

        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock, side_effect=side_effect):
            downgrades, _ = await check_pin_downgrades(Path("/repo"), "base", "head")

        assert len(downgrades) == 1
        assert downgrades[0].package == "requests"

    async def test_does_not_flag_lockfile_root_self_entry_version(self) -> None:
        """A v2/v3 lockfile's root self-entry (`"packages": { "": { "version": ... } } `)

        is the project's own version, not a dependency pin; the empty key never matches
        `_LOCKFILE_KEY_OPEN_RE`, so without filtering the "packages" container key the
        version line would still be reported as a downgrade of a nonexistent "packages"
        package. A genuine dependency downgrade elsewhere in the same diff must still be
        flagged.
        """

        async def side_effect(*args: str, cwd: object = None, **kwargs: object) -> ShellResult:
            if args[:2] == ("git", "diff") and "--" not in args:
                return _ok(stdout="package-lock.json\n")
            if "--" in args:
                return _ok(
                    stdout=(
                        '  "packages": {\n'
                        '    "": {\n'
                        '      "name": "test-pkg",\n'
                        '-      "version": "2.0.0",\n'
                        '+      "version": "1.0.0",\n'
                        "    },\n"
                        '    "node_modules/requests": {\n'
                        '-      "version": "2.31.0",\n'
                        '+      "version": "2.20.0",\n'
                        "    }\n"
                        "  }\n"
                    )
                )
            raise AssertionError(f"unexpected call: {args}")

        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock, side_effect=side_effect):
            downgrades, _ = await check_pin_downgrades(Path("/repo"), "base", "head")

        assert len(downgrades) == 1
        assert downgrades[0].package == "requests"
        assert downgrades[0].from_version == "2.31.0"
        assert downgrades[0].to_version == "2.20.0"

    async def test_does_not_cross_pair_same_package_in_different_sections(self) -> None:
        """A package pinned in two places in one file (dependencies and devDependencies,

        or pyproject.toml's main deps and an optional-dependency group) must not have a
        removal from one section paired with an addition from the other: that silently
        overwrites the first occurrence and can mask a real downgrade entirely.
        """

        async def side_effect(*args: str, cwd: object = None, **kwargs: object) -> ShellResult:
            if args[:2] == ("git", "diff") and "--" not in args:
                return _ok(stdout="package.json\n")
            if "--" in args:
                return _ok(
                    stdout=(
                        '-    "lodash": "4.17.21",\n'
                        '+    "lodash": "4.17.10",\n'
                        '-    "lodash": "3.0.0",\n'
                        '+    "lodash": "3.5.0",\n'
                    )
                )
            raise AssertionError(f"unexpected call: {args}")

        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock, side_effect=side_effect):
            downgrades, _ = await check_pin_downgrades(Path("/repo"), "base", "head")

        assert len(downgrades) == 1
        assert downgrades[0].package == "lodash"
        assert downgrades[0].from_version == "4.17.21"
        assert downgrades[0].to_version == "4.17.10"

    async def test_per_file_diff_failure_is_skipped_but_marked_inconclusive(self) -> None:
        """A git failure diffing one dependency file must not abort the whole scan, but it must

        not look like a clean result either: the caller needs to know this specific file's
        pins could not actually be checked.
        """

        async def side_effect(*args: str, cwd: object = None, **kwargs: object) -> ShellResult:
            if args[:2] == ("git", "diff") and "--" not in args:
                return _ok(stdout="requirements.txt\n")
            if "--" in args:
                return _fail(stderr="fatal: bad revision")
            raise AssertionError(f"unexpected call: {args}")

        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock, side_effect=side_effect):
            downgrades, inconclusive = await check_pin_downgrades(Path("/repo"), "base", "head")

        assert downgrades == []
        assert inconclusive is True

    async def test_top_level_diff_failure_is_inconclusive_not_clean(self) -> None:
        """diff_file_set() distinguishes "git failed" (None) from "nothing changed" (an empty

        set). Collapsing both into "no downgrades" would silently disable this check on a
        git failure, since both are in the function's success range.
        """

        async def side_effect(*args: str, cwd: object = None, **kwargs: object) -> ShellResult:
            if args[:2] == ("git", "diff") and args[2] == "--name-only":
                return _fail(stderr="fatal: bad revision")
            raise AssertionError(f"unexpected call: {args}")

        with patch("sova.core.pr_diff_guard.run_shell", new_callable=AsyncMock, side_effect=side_effect):
            downgrades, inconclusive = await check_pin_downgrades(Path("/repo"), "base", "head")

        assert downgrades == []
        assert inconclusive is True

    async def test_real_git_diff_does_not_mispair_a_replaced_lockfile_dependency(self, tmp_path: Path) -> None:
        """An ordinary `npm install` swap of one dependency for another (package A's entire

        lockfile entry removed, package B's entirely new entry added) must not be reported
        as if B's version were a downgrade of A: that is a replacement, not a downgrade of
        either package. A genuine same-package downgrade elsewhere in the same diff must
        still be flagged. Uses a real git repo (not a hand-built diff string) because the
        line-adjacency shape this guards against is exactly what git itself emits for this
        case, which a synthetic fixture can hide.
        """
        import subprocess

        def git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

        git("init", "-q", "-b", "main")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "Test")
        # Neutralize a developer's global git config (commit.gpgsign, hooks):
        # without this, `git commit` fails on a machine with signing enabled
        # (see tests/test_rebase_verification.py's fixture repo for the same
        # guard).
        git("config", "commit.gpgsign", "false")
        git("config", "core.hooksPath", "/dev/null")

        lockfile_v1 = """{
  "name": "test-pkg",
  "packages": {
    "node_modules/aaa": {
      "version": "1.0.1",
      "resolved": "https://registry.npmjs.org/aaa/-/aaa-1.0.1.tgz"
    },
    "node_modules/mmm": {
      "version": "2.0.0",
      "resolved": "https://registry.npmjs.org/mmm/-/mmm-2.0.0.tgz"
    }
  }
}
"""
        (tmp_path / "package-lock.json").write_text(lockfile_v1)
        git("add", "package-lock.json")
        git("commit", "-q", "-m", "initial")
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout.strip()

        # aaa is replaced by an unrelated new package zzz (an ordinary dependency swap,
        # the same shape `npm install` produces): confirmed via a real `git diff` on this
        # exact fixture that its "-version" line sits immediately before zzz's "+version"
        # line, with nothing else between them. mmm is genuinely downgraded.
        lockfile_v2 = """{
  "name": "test-pkg",
  "packages": {
    "node_modules/zzz": {
      "version": "0.5.0",
      "resolved": "https://registry.npmjs.org/zzz/-/zzz-0.5.0.tgz"
    },
    "node_modules/mmm": {
      "version": "1.9.0",
      "resolved": "https://registry.npmjs.org/mmm/-/mmm-1.9.0.tgz"
    }
  }
}
"""
        (tmp_path / "package-lock.json").write_text(lockfile_v2)
        git("add", "package-lock.json")
        git("commit", "-q", "-m", "swap aaa for zzz, downgrade mmm")
        head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout.strip()

        downgrades, inconclusive = await check_pin_downgrades(tmp_path, base_sha, head_sha)

        assert inconclusive is False
        assert len(downgrades) == 1
        assert downgrades[0].package == "mmm"
        assert downgrades[0].from_version == "2.0.0"
        assert downgrades[0].to_version == "1.9.0"


class TestCompareVersions:
    async def test_generic_parser_exception_falls_through_to_tuple_compare(self) -> None:
        from sova.core.pr_diff_guard import _compare_versions

        with patch("packaging.version.Version", side_effect=TypeError("boom")):
            result = _compare_versions("1.0.0", "2.0.0")

        assert result is False

    async def test_unicode_digit_that_int_rejects_is_unparseable(self) -> None:
        """``str.isdigit()`` is True for Unicode superscript digits, but ``int()`` rejects them."""
        from sova.core.pr_diff_guard import _compare_versions

        result = _compare_versions("1.².0", "1.0.0")

        assert result is None

    async def test_packaging_import_failure_falls_through_to_tuple_compare(self) -> None:
        """A missing/broken ``packaging`` install must degrade to the tuple comparison, not

        raise ``UnboundLocalError`` from evaluating ``except InvalidVersion`` before the name
        was ever bound.
        """
        import sys

        from sova.core.pr_diff_guard import _compare_versions

        with patch.dict(sys.modules, {"packaging.version": None}):
            result = _compare_versions("2.0.0", "1.0.0")

        assert result is True
