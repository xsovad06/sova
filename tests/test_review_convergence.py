"""The review loop converges: later rounds block only on serious findings, a review gap re-runs once,
an unverifiable blocking finding is advisory, and a review that reviewed nothing is not a round."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from sova.adapters.base import Task, TaskState
from sova.config.models import ProjectConfig, ReviewConfig
from sova.core.context import ExecutionContext
from sova.llm.models import LLMResult
from sova.roles._review_comments import (
    EVIDENCE_PLACEHOLDER,
    UNVERIFIED_EVIDENCE_PREFIX,
    ReviewFinding,
    ReviewResult,
    _build_review_prompt,
    verify_evidence,
)
from sova.roles.reviewer import ReviewerRole, _review_next_action

# Captured at import, before tests/conftest.py's autouse stand-in replaces the method for the run.
_REAL_IS_REREVIEW = ReviewerRole._is_rereview

_ADDRESSED = [{"source": "sova-review", "file": "a.py", "description": "fixed"}]


def _ctx(tmp_path: Path, pr_number: int | None = 7) -> ExecutionContext:
    adapter = AsyncMock()
    adapter.get_state.return_value = TaskState.IN_REVIEW
    adapter.get_task.return_value = Task(
        id="42", title="Test issue", body="Some description", state=TaskState.IN_REVIEW
    )
    return ExecutionContext(
        project_dir=tmp_path,
        config=ProjectConfig(),
        adapter=adapter,
        issue_number="42",
        role="reviewer",
        pr_number=pr_number,
    )


class TestReviewConfigReviseAt:
    def test_rereview_uses_the_higher_threshold(self) -> None:
        cfg = ReviewConfig()
        assert (cfg.revise_at(rereview=False), cfg.revise_at(rereview=True)) == (3, 5)

    def test_rereview_is_never_stricter_than_the_first_round(self) -> None:
        cfg = ReviewConfig(revise_severity=6, rereview_revise_severity=4)
        assert cfg.revise_at(rereview=True) == 6

    def test_rereview_threshold_above_block_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="rereview_revise_severity"):
            ReviewConfig(rereview_revise_severity=8, block_severity=7)

    def test_omitted_rereview_threshold_keeps_a_low_block_config_loadable(self) -> None:
        """A config valid before the field existed still loads, and its re-review stays below BLOCK."""
        cfg = ReviewConfig(revise_severity=3, block_severity=4)
        assert cfg.revise_at(rereview=True) == 4
        assert ReviewConfig.model_validate(cfg.model_dump()).revise_at(rereview=True) == 4


class TestIsRereview:
    @pytest.mark.parametrize(
        ("cycles", "addressed", "expected"),
        [(1, [], True), (0, [], False), (0, _ADDRESSED, False)],
        ids=["fix-marker", "first-round", "issue-keyed-findings-from-another-pr"],
    )
    async def test_pr_fix_markers_decide(self, tmp_path: Path, cycles: int, addressed: list, expected: bool) -> None:
        """The PR's own markers win over the handoff, whose DB fallback is keyed by issue, not PR."""
        history = {"address_cycles": cycles}
        with patch.object(ReviewerRole, "_review_history", new=AsyncMock(return_value=history)):
            assert await _REAL_IS_REREVIEW(ReviewerRole(), _ctx(tmp_path), addressed) is expected

    @pytest.mark.parametrize("failure", [AsyncMock(return_value=None), AsyncMock(side_effect=RuntimeError("gh"))])
    @pytest.mark.parametrize("addressed", [[], _ADDRESSED])
    async def test_unreadable_history_falls_back_to_addressed_findings(
        self, tmp_path: Path, failure: AsyncMock, addressed: list
    ) -> None:
        with patch.object(ReviewerRole, "_review_history", new=failure):
            assert await _REAL_IS_REREVIEW(ReviewerRole(), _ctx(tmp_path), addressed) is bool(addressed)

    async def test_no_pr_number_skips_the_lookup(self, tmp_path: Path) -> None:
        lookup = AsyncMock()
        with patch.object(ReviewerRole, "_review_history", new=lookup):
            assert await _REAL_IS_REREVIEW(ReviewerRole(), _ctx(tmp_path, pr_number=None), _ADDRESSED) is True
        lookup.assert_not_awaited()


class TestRereviewThreshold:
    async def _review(self, tmp_path: Path, *, rereview: bool) -> tuple[object, str, str]:
        """Run one review returning a single medium (4) finding; return the run's ctx, prompt and next action."""
        ctx = _ctx(tmp_path, pr_number=99)
        shared_config = ctx.config
        finding = {
            "file": "a.py",
            "line": 1,
            "severity": 4,
            "category": "testing",
            "description": "d",
            "suggestion": "f",
        }
        llm_result = LLMResult(
            text=json.dumps({"findings": [finding], "summary": "s"}), model="m", cost_usd=Decimal("0")
        )
        with (
            patch.object(ReviewerRole, "_is_rereview", new=AsyncMock(return_value=rereview)),
            patch(
                "sova.roles.reviewer.get_pr_branch_and_head_sha",
                new_callable=AsyncMock,
                return_value=("feat/issue-42", "abc123"),
            ),
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value="diff"),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["a.py"]),
            patch("sova.roles.reviewer.get_pr_head_sha", new_callable=AsyncMock, return_value="abc123"),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, return_value=llm_result) as invoke,
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock),
            patch("sova.roles.reviewer.write_handoff_file", new_callable=MagicMock) as handoff,
        ):
            result = await ReviewerRole().execute(ctx)
        assert result.success
        assert shared_config.review.revise_severity == 3, "the shared config must not be mutated"
        return ctx, invoke.call_args.args[0], handoff.call_args.args[1].details["next_action"]

    async def test_first_round_blocks_on_a_medium_finding(self, tmp_path: Path) -> None:
        ctx, prompt, next_action = await self._review(tmp_path, rereview=False)
        assert ctx.config.review.revise_severity == 3
        assert "Findings at severity 3 or above block" in prompt
        assert next_action == "address_review"

    async def test_panel_review_keeps_its_threshold_after_a_fix_cycle(self, tmp_path: Path) -> None:
        """The re-review adjustment is the single reviewer's: a panel run never raises revise_severity."""
        ctx = _ctx(tmp_path, pr_number=99)
        ctx.config.review.panel.enabled = True
        panel = ReviewResult(summary="s")
        with (
            patch.object(ReviewerRole, "_is_rereview", new=AsyncMock(return_value=True)) as decided,
            patch.object(ReviewerRole, "_discover_pr", new=AsyncMock(return_value=("", "abc123"))),
            patch.object(ReviewerRole, "_run_review", new=AsyncMock(return_value=panel)) as run_review,
            patch.object(ReviewerRole, "_record_review_costs", new=AsyncMock()),
            patch.object(ReviewerRole, "_post_review", new=AsyncMock(return_value=True)),
            patch.object(ReviewerRole, "_write_verdict_label", new=AsyncMock()),
            patch.object(ReviewerRole, "_write_handoff", new=AsyncMock()),
            patch.object(ReviewerRole, "_incomplete_before", new=AsyncMock(return_value=True)),
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value="diff"),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["a.py"]),
        ):
            await ReviewerRole().execute(ctx)
        decided.assert_not_awaited()
        assert ctx.config.review.revise_severity == 3
        assert run_review.call_args.kwargs["rereview"] is False

    async def test_rereview_records_a_medium_finding_as_advisory(self, tmp_path: Path) -> None:
        ctx, prompt, next_action = await self._review(tmp_path, rereview=True)
        assert ctx.config.review.revise_severity == 5
        assert "Findings at severity 5 or above block" in prompt
        assert next_action != "address_review"


class TestRereviewPrompt:
    """The later-round instruction follows the PR's re-review decision, not whether findings were retained."""

    TASK = Task(id="1", title="T", body="b", state=TaskState.BACKLOG)
    EXTERNAL = [{"source": "sonarcloud", "severity": "MAJOR", "file_path": "a.py", "message": "m"}]

    def test_first_review_with_only_external_findings_is_not_called_a_later_round(self) -> None:
        prompt = _build_review_prompt(self.TASK, "diff", ["a.py"], addressed_findings=self.EXTERNAL)
        assert "Already Addressed in Earlier Rounds" in prompt
        assert "not a fresh review" not in prompt

    def test_rereview_with_findings_says_earlier_rounds_covered_the_diff(self) -> None:
        prompt = _build_review_prompt(self.TASK, "diff", ["a.py"], addressed_findings=self.EXTERNAL, rereview=True)
        assert "not a fresh review" in prompt

    def test_rereview_without_retained_findings_still_gets_the_instruction(self) -> None:
        prompt = _build_review_prompt(self.TASK, "diff", ["a.py"], addressed_findings=[], rereview=True)
        assert "not a fresh review" in prompt
        assert "Already Addressed" not in prompt

    def test_first_review_without_findings_has_no_section(self) -> None:
        prompt = _build_review_prompt(self.TASK, "diff", ["a.py"], addressed_findings=[])
        assert "not a fresh review" not in prompt

    def test_first_review_keeps_the_original_checklist_floors(self) -> None:
        prompt = _build_review_prompt(self.TASK, "diff", ["a.py"], addressed_findings=self.EXTERNAL)
        assert "**Testing gaps** (3-6)" in prompt
        assert "**Performance** (3-6)" in prompt
        assert "**Design** (3-5)" in prompt

    def test_rereview_lowers_the_checklist_floors(self) -> None:
        prompt = _build_review_prompt(self.TASK, "diff", ["a.py"], addressed_findings=[], rereview=True)
        assert "**Testing gaps** (2-5)" in prompt
        assert "**Performance** (2-6)" in prompt
        assert "**Design** (2-4)" in prompt

    def test_rereview_rule_follows_the_rereview_flag_not_the_retained_findings(self) -> None:
        without_findings = _build_review_prompt(self.TASK, "diff", ["a.py"], addressed_findings=[], rereview=True)
        external_first_review = _build_review_prompt(self.TASK, "diff", ["a.py"], addressed_findings=self.EXTERNAL)
        assert "On a re-review, raise a finding on unchanged code" in without_findings
        assert "On a re-review, raise a finding on unchanged code" not in external_first_review


class TestIncompleteReviewRetry:
    """A partly reviewed diff re-runs its review once per head before any fix cycle starts."""

    def _partial(self) -> tuple[ReviewResult, list[ReviewFinding]]:
        review = ReviewResult(findings=[ReviewFinding(file="a.py", severity=8, category="bug", description="b")])
        review.mark_unreviewed(["b.py"], "the model call timed out")
        return review, list(review.findings)

    def test_pending_rerun_routes_a_gap_ahead_of_blocking_findings(self) -> None:
        review, blocking = self._partial()
        assert _review_next_action(review, blocking) == "address_review"
        review.rerun_pending = True
        assert _review_next_action(review, blocking) == "review_incomplete"

    @pytest.mark.parametrize(
        ("rerun_pending", "expected"),
        [(True, ("review_incomplete", "rerun_review", True)), (False, ("address_review", "address_review", True))],
        ids=["first-gap-reruns-itself", "second-gap-at-same-head-addresses"],
    )
    async def test_handoff_follows_the_rerun_decision(
        self, tmp_path: Path, rerun_pending: bool, expected: tuple[str, str, bool]
    ) -> None:
        review, _ = self._partial()
        review.rerun_pending = rerun_pending
        with (
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock),
            patch("sova.roles.reviewer.write_handoff_file") as handoff_file,
        ):
            await ReviewerRole()._write_handoff(_ctx(tmp_path, pr_number=99), review, review_head_sha="abc123")

        handoff = handoff_file.call_args.args[1]
        action = handoff.next_actions[0]
        assert (handoff.details["next_action"], action.id, action.auto_execute) == expected

    def test_pending_rerun_parks_the_pr_even_with_real_blockers(self) -> None:
        """The incomplete marker makes the dashboard and supervisor wait for the re-run, not start a fix cycle."""
        from sova.roles._review_format import format_review_body
        from sova.utils.review_markers import SOVA_INCOMPLETE_REVIEW_MARKER

        blocker = [{"file": "a.py", "line": 1, "severity": 8, "category": "bug", "description": "b"}]
        assert SOVA_INCOMPLETE_REVIEW_MARKER in format_review_body(blocker, "s", rerun_pending=True)
        assert SOVA_INCOMPLETE_REVIEW_MARKER not in format_review_body(blocker, "s")

    @pytest.mark.parametrize(("seen_before", "expected"), [(False, True), (True, False)])
    async def test_run_decides_once_before_posting(self, tmp_path: Path, seen_before: bool, expected: bool) -> None:
        """A chunk that fails leaves a gap; whether it re-runs depends on an earlier gap at the same head."""
        ctx = _ctx(tmp_path, pr_number=99)
        posted: list[bool] = []

        async def _post(self: ReviewerRole, ctx: object, review: ReviewResult, diff: str, sha: object = None) -> bool:
            posted.append(review.rerun_pending)
            return True

        with (
            patch.object(ReviewerRole, "_incomplete_before", new=AsyncMock(return_value=seen_before)) as before,
            patch.object(ReviewerRole, "_post_review", new=_post),
            patch(
                "sova.roles.reviewer.get_pr_branch_and_head_sha",
                new_callable=AsyncMock,
                return_value=("feat/issue-42", "abc123"),
            ),
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value="diff --git a/a.py\n+x"),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["a.py"]),
            patch("sova.roles.reviewer.get_pr_head_sha", new_callable=AsyncMock, return_value="abc123"),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, side_effect=TimeoutError("hung")),
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock),
            patch("sova.roles.reviewer.write_handoff_file", new_callable=MagicMock),
        ):
            await ReviewerRole().execute(ctx)

        assert posted == [expected]
        before.assert_awaited_once_with(ctx, "abc123")

    async def test_panel_review_never_reruns_an_incomplete_result_itself(self, tmp_path: Path) -> None:
        """Panel routing is unchanged: an incomplete panel result goes to the fix step, not an automatic rerun."""
        ctx = _ctx(tmp_path, pr_number=99)
        ctx.config.review.panel.enabled = True
        panel = ReviewResult(summary="s", unreviewed_files=["a.py"])
        posted: list[bool] = []

        async def _post(self: ReviewerRole, ctx: object, review: ReviewResult, diff: str, sha: object = None) -> bool:
            posted.append(review.rerun_pending)
            return True

        with (
            patch.object(ReviewerRole, "_discover_pr", new=AsyncMock(return_value=("", "abc123"))),
            patch.object(ReviewerRole, "_run_review", new=AsyncMock(return_value=panel)),
            patch.object(ReviewerRole, "_record_review_costs", new=AsyncMock()),
            patch.object(ReviewerRole, "_post_review", new=_post),
            patch.object(ReviewerRole, "_write_verdict_label", new=AsyncMock()),
            patch.object(ReviewerRole, "_write_handoff", new=AsyncMock()),
            patch.object(ReviewerRole, "_incomplete_before", new=AsyncMock(return_value=False)),
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value="diff"),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["a.py"]),
        ):
            await ReviewerRole().execute(ctx)

        assert posted == [False]


@pytest.mark.usefixtures("reset_db_rows")
class TestIncompleteBefore:
    async def _add_review_run(self, handoff: dict, pr_number: int = 99) -> None:
        from sova.db.models import TaskRun
        from sova.db.session import get_session

        async with await get_session() as session:
            session.add(
                TaskRun(issue_number="42", role="reviewer", status="done", pr_number=pr_number, handoff_json=handoff)
            )
            await session.commit()

    @pytest.mark.parametrize(
        "handoff",
        [
            {"next_action": "address_review", "metadata": {"review_head_sha": "abc123", "review_incomplete": True}},
            {"next_action": "review_incomplete", "review_head_sha": "abc123"},
        ],
        ids=["agent-handoff-shape", "dashboard-details-shape"],
    )
    async def test_earlier_gap_at_the_same_head_is_found(self, tmp_path: Path, handoff: dict) -> None:
        await self._add_review_run(handoff)
        role, ctx = ReviewerRole(), _ctx(tmp_path, pr_number=99)
        assert await role._incomplete_before(ctx, "abc123") is True
        assert await role._incomplete_before(ctx, "def456") is False

    async def test_complete_reviews_and_other_prs_do_not_count(self, tmp_path: Path) -> None:
        await self._add_review_run({"next_action": "address_review", "metadata": {"review_head_sha": "abc123"}})
        await self._add_review_run({"next_action": "review_incomplete", "review_head_sha": "abc123"}, pr_number=5)
        assert await ReviewerRole()._incomplete_before(_ctx(tmp_path, pr_number=99), "abc123") is False

    async def test_unknown_head_never_reruns(self, tmp_path: Path) -> None:
        """Without a head there is nothing to key the once-per-head rule on, so a rerun could loop."""
        assert await ReviewerRole()._incomplete_before(_ctx(tmp_path, pr_number=99), None) is True


class TestSpecFreshnessNote:
    async def test_note_is_appended_so_a_total_failure_still_reads_as_one(self, tmp_path: Path) -> None:
        """The failure marker stays at the start of the summary, where reviewed_nothing() reads it."""
        from sova.utils.review_markers import reviewed_nothing

        ctx = _ctx(tmp_path, pr_number=99)
        ctx.config.review.panel.enabled = True
        total_failure = ReviewResult(unreviewed_files=["a.py"])
        total_failure.summary = total_failure.unavailable_summary()
        task = Task(id="42", title="t", body="b", state=TaskState.IN_REVIEW)
        with (
            patch.object(ReviewerRole, "_load_spec_sections", return_value={"Staleness Note": "spec is old"}),
            patch.object(ReviewerRole, "_run_panel_review", new=AsyncMock(return_value=total_failure)),
        ):
            review = await ReviewerRole()._run_review(ctx, task, "diff", ["a.py"])

        assert review.summary.endswith("[Spec freshness: spec is old]")
        assert reviewed_nothing(review.summary) is True


class TestReviewedNothing:
    def test_only_a_wholly_unavailable_review_reviewed_nothing(self) -> None:
        """The predicate reads the summaries unavailable_summary() writes, so the two cannot drift apart."""
        from sova.utils.review_markers import reviewed_nothing

        review = ReviewResult()
        review.mark_unreviewed(["a.py"], "the model call timed out")
        assert reviewed_nothing(review.unavailable_summary()) is True
        assert reviewed_nothing(review.unavailable_summary(partial=True)) is False
        assert reviewed_nothing("Routing matches the spec.") is False


class TestVerifyEvidence:
    DIFF = "## Diff\n```\n+    if user is None:\n+        return cache[key]\n```"

    def _finding(self, severity: int = 7, evidence: str = "") -> ReviewFinding:
        return ReviewFinding(file="a.py", severity=severity, category="bug", description="d", evidence=evidence)

    @pytest.mark.parametrize("evidence", ["return cache[key]", "+        return   cache[key]", "if user is None:"])
    def test_quote_from_the_diff_keeps_the_finding_blocking(self, evidence: str) -> None:
        """Indentation, diff markers and spacing differ between the quote and the diff; the code matches."""
        [finding] = verify_evidence([self._finding(evidence=evidence)], self.DIFF, revise_at=3)
        assert (finding.severity, finding.description) == (7, "d")

    def test_quote_not_shown_makes_the_finding_advisory(self) -> None:
        [finding] = verify_evidence([self._finding(evidence="return session.refresh(token)")], self.DIFF, revise_at=5)
        assert finding.severity == 4
        assert finding.description.startswith(UNVERIFIED_EVIDENCE_PREFIX)

    def test_unsupported_finding_at_threshold_one_cannot_block(self) -> None:
        """No severity is below 1, so the finding is dropped rather than left to start a fix round."""
        kept = verify_evidence(
            [self._finding(severity=7, evidence="return session.refresh(token)"), self._finding(evidence="")],
            self.DIFF,
            revise_at=1,
        )
        assert [f.evidence for f in kept] == [""]

    @pytest.mark.parametrize(
        "finding_kwargs",
        [{"evidence": ""}, {"evidence": "return"}, {"severity": 2, "evidence": "return session.refresh(token)"}],
        ids=["no-evidence", "short-quote-present", "already-advisory"],
    )
    def test_findings_it_cannot_judge_are_left_alone(self, finding_kwargs: dict) -> None:
        original = self._finding(**finding_kwargs)
        [finding] = verify_evidence([self._finding(**finding_kwargs)], self.DIFF, revise_at=3)
        assert (finding.severity, finding.description) == (original.severity, original.description)

    def test_short_quote_absent_from_the_diff_is_downgraded_too(self) -> None:
        """Absence needs no minimum length: a short fabricated quote is as unsupported as a long one."""
        [finding] = verify_evidence([self._finding(evidence="eval(x)")], self.DIFF, revise_at=5)
        assert finding.severity == 4
        assert finding.description.startswith(UNVERIFIED_EVIDENCE_PREFIX)

    def test_echoed_output_format_placeholder_is_unsupported(self) -> None:
        """The prompt's own example line must not count as shown code."""
        prompt = _build_review_prompt(
            Task(id="1", title="T", body="b", state=TaskState.BACKLOG), "+    x = 1", ["a.py"], addressed_findings=[]
        )
        assert EVIDENCE_PLACEHOLDER in prompt
        [finding] = verify_evidence([self._finding(evidence=EVIDENCE_PLACEHOLDER)], prompt, revise_at=5)
        assert finding.severity == 4
        assert finding.description.startswith(UNVERIFIED_EVIDENCE_PREFIX)

    async def test_review_downgrades_a_finding_citing_code_it_was_not_shown(self, tmp_path: Path) -> None:
        """End to end through ReviewerRole: the hallucinated blocker no longer starts a fix round."""
        ctx = _ctx(tmp_path, pr_number=99)
        finding = {
            "file": "a.py",
            "line": 1,
            "severity": 8,
            "category": "bug",
            "description": "d",
            "evidence": "def refresh_token(session):",
        }
        llm_result = LLMResult(
            text=json.dumps({"findings": [finding], "summary": "s"}), model="m", cost_usd=Decimal("0")
        )
        with (
            patch(
                "sova.roles.reviewer.get_pr_branch_and_head_sha",
                new_callable=AsyncMock,
                return_value=("feat/issue-42", "abc123"),
            ),
            patch("sova.roles.reviewer.get_pr_diff", new_callable=AsyncMock, return_value="diff --git a/a.py\n+x = 1"),
            patch("sova.roles.reviewer.get_pr_files", new_callable=AsyncMock, return_value=["a.py"]),
            patch("sova.roles.reviewer.get_pr_head_sha", new_callable=AsyncMock, return_value="abc123"),
            patch("sova.roles.reviewer.invoke", new_callable=AsyncMock, return_value=llm_result),
            patch("sova.roles.reviewer.write_handoff", new_callable=AsyncMock),
            patch("sova.roles.reviewer.write_handoff_file", new_callable=MagicMock) as handoff,
        ):
            assert (await ReviewerRole().execute(ctx)).success
        details = handoff.call_args.args[1].details
        assert details["next_action"] != "address_review"
        assert details["pending_findings"][0]["severity"] == 2
