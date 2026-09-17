"""ResolveExternalReviewsStep posts the address summary the pipeline used to leave out.

The `/address-pr` command posts an `## Address Review: Round N` summary on the
PR; the autonomous address-review pipeline posted nothing, so a pipeline-
addressed PR carried no GitHub evidence that its findings were handled and the
dashboard kept routing it to "Address" (#1063).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sova.adapters.base import PRReview
from sova.config.models import ProjectConfig
from sova.core.context import ExecutionContext
from sova.core.steps.resolve_external_reviews import ResolveExternalReviewsStep, _post_address_summary

_HEAD = "892372e3512475e42638ffe7cab9a14d0734a2b5"


def _ctx(findings: list[dict], tmp_path: Path) -> ExecutionContext:
    adapter = AsyncMock()
    adapter.get_pr_reviews = AsyncMock(return_value=[])
    adapter.post_pr_review = AsyncMock()
    ctx = ExecutionContext(
        project_dir=tmp_path,
        config=ProjectConfig(github_user="xsovad06", github_repo="user/repo"),
        adapter=adapter,
        issue_number="",
        role="developer",
        pr_number=1063,
    )
    ctx.addressed_review_findings = findings
    return ctx


def _git_head_ok() -> AsyncMock:
    return AsyncMock(return_value=MagicMock(success=True, stdout=f"{_HEAD}\n"))


class TestPostAddressSummary:
    @pytest.mark.asyncio
    async def test_posts_comment_review_anchored_to_pushed_head(self, tmp_path: Path) -> None:
        ctx = _ctx([{"file": "a.py", "line": 1, "severity": 5, "description": "bug"}], tmp_path)
        with patch("sova.core.steps.resolve_external_reviews.run", new=_git_head_ok()):
            posted = await _post_address_summary(ctx)

        assert posted is True
        ctx.adapter.post_pr_review.assert_awaited_once()
        args, kwargs = ctx.adapter.post_pr_review.call_args
        assert args[0] == 1063
        assert kwargs["event"] == "COMMENT"
        assert kwargs["comments"] == []
        assert kwargs["body"].startswith(f"<!-- sova-addressed: sha={_HEAD} -->")
        assert "## Address Review: Round 1" in kwargs["body"]
        assert "`a.py:1`: bug" in kwargs["body"]

    @pytest.mark.asyncio
    async def test_round_counts_earlier_summaries(self, tmp_path: Path) -> None:
        ctx = _ctx([{"file": "a.py", "line": 1, "description": "bug"}], tmp_path)
        ctx.adapter.get_pr_reviews = AsyncMock(
            return_value=[
                PRReview("me", "COMMENTED", "<!-- sova-review: revise -->", "2026-09-17T21:00:00Z", False),
                PRReview(
                    "me",
                    "COMMENTED",
                    "<!-- sova-addressed: sha=abcdef1 -->\n## Address Review: Round 1",
                    "2026-09-17T22:00:00Z",
                    False,
                ),
            ]
        )
        with patch("sova.core.steps.resolve_external_reviews.run", new=_git_head_ok()):
            await _post_address_summary(ctx)

        assert "## Address Review: Round 2" in ctx.adapter.post_pr_review.call_args.kwargs["body"]

    @pytest.mark.asyncio
    async def test_nothing_posted_without_findings(self, tmp_path: Path) -> None:
        ctx = _ctx([], tmp_path)
        with patch("sova.core.steps.resolve_external_reviews.run", new=_git_head_ok()):
            posted = await _post_address_summary(ctx)

        assert posted is False
        ctx.adapter.post_pr_review.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_post_failure_is_non_fatal(self, tmp_path: Path) -> None:
        ctx = _ctx([{"file": "a.py", "line": 1, "description": "bug"}], tmp_path)
        ctx.adapter.post_pr_review = AsyncMock(side_effect=RuntimeError("gh exploded"))
        with patch("sova.core.steps.resolve_external_reviews.run", new=_git_head_ok()):
            posted = await _post_address_summary(ctx)

        assert posted is False

    @pytest.mark.asyncio
    async def test_unknown_head_leaves_marker_unanchored(self, tmp_path: Path) -> None:
        ctx = _ctx([{"file": "a.py", "line": 1, "description": "bug"}], tmp_path)
        failed_git = AsyncMock(return_value=MagicMock(success=False, stdout=""))
        with patch("sova.core.steps.resolve_external_reviews.run", new=failed_git):
            posted = await _post_address_summary(ctx)

        assert posted is True
        assert ctx.adapter.post_pr_review.call_args.kwargs["body"].startswith("<!-- sova-addressed -->")


class TestStepIntegration:
    @pytest.mark.asyncio
    async def test_execute_reports_summary_in_step_result(self, tmp_path: Path) -> None:
        ctx = _ctx([{"file": "a.py", "line": 1, "description": "bug"}], tmp_path)
        step = ResolveExternalReviewsStep()
        with (
            patch(
                "sova.adapters.external_reviews._fetch_coderabbit_threads",
                new=AsyncMock(return_value=MagicMock(thread_ids=[])),
            ),
            patch("sova.core.steps.resolve_external_reviews._dismiss_bot_reviews", new=AsyncMock(return_value=0)),
            patch("sova.core.steps.resolve_external_reviews.get_active_gh_user", new=AsyncMock(return_value=None)),
            patch("sova.core.steps.resolve_external_reviews.run", new=_git_head_ok()),
        ):
            result = await step.execute(ctx)

        assert result.success is True
        assert "address summary posted" in result.summary
        ctx.adapter.post_pr_review.assert_awaited_once()
