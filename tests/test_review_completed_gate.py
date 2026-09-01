"""Tests for sova.supervisor.gates.review_completed."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sova.supervisor.gates.review_completed import (
    _has_human_approval,
    _has_sova_label,
    check_review_completed_gate,
)


class TestHasSovaLabel:
    def test_approved_label(self) -> None:
        assert _has_sova_label(["sova:approved", "type: feature"]) is True

    def test_revise_label(self) -> None:
        assert _has_sova_label(["sova:revise"]) is True

    def test_block_label(self) -> None:
        assert _has_sova_label(["sova:block"]) is True

    def test_no_sova_label(self) -> None:
        assert _has_sova_label(["type: feature", "priority: high"]) is False

    def test_empty_labels(self) -> None:
        assert _has_sova_label([]) is False


class TestHasHumanApproval:
    def test_no_pr_data(self) -> None:
        assert _has_human_approval(None) is False

    def test_not_approved(self) -> None:
        assert _has_human_approval({"review_decision": "CHANGES_REQUESTED"}) is False

    def test_approved_with_human_reviewer(self) -> None:
        pr = {
            "review_decision": "APPROVED",
            "latest_reviews": [{"state": "APPROVED", "author": {"type": "User"}}],
        }
        assert _has_human_approval(pr) is True

    def test_approved_by_bot_only(self) -> None:
        pr = {
            "review_decision": "APPROVED",
            "latest_reviews": [{"state": "APPROVED", "author": {"type": "Bot"}}],
        }
        assert _has_human_approval(pr) is False

    def test_approved_no_reviews_list(self) -> None:
        pr = {"review_decision": "APPROVED", "latest_reviews": []}
        assert _has_human_approval(pr) is False

    def test_approved_with_graphql_typename_bot(self) -> None:
        pr = {
            "review_decision": "APPROVED",
            "latest_reviews": [{"state": "APPROVED", "author": {"__typename": "Bot"}}],
        }
        assert _has_human_approval(pr) is False

    def test_mixed_reviews_human_and_bot(self) -> None:
        pr = {
            "review_decision": "APPROVED",
            "latest_reviews": [
                {"state": "COMMENTED", "author": {"type": "Bot"}},
                {"state": "APPROVED", "author": {"type": "User"}},
            ],
        }
        assert _has_human_approval(pr) is True

    def test_real_gh_cli_shape_has_no_type_field(self) -> None:
        """`gh pr list --json latestReviews` only returns author.login, never type/__typename.

        CodeRabbit's real (non-App) login has no `[bot]` suffix, so bot detection
        must fall back to the known-authors allowlist, not a type discriminator.
        """
        pr = {
            "review_decision": "APPROVED",
            "latest_reviews": [{"state": "APPROVED", "author": {"login": "coderabbitai"}}],
        }
        assert _has_human_approval(pr) is False

    def test_real_gh_cli_shape_bot_suffix_login(self) -> None:
        pr = {
            "review_decision": "APPROVED",
            "latest_reviews": [{"state": "APPROVED", "author": {"login": "dependabot[bot]"}}],
        }
        assert _has_human_approval(pr) is False

    def test_real_gh_cli_shape_human_login(self) -> None:
        pr = {
            "review_decision": "APPROVED",
            "latest_reviews": [{"state": "APPROVED", "author": {"login": "xsovad06"}}],
        }
        assert _has_human_approval(pr) is True


class TestCheckReviewCompletedGate:
    @pytest.mark.asyncio
    async def test_passes_with_sova_label(self) -> None:
        result = await check_review_completed_gate(
            42,
            labels=["sova:approved"],
            pr_number=100,
            project_dir=Path("/tmp/test"),
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_passes_with_reviewer_run(self) -> None:
        verdict = {
            "has_sova_review": True,
            "verdict": "approve",
            "finding_count": 0,
            "reviewed_at": "2026-01-01",
            "run_status": "done",
        }
        with patch(
            "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
            new_callable=AsyncMock,
            return_value=verdict,
        ):
            result = await check_review_completed_gate(
                42,
                labels=[],
                pr_number=100,
                project_dir=Path("/tmp/test"),
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_blocks_with_failed_reviewer_run(self) -> None:
        """A failed run may still carry handoff_json, but it never finished the review."""
        verdict = {
            "has_sova_review": True,
            "verdict": "revise",
            "finding_count": 1,
            "reviewed_at": "2026-01-01",
            "run_status": "failed",
        }
        with patch(
            "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
            new_callable=AsyncMock,
            return_value=verdict,
        ):
            result = await check_review_completed_gate(
                42,
                labels=[],
                pr_number=100,
                project_dir=Path("/tmp/test"),
            )
        assert result is not None
        assert result.gate == "review_completed"

    @pytest.mark.asyncio
    async def test_blocks_with_interrupted_reviewer_run(self) -> None:
        verdict = {
            "has_sova_review": True,
            "verdict": "approve",
            "finding_count": 0,
            "reviewed_at": "2026-01-01",
            "run_status": "interrupted",
        }
        with patch(
            "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
            new_callable=AsyncMock,
            return_value=verdict,
        ):
            result = await check_review_completed_gate(
                42,
                labels=[],
                pr_number=100,
                project_dir=Path("/tmp/test"),
            )
        assert result is not None
        assert result.gate == "review_completed"

    @pytest.mark.asyncio
    async def test_blocks_with_unresolved_threads_despite_sova_label(self) -> None:
        """Unresolved review threads block integration even with an approved label."""
        pr_data = {"thread_total": 3, "thread_resolved": 1}
        result = await check_review_completed_gate(
            42,
            labels=["sova:approved"],
            pr_number=100,
            project_dir=Path("/tmp/test"),
            pr_data=pr_data,
        )
        assert result is not None
        assert result.gate == "review_completed"
        assert "unresolved" in result.detail.lower()

    @pytest.mark.asyncio
    async def test_passes_with_all_threads_resolved(self) -> None:
        pr_data = {"thread_total": 3, "thread_resolved": 3}
        result = await check_review_completed_gate(
            42,
            labels=["sova:approved"],
            pr_number=100,
            project_dir=Path("/tmp/test"),
            pr_data=pr_data,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_passes_with_human_approval(self) -> None:
        verdict = {"has_sova_review": False, "verdict": None}
        pr_data = {
            "review_decision": "APPROVED",
            "latest_reviews": [{"state": "APPROVED", "author": {"type": "User"}}],
        }
        with patch(
            "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
            new_callable=AsyncMock,
            return_value=verdict,
        ):
            result = await check_review_completed_gate(
                42,
                labels=[],
                pr_number=100,
                project_dir=Path("/tmp/test"),
                pr_data=pr_data,
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_blocks_with_no_review(self) -> None:
        verdict = {"has_sova_review": False, "verdict": None}
        with patch(
            "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
            new_callable=AsyncMock,
            return_value=verdict,
        ):
            result = await check_review_completed_gate(
                42,
                labels=[],
                pr_number=100,
                project_dir=Path("/tmp/test"),
            )
        assert result is not None
        assert result.gate == "review_completed"
        assert "#42" in result.detail

    @pytest.mark.asyncio
    async def test_blocks_with_bot_only_approval(self) -> None:
        verdict = {"has_sova_review": False, "verdict": None}
        pr_data = {
            "review_decision": "APPROVED",
            "latest_reviews": [{"state": "APPROVED", "author": {"type": "Bot"}}],
        }
        with patch(
            "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
            new_callable=AsyncMock,
            return_value=verdict,
        ):
            result = await check_review_completed_gate(
                42,
                labels=[],
                pr_number=100,
                project_dir=Path("/tmp/test"),
                pr_data=pr_data,
            )
        assert result is not None
        assert result.gate == "review_completed"

    @pytest.mark.asyncio
    async def test_db_error_falls_through(self) -> None:
        with patch(
            "sova.dashboard.services.agent_recovery.get_sova_review_verdict",
            new_callable=AsyncMock,
            side_effect=Exception("DB error"),
        ):
            result = await check_review_completed_gate(
                42,
                labels=[],
                pr_number=100,
                project_dir=Path("/tmp/test"),
            )
        assert result is not None
        assert result.gate == "review_completed"
