"""Tests for sova.supervisor.file_overlap (file overlap gate)."""

from __future__ import annotations

import pytest

from sova.supervisor.file_overlap import (
    _CROSS_CUTTING_FILES,
    BranchFileSet,
    OverlapResult,
    _extract_files_from_body,
    _fetch_branch_files,
    _find_matching_files,
    check_file_overlap,
    get_active_branch_file_sets,
    predict_candidate_files,
)

# ---------------------------------------------------------------------------
# BranchFileSet / OverlapResult dataclass tests
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_branch_file_set_frozen(self) -> None:
        bfs = BranchFileSet(
            issue_number="42",
            run_id=1,
            pr_number=100,
            branch_name="feat/issue-42",
            files=frozenset(["sova/core/steps/foo.py"]),
        )
        assert bfs.issue_number == "42"
        with pytest.raises(AttributeError):
            bfs.issue_number = "99"  # type: ignore[misc]

    def test_overlap_result_frozen(self) -> None:
        o = OverlapResult(
            conflicting_issue="10",
            conflicting_branch="feat/issue-10",
            overlapping_files=frozenset(["sova/core/steps/foo.py"]),
        )
        assert o.conflicting_issue == "10"
        with pytest.raises(AttributeError):
            o.conflicting_issue = "20"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _extract_files_from_body
# ---------------------------------------------------------------------------


class TestExtractFilesFromBody:
    def test_empty_body(self) -> None:
        assert _extract_files_from_body("") == set()

    def test_no_paths(self) -> None:
        assert _extract_files_from_body("This is a plain text body") == set()

    def test_single_path(self) -> None:
        body = "Fix the bug in `sova/core/steps/monitor_ci.py`"
        result = _extract_files_from_body(body)
        assert result == {"sova/core/steps/monitor_ci.py"}

    def test_multiple_paths(self) -> None:
        body = """Modify `sova/config/models.py` and `sova/dashboard/settings_meta.py`.
Also update `tests/test_progression.py`."""
        result = _extract_files_from_body(body)
        assert result == {
            "sova/config/models.py",
            "sova/dashboard/settings_meta.py",
            "tests/test_progression.py",
        }

    def test_dotted_path_prefixes(self) -> None:
        body = "Edit `.claude/commands/develop.md` and `.github/workflows/ci.yml`"
        result = _extract_files_from_body(body)
        assert result == {
            ".claude/commands/develop.md",
            ".github/workflows/ci.yml",
        }

    def test_non_project_paths_ignored(self) -> None:
        body = "Run `npm install` and edit `package.json`"
        result = _extract_files_from_body(body)
        assert result == set()


# ---------------------------------------------------------------------------
# predict_candidate_files
# ---------------------------------------------------------------------------


class TestPredictCandidateFiles:
    def test_no_labels_no_body(self) -> None:
        result = predict_candidate_files([], "")
        assert result == set()

    def test_body_files_take_priority(self) -> None:
        result = predict_candidate_files(
            ["area:dashboard"],
            "Fix `sova/core/steps/foo.py` here",
        )
        assert "sova/core/steps/foo.py" in result
        assert "sova/dashboard/" not in result
        for f in _CROSS_CUTTING_FILES:
            assert f in result

    def test_area_label_maps_to_dirs(self) -> None:
        result = predict_candidate_files(["area:dashboard"], "")
        assert "sova/dashboard/" in result

    def test_area_label_no_cross_cutting(self) -> None:
        result = predict_candidate_files(["area:supervisor"], "")
        assert "sova/supervisor/" in result
        for f in _CROSS_CUTTING_FILES:
            assert f not in result

    def test_unknown_area_label(self) -> None:
        result = predict_candidate_files(["area:unknown_thing"], "")
        assert result == set()

    def test_non_area_labels_ignored(self) -> None:
        result = predict_candidate_files(["type:feature", "priority:high"], "")
        assert result == set()

    def test_multiple_area_labels(self) -> None:
        result = predict_candidate_files(["area:core", "area:cli"], "")
        assert "sova/core/" in result
        assert "sova/cli/" in result


# ---------------------------------------------------------------------------
# check_file_overlap
# ---------------------------------------------------------------------------


class TestCheckFileOverlap:
    def test_empty_candidate_files(self) -> None:
        bfs = BranchFileSet("10", 1, None, "feat/issue-10", frozenset(["sova/core/foo.py"]))
        assert check_file_overlap(set(), [bfs]) == []

    def test_empty_active_file_sets(self) -> None:
        assert check_file_overlap({"sova/core/foo.py"}, []) == []

    def test_no_overlap(self) -> None:
        bfs = BranchFileSet("10", 1, None, "feat/issue-10", frozenset(["sova/dashboard/app.py"]))
        result = check_file_overlap({"sova/core/foo.py"}, [bfs])
        assert result == []

    def test_exact_file_overlap(self) -> None:
        bfs = BranchFileSet("10", 1, None, "feat/issue-10", frozenset(["sova/core/foo.py"]))
        result = check_file_overlap({"sova/core/foo.py"}, [bfs])
        assert len(result) == 1
        assert result[0].conflicting_issue == "10"
        assert "sova/core/foo.py" in result[0].overlapping_files

    def test_prefix_overlap(self) -> None:
        bfs = BranchFileSet(
            "10",
            1,
            None,
            "feat/issue-10",
            frozenset(["sova/dashboard/app.py", "sova/dashboard/routers/foo.py"]),
        )
        result = check_file_overlap({"sova/dashboard/"}, [bfs])
        assert len(result) == 1
        assert len(result[0].overlapping_files) == 2

    def test_multiple_branches_overlap(self) -> None:
        bfs1 = BranchFileSet("10", 1, None, "feat/issue-10", frozenset(["sova/core/foo.py"]))
        bfs2 = BranchFileSet("20", 2, None, "feat/issue-20", frozenset(["sova/core/bar.py"]))
        result = check_file_overlap({"sova/core/"}, [bfs1, bfs2])
        assert len(result) == 2

    def test_branch_with_no_matching_files(self) -> None:
        bfs1 = BranchFileSet("10", 1, None, "feat/issue-10", frozenset(["sova/core/foo.py"]))
        bfs2 = BranchFileSet("20", 2, None, "feat/issue-20", frozenset(["docs/readme.md"]))
        result = check_file_overlap({"sova/core/"}, [bfs1, bfs2])
        assert len(result) == 1
        assert result[0].conflicting_issue == "10"

    def test_mixed_exact_and_prefix(self) -> None:
        bfs = BranchFileSet(
            "10",
            1,
            None,
            "feat/issue-10",
            frozenset(["sova/config/models.py", "sova/dashboard/app.py"]),
        )
        result = check_file_overlap({"sova/config/models.py", "sova/dashboard/"}, [bfs])
        assert len(result) == 1
        assert len(result[0].overlapping_files) == 2


# ---------------------------------------------------------------------------
# _find_matching_files
# ---------------------------------------------------------------------------


class TestFindMatchingFiles:
    def test_exact_match(self) -> None:
        result = _find_matching_files(
            frozenset(["sova/core/foo.py"]),
            {"sova/core/foo.py"},
            set(),
        )
        assert result == {"sova/core/foo.py"}

    def test_prefix_match(self) -> None:
        result = _find_matching_files(
            frozenset(["sova/dashboard/app.py", "sova/dashboard/routers/x.py"]),
            set(),
            {"sova/dashboard/"},
        )
        assert result == {"sova/dashboard/app.py", "sova/dashboard/routers/x.py"}

    def test_no_match(self) -> None:
        result = _find_matching_files(
            frozenset(["sova/core/foo.py"]),
            {"sova/dashboard/app.py"},
            {"sova/cli/"},
        )
        assert result == set()

    def test_mixed_match(self) -> None:
        result = _find_matching_files(
            frozenset(["sova/config/models.py", "sova/dashboard/app.py"]),
            {"sova/config/models.py"},
            {"sova/dashboard/"},
        )
        assert result == {"sova/config/models.py", "sova/dashboard/app.py"}


# ---------------------------------------------------------------------------
# check_file_overlap with threshold
# ---------------------------------------------------------------------------


class TestCheckFileOverlapThreshold:
    def test_threshold_zero_reports_any_overlap(self) -> None:
        bfs = BranchFileSet(
            "10",
            1,
            None,
            "feat/issue-10",
            frozenset(["sova/core/foo.py", "sova/dashboard/app.py", "docs/readme.md"]),
        )
        result = check_file_overlap({"sova/core/foo.py"}, [bfs], threshold=0.0)
        assert len(result) == 1

    def test_threshold_filters_low_overlap(self) -> None:
        bfs = BranchFileSet(
            "10",
            1,
            None,
            "feat/issue-10",
            frozenset(["sova/core/foo.py", "sova/dashboard/app.py", "docs/readme.md"]),
        )
        result = check_file_overlap({"sova/core/foo.py", "sova/cli/app.py"}, [bfs], threshold=0.5)
        assert len(result) == 0

    def test_threshold_passes_high_overlap(self) -> None:
        bfs = BranchFileSet(
            "10",
            1,
            None,
            "feat/issue-10",
            frozenset(["sova/core/foo.py"]),
        )
        result = check_file_overlap({"sova/core/foo.py"}, [bfs], threshold=0.5)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# get_active_branch_file_sets
# ---------------------------------------------------------------------------


class TestGetActiveBranchFileSets:
    @pytest.mark.asyncio
    async def test_excludes_current_issue(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.db.models import TaskRun

        run = MagicMock(spec=TaskRun)
        run.issue_number = "42"
        run.branch_name = "feat/issue-42"
        run.pr_number = None

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [run]
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(return_value=mock_result)

        mock_factory = MagicMock()
        mock_factory.return_value = mock_session

        with patch("sova.supervisor.file_overlap.load_config") as mock_cfg:
            cfg = MagicMock()
            cfg.github_repo = "user/repo"
            cfg.github_user = "user"
            cfg.base_branch = "main"
            mock_cfg.return_value = cfg

            result = await get_active_branch_file_sets(mock_factory, MagicMock(), exclude_issue="42")
            assert result == []

    @pytest.mark.asyncio
    async def test_returns_file_sets_for_active_runs(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.db.models import TaskRun

        run = MagicMock(spec=TaskRun)
        run.issue_number = "10"
        run.branch_name = "feat/issue-10"
        run.pr_number = None
        run.id = 1

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [run]
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(return_value=mock_result)

        mock_factory = MagicMock()
        mock_factory.return_value = mock_session

        with (
            patch("sova.supervisor.file_overlap.load_config") as mock_cfg,
            patch("sova.supervisor.file_overlap._fetch_branch_files") as mock_fetch,
        ):
            cfg = MagicMock()
            cfg.github_repo = "user/repo"
            cfg.github_user = "user"
            cfg.base_branch = "main"
            mock_cfg.return_value = cfg

            bfs = BranchFileSet("10", 1, None, "feat/issue-10", frozenset(["sova/core/foo.py"]))
            mock_fetch.return_value = bfs

            result = await get_active_branch_file_sets(mock_factory, MagicMock())
            assert len(result) == 1
            assert result[0].issue_number == "10"

    @pytest.mark.asyncio
    async def test_empty_when_no_active_runs(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(return_value=mock_result)

        mock_factory = MagicMock()
        mock_factory.return_value = mock_session

        with patch("sova.supervisor.file_overlap.load_config") as mock_cfg:
            cfg = MagicMock()
            cfg.github_repo = "user/repo"
            cfg.github_user = "user"
            cfg.base_branch = "main"
            mock_cfg.return_value = cfg

            result = await get_active_branch_file_sets(mock_factory, MagicMock())
            assert result == []

    @pytest.mark.asyncio
    async def test_fetch_exception_skipped(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.db.models import TaskRun

        run = MagicMock(spec=TaskRun)
        run.issue_number = "10"
        run.branch_name = "feat/issue-10"
        run.pr_number = None

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [run]
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(return_value=mock_result)

        mock_factory = MagicMock()
        mock_factory.return_value = mock_session

        with (
            patch("sova.supervisor.file_overlap.load_config") as mock_cfg,
            patch("sova.supervisor.file_overlap._fetch_branch_files", side_effect=RuntimeError("boom")),
        ):
            cfg = MagicMock()
            cfg.github_repo = "user/repo"
            cfg.github_user = "user"
            cfg.base_branch = "main"
            mock_cfg.return_value = cfg

            result = await get_active_branch_file_sets(mock_factory, MagicMock())
            assert result == []


# ---------------------------------------------------------------------------
# _fetch_branch_files
# ---------------------------------------------------------------------------


class TestFetchBranchFiles:
    @pytest.mark.asyncio
    async def test_pr_api_path(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.db.models import TaskRun

        run = MagicMock(spec=TaskRun)
        run.issue_number = "10"
        run.id = 1
        run.pr_number = 100
        run.branch_name = "feat/issue-10"

        with patch("sova.git.pr.get_pr_files", new_callable=AsyncMock) as mock_pr:
            mock_pr.return_value = ["sova/core/foo.py", "sova/core/bar.py"]

            result = await _fetch_branch_files(run, "user/repo", "user", "main")
            assert result is not None
            assert result.files == frozenset(["sova/core/foo.py", "sova/core/bar.py"])
            assert result.pr_number == 100
            mock_pr.assert_called_once_with(100, repo="user/repo", github_user="user")

    @pytest.mark.asyncio
    async def test_git_diff_fallback(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.db.models import TaskRun

        run = MagicMock(spec=TaskRun)
        run.issue_number = "10"
        run.id = 1
        run.pr_number = None
        run.branch_name = "feat/issue-10"

        shell_result = MagicMock()
        shell_result.success = True
        shell_result.stdout = "sova/core/foo.py\nsova/core/bar.py\n"

        with patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=shell_result) as mock_run:
            result = await _fetch_branch_files(run, "user/repo", "user", "develop")
            assert result is not None
            assert result.files == frozenset(["sova/core/foo.py", "sova/core/bar.py"])
            mock_run.assert_called_once_with("git", "diff", "--name-only", "origin/develop...feat/issue-10")

    @pytest.mark.asyncio
    async def test_returns_none_when_no_files(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.db.models import TaskRun

        run = MagicMock(spec=TaskRun)
        run.issue_number = "10"
        run.id = 1
        run.pr_number = None
        run.branch_name = "feat/issue-10"

        shell_result = MagicMock()
        shell_result.success = True
        shell_result.stdout = ""

        with patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=shell_result):
            result = await _fetch_branch_files(run, "", "", "main")
            assert result is None

    @pytest.mark.asyncio
    async def test_pr_api_failure_falls_back_to_git(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.db.models import TaskRun

        run = MagicMock(spec=TaskRun)
        run.issue_number = "10"
        run.id = 1
        run.pr_number = 100
        run.branch_name = "feat/issue-10"

        shell_result = MagicMock()
        shell_result.success = True
        shell_result.stdout = "sova/core/foo.py\n"

        with (
            patch("sova.git.pr.get_pr_files", new_callable=AsyncMock, side_effect=RuntimeError("API error")),
            patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=shell_result),
        ):
            result = await _fetch_branch_files(run, "user/repo", "user", "main")
            assert result is not None
            assert result.files == frozenset(["sova/core/foo.py"])

    @pytest.mark.asyncio
    async def test_uses_configured_base_branch(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from sova.db.models import TaskRun

        run = MagicMock(spec=TaskRun)
        run.issue_number = "10"
        run.id = 1
        run.pr_number = None
        run.branch_name = "feat/issue-10"

        shell_result = MagicMock()
        shell_result.success = True
        shell_result.stdout = "sova/core/foo.py\n"

        with patch("sova.utils.shell.run", new_callable=AsyncMock, return_value=shell_result) as mock_run:
            await _fetch_branch_files(run, "", "", "release/v2")
            mock_run.assert_called_once_with("git", "diff", "--name-only", "origin/release/v2...feat/issue-10")
