"""PRs API route and PR helpers.

Split from tests/test_dashboard.py; see docs/testing-guidelines.md, "Test File Layout".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import AsyncClient

# Uses the shared per-worker in-memory DB, emptied before each test
# (see ``tests/conftest.py:reset_db_rows``). A test here that leaves a
# background task touching the DB when its event loop closes (the real
# ``start_agent()`` calls in ``TestDuplicateAgentPrevention``) can invalidate the
# shared connection; the next test's sweep recreates the schema.
pytestmark = pytest.mark.usefixtures("reset_db_rows")


class TestPrsAPI:
    async def test_open_prs_returns_list(self, client: AsyncClient, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        monkeypatch.setattr(
            "sova.dashboard.routers.prs.list_open_prs_with_state",
            AsyncMock(return_value=[{"number": 1, "title": "Test", "computed_state": "draft"}]),
        )
        resp = await client.get("/api/prs/open")
        assert resp.status_code == 200
        data = resp.json()
        assert "prs" in data
        assert len(data["prs"]) == 1
        assert data["prs"][0]["number"] == 1

    async def test_open_prs_empty(self, client: AsyncClient, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        monkeypatch.setattr(
            "sova.dashboard.routers.prs.list_open_prs_with_state",
            AsyncMock(return_value=[]),
        )
        resp = await client.get("/api/prs/open")
        assert resp.status_code == 200
        assert resp.json()["prs"] == []


class TestSummarizeCi:
    """Tests for _summarize_ci: CI rollup aggregation."""

    @pytest.mark.parametrize(
        ("rollup", "expected"),
        [
            pytest.param(None, "none", id="none-rollup"),
            pytest.param([], "none", id="empty-rollup"),
            pytest.param(
                [
                    {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
                    {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
                ],
                "passed",
                id="all-success",
            ),
            # CANCELLED runs from a superseded push must not poison a later successful run.
            pytest.param(
                [
                    {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "CANCELLED"},
                    {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "CANCELLED"},
                    {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
                    {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
                ],
                "passed",
                id="cancelled-plus-success",
            ),
            # CANCELLED without SUCCESS returns passed (via skipped-only path), not failed.
            pytest.param(
                [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "CANCELLED"}],
                "passed",
                id="cancelled-alone",
            ),
            pytest.param(
                [
                    {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"},
                    {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
                ],
                "failed",
                id="failure-plus-success",
            ),
            # A real failure dominates over cancelled runs.
            pytest.param(
                [
                    {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "CANCELLED"},
                    {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"},
                ],
                "failed",
                id="cancelled-plus-failure",
            ),
            pytest.param(
                [{"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": None}],
                "pending",
                id="pending-check",
            ),
            pytest.param([{"__typename": "StatusContext", "state": "SUCCESS"}], "passed", id="status-context-success"),
            pytest.param([{"__typename": "StatusContext", "state": "FAILURE"}], "failed", id="status-context-failure"),
        ],
    )
    def test_summarizes_rollup(self, rollup: list[dict[str, object]] | None, expected: str) -> None:
        from sova.work.pr_state import _summarize_ci

        assert _summarize_ci(rollup) == expected


class TestExtractLinkedIssue:
    """Tests for _extract_linked_issue PR-vs-issue disambiguation."""

    def test_prefers_closing_issues_references(self) -> None:
        from sova.work.pr_state import _extract_linked_issue

        raw = {"closingIssuesReferences": [{"number": 42}], "body": "Closes #99"}
        assert _extract_linked_issue(raw) == 42

    def test_falls_back_to_body_parsing(self) -> None:
        from sova.work.pr_state import _extract_linked_issue

        raw = {"closingIssuesReferences": [], "body": "Fixes #55"}
        assert _extract_linked_issue(raw) == 55

    def test_returns_none_when_no_link(self) -> None:
        from sova.work.pr_state import _extract_linked_issue

        raw = {"closingIssuesReferences": [], "body": "No issue link"}
        assert _extract_linked_issue(raw) is None

    def test_handles_missing_fields(self) -> None:
        from sova.work.pr_state import _extract_linked_issue

        assert _extract_linked_issue({}) is None


# ---------------------------------------------------------------------------
# _resolve_issue_from_pr
# ---------------------------------------------------------------------------


class TestResolveIssueFromPr:
    """Tests for _resolve_issue_from_pr in agent_lifecycle."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("pr_number", "pr_body", "branch", "refs", "is_issue", "expected"),
        [
            pytest.param(99, "## Summary\n\nCloses #42\n", "", [], True, "42", id="closes-issue"),
            pytest.param(99, "Fixes #123", "", [], True, "123", id="fixes-issue"),
            pytest.param(99, "resolves #77", "", [], True, "77", id="resolves-case-insensitive"),
            # When 'Closes #N' references a PR (not an issue), return empty.
            pytest.param(341, "Closes #339", "", [], False, "", id="skips-pr-reference"),
            pytest.param(
                99,
                "## Issue\n\nCloses #1354\n\n## Test plan\n- `closes #12`",
                "feat/issue-1354",
                [],
                True,
                "1354",
                id="run-3003-code-span-keyword-is-never-read",
            ),
            pytest.param(
                99, "Tested with `Closes #12`", "", [], True, "", id="only-keyword-in-code-span-resolves-nothing"
            ),
            pytest.param(
                99,
                "## Issue\n\nPart of #1213\n\n- Closes #42",
                "feat/issue-1213",
                [{"number": 42}],
                True,
                "1213",
                id="pr-1325-shape-branch-and-section-outrank-lone-reference",
            ),
            pytest.param(
                99, "", "feat/issue-7", [{"number": 42}, {}], True, "7", id="branch-outranks-listed-reference"
            ),
            pytest.param(99, "", "feat/issue-7", [], True, "7", id="empty-body-falls-back-to-branch-like-reader"),
            pytest.param(99, "", "feat/issue-7", [], False, "", id="branch-fallback-still-rejects-a-pr-number"),
            pytest.param(99, "", "feat/PROJ-42-thing", [], True, "", id="jira-branch-is-not-a-github-issue"),
            pytest.param(99, "", "feat/issue-7", [], True, "7", id="github-branch-fallback-is-kept"),
            pytest.param(99, "", "feat/no-number", [], True, "", id="no-body-and-no-branch-number-resolves-nothing"),
        ],
    )
    async def test_extracts_linked_issue(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        pr_number: int,
        pr_body: str,
        branch: str,
        refs: list[dict],
        is_issue: bool,
        expected: str,
    ) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_lifecycle import _resolve_issue_from_pr

        mock_result = AsyncMock()
        mock_result.return_value.success = True
        mock_result.return_value.stdout = json.dumps(
            {"body": pr_body, "headRefName": branch, "closingIssuesReferences": refs}
        )
        monkeypatch.setattr("sova.dashboard.services.agent_context.run_shell", mock_result)
        monkeypatch.setattr("sova.dashboard.services.agent_context._is_issue", AsyncMock(return_value=is_issue))

        result = await _resolve_issue_from_pr(pr_number, tmp_path)
        assert result == expected

    @pytest.mark.asyncio
    async def test_overridden_reference_is_not_a_fallback_candidate(self, tmp_path, monkeypatch) -> None:
        """A failed _is_issue on the branch's issue must not file the run under the stray listed one."""
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_lifecycle import _resolve_issue_from_pr

        mock_result = AsyncMock()
        mock_result.return_value.success = True
        mock_result.return_value.stdout = json.dumps(
            {
                "body": "## Issue\n\nPart of #1213",
                "headRefName": "feat/issue-1213",
                "closingIssuesReferences": [{"number": 42}],
            }
        )
        monkeypatch.setattr("sova.dashboard.services.agent_context.run_shell", mock_result)
        monkeypatch.setattr(
            "sova.dashboard.services.agent_context._is_issue",
            AsyncMock(side_effect=lambda number, _dir: number == "42"),
        )

        assert await _resolve_issue_from_pr(99, tmp_path) == ""

    @pytest.mark.asyncio
    async def test_listed_primary_is_kept_when_is_issue_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GitHub vouches for a listed primary, so a failed _is_issue never falls back to another listed ref."""
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_lifecycle import _resolve_issue_from_pr

        mock_result = AsyncMock()
        mock_result.return_value.success = True
        mock_result.return_value.stdout = json.dumps(
            {
                "body": "## Issue\n\nCloses #1354",
                "headRefName": "feat/issue-1354",
                "closingIssuesReferences": [{"number": 12}, {"number": 1354}],
            }
        )
        monkeypatch.setattr("sova.dashboard.services.agent_context.run_shell", mock_result)
        is_issue = AsyncMock(side_effect=lambda number, _dir: number == "12")
        monkeypatch.setattr("sova.dashboard.services.agent_context._is_issue", is_issue)

        assert await _resolve_issue_from_pr(99, tmp_path) == "1354"
        is_issue.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("title", ["[PROJ-42] add thing", "feat: PROJ-42 add thing"])
    async def test_jira_key_in_title_is_not_a_github_issue(self, tmp_path, monkeypatch, title: str) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_lifecycle import _resolve_issue_from_pr

        mock_result = AsyncMock()
        mock_result.return_value.success = True
        mock_result.return_value.stdout = json.dumps(
            {"body": "", "title": title, "headRefName": "feat/PROJ-42-thing", "closingIssuesReferences": []}
        )
        is_issue = AsyncMock(return_value=True)
        monkeypatch.setattr("sova.dashboard.services.agent_context.run_shell", mock_result)
        monkeypatch.setattr("sova.dashboard.services.agent_context._is_issue", is_issue)

        assert await _resolve_issue_from_pr(99, tmp_path) == ""
        is_issue.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_pr_ref_returns_second_valid_issue(self, tmp_path, monkeypatch) -> None:
        """When the first reference is a PR, evaluate subsequent matches and return the first valid issue."""
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_lifecycle import _resolve_issue_from_pr

        mock_result = AsyncMock()
        mock_result.return_value.success = True
        mock_result.return_value.stdout = json.dumps({"body": "Closes #339 and Fixes #42"})
        monkeypatch.setattr("sova.dashboard.services.agent_context.run_shell", mock_result)
        is_issue = AsyncMock(side_effect=lambda n, _d: n == "42")
        monkeypatch.setattr("sova.dashboard.services.agent_context._is_issue", is_issue)

        result = await _resolve_issue_from_pr(341, tmp_path)
        assert result == "42"

    @pytest.mark.asyncio
    async def test_falls_back_to_title_when_body_and_branch_name_no_issue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_lifecycle import _resolve_issue_from_pr

        mock_result = AsyncMock()
        mock_result.return_value.success = True
        mock_result.return_value.stdout = json.dumps(
            {"body": "", "title": "fix(core): tidy (#31)", "headRefName": "feat/tidy", "closingIssuesReferences": []}
        )
        monkeypatch.setattr("sova.dashboard.services.agent_context.run_shell", mock_result)
        monkeypatch.setattr("sova.dashboard.services.agent_context._is_issue", AsyncMock(return_value=True))

        assert await _resolve_issue_from_pr(99, tmp_path) == "31"

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_match(self, tmp_path, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_lifecycle import _resolve_issue_from_pr

        mock_result = AsyncMock()
        mock_result.return_value.success = True
        mock_result.return_value.stdout = json.dumps({"body": "Just a regular PR body"})
        monkeypatch.setattr("sova.dashboard.services.agent_context.run_shell", mock_result)

        result = await _resolve_issue_from_pr(99, tmp_path)
        assert result == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stdout", ["not json", "[1, 2]", "null"])
    async def test_returns_empty_on_malformed_payload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: str
    ) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_lifecycle import _resolve_issue_from_pr

        mock_result = AsyncMock()
        mock_result.return_value.success = True
        mock_result.return_value.stdout = stdout
        monkeypatch.setattr("sova.dashboard.services.agent_context.run_shell", mock_result)

        assert await _resolve_issue_from_pr(99, tmp_path) == ""

    @pytest.mark.asyncio
    async def test_returns_empty_on_failure(self, tmp_path, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_lifecycle import _resolve_issue_from_pr

        mock_result = AsyncMock()
        mock_result.return_value.success = False
        mock_result.return_value.stdout = ""
        monkeypatch.setattr("sova.dashboard.services.agent_context.run_shell", mock_result)

        result = await _resolve_issue_from_pr(99, tmp_path)
        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_empty_on_exception(self, tmp_path, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from sova.dashboard.services.agent_lifecycle import _resolve_issue_from_pr

        monkeypatch.setattr(
            "sova.dashboard.services.agent_context.run_shell",
            AsyncMock(side_effect=OSError("nope")),
        )

        result = await _resolve_issue_from_pr(99, tmp_path)
        assert result == ""
